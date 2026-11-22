"""
JARVIS Core AI Engine powered by Groq & Dynamic Tool/Function Calling
Strict language matching (EN / UR / MIX) + fast token-to-sentence streaming
+ camera vision (image_base64) with optional Gemini fallback + auto-learning.

Environment variables:
    GROQ_API_KEY, GROQ_MODEL, GROQ_FAST_MODEL, GROQ_VISION_MODEL
    GEMINI_API_KEY (optional vision fallback), GEMINI_MODEL (optional)
"""

import os
import json
import logging
import re
import random
import asyncio
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple, AsyncGenerator
from dotenv import load_dotenv
from groq import Groq, AsyncGroq

import tools as tools_module
from tools import TOOLS, FUNCS, execute_pending_action

load_dotenv()
logger = logging.getLogger("JarvisClient")

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
        },
    }
    for tool in TOOLS
]

_SENTENCE_END = re.compile(r"([.!?؛؟۔]\s+)")
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
MAX_HISTORY = 14
MAX_TOOL_ROUNDS = 3

_ROMAN_URDU_WORDS = {
    "aap", "ap", "kaise", "kese", "kesy", "kaisa", "kaisi", "kya", "hain", "hai", "hei",
    "nahin", "nahi", "ni", "haan", "ji", "theek", "thk", "kahan", "kyun", "kab", "ka", "ki",
    "ko", "ke", "mein", "main", "mujhe", "mujy", "tum", "mera", "meri", "mere", "mery",
    "apna", "apni", "hum", "wo", "ye", "yeh", "woh", "bhi", "abhi", "pehle", "baad", "saath",
    "sath", "karna", "krna", "kro", "karo", "kr", "kar", "krdo", "sakta", "skta", "batao",
    "btao", "bata", "kaho", "bolo", "sunao", "dikhao", "chalao", "lagao", "kholo", "kuch",
    "kon", "koun", "kia", "kitna", "kitni", "bhej", "bhejo", "likho", "yaad", "bhai", "yaar",
    "chaho", "chahta", "madad", "chae", "tumhe", "tumy", "sb", "sab", "aur", "mausam",
    "raha", "rahi", "rha", "rhi", "acha", "accha", "bohat", "bahut", "zara", "zaroor",
    "shukriya",
}


def _has_phrase(clean: str, phrases) -> bool:
    """Whole-word phrase match, so 'open chat' does not fire on 'open chatgpt'."""
    return any(re.search(rf"\b{re.escape(p)}\b", clean) for p in phrases)


def _msg_language(msg: str) -> str:
    return "ur" if _ARABIC_SCRIPT.search(msg) else "en"


