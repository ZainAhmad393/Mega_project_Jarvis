import os
import sys
import time
import re
import signal
import base64
import threading
import webbrowser
import socket
import logging
from dotenv import load_dotenv
import uvicorn
from colorama import Fore, Style, init

load_dotenv()
init(autoreset=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("JarvisMain")

HOST = os.getenv("HOST") or os.getenv("SERVER_HOST", "0.0.0.0")
DEFAULT_PORT = int(os.getenv("PORT") or os.getenv("SERVER_PORT", 8000))
WAKE_WORD_ENABLED = os.getenv("WAKE_WORD_ENABLED", "false").lower() in ("true", "1", "yes")


def find_available_port(start_port: int, max_attempts: int = 15) -> int:
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, 0))
        return s.getsockname()[1]


def get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


PORT = find_available_port(DEFAULT_PORT)
_shutdown_event = threading.Event()


def _handle_shutdown(signum, frame):
    logger.info("Shutdown signal received. Stopping Jarvis gracefully...")
    _shutdown_event.set()


signal.signal(signal.SIGINT, _handle_shutdown)
signal.signal(signal.SIGTERM, _handle_shutdown)


def open_browser(url: str, delay: float = 1.5):
    def _open():
        time.sleep(delay)
        webbrowser.open(url)
    threading.Thread(target=_open, daemon=True).start()


def _is_likely_noise(text: str) -> bool:
    if not text or not text.strip():
        return True
    t = text.strip()
    if len(t) < 2:
        return True
    return bool(re.match(r"^(uh|um|hmm|ah|er|oh)$", t, flags=re.IGNORECASE))


def start_wake_word_listener():
    """Optional PC microphone background wake-word listener ('Hi Jarvis' / 'Jarvis')."""
    try:
        import speech_recognition as sr
        import client as ai_client
        import voice_service as voice
        from server import manager, broadcast_from_thread

        recognizer = sr.Recognizer()
        recognizer.energy_threshold = 300
        recognizer.dynamic_energy_threshold = True
        recognizer.pause_threshold = 0.9

        def _listen_loop():
            try:
                mic = sr.Microphone()
            except Exception as e:
                logger.warning("Microphone unavailable for background wake word: %s", e)
                return

            with mic as source:
                recognizer.adjust_for_ambient_noise(source, duration=1.5)
                logger.info("PC wake word active. Say 'Hi Jarvis' or 'Jarvis'.")

                while not _shutdown_event.is_set():
                    try:
                        audio = recognizer.listen(source, timeout=5.0, phrase_time_limit=12.0)
                        text = voice.transcribe_audio_groq(audio.get_wav_data())
                        if _is_likely_noise(text):
                            continue

                        if "jarvis" in text.lower():
                            command = re.sub(r"\b(hi|hey|hello)?\s*jarvis\b", "", text, flags=re.IGNORECASE).strip()
                            if not command:
                                command = "hi jarvis"

                            broadcast_from_thread(manager.set_state("thinking", text=command))
                            res = ai_client.jarvis_ai.process(command)
                            reply = res["response"]
                            sentiment = res.get("sentiment", "neutral")

                            async def _send_reply():
                                await manager.set_state("speaking", sentiment=sentiment, text=reply)
                                async for sent, audio_bytes in voice.stream_tts_sentences(reply):
                                    payload = {
                                        "type": "audio_sentence",
                                        "text": sent,
                                        "audio_base64": base64.b64encode(audio_bytes).decode("utf-8"),
                                        "sentiment": sentiment,
                                    }
                                    await manager.broadcast(payload)
                                await manager.set_state("idle")

                            broadcast_from_thread(_send_reply())
                    except sr.WaitTimeoutError:
                        continue
                    except Exception as e:
                        logger.debug("Wake loop note: %s", e)
                        time.sleep(0.4)

        threading.Thread(target=_listen_loop, daemon=True).start()
    except ImportError:
        logger.warning("SpeechRecognition not installed for PC wake-word.")


def main():
    for d in ["data", "temp", "temp/cache", "logs", "face"]:
        os.makedirs(d, exist_ok=True)

    local_url = f"http://127.0.0.1:{PORT}/face/index.html"
    lan_ip = get_lan_ip()
    mobile_url = f"http://{lan_ip}:{PORT}/face/index.html"

    print(f"{Fore.CYAN}● JARVIS Enterprise v2.2 Online{Style.RESET_ALL}")
    print(f"{Fore.GREEN}● Desktop URL: {Style.BRIGHT}{local_url}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}● Mobile LAN:  {Style.BRIGHT}{mobile_url}{Style.RESET_ALL}")
    print(f"{Fore.YELLOW}● Wake Word:   {WAKE_WORD_ENABLED}{Style.RESET_ALL}\n")

    if WAKE_WORD_ENABLED:
        start_wake_word_listener()

    open_browser(local_url, delay=1.8)

    uvicorn.run(
        "server:app",
        host=HOST,
        port=PORT,
        log_level="warning",
        access_log=False,
        reload=False,
        loop="asyncio",
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{Fore.CYAN}JARVIS shutdown complete. Goodbye Sir.{Style.RESET_ALL}")
        sys.exit(0)