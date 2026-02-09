from fastapi import FastAPI, WebSocket
from fastapi.responses import Response
import os, json, base64, asyncio, audioop
import websockets

app = FastAPI()

RENDER_HOST = "clinic-ai-call-line.onrender.com"  # <- change if your Render domain differs
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Per OpenAI docs, a Realtime model can be "gpt-realtime"
# If this fails, we’ll adjust based on your account access.
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime")
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"

SYSTEM_PROMPT = """
You are an intake assistant for a low-income clinic serving immigrant patients.
Speak naturally and briefly. Ask one question at a time.
Collect: name, DOB or age, callback number, preferred language, reason for call,
symptom onset and severity, red flags (breathing trouble, chest pain, severe bleeding),
established patient yes/no, best callback time, voicemail ok yes/no.
If red flags are present, advise calling 911/ER.
Do NOT ask for SSN or payment card details.
"""

@app.post("/twilio/voice")
async def twilio_voice():
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">
        Hello. This is the clinic intake line.
    </Say>
    <Connect>
        <Stream url="wss://{RENDER_HOST}/media" />
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")

def mulaw8k_to_pcm16_16k(mulaw_bytes: bytes) -> bytes:
    pcm16_8k = audioop.ulaw2lin(mulaw_bytes, 2)
    pcm16_16k, _ = audioop.ratecv(pcm16_8k, 2, 1, 8000, 16000, None)
    return pcm16_16k

def pcm16_16k_to_mulaw8k(pcm16_16k: bytes) -> bytes:
    pcm16_8k, _ = audioop.ratecv(pcm16_16k, 2, 1, 16000, 8000, None)
    mulaw_8k = audioop.lin2ulaw(pcm16_8k, 2)
    return mulaw_8k

@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    print("Twilio websocket connected")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
    }

    async with websockets.connect(OPENAI_WS_URL, extra_headers=headers) as oai:
        # Configure session
        await oai.send(json.dumps({
            "type": "session.update",
            "session": {
                "instructions": SYSTEM_PROMPT,
                "modalities": ["text", "audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {"type": "server_vad"},
            }
        }))

        # Have assistant greet first
        await oai.send(json.dumps({
            "type": "response.create",
            "response": {"instructions": "Greet the caller and ask what language they prefer (English or Spanish)."}
        }))

        async def twilio_to_openai():
            while True:
                msg = await ws.receive_text()
                data = json.loads(msg)
                event = data.get("event")

                if event == "start":
                    print("Twilio stream started")
                elif event == "media":
                    mulaw = base64.b64decode(data["media"]["payload"])
                    pcm16_16k = mulaw8k_to_pcm16_16k(mulaw)
                    await oai.send(json.dumps({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm16_16k).decode("utf-8"),
                    }))
                elif event == "stop":
                    print("Twilio stream stopped")
                    await oai.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    break

        async def openai_to_twilio():
            while True:
                evt = json.loads(await oai.recv())
                t = evt.get("type")

                # AI audio back to Twilio
                if t in ("response.audio.delta", "output_audio.delta"):
                    pcm_b64 = evt.get("delta") or evt.get("audio")
                    if not pcm_b64:
                        continue
                    pcm16_16k = base64.b64decode(pcm_b64)
                    mulaw_8k = pcm16_16k_to_mulaw8k(pcm16_16k)

                    await ws.send_text(json.dumps({
                        "event": "media",
                        "media": {"payload": base64.b64encode(mulaw_8k).decode("utf-8")}
                    }))

        await asyncio.gather(twilio_to_openai(), openai_to_twilio())
