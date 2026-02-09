from fastapi import FastAPI, WebSocket
from fastapi.responses import Response
import os, json, base64, asyncio
import audioop
import websockets

app = FastAPI()

# --- Config ---
RENDER_HOST = os.getenv("RENDER_HOST", "clinic-ai-call-line.onrender.com")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# Use a safer default; override via Render env var if needed.
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"

SYSTEM_PROMPT = """
You are an intake assistant for a low-income clinic serving immigrant patients.
Speak naturally and briefly. Ask one question at a time.
Collect: name, DOB or age, callback number, preferred language, reason for call,
symptom onset and severity, red flags (breathing trouble, chest pain, severe bleeding),
established patient yes/no, best callback time, voicemail ok yes/no.
If red flags are present, advise calling 911/ER.
Do NOT ask for SSN or payment card details.
""".strip()


@app.post("/twilio/voice")
async def twilio_voice():
    # TwiML: greet then keep call open and stream audio to our WebSocket
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice">Hello. This is the clinic intake line.</Say>
  <Connect>
    <Stream url="wss://{RENDER_HOST}/media" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="text/xml")


# Twilio sends 8kHz mu-law; OpenAI expects PCM16 (we'll use 16kHz PCM16)
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

    # Twilio stream identifier (needed when sending audio back)
    stream_sid: str | None = None

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }

    try:
        print("Connecting to OpenAI Realtime:", OPENAI_WS_URL)

        # NOTE: use `additional_headers` (NOT `extra_headers`)
        async with websockets.connect(OPENAI_WS_URL, additional_headers=headers) as oai:
            print("Connected to OpenAI Realtime ✅")

            # Configure session
            await oai.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "instructions": SYSTEM_PROMPT,
                            "modalities": ["text", "audio"],
                            "input_audio_format": "pcm16",
                            "output_audio_format": "pcm16",
                            "turn_detection": {"type": "server_vad"},
                            # Voice helps ensure audio is produced
                            "voice": "alloy",
                        },
                    }
                )
            )

            # Ask assistant to greet first
            await oai.send(
                json.dumps(
                    {
                        "type": "response.create",
                        "response": {
                            "modalities": ["audio", "text"],
                            "instructions": "Greet the caller and ask what language they prefer (English or Spanish).",
                        },
                    }
                )
            )

            async def twilio_to_openai():
                nonlocal stream_sid
                while True:
                    msg = await ws.receive_text()
                    data = json.loads(msg)
                    event = data.get("event")

                    if event == "start":
                        stream_sid = data["start"]["streamSid"]
                        print("Twilio stream started:", stream_sid)

                    elif event == "media":
                        # Incoming Twilio audio (base64 mu-law 8kHz)
                        mulaw = base64.b64decode(data["media"]["payload"])
                        pcm16_16k = mulaw8k_to_pcm16_16k(mulaw)

                        await oai.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(pcm16_16k).decode("utf-8"),
                                }
                            )
                        )

                    elif event == "stop":
                        print("Twilio stream stopped")
                        break

            async def openai_to_twilio():
                while True:
                    evt = json.loads(await oai.recv())
                    t = evt.get("type")

                    # Print OpenAI errors if any
                    if t == "error":
                        print("OPENAI ERROR EVENT:", evt)

                    # OpenAI audio delta events (handle a few variants)
                    if t in (
                        "response.output_audio.delta",
                        "response.audio.delta",
                        "output_audio.delta",
                    ):
                        pcm_b64 = evt.get("delta") or evt.get("audio")
                        if not pcm_b64:
                            continue

                        # Wait until Twilio has given us streamSid
                        if not stream_sid:
                            continue

                        pcm16_16k = base64.b64decode(pcm_b64)
                        mulaw_8k = pcm16_16k_to_mulaw8k(pcm16_16k)

                        # Send audio back to Twilio
                        await ws.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": base64.b64encode(mulaw_8k).decode("utf-8")},
                                }
                            )
                        )

            await asyncio.gather(twilio_to_openai(), openai_to_twilio())

    except Exception as e:
        print("ERROR in /media bridge:", repr(e))
        try:
            await ws.close()
        except Exception:
            pass
