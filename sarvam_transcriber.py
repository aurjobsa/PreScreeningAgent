"""
Sarvam Speech-to-Text Transcriber
Real-time transcription via WebSocket API (docs-compliant)
"""

import asyncio
import base64
import io
import json
import logging
import time
import wave
from typing import Optional, AsyncGenerator, Dict, Any

import websockets
from websockets.exceptions import InvalidHandshake

from audio_processor import AudioProcessor
from config import Config

logger = logging.getLogger(__name__)


class SarvamTranscriber:
    """
    Real-time speech transcription using Sarvam AI WebSocket API.

    Follows official docs:
    - WebSocket endpoint: wss://api.sarvam.ai/speech-to-text/ws
    - Auth header: Api-Subscription-Key
    - Audio message shape:
      {
        "audio": {
          "data": "<base64 wav>",
          "sample_rate": "16000",
          "encoding": "audio/wav"
        }
      }
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = Config.TRANSCRIBER_MODEL,
        language: str = Config.TRANSCRIBER_LANGUAGE,
        high_vad_sensitivity: bool = True,
        vad_signals: bool = True,
        chunk_duration_ms: int = 400,  # how much audio to batch per STT send
    ):
        self.api_key = api_key or Config.SARVAM_API_KEY
        self.model = model
        self.language = language
        self.high_vad_sensitivity = high_vad_sensitivity
        self.vad_signals = vad_signals
        self.chunk_duration_ms = chunk_duration_ms

        # WebSocket config
        self.api_host = Config.SARVAM_API_HOST
        self.ws_url = self._build_ws_url()

        # Connection state
        self.websocket = None
        self.is_connected = False
        self.connection_time_ms: Optional[int] = None

        # Audio processing
        self.audio_processor = AudioProcessor()
        self._pcm_buffer = b""  # PCM 16kHz mono
        # bytes per ms for 16kHz, 16-bit mono: 16000 samples/s * 2 bytes / 1000ms
        self._bytes_per_ms = int(Config.SARVAM_SAMPLE_RATE * 2 / 1000)

        # Performance tracking
        self.audio_chunks_sent = 0
        self.transcripts_received = 0
        self.first_transcript_latency_ms: Optional[int] = None
        self.turn_start_time: Optional[float] = None

        # Tasks
        self.sender_task: Optional[asyncio.Task] = None
        self.receiver_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None

        # Queues
        self.audio_queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self.transcript_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

    # -------------------------------------------------------------------------
    # WebSocket URL construction (per API reference)
    # -------------------------------------------------------------------------
    def _build_ws_url(self) -> str:
        base_url = f"wss://{self.api_host}/speech-to-text/ws"

        params = {
            "language-code": self.language,
            "model": self.model,
            "sample_rate": str(Config.SARVAM_SAMPLE_RATE),  # 16000
            "input_audio_codec": "wav",  # we send WAV chunks
        }

        if self.high_vad_sensitivity:
            params["high_vad_sensitivity"] = "true"

        if self.vad_signals:
            params["vad_signals"] = "true"

        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{base_url}?{query}"

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

                self.connection_time_ms = round(
                    (time.perf_counter() - start_time) * 1000
                )
                self.is_connected = True
                logger.info(
                    f"✅ Connected to Sarvam STT in {self.connection_time_ms}ms"
                )
                return True

            except asyncio.TimeoutError:
                logger.error(
                    f"⏱️ Timeout connecting to Sarvam STT "
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
                    f"❌ Connection error (attempt {attempt + 1}/{retries}): {e}"
                )

            if attempt < retries - 1:
                await asyncio.sleep(2**attempt)

        return False

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
    # Public lifecycle API (used by VoiceAgent)
    # -------------------------------------------------------------------------
    async def start(self):
        if not self.is_connected:
            ok = await self.connect()
            if not ok:
                raise ConnectionError("Failed to connect to Sarvam STT")

        self.sender_task = asyncio.create_task(self._sender())
        self.receiver_task = asyncio.create_task(self._receiver())
        self.heartbeat_task = asyncio.create_task(self._heartbeat())

        logger.info("✅ Transcription tasks started")

    async def stop(self):
        logger.info("🛑 Stopping transcriber")

        self.is_connected = False
        await self.audio_queue.put(None)  # stop signal

        # cancel tasks
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

        logger.info("✅ Transcriber stopped")

    # -------------------------------------------------------------------------
    # Sending audio (Twilio μ-law 8k → PCM 16k → WAV → JSON)
    # -------------------------------------------------------------------------
    async def send_audio(self, audio_data: bytes):
        """
        Called by VoiceAgent.process_audio().

        audio_data: μ-law 8kHz audio bytes from Twilio.
        """
        await self.audio_queue.put(audio_data)

    def _pcm16_to_wav(self, pcm_data: bytes, sample_rate: int) -> bytes:
        """Wrap raw PCM 16-bit mono into a WAV container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return buf.getvalue()

    async def _flush_buffer_to_sarvam(self):
        """Send buffered PCM as one STT audio message to Sarvam."""
        if not self._pcm_buffer or not self.websocket:
            return

        wav_bytes = self._pcm16_to_wav(
            self._pcm_buffer, Config.SARVAM_SAMPLE_RATE
        )
        audio_b64 = base64.b64encode(wav_bytes).decode("ascii")

        msg = {
            "audio": {
                "data": audio_b64,
                "sample_rate": str(Config.SARVAM_SAMPLE_RATE),
                "encoding": "audio/wav",
            }
        }

        await self.websocket.send(json.dumps(msg))
        self.audio_chunks_sent += 1

        if self.audio_chunks_sent == 1:
            self.turn_start_time = time.perf_counter()

        logger.debug(
            f"📤 Sent STT audio chunk "
            f"({len(self._pcm_buffer)} bytes PCM -> WAV)"
        )
        self._pcm_buffer = b""

    async def _sender(self):
        try:
            min_bytes = self._bytes_per_ms * self.chunk_duration_ms

            while self.is_connected and self.websocket:
                try:
                    mulaw = await asyncio.wait_for(
                        self.audio_queue.get(), timeout=5.0
                    )
                    if mulaw is None:
                        # final flush and exit
                        await self._flush_buffer_to_sarvam()
                        break

                    # Twilio μ-law 8k → PCM16 16k
                    pcm_16k = self.audio_processor.mulaw_8k_to_pcm16_16k(mulaw)
                    self._pcm_buffer += pcm_16k

                    if len(self._pcm_buffer) >= min_bytes:
                        await self._flush_buffer_to_sarvam()

                except asyncio.TimeoutError:
                    # nothing new; if we have some buffered audio, flush it
                    if self._pcm_buffer:
                        await self._flush_buffer_to_sarvam()
                    continue
                except Exception as e:
                    logger.error(f"❌ STT sender error: {e}")
                    break

        except asyncio.CancelledError:
            logger.info("🛑 STT sender task cancelled")
        finally:
            logger.info(
                f"📤 STT sender finished "
                f"({self.audio_chunks_sent} chunks sent)"
            )

    # -------------------------------------------------------------------------
    # Receiving transcripts
    # -------------------------------------------------------------------------
    async def _receiver(self):
        """
        Handle responses from Sarvam.

        Supports:
        - API ref style:
          { "type": "data", "data": { "transcript": "...", ... } }
        - Streaming guide style:
          { "type": "transcript", "text": "..." }
          { "type": "speech_start" } / { "type": "speech_end" }
        """
        try:
            while self.is_connected and self.websocket:
                try:
                    message = await asyncio.wait_for(
                        self.websocket.recv(), timeout=30.0
                    )
                    data = json.loads(message)

                    msg_type = data.get("type")

                    # 1) Transcript messages
                    transcript_text: Optional[str] = None

                    if msg_type == "data":
                        inner = data.get("data") or {}
                        transcript_text = inner.get("transcript") or inner.get(
                            "text"
                        )
                    elif msg_type in ("transcript", "speech_transcript"):
                        transcript_text = data.get("text") or data.get(
                            "transcript"
                        )

                    if transcript_text:
                        transcript_text = transcript_text.strip()
                        if transcript_text:
                            self.transcripts_received += 1

                            if (
                                self.transcripts_received == 1
                                and self.turn_start_time
                            ):
                                self.first_transcript_latency_ms = round(
                                    (
                                        time.perf_counter()
                                        - self.turn_start_time
                                    )
                                    * 1000
                                )
                                logger.info(
                                    "⚡ First transcript in "
                                    f"{self.first_transcript_latency_ms}ms"
                                )

                            await self.transcript_queue.put(
                                {
                                    "type": "transcript",
                                    "text": transcript_text,
                                    "is_final": True,  # Sarvam generally sends final in this style
                                    "timestamp": time.time(),
                                }
                            )
                            logger.info(f"📝 Final: {transcript_text}")
                            continue

                    # 2) VAD / speech signals (from streaming guide)
                    if msg_type in ("speech_start", "START_SPEECH"):
                        await self.transcript_queue.put(
                            {
                                "type": "vad",
                                "signal": "START_SPEECH",
                                "timestamp": time.time(),
                            }
                        )
                        logger.debug("🎤 Speech started")
                        self.turn_start_time = time.perf_counter()
                        continue

                    if msg_type in ("speech_end", "END_SPEECH"):
                        await self.transcript_queue.put(
                            {
                                "type": "vad",
                                "signal": "END_SPEECH",
                                "timestamp": time.time(),
                            }
                        )
                        logger.debug("🔇 Speech ended")
                        continue

                    # 3) Error messages
                    if msg_type == "error":
                        logger.error(f"❌ STT error from Sarvam: {data}")
                        continue

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    logger.error(f"❌ STT JSON decode error: {e}")
                except Exception as e:
                    logger.error(f"❌ STT receiver error: {e}")
                    break

        except asyncio.CancelledError:
            logger.info("🛑 STT receiver task cancelled")
        finally:
            logger.info(
                f"📥 STT receiver finished "
                f"({self.transcripts_received} transcripts)"
            )

    # -------------------------------------------------------------------------
    # Public consumption API for VoiceAgent
    # -------------------------------------------------------------------------
    async def get_transcript(
        self, timeout: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        try:
            if timeout:
                return await asyncio.wait_for(
                    self.transcript_queue.get(), timeout=timeout
                )
            return await self.transcript_queue.get()
        except asyncio.TimeoutError:
            return None

    async def transcripts(self) -> AsyncGenerator[Dict[str, Any], None]:
        while self.is_connected:
            event = await self.get_transcript(timeout=1.0)
            if event:
                yield event

    def get_stats(self) -> Dict[str, Any]:
        return {
            "connection_time_ms": self.connection_time_ms,
            "audio_chunks_sent": self.audio_chunks_sent,
            "transcripts_received": self.transcripts_received,
            "first_transcript_latency_ms": self.first_transcript_latency_ms,
        }
