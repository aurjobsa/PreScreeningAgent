# Updated webhook code - receives questions directly from URL
from fastapi import FastAPI, Request, Form
from fastapi.responses import Response
import json
import urllib.parse
import time
from fastapi.middleware.cors import CORSMiddleware
from twilio.twiml.voice_response import VoiceResponse

app = FastAPI()

# In-memory storage for responses during call session
call_responses = {}

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ✅ Allow all origins
    allow_credentials=False,  # ❌ Must be False when using allow_origins=["*"]
    allow_methods=["*"],
    allow_headers=["*"],
)


# FIXED VERSION - The key issue is URL encoding in recording_url
@app.get("/jmd")
async def jmd():
    return "Jai mata Di"

@app.post("/voice/{session_id}")
async def handle_call(session_id: str, request: Request):
    """Handle Twilio voice calls with proper URL encoding."""
    try:
        print(f"🔄 Webhook called for session: {session_id}")
        
        # Get parameters from URL
        query_params = dict(request.query_params)
        print(f"📋 Query params: {query_params}")
        
        current_question = int(query_params.get("question", "1"))
        encoded_questions = query_params.get("questions")
        chat_id = query_params.get("chat_id")
        candidate_id = query_params.get("candidate_id")
        
        print(f"📊 Current question: {current_question}")
        print(f"💬 Chat ID: {chat_id}")
        print(f"👤 Candidate ID: {candidate_id}")
        
        if not encoded_questions:
            print("❌ No encoded questions found")
            response = VoiceResponse()
            response.say("Sorry, we are unable to fetch your questions right now.")
            return Response(content=str(response), media_type="application/xml")
        
        # Decode questions from URL
        try:
            questions_json = urllib.parse.unquote(encoded_questions)
            questions = json.loads(questions_json)
            print(f"✅ Successfully decoded {len(questions)} questions")
        except Exception as e:
            print(f"❌ Error decoding questions: {e}")
            response = VoiceResponse()
            response.say("Sorry, there was an error processing your interview questions.")
            return Response(content=str(response), media_type="application/xml")
        
        response = VoiceResponse()
        
        # Initialize session storage if not exists
        if session_id not in call_responses:
            print(f"🆕 Creating new session: {session_id}")
            call_responses[session_id] = {
                "chat_id": chat_id,
                "candidate_id": candidate_id,
                "questions": questions,
                "responses": [],
                "total_questions": len(questions),
                "started_at": time.time()
            }
        else:
            print(f"📂 Session exists: {session_id}")
        
        # Check if we've asked all questions
        if current_question > len(questions):
            print(f"✅ All questions completed for session: {session_id}")
            response.say("Your interview is complete. Thank you and have a great day!")
            response.hangup()
            return Response(content=str(response), media_type="application/xml")
        
        # Introduction for first question
        if current_question == 1:
            print("🎤 Playing introduction...")
            response.say("Hello, we are from AurJobs and we are going to take your interview.", voice='Polly.Joanna')
            response.pause(length=1)
        
        # Ask the current question
        question = questions[current_question - 1]
        print(f"❓ Asking question {current_question}: {question[:50]}...")
        
        response.say(f"Question {current_question}. {question}. Speak your answer after the beep", voice='Polly.Joanna')
        
        # 🔥 FIX: Properly encode the recording URL parameters
        # The issue is that encoded_questions contains special characters that need to be URL encoded again
        recording_params = {
            "session_id": session_id,
            "question": str(current_question),
            "questions": encoded_questions,  # This is already URL encoded from the original request
            "chat_id": chat_id,
            "candidate_id": candidate_id
        }
        
        # Build the recording URL with proper encoding
        recording_url = "/recording?" + urllib.parse.urlencode(recording_params)
        print(f"🎵 Recording URL: {recording_url}")
        
        response.record(
            action=recording_url,
            method="POST",
            max_length=60,
            timeout=5,
            play_beep=True,
            finish_on_key="#"
        )
        
        print(f"✅ Successfully generated TwiML for session: {session_id}")
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR in handle_call: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Return error response to Twilio
        response = VoiceResponse()
        response.say("Sorry, there was a technical error. Please try again later.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

@app.post("/recording")
async def handle_recording(request: Request):
    """Handle recording responses with proper URL encoding."""
    try:
        print("🎵 Recording webhook called")
        
        form = await request.form()
        recording_url = form.get("RecordingUrl")
        session_id = request.query_params.get("session_id")
        question_number = request.query_params.get("question")
        encoded_questions = request.query_params.get("questions")
        chat_id = request.query_params.get("chat_id")
        candidate_id = request.query_params.get("candidate_id")
        
        print(f"📥 Recording received for session {session_id}, question {question_number}")
        print(f"🎵 Recording URL: {recording_url}")
        print(f"📋 All params: session_id={session_id}, question={question_number}, chat_id={chat_id}, candidate_id={candidate_id}")
        
        if not all([recording_url, session_id, question_number, encoded_questions]):
            print("❌ Missing required data in recording webhook")
            return Response(status_code=400, content="Missing required data.")
        
        # Decode questions
        try:
            questions_json = urllib.parse.unquote(encoded_questions)
            questions = json.loads(questions_json)
            print(f"✅ Decoded {len(questions)} questions successfully")
        except Exception as e:
            print(f"❌ Error decoding questions: {e}")
            return Response(status_code=400, content="Error decoding questions.")
        
        # Store response in memory
        recording_url += ".mp3"
        response_data = {
            "question_number": int(question_number),
            "question": questions[int(question_number) - 1],
            "audio_url": recording_url,
            "timestamp": time.time()
        }
        
        # Add to session responses
        if session_id in call_responses:
            call_responses[session_id]["responses"].append(response_data)
            print(f"✅ Added response to existing session: {session_id}")
        else:
            # Initialize if somehow missing
            call_responses[session_id] = {
                "chat_id": chat_id,
                "candidate_id": candidate_id,
                "questions": questions,
                "responses": [response_data],
                "total_questions": len(questions),
                "started_at": time.time()
            }
            print(f"🆕 Created new session for recording: {session_id}")
        
        print(f"✅ Stored response in memory for session: {session_id}")
        print(f"📊 Total responses so far: {len(call_responses[session_id]['responses'])}")
        
        response = VoiceResponse()
        total_questions = len(questions)
        
        # Check if this was the last question
        if int(question_number) >= total_questions:
            print(f"🎉 Interview completed for session: {session_id}")
            response.say("Thank you for your responses. Your interview is complete!")
            call_responses[session_id]["completed_at"] = time.time()
            response.hangup()
        else:
            # 🔥 FIX: Properly encode the redirect URL parameters
            next_question = int(question_number) + 1
            redirect_params = {
                "question": str(next_question),
                "questions": encoded_questions,  # Keep the already encoded questions
                "chat_id": chat_id,
                "candidate_id": candidate_id
            }
            
            next_url = f"/voice/{session_id}?" + urllib.parse.urlencode(redirect_params)
            print(f"➡️ Redirecting to next question: {next_question}")
            print(f"🔗 Redirect URL: {next_url}")
            response.redirect(next_url, method="POST")
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        print(f"❌ CRITICAL ERROR in handle_recording: {str(e)}")
        import traceback
        traceback.print_exc()
        
        # Return error response to Twilio
        response = VoiceResponse()
        response.say("Sorry, there was a technical error processing your response.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

# 🔥 ALTERNATIVE APPROACH: Use Base64 encoding for questions to avoid URL encoding issues
import base64

def encode_questions_base64(questions):
    """Encode questions using base64 to avoid URL encoding issues."""
    questions_json = json.dumps(questions)
    encoded = base64.b64encode(questions_json.encode()).decode()
    return encoded

def decode_questions_base64(encoded_questions):
    """Decode base64 encoded questions."""
    decoded_json = base64.b64decode(encoded_questions.encode()).decode()
    return json.loads(decoded_json)

# Update your trigger_twilio_call function to use base64:
def create_questions_base64(job_description):
    """Create questions and encode them with base64."""
    questions = [
        f"Describe a complex data migration you performed using Django and PostgreSQL. What challenges did you encounter, and how did you overcome them?",
        f"Imagine a scenario where your Django application, backed by PostgreSQL, experiences a sudden surge in traffic, leading to performance degradation. How would you approach identifying the bottleneck and implementing a solution to improve performance?"
    ]
    return encode_questions_base64(questions)

# Then in your webhook, use the base64 decode function instead of urllib.parse.unquote
@app.get("/status/{session_id}")
async def get_session_status(session_id: str):
    """Get current status of a screening session from memory."""
    if session_id not in call_responses:
        return {"success": False, "error": "Session not found"}
    
    session_data = call_responses[session_id]
    total_questions = session_data["total_questions"]
    completed = len(session_data["responses"])
    
    status = "completed" if "completed_at" in session_data else "in_progress"
    
    return {
        "success": True,
        "session_id": session_id,
        "status": status,
        "total_questions": total_questions,
        "completed_questions": completed,
        "progress_percentage": (completed / total_questions) * 100 if total_questions > 0 else 0,
        "chat_id": session_data["chat_id"],
        "candidate_id": session_data["candidate_id"]
    }

@app.get("/responses/{session_id}")
async def get_session_responses(session_id: str):
    """Get all responses for a session from memory."""
    if session_id not in call_responses:
        return {"success": False, "error": "Session not found"}
    
    session_data = call_responses[session_id]
    
    return {
        "success": True,
        "session_id": session_id,
        "chat_id": session_data["chat_id"],
        "candidate_id": session_data["candidate_id"],
        "questions": session_data["questions"],
        "responses": session_data["responses"],
        "total_questions": session_data["total_questions"],
        "completed_questions": len(session_data["responses"]),
        "started_at": session_data.get("started_at"),
        "completed_at": session_data.get("completed_at")
    }


from azurestorage import (
    delete_file_from_azure,
    upload_file_to_azure,
    validate_pdf_file,
    generate_unique_filename,
    azure_config,
    ResumeUploadResponse,
    JobDescriptionUploadResponse
)


from typing import List, Optional

from fastapi import FastAPI, HTTPException, File, UploadFile

@app.post("/upload-resumes", response_model=ResumeUploadResponse)
async def upload_resumes(
    resumes: List[UploadFile] = File(..., description="Resume PDF files")
):
    """
    Upload resume PDF files to S3 bucket
    
    Args:
        resumes: List of resume PDF files
    
    Returns:
        JSON response with downloadable URLs for uploaded resume files
    """
    
    # Validate that files are provided
    if not resumes:
        raise HTTPException(
            status_code=400, 
            detail="Resume files are required"
        )
    
    # Validate file types
    # for file in resumes:
    #     if not validate_pdf_file(file):
    #         raise HTTPException(
    #             status_code=400,
    #             detail=f"Only PDF files are allowed. Invalid file: {file.filename}"
    #         )
    
 
    
    
    
    uploaded_resume_urls = []
    
    try:
        # Upload resume files
        for resume_file in resumes:
            # Reset file pointer to beginning
            await resume_file.seek(0)
            url = await upload_file_to_azure(resume_file, "resumes")
            uploaded_resume_urls.append(url)
        
        return ResumeUploadResponse(resumes=uploaded_resume_urls)
        
    except Exception as e:
        # Clean up uploaded files if there's an error
        print(f"Error during resume upload: {str(e)}")
        
        # Extract S3 keys from URLs and delete files
        for url in uploaded_resume_urls:
            try:
                # Extract S3 key from URL
                blob_path = url.split(f"{azure_config.container_name}/")[-1]
                delete_file_from_azure(blob_path)
            except Exception as cleanup_error:
                print(f"Error during cleanup: {str(cleanup_error)}")
        
        raise HTTPException(status_code=500, detail=f"Resume upload failed: {str(e)}")

@app.post("/upload-job-descriptions", response_model=JobDescriptionUploadResponse)

async def upload_job_descriptions(
    job_description: UploadFile = File(..., description="Job description PDF file")
):
    """
    Upload a single job description PDF file to S3 bucket
    
    Args:
        job_description: Single job description PDF file
    
    Returns:
        JSON response with downloadable URL for uploaded job description file
    """
    
    # Validate that file is provided
    if not job_description:
        raise HTTPException(
            status_code=400, 
            detail="Job description file is required"
        )
    
    # Validate file type
    if not validate_pdf_file(job_description):
        raise HTTPException(
            status_code=400,
            detail=f"Only PDF files are allowed. Invalid file: {job_description.filename}"
        )
    
   
    
    try:
        # Upload job description file
        await job_description.seek(0)
        url = await upload_file_to_azure(job_description, "job_descriptions")
        
        return JobDescriptionUploadResponse(job_descriptions=url)
        
    except Exception as e:
        # Clean up uploaded file if there's an error
        print(f"Error during job description upload: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Job description upload failed: {str(e)}")



@app.delete("/session/{session_id}")
async def cleanup_session(session_id: str):
    """Clean up session data from memory."""
    if session_id in call_responses:
        del call_responses[session_id]
        return {"success": True, "message": "Session cleaned up"}
    return {"success": False, "error": "Session not found"}

# Usage Example:
"""
# Step 1: Trigger call with questions directly
result = trigger_twilio_call(candidate, questions, chat_id)
session_id = result["session_id"]

# Step 2: Check status via API
import requests
status = requests.get(f"your-webhook-url/status/{session_id}").json()

# Step 3: Get responses when complete  
responses = requests.get(f"your-webhook-url/responses/{session_id}").json()

# Step 4: Clean up when done
requests.delete(f"your-webhook-url/session/{session_id}")
"""


















# from fastapi import FastAPI, Request, Form, HTTPException
# from fastapi.responses import Response
# import os
# import json
# from twilio.twiml.voice_response import VoiceResponse
# from twilio.rest import Client
# from dotenv import load_dotenv
# from typing import Dict, List
# import time
# from supbase_client import supabase

# load_dotenv()
# app = FastAPI()
# def get_screening_data(chat_id: str):
#     """Get screening data from Supabase"""
#     try:
#         result = supabase.table("screening").select("questions, responses").eq("chat_id", chat_id).execute()
#         if result.data:
#             return result.data[0]
#         return None
#     except Exception as e:
#         print(f"Error getting screening data: {str(e)}")
#         return None

# def save_response_to_db(chat_id: str, question_number: int, response_data: dict):
#     """Save response to Supabase"""
#     try:
#         # First, get current responses
#         current_data = supabase.table("screening").select("responses").eq("chat_id", chat_id).execute()
        
#         if current_data.data:
#             current_responses = current_data.data[0]['responses'] if current_data.data[0]['responses'] else []
            
#             # Add new response
#             new_response = {
#                 "question_number": question_number,
#                 "audio_url": response_data["audio_url"],
#                 "timestamp": time.time()
#             }
#             current_responses.append(new_response)
            
#             # Update the database
#             result = supabase.table("screening").update({
#                 "responses": current_responses
#             }).eq("chat_id", chat_id).execute()
            
#             return len(result.data) > 0
        
#         return False
#     except Exception as e:
#         print(f"Error saving response: {str(e)}")
#         return False

# @app.post("/voice/{chat_id}")
# async def handle_call(chat_id: str, request: Request):
#     response = VoiceResponse()

#     # Determine which question to ask (default to 1)
#     query_params = dict(request.query_params)
#     current_question = int(query_params.get("question", "1"))

#     try:
#         # Get screening data from Supabase
#         screening_data = get_screening_data(chat_id)
        
#         if not screening_data or not screening_data['questions']:
#             response.say("Sorry, we are unable to fetch your questions right now.")
#             return Response(content=str(response), media_type="application/xml")

#         questions = screening_data['questions']
        
#         # Check if we've asked all questions
#         if current_question > len(questions):
#             response.say("Your interview is done and you will be informed through mail")
#             response.hangup()
#             return Response(content=str(response), media_type="application/xml")
        
#         # Introduction for first question
#         if current_question == 1:
#             response.say("Hello, we are from AurJobs and we are going to take your interview.", voice='Polly.Joanna')
#             response.pause(length=1)
        
#         # Ask the current question
#         question = questions[current_question - 1]
#         response.say(f"Question {current_question}. {question} , Speak your answer after the beep", voice='Polly.Joanna')
#         response.record(
#             action=f"/recording?chat_id={chat_id}&question={current_question}",
#             method="POST",
#             max_length=30,
#             timeout=5,
#             play_beep=True,
#             finish_on_key="#"
#         )

#     except Exception as e:
#         print(f"Error in handle_call: {str(e)}")
#         response.say("Sorry, there was an error processing your request.")
#         return Response(content=str(response), media_type="application/xml")

#     return Response(content=str(response), media_type="application/xml")


# @app.post("/recording")
# async def handle_recording(request: Request):
#     form = await request.form()
#     recording_url = form.get("RecordingUrl")
#     chat_id = request.query_params.get("chat_id")
#     question_number = request.query_params.get("question")

#     print(f"📥 Recording received for chat {chat_id}, question {question_number}")
#     print(f"🎵 Recording URL: {recording_url}")

#     if not recording_url or not chat_id or not question_number:
#         print("❌ Missing required data in recording webhook")
#         return Response(status_code=400, content="Missing required data.")

#     # Add .mp3 extension for Whisper compatibility
#     recording_url += ".mp3"
#     response_data = {
#         "audio_url": recording_url,
#     }

#     try:
#         # Save response to Supabase
#         success = save_response_to_db(chat_id, int(question_number), response_data)
        
#         if not success:
#             print(f"❌ Failed to save response for chat {chat_id}")
#             return Response(status_code=500, content="Failed to save response.")

#         print(f"✅ Saved response to database for chat {chat_id}, question {question_number}")

#         # Get total questions to check if this was the last one
#         screening_data = get_screening_data(chat_id)
#         total_questions = len(screening_data['questions']) if screening_data and screening_data['questions'] else 1

#         response = VoiceResponse()
        
#         # Check if this was the last question
#         if int(question_number) >= total_questions:
#             response.say("Thank you for your responses. Have a great day!")
#             response.hangup()
#         else:
#             # Redirect to next question
#             next_question = int(question_number) + 1
#             response.redirect(f"/voice/{chat_id}?question={next_question}", method="POST")
        
#         return Response(content=str(response), media_type="application/xml")

#     except Exception as e:
#         print(f"Error in handle_recording: {str(e)}")
#         response = VoiceResponse()
#         response.say("Sorry, there was an error processing your response.")
#         response.hangup()
#         return Response(content=str(response), media_type="application/xml")


# # Add a status endpoint to check screening data (for debugging)
# @app.get("/debug/{chat_id}")
# async def debug_screening(chat_id: str):
#     try:
#         screening_data = get_screening_data(chat_id)
#         if screening_data:
#             return {
#                 "chat_id": chat_id,
#                 "questions_count": len(screening_data['questions']) if screening_data['questions'] else 0,
#                 "responses_count": len(screening_data['responses']) if screening_data['responses'] else 0,
#                 "questions": screening_data['questions'],
#                 "responses": screening_data['responses']
#             }
#         else:
#             return {"error": "No screening data found for this chat_id"}
#     except Exception as e:
#         return {"error": str(e)}
    




# from fastapi import FastAPI, Request, Form
# from fastapi.responses import Response
# import os
# import json
# from twilio.twiml.voice_response import VoiceResponse
# from dotenv import load_dotenv

# load_dotenv()
# app = FastAPI()

# BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# @app.post("/voice/{candidate_id}")
# async def handle_call(candidate_id: str, request: Request):
#     response = VoiceResponse()

#     # Determine which question to ask (default to 1)
#     query_params = dict(request.query_params)
#     current_question = int(query_params.get("question", "1"))

#     questions_path = os.path.join(BASE_DIR, f"temp_questions_{candidate_id}.json")

#     if not os.path.exists(questions_path):
#         response.say("Sorry, we are unable to fetch your questions right now.")
#         return Response(content=str(response), media_type="application/xml")

#     with open(questions_path, "r") as file:
#         data = json.load(file)
#         questions = data.get("questions", [])
        
    
        
#     # Check if we've asked all questions
#     if current_question > len(questions):
#         response.say("Your interview is done and you will be informed through mail")
#         response.hangup()  # Explicitly end the call
#         return Response(content=str(response), media_type="application/xml")
#      # Introduction for first question
#     if current_question == 1:
#         response.say("Hello, we are from AurJobs and we are going to take your interview.", voice='Polly.Joanna')
#         response.pause(length=1)
#     # Ask the current question
#     question = questions[current_question - 1]
#     response.say(f"Question {current_question}. {question} , Speak your answer after the beep", voice='Polly.Joanna')
#     response.record(
#         action=f"/recording?candidate_id={candidate_id}&question={current_question}",
#         method="POST",
#         max_length=30,
#         timeout=5,
#         play_beep=True,
#         finish_on_key="#"  # Allow early termination
#     )

#     return Response(content=str(response), media_type="application/xml")


# @app.post("/recording")
# async def handle_recording(request: Request):
#     form = await request.form()
#     recording_url = form.get("RecordingUrl")
#     candidate_id = request.query_params.get("candidate_id")
#     question_number = request.query_params.get("question")

#     print(f"📥 Recording received for candidate {candidate_id}, question {question_number}")
#     print(f"🎵 Recording URL: {recording_url}")

#     if not recording_url or not candidate_id or not question_number:
#         print("❌ Missing required data in recording webhook")
#         return Response(status_code=400, content="Missing required data.")

#     # Add .mp3 extension for Whisper compatibility
#     recording_url += ".mp3"
#     audio_data = {
#         "candidate_id": candidate_id,
#         "question_number": question_number,
#         "audio_url": recording_url,  # Changed from 'recording_url' to 'audio_url' to match your transcribe function
#         # "timestamp": time.time()
#     }

#     # Store the recording info
#     file_path = os.path.join(BASE_DIR, f"responses_{candidate_id}_q{question_number}.json")
#     with open(file_path, "w") as file:
#         json.dump(audio_data, file)
    
#     print(f"✅ Saved response file: {file_path}")

#     # Load questions to check if this was the last one
#     questions_path = os.path.join(BASE_DIR, f"temp_questions_{candidate_id}.json")
#     total_questions = 1  # default
    
#     if os.path.exists(questions_path):
#         with open(questions_path, "r") as file:
#             data = json.load(file)
#             total_questions = len(data.get("questions", []))

#     response = VoiceResponse()
    
#     # Check if this was the last question
#     if int(question_number) >= total_questions:
#         response.say("Thank you for your responses. Have a great day!")
#         response.hangup()
#     else:
#         # Redirect to next question
#         next_question = int(question_number) + 1
#         response.redirect(f"/voice/{candidate_id}?question={next_question}", method="POST")
    
#     return Response(content=str(response), media_type="application/xml")


# # Add a status endpoint to check files (for debugging)
# @app.get("/debug/{candidate_id}")
# async def debug_files(candidate_id: str):
#     files = []
#     for file in os.listdir(BASE_DIR):
#         if file.startswith(f"responses_{candidate_id}"):
#             files.append(file)
    
#     return {
#         "candidate_id": candidate_id,
#         "response_files": files,
#         "base_dir": BASE_DIR
#     }



