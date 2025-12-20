"""
Sarvam Text-to-Speech Synthesizer
Real-time speech synthesis via WebSocket API (docs-compliant)
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional, AsyncGenerator, Dict, Any

import websockets
from websockets.exceptions import InvalidHandshake

from audio_processor import AudioProcessor
from config import Config

logger = logging.getLogger(__name__)


class SarvamSynthesizer:
    """
    Real-time TTS using Sarvam AI WebSocket API.

    Follows official docs:
    - WebSocket endpoint: wss://api.sarvam.ai/text-to-speech/ws
    - Auth header: Api-Subscription-Key
    - Messages:
      - Config: { "type": "config", "data": { ... } }
      - Text:   { "type": "text",   "data": { "text": "..." } }
      - Flush:  { "type": "flush" }
      - Audio:  { "type": "audio",  "data": { "content_type": "...", "audio": "<base64>" } }
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = Config.SYNTHESIZER_MODEL,
        voice: str = Config.SYNTHESIZER_VOICE,
        language: str = Config.SYNTHESIZER_LANGUAGE,
        speed: float = Config.SYNTHESIZER_SPEED,
        pitch: float = Config.SYNTHESIZER_PITCH,
        loudness: float = Config.SYNTHESIZER_LOUDNESS,
        buffer_size: int = Config.SYNTHESIZER_BUFFER_SIZE,
    ):
        self.api_key = api_key or Config.SARVAM_API_KEY
        self.model = model
        self.voice = voice
        self.language = language
        self.speed = speed
        self.pitch = pitch
        self.loudness = loudness
        # clamp buffer_size to 30–200 chars as per docs suggestion range
        self.buffer_size = max(30, min(200, buffer_size))

        # WebSocket config
        self.api_host = Config.SARVAM_API_HOST
        self.ws_url = (
            f"wss://{self.api_host}/text-to-speech/ws"
            f"?model={self.model}&send_completion_event=true"
        )

        # Connection state
        self.websocket = None
        self.is_connected = False
        self.connection_time_ms: Optional[int] = None

        # Audio processing
        self.audio_processor = AudioProcessor()

        # Synthesis state
        self.is_speaking = False

        # Performance tracking
        self.text_chunks_sent = 0
        self.audio_chunks_received = 0
        self.first_audio_latency_ms: Optional[int] = None
        self.turn_start_time: Optional[float] = None

        # Tasks
        self.sender_task: Optional[asyncio.Task] = None
        self.receiver_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None

        # Queues
        self.text_queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue()
        self.audio_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

        # Has config been sent once per connection
        self.config_sent = False

    # -------------------------------------------------------------------------
    # Connection management
    # -------------------------------------------------------------------------
    async def connect(self, retries: int = 3, timeout: float = 10.0) -> bool:
        for attempt in range(retries):
            try:
                start_time = time.perf_counter()
                self.websocket = await asyncio.wait_for(
                    websockets.connect(
                        self.ws_url,
                        extra_headers={"Api-Subscription-Key": self.api_key},
                    ),
                    timeout=timeout,
                )

                # Send config on connect
                await self._send_config()

                self.connection_time_ms = round(
                    (time.perf_counter() - start_time) * 1000
                )
                self.is_connected = True
                logger.info(
                    f"✅ Connected to Sarvam TTS in {self.connection_time_ms}ms"
                )
                return True

            except asyncio.TimeoutError:
                logger.error(
                    f"⏱️ Timeout connecting to Sarvam TTS "
                    f"(attempt {attempt + 1}/{retries})"
                )
            except InvalidHandshake as e:
                msg = str(e)
                if "401" in msg or "403" in msg:
                    logger.error("🔐 Authentication failed: Invalid API key")
                    return False
                elif "404" in msg:
                    logger.error("🔍 Endpoint not found: Check model/config.")
                    return False
                else:
                    logger.error(f"🤝 Handshake failed: {e}")
            except Exception as e:
                logger.error(
                    f"❌ TTS connection error (attempt {attempt + 1}/{retries}): {e}"
                )

            if attempt < retries - 1:
                await asyncio.sleep(2**attempt)

        return False

    async def _send_config(self):
        """
        Send initial configuration to Sarvam TTS.
        Follows streaming docs (target_language_code, speaker, pitch, pace, etc.).
        """
        if not self.websocket:
            return

        config_message = {
            "type": "config",
            "data": {
                "target_language_code": self.language,
                "speaker": self.voice,
                "pitch": self.pitch,
                "pace": self.speed,
                "loudness": self.loudness,
                "min_buffer_size": self.buffer_size,
                "max_chunk_length": 250,
                "output_audio_codec": "wav",
                "output_audio_bitrate": "32k",
            }
        }
        logger.info(
    f"TTS config -> model={self.model}, speaker={self.voice}, "
    f"lang={self.language}, pitch={self.pitch}, pace={self.speed}, loudness={self.loudness}"
)

        await self.websocket.send(json.dumps(config_message))
        self.config_sent = True
        logger.debug("📤 TTS config sent to Sarvam")

    async def _heartbeat(self, interval: float = 10.0):
        try:
            while self.is_connected and self.websocket:
                await asyncio.sleep(interval)
                try:
                    await self.websocket.ping()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    # -------------------------------------------------------------------------
    # Lifecycle used by VoiceAgent
    # -------------------------------------------------------------------------
    async def start(self):
        if not self.is_connected:
            ok = await self.connect()
            if not ok:
                raise ConnectionError("Failed to connect to Sarvam TTS")

        self.sender_task = asyncio.create_task(self._sender())
        self.receiver_task = asyncio.create_task(self._receiver())
        self.heartbeat_task = asyncio.create_task(self._heartbeat())

        logger.info("✅ TTS synthesis tasks started")

    async def stop(self):
        logger.info("🛑 Stopping synthesizer")

        self.is_connected = False
        await self.text_queue.put(None)  # stop signal

        for task in [self.sender_task, self.receiver_task, self.heartbeat_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
            self.websocket = None

        logger.info("✅ Synthesizer stopped")

    # -------------------------------------------------------------------------
    # Sending text
    # -------------------------------------------------------------------------
    async def synthesize(self, text: str, flush: bool = True):
        """
        Called by VoiceAgent.speak()

        text: text to synthesize
        flush: whether to send a flush signal after the text (end of utterance)
        """
        await self.text_queue.put({"text": text, "flush": flush})

    async def _sender(self):
        try:
            while self.is_connected and self.websocket:
                try:
                    item = await asyncio.wait_for(
                        self.text_queue.get(), timeout=5.0
                    )
                    if item is None:
                        # stop
                        break

                    text = item.get("text", "")
                    flush = item.get("flush", True)

                    if not text:
                        continue

                    # track start of synthesis
                    if self.text_chunks_sent == 0:
                        self.turn_start_time = time.perf_counter()
                        self.is_speaking = True

                    text_message = {
                        "type": "text",
                        "data": {"text": text},
                    }
                    await self.websocket.send(json.dumps(text_message))
                    self.text_chunks_sent += 1

                    logger.debug(f"📤 TTS text sent: {text[:60]}")

                    if flush:
                        flush_message = {"type": "flush"}
                        await self.websocket.send(json.dumps(flush_message))
                        logger.debug("📤 TTS flush sent")

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"❌ TTS sender error: {e}")
                    break

        except asyncio.CancelledError:
            logger.info("🛑 TTS sender task cancelled")
        finally:
            logger.info(
                f"📤 TTS sender finished "
                f"({self.text_chunks_sent} text chunks sent)"
            )

    # -------------------------------------------------------------------------
    # Receiving audio
    # -------------------------------------------------------------------------
    async def _receiver(self):
        """
        Receives:
        - { "type": "audio", "data": { "content_type": "...", "audio": "<base64>" } }
        - { "type": "event", ... } / completion events
        - { "type": "error", ... }
        """
        try:
            while self.is_connected and self.websocket:
                try:
                    message = await asyncio.wait_for(
                        self.websocket.recv(), timeout=30.0
                    )
                    data = json.loads(message)
                    msg_type = data.get("type")

                    if msg_type == "audio":
                        inner = data.get("data") or {}
                        audio_b64 = inner.get("audio")

                        if not audio_b64:
                            continue

                        wav_bytes = base64.b64decode(audio_b64)

                        # first audio latency
                        if (
                            self.audio_chunks_received == 0
                            and self.turn_start_time
                        ):
                            self.first_audio_latency_ms = round(
                                (
                                    time.perf_counter()
                                    - self.turn_start_time
                                )
                                * 1000
                            )
                            logger.info(
                                "⚡ First TTS audio in "
                                f"{self.first_audio_latency_ms}ms"
                            )

                    # Extract PCM + actual sample rate from WAV
                        pcm_data, sample_rate = self.audio_processor.wav_to_pcm(wav_bytes)

                        # Resample from actual sample rate → 8kHz (telephony)
                        pcm_8k = self.audio_processor.resample_audio(
                            pcm_data,
                            from_rate=sample_rate,
                            to_rate=8000,
                            sample_width=2,  # 16-bit
                        )

                        # Convert 16-bit PCM → μ-law for Twilio
                        mulaw_8k = self.audio_processor.pcm16_to_mulaw(pcm_8k)

                        await self.audio_queue.put(
                            {
                               "type": "audio",
                               "data": mulaw_8k,
                               "timestamp": time.time(),
                           }
                        )
                       
                        self.audio_chunks_received += 1

                    elif msg_type == "event":
                        # completion events etc
                        logger.debug(f"📩 TTS event: {data}")
                        # stop speaking when final event arrives
                        event_data = data.get("data") or {}
                        if event_data.get("event_type") == "final":
                            self.is_speaking = False

                    elif msg_type == "error":
                        logger.error(f"❌ TTS error from Sarvam: {data}")

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    logger.error(f"❌ TTS JSON decode error: {e}")
                except Exception as e:
                    logger.error(f"❌ TTS receiver error: {e}")
                    break

        except asyncio.CancelledError:
            logger.info("🛑 TTS receiver task cancelled")
        finally:
            self.is_speaking = False
            logger.info(
                f"📥 TTS receiver finished "
                f"({self.audio_chunks_received} audio chunks)"
            )
    
    # async def _receiver(self):
    #     """
    #     Receives:
    #     - { "type": "audio", "data": { "content_type": "...", "audio": "<base64>" } }
    #     - { "type": "event", ... } / completion events
    #     - { "type": "error", ... }
    #     """
    #     try:
    #         while self.is_connected and self.websocket:
    #             try:
    #                 message = await asyncio.wait_for(
    #                     self.websocket.recv(), timeout=30.0
    #                 )
    #                 data = json.loads(message)
    #                 msg_type = data.get("type")

    #                 if msg_type == "audio":
    #                     inner = data.get("data") or {}
    #                     audio_b64 = inner.get("audio")

    #                     if not audio_b64:
    #                         continue

    #                     wav_bytes = base64.b64decode(audio_b64)

    #                     # first audio latency
    #                     if (
    #                         self.audio_chunks_received == 0
    #                         and self.turn_start_time
    #                     ):
    #                         self.first_audio_latency_ms = round(
    #                             (
    #                                 time.perf_counter()
    #                                 - self.turn_start_time
    #                             )
    #                             * 1000
    #                         )
    #                         logger.info(
    #                             "⚡ First TTS audio in "
    #                             f"{self.first_audio_latency_ms}ms"
    #                         )

    #                     # Extract PCM + actual sample rate from WAV
    #                     pcm_data, sample_rate = self.audio_processor.wav_to_pcm(wav_bytes)

    #                     # Resample from actual sample rate → 8kHz (telephony)
    #                     pcm_8k = self.audio_processor.resample_audio(
    #                         pcm_data,
    #                         from_rate=sample_rate,
    #                         to_rate=8000,
    #                         sample_width=2,  # 16-bit
    #                     )

    #                     # Convert 16-bit PCM → μ-law for Twilio
    #                     mulaw_8k = self.audio_processor.pcm16_to_mulaw(pcm_8k)

    #                     # Twilio requires 20ms μ-law frames
    #                     FRAME_SIZE = 160  # 160 bytes = 20ms @ 8kHz μ-law

    #                     for i in range(0, len(mulaw_8k), FRAME_SIZE):
    #                         frame = mulaw_8k[i:i + FRAME_SIZE]

    #                         # Only send complete frames
    #                         if len(frame) == FRAME_SIZE:
    #                             await self.audio_queue.put(
    #                                 {
    #                                     "type": "audio",
    #                                     "data": frame,
    #                                     "timestamp": time.time(),
    #                                 }
    #                             )

    #                     self.audio_chunks_received += 1

    #                 elif msg_type == "event":
    #                     # completion events etc
    #                     logger.debug(f"📩 TTS event: {data}")
    #                     # stop speaking when final event arrives
    #                     event_data = data.get("data") or {}
    #                     if event_data.get("event_type") == "final":
    #                         self.is_speaking = False

    #                 elif msg_type == "error":
    #                     logger.error(f"❌ TTS error from Sarvam: {data}")

    #             except asyncio.TimeoutError:
    #                 continue
    #             except json.JSONDecodeError as e:
    #                 logger.error(f"❌ TTS JSON decode error: {e}")
    #             except Exception as e:
    #                 logger.error(f"❌ TTS receiver error: {e}")
    #                 break

    #     except asyncio.CancelledError:
    #         logger.info("🛑 TTS receiver task cancelled")
    #     finally:
    #         self.is_speaking = False
    #         logger.info(
    #             f"📥 TTS receiver finished "
    #             f"({self.audio_chunks_received} audio chunks)"
    #         )
    #     # -------------------------------------------------------------------------
    # # Consumption API for VoiceAgent
    # # -------------------------------------------------------------------------
    async def get_audio(self, timeout: Optional[float] = None) -> Optional[bytes]:
        try:
            if timeout:
                item = await asyncio.wait_for(
                    self.audio_queue.get(), timeout=timeout
                )
            else:
                item = await self.audio_queue.get()

            return item.get("data") if item else None
        except asyncio.TimeoutError:
            return None

    async def audio_stream(self) -> AsyncGenerator[bytes, None]:
        while self.is_connected or not self.audio_queue.empty():
            audio = await self.get_audio(timeout=1.0)
            if audio:
                yield audio
            elif not self.is_connected:
                break

    async def interrupt(self ):
        """
        Interrupt current synthesis:
        - clear pending text
        - clear pending audio
        """
        logger.info("🛑 Interrupting TTS synthesis")
      
       
        while not self.text_queue.empty():
             try:
                self.text_queue.get_nowait()
             except asyncio.QueueEmpty:
                break
        while not self.audio_queue.empty():
             try:
                self.audio_queue.get_nowait()
             except asyncio.QueueEmpty:
                break

        

        # while not self.audio_queue.empty():
        #     try:
        #         self.audio_queue.get_nowait()
        #     except asyncio.QueueEmpty:
        #         break

      
        self.is_speaking = False
        self.text_chunks_sent = 0
        self.audio_chunks_received = 0
        self.turn_start_time = None

        logger.info("✅ TTS synthesis interrupted & cleared")

    def get_stats(self) -> Dict[str, Any]:
        return {
            "connection_time_ms": self.connection_time_ms,
            "text_chunks_sent": self.text_chunks_sent,
            "audio_chunks_received": self.audio_chunks_received,
            "first_audio_latency_ms": self.first_audio_latency_ms,
            "is_speaking": self.is_speaking,
        }
