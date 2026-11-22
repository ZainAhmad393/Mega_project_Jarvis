"""
tools.py — Active Function-Calling Tools for JARVIS 2.5
Contacts + Social Messaging (IG/FB/TikTok/X/LinkedIn), Numbered Tasks with fuzzy completion,
Daily/Weekly Task Reports, Jarvis Self-Diagnostic Reports, Weather & Briefing,
Confirmed Email/WhatsApp/SMS, Gmail SMTP, Music, Skills.
"""

import os
import re
import sys
import quopri
import smtplib
import logging
import webbrowser
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote_plus
from typing import Optional, Dict, Any

from memory import memory, local_now

logger = logging.getLogger("JarvisTools")


# ------------------------------------------------------------------ helpers
def format_due_readable(iso_str: Optional[str]) -> str:
    """'2026-11-23T09:30' -> 'Tomorrow at 9:30 AM' (never show raw ISO to the user)."""
    if not iso_str:
        return "No deadline"
    try:
        dt = datetime.fromisoformat(str(iso_str))
        today = local_now().date()
        time_part = dt.strftime("%I:%M %p").lstrip("0")
        if dt.date() == today:
            return f"Today at {time_part}"
        if dt.date() == today + timedelta(days=1):
            return f"Tomorrow at {time_part}"
        if dt.date() == today - timedelta(days=1):
            return f"Yesterday at {time_part}"
        return dt.strftime("%a, %b %d at ") + time_part
    except (ValueError, TypeError):
        return str(iso_str)


def _parse_due_time(due_time: Optional[str]) -> Optional[str]:
    if not due_time or not str(due_time).strip():
        return None
    raw = str(due_time).strip()
    try:
        return datetime.fromisoformat(raw).isoformat(timespec="minutes")
    except ValueError:
        pass
    now = local_now()
    lower = raw.lower()
    day_offset = 0
    if lower.startswith("tomorrow"):
        day_offset = 1
        raw = re.sub(r"^tomorrow\s*(at\s*)?", "", lower, flags=re.IGNORECASE).strip()
    elif lower.startswith("today"):
        raw = re.sub(r"^today\s*(at\s*)?", "", lower, flags=re.IGNORECASE).strip()
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            t = datetime.strptime(raw.upper(), fmt).time()
            candidate = datetime.combine(now.date() + timedelta(days=day_offset), t)
            if day_offset == 0 and candidate <= now:
                candidate += timedelta(days=1)
            return candidate.isoformat(timespec="minutes")
        except ValueError:
            continue
    return None


def _vcf_field(block: str, field: str) -> str:
    """Read one vCard field, decoding QUOTED-PRINTABLE values (common in Android exports)."""
    m = re.search(rf"^{field}((?:;[^:\r\n]*)?):(.+)$", block, flags=re.MULTILINE | re.IGNORECASE)
    if not m:
        return ""
    params, value = m.group(1), m.group(2).strip()
    if "QUOTED-PRINTABLE" in params.upper():
        try:
            value = quopri.decodestring(value.encode("utf-8")).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return value


def import_vcf_contacts(vcf_text: str) -> int:
    """Parse an Android/iOS .vcf (vCard) export and save contacts."""
    vcf_text = re.sub(r"=\r?\n", "", vcf_text)
    vcf_text = re.sub(r"\r?\n[ \t]", "", vcf_text)

    imported = 0
    for block in re.split(r"END:VCARD", vcf_text, flags=re.IGNORECASE):
        if "BEGIN:VCARD" not in block.upper():
            continue
        name = _vcf_field(block, "FN")
        if not name:
            parts = _vcf_field(block, "N").split(";")
            given = parts[1].strip() if len(parts) > 1 else ""
            family = parts[0].strip() if parts else ""
            name = f"{given} {family}".strip()

        phone = re.sub(r"[^\d+]", "", _vcf_field(block, "TEL"))
        email = _vcf_field(block, "EMAIL")
        if name and (phone or email):
            memory.save_contact(name=name, phone=phone, email=email)
            imported += 1
    return imported


