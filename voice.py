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
TTS_RATE_UR = os.getenv("TTS_RATE_UR", "+4%").strip()
TTS_PITCH = os.getenv("TTS_PITCH", "-2Hz").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo").strip()

_groq_client: Optional[Groq] = None

_URDU_ABBREVIATIONS = {
    "email": "ای میل",
    "whatsapp": "واٹس ایپ",
    "youtube": "یوٹیوب",
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
}

# Optional pronunciation hints for Roman-Urdu text. Not applied automatically because words
# like "main" are also normal English; call prepare_text_for_tts() yourself where needed.
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
    urdu_chars = sum(1 for c in text if _is_urdu_char(c))
    latin_chars = sum(1 for c in text if c.isascii() and c.isalpha())
    if urdu_chars > 0 and latin_chars > 0:
        return "mix" if urdu_chars >= 3 and latin_chars >= 3 else ("ur" if urdu_chars > latin_chars else "en")
    if urdu_chars > 0:
        return "ur"
    return "en"


def get_voice_for_text(text: str) -> str:
    return TTS_VOICE_UR if detect_language(text) == "ur" else TTS_VOICE_EN


def split_by_script(text: str) -> List[Tuple[str, str]]:
    """Splits mixed Urdu & English text into clean chunks per script so each uses its native voice."""
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
        lang = "ur" if has_urdu else "en"
        if segments and segments[-1][1] == lang:
            segments[-1] = (segments[-1][0] + " " + stripped, lang)
        else:
            segments.append((stripped, lang))

    merged: List[Tuple[str, str]] = []
    for seg_text, lang in segments:
        if lang == "ur" and len(seg_text.split()) < 2 and merged:
            merged[-1] = (merged[-1][0] + " " + seg_text, merged[-1][1])
        else:
            merged.append((seg_text, lang))
    return merged


def clean_for_tts(text: str, language: Optional[str] = None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"https?://\S+|www\.\S+", " link ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"[\u2010-\u2015\-_*>#`~]+", " ", cleaned)
    cleaned = "".join(c for c in cleaned if not unicodedata.category(c).startswith(("So", "Cs")))
    lang = language or ("ur" if any(_is_urdu_char(c) for c in cleaned) else "en")
    replacements = _URDU_ABBREVIATIONS if lang == "ur" else _ENGLISH_ABBREVIATIONS
    for src, dst in replacements.items():
        cleaned = re.sub(rf"\b{re.escape(src)}\b", dst, cleaned, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()


def get_cache_path(text: str, voice: str) -> str:
    rate = TTS_RATE_UR if voice.startswith("ur-") else TTS_RATE
    key = f"edge3:{voice}:{rate}:{TTS_PITCH}:{text.strip().lower()}"
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.mp3")


def _write_cache_atomic(path: str, data: bytes):
    """Write to a temp file then rename, so a parallel request never reads a half-written mp3."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("TTS cache write failed: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass


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
        detected = "ur" if any(_is_urdu_char(c) for c in clean_text) else "en"

    spoken_text = clean_for_tts(clean_text, language=detected)
    if not spoken_text:
        return b""

    chosen_voice = voice or (TTS_VOICE_UR if detected == "ur" else TTS_VOICE_EN)
    cache_file = get_cache_path(spoken_text, chosen_voice)

    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 0:
        try:
            with open(cache_file, "rb") as f:
                return f.read()
        except OSError:
            pass

    try:
        audio_bytes = await _synthesize_edge(spoken_text, chosen_voice)
        if audio_bytes:
            _write_cache_atomic(cache_file, audio_bytes)
        return audio_bytes
    except Exception as e:
        logger.error("Edge-TTS failed (%s): %s", chosen_voice, e)
        return b""


async def generate_tts_segments(text: str, language_hint: Optional[str] = None) -> List[Tuple[str, bytes]]:
    if not text or not text.strip():
        return []
    segments = split_by_script(text)
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

_AUDIO_EXTENSIONS = {
    "webm": "webm", "ogg": "ogg", "opus": "ogg", "mp4": "mp4", "m4a": "m4a", "x-m4a": "m4a",
    "aac": "m4a", "mpeg": "mp3", "mp3": "mp3", "wav": "wav", "x-wav": "wav", "wave": "wav",
    "flac": "flac",
}


def audio_filename_for_mime(mime_type: str) -> str:
    """Groq decides the audio format from the FILE NAME, so name it after the real container.
    Browsers record webm/ogg/mp4; calling them 'audio.wav' makes decoding fail."""
    subtype = (mime_type or "").split(";")[0].strip().lower().split("/")[-1]
    return f"audio.{_AUDIO_EXTENSIONS.get(subtype, 'wav')}"


def transcribe_audio_groq(audio_file_path_or_bytes, mime_type: str = "audio/wav", language: Optional[str] = None) -> str:
    file_obj = None
    try:
        client = get_groq_client()
        if isinstance(audio_file_path_or_bytes, bytes):
            if len(audio_file_path_or_bytes) < 2500:
                return ""
            file_obj = (audio_filename_for_mime(mime_type), audio_file_path_or_bytes, (mime_type or "audio/wav").split(";")[0])
        else:
            file_obj = open(audio_file_path_or_bytes, "rb")

        kwargs = dict(
            model=STT_MODEL,
            file=file_obj,
            response_format="text",
            temperature=0.0,
        )
        if language in ("en", "ur"):
            kwargs["language"] = language

        transcription = client.audio.transcriptions.create(**kwargs)
        result = transcription.strip() if isinstance(transcription, str) else str(getattr(transcription, "text", transcription)).strip()
        if result.lower() in _WHISPER_HALLUCINATIONS:
            return ""
        return result
    except Exception as e:
        logger.error("Groq Whisper STT failed: %s", e)
        return ""
    finally:
        if file_obj is not None and hasattr(file_obj, "close"):
            try:
                file_obj.close()
            except Exception:
                pass