"""
Voice Agent Orchestrator
Manages conversation flow, interruption handling, and component integration
"""
import asyncio
import logging
import time
import requests
from typing import Optional, Dict, Any, List
from openai import AsyncAzureOpenAI

from config import Config, SYSTEM_PROMPT
from sarvam_transcriber import SarvamTranscriber
from sarvam_synthesizer import SarvamSynthesizer
from audio_processor import AudioProcessor

from hiring_workflow import (
    get_hiring_system_prompt,
    is_interview_finished,
    get_first_question
)
from sales_workflow import (
    get_sales_system_prompt,
    get_sales_first_question,
    is_sales_workflow_complete
)

logger = logging.getLogger(__name__)


class VoiceAgent:
    """
    Main voice agent orchestrator
    """

    def __init__(self, call_sid: str, stream_sid: str, websocket, workflow_type: str, workflow_data: dict):
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.ws = websocket

        # Conversation
        self.conversation: List[Dict[str, str]] = []
        self.transcript: List[Dict[str, str]] = []   # ✅ ADDED

        self.max_questions = Config.MAX_QUESTIONS

        self.transcriber: Optional[SarvamTranscriber] = None
        self.synthesizer: Optional[SarvamSynthesizer] = None
        self.audio_processor = AudioProcessor()

        self.openai_client = AsyncAzureOpenAI(
            api_key=Config.AZURE_OPENAI_API_KEY,
            api_version=Config.AZURE_OPENAI_VERSION,
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
        )

        self.is_speaking = False
        self.awaiting_response = False
        self.user_is_speaking = False
        self.conversation_ended = False

        self.transcription_handler_task = None
        self.synthesis_handler_task = None

        self.call_start_time = time.time()
        self.total_transcripts = 0
        self.total_responses = 0

        self.last_activity = time.time()
        self.idle_timeout = Config.IDLE_TIMEOUT_SECONDS
        self.idle_task = None
        self.auto_hangup_enabled = True

        self.workflow_type = workflow_type
        self.workflow_data = workflow_data
        self.question_number = 0

        self.resume_text = workflow_data.get("resume_text", "")
        self.jd_text = workflow_data.get("job_description_text", "")
        self.candidate_name = workflow_data.get("candidate_name", "Candidate")
        self.chat_id = workflow_data.get("chat_id")   # ✅ USED FOR WEBHOOK

        logger.info(f"🎬 Agent initialized for call {call_sid}")

    def _load_system_prompt(self):
        if self.workflow_type == "hiring":
            return get_hiring_system_prompt(
                self.candidate_name,
                self.resume_text,
                self.jd_text
            )
        if self.workflow_type == "sales":
            return get_sales_system_prompt(
                self.workflow_data.get("company_name", "Our Company"),
                self.workflow_data.get("product_name", "our product")
            )
        return "You are a helpful assistant."

    async def initialize(self):
        self.transcriber = SarvamTranscriber()
        await self.transcriber.start()

        self.synthesizer = SarvamSynthesizer()
        await self.synthesizer.start()

        self.transcription_handler_task = asyncio.create_task(self._handle_transcriptions())
        self.synthesis_handler_task = asyncio.create_task(self._handle_synthesis())
        self.idle_task = asyncio.create_task(self._monitor_idle_timeout())

    async def process_audio(self, audio_payload: str):
        if self.transcriber and not self.conversation_ended:
            import base64
            audio_bytes = base64.b64decode(audio_payload)
            await self.transcriber.send_audio(audio_bytes)

    async def _handle_transcriptions(self):
        async for event in self.transcriber.transcripts():
            if self.conversation_ended:
                break

            if event.get("type") == "transcript":
                text = event.get("text", "").strip()
                if not text or not event.get("is_final"):
                    continue

                logger.info(f"📝 User: {text}")
                self.total_transcripts += 1
                self.last_activity = time.time()

                self.conversation.append({"role": "user", "content": text})
                self.transcript.append({"speaker": "candidate", "text": text})  # ✅ ADDED

                if self.awaiting_response:
                    self.awaiting_response = False
                    asyncio.create_task(self._generate_response())

    async def _handle_synthesis(self):
        async for audio_chunk in self.synthesizer.audio_stream():
            if self.conversation_ended:
                break
            await self._stream_audio_to_twilio(audio_chunk)

    async def _generate_response(self):
        messages = [{"role": "system", "content": self._load_system_prompt()}] + self.conversation
        response_text = ""

        stream = await self.openai_client.chat.completions.create(
            model=Config.AZURE_OPENAI_DEPLOYMENT,
            messages=messages,
            stream=True,
            temperature=0.7,
            max_tokens=150,
        )

        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta:
                content = getattr(chunk.choices[0].delta, "content", None)
                if content:
                    response_text += content

        if not response_text:
            return

        logger.info(f"💬 Assistant: {response_text}")

        self.conversation.append({"role": "assistant", "content": response_text})
        self.transcript.append({"speaker": "assistant", "text": response_text})  # ✅ ADDED
        self.total_responses += 1
        self.question_number += 1

        if self.workflow_type == "hiring" and is_interview_finished(self.question_number):
            await self.speak("Thank you, an HR representative will contact you soon.")
            await self.end_call()
            return

        await self.speak(response_text)
        self.awaiting_response = True

    async def speak(self, text: str):
        self.is_speaking = True
        self.last_activity = time.time()
        await self.synthesizer.synthesize(text, flush=True)

    async def end_call(self):
        await self.speak("धन्यवाद! AurJobs AI से बात करने के लिए शुक्रिया।")
        await asyncio.sleep(3)
        await self._hangup_twilio()
        await self.cleanup()

    async def cleanup(self):
        logger.info("🧹 Cleaning up agent resources")

        self.conversation_ended = True

        for task in [self.transcription_handler_task, self.synthesis_handler_task, self.idle_task]:
            if task and not task.done():
                task.cancel()

        if self.transcriber:
            await self.transcriber.stop()
        if self.synthesizer:
            await self.synthesizer.stop()

        # ✅ SEND WEBHOOK (ONLY ADDITION)
        try:
            payload = {
                "call_sid": self.call_sid,
                "chat_id": self.chat_id,
                "transcript": self.transcript,
                "duration": int(time.time() - self.call_start_time)
            }

            requests.post(
                Config.CALL_RESULT_WEBHOOK_URL,
                json=payload,
                timeout=5
            )

            logger.info("📤 Call result webhook sent")

        except Exception as e:
            logger.error(f"❌ Webhook send failed: {e}")

        logger.info("✅ Cleanup complete")

    async def _stream_audio_to_twilio(self, audio_data: bytes):
        import base64
        audio_b64 = base64.b64encode(audio_data).decode()
        await self.ws.send_json({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": audio_b64}
        })

    async def _hangup_twilio(self):
        from twilio.rest import Client
        client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
        client.calls(self.call_sid).update(status="completed")