# ------------------------------------------------------------------ weather & briefing
_WMO_WEATHER = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "moderate rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def get_weather(city: str = "") -> Dict[str, Any]:
    """Live weather via free Open-Meteo (no API key)."""
    import requests

    target_city = (city or "").strip()
    if not target_city:
        saved = memory.recall("home_city")
        target_city = saved["value"] if saved else "Lahore"

    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": target_city, "count": 1, "language": "en"},
            timeout=4,
        ).json()
        results = geo.get("results") or []
        if not results:
            return {"status": "not_found", "message": f"I could not find weather data for {target_city}, Sir."}

        loc = results[0]
        lat, lon = loc["latitude"], loc["longitude"]
        resolved_name = loc.get("name", target_city)

        wx = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min",
                "timezone": "auto", "forecast_days": 1,
            },
            timeout=4,
        ).json()

        cur = wx.get("current", {})
        daily = wx.get("daily", {})
        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        humidity = cur.get("relative_humidity_2m")
        cond = _WMO_WEATHER.get(cur.get("weather_code", 0), "clear")
        t_max = (daily.get("temperature_2m_max") or [None])[0]
        t_min = (daily.get("temperature_2m_min") or [None])[0]

        msg = f"In {resolved_name}, it is currently {temp} degrees Celsius with {cond}, feels like {feels} degrees, humidity {humidity} percent."
        if t_max is not None and t_min is not None:
            msg += f" Today's high is {t_max} and low is {t_min} degrees."
        return {"status": "success", "city": resolved_name, "temperature_c": temp, "condition": cond, "message": msg}
    except Exception as e:
        memory.log_issue("tool_error", "weather", f"Open-Meteo failed: {e}")
        return {"status": "error", "message": "Weather service is temporarily unavailable, Sir."}


def morning_briefing(city: str = "") -> Dict[str, Any]:
    now_str = local_now().strftime("%A, %I:%M %p")
    wx = get_weather(city)
    todos = memory.get_todos("pending")
    if todos:
        parts = [
            f"{idx}. {t['task']}" + (f" ({format_due_readable(t['due_time'])})" if t.get("due_time") else "")
            for idx, t in enumerate(todos[:5], 1)
        ]
        tasks_text = f"You have {len(todos)} pending tasks: " + ", ".join(parts) + "."
    else:
        tasks_text = "Your schedule is currently clear with no pending tasks."
    return {"status": "success", "message": f"Good day, Sir. It is {now_str}. {wx.get('message', '')} {tasks_text}".replace("  ", " ")}


# ------------------------------------------------------------------ media / web
def play_music(name: str) -> Dict[str, Any]:
    key = name.strip().lower()
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(name)}"
    try:
        try:
            from musicLibrary import music
        except ImportError:
            music = {}
        url = music.get(key)
        if not url:
            return {"status": "search", "song": name, "url": search_url, "open_url": search_url, "message": f"Searching YouTube for {name}"}
        return {"status": "success", "song": key, "url": url, "open_url": url, "message": f"Playing {key}"}
    except Exception as e:
        memory.log_issue("tool_error", "music", str(e))
        return {"status": "search", "song": name, "url": search_url, "open_url": search_url, "message": f"Searching YouTube for {name}"}


def open_platform(platform: str, query: str = "") -> Dict[str, Any]:
    p = platform.strip().lower()
    q = quote_plus(query.strip()) if query else ""
    urls = {
        "youtube": f"https://www.youtube.com/results?search_query={q}" if q else "https://www.youtube.com",
        "whatsapp": "https://web.whatsapp.com",
        "instagram": f"https://www.instagram.com/{query.strip().lstrip('@')}/" if query else "https://www.instagram.com",
        "facebook": f"https://www.facebook.com/search/top?q={q}" if q else "https://www.facebook.com",
        "tiktok": f"https://www.tiktok.com/@{query.strip().lstrip('@')}" if query else "https://www.tiktok.com",
        "linkedin": f"https://www.linkedin.com/search/results/all/?keywords={q}" if q else "https://www.linkedin.com",
        "twitter": f"https://x.com/search?q={q}" if q else "https://x.com",
        "x": f"https://x.com/search?q={q}" if q else "https://x.com",
        "google": f"https://www.google.com/search?q={q}" if q else "https://www.google.com",
        "gmail": "https://mail.google.com",
    }
    target_url = urls.get(p, f"https://www.google.com/search?q={quote_plus(platform + ' ' + query)}")
    return {"status": "success", "platform": p, "open_url": target_url, "message": f"Opening {platform}, Sir."}


