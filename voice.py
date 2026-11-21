"""
voice.py — Jarvis Edge-TTS CLI test utility
--------------------------------------------
Usage:
  python voice.py --test    Generate and PLAY Urdu, English, and mixed samples
  python voice.py --list    Print available ur-* and en-* edge-tts voices
"""

import asyncio
import argparse
import subprocess
import sys
from pathlib import Path

import edge_tts

from voice_service import (
    TTS_VOICE_EN,
    TTS_VOICE_UR,
    TTS_RATE,
    TTS_PITCH,
    generate_tts_bytes,
    clean_for_tts,
    split_by_script,
)

OUTPUT_DIR = Path(__file__).parent / "temp" / "tts_test"


# ---------------------------------------------------------------------------
# --list: print available Urdu and English voices
# ---------------------------------------------------------------------------
async def list_voices() -> None:
    voices = await edge_tts.list_voices()
    print(f"\n{'Name':<35} {'Gender':<10} {'Locale'}")
    print("-" * 65)
    for voice in sorted(voices, key=lambda v: v.get("ShortName", "")):
        name = voice.get("ShortName", "")
        if name.startswith(("ur-", "en-")):
            print(f"{name:<35} {voice.get('Gender', ''):<10} {voice.get('Locale', '')}")


# ---------------------------------------------------------------------------
# --test: synthesize and play three samples
# ---------------------------------------------------------------------------
_SAMPLES = {
    "urdu":    "السلام علیکم جناب، آپ کا دن کیسا گزرا؟ میں ہر وقت آپ کی خدمت کے لیے حاضر ہوں۔",
    "english": "Good evening, Sir. All systems are operational. How may I assist you today?",
    "mixed":   "جناب، آپ کا WhatsApp message تیار ہے۔ کیا میں اسے بھیج دوں؟",
}


def _play(path: Path) -> None:
    """Play an MP3 file using the best available player on Windows."""
    try:
        # Windows: use built-in Media Player via PowerShell
        subprocess.run(
            ["powershell", "-c", f"(New-Object Media.SoundPlayer).Stop(); "
             f"Add-Type -AssemblyName presentationCore; "
             f"$mp = New-Object System.Windows.Media.MediaPlayer; "
             f"$mp.Open([uri]'{path.as_posix()}'); $mp.Play(); Start-Sleep -s 5; $mp.Close()"],
            check=False, timeout=15, capture_output=True
        )
    except Exception:
        # Fallback: just notify the user
        print(f"  ▶  Audio saved to {path}  (open manually to listen)")


async def run_test() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\nVoices  : Urdu={TTS_VOICE_UR}  English={TTS_VOICE_EN}")
    print(f"Settings: rate={TTS_RATE}  pitch={TTS_PITCH}\n")

    for label, raw_text in _SAMPLES.items():
        print(f"[{label.upper()}] Original text  : {raw_text}")
        cleaned = clean_for_tts(raw_text)
        print(f"[{label.upper()}] After clean_for_tts: {cleaned}")

        # Detect script segments to pick the right voice automatically
        segments = split_by_script(cleaned)
        all_audio = bytearray()
        for seg_text, lang in segments:
            seg_voice = TTS_VOICE_UR if lang == "ur" else TTS_VOICE_EN
            audio = await generate_tts_bytes(seg_text, voice=seg_voice)
            all_audio.extend(audio)
            print(f"  segment ({lang}, {seg_voice}): {seg_text!r} → {len(audio)} bytes")

        out_path = OUTPUT_DIR / f"{label}.mp3"
        out_path.write_bytes(bytes(all_audio))
        print(f"  ✔ Saved: {out_path}  ({len(all_audio)} bytes total)")
        _play(out_path)
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Jarvis Edge-TTS voice test utility")
    parser.add_argument("--test", action="store_true",
                        help="Synthesize and play Urdu, English, and mixed samples")
    parser.add_argument("--list", action="store_true",
                        help="List all available ur-* and en-* Edge-TTS voices")
    args = parser.parse_args()

    if args.list:
        asyncio.run(list_voices())
    elif args.test:
        asyncio.run(run_test())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
