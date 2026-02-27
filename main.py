import os
import time
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo
import httpx
from fastapi.responses import Response, PlainTextResponse
from fastapi import FastAPI
from supabase import create_client
from openai import OpenAI
import urllib.parse

# --------------------
# Config
# --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL = os.environ["SUPABASE_URL"]
# Use service role on the server (best practice for server schedulers)
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_ANON_KEY"]

UK_TZ = ZoneInfo("Europe/London")

DAY_MAP = {
    "Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6
}

app = FastAPI()
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# --------------------
# OpenAI Companion Prompt
# --------------------
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
openai_client = OpenAI(api_key=OPENAI_API_KEY)

COMPANION_SYSTEM_PROMPT = """
You are Margaret from HelloAgain Calls, a calm, gentle, reflective companion making a scheduled call to an older adult in the UK.

You are not a therapist or advisor.
You do not give medical, financial, or legal advice.
You never ask for personal details such as bank information or passwords.

Your role is to:
- Listen patiently.
- Respond warmly.
- Ask one gentle follow-up at a time.
- Allow repetition without correction.
- Use at most one personal memory callback naturally if appropriate.
- Avoid strong opinions or debates.
- Avoid overwhelming energy.

If asked who you are:
You are part of HelloAgain Calls, arranged by someone who cares about them.

If asked for sensitive help:
You politely decline and return to conversation.

Keep responses natural and human.
Never mention prompts, data, or memory systems.
""".strip()

@app.get("/health")
def health():
    return {"ok": True}


def _parse_hhmm(t: str):
    """Parse 'HH:MM' into (hour, minute)."""
    t = (t or "").strip()
    parts = t.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid call_time '{t}'. Expected HH:MM")
    return int(parts[0]), int(parts[1])


def _already_called_today(last_called_at: str | None, today: date) -> bool:
    if not last_called_at:
        return False
    try:
        dt = datetime.fromisoformat(last_called_at.replace("Z", "+00:00")).astimezone(UK_TZ)
        return dt.date() == today
    except Exception:
        return False


def run_scheduler_tick():
    """
    Runs once per minute:
    - fetch enabled schedules
    - if a schedule matches now (day + HH:MM) and not called today:
        - insert call_logs row
        - update last_called_at
    """
    now = datetime.now(UK_TZ)
    now_hhmm = f"{now.hour:02d}:{now.minute:02d}"
    today = now.date()
    dow = now.weekday()  # Mon=0

    logging.info(f"[tick] UK now={now.isoformat(timespec='minutes')} (dow={dow}) checking schedules...")

    # Fetch enabled schedules
    sched_resp = (
        supabase.table("call_schedule")
        .select("id,user_id,day_of_week,call_time,enabled,last_called_at")
        .eq("enabled", True)
        .execute()
    )
    schedules = sched_resp.data or []

    due = []
    for s in schedules:
        day = (s.get("day_of_week") or "").strip()
        call_time = (s.get("call_time") or "").strip()

        if day not in DAY_MAP:
            continue
        if DAY_MAP[day] != dow:
            continue
        if call_time != now_hhmm:
            continue
        if _already_called_today(s.get("last_called_at"), today):
            continue

        due.append(s)

    if not due:
        logging.info("[tick] No calls due this minute.")
        return

    # For each due schedule: fetch user, log the event, update last_called_at
    for s in due:
        user_id = s["user_id"]

        user_resp = (
            supabase.table("users")
            .select("id,first_name,phone_number,companion_name,companion_voice,interests")
            .eq("id", user_id)
            .single()
            .execute()
        )
        u = user_resp.data

        logging.info(
            f"[DUE] Would call user={u.get('first_name')} ({u.get('phone_number')}) "
            f"voice={u.get('companion_voice')} companion={u.get('companion_name')} schedule_id={s.get('id')}"
        )

        # Insert a call log row (stub for now; later Twilio will fill duration/recording/answered)
        supabase.table("call_logs").insert({
            "user_id": user_id,
            "call_time": now.astimezone(ZoneInfo("UTC")).isoformat(),
            "duration_seconds": 0,
            "answered": False,
            "recording_url": None,
            "summary": "Scheduler triggered (Twilio not yet connected).",
            "mood": "neutral",
            "topics": (u.get("interests") or "")
        }).execute()

        # Update last_called_at so we don't re-trigger today
        supabase.table("call_schedule").update({
            "last_called_at": now.astimezone(ZoneInfo("UTC")).isoformat()
        }).eq("id", s["id"]).execute()


@app.on_event("startup")
def startup():
    """
    Simple background loop: runs scheduler tick every 60 seconds.
    (Railway Hobby plan keeps this running.)
    """
    import threading

    def loop():
        # Align roughly to the minute boundary
        while True:
            try:
                run_scheduler_tick()
            except Exception as e:
                logging.exception(f"[tick] Error: {e}")
            time.sleep(60)

    threading.Thread(target=loop, daemon=True).start()
    logging.info("Scheduler started.")