def web_search(query: str) -> Dict[str, Any]:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS(timeout=4) as ddgs:
            results = list(ddgs.text(query, max_results=3))
        if results:
            snippets = [f"{r.get('title', '')}: {r.get('body', '')}" for r in results]
            return {"status": "success", "results": results, "message": " | ".join(snippets)}
        return {"status": "empty", "results": [], "message": f"No web results found for {query}"}
    except Exception as e:
        memory.log_issue("tool_error", "web_search", str(e))
        return {"status": "error", "results": [], "message": "Web search timed out or unavailable"}


# ------------------------------------------------------------------ contacts / calls
def call_contact(name: str, via: str = "ask") -> Dict[str, Any]:
    contact = memory.get_contact(name)
    if not contact or not contact.get("phone"):
        return {"status": "not_found", "message": f"I could not find a phone number for {name} in Contacts, Sir."}
    phone = contact["phone"]
    display = contact["display_name"]
    v = (via or "ask").strip().lower()
    if v in ("sim", "phone", "mobile"):
        return {"status": "ready", "open_url": f"tel:{phone}", "message": f"Dialing {display} on SIM now, Sir."}
    if v == "whatsapp":
        wa_num = re.sub(r"[^\d]", "", phone)
        return {"status": "ready", "open_url": f"https://wa.me/{wa_num}", "message": f"Opening WhatsApp for {display}, Sir."}
    return {"status": "need_channel", "contact": contact, "message": f"I found {display} ({phone}) in Contacts, Sir. Should I place this via SIM or WhatsApp?"}


def save_contact(
    name: str,
    phone: str = "",
    email: str = "",
    instagram: str = "",
    facebook: str = "",
    tiktok: str = "",
    twitter: str = "",
    linkedin: str = "",
    notes: str = "",
) -> Dict[str, Any]:
    try:
        contact = memory.save_contact(
            name=name, phone=phone, email=email,
            instagram=instagram, facebook=facebook, tiktok=tiktok, twitter=twitter, linkedin=linkedin,
            notes=notes,
        )
        return {"status": "success", "contact": contact, "message": f"Saved contact {contact['name']}"}
    except Exception as e:
        memory.log_issue("tool_error", "contacts", str(e))
        return {"status": "error", "message": "Could not save contact"}


def get_contact(name: str) -> Dict[str, Any]:
    contact = memory.get_contact(name)
    if not contact:
        return {"status": "not_found", "contact": None, "message": f"No contact saved for {name}"}
    return {"status": "found", "contact": contact, "message": f"Found contact {contact['display_name']}"}


# ------------------------------------------------------------------ tasks / schedule
def add_todo(task: str, due_time: Optional[str] = None, recurrence: str = "once", remind_before_mins: int = 30) -> Dict[str, Any]:
    try:
        parsed_due = _parse_due_time(due_time)
        item = memory.add_todo(task, due_time=parsed_due, recurrence=recurrence, remind_before_mins=remind_before_mins)
        msg = f"Task added: {item['task']}"
        if parsed_due:
            msg += f" ({format_due_readable(parsed_due)}, {item['recurrence']}, reminders set 30 minutes and 5 minutes before)"
        elif due_time:
            msg += f" (I could not understand the time '{due_time}', so no reminder was set)"
        return {"status": "success", "todo_id": item["id"], "task": item["task"], "due_time": parsed_due, "message": msg}
    except Exception as e:
        memory.log_issue("tool_error", "tasks", str(e))
        return {"status": "error", "todo_id": None, "task": task, "message": "Could not add task"}


