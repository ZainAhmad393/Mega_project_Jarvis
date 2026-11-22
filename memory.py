"""
JARVIS Persistent Memory & Diagnostic Storage
Supports Permanent Cloud PostgreSQL (via DATABASE_URL) and Local SQLite fallback.
Handles Scheduled Tasks + Pre-Reminders, Contacts + Social Handles, Task Reports,
Learned Skills, Episodic Memories, Chat History, Pending Actions, & System Issue Diagnostics.
"""

import os
import sqlite3
import json
import logging
from contextlib import contextmanager
from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("JarvisMemory")

DB_PATH = os.getenv("DATABASE_PATH", "jarvis.db")


class MemoryManager:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(MemoryManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_path: str = DB_PATH, database_url: Optional[str] = None):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self.db_path = db_path
        env_url = os.getenv("DATABASE_URL", "").strip() if database_url is None else database_url.strip()
        self.database_url = env_url if (env_url.startswith(("postgres://", "postgresql://")) and db_path == DB_PATH) else ""
        self.is_postgres = bool(self.database_url)
        self._init_db()
        self._initialized = True

    def close(self):
        self._initialized = False

    @classmethod
    def reset_instance(cls, db_path: str = DB_PATH):
        cls._instance = None
        inst = cls(db_path=db_path, database_url="")
        inst.db_path = db_path
        inst._init_db()
        global memory
        memory = inst
        return inst

    @contextmanager
    def _get_connection(self):
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row
            conn = psycopg.connect(self.database_url, row_factory=dict_row)
            try:
                yield conn
            finally:
                conn.close()
        else:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    def _q(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def _insert_get_id(self, cursor, sql: str, params: tuple) -> int:
        if self.is_postgres:
            cursor.execute(self._q(sql) + " RETURNING id", params)
            return int(cursor.fetchone()["id"])
        cursor.execute(sql, params)
        return int(cursor.lastrowid)

    def _ensure_column(self, cursor, table: str, column: str, definition: str):
        if self.is_postgres:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        else:
            cursor.execute(f"PRAGMA table_info({table})")
            cols = {row["name"] for row in cursor.fetchall()}
            if column not in cols:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_db(self):
        id_col = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS todos (
                    id {id_col},
                    task TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    due_time TEXT DEFAULT NULL,
                    recurrence TEXT DEFAULT 'once',
                    reminded INTEGER DEFAULT 0,
                    pre_reminded INTEGER DEFAULT 0,
                    remind_before_mins INTEGER DEFAULT 15,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT DEFAULT NULL
                )
            """)
            self._ensure_column(cursor, "todos", "due_time", "TEXT DEFAULT NULL")
            self._ensure_column(cursor, "todos", "recurrence", "TEXT DEFAULT 'once'")
            self._ensure_column(cursor, "todos", "reminded", "INTEGER DEFAULT 0")
            self._ensure_column(cursor, "todos", "pre_reminded", "INTEGER DEFAULT 0")
            self._ensure_column(cursor, "todos", "remind_before_mins", "INTEGER DEFAULT 15")

            # Completion history log (keeps daily & one-off completions for reports)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS task_completions (
                    id {id_col},
                    todo_id INTEGER DEFAULT NULL,
                    task TEXT NOT NULL,
                    recurrence TEXT DEFAULT 'once',
                    completed_at TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    updated_at TEXT DEFAULT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    instagram TEXT DEFAULT '',
                    facebook TEXT DEFAULT '',
                    tiktok TEXT DEFAULT '',
                    twitter TEXT DEFAULT '',
                    linkedin TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    updated_at TEXT DEFAULT NULL
                )
            """)
            for col in ("instagram", "facebook", "tiktok", "twitter", "linkedin"):
                self._ensure_column(cursor, "contacts", col, "TEXT DEFAULT ''")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    name TEXT PRIMARY KEY,
                    trigger_phrase TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    usage_count INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT NULL
                )
            """)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS episodic_memories (
                    id {id_col},
                    summary TEXT NOT NULL,
                    importance INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS system_issues (
                    id {id_col},
                    issue_type TEXT NOT NULL,
                    component TEXT NOT NULL,
                    details TEXT NOT NULL,
                    latency_ms INTEGER DEFAULT NULL,
                    resolved INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id {id_col},
                    action_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id {id_col},
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    action_url TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            logger.info("Database ready (%s)", "PostgreSQL Cloud" if self.is_postgres else self.db_path)

    # ------------------ To-Do & Scheduled Task Management ------------------
    def add_todo(
        self,
        task: str,
        due_time: Optional[str] = None,
        recurrence: str = "once",
        remind_before_mins: int = 15,
    ) -> Dict[str, Any]:
        clean_rec = recurrence if recurrence in ("once", "daily") else "once"
        try:
            mins = max(0, int(remind_before_mins))
        except (TypeError, ValueError):
            mins = 15
        with self._get_connection() as conn:
            cursor = conn.cursor()
            todo_id = self._insert_get_id(
                cursor,
                """
                INSERT INTO todos (task, status, due_time, recurrence, reminded, pre_reminded, remind_before_mins)
                VALUES (?, 'pending', ?, ?, 0, 0, ?)
                """,
                (task.strip(), due_time, clean_rec, mins),
            )
            conn.commit()
            return {
                "id": todo_id,
                "task": task.strip(),
                "status": "pending",
                "due_time": due_time,
                "recurrence": clean_rec,
                "remind_before_mins": mins,
            }

    def get_todos(self, status: Optional[str] = "pending") -> List[Dict[str, Any]]:
        cols = "id, task, status, due_time, recurrence, reminded, pre_reminded, remind_before_mins, created_at, completed_at"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(self._q(f"SELECT {cols} FROM todos WHERE status = ? ORDER BY id ASC"), (status,))
            else:
                cursor.execute(f"SELECT {cols} FROM todos ORDER BY id ASC")
            return [dict(row) for row in cursor.fetchall()]

    def get_completed_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._q("SELECT id, todo_id, task, recurrence, completed_at FROM task_completions ORDER BY id DESC LIMIT ?"),
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def get_upcoming_and_due_reminders(self, now: Optional[datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Returns {'pre_due': [...], 'due_now': [...]}. pre_due items carry 'mins_left'."""
        now = now or datetime.now()
        pre_due, due_now = [], []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, task, due_time, recurrence, reminded, pre_reminded, remind_before_mins "
                "FROM todos WHERE status = 'pending' AND due_time IS NOT NULL"
            )
            rows = cursor.fetchall()
        for row in rows:
            item = dict(row)
            try:
                due_dt = datetime.fromisoformat(item["due_time"])
            except (ValueError, TypeError):
                continue
            mins_before = int(item.get("remind_before_mins") or 15)
            pre_dt = due_dt - timedelta(minutes=mins_before)
            if int(item.get("reminded") or 0) == 0 and due_dt <= now:
                due_now.append(item)
            elif int(item.get("pre_reminded") or 0) == 0 and mins_before > 0 and pre_dt <= now < due_dt:
                item["mins_left"] = max(1, round((due_dt - now).total_seconds() / 60))
                pre_due.append(item)
        return {"pre_due": pre_due, "due_now": due_now}

    def get_due_reminders(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        return self.get_upcoming_and_due_reminders(now)["due_now"]

    def mark_todo_pre_reminded(self, todo_id: int):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("UPDATE todos SET pre_reminded = 1 WHERE id = ?"), (todo_id,))
            conn.commit()

    def mark_todo_reminded(self, todo_id: int, recurrence: str = "once", current_due: Optional[str] = None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if recurrence == "daily" and current_due:
                try:
                    next_dt = datetime.fromisoformat(current_due) + timedelta(days=1)
                    cursor.execute(
                        self._q("UPDATE todos SET due_time = ?, reminded = 0, pre_reminded = 0 WHERE id = ?"),
                        (next_dt.isoformat(timespec="minutes"), todo_id),
                    )
                    conn.commit()
                    return
                except ValueError:
                    pass
            cursor.execute(self._q("UPDATE todos SET reminded = 1 WHERE id = ?"), (todo_id,))
            conn.commit()

    def complete_todo(self, todo_id: Optional[int] = None, task_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Complete by DB id or fuzzy task name. Logs to task_completions; daily tasks roll to tomorrow.
        Returns the completed task dict, or None if nothing matched."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            row = None
            if todo_id is not None:
                cursor.execute(self._q("SELECT * FROM todos WHERE id = ? AND status = 'pending'"), (int(todo_id),))
                row = cursor.fetchone()
            elif task_name and task_name.strip():
                cursor.execute(
                    self._q("SELECT * FROM todos WHERE status = 'pending' AND LOWER(task) LIKE ? ORDER BY id ASC LIMIT 1"),
                    (f"%{task_name.strip().lower()}%",),
                )
                row = cursor.fetchone()
            if not row:
                return None

            item = dict(row)
            now_iso = datetime.now().isoformat(timespec="seconds")

            self._insert_get_id(
                cursor,
                "INSERT INTO task_completions (todo_id, task, recurrence, completed_at) VALUES (?, ?, ?, ?)",
                (item["id"], item["task"], item.get("recurrence", "once"), now_iso),
            )

            if item.get("recurrence") == "daily":
                next_due = None
                if item.get("due_time"):
                    try:
                        due_dt = datetime.fromisoformat(item["due_time"])
                        while due_dt <= datetime.now():
                            due_dt += timedelta(days=1)
                        next_due = due_dt.isoformat(timespec="minutes")
                    except ValueError:
                        next_due = None
                cursor.execute(
                    self._q("UPDATE todos SET due_time = ?, reminded = 0, pre_reminded = 0, completed_at = ? WHERE id = ?"),
                    (next_due, now_iso, item["id"]),
                )
            else:
                cursor.execute(
                    self._q("UPDATE todos SET status = 'completed', completed_at = ? WHERE id = ?"),
                    (now_iso, item["id"]),
                )
            conn.commit()
            return item

    def delete_todo(self, todo_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("DELETE FROM todos WHERE id = ?"), (int(todo_id),))
            conn.commit()
            return cursor.rowcount > 0

    def get_task_report(self, period: str = "daily") -> Dict[str, Any]:
        now = datetime.now()
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "daily" else (now - timedelta(days=7))
        pending = self.get_todos("pending")
        completed_period = []
        for c in self.get_completed_history(200):
            try:
                if datetime.fromisoformat(c["completed_at"]) >= cutoff:
                    completed_period.append(c)
            except (ValueError, TypeError):
                continue

        total = len(pending) + len(completed_period)
        rate = round((len(completed_period) / total) * 100) if total else 0
        return {
            "period": period,
            "pending_count": len(pending),
            "completed_count": len(completed_period),
            "completion_rate": rate,
            "completed_tasks": completed_period,
            "pending_tasks": pending,
        }

    # ------------------ Contacts Management (CRUD + Social Media) ------------------
    def save_contact(
        self,
        name: str,
        phone: str = "",
        email: str = "",
        instagram: str = "",
        facebook: str = "",
        tiktok: str = "",
        twitter: str = "",
        linkedin: str = "",
        notes: str = "",
        overwrite_empty: bool = False,
    ) -> Dict[str, Any]:
        """Create or update. By default empty arguments keep the stored value;
        with overwrite_empty=True (used by the Edit form) empty arguments clear it."""
        key = name.strip().lower()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("SELECT * FROM contacts WHERE name = ?"), (key,))
            r = cursor.fetchone()
            existing = dict(r) if r else {}

            def pick(val: str, col: str) -> str:
                cleaned = (val or "").strip()
                if overwrite_empty:
                    return cleaned
                return cleaned or (existing.get(col) or "")

            vals = {
                "phone": pick(phone, "phone"),
                "email": pick(email, "email"),
                "instagram": pick(instagram, "instagram").lstrip("@"),
                "facebook": pick(facebook, "facebook"),
                "tiktok": pick(tiktok, "tiktok").lstrip("@"),
                "twitter": pick(twitter, "twitter").lstrip("@"),
                "linkedin": pick(linkedin, "linkedin"),
                "notes": pick(notes, "notes"),
            }
            cursor.execute(
                self._q("""
                    INSERT INTO contacts (name, display_name, phone, email, instagram, facebook, tiktok, twitter, linkedin, notes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        display_name = excluded.display_name,
                        phone = excluded.phone,
                        email = excluded.email,
                        instagram = excluded.instagram,
                        facebook = excluded.facebook,
                        tiktok = excluded.tiktok,
                        twitter = excluded.twitter,
                        linkedin = excluded.linkedin,
                        notes = excluded.notes,
                        updated_at = excluded.updated_at
                """),
                (
                    key, name.strip(), vals["phone"], vals["email"],
                    vals["instagram"], vals["facebook"], vals["tiktok"], vals["twitter"], vals["linkedin"],
                    vals["notes"], datetime.now().isoformat(),
                ),
            )
            conn.commit()
            return {"key": key, "name": name.strip(), "display_name": name.strip(), **vals}

    def update_contact_by_key(self, contact_key: str, fields: Dict[str, str]) -> Optional[Dict[str, Any]]:
        old_key = contact_key.strip().lower()
        new_name = (fields.get("name") or fields.get("display_name") or "").strip()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("SELECT display_name FROM contacts WHERE name = ?"), (old_key,))
            row = cursor.fetchone()
            if not row:
                return None
            old_display = row["display_name"]
            if new_name and new_name.lower() != old_key:
                cursor.execute(self._q("DELETE FROM contacts WHERE name = ?"), (old_key,))
                conn.commit()
        return self.save_contact(
            name=new_name or old_display,
            phone=fields.get("phone", ""),
            email=fields.get("email", ""),
            instagram=fields.get("instagram", ""),
            facebook=fields.get("facebook", ""),
            tiktok=fields.get("tiktok", ""),
            twitter=fields.get("twitter", ""),
            linkedin=fields.get("linkedin", ""),
            notes=fields.get("notes", ""),
            overwrite_empty=True,
        )

    def delete_contact(self, contact_key: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("DELETE FROM contacts WHERE name = ?"), (contact_key.strip().lower(),))
            conn.commit()
            return cursor.rowcount > 0

    def get_contact(self, name: str) -> Optional[Dict[str, Any]]:
        """Exact match first, then names starting with the text, then names containing it."""
        key = name.strip().lower()
        if not key:
            return None
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._q("""
                    SELECT name AS key, display_name, phone, email, instagram, facebook, tiktok, twitter, linkedin, notes
                    FROM contacts
                    WHERE name = ? OR LOWER(display_name) LIKE ?
                    ORDER BY CASE WHEN name = ? THEN 0 WHEN LOWER(display_name) LIKE ? THEN 1 ELSE 2 END,
                             LENGTH(display_name) ASC
                    LIMIT 1
                """),
                (key, f"%{key}%", key, f"{key}%"),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_contacts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name AS key, display_name, phone, email, instagram, facebook, tiktok, twitter, linkedin, notes "
                "FROM contacts ORDER BY display_name ASC"
            )
            return [dict(r) for r in cursor.fetchall()]

    # ------------------ Skills, Episodes & Self-Diagnostics ------------------
    def learn_skill(self, name: str, trigger_phrase: str, instructions: str) -> Dict[str, Any]:
        key = name.strip().lower()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._q("""
                    INSERT INTO skills (name, trigger_phrase, instructions, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        trigger_phrase = excluded.trigger_phrase,
                        instructions = excluded.instructions,
                        updated_at = excluded.updated_at
                """),
                (key, trigger_phrase.strip(), instructions.strip(), now),
            )
            conn.commit()
            return {"name": key, "trigger_phrase": trigger_phrase.strip(), "instructions": instructions.strip()}

    def get_skills(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name, trigger_phrase, instructions, usage_count FROM skills ORDER BY name ASC")
            return [dict(r) for r in cursor.fetchall()]

    def log_episode(self, summary: str, importance: int = 1) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            ep_id = self._insert_get_id(
                cursor,
                "INSERT INTO episodic_memories (summary, importance) VALUES (?, ?)",
                (summary.strip(), importance),
            )
            conn.commit()
            return ep_id

    def get_recent_episodes(self, limit: int = 8) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("SELECT summary, importance, created_at FROM episodic_memories ORDER BY id DESC LIMIT ?"), (limit,))
            return [dict(r) for r in cursor.fetchall()]

    def log_issue(self, issue_type: str, component: str, details: str, latency_ms: Optional[int] = None) -> int:
        """Never raises: diagnostics must not be able to break the assistant."""
        try:
            now_iso = datetime.now().isoformat(timespec="seconds")
            with self._get_connection() as conn:
                cursor = conn.cursor()
                issue_id = self._insert_get_id(
                    cursor,
                    "INSERT INTO system_issues (issue_type, component, details, latency_ms, resolved, created_at) VALUES (?, ?, ?, ?, 0, ?)",
                    (issue_type, component, (details or "")[:400], latency_ms, now_iso),
                )
                conn.commit()
                return issue_id
        except Exception as e:  # pragma: no cover
            logger.warning("Could not log issue: %s", e)
            return 0

    def resolve_issue(self, issue_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("UPDATE system_issues SET resolved = 1 WHERE id = ?"), (int(issue_id),))
            conn.commit()
            return cursor.rowcount > 0

    def get_system_report(self, period: str = "weekly") -> Dict[str, Any]:
        days = 30 if period == "monthly" else 7
        cutoff = datetime.now() - timedelta(days=days)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, issue_type, component, details, latency_ms, resolved, created_at "
                "FROM system_issues ORDER BY id DESC LIMIT 200"
            )
            all_issues = [dict(r) for r in cursor.fetchall()]

        recent_issues = []
        by_component: Dict[str, int] = {}
        for iss in all_issues:
            try:
                if datetime.fromisoformat(iss["created_at"]) >= cutoff:
                    recent_issues.append(iss)
                    if not iss["resolved"]:
                        by_component[iss["component"]] = by_component.get(iss["component"], 0) + 1
            except (ValueError, TypeError):
                continue

        unresolved = [i for i in recent_issues if not i["resolved"]]
        return {
            "period": period,
            "days": days,
            "total_issues": len(recent_issues),
            "unresolved_count": len(unresolved),
            "by_component": by_component,
            "issues": recent_issues[:25],
        }

    # ------------------ User Memory & Facts ------------------
    def remember(self, key: str, value: str, category: str = "general") -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute(
                self._q("""
                    INSERT INTO memories (key, value, category, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        category = excluded.category,
                        updated_at = excluded.updated_at
                """),
                (key.strip().lower(), value.strip(), category, now),
            )
            conn.commit()
            return {"key": key, "value": value, "category": category}

    def recall(self, key: Optional[str] = None) -> Any:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if key:
                cursor.execute(self._q("SELECT key, value, category FROM memories WHERE key = ?"), (key.strip().lower(),))
                row = cursor.fetchone()
                return dict(row) if row else None
            cursor.execute("SELECT key, value, category FROM memories ORDER BY category, key ASC")
            return [dict(row) for row in cursor.fetchall()]

    def get_system_prompt_context(self) -> str:
        sections = []
        memories = self.recall()
        if memories:
            sections.append("FACTS:\n" + "\n".join(f"- {m['key']}: {m['value']}" for m in memories[:25]))
        contacts = self.list_contacts()
        if contacts:
            c_lines = []
            for c in contacts[:25]:
                socials = [s for s in ("instagram", "facebook", "tiktok", "twitter", "linkedin") if c.get(s)]
                soc_txt = (", socials: " + "/".join(socials)) if socials else ""
                c_lines.append(f"- {c['display_name']} (phone: {c['phone'] or 'N/A'}, email: {c['email'] or 'N/A'}{soc_txt})")
            sections.append("CONTACTS:\n" + "\n".join(c_lines))
        skills = self.get_skills()
        if skills:
            sections.append(
                "LEARNED SKILLS:\n"
                + "\n".join(f"- [{s['name']}] When '{s['trigger_phrase']}': {s['instructions']}" for s in skills[:15])
            )
        episodes = self.get_recent_episodes(5)
        if episodes:
            sections.append("VIRTUAL LIFE JOURNAL:\n" + "\n".join(f"- {e['summary']}" for e in episodes))
        return "\n\n".join(sections) if sections else "No previous memories recorded."

    # ------------------ Pending Action Confirmations ------------------
    def create_pending_action(self, action_type: str, payload: Dict[str, Any], summary: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            act_id = self._insert_get_id(
                cursor,
                "INSERT INTO pending_actions (action_type, payload, summary, status) VALUES (?, ?, ?, 'pending')",
                (action_type, json.dumps(payload), summary),
            )
            conn.commit()
            return act_id

    def get_latest_pending_action(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, action_type, payload, summary, status, created_at "
                "FROM pending_actions WHERE status = 'pending' ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
            if not row:
                return None
            data = dict(row)
            data["payload"] = json.loads(data["payload"])
            return data

    def resolve_pending_action(self, action_id: int, status: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(self._q("UPDATE pending_actions SET status = ? WHERE id = ?"), (status, action_id))
            conn.commit()
            return cursor.rowcount > 0

    # ------------------ Persistent Chat History ------------------
    def save_chat_message(self, role: str, content: str, action_url: Optional[str] = None) -> int:
        if not content or not content.strip():
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            msg_id = self._insert_get_id(
                cursor,
                "INSERT INTO chat_messages (role, content, action_url) VALUES (?, ?, ?)",
                (role.strip(), content.strip(), action_url),
            )
            conn.commit()
            return msg_id

    def get_recent_chat_messages(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._q("SELECT id, role, content, action_url, created_at FROM chat_messages ORDER BY id DESC LIMIT ?"),
                (limit,),
            )
            rows = [dict(r) for r in cursor.fetchall()]
            rows.reverse()
            return rows

    def clear_chat_messages(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM chat_messages")
            conn.commit()


memory = MemoryManager()