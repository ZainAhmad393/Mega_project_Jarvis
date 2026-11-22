import os
import re
import hashlib
import asyncio
import logging
import unicodedata
from typing import AsyncGenerator, Optional, Tuple, List
import edge_tts
from groq import Groq
from dotenv import load_dotenv

load_dotenv(override=True)
logger = logging.getLogger("JarvisVoice")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

TTS_VOICE_EN = os.getenv("TTS_VOICE_EN", "en-GB-RyanNeural").strip()
TTS_VOICE_UR = os.getenv("TTS_VOICE_UR", "ur-PK-AsadNeural").strip()
TTS_RATE = os.getenv("TTS_RATE", "+4%").strip()
TTS_RATE_UR = os.getenv("TTS_RATE_UR", "+0%").strip()
TTS_PITCH = os.getenv("TTS_PITCH", "-2Hz").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

_groq_client: Optional[Groq] = None

_URDU_ABBREVIATIONS = {
    "email": "ای میل",
    "whatsapp": "واٹس ایپ",
    "youtube": "یوٹیوب",
    "ai": "اے آئی",
    "api": "اے پی آئی",
    "todo": "کام",
    "task": "ٹاسک",
    "tasks": "ٹاسک",
    "ٹاسکس": "ٹاسک",
    "message": "میسج",
    "call": "کال",
    "meeting": "میٹنگ",
    "reminder": "ریمائنڈر",
    "schedule": "شیڈول",
    "online": "آن لائن",
    "offline": "آف لائن",
    "ready": "تیار",
    "sir": "سر",
    "jarvis": "جاروس",
    "ok": "اوکے",
    "okay": "اوکے",
    "sorry": "سوری",
    "hello": "ہیلو",
    "hi": "ہائے",
    "thanks": "شکریہ",
    "thank you": "شکریہ",
    "zain": "زین",
    "ahmad": "احمد",
}

_ROMAN_TO_URDU_WORDS = {
    "assalamualaikum": "السلام علیکم",
    "salam": "سلام",
    "walaikum": "وعلیکم",
    "aap": "آپ",
    "ap": "آپ",
    "kaise": "کیسے",
    "kese": "کیسے",
    "kaisa": "کیسا",
    "kaisi": "کیسی",
    "theek": "ٹھیک",
    "thk": "ٹھیک",
    "hoon": "ہوں",
    "hun": "ہوں",
    "hn": "ہوں",
    "hu": "ہوں",
    "hai": "ہے",
    "hei": "ہے",
    "hain": "ہیں",
    "ho": "ہو",
    "mein": "میں",
    "main": "میں",
    "mujhe": "مجھے",
    "mujy": "مجھے",
    "mera": "میرا",
    "meri": "میری",
    "mere": "میرے",
    "tum": "تم",
    "tumhara": "تمہارا",
    "hum": "ہم",
    "ham": "ہم",
    "kya": "کیا",
    "kia": "کیا",
    "kyun": "کیوں",
    "kab": "کب",
    "kahan": "کہاں",
    "kaun": "کون",
    "kon": "کون",
    "haan": "ہاں",
    "han": "ہاں",
    "ji": "جی",
    "nahi": "نہیں",
    "nahin": "نہیں",
    "ni": "نہیں",
    "acha": "اچھا",
    "accha": "اچھا",
    "bohat": "بہت",
    "bahut": "بہت",
    "bht": "بہت",
    "bhai": "بھائی",
    "bahi": "بھائی",
    "yaar": "یار",
    "karo": "کرو",
    "kro": "کرو",
    "karna": "کرنا",
    "krna": "کرنا",
    "kardo": "کر دو",
    "krdo": "کر دو",
    "batao": "بتاؤ",
    "btao": "بتاؤ",
    "bolo": "بولو",
    "kaho": "کہو",
    "bhejo": "بھیجو",
    "likho": "لکھو",
    "kholo": "کھولو",
    "chahiye": "چاہیے",
    "madad": "مدد",
    "shukriya": "شکریہ",
    "baat": "بات",
    "bat": "بات",
    "abhi": "ابھی",
    "aur": "اور",
    "bhi": "بھی",
    "sab": "سب",
    "kuch": "کچھ",
    "yaad": "یاد",
    "waqt": "وقت",
    "mausam": "موسم",
    "raha": "رہا",
    "rahi": "رہی",
    "gaya": "گیا",
    "gayi": "گئی",
}