def get_todo() -> Dict[str, Any]:
    try:
        rows = memory.get_todos("pending")
        if not rows:
            return {"status": "empty", "count": 0, "todos": [], "message": "You have 0 pending tasks, Sir."}
        lines = [
            f"{idx}. {r['task']}" + (f" ({format_due_readable(r['due_time'])})" if r.get("due_time") else "")
            for idx, r in enumerate(rows, 1)
        ]
        return {"status": "success", "count": len(rows), "todos": rows,
                "message": f"You have {len(rows)} pending tasks: " + "; ".join(lines)}
    except Exception as e:
        memory.log_issue("tool_error", "tasks", str(e))
        return {"status": "error", "count": 0, "todos": [], "message": "Could not read tasks"}


def complete_todo(
    task_number: Optional[int] = None,
    task_name: Optional[str] = None,
    todo_id: Optional[int] = None,
) -> Dict[str, Any]:
    try:
        done_item = None
        if todo_id is not None:
            done_item = memory.complete_todo(todo_id=int(todo_id))
        if not done_item and task_number is not None:
            pending = memory.get_todos("pending")
            idx = int(task_number) - 1
            if 0 <= idx < len(pending):
                done_item = memory.complete_todo(todo_id=pending[idx]["id"])
        if not done_item and task_name:
            done_item = memory.complete_todo(task_name=task_name)

        if not done_item:
            return {"status": "not_found", "message": "I could not find that pending task, Sir."}

        msg = f"Task completed and logged: {done_item['task']}."
        if done_item.get("recurrence") == "daily":
            msg = f"Completed today's daily task: {done_item['task']}. I have logged it and scheduled the next one for tomorrow."
        return {"status": "success", "todo_id": done_item["id"], "message": msg}
    except Exception as e:
        memory.log_issue("tool_error", "tasks", str(e))
        return {"status": "error", "message": "Could not complete task"}


def daily_plan() -> Dict[str, Any]:
    result = get_todo()
    if result["status"] in ("success", "empty"):
        tasks_summary = [
            f"{row['task']}" + (f" ({format_due_readable(row['due_time'])})" if row.get("due_time") else "")
            for row in result["todos"]
        ]
        return {"status": "plan_ready", "todos": result["todos"],
                "message": "Plan ready: " + (", ".join(tasks_summary) if tasks_summary else "no pending tasks")}
    return {"status": "error", "todos": [], "message": "Could not prepare plan"}


def get_task_report(period: str = "daily") -> Dict[str, Any]:
    p = "weekly" if "week" in (period or "").lower() else "daily"
    rep = memory.get_task_report(p)
    label = "this week" if p == "weekly" else "today"
    completed_names = ", ".join(c["task"] for c in rep["completed_tasks"][:5])
    msg = (
        f"For {label}, you completed {rep['completed_count']} tasks "
        f"({rep['completion_rate']} percent completion rate) and have {rep['pending_count']} pending."
    )
    if completed_names:
        msg += f" Completed: {completed_names}."
    return {"status": "success", "report": rep, "message": msg}


def get_jarvis_report(period: str = "weekly") -> Dict[str, Any]:
    p = "monthly" if "month" in (period or "").lower() else "weekly"
    rep = memory.get_system_report(p)
    if rep["total_issues"] == 0:
        return {"status": "success", "report": rep, "message": f"Zero issues logged in the {p} diagnostics, Sir. All systems ran smoothly."}
    comp_summary = ", ".join(f"{k}: {v}" for k, v in rep["by_component"].items()) or "all resolved"
    return {
        "status": "success",
        "report": rep,
        "message": f"In the {p} self-analysis, I logged {rep['total_issues']} issues ({rep['unresolved_count']} unresolved). Breakdown: {comp_summary}.",
    }


# ------------------------------------------------------------------ memory / skills
def remember(key: str, value: str) -> Dict[str, Any]:
    try:
        item = memory.remember(key, value)
        return {"status": "remembered", "key": item["key"], "value": item["value"], "message": f"Remembered {key}: {value}"}
    except Exception as e:
        logger.error("remember error: %s", e)
        return {"status": "error", "key": key, "value": value, "message": "Could not remember"}


