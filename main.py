import os
import time
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from supabase import create_client

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