_ENGLISH_ABBREVIATIONS = {
    "jarvis": "Jarvis",
    "zain ahmad": "Zayn Ahmad",
}


def _is_urdu_char(char: str) -> bool:
    return bool(re.match(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]", char))


def _contains_roman_urdu(text: str) -> bool:
    words = re.findall(r"\b[a-zA-Z]+\b", text.lower())
    if not words:
        return False
    hits = sum(1 for w in words if w in _ROMAN_TO_URDU_WORDS)
    return hits >= 2 or (hits >= 1 and hits / len(words) >= 0.25)


def _transliterate_roman_urdu(text: str) -> str:
    def _sub(m):
        w = m.group(0).lower()
        return _ROMAN_TO_URDU_WORDS.get(w) or _URDU_ABBREVIATIONS.get(w) or m.group(0)

    all_keys = list(_ROMAN_TO_URDU_WORDS.keys()) + list(_URDU_ABBREVIATIONS.keys())
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, all_keys)) + r")\b", re.IGNORECASE)
    return pattern.sub(_sub, text)


def detect_language(text: str) -> str:
    urdu_chars = sum(1 for c in text if _is_urdu_char(c))
    latin_chars = sum(1 for c in text if c.isascii() and c.isalpha())
    if urdu_chars > 0 and latin_chars > 0:
        return "mix" if urdu_chars >= 3 and latin_chars >= 6 else ("ur" if urdu_chars >= latin_chars else "en")
    if urdu_chars > 0:
        return "ur"
    if _contains_roman_urdu(text):
        return "ur"
    return "en"


def get_voice_for_text(text: str) -> str:
    return TTS_VOICE_UR if detect_language(text) in ("ur", "mix") else TTS_VOICE_EN


def split_by_script(text: str, language_hint: Optional[str] = None) -> List[Tuple[str, str]]:
    """Splits text per script, keeping Urdu-mode sentences smoothly in Pakistani Urdu voice."""
    if language_hint in ("ur", "mix") or _contains_roman_urdu(text):
        converted = _transliterate_roman_urdu(text)
        if any(_is_urdu_char(c) for c in converted):
            return [(converted, "ur")]

    parts = re.findall(
        r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF\s,،؛؟!.\d]+|[^\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]+",
        text,
    )
    segments: List[Tuple[str, str]] = []
    for part in parts:
        stripped = part.strip()
        if not stripped:
            continue
        has_urdu = any(_is_urdu_char(c) for c in stripped)
        has_latin = any(c.isascii() and c.isalpha() for c in stripped)
        if not has_urdu and not has_latin:
            if segments:
                segments[-1] = (segments[-1][0] + " " + stripped, segments[-1][1])
            continue
        lang = "ur" if (has_urdu or _contains_roman_urdu(stripped)) else "en"
        if segments and segments[-1][1] == lang:
            segments[-1] = (segments[-1][0] + " " + stripped, lang)
        else:
            segments.append((stripped, lang))

    return segments


