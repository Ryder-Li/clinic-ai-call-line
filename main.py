from fastapi import FastAPI
from fastapi.responses import Response

app = FastAPI()

@app.post("/twilio/voice")
async def twilio_voice():
    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">
        Hello. This is the clinic intake line.
        Thank you for calling. Goodbye.
    </Say>
</Response>
"""
    return Response(content=twiml, media_type="text/xml")