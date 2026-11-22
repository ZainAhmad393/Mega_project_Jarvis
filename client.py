"""
JARVIS Core AI Engine powered by Groq & Dynamic Tool/Function Calling
Strict language matching (EN / UR / MIX) + fast token-to-sentence streaming
+ camera vision (image_base64).
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

import memory as memory_module
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

# Sentence boundary for streaming TTS (includes Urdu full stop U+06D4 and Arabic ? ;)
_SENTENCE_END = re.compile(r"([.!?؛؟۔]\s+)")
MAX_HISTORY = 14
MAX_TOOL_ROUNDS = 3


class JarvisAI:
    def __init__(self):
        self.api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not self.api_key or "your_" in self.api_key:
            logger.warning("GROQ_API_KEY is not configured in .env")

        self.client = Groq(api_key=self.api_key) if self.api_key else None
        self.async_client = AsyncGroq(api_key=self.api_key) if self.api_key else None
        self.model = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
        self.fast_model = os.getenv("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
        self.vision_model = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        self.history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ language
    def _detect_input_language(self, user_message: str) -> str:
        urdu_chars = len(re.findall(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", user_message))
        latin_words = re.findall(r"\b[a-zA-Z]+\b", user_message.lower())

        if urdu_chars > 0 and len(latin_words) >= 2:
            return "mix"
        if urdu_chars > 0:
            return "ur"

        roman_urdu_words = {
            "aap", "ap", "kaise", "kese", "kesy", "kya", "hain", "hai", "hei", "nahin", "nahi",
            "ni", "haan", "ji", "theek", "thk", "kahan", "kyun", "kab", "ka", "ki", "ko", "ke",
            "mein", "main", "mujhe", "mujy", "tum", "mera", "meri", "mere", "mery", "hum", "wo",
            "ye", "yeh", "woh", "bhi", "abhi", "pehle", "baad", "saath", "sath", "karna", "krna",
            "kro", "karo", "kr", "kar", "krdo", "sakta", "skta", "batao", "btao", "kaho", "bolo",
            "sunao", "kuch", "kon", "koun", "kia", "bhej", "bhejo", "likho", "yaad", "bhai",
            "chaho", "chahta", "madad", "chae", "tumhe", "tumy", "sb", "sab", "or", "aur",
        }
        if latin_words:
            matched = sum(1 for w in latin_words if w in roman_urdu_words)
            if matched >= 2 or (matched >= 1 and matched / len(latin_words) >= 0.22):
                return "mix"
        return "en"

    def _get_system_prompt(self, language: str = "en") -> str:
        memory_context = memory_module.memory.get_system_prompt_context()
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
                "Actually answer their exact question or banter (e.g. if they ask what help YOU need, reply playfully like Tony Stark's AI rather than listing your features). Keep English tech/brand words only when natural."
            )
        else:
            lang_instruction = (
                "LANGUAGE RULE (STRICT): The user spoke in English. Reply 100% in sharp, witty British English like Tony Stark's JARVIS. "
                "NEVER insert Urdu words. Address the user as 'Sir'."
            )

        return f"""You are J.A.R.V.I.S., an ultra-capable, witty, loyal AI assistant inspired by Iron Man, built by Zain Ahmad.
Current local date/time: {now_str}

PERSISTENT MEMORY, PHONEBOOK & LEARNED SKILLS:
{memory_context}