def recall(key: Optional[str] = None) -> Dict[str, Any]:
    try:
        if key:
            item = memory.recall(key)
            if not item:
                return {"status": "not_found", "data": None, "message": "No matching memory"}
            return {"status": "recalled", "data": item, "message": f"Recalled {key}"}
        return {"status": "recalled", "data": memory.recall(), "message": "Recalled memories"}
    except Exception as e:
        logger.error("recall error: %s", e)
        return {"status": "error", "data": None, "message": "Could not recall"}


def learn_skill(name: str, trigger_phrase: str, instructions: str) -> Dict[str, Any]:
    try:
        skill = memory.learn_skill(name, trigger_phrase, instructions)
        memory.log_episode(f"Learned skill '{name}' for '{trigger_phrase}'", importance=2)
        return {"status": "learned", "skill": skill, "message": f"Skill '{name}' learned, Sir."}
    except Exception as e:
        memory.log_issue("tool_error", "skills", str(e))
        return {"status": "error", "message": "Could not learn skill"}


# ------------------------------------------------------------------ messaging (confirmation flow)
def _store_pending(action_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action_type == "email":
        desc = f"Send email to {params['to']} with subject '{params['subject']}' saying '{params['body']}'"
    elif action_type == "whatsapp":
        label = params.get("display_name") or params["contact"]
        desc = f"Send WhatsApp message to {label} ({params['contact']}) saying '{params['message']}'"
    elif action_type == "sms":
        label = params.get("display_name") or params["contact"]
        desc = f"Open SIM SMS to {label} ({params['contact']}) saying '{params['message']}'"
    elif action_type == "social":
        desc = f"Open {params['platform'].title()} for {params['display_name']} (@{params['handle']}) with message '{params['message']}'"
    else:
        desc = f"{action_type} pending"

    action_id = memory.create_pending_action(action_type, params, desc)
    return {"status": "pending_confirmation", "action_id": action_id,
            "message": f"{desc}. Should I proceed, Sir? Say yes or cancel."}


def send_email(to: str, subject: str, body: str) -> Dict[str, Any]:
    recipient = to.strip()
    if "@" not in recipient:
        contact = memory.get_contact(recipient)
        if contact and contact.get("email"):
            recipient = contact["email"]
        else:
            return {"status": "missing_contact_info", "message": f"No email saved for {to}, Sir. What is their email?"}
    return _store_pending("email", {"to": recipient, "subject": subject, "body": body})


def send_whatsapp(contact: str = "", message: str = "", contact_or_phone: str = "", channel: str = "whatsapp") -> Dict[str, Any]:
    target = (contact_or_phone or contact).strip()
    if not target:
        return {"status": "missing_contact_info", "message": "Who should I message, Sir?"}

    display_name = target
    digits = re.sub(r"[^\d+]", "", target)
    if len(digits) < 7:
        saved = memory.get_contact(target)
        if saved and saved.get("phone"):
            display_name = saved["display_name"]
            target = saved["phone"]
        else:
            return {"status": "missing_contact_info", "message": f"No number saved for {target}, Sir. Please add their number in Contacts."}

    action_kind = "sms" if (channel or "").strip().lower() in ("sim", "sms") else "whatsapp"
    return _store_pending(action_kind, {"contact": target, "display_name": display_name, "message": message})


def send_social_message(contact: str, platform: str, message: str) -> Dict[str, Any]:
    p = (platform or "").strip().lower()
    p = {"insta": "instagram", "ig": "instagram", "fb": "facebook", "x": "twitter", "tweet": "twitter"}.get(p, p)
    if p not in ("instagram", "facebook", "tiktok", "twitter", "linkedin"):
        return {"status": "unsupported", "message": "Please specify Instagram, Facebook, TikTok, X, or LinkedIn, Sir."}

    saved = memory.get_contact(contact)
    if not saved:
        return {"status": "missing_contact_info", "message": f"I don't have {contact} in Contacts yet, Sir."}

    handle = (saved.get(p) or "").strip()
    if not handle:
        return {"status": "missing_contact_info",
                "message": f"I don't have {saved['display_name']}'s {p.title()} handle saved yet, Sir. Please add it in Contacts."}

    return _store_pending("social", {
        "display_name": saved["display_name"], "platform": p,
        "handle": handle.lstrip("@"), "message": message,
    })


def _send_email_smtp(to_addr: str, subject: str, body: str) -> Dict[str, Any]:
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD", "").strip()
    if not gmail_user or not gmail_pass or "your_email" in gmail_user or "your_16_char" in gmail_pass:
        return {"status": "config_missing", "message": "Email confirmed, Sir, but Gmail credentials are not configured in .env yet."}

    msg = EmailMessage()
    msg["From"] = gmail_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)
    memory.log_episode(f"Sent email to {to_addr}: {subject}")
    return {"status": "sent", "message": f"Email sent to {to_addr}, Sir."}


