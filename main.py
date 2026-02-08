from fastapi import FastAPI, WebSocket
from fastapi.responses import Response
import json

app = FastAPI()

@app.post("/twilio/voice")
async def twilio_voice():
    # This TwiML tells Twilio: keep the call open and stream audio to our WebSocket.
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">
        Hello. Please start speaking after the beep.
    </Say>
    <Connect>
        <Stream url="wss://clinic-ai-call-line.onrender.com/media" />
    </Connect>
</Response>
"""
    return Response(content=twiml, media_type="text/xml")


@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)

            # Twilio will send events like: start, media, stop
            event = data.get("event")
            if event == "start":
                print("Twilio stream started")
            elif event == "media":
                # This contains base64 audio frames in data["media"]["payload"]
                print("Received audio frame")
            elif event == "stop":
                print("Twilio stream stopped")
                break
    except Exception as e:
        print("WebSocket error:", e)