RULES:
1. USE TOOLS IMMEDIATELY when the user asks to add a task/reminder (add_todo — convert times like '5 baje' or '5 pm' to '17:00'), list tasks (get_todo), call (call_contact), message on WhatsApp/SMS (send_whatsapp), email (send_email), open/play on YouTube or social media (open_platform/play_music), weather (get_weather), daily briefing (morning_briefing), search web, save contacts, remember facts, or learn skills.
2. CONFIRMATION: Never fake sending an email/WhatsApp/SMS. Always prepare it via the tool and ask confirmation first.
3. VOICE SPEED & NATURALNESS: Keep replies to 1-2 crisp, smart, natural sentences. Listen carefully to what the user actually asked—never give generic feature lists unless asked what you can do.
4. {lang_instruction}
5. NO MARKDOWN OR EMOJIS: Never output *, #, ---, bullet points, or emojis."""

    # ------------------------------------------------------------------ quick intents (no LLM)
    def _check_pending_confirmation(self, user_text: str) -> Optional[Tuple[str, str, Optional[str]]]:
        pending = memory_module.memory.get_latest_pending_action()
        if not pending:
            return None

        clean = re.sub(r"[^\w\s]", "", user_text.lower()).strip()
        words = clean.split()
        confirm_exact = {
            "yes", "haan", "ha", "han", "kar do", "karo", "bhej do", "send", "send it",
            "confirm", "proceed", "sure", "yup", "ok", "okay", "theek hai", "ji", "go ahead",
            "haan kar do", "haan bhej do", "yes send it", "yes please", "ok send it", "yes go ahead",
        }
        cancel_exact = {"no", "nahin", "nahi", "na", "mat bhejo", "cancel", "stop", "rehne do", "dont", "abort", "chhoro"}
        confirm_first = {"yes", "haan", "confirm", "ok", "okay", "sure"}
        cancel_first = {"no", "nahin", "nahi", "cancel", "mat", "stop"}

        # Prefix rules only apply to very short replies so "send an email to Ali"
        # or "no I want to call Sam" are NOT mistaken for answers to the pending action.
        is_confirm = clean in confirm_exact or (len(words) <= 2 and words and words[0] in confirm_first)
        is_cancel = clean in cancel_exact or (len(words) <= 2 and words and words[0] in cancel_first)

        if is_confirm:
            res = execute_pending_action(pending["id"], confirm=True)
            return (res["message"], "joy", res.get("open_url"))
        if is_cancel:
            res = execute_pending_action(pending["id"], confirm=False)
            return (res["message"], "neutral", None)
        return None

    def _check_call_intent(self, user_text: str) -> Optional[Tuple[str, str, str]]:
        clean = re.sub(r"[^\w\s]", "", user_text.lower()).strip()
        end_words = {"ok bye", "bye bye", "bye", "goodbye", "end call", "disconnect", "call kato", "band karo", "alvida", "exit call"}
        if clean in end_words:
            return ("Goodbye Sir. Call ended.", "end_call", "neutral")

        video_phrases = ("video call", "start video call", "connect video call", "video call connect", "turn on video", "camera on", "video lagao")
        if any(p in clean for p in video_phrases) and "whatsapp" not in clean:
            return ("Sir, video call activated. Direct visual link established.", "start_video_call", "joy")

        audio_phrases = ("audio call", "start audio call", "connect audio call", "audio call connect", "call jarvis", "voice call")
        if any(p in clean for p in audio_phrases) and "whatsapp" not in clean:
            return ("Sir, audio call connected. I am listening continuously.", "start_audio_call", "joy")
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
        """Confirmation / call / greeting shortcuts. Blocking (may send email), so
        async callers must run it in a thread."""
        conf = self._check_pending_confirmation(text)
        if conf:
            msg, sentiment, open_url = conf
            return {"text": msg, "sentiment": sentiment, "action": "confirmation",
                    "open_url": open_url, "language": self._detect_input_language(msg)}
        call = self._check_call_intent(text)
        if call:
            msg, action, sentiment = call
            return {"text": msg, "sentiment": sentiment, "action": action, "open_url": None, "language": "en"}
        greet = self._check_greeting_intent(text)
        if greet:
            msg, action, sentiment = greet
            return {"text": msg, "sentiment": sentiment, "action": action, "open_url": None, "language": "en"}
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
        """Split complete sentences off the front of a streaming buffer."""
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

    def _detect_sentiment(self, text: str) -> str:
        lower = text.lower()
        if any(w in lower for w in ("great", "excellent", "done", "sent", "ready", "pleasure", "shandar")):
            return "joy"
        if any(w in lower for w in ("sorry", "apologies", "error", "failed", "afsos", "maazrat")):
            return "sorrow"
        if any(w in lower for w in ("joke", "funny", "haha", "tony")):
            return "fun"
        return "neutral"

    def _remember_turn(self, user_text: str, reply: str):
        self.history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": reply}])
        self._trim_history()

    def _trim_history(self):
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

    # ------------------------------------------------------------------ model selection
    @staticmethod
    def _is_model_error(e: Exception) -> bool:
        err = str(e).lower()
        return any(x in err for x in ("does not exist", "model_not_found", "decommissioned", "404"))

    def _pick_available_model(self) -> Optional[str]:
        """Queries Groq for currently active chat models on this API key."""
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
        """Open a streaming completion, auto-switching model if Groq retired it."""
        kwargs: Dict[str, Any] = dict(messages=messages, temperature=temperature, max_tokens=max_tokens, stream=True)
        if use_tools and not vision:
            kwargs.update(tools=TOOL_DEFINITIONS, tool_choice="auto")
        model = self.vision_model if vision else self.model
        try:
            return await self.async_client.chat.completions.create(model=model, **kwargs)
        except Exception as e:
            if not self._is_model_error(e):
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
            return await self.async_client.chat.completions.create(model=picked, **kwargs)

    def _call_groq(self, messages: List[Dict], tools=None, tool_choice=None):
        """Blocking completion used by the sync process() path."""
        if tools is None:
            tools = TOOL_DEFINITIONS
        params = dict(messages=messages, tools=tools, tool_choice=tool_choice, temperature=0.45, max_tokens=550)
        try:
            return self.client.chat.completions.create(model=self.model, **params)
        except Exception as e:
            if self._is_model_error(e):
                picked = self._pick_available_model()
                if picked and picked != self.model:
                    logger.warning("Model %s unavailable; auto-switched to %s", self.model, picked)
                    self.model = picked
                    return self.client.chat.completions.create(model=self.model, **params)
            raise

    # ------------------------------------------------------------------ tools
    @staticmethod
    def _execute_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        fn = FUNCS.get(name)
        if not fn:
            return {"status": "error", "message": f"Unknown tool {name}"}
        try:
            return fn(**args)
        except TypeError as e:
            logger.error("Tool %s bad arguments %s: %s", name, args, e)
            return {"status": "error", "message": f"Invalid arguments for {name}"}
        except Exception as e:
            logger.error("Tool %s failed: %s", name, e)
            return {"status": "error", "message": f"{name} failed"}

    def _run_tool_calls(self, calls: List[Dict[str, Any]], messages: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
        """Execute tool calls (blocking), append assistant+tool messages. Returns (last_action, open_url)."""
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

        quick = self._quick_intent(user_message)
        if quick:
            self._remember_turn(user_message, quick["text"])
            return {"response": quick["text"], "action": quick["action"], "sentiment": quick["sentiment"], "open_url": quick["open_url"]}

        lang = self._detect_input_language(user_message)
        if not self.client:
            return {"response": "My API key is not configured, Sir.", "action": "error", "sentiment": "sorrow", "language": lang}

        self.history.append({"role": "user", "content": user_message})
        self._trim_history()

        messages = [{"role": "system", "content": self._get_system_prompt(lang)}] + self.history
        tool_used, action_type, open_url = None, "chat", None

        try:
            response = self._call_groq(messages=messages, tool_choice="auto")
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
                response = self._call_groq(messages=messages, tools=[] if last_round else None,
                                           tool_choice=None if last_round else "auto")
                msg = response.choices[0].message

            reply_text = self._clean_reply_for_speech(msg.content or "")
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
            return {"response": "My apologies Sir, I encountered a temporary issue.", "action": "error", "sentiment": "sorrow", "language": lang}

    # ------------------------------------------------------------------ streaming path (WebSocket)
    async def _vision_stream(self, text: str, lang: str, image_base64: str) -> AsyncGenerator[Dict[str, Any], None]:
        img_url = image_base64 if image_base64.startswith("data:") else f"data:image/jpeg;base64,{image_base64}"
        messages = [
            {"role": "system", "content": self._get_system_prompt(lang)},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": img_url}},
            ]},
        ]
        try:
            stream = await self._open_stream(messages, vision=True, max_tokens=450, temperature=0.4)
            buffer, full = "", ""
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
                    yield {"sentence": sent, "sentiment": self._detect_sentiment(sent), "action": "vision", "language": lang, "done": False}

            leftover = self._clean_reply_for_speech(buffer)
            if leftover:
                yield {"sentence": leftover, "sentiment": self._detect_sentiment(full), "action": "vision", "language": lang, "done": True}
            cleaned = self._clean_reply_for_speech(full)
            if cleaned:
                self.history.append({"role": "assistant", "content": cleaned})
        except Exception as e:
            logger.error("Vision error: %s", e)
            yield {"sentence": "I could not analyze the camera image right now, Sir.", "sentiment": "sorrow", "action": "error", "language": "en", "done": True}

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

        # Shortcuts may send an email / WhatsApp (blocking I/O) -> keep them off the event loop.
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
        pending_url: Optional[str] = None  # attach a tool's open_url to ONE sentence only
        buffer, full_reply = "", ""

        try:
            for round_no in range(MAX_TOOL_ROUNDS + 1):
                allow_tools = round_no < MAX_TOOL_ROUNDS
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
                # Tool produced a link but the model said nothing: still deliver the link.
                yield {"sentence": "Right away, Sir.", "sentiment": "joy", "action": action_type,
                       "language": "en", "open_url": pending_url, "done": True}

            cleaned_full = self._clean_reply_for_speech(full_reply)
            if cleaned_full:
                self.history.append({"role": "assistant", "content": cleaned_full})
        except Exception as e:
            logger.error("Streaming AI error: %s", e)
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