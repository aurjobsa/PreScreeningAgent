"""
Voice Agent Orchestrator
Manages conversation flow, interruption handling, and component integration
"""
import asyncio
import logging
import time
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
    
    Manages:
    - Conversation state and history
    - Transcriber and synthesizer lifecycle
    - Turn-taking and interruption handling
    - LLM integration for response generation
    - Twilio WebSocket communication
    """
    
    def __init__(self, call_sid: str, stream_sid: str, websocket, workflow_type: str, workflow_data: dict):
        """
        Initialize voice agent
        
        Args:
            call_sid: Twilio call SID
            stream_sid: Twilio stream SID
            websocket: FastAPI WebSocket connection
        """
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.ws = websocket
        
        # Conversation state
        self.conversation: List[Dict[str, str]] = []
        # self.questions_asked = 0
        # self.question_number = 0
        self.max_questions = Config.MAX_QUESTIONS
        
        # Component initialization
        self.transcriber: Optional[SarvamTranscriber] = None
        self.synthesizer: Optional[SarvamSynthesizer] = None
        self.audio_processor = AudioProcessor()
        
        self.workflow_run_id = workflow_data.get("workflow_run_id")
        
        # LLM client
        self.openai_client = AsyncAzureOpenAI(
            api_key=Config.AZURE_OPENAI_API_KEY,
            api_version=Config.AZURE_OPENAI_VERSION,
            azure_endpoint=Config.AZURE_OPENAI_ENDPOINT,
        )
        
        # State flags
        self.is_speaking = False
        self.awaiting_response = False
        self.user_is_speaking = False
        self.conversation_ended = False
        
        # Tasks
        self.transcription_handler_task: Optional[asyncio.Task] = None
        self.synthesis_handler_task: Optional[asyncio.Task] = None
        
        # Performance tracking
        self.call_start_time = time.time()
        self.total_transcripts = 0
        self.total_responses = 0
        
        # Auto-hangup / idle detection
        self.last_activity = time.time()  # last time user or assistant spoke
        self.idle_timeout = Config.IDLE_TIMEOUT_SECONDS
        self.idle_task: Optional[asyncio.Task] = None
        self.auto_hangup_enabled = True
        
        
        #hiring workflow stateself.workflow_type = workflow_type
        # self.workflow_data = workflow_data
        self.workflow_type = workflow_type              # <-- FIXED
        self.workflow_data = workflow_data
        self.question_number = 0
        self.resume_text = workflow_data.get("resume_text", "")
        self.jd_text = workflow_data.get("job_description_text", "")
        self.candidate_name = workflow_data.get("candidate_name", "Candidate")
        self.workflow_run_id = workflow_data.get("workflow_run_id")
        
        self.transcript = []          # for final transcript
        self.chat_id = workflow_data.get("chat_id")
        
        self.webhook_sent = False
        self.latest_user_utterance = None
        self.user_speaking = False
        self.response_task: Optional[asyncio.Task] = None

        logger.info(f"workflow_data-----------------------------: {self.workflow_data}")

        logger.info(f"🎬 Agent initialized for call {call_sid}")
        
        # ---- TURN MANAGEMENT (Bolna-style) ----
        self.processing_turn = False
        self.last_turn_id = None
        self.last_response_time = 0

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
        """Initialize transcriber and synthesizer"""
        try:
            # Initialize transcriber
            self.transcriber = SarvamTranscriber()
            await self.transcriber.start()
            logger.info("✅ Transcriber initialized")
            
            # Initialize synthesizer
            self.synthesizer = SarvamSynthesizer()
            await self.synthesizer.start()
            logger.info("✅ Synthesizer initialized")
            
            # Start handlers
            self.transcription_handler_task = asyncio.create_task(self._handle_transcriptions())
            self.synthesis_handler_task = asyncio.create_task(self._handle_synthesis())
            self.idle_task = asyncio.create_task(self._monitor_idle_timeout())
            
            logger.info("✅ Agent fully initialized")
            
        except Exception as e:
            logger.error(f"❌ Initialization error: {e}")
            raise
    
    async def process_audio(self, audio_payload: str):
        """
        Process incoming audio from Twilio
        
        Args:
            audio_payload: Base64-encoded μ-law audio from Twilio
        """
        if self.transcriber and not self.conversation_ended:
            import base64
            audio_bytes = base64.b64decode(audio_payload)
            await self.transcriber.send_audio(audio_bytes)
    
    async def _handle_transcriptions(self):
        """Handle incoming transcriptions and VAD events"""
        try:
            async for event in self.transcriber.transcripts():
                if self.conversation_ended:
                    break
                
                event_type = event.get("type")
                
                if event_type == "transcript":
                    text = event.get("text", "").strip()
                    is_final = event.get("is_final", False)
                    
                    if not text:
                        continue
                    
                    # Log partials but only process finals
                    if not is_final:
                        logger.debug(f"🎤 (partial) {text}")
                        continue
                    
                    logger.info(f"📝 User: {text}")
                    self.total_transcripts += 1
                    self.user_is_speaking = False
                    
                    # Update activity timestamp
                    self.last_activity = time.time()
                    
                    # Handle interruption if bot was speaking
                    if self.is_speaking and len(text) >= Config.INTERRUPTION_MIN_LENGTH:
                        logger.warning("🚨 User interrupted bot")
                        await self._handle_interruption()
                    
                    # Add to conversation
                    self.conversation.append({"role": "user", "content": text})
                    #add to transcript
                    self.transcript.append({"speaker": "candidate", "text": text})
                    
                    if self.processing_turn:
                        logger.debug("⏳ Still processing previous turn, ignoring this utterance for response generation")
                        continue
                    
                    
                    if self.awaiting_response:
                        self.awaiting_response = False
                         # 🔥 Cancel older response task if exists
                        if self.response_task and not self.response_task.done():
                          self.response_task.cancel()

                        self.response_task = asyncio.create_task(self._generate_response())
                        # asyncio.create_task(self._generate_response())
                
                elif event_type == "vad":
                    signal = event.get("signal")
                    
                    if signal == "START_SPEECH":
                        self.user_is_speaking = True
                        logger.debug("🎤 Speech detected")
                    
                    elif signal == "END_SPEECH":
                        self.user_is_speaking = False
                        logger.debug("🔇 Speech ended")
                        
        except Exception as e:
            logger.error(f"❌ Transcription handler error: {e}")
    
    async def _handle_synthesis(self):
        """Stream synthesized audio to Twilio"""
        try: 
            FRAME_DURATION = 0.02
            async for audio_chunk in self.synthesizer.audio_stream():
                if self.conversation_ended:
                    break
                
                # Send to Twilio
                await self._stream_audio_to_twilio(audio_chunk)
                 # 🔴 CRITICAL: pace audio to prevent Twilio starvation
                await asyncio.sleep(FRAME_DURATION)

                
        except Exception as e:
            logger.error(f"❌ Synthesis handler error: {e}")
    
    async def _handle_interruption(self):
        """Handle user interruption of bot speech"""
        try:
            self.is_speaking = False
             # 🔥 CANCEL pending LLM response
            if self.response_task and not self.response_task.done():
               self.response_task.cancel()
               self.response_task = None
            # Interrupt synthesizer
            if self.synthesizer:
                await self.synthesizer.interrupt()
            
            # Send clear command to Twilio to stop playback
            await self._send_twilio_clear()
            
            logger.info("✅ Interruption handled")
            
        except Exception as e:
            logger.error(f"❌ Interruption handling error: {e}")
    
    async def _monitor_idle_timeout(self):
        """Monitor for idle timeout and auto-hangup if no activity"""
        try:
            while not self.conversation_ended and self.auto_hangup_enabled:
                await asyncio.sleep(5)  # Check every 5 seconds
                
                idle_duration = time.time() - self.last_activity
                
                if idle_duration >= self.idle_timeout:
                    logger.warning(
                        f"⏰ Idle timeout reached ({idle_duration:.1f}s). Auto-hanging up."
                    )
                    await self.end_call()
                    break
                    
        except asyncio.CancelledError:
            logger.info("🛑 Idle monitor task cancelled")
        except Exception as e:
            logger.error(f"❌ Idle monitor error: {e}")
    
   
    async def _generate_response(self):
        """Generate AI response using Azure OpenAI"""
        # if self.questions_asked >= self.max_questions:
        #     await self.end_call()
        #     return
        
        try:
            logger.info("🤖 Generating response...")
            
            # messages = [{"role": "system", "content": SYSTEM_PROMPT}] + self.conversation
            messages = [
                       {"role": "system", "content": self._load_system_prompt()}
                     ] + self.conversation

            
            # Stream response from OpenAI
            response_text = ""
            
            stream = await self.openai_client.chat.completions.create(
                model=Config.AZURE_OPENAI_DEPLOYMENT,
                messages=messages,
                stream=True,
                temperature=0.7,
                max_tokens=150,
            )
            
            async for chunk in stream:
                if self.conversation_ended:
                    break

                # 🔐 Some chunks may have no choices (control / final chunks)
                if not getattr(chunk, "choices", None):
                    continue
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # Extra safety: delta itself can be None in weird cases
                if not delta:
                    continue

                # Typical content field
                content_piece = getattr(delta, "content", None)
                if content_piece:
                    response_text += content_piece
            
            if not response_text:
                logger.warning("⚠️ Empty response from LLM")
                return
            
            logger.info(f"💬 Assistant: {response_text}")
            
            # ✅ ADD THIS LINE
            self.transcript.append({"speaker": "assistant", "text": response_text})

            
            # Check for hangup signal from LLM
            if "HANGUP_NOW" in response_text:
                logger.info("🛑 LLM detected hangup intent, ending call")
                await self.end_call()
                return
            
            # Add to conversation
            self.conversation.append({"role": "assistant", "content": response_text})
            # self.questions_asked += 1
            self.total_responses += 1
            # Increase assistant question count
            self.question_number += 1

            # Hiring interview ends automatically
            if self.workflow_type == "hiring" and is_interview_finished(self.question_number):

                closing = "Thank you, an HR representative will contact you soon."
                await self.speak(closing)
                self.conversation.append({"role": "assistant", "content": closing})
                await self.end_call()
                return
            # Sales workflow auto-stop
            if self.workflow_type == "sales":
                disinterest_count = self.workflow_data.get("disinterest_count", 0)

                # detect customer disinterest
                if "not interested" in response_text.lower() or "no thanks" in response_text.lower():
                    disinterest_count += 1
                    self.workflow_data["disinterest_count"] = disinterest_count

                if is_sales_workflow_complete(self.question_number, disinterest_count):
                    closing = "Thank you for your time! I will send you the details shortly."
                    await self.speak(closing)
                    self.conversation.append({"role": "assistant", "content": closing})
                    await self.end_call()
                    return

            # Speak the response
            await self.speak(response_text)
            
            # Wait for user response
            self.awaiting_response = True
            
        except Exception as e:
            logger.error(f"❌ Response generation error: {e}")
            import traceback
            traceback.print_exc()
       

    async def speak(self, text: str):
        """
        Speak text via synthesizer
        
        Args:
            text: Text to speak
        """
        try:
            self.is_speaking = True
            
            # Update activity timestamp
            self.last_activity = time.time()
            
            # Send text to synthesizer
            await self.synthesizer.synthesize(text, flush=True)
            
            logger.info(f"🔊 Speaking: {text[:50]}...")
            
        except Exception as e:
            logger.error(f"❌ Speech error: {e}")
            self.is_speaking = False
    
    async def start_conversation(self):
        """Start conversation with greeting"""
        logger.info("🎬 Starting conversation")

        if self.workflow_type == "hiring":
            first_q = get_first_question(self.candidate_name)
            await self.speak(first_q)
            self.conversation.append({"role": "assistant", "content": first_q})

            # 🔥 REQUIRED
            self.awaiting_response = True
            return

        if self.workflow_type == "sales":
            first_q = get_sales_first_question(
                self.workflow_data.get("company_name", "Our Company")
            )
            await self.speak(first_q)
            self.conversation.append({"role": "assistant", "content": first_q})

            # 🔥 REQUIRED
            self.awaiting_response = True
            return

        # default behavior
        greeting = "Namaste! Main AI assistant bol rahi hoon."
        await self.speak(greeting)
        self.conversation.append({"role": "assistant", "content": greeting})

        # 🔥 REQUIRED
        self.awaiting_response = True

    async def end_call(self):
        """End call gracefully"""
        logger.info("👋 Ending call")
        
        self.conversation_ended = True
        
        goodbye = "धन्यवाद! AurJobs AI से बात करने के लिए शुक्रिया। हम जल्द ही आपसे संपर्क करेंगे। अच्छा दिन रहे!"
        
        # Speak goodbye
        await self.speak(goodbye)
        
        # Wait for audio to finish
        await asyncio.sleep(3.0)
        
        # Hang up via Twilio
        await self._hangup_twilio()
        
        # Cleanup
        await self.cleanup()
    
    async def _stream_audio_to_twilio(self, audio_data: bytes):
        """
        Stream audio to Twilio
        
        Args:
            audio_data: μ-law 8kHz audio bytes
        """
        try:
            import base64
            
            # Encode to base64
            audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            
            # Send media message to Twilio
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": audio_b64
                }
            }
            
            await self.ws.send_json(message)
            
        except Exception as e:
            logger.error(f"❌ Twilio stream error: {e}")
    
    async def _send_twilio_clear(self):
        """Send clear command to Twilio to stop current audio playback"""
        try:
            message = {
                "event": "clear",
                "streamSid": self.stream_sid
            }
            await self.ws.send_json(message)
            logger.debug("📤 Clear command sent to Twilio")
        except Exception as e:
            logger.error(f"❌ Clear command error: {e}")
    
    async def _hangup_twilio(self):
        """Terminate Twilio call"""
        try:
            from twilio.rest import Client
            
            client = Client(Config.TWILIO_ACCOUNT_SID, Config.TWILIO_AUTH_TOKEN)
            client.calls(self.call_sid).update(status="completed")
            
            logger.info(f"✅ Call {self.call_sid} terminated")
            
        except Exception as e:
            logger.error(f"❌ Hangup error: {e}")
    
    async def cleanup(self):
        """Cleanup resources"""
        logger.info("🧹 Cleaning up agent resources")
        
        self.conversation_ended = True
        
        # Cancel tasks
        tasks = [self.transcription_handler_task, self.synthesis_handler_task, self.idle_task]
        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        # Stop components
        if self.transcriber:
            await self.transcriber.stop()
        
        if self.synthesizer:
            await self.synthesizer.stop()
        
        # Log stats
        call_duration = time.time() - self.call_start_time
        logger.info(f"📊 Call stats:")
        logger.info(f"   Duration: {call_duration:.1f}s")
        logger.info(f"   Transcripts: {self.total_transcripts}")
        logger.info(f"   Responses: {self.total_responses}")
        
        if self.transcriber:
            logger.info(f"   Transcriber: {self.transcriber.get_stats()}")
        
        if self.synthesizer:
            logger.info(f"   Synthesizer: {self.synthesizer.get_stats()}")
                # -----------------------------
        # Send call result webhook
        # -----------------------------
        if not self.webhook_sent:
          self.webhook_sent = True
          try:
            import requests

            payload = {
                "call_sid": self.call_sid,
                "chat_id": self.chat_id,
                "transcript": self.transcript,
                "duration": int(call_duration),
                "workflow_run_id": self.workflow_run_id
            }

            requests.post(
                Config.CALL_RESULT_WEBHOOK_URL,
                # "http://localhost:7071/api/call_result",

                  
                json=payload,
                timeout=5
            )

            logger.info(f"📤 Call result webhook sent for workflow_run_id {self.workflow_run_id}, resume is {self.resume_text} and jd is {self.jd_text}")

          except Exception as e:
            logger.error(f"❌ Failed to send call result webhook: {e}")

        
        logger.info("✅ Cleanup complete")