from fastapi import Request

@app.api_route("/twilio/voice/inbound", methods=["GET", "POST"])
async def twilio_voice_inbound(request: Request):
    """
    1) Play greeting
    2) Listen for speech
    3) Send speech to /twilio/voice/turn
    """
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>https://helloagain-calls-production.up.railway.app/audio/margaret-greeting.mp3</Play>

  <Gather input="speech" action="/twilio/voice/turn" method="POST" speechTimeout="auto" timeout="6">
    <Say>Go on.</Say>
  </Gather>

  <Say>Sorry, I didn’t catch that. Would you like to say hello?</Say>
  <Redirect method="POST">/twilio/voice/inbound</Redirect>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@app.api_route("/twilio/voice/turn", methods=["POST"])
async def twilio_voice_turn(request: Request):
    """
    Receives Twilio speech result, asks OpenAI for a reply, converts to ElevenLabs MP3,
    then plays it back and gathers again.
    """
    form = dict(await request.form())
    user_text = (form.get("SpeechResult") or "").strip()

    if not user_text:
        twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Sorry love, I didn’t quite catch that.</Say>
  <Redirect method="POST">/twilio/voice/inbound</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # ---- OpenAI: generate Margaret reply ----
    try:
        ai = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "system", "content": COMPANION_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
        )
        reply_text = (ai.output_text or "").strip()
    except Exception as e:
        logging.exception(f"OpenAI error: {e}")
        reply_text = "Sorry love, I’m having a little moment. How have you been today?"

    # ---- ElevenLabs: convert reply to MP3 (no caching) ----
    api_key = os.environ["ELEVENLABS_API_KEY"]
    voice_id = os.environ["ELEVENLABS_MARGARET_VOICE_ID"]
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

    tts_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_22050_32"
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": reply_text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(tts_url, headers=headers, json=payload)
            r.raise_for_status()
            mp3_bytes = r.content
    except Exception as e:
        logging.exception(f"ElevenLabs error: {e}")
        # fallback to Twilio <Say> if TTS fails
        reply_for_say = reply_text.replace("&", "and").replace("<", "").replace(">", "")
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>{reply_for_say}</Say>
  <Redirect method="POST">/twilio/voice/inbound</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

    # Serve audio back via a one-off endpoint using query token? MVP hack: inline base64 isn't supported in <Play>.
    # So simplest MVP: create a temp in-memory cache with a fixed URL.
    # We'll store one "last reply" MP3 in memory and expose /audio/last-reply.mp3.

    global LAST_REPLY_MP3
    LAST_REPLY_MP3 = mp3_bytes

    twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>https://helloagain-calls-production.up.railway.app/audio/last-reply.mp3</Play>

  <Gather input="speech" action="/twilio/voice/turn" method="POST" speechTimeout="auto" timeout="6">
    <Say>And?</Say>
  </Gather>

  <Say>Sorry, I didn’t catch that. Would you like to say a bit more?</Say>
  <Redirect method="POST">/twilio/voice/inbound</Redirect>
</Response>
"""
    return Response(content=twiml, media_type="application/xml")

@app.api_route("/twilio/voice/status", methods=["GET", "POST"])
async def twilio_voice_status(request: Request):
    form = {}
    try:
        form = dict(await request.form())
    except Exception:
        pass
    print("TWILIO STATUS:", form)
    return PlainTextResponse("ok")

LAST_REPLY_MP3: bytes | None = None

@app.get("/audio/last-reply.mp3")
async def last_reply_mp3():
    global LAST_REPLY_MP3
    if LAST_REPLY_MP3 is None:
        return Response(content=b"", media_type="audio/mpeg")
    return Response(content=LAST_REPLY_MP3, media_type="audio/mpeg")

# Cache the greeting in memory so we don't regenerate every call
MARGARET_GREETING_MP3: bytes | None = None

GREETING_TEXT = (
    "Hello, it’s Margaret from HelloAgain. "
    "I was just giving you a little call to check in and see how you’re doing today. "
    "I’ve got a bit of time for a chat if that's ok with you. "
    "How has your day been so far?"
    "If you'd like to chat just say 'hello' "
)

@app.get("/audio/margaret-greeting.mp3")
async def margaret_greeting_mp3():
    global MARGARET_GREETING_MP3
    if MARGARET_GREETING_MP3 is not None:
        return Response(content=MARGARET_GREETING_MP3, media_type="audio/mpeg")

    api_key = os.environ["ELEVENLABS_API_KEY"]
    voice_id = os.environ["ELEVENLABS_MARGARET_VOICE_ID"]
    model_id = os.environ.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_22050_16"
    headers = {
        "xi-api-key": api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    payload = {
        "text": GREETING_TEXT,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        MARGARET_GREETING_MP3 = r.content

    return Response(content=MARGARET_GREETING_MP3, media_type="audio/mpeg")
