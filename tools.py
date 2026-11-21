"""
tools.py — Active Function-Calling Tools for JARVIS 2.2
Includes Phonebook Contacts (.vcf import), Call/WhatsApp/SIM routing,
Real Gmail SMTP, Timed Daily Reminders, Learned Skills, Music, & Social Apps.
"""

import os
import re
import sys
import smtplib
import logging
import webbrowser
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote_plus
from typing import Optional, Dict, Any

from memory import memory

logger = logging.getLogger("JarvisTools")


def _parse_due_time(due_time: Optional[str]) -> Optional[str]:
    if not due_time or not due_time.strip():
        return None
    raw = due_time.strip()
    try:
        return datetime.fromisoformat(raw).isoformat(timespec="minutes")
    except ValueError:
        pass
    now = datetime.now()
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            t = datetime.strptime(raw.upper(), fmt).time()
            candidate = datetime.combine(now.date(), t)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate.isoformat(timespec="minutes")
        except ValueError:
            continue
    return None


def import_vcf_contacts(vcf_text: str) -> int:
    """Parse an Android/iOS .vcf (vCard) export and save contacts into SQLite."""
    imported = 0
    blocks = re.split(r"END:VCARD", vcf_text, flags=re.IGNORECASE)
    for block in blocks:
        if "BEGIN:VCARD" not in block.upper():
            continue
        fn_match = re.search(r"^FN(?:;[^:]*)?:(.+)$", block, flags=re.MULTILINE | re.IGNORECASE)
        n_match = re.search(r"^N(?:;[^:]*)?:([^;\r\n]+)", block, flags=re.MULTILINE | re.IGNORECASE)
        tel_match = re.search(r"^TEL(?:;[^:]*)?:(.+)$", block, flags=re.MULTILINE | re.IGNORECASE)
        email_match = re.search(r"^EMAIL(?:;[^:]*)?:(.+)$", block, flags=re.MULTILINE | re.IGNORECASE)

        name = (fn_match.group(1) if fn_match else (n_match.group(1) if n_match else "")).strip()
        phone = re.sub(r"[^\d+]", "", tel_match.group(1)) if tel_match else ""
        email = email_match.group(1).strip() if email_match else ""

        if name and (phone or email):
            memory.save_contact(name=name, phone=phone, email=email)
            imported += 1
    return imported


def play_music(name: str) -> Dict[str, Any]:
    try:
        from musicLibrary import music
        key = name.strip().lower()
        url = music.get(key)
        if not url:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(name)}"
            return {"status": "search", "song": name, "url": search_url, "open_url": search_url, "message": f"Searching YouTube for {name}"}
        return {"status": "success", "song": key, "url": url, "open_url": url, "message": f"Playing {key}"}
    except Exception as e:
        logger.error("play_music error: %s", e)
        return {"status": "error", "song": name, "url": "", "message": "Could not start music"}


def open_platform(platform: str, query: str = "") -> Dict[str, Any]:
    """Open or search social media & web platforms (YouTube, WhatsApp, Instagram, Facebook, LinkedIn, Google)."""
    p = platform.strip().lower()
    q = quote_plus(query.strip()) if query else ""
    urls = {
        "youtube": f"https://www.youtube.com/results?search_query={q}" if q else "https://www.youtube.com",
        "whatsapp": "https://web.whatsapp.com",
        "instagram": f"https://www.instagram.com/{query.strip().lstrip('@')}/" if query else "https://www.instagram.com",
        "facebook": f"https://www.facebook.com/search/top?q={q}" if q else "https://www.facebook.com",
        "linkedin": f"https://www.linkedin.com/search/results/all/?keywords={q}" if q else "https://www.linkedin.com",
        "twitter": f"https://x.com/search?q={q}" if q else "https://x.com",
        "x": f"https://x.com/search?q={q}" if q else "https://x.com",
        "google": f"https://www.google.com/search?q={q}" if q else "https://www.google.com",
        "gmail": "https://mail.google.com",
    }
    target_url = urls.get(p, f"https://www.google.com/search?q={quote_plus(platform + ' ' + query)}")
    return {"status": "success", "platform": p, "open_url": target_url, "message": f"Opening {platform}, Sir."}


def call_contact(name: str, via: str = "ask") -> Dict[str, Any]:
    """Look up a contact in the phonebook and initiate a SIM or WhatsApp call."""
    contact = memory.get_contact(name)
    if not contact or not contact.get("phone"):
        return {
            "status": "not_found",
            "message": f"I could not find {name} in your saved phonebook, Sir. Please share their number or import your contacts.",
        }
    phone = contact["phone"]
    display = contact["display_name"]
    v = via.strip().lower()
    if v in ("sim", "phone", "mobile"):
        return {"status": "ready", "open_url": f"tel:{phone}", "message": f"Dialing {display} on SIM now, Sir."}
    if v == "whatsapp":
        wa_num = re.sub(r"[^\d]", "", phone)
        return {"status": "ready", "open_url": f"https://wa.me/{wa_num}", "message": f"Opening WhatsApp for {display}, Sir."}
    return {
        "status": "need_channel",
        "contact": contact,
        "message": f"I found {display} ({phone}) in your phonebook, Sir. Should I place this via SIM or WhatsApp?",
    }