def _send_whatsapp_real(phone_or_contact: str, message: str) -> Dict[str, Any]:
    clean_phone = re.sub(r"[^\d]", "", phone_or_contact)
    open_url = f"https://wa.me/{clean_phone}?text={quote_plus(message)}"

    if sys.platform == "win32":
        def _dispatch():
            try:
                import pywhatkit
                pywhatkit.sendwhatmsg_instantly(
                    phone_no=f"+{clean_phone}", message=message, wait_time=12, tab_close=True, close_time=3,
                )
            except Exception:
                webbrowser.open(open_url)

        threading.Thread(target=_dispatch, daemon=True).start()

    memory.log_episode(f"Sent WhatsApp message to {clean_phone}")
    return {"status": "sent", "open_url": open_url, "message": f"WhatsApp message ready for {phone_or_contact}, Sir."}


def _open_social_dm(platform: str, handle: str, message: str, display_name: str) -> Dict[str, Any]:
    h = handle.lstrip("@")
    is_url = h.startswith("http")
    links = {
        "instagram": h if is_url else f"https://ig.me/m/{h}",
        "facebook": h if is_url else f"https://m.me/{h}",
        "tiktok": h if is_url else f"https://www.tiktok.com/@{h}",
        "twitter": h if is_url else f"https://x.com/{h}",
        "linkedin": h if is_url else f"https://www.linkedin.com/in/{h}/",
    }
    url = links.get(platform, "https://www.google.com")
    memory.log_episode(f"Prepared {platform} message to {display_name}")
    return {"status": "sent", "open_url": url,
            "message": f"Opening {platform.title()} for {display_name}, Sir. Your message is ready: {message}"}


def execute_pending_action(action_id: int, confirm: bool) -> Dict[str, Any]:
    pending = memory.get_latest_pending_action()
    if not pending or pending.get("id") != action_id:
        return {"message": "No pending action found, Sir.", "status": "error"}
    if not confirm:
        memory.resolve_pending_action(action_id, "cancelled")
        return {"message": "Action cancelled, Sir.", "status": "cancelled"}

    action_type = pending["action_type"]
    payload = pending["payload"]
    try:
        if action_type == "email":
            result = _send_email_smtp(payload["to"], payload["subject"], payload["body"])
        elif action_type == "whatsapp":
            result = _send_whatsapp_real(payload["contact"], payload["message"])
        elif action_type == "sms":
            sms_url = f"sms:{payload['contact']}?body={quote_plus(payload['message'])}"
            result = {"status": "sent", "open_url": sms_url,
                      "message": f"Opening SIM SMS for {payload.get('display_name') or payload['contact']}, Sir."}
        elif action_type == "social":
            result = _open_social_dm(payload["platform"], payload["handle"], payload["message"], payload["display_name"])
        else:
            result = {"message": "Action completed, Sir.", "status": "completed"}
        memory.resolve_pending_action(action_id, "completed")
        return result
    except Exception as e:
        memory.log_issue("action_error", action_type, str(e))
        memory.resolve_pending_action(action_id, "failed")
        return {"message": f"Could not complete {action_type}, Sir: {e}", "status": "error"}


