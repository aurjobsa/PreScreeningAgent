from fastapi import FastAPI, Request, Form
from fastapi.responses import Response
import os
import json
from twilio.twiml.voice_response import VoiceResponse
from dotenv import load_dotenv

load_dotenv()
app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.post("/voice/{candidate_id}")
async def handle_call(candidate_id: str, request: Request):
    response = VoiceResponse()

    # Determine which question to ask (default to 1)
    query_params = dict(request.query_params)
    current_question = int(query_params.get("question", "1"))

    questions_path = os.path.join(BASE_DIR, f"temp_questions_{candidate_id}.json")

    if not os.path.exists(questions_path):
        response.say("Sorry, we are unable to fetch your questions right now.")
        return Response(content=str(response), media_type="application/xml")

    with open(questions_path, "r") as file:
        data = json.load(file)
        questions = data.get("questions", [])
        
    
        
    # Check if we've asked all questions
    if current_question > len(questions):
        response.say("Your interview is done and you will be informed through mail")
        response.hangup()  # Explicitly end the call
        return Response(content=str(response), media_type="application/xml")
     # Introduction for first question
    if current_question == 1:
        response.say("Hello, we are from AurJobs and we are going to take your interview.", voice='Polly.Joanna')
        response.pause(length=1)
    # Ask the current question
    question = questions[current_question - 1]
    response.say(f"Question {current_question}. {question} , Speak your answer after the beep", voice='Polly.Joanna')
    response.record(
        action=f"/recording?candidate_id={candidate_id}&question={current_question}",
        method="POST",
        max_length=30,
        timeout=5,
        play_beep=True,
        finish_on_key="#"  # Allow early termination
    )

    return Response(content=str(response), media_type="application/xml")


@app.post("/recording")
async def handle_recording(request: Request):
    form = await request.form()
    recording_url = form.get("RecordingUrl")
    candidate_id = request.query_params.get("candidate_id")
    question_number = request.query_params.get("question")

    print(f"📥 Recording received for candidate {candidate_id}, question {question_number}")
    print(f"🎵 Recording URL: {recording_url}")

    if not recording_url or not candidate_id or not question_number:
        print("❌ Missing required data in recording webhook")
        return Response(status_code=400, content="Missing required data.")

    # Add .mp3 extension for Whisper compatibility
    recording_url += ".mp3"
    audio_data = {
        "candidate_id": candidate_id,
        "question_number": question_number,
        "audio_url": recording_url,  # Changed from 'recording_url' to 'audio_url' to match your transcribe function
        # "timestamp": time.time()
    }

    # Store the recording info
    file_path = os.path.join(BASE_DIR, f"responses_{candidate_id}_q{question_number}.json")
    with open(file_path, "w") as file:
        json.dump(audio_data, file)
    
    print(f"✅ Saved response file: {file_path}")

    # Load questions to check if this was the last one
    questions_path = os.path.join(BASE_DIR, f"temp_questions_{candidate_id}.json")
    total_questions = 1  # default
    
    if os.path.exists(questions_path):
        with open(questions_path, "r") as file:
            data = json.load(file)
            total_questions = len(data.get("questions", []))

    response = VoiceResponse()
    
    # Check if this was the last question
    if int(question_number) >= total_questions:
        response.say("Thank you for your responses. Have a great day!")
        response.hangup()
    else:
        # Redirect to next question
        next_question = int(question_number) + 1
        response.redirect(f"/voice/{candidate_id}?question={next_question}", method="POST")
    
    return Response(content=str(response), media_type="application/xml")


# Add a status endpoint to check files (for debugging)
@app.get("/debug/{candidate_id}")
async def debug_files(candidate_id: str):
    files = []
    for file in os.listdir(BASE_DIR):
        if file.startswith(f"responses_{candidate_id}"):
            files.append(file)
    
    return {
        "candidate_id": candidate_id,
        "response_files": files,
        "base_dir": BASE_DIR
    }