def clean_for_tts(text: str, language: Optional[str] = None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"https?://\S+|www\.\S+", " link ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bj\s*\.\s*a\s*\.\s*r\s*\.\s*v\s*\.\s*i\s*\.\s*s\.?", "Jarvis", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\u2010-\u2015\-_*>#`~]+", " ", cleaned)
    cleaned = "".join(c for c in cleaned if not unicodedata.category(c).startswith(("So", "Cs")))
    lang = language or ("ur" if (any(_is_urdu_char(c) for c in cleaned) or _contains_roman_urdu(cleaned)) else "en")
    if lang in ("ur", "mix"):
        cleaned = _transliterate_roman_urdu(cleaned)
    else:
        for src, dst in _ENGLISH_ABBREVIATIONS.items():
            cleaned = re.sub(rf"\b{re.escape(src)}\b", dst, cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def get_cache_path(text: str, voice: str) -> str:
    rate = TTS_RATE_UR if voice.startswith("ur-") else TTS_RATE
    key = f"edge5:{voice}:{rate}:{TTS_PITCH}:{text.strip().lower()}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.mp3")


async def _synthesize_edge(text: str, voice: str) -> bytes:
    if not re.search(r"[A-Za-z0-9\u0600-\u06FF]", text):
        return b""
    try:
        if voice.startswith("ur-"):
            communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE_UR)
        else:
            communicate = edge_tts.Communicate(text, voice, rate=TTS_RATE, pitch=TTS_PITCH)
        audio_stream = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_stream.extend(chunk["data"])
        if audio_stream:
            return bytes(audio_stream)
    except Exception as e:
        logger.warning("Edge-TTS primary failed (%s): %s", voice, e)

    for fallback_voice in (voice, "ur-PK-UzmaNeural" if voice.startswith("ur-") else TTS_VOICE_EN):
        try:
            communicate = edge_tts.Communicate(text, fallback_voice)
            audio_stream = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_stream.extend(chunk["data"])
            if audio_stream:
                return bytes(audio_stream)
        except Exception as e:
            logger.warning("Edge-TTS fallback failed (%s): %s", fallback_voice, e)
            continue
    return b""


async def generate_tts_bytes(text: str, voice: Optional[str] = None, language: Optional[str] = None) -> bytes:
    clean_text = (text or "").strip()
    if not clean_text:
        return b""

    detected = language or detect_language(clean_text)
    if detected == "mix":
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


async def generate_tts_segments(text: str, language_hint: Optional[str] = None) -> List[Tuple[str, bytes]]:
    if not text or not text.strip():
        return []
    normalized = re.sub(r"\bj\s*\.\s*a\s*\.\s*r\s*\.\s*v\s*\.\s*i\s*\.\s*s\.?", "Jarvis", text, flags=re.IGNORECASE)
    segments = split_by_script(normalized, language_hint=language_hint)
    if not segments:
        return []

    tasks = [
        asyncio.create_task(generate_tts_bytes(seg_text, language=seg_lang))
        for seg_text, seg_lang in segments
    ]
    results = []
    for (seg_text, seg_lang), task in zip(segments, tasks):
        audio = await task
        if audio:
            results.append((clean_for_tts(seg_text, language=seg_lang), audio))
    return results


def split_into_sentences(text: str) -> list[str]:
    sentences = re.split(r"(?<=[.!?؛؟\n])\s+", text)
    cleaned = [s.strip() for s in sentences if s.strip()]
    return cleaned if cleaned else [text.strip()]


async def stream_tts_sentences(
    text: str,
    voice: Optional[str] = None,
    language: Optional[str] = None,
) -> AsyncGenerator[Tuple[str, bytes], None]:
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


_WHISPER_HALLUCINATIONS = {
    "transcribe accurately in english, urdu, or roman urdu for jarvis assistant.",
    "transcribe in english, urdu, or roman urdu.",
    "thank you.", "thanks for watching.", "the the", "you", "okay.", "bye.", ".",
}


def transcribe_audio_groq(audio_file_path_or_bytes, mime_type: str = "audio/wav", language: Optional[str] = None) -> str:
    try:
        client = get_groq_client()
        if isinstance(audio_file_path_or_bytes, bytes):
            if len(audio_file_path_or_bytes) < 2500:
                return ""
            file_obj = ("audio.wav", audio_file_path_or_bytes, mime_type)
        else:
            file_obj = open(audio_file_path_or_bytes, "rb")

        kwargs = dict(
            model="whisper-large-v3-turbo",
            file=file_obj,
            response_format="text",
            temperature=0.0,
        )
        if language in ("en", "ur"):
            kwargs["language"] = language

        transcription = client.audio.transcriptions.create(**kwargs)
        if hasattr(file_obj, "close"):
            file_obj.close()
        result = transcription.strip() if isinstance(transcription, str) else str(transcription).strip()
        if result.lower() in _WHISPER_HALLUCINATIONS:
            return ""
        return result
    except Exception as e:
        logger.error("Groq Whisper STT failed: %s", e)
        return ""