def add_todo(task: str, due_time: Optional[str] = None, recurrence: str = "once") -> Dict[str, Any]:
    try:
        parsed_due = _parse_due_time(due_time)
        item = memory.add_todo(task, due_time=parsed_due, recurrence=recurrence)
        msg = f"Todo added: {item['task']}"
        if parsed_due:
            readable = datetime.fromisoformat(parsed_due).strftime("%I:%M %p")
            msg += f" (reminder set for {readable}, {item['recurrence']})"
        return {"status": "success", "todo_id": item["id"], "task": item["task"], "due_time": parsed_due, "message": msg}
    except Exception as e:
        logger.error("add_todo error: %s", e)
        return {"status": "error", "todo_id": None, "task": task, "message": "Could not add todo"}


def get_todo() -> Dict[str, Any]:
    try:
        rows = memory.get_todos("pending")
        if not rows:
            return {"status": "empty", "todos": [], "message": "No pending todos"}
        return {"status": "success", "todos": rows, "message": "Pending todos retrieved"}
    except Exception as e:
        logger.error("get_todo error: %s", e)
        return {"status": "error", "todos": [], "message": "Could not read todos"}


def complete_todo(todo_id: int) -> Dict[str, Any]:
    try:
        completed = memory.complete_todo(int(todo_id))
        if not completed:
            return {"status": "not_found", "todo_id": todo_id, "message": "Todo not found"}
        return {"status": "success", "todo_id": todo_id, "message": f"Todo {todo_id} completed"}
    except Exception as e:
        logger.error("complete_todo error: %s", e)
        return {"status": "error", "todo_id": todo_id, "message": "Could not complete todo"}


def daily_plan() -> Dict[str, Any]:
    result = get_todo()
    if result["status"] in ("success", "empty"):
        tasks_summary = []
        for row in result["todos"]:
            if row.get("due_time"):
                t_str = datetime.fromisoformat(row["due_time"]).strftime("%I:%M %p")
                tasks_summary.append(f"{row['task']} at {t_str}")
            else:
                tasks_summary.append(row["task"])
        return {
            "status": "plan_ready",
            "todos": result["todos"],
            "message": "Plan ready: " + (", ".join(tasks_summary) if tasks_summary else "no pending tasks"),
        }
    return {"status": "error", "todos": [], "message": "Could not prepare plan"}


def save_contact(name: str, phone: str = "", email: str = "", notes: str = "") -> Dict[str, Any]:
    try:
        contact = memory.save_contact(name=name, phone=phone, email=email, notes=notes)
        return {"status": "success", "contact": contact, "message": f"Saved contact {contact['name']}"}
    except Exception as e:
        logger.error("save_contact error: %s", e)
        return {"status": "error", "message": "Could not save contact"}


def get_contact(name: str) -> Dict[str, Any]:
    contact = memory.get_contact(name)
    if not contact:
        return {"status": "not_found", "contact": None, "message": f"No contact saved for {name}"}
    return {"status": "found", "contact": contact, "message": f"Found contact {contact['display_name']}"}


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
        items = memory.recall()
        return {"status": "recalled", "data": items, "message": "Recalled memories"}
    except Exception as e:
        logger.error("recall error: %s", e)
        return {"status": "error", "data": None, "message": "Could not recall"}


def learn_skill(name: str, trigger_phrase: str, instructions: str) -> Dict[str, Any]:
    try:
        skill = memory.learn_skill(name, trigger_phrase, instructions)
        memory.log_episode(f"Learned skill '{name}' for '{trigger_phrase}'", importance=2)
        return {"status": "learned", "skill": skill, "message": f"Skill '{name}' learned, Sir."}
    except Exception as e:
        logger.error("learn_skill error: %s", e)
        return {"status": "error", "message": "Could not learn skill"}


def _store_pending(action_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    if action_type == "email":
        desc = f"Send email to {params['to']} with subject '{params['subject']}' saying '{params['body']}'"
    elif action_type == "whatsapp":
        label = params.get("display_name") or params["contact"]
        desc = f"Send WhatsApp message to {label} ({params['contact']}) saying '{params['message']}'"
    elif action_type == "sms":
        label = params.get("display_name") or params["contact"]
        desc = f"Open SIM SMS to {label} ({params['contact']}) saying '{params['message']}'"
    else:
        desc = f"{action_type} pending"

    action_id = memory.create_pending_action(action_type, params, desc)
    return {
        "status": "pending_confirmation",
        "action_id": action_id,
        "message": f"{desc}. Should I send it, Sir? Say yes or cancel.",
    }


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
            return {"status": "missing_contact_info", "message": f"No number saved for {target}, Sir. Please tell me their number or import your contacts."}

    action_kind = "sms" if channel.strip().lower() in ("sim", "sms") else "whatsapp"
    return _store_pending(action_kind, {"contact": target, "display_name": display_name, "message": message})


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
        logger.error("web_search error: %s", e)
        return {"status": "error", "results": [], "message": "Web search timed out or unavailable"}
    

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
                    phone_no=f"+{clean_phone}",
                    message=message,
                    wait_time=12,
                    tab_close=True,
                    close_time=3,
                )
            except Exception:
                webbrowser.open(open_url)

        threading.Thread(target=_dispatch, daemon=True).start()

    memory.log_episode(f"Sent WhatsApp message to {clean_phone}")
    return {"status": "sent", "open_url": open_url, "message": f"WhatsApp message sent to {phone_or_contact}, Sir."}