class JarvisAI:
    def __init__(self):
        raw_key = os.getenv("GROQ_API_KEY", "").strip()
        if not raw_key or "your_" in raw_key:
            logger.warning("GROQ_API_KEY is not configured in .env")
            raw_key = ""
        self.api_key = raw_key

        self.client = Groq(api_key=self.api_key) if self.api_key else None
        self.async_client = AsyncGroq(api_key=self.api_key) if self.api_key else None
        self.model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        self.fast_model = os.getenv("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
        self.vision_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        self.history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ language
    def _detect_input_language(self, user_message: str) -> str:
        urdu_chars = len(_ARABIC_SCRIPT.findall(user_message))
        latin_words = re.findall(r"\b[a-zA-Z]+\b", user_message.lower())

        if urdu_chars > 0 and len(latin_words) >= 2:
            return "mix"
        if urdu_chars > 0:
            return "ur"

        if latin_words:
            matched = sum(1 for w in latin_words if w in _ROMAN_URDU_WORDS)
            if matched >= 2 or (matched >= 1 and matched / len(latin_words) >= 0.22):
                return "mix"
        return "en"

    def _get_system_prompt(self, language: str = "en") -> str:
        try:
            memory_context = tools_module.memory.get_system_prompt_context()
        except Exception as e:
            logger.warning("Could not load memory context: %s", e)
            memory_context = ""
        now_str = datetime.now().strftime("%Y-%m-%d %I:%M %p")

        if language == "ur":
            lang_instruction = (
                "LANGUAGE RULE (STRICT): The user spoke in Urdu. Reply 100% in warm, natural, conversational Urdu script (اردو). "
                "Never use stiff textbook phrases like 'میں بھی بخیر ہوں' or 'ٹاسک مینجمنٹ'. Address the user as 'سر' or 'جناب'."
            )
        elif language == "mix":
            lang_instruction = (
                "LANGUAGE RULE (STRICT): The user spoke in Roman Urdu / Urdu-English mix. "
                "Reply in warm, natural, everyday Pakistani Urdu script (اردو) — witty and human-like, NOT stiff textbook Urdu. "
                "Actually answer their exact question or banter naturally."
            )
        else:
            lang_instruction = (
                "LANGUAGE RULE (STRICT): The user spoke in English. Reply 100% in sharp, witty British English like Tony Stark's JARVIS. "
                "NEVER insert Urdu words. Address the user as 'Sir'."
            )

        return f"""You are J.A.R.V.I.S., an ultra-capable, witty, loyal AI assistant inspired by Iron Man, built by Zain Ahmad.
Current local date/time: {now_str}

PERSISTENT MEMORY, CONTACTS & VIRTUAL LIFE JOURNAL:
{memory_context}

RULES:
1. USE TOOLS IMMEDIATELY for tasks/reminders (add_todo, get_todo showing count & numbered items, complete_todo by number or task_name, get_task_report for daily/weekly report), calls (call_contact), WhatsApp/SMS (send_whatsapp), Instagram/Facebook/TikTok/X/LinkedIn messages (send_social_message), email (send_email), weather (get_weather), morning briefing (morning_briefing), Jarvis weekly/monthly self-diagnostics (get_jarvis_report), web search, contacts (save_contact/get_contact), memories, and learning skills.
2. CONFIRMATION: Never fake sending an email/WhatsApp/SMS/social message. Always prepare it via the tool and ask confirmation first.
3. COMPLETE & NATURAL ANSWERS: Keep casual chat to 1-2 crisp sentences. When asked about a city, person, history, weather, tasks, or reports, give a complete, clear 3-4 sentence answer—never cut off mid-fact.
4. {lang_instruction}
5. NO MARKDOWN OR EMOJIS: Never output *, #, ---, bullet points, or emojis."""

    # ------------------------------------------------------------------ quick intents (no LLM)
    def _check_pending_confirmation(self, user_text: str) -> Optional[Tuple[str, str, Optional[str]]]:
        pending = tools_module.memory.get_latest_pending_action()
        if not pending:
            return None

        clean = re.sub(r"[^\w\s]", "", user_text.lower()).strip()
        words = clean.split()
        if not clean or len(words) > 5:
            return None

        confirm_words = {
            "yes", "haan", "ha", "han", "kar do", "karo", "bhej do", "send", "send it",
            "confirm", "proceed", "sure", "yup", "ok", "okay", "theek hai", "ji", "go ahead",
            "haan send kar do", "ok send", "ok send it", "ok kar do", "ok bhej do",
            "ہاں", "جی", "ٹھیک ہے", "بھیج دو", "کر دو", "کرو",
        }
        cancel_words = {
            "no", "nahin", "nahi", "na", "mat bhejo", "cancel", "stop", "rehne do", "dont",
            "abort", "chhoro", "نہیں", "رہنے دو", "منسوخ", "مت بھیجو",
        }

        if clean in confirm_words or clean.split()[0] in ("yes", "haan", "confirm"):
            res = execute_pending_action(pending["id"], confirm=True)
            return (res.get("message", "Done, Sir."), "joy", res.get("open_url"))
        if clean in cancel_words or clean.split()[0] in ("no", "cancel", "nahin", "mat"):
            res = execute_pending_action(pending["id"], confirm=False)
            return (res.get("message", "Cancelled, Sir."), "neutral", None)
        return None

    def _check_call_intent(self, user_text: str) -> Optional[Tuple[str, str, str]]:
        clean = re.sub(r"[^\w\s]", "", user_text.lower()).strip()
        end_words = {
            "ok bye", "bye bye", "bye", "goodbye", "end call", "call end", "disconnect",
            "call kato", "call kaato", "band karo", "alvida", "exit call",
        }
        if clean in end_words:
            return ("Goodbye Sir. Call ended.", "end_call", "neutral")

        video_phrases = ("video call", "start video call", "connect video call", "video call connect",
                         "turn on video", "camera on", "video lagao")
        if _has_phrase(clean, video_phrases) and "whatsapp" not in clean:
            return ("Sir, video call activated. Direct visual link established.", "start_video_call", "joy")

        audio_phrases = ("audio call", "start audio call", "connect audio call", "audio call connect",
                         "call jarvis", "voice call")
        if _has_phrase(clean, audio_phrases) and "whatsapp" not in clean:
            return ("Sir, audio call connected. I am listening continuously.", "start_audio_call", "joy")
        return None

    def _check_ui_control_intent(self, user_text: str) -> Optional[Tuple[str, str, str]]:
        clean = re.sub(r"[^\w\s]", "", user_text.lower()).strip()

        if _has_phrase(clean, ("open schedule", "open tasks", "show schedule", "show tasks tab")):
            return ("Opening Schedule, Sir.", "ui_tab:tasks", "neutral")
        if _has_phrase(clean, ("open contacts", "open phonebook", "show contacts")):
            return ("Opening Contacts, Sir.", "ui_tab:contacts", "neutral")
        if _has_phrase(clean, ("open social", "open socials", "show social media")):
            return ("Opening Social Media Hub, Sir.", "ui_tab:socials", "neutral")
        if _has_phrase(clean, ("open mind", "open diagnostics", "open journal", "show life")):
            return ("Opening Jarvis Mind and Diagnostics, Sir.", "ui_tab:memory", "neutral")
        if _has_phrase(clean, ("open chat", "show chat")):
            return ("Opening Chat, Sir.", "ui_tab:chat", "neutral")

        if _has_phrase(clean, ("switch to urdu", "speak in urdu", "urdu mode")):
            return ("اب میں اردو میں بات کروں گا، سر۔", "ui_lang:ur", "joy")
        if _has_phrase(clean, ("switch to english", "speak in english", "english mode")):
            return ("Switching to English mode, Sir.", "ui_lang:en", "joy")
        if _has_phrase(clean, ("auto language", "mix mode", "urdu english mix")):
            return ("Language set to Auto mode, Sir.", "ui_lang:auto", "joy")

        if _has_phrase(clean, ("volume up", "louder", "increase volume")):
            return ("Increasing volume, Sir.", "ui_vol:up", "neutral")
        if _has_phrase(clean, ("volume down", "quieter", "lower volume")):
            return ("Lowering volume, Sir.", "ui_vol:down", "neutral")
        if clean in ("mute", "mute audio", "be quiet", "stop speaking"):
            return ("Muted, Sir.", "ui_vol:mute", "neutral")
        if _has_phrase(clean, ("clear chat", "clear history")):
            return ("Clearing chat history, Sir.", "ui_clear_chat", "neutral")
        if _has_phrase(clean, ("turn off wake", "stop listening", "sleep jarvis")):
            return ("Standby mode enabled, Sir. Tap Hi Jarvis when you need me.", "ui_wake:off", "neutral")
        return None

    def _check_greeting_intent(self, user_text: str) -> Optional[Tuple[str, str, str]]:
        clean = re.sub(r"[^\w\s]", "", user_text.lower()).strip()
        pure_greetings = {
            "jarvis", "hi jarvis", "hello jarvis", "hey jarvis", "hi", "hello", "hey",
            "good morning", "good evening", "good afternoon", "how are you", "how are you jarvis",
            "are you there", "wake up",
        }
        if clean not in pure_greetings:
            return None
        responses = [
            "At your service, Sir. How may I assist you?",
            "Online and ready, Sir. Your command?",
            "Hello, Sir. All systems are nominal.",
        ]
        return (random.choice(responses), "chat", "joy")

    def _quick_intent(self, text: str) -> Optional[Dict[str, Any]]:
        conf = self._check_pending_confirmation(text)
        if conf:
            msg, sentiment, open_url = conf
            return {"text": msg, "sentiment": sentiment, "action": "confirmation",
                    "open_url": open_url, "language": self._detect_input_language(msg)}

        for checker in (self._check_call_intent, self._check_ui_control_intent, self._check_greeting_intent):
            hit = checker(text)
            if hit:
                msg, action, sentiment = hit
                return {"text": msg, "sentiment": sentiment, "action": action,
                        "open_url": None, "language": _msg_language(msg)}
        return None

    # ------------------------------------------------------------------ text helpers
    @staticmethod
    def _is_likely_noise(text: str) -> bool:
        if not text or not text.strip():
            return True
        t = text.strip()
        if len(t) < 2:
            return True
        letters = sum(1 for c in t if c.isalpha())
        if letters / len(t) < 0.4:
            return True
        return bool(re.match(r"^(uh|um|hmm|ah|er|oh)$", t, flags=re.IGNORECASE))

    @staticmethod
    def _clean_reply_for_speech(text: str) -> str:
        text = re.sub(r"[-=_*~#`]{2,}", " ", text)
        text = re.sub(r"[*_`#]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _pop_sentences(self, buffer: str) -> Tuple[List[str], str]:
        sentences: List[str] = []
        while True:
            match = _SENTENCE_END.search(buffer)
            if not match:
                break
            cut = match.end()
            sent = self._clean_reply_for_speech(buffer[:cut])
            buffer = buffer[cut:]
            if sent:
                sentences.append(sent)
        return sentences, buffer

    def _split_reply(self, text: str) -> List[str]:
        sentences, rest = self._pop_sentences(text + " ")
        rest = self._clean_reply_for_speech(rest)
        if rest:
            sentences.append(rest)
        return sentences

    def _detect_sentiment(self, text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ("great", "excellent", "done", "sent", "ready", "pleasure", "shandar")):
            return "joy"
        if any(w in lower for w in ("sorry", "apologies", "error", "failed", "afsos", "maazrat")):
            return "sorrow"
        if any(w in lower for w in ("joke", "funny", "haha", "tony")):
            return "fun"
        return "neutral"

    @staticmethod
    def _say(lang: str, en: str, ur: str) -> str:
        return en if lang == "en" else ur

    @staticmethod
    def _log_issue(kind: str, source: str, detail: str):
        try:
            tools_module.memory.log_issue(kind, source, detail)
        except Exception as e:
            logger.warning("Could not log issue (%s/%s): %s", kind, source, e)

    # ------------------------------------------------------------------ history
    def _remember_turn(self, user_text: str, reply: str):
        self.history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])
        self._trim_history()

    def _trim_history(self):
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]
        while self.history and self.history[0].get("role") != "user":
            self.history.pop(0)

    def _rollback_user_turn(self, text: str):
        if self.history and self.history[-1].get("role") == "user" and self.history[-1].get("content") == text:
            self.history.pop()

    # ------------------------------------------------------------------ auto-learning
    async def auto_learn_from_turn(self, user_text: str):
        """Silently extracts durable personal facts/preferences in the background without blocking chat."""
        if not user_text or len(user_text.split()) < 4 or not self.async_client:
            return
        try:
            resp = await self.async_client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Extract at most ONE durable personal fact, city, habit, preference, or relationship from the user's message "
                            "as JSON {\"key\": \"short_snake_key\", \"value\": \"concise fact\"} or {} if nothing worth remembering. Output JSON only."
                        ),
                    },
                    {"role": "user", "content": user_text},
                ],
                temperature=0.1,
                max_tokens=80,
            )
            raw = (resp.choices[0].message.content or "").strip()
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                if data.get("key") and data.get("value"):
                    tools_module.memory.remember(str(data["key"]), str(data["value"]), category="auto_learned")
                    tools_module.memory.log_episode(f"Auto-learned: {data['key']} = {data['value']}")
        except Exception:
            pass

    # ------------------------------------------------------------------ model selection
    @staticmethod
    def _is_model_error(e: Exception) -> bool:
        err = str(e).lower()
        return any(x in err for x in ("does not exist", "model_not_found", "decommissioned", "404"))

    @staticmethod
    def _is_tool_error(e: Exception) -> bool:
        err = str(e).lower()
        return any(x in err for x in ("tool_use_failed", "failed_generation", "failed to call a function"))

    def _pick_available_model(self) -> Optional[str]:
        try:
            models = [
                m.id for m in self.client.models.list().data
                if not any(x in m.id.lower() for x in ("whisper", "guard", "tts", "orpheus", "safeguard"))
            ]
            preferred = [
                "llama-3.3-70b-versatile",
                "openai/gpt-oss-20b",
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "openai/gpt-oss-120b",
                "qwen/qwen3-32b",
            ]
            for p in preferred:
                if p in models:
                    return p
            return models[0] if models else None
        except Exception as e:
            logger.warning("Could not list Groq models: %s", e)
            return None

    def _pick_available_vision_model(self) -> Optional[str]:
        try:
            models = [m.id for m in self.client.models.list().data]
            for candidate in (
                "meta-llama/llama-4-scout-17b-16e-instruct",
                "meta-llama/llama-4-maverick-17b-128e-instruct",
            ):
                if candidate in models:
                    return candidate
            for m in models:
                if "llama-4" in m.lower() or "vision" in m.lower():
                    return m
            return None
        except Exception:
            return None

    async def _open_stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        vision: bool = False,
        use_tools: bool = True,
        max_tokens: int = 550,
        temperature: float = 0.45,
    ):
        kwargs: Dict[str, Any] = dict(messages=messages, temperature=temperature, max_tokens=max_tokens, stream=True)
        if use_tools and not vision and TOOL_DEFINITIONS:
            kwargs.update(tools=TOOL_DEFINITIONS, tool_choice="auto")

        model = self.vision_model if vision else self.model
        tool_retry_left = 1 if "tools" in kwargs else 0
        switched = False

        while True:
            try:
                return await self.async_client.chat.completions.create(model=model, **kwargs)
            except Exception as e:
                if tool_retry_left and self._is_tool_error(e):
                    tool_retry_left -= 1
                    logger.warning("Malformed tool call from model; retrying once")
                    continue
                if switched or not self._is_model_error(e):
                    raise
                picker = self._pick_available_vision_model if vision else self._pick_available_model
                picked = await asyncio.to_thread(picker)
                if not picked or picked == model:
                    raise
                logger.warning("Model %s unavailable; auto-switched to %s", model, picked)
                if vision:
                    self.vision_model = picked
                else:
                    self.model = picked
                model, switched = picked, True

    def _call_groq(self, messages: List[Dict], use_tools: bool = True):
        params: Dict[str, Any] = dict(messages=messages, temperature=0.45, max_tokens=550)
        if use_tools and TOOL_DEFINITIONS:
            params.update(tools=TOOL_DEFINITIONS, tool_choice="auto")

        model = self.model
        tool_retry_left = 1 if "tools" in params else 0
        switched = False

        while True:
            try:
                return self.client.chat.completions.create(model=model, **params)
            except Exception as e:
                if tool_retry_left and self._is_tool_error(e):
                    tool_retry_left -= 1
                    logger.warning("Malformed tool call from model; retrying once")
                    continue
                if switched or not self._is_model_error(e):
                    raise
                picked = self._pick_available_model()
                if not picked or picked == model:
                    raise
                logger.warning("Model %s unavailable; auto-switched to %s", model, picked)
                self.model = picked
                model, switched = picked, True

    # ------------------------------------------------------------------ tools
    def _execute_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        fn = FUNCS.get(name)
        if not fn:
            return {"status": "error", "message": f"Unknown tool {name}"}
        try:
            return fn(**args)
        except TypeError as e:
            self._log_issue("tool_args_error", name, str(e))
            return {"status": "error", "message": f"Invalid arguments for {name}"}
        except Exception as e:
            self._log_issue("tool_error", name, str(e))
            return {"status": "error", "message": f"{name} failed"}

    def _run_tool_calls(self, calls: List[Dict[str, Any]], messages: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        if not calls:
            return None, None
        messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        action, open_url = None, None
        for call in calls:
            name = call["function"]["name"]
            action = name
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
                if not isinstance(args, dict):
                    args = {}
            except Exception:
                args = {}
            result = self._execute_tool(name, args)
            if isinstance(result, dict) and result.get("open_url"):
                open_url = result["open_url"]
            messages.append({
                "role": "tool", "tool_call_id": call["id"], "name": name,
                "content": json.dumps(result, default=str),
            })
        return action, open_url

    @staticmethod
    def _calls_from_accumulator(acc: Dict[int, Dict[str, str]]) -> List[Dict[str, Any]]:
        return [
            {"id": v["id"] or f"call_{i}", "type": "function",
             "function": {"name": v["name"], "arguments": v["arguments"] or "{}"}}
            for i, v in sorted(acc.items()) if v["name"]
        ]

    # ------------------------------------------------------------------ sync path (used by /api/chat)
    def process(self, user_message: str) -> Dict[str, Any]:
        if not user_message or not user_message.strip() or self._is_likely_noise(user_message):
            return {"response": "I didn't quite catch that, Sir. Could you say it again?", "action": "chat", "sentiment": "neutral"}

        user_message = user_message.strip()

        quick = self._quick_intent(user_message)
        if quick:
            self._remember_turn(user_message, quick["text"])
            return {"response": quick["text"], "action": quick["action"], "sentiment": quick["sentiment"],
                    "open_url": quick["open_url"], "language": quick["language"]}

        lang = self._detect_input_language(user_message)
        if not self.client:
            return {"response": "My API key is not configured, Sir.", "action": "error", "sentiment": "sorrow", "language": lang}

        self.history.append({"role": "user", "content": user_message})
        self._trim_history()

        messages = [{"role": "system", "content": self._get_system_prompt(lang)}] + self.history
        tool_used, action_type, open_url = None, "chat", None

        try:
            response = self._call_groq(messages)
            msg = response.choices[0].message

            rounds = 0
            while msg.tool_calls and rounds < MAX_TOOL_ROUNDS:
                rounds += 1
                calls = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                    for tc in msg.tool_calls
                ]
                action, url = self._run_tool_calls(calls, messages)
                if action:
                    tool_used, action_type = action, action
                if url:
                    open_url = url
                last_round = rounds >= MAX_TOOL_ROUNDS
                response = self._call_groq(messages, use_tools=not last_round)
                msg = response.choices[0].message

            reply_text = self._clean_reply_for_speech(msg.content or "")
            if not reply_text:
                reply_text = self._say(lang, "Done, Sir.", "جی سر، ہو گیا۔") if tool_used else \
                    self._say(lang, "I'm afraid I have no answer for that, Sir.", "معذرت سر، اس کا میرے پاس کوئی جواب نہیں۔")
            self.history.append({"role": "assistant", "content": reply_text})
            return {
                "response": reply_text,
                "action": action_type,
                "sentiment": self._detect_sentiment(reply_text),
                "tool_used": tool_used,
                "language": lang,
                "open_url": open_url,
            }
        except Exception as e:
            logger.error("AI error: %s", e)
            self._log_issue("llm_error", "groq_sync", str(e))
            self._rollback_user_turn(user_message)
            return {"response": "My apologies Sir, I encountered a temporary issue.", "action": "error", "sentiment": "sorrow", "language": lang}

    # ------------------------------------------------------------------ vision
    async def _gemini_vision(self, text: str, lang: str, image_base64: str) -> Optional[str]:
        key = os.getenv("GEMINI_API_KEY", "").strip()
        if not key or "your_" in key:
            return None

        import httpx

        mime, b64 = "image/jpeg", image_base64
        if image_base64.startswith("data:") and "," in image_base64:
            header, b64 = image_base64.split(",", 1)
            m = re.match(r"data:([\w./+-]+);base64", header)
            if m:
                mime = m.group(1)

        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "system_instruction": {"parts": [{"text": self._get_system_prompt(lang)}]},
            "contents": [{"role": "user", "parts": [
                {"text": text},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 450},
        }
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(url, headers={"x-goog-api-key": key}, json=payload)
            resp.raise_for_status()
            data = resp.json()
        parts = data["candidates"][0]["content"]["parts"]
        return self._clean_reply_for_speech(" ".join(p.get("text", "") for p in parts))

    async def _vision_stream(self, text: str, lang: str, image_base64: str) -> AsyncGenerator[Dict[str, Any], None]:
        img_url = image_base64 if image_base64.startswith("data:") else f"data:image/jpeg;base64,{image_base64}"
        messages = [
            {"role": "system", "content": self._get_system_prompt(lang)},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": img_url}},
            ]},
        ]
        streamed_any = False
        buffer, full = "", ""

        try:
            stream = await self._open_stream(messages, vision=True, max_tokens=450, temperature=0.4)
            async for chunk in stream:
                if not chunk.choices:
                    continue
                token = chunk.choices[0].delta.content
                if not token:
                    continue
                buffer += token
                full += token
                sentences, buffer = self._pop_sentences(buffer)
                for sent in sentences:
                    streamed_any = True
                    yield {"sentence": sent, "sentiment": self._detect_sentiment(sent), "action": "vision",
                           "language": lang, "done": False}

            leftover = self._clean_reply_for_speech(buffer)
            cleaned = self._clean_reply_for_speech(full)
            if leftover:
                yield {"sentence": leftover, "sentiment": self._detect_sentiment(full), "action": "vision",
                       "language": lang, "done": True}
            elif streamed_any:
                yield {"sentence": "", "sentiment": self._detect_sentiment(full), "action": "vision",
                       "language": lang, "done": True}
            else:
                yield {"sentence": self._say(lang, "I could not make anything out in that image, Sir.",
                                             "معذرت سر، اس تصویر میں مجھے کچھ سمجھ نہیں آیا۔"),
                       "sentiment": "neutral", "action": "vision", "language": lang, "done": True}
            if cleaned:
                self.history.append({"role": "assistant", "content": cleaned})
            else:
                self._rollback_user_turn(text)
            return

        except Exception as e:
            logger.error("Groq vision error: %s", e)
            self._log_issue("vision_error", "groq_vision", str(e))

            if streamed_any:
                partial = self._clean_reply_for_speech(full)
                if partial:
                    self.history.append({"role": "assistant", "content": partial})
                yield {"sentence": "", "sentiment": "neutral", "action": "vision", "language": lang, "done": True}
                return

            groq_error = e

        try:
            answer = await self._gemini_vision(text, lang, image_base64)
        except Exception as ge:
            logger.error("Gemini vision error: %s", ge)
            self._log_issue("vision_error", "gemini_vision", str(ge))
            answer = None

        if answer:
            self.history.append({"role": "assistant", "content": answer})
            sentences = self._split_reply(answer)
            for i, sent in enumerate(sentences):
                yield {"sentence": sent, "sentiment": self._detect_sentiment(sent), "action": "vision",
                       "language": lang, "done": i == len(sentences) - 1}
            return

        self._rollback_user_turn(text)
        err = str(groq_error).lower()
        if self._is_model_error(groq_error) or any(x in err for x in ("permission", "not enabled", "terms", "access")):
            msg = ("Vision model is not enabled on your Groq key yet, Sir. "
                   "Add a free GEMINI_API_KEY in settings to unlock live camera vision.")
        else:
            msg = "I could not analyze the camera image right now, Sir."
        yield {"sentence": msg, "sentiment": "sorrow", "action": "error", "language": "en", "done": True}

    # ------------------------------------------------------------------ streaming path (WebSocket)
    async def process_stream(
        self,
        user_message: str,
        language_hint: Optional[str] = None,
        image_base64: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        text = (user_message or "").strip()
        has_image = bool(image_base64)
        if has_image and not text:
            text = "What do you see in this image? Describe it briefly."

        if not has_image and (not text or self._is_likely_noise(text)):
            yield {"sentence": "I didn't quite catch that, Sir. Could you say it again?", "sentiment": "neutral", "action": "chat", "language": "en", "done": True}
            return

        if not self.async_client:
            yield {"sentence": "My API key is not configured, Sir.", "sentiment": "sorrow", "action": "error", "language": "en", "done": True}
            return

        if not has_image:
            quick = await asyncio.to_thread(self._quick_intent, text)
            if quick:
                self._remember_turn(text, quick["text"])
                yield {"sentence": quick["text"], "sentiment": quick["sentiment"], "action": quick["action"],
                       "language": quick["language"], "open_url": quick["open_url"], "done": True}
                return

        lang = language_hint if language_hint in ("en", "ur", "mix") else self._detect_input_language(text)
        self.history.append({"role": "user", "content": text})
        self._trim_history()

        if has_image:
            async for chunk in self._vision_stream(text, lang, image_base64):
                yield chunk
            return

        messages = [{"role": "system", "content": self._get_system_prompt(lang)}] + self.history
        action_type: str = "chat"
        pending_url: Optional[str] = None
        buffer, full_reply = "", ""
        streamed_any = False

        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                allow_tools = round_no < MAX_TOOL_ROUNDS
                if round_no and full_reply and not full_reply.endswith(" "):
                    full_reply += " "
                    buffer += " "

                stream = await self._open_stream(messages, use_tools=allow_tools,
                                                 max_tokens=550 if round_no == 0 else 300)
                tool_acc: Dict[int, Dict[str, str]] = {}

                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            entry = tool_acc.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                entry["id"] = tc.id
                            if tc.function and tc.function.name:
                                entry["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                entry["arguments"] += tc.function.arguments
                    if delta.content:
                        buffer += delta.content
                        full_reply += delta.content
                        sentences, buffer = self._pop_sentences(buffer)
                        for sent in sentences:
                            url, pending_url = pending_url, None
                            streamed_any = True
                            yield {"sentence": sent, "sentiment": self._detect_sentiment(sent), "action": action_type,
                                   "language": lang, "open_url": url, "done": False}

                calls = self._calls_from_accumulator(tool_acc)
                if not calls:
                    break
                action, url = await asyncio.to_thread(self._run_tool_calls, calls, messages)
                if action:
                    action_type = action
                if url:
                    pending_url = url

            leftover = self._clean_reply_for_speech(buffer)
            if leftover:
                url, pending_url = pending_url, None
                yield {"sentence": leftover, "sentiment": self._detect_sentiment(full_reply), "action": action_type,
                       "language": lang, "open_url": url, "done": True}
            elif pending_url:
                yield {"sentence": self._say(lang, "Right away, Sir.", "جی سر، ابھی۔"), "sentiment": "joy",
                       "action": action_type, "language": lang, "open_url": pending_url, "done": True}
            elif streamed_any:
                yield {"sentence": "", "sentiment": self._detect_sentiment(full_reply), "action": action_type,
                       "language": lang, "open_url": None, "done": True}
            else:
                fallback = (self._say(lang, "Done, Sir.", "جی سر، ہو گیا۔") if action_type != "chat"
                            else self._say(lang, "I'm afraid I have no answer for that, Sir.",
                                           "معذرت سر، اس کا میرے پاس کوئی جواب نہیں۔"))
                yield {"sentence": fallback, "sentiment": "neutral", "action": action_type,
                       "language": lang, "open_url": None, "done": True}
                full_reply = fallback

            cleaned_full = self._clean_reply_for_speech(full_reply)
            if cleaned_full:
                self.history.append({"role": "assistant", "content": cleaned_full})
        except Exception as e:
            logger.error("Streaming AI error: %s", e)
            self._log_issue("llm_error", "groq_stream", str(e))
            partial = self._clean_reply_for_speech(full_reply)
            if partial:
                self.history.append({"role": "assistant", "content": partial})
            else:
                self._rollback_user_turn(text)
            yield {"sentence": "My apologies Sir, there was a temporary connection issue.", "sentiment": "sorrow", "action": "error", "language": "en", "done": True}

    def reset(self):
        self.history = []


jarvis_ai = JarvisAI()


async def ai_process_full(user_message: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, jarvis_ai.process, user_message)


async def ai_process(user_message: str) -> tuple[str, str]:
    res = await ai_process_full(user_message)
    return res["response"], res.get("sentiment", "neutral")


def ai_process_sync(user_message: str) -> tuple[str, str]:
    res = jarvis_ai.process(user_message)
    return res["response"], res.get("sentiment", "neutral")