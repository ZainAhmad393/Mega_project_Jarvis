"""
JARVIS Persistent Memory & SQLite Storage
Handles To-Do list with scheduled reminders, Contacts, Learned Skills,
Episodic Memories, Key-Value Facts, and Pending Safety Confirmations.
"""

import os
import sqlite3
import json
import logging
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

    def __init__(self, db_path: str = DB_PATH):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self.db_path = db_path
        self._init_db()
        self._initialized = True

    def close(self):
        """Close resources and reset initialization for testing."""
        self._initialized = False

    @classmethod
    def reset_instance(cls, db_path: str = DB_PATH):
        cls._instance = None
        inst = cls(db_path=db_path)
        inst.db_path = db_path
        inst._init_db()
        global memory
        memory = inst
        return inst

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_column(self, cursor: sqlite3.Cursor, table: str, column: str, definition: str):
        cursor.execute(f"PRAGMA table_info({table})")
        cols = {row["name"] for row in cursor.fetchall()}
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _init_db(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # To-Do / Scheduled Tasks
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    due_time TEXT DEFAULT NULL,
                    recurrence TEXT DEFAULT 'once',
                    reminded INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP
                )
            """)
            # Safe migration for older jarvis.db files
            self._ensure_column(cursor, "todos", "due_time", "TEXT DEFAULT NULL")
            self._ensure_column(cursor, "todos", "recurrence", "TEXT DEFAULT 'once'")
            self._ensure_column(cursor, "todos", "reminded", "INTEGER DEFAULT 0")

            # Key-value memories/facts
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Contacts book (for WhatsApp & Email by name like "Tom")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    phone TEXT DEFAULT '',
                    email TEXT DEFAULT '',
                    notes TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Digital Life: Learned Skills & Routines
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS skills (
                    name TEXT PRIMARY KEY,
                    trigger_phrase TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    usage_count INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Digital Life: Episodic Experiences / Conversation Highlights
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS episodic_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    summary TEXT NOT NULL,
                    importance INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Pending confirmation actions (email / whatsapp safety)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Persistent chat history
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    action_url TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            logger.info("SQLite database verified at %s", self.db_path)

    # ------------------ To-Do & Scheduled Task Management ------------------
    def add_todo(
        self,
        task: str,
        due_time: Optional[str] = None,
        recurrence: str = "once",
    ) -> Dict[str, Any]:
        clean_recurrence = recurrence if recurrence in ("once", "daily") else "once"
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO todos (task, status, due_time, recurrence, reminded)
                VALUES (?, 'pending', ?, ?, 0)
                """,
                (task.strip(), due_time, clean_recurrence),
            )
            conn.commit()
            return {
                "id": cursor.lastrowid,
                "task": task.strip(),
                "status": "pending",
                "due_time": due_time,
                "recurrence": clean_recurrence,
            }

    def get_todos(self, status: Optional[str] = "pending") -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    """
                    SELECT id, task, status, due_time, recurrence, reminded, created_at
                    FROM todos WHERE status = ? ORDER BY id ASC
                    """,
                    (status,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, task, status, due_time, recurrence, reminded, created_at
                    FROM todos ORDER BY id ASC
                    """
                )
            return [dict(row) for row in cursor.fetchall()]

    def get_due_reminders(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Return pending todos whose due_time <= now and haven't been reminded yet."""
        now = now or datetime.now()
        due_items = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, task, due_time, recurrence, reminded
                FROM todos
                WHERE status = 'pending' AND due_time IS NOT NULL AND reminded = 0
                """
            )
            rows = cursor.fetchall()
            for row in rows:
                item = dict(row)
                try:
                    due_dt = datetime.fromisoformat(item["due_time"])
                    if due_dt <= now:
                        due_items.append(item)
                except ValueError:
                    continue
        return due_items

    def mark_todo_reminded(self, todo_id: int, recurrence: str = "once", current_due: Optional[str] = None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if recurrence == "daily" and current_due:
                try:
                    next_dt = datetime.fromisoformat(current_due) + timedelta(days=1)
                    cursor.execute(
                        "UPDATE todos SET due_time = ?, reminded = 0 WHERE id = ?",
                        (next_dt.isoformat(timespec="minutes"), todo_id),
                    )
                    conn.commit()
                    return
                except ValueError:
                    pass
            cursor.execute("UPDATE todos SET reminded = 1 WHERE id = ?", (todo_id,))
            conn.commit()

    def complete_todo(self, todo_id: int) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute(
                "UPDATE todos SET status = 'completed', completed_at = ? WHERE id = ?",
                (now, todo_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ------------------ Contacts Management ------------------
    def save_contact(self, name: str, phone: str = "", email: str = "", notes: str = "") -> Dict[str, Any]:
        key = name.strip().lower()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT phone, email, notes FROM contacts WHERE name = ?", (key,))
            existing = cursor.fetchone()
            final_phone = phone.strip() or (existing["phone"] if existing else "")
            final_email = email.strip() or (existing["email"] if existing else "")
            final_notes = notes.strip() or (existing["notes"] if existing else "")
            now = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO contacts (name, display_name, phone, email, notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    display_name = excluded.display_name,
                    phone = excluded.phone,
                    email = excluded.email,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (key, name.strip(), final_phone, final_email, final_notes, now),
            )
            conn.commit()
            return {
                "name": name.strip(),
                "phone": final_phone,
                "email": final_email,
                "notes": final_notes,
            }

    def get_contact(self, name: str) -> Optional[Dict[str, Any]]:
        key = name.strip().lower()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT display_name, phone, email, notes FROM contacts WHERE name = ? OR LOWER(display_name) LIKE ?",
                (key, f"%{key}%"),
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def list_contacts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT display_name, phone, email, notes FROM contacts ORDER BY display_name ASC")
            return [dict(r) for r in cursor.fetchall()]

    # ------------------ Digital Life: Learned Skills & Episodes ------------------
    def learn_skill(self, name: str, trigger_phrase: str, instructions: str) -> Dict[str, Any]:
        key = name.strip().lower()
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO skills (name, trigger_phrase, instructions, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    trigger_phrase = excluded.trigger_phrase,
                    instructions = excluded.instructions,
                    updated_at = excluded.updated_at
                """,
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
            cursor.execute(
                "INSERT INTO episodic_memories (summary, importance) VALUES (?, ?)",
                (summary.strip(), importance),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recent_episodes(self, limit: int = 6) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT summary, importance, created_at FROM episodic_memories ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in cursor.fetchall()]

    # ------------------ User Memory & Facts ------------------
    def remember(self, key: str, value: str, category: str = "general") -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = datetime.now().isoformat()
            cursor.execute(
                """
                INSERT INTO memories (key, value, category, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    category = excluded.category,
                    updated_at = excluded.updated_at
                """,
                (key.strip().lower(), value.strip(), category, now),
            )
            conn.commit()
            return {"key": key, "value": value, "category": category}

    def recall(self, key: Optional[str] = None) -> Any:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if key:
                cursor.execute("SELECT key, value, category FROM memories WHERE key = ?", (key.strip().lower(),))
                row = cursor.fetchone()
                return dict(row) if row else None
            cursor.execute("SELECT key, value, category FROM memories ORDER BY category, key ASC")
            return [dict(row) for row in cursor.fetchall()]

    def get_system_prompt_context(self) -> str:
        """Serializes memories, contacts, skills, and recent episodes for the LLM."""
        sections = []

        memories = self.recall()
        if memories:
            sections.append("FACTS:\n" + "\n".join(f"- {m['key']}: {m['value']}" for m in memories[:25]))

        contacts = self.list_contacts()
        if contacts:
            sections.append(
                "CONTACTS:\n"
                + "\n".join(
                    f"- {c['display_name']} (phone: {c['phone'] or 'N/A'}, email: {c['email'] or 'N/A'})"
                    for c in contacts[:25]
                )
            )

        skills = self.get_skills()
        if skills:
            sections.append(
                "LEARNED SKILLS (follow when triggered):\n"
                + "\n".join(f"- [{s['name']}] When '{s['trigger_phrase']}': {s['instructions']}" for s in skills[:15])
            )

        episodes = self.get_recent_episodes(5)
        if episodes:
            sections.append("RECENT EXPERIENCES:\n" + "\n".join(f"- {e['summary']}" for e in episodes))

        return "\n\n".join(sections) if sections else "No previous memories recorded."

    # ------------------ Pending Action Confirmations ------------------
    def create_pending_action(self, action_type: str, payload: Dict[str, Any], summary: str) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO pending_actions (action_type, payload, summary, status)
                VALUES (?, ?, ?, 'pending')
                """,
                (action_type, json.dumps(payload), summary),
            )
            conn.commit()
            return cursor.lastrowid

    def get_latest_pending_action(self) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, action_type, payload, summary, status, created_at
                FROM pending_actions
                WHERE status = 'pending'
                ORDER BY id DESC LIMIT 1
                """
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
            cursor.execute("UPDATE pending_actions SET status = ? WHERE id = ?", (status, action_id))
            conn.commit()
            return cursor.rowcount > 0
# ------------------ Persistent Chat History ------------------
    def save_chat_message(self, role: str, content: str, action_url: Optional[str] = None) -> int:
        if not content or not content.strip():
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO chat_messages (role, content, action_url) VALUES (?, ?, ?)",
                (role.strip(), content.strip(), action_url),
            )
            conn.commit()
            return cursor.lastrowid

    def get_recent_chat_messages(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, role, content, action_url, created_at
                FROM chat_messages
                ORDER BY id DESC LIMIT ?
                """,
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