# ------------------------------------------------------------------ registry
TOOLS = [
    {"name": "get_weather", "func": get_weather, "description": "Get live weather for any city.",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
    {"name": "morning_briefing", "func": morning_briefing, "description": "Daily briefing with time, weather, and pending tasks.",
     "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
    {"name": "play_music", "func": play_music, "description": "Play a song or search YouTube.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "open_platform", "func": open_platform, "description": "Open YouTube, WhatsApp, Instagram, Facebook, TikTok, LinkedIn, X, Gmail, or Google.",
     "parameters": {"type": "object", "properties": {"platform": {"type": "string"}, "query": {"type": "string"}}, "required": ["platform"]}},
    {"name": "call_contact", "func": call_contact, "description": "Check Contacts and call someone via SIM or WhatsApp.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "via": {"type": "string", "enum": ["ask", "sim", "whatsapp"]}}, "required": ["name"]}},
    {"name": "send_social_message", "func": send_social_message,
     "description": "Prepare a message for a contact on Instagram, Facebook, TikTok, X/Twitter, or LinkedIn (asks confirmation first).",
     "parameters": {"type": "object", "properties": {"contact": {"type": "string"}, "platform": {"type": "string", "enum": ["instagram", "facebook", "tiktok", "twitter", "linkedin"]}, "message": {"type": "string"}}, "required": ["contact", "platform", "message"]}},
    {"name": "add_todo", "func": add_todo,
     "description": "Add a task or timed reminder (notifies 30 minutes before, 5 minutes before, and at due time). Convert times like '5 baje' to '17:00' or ISO date-time.",
     "parameters": {"type": "object", "properties": {"task": {"type": "string"}, "due_time": {"type": "string", "description": "e.g. '2026-11-25T17:30', 'tomorrow 5pm', or '5:30 PM'"}, "recurrence": {"type": "string", "enum": ["once", "daily"]}, "remind_before_mins": {"type": "integer", "description": "Default 30"}}, "required": ["task"]}},
    {"name": "get_todo", "func": get_todo, "description": "Show the count and numbered list of pending tasks.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "complete_todo", "func": complete_todo,
     "description": "Mark a task completed. Use task_number or task_name.",
     "parameters": {"type": "object", "properties": {"task_number": {"type": "integer"}, "task_name": {"type": "string"}}}},
    {"name": "daily_plan", "func": daily_plan, "description": "Summarize today's pending tasks.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "get_task_report", "func": get_task_report, "description": "Get a daily or weekly report of completed and pending tasks.",
     "parameters": {"type": "object", "properties": {"period": {"type": "string", "enum": ["daily", "weekly"]}}}},
    {"name": "get_jarvis_report", "func": get_jarvis_report, "description": "Get Jarvis' weekly or monthly self-analysis report of issues faced.",
     "parameters": {"type": "object", "properties": {"period": {"type": "string", "enum": ["weekly", "monthly"]}}}},
    {"name": "save_contact", "func": save_contact, "description": "Save a contact with phone, email, or social handles.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}, "instagram": {"type": "string"}, "facebook": {"type": "string"}, "tiktok": {"type": "string"}, "twitter": {"type": "string"}, "linkedin": {"type": "string"}}, "required": ["name"]}},
    {"name": "get_contact", "func": get_contact, "description": "Look up a contact.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "remember", "func": remember, "description": "Save a permanent memory/fact.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]}},
    {"name": "recall", "func": recall, "description": "Retrieve saved facts.",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}}}},
    {"name": "learn_skill", "func": learn_skill, "description": "Teach Jarvis a new routine or skill.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "trigger_phrase": {"type": "string"}, "instructions": {"type": "string"}}, "required": ["name", "trigger_phrase", "instructions"]}},
    {"name": "send_email", "func": send_email, "description": "Prepare an email (waits for confirmation).",
     "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
    {"name": "send_whatsapp", "func": send_whatsapp, "description": "Prepare a WhatsApp or SIM SMS message (waits for confirmation).",
     "parameters": {"type": "object", "properties": {"contact": {"type": "string"}, "message": {"type": "string"}, "channel": {"type": "string", "enum": ["whatsapp", "sim"]}}, "required": ["contact", "message"]}},
    {"name": "web_search", "func": web_search, "description": "Search the web.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

FUNCS = {tool["name"]: tool["func"] for tool in TOOLS}