"""
JARVIS Voice Service (100% Free Edge-TTS + Groq Whisper STT)
Optimized for low latency, crisp British JARVIS English, and natural Urdu pronunciation.
"""

import os
import re
import hashlib
import asyncio
import logging
import unicodedata
from typing import AsyncGenerator, Optional, Tuple
import edge_tts
from groq import Groq
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger("JarvisVoice")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

TTS_VOICE_EN = os.getenv("TTS_VOICE_EN", "en-GB-RyanNeural").strip()
TTS_VOICE_UR = os.getenv("TTS_VOICE_UR", "ur-PK-AsadNeural").strip()
TTS_RATE = os.getenv("TTS_RATE", "+2%").strip()
TTS_RATE_UR = os.getenv("TTS_RATE_UR", "+0%").strip()
TTS_PITCH = os.getenv("TTS_PITCH", "-2Hz").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

_groq_client: Optional[Groq] = None

_URDU_ABBREVIATIONS = {
    "email": "ای میل",
    "whatsapp": "واٹس ایپ",
    "ai": "اے آئی",
    "api": "اے پی آئی",
    "todo": "کام",
    "task": "کام",
    "tasks": "کام",
    "ٹاسکس": "کام",
    "sir": "سر",
    "zain": "زین",
    "ahmad": "احمد",
}

_ENGLISH_ABBREVIATIONS = {
    "jarvis": "Jarvis",
    "zain ahmad": "Zayn Ahmad",
    "digix": "Dijix",
}

_PRONUNCIATION_ALIASES = {
    "assalamualaikum": "As salaam o alaikum",
    "walaikum": "wa alaikum",
    "aap": "aap",
    "ap": "aap",
    "kaise": "kaisay",
    "kaisa": "kaysa",
    "hai": "hay",
    "hain": "hain",
    "mein": "mayn",
    "main": "mayn",
    "mujhe": "mujhay",
    "kya": "kyaa",
    "kyun": "kyoon",
    "chahiye": "chaahiye",
    "ji": "jee",
    "zain": "Zayn",
    "ahmad": "Ah-mad",
    "junaid": "Joo-naid",
}


def prepare_text_for_tts(text: str) -> str:
    """Make common Roman Urdu words and names clearer to the English TTS voice."""
    if not text:
        return ""
    pattern = re.compile(
        r"\b(" + "|".join(map(re.escape, _PRONUNCIATION_ALIASES)) + r")\b",
        re.IGNORECASE,
    )
    return pattern.sub(lambda m: _PRONUNCIATION_ALIASES[m.group(0).lower()], text)


def _is_urdu_char(char: str) -> bool:
    return bool(re.match(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", char))


def detect_language(text: str) -> str:
    if any(_is_urdu_char(c) for c in text):
        return "ur"
    return "en"


def get_voice_for_text(text: str) -> str:
    return TTS_VOICE_UR if detect_language(text) == "ur" else TTS_VOICE_EN


def split_by_script(text: str) -> list[Tuple[str, str]]:
    parts = re.findall(
        r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\s,،؛؟!.]+|[^\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]+",
        text,
    )
    segments: list[Tuple[str, str]] = []
    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue
        lang = "ur" if any(_is_urdu_char(c) for c in stripped) else "en"
        if segments and segments[-1][1] == lang:
            segments[-1] = (segments[-1][0] + " " + stripped, lang)
        else:
            segments.append((stripped, lang))
    return segments


def clean_for_tts(text: str, language: Optional[str] = None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"https?://\S+|www\.\S+", " link ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[*_`#>~]", "", cleaned)
    cleaned = "".join(
        c for c in cleaned
        if not unicodedata.category(c).startswith(("So", "Cs"))
    )
    lang = language or detect_language(cleaned)
    replacements = _URDU_ABBREVIATIONS if lang == "ur" else _ENGLISH_ABBREVIATIONS
    for src, dst in replacements.items():
        cleaned = re.sub(rf"\b{re.escape(src)}\b", dst, cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def get_cache_path(text: str, voice: str) -> str:
    rate = TTS_RATE_UR if voice.startswith("ur-") else TTS_RATE
    key = f"edge:{voice}:{rate}:{TTS_PITCH}:{text.strip().lower()}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.mp3")


async def _synthesize_edge(text: str, voice: str) -> bytes:
    if voice.startswith("ur-"):
        communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE_UR)
    else:
        communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE, pitch=TTS_PITCH)
    audio_stream = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_stream.extend(chunk["data"])
    return bytes(audio_stream)


async def generate_tts_bytes(text: str, voice: Optional[str] = None, language: Optional[str] = None) -> bytes:
    clean_text = (text or "").strip()
    if not clean_text:
        return b""

    detected = language or detect_language(clean_text)
    if any(_is_urdu_char(c) for c in clean_text):
        detected = "ur"

    spoken_text = clean_for_tts(clean_text, language=detected)
    if not spoken_text:
        return b""

    chosen_voice = voice or (TTS_VOICE_UR if detected == "ur" else TTS_VOICE_EN)
    cache_file = get_cache_path(spoken_text, chosen_voice)

    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
        with open(cache_file, "rb") as f:
            return f.read()

    try:
        audio_bytes = await _synthesize_edge(spoken_text, chosen_voice)
        if audio_bytes:
            with open(cache_file, "wb") as f:
                f.write(audio_bytes)
        return audio_bytes
    except Exception as e:
        logger.error("Edge-TTS failed (%s): %s", chosen_voice, e)
        return b""


def split_into_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?؛؟\n])\s+", text)
    cleaned = [s.strip() for s in sentences if s.strip()]
    return cleaned if cleaned else [text.strip()]


async def stream_tts_sentences(
    text: str,
    voice: Optional[str] = None,
    language: Optional[str] = None,
) -> AsyncGenerator[Tuple[str, bytes], None]:
    """Generate sentence TTS concurrently (prefetches sentence N+1 while yielding N)."""
    sentences = [s for s in split_into_sentences(text) if s.strip()]
    if not sentences:
        return

    tasks = [
        asyncio.create_task(generate_tts_bytes(sent, voice=voice, language=language))
        for sent in sentences
    ]
    for sent, task in zip(sentences, tasks):
        audio_data = await task
        if audio_data:
            yield clean_for_tts(sent, language=language), audio_data


def get_groq_client() -> Groq:
    global _groq_client
    if _groq_client is None:
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def transcribe_audio_groq(audio_file_path_or_bytes, mime_type: str = "audio/wav", language: Optional[str] = None) -> str:
    try:
        client = get_groq_client()
        if isinstance(audio_file_path_or_bytes, bytes):
            file_obj = ("audio.wav", audio_file_path_or_bytes, mime_type)
        else:
            file_obj = open(audio_file_path_or_bytes, "rb")

        kwargs = dict(
            model="whisper-large-v3-turbo",
            file=file_obj,
            response_format="text",
            temperature=0.0,
            prompt="Transcribe clearly in English or Urdu for JARVIS voice assistant.",
        )
        if language:
            kwargs["language"] = language

        transcription = client.audio.transcriptions.create(**kwargs)
        if hasattr(file_obj, "close"):
            file_obj.close()
        return transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
    except Exception as e:
        logger.error("Groq Whisper STT failed: %s", e)
        return ""