def execute_pending_action(action_id: int, confirm: bool) -> Dict[str, Any]:
    pending = memory.get_latest_pending_action()
    if not pending or pending.get("id") != action_id:
        return {"message": "No pending action found, Sir.", "status": "error"}
    if not confirm:
        memory.resolve_pending_action(action_id, "cancelled")
        return {"message": "Action cancelled, Sir.", "status": "cancelled"}

    action_type = pending["action_type"]
    payload = pending["payload"]
    memory.resolve_pending_action(action_id, "completed")
    try:
        if action_type == "email":
            return _send_email_smtp(payload["to"], payload["subject"], payload["body"])
        if action_type == "whatsapp":
            return _send_whatsapp_real(payload["contact"], payload["message"])
        if action_type == "sms":
            sms_url = f"sms:{payload['contact']}?body={quote_plus(payload['message'])}"
            return {"status": "sent", "open_url": sms_url, "message": f"Opening SIM SMS for {payload.get('display_name') or payload['contact']}, Sir."}
        return {"message": "Action completed, Sir.", "status": "completed"}
    except Exception as e:
        return {"message": f"Could not complete {action_type}, Sir: {e}", "status": "error"}


TOOLS = [
    {
        "name": "play_music",
        "func": play_music,
        "description": "Play a song from the music library or search YouTube.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "open_platform",
        "func": open_platform,
        "description": "Open or search YouTube, WhatsApp, Instagram, Facebook, LinkedIn, X, Gmail, or Google.",
        "parameters": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "description": "Platform name e.g. youtube, instagram, whatsapp, facebook, linkedin, google"},
                "query": {"type": "string", "description": "Optional search term or username"},
            },
            "required": ["platform"],
        },
    },
    {
        "name": "call_contact",
        "func": call_contact,
        "description": "Check phonebook and call a contact via SIM or WhatsApp.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "via": {"type": "string", "enum": ["ask", "sim", "whatsapp"], "description": "Use 'ask' unless user specified SIM or WhatsApp"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "add_todo",
        "func": add_todo,
        "description": "Add a task or timed daily reminder.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "due_time": {"type": "string", "description": "e.g. '17:30' or '5:30 PM'"},
                "recurrence": {"type": "string", "enum": ["once", "daily"]},
            },
            "required": ["task"],
        },
    },
    {"name": "get_todo", "func": get_todo, "description": "List pending tasks.", "parameters": {"type": "object", "properties": {}}},
    {"name": "complete_todo", "func": complete_todo, "description": "Complete a todo by ID.", "parameters": {"type": "object", "properties": {"todo_id": {"type": "integer"}}, "required": ["todo_id"]}},
    {"name": "daily_plan", "func": daily_plan, "description": "Summarize today's tasks.", "parameters": {"type": "object", "properties": {}}},
    {
        "name": "save_contact",
        "func": save_contact,
        "description": "Save a contact's name, phone number, or email.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "phone": {"type": "string"}, "email": {"type": "string"}},
            "required": ["name"],
        },
    },
    {"name": "get_contact", "func": get_contact, "description": "Look up a contact.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {
        "name": "remember",
        "func": remember,
        "description": "Save a permanent memory/fact.",
        "parameters": {"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}}, "required": ["key", "value"]},
    },
    {"name": "recall", "func": recall, "description": "Retrieve saved facts.", "parameters": {"type": "object", "properties": {"key": {"type": "string"}}}},
    {
        "name": "learn_skill",
        "func": learn_skill,
        "description": "Teach Jarvis a new routine or skill.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "trigger_phrase": {"type": "string"}, "instructions": {"type": "string"}},
            "required": ["name", "trigger_phrase", "instructions"],
        },
    },
    {
        "name": "send_email",
        "func": send_email,
        "description": "Prepare an email (waits for confirmation).",
        "parameters": {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]},
    },
    {
        "name": "send_whatsapp",
        "func": send_whatsapp,
        "description": "Prepare a WhatsApp or SIM SMS message (waits for confirmation).",
        "parameters": {
            "type": "object",
            "properties": {
                "contact": {"type": "string"},
                "message": {"type": "string"},
                "channel": {"type": "string", "enum": ["whatsapp", "sim"]},
            },
            "required": ["contact", "message"],
        },
    },
    {"name": "web_search", "func": web_search, "description": "Search the web.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]

FUNCS = {tool["name"]: tool["func"] for tool in TOOLS}