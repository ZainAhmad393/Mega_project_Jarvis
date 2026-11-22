import os
import hmac
import hashlib
import base64
import json
import logging
import asyncio
import time
import random
import uuid
from contextlib import asynccontextmanager
from typing import Set, Dict, Any, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Request, Response, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

import client as ai_client
import voice_service as voice
import tools
from memory import memory

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("JarvisServer")

MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None
JARVIS_PIN = os.getenv("JARVIS_PIN", "").strip()
_AUTH_KEY = hashlib.sha256(
    (os.getenv("AUTH_SECRET") or f"jarvis-pin-secret-{JARVIS_PIN}").encode("utf-8")
).digest()

INTRO_GREETINGS = [
    "Good day, Sir. Jarvis is online and ready. How may I assist you?",
    "Hello, Sir. Jarvis at your service. What can I handle for you?",
    "Jarvis online, Sir. All systems are operational.",
    "Standing by for your command, Sir.",
]


def get_random_greeting() -> str:
    return random.choice(INTRO_GREETINGS)


def _make_auth_token() -> str:
    exp = str(int(time.time()) + 30 * 24 * 3600)
    sig = hmac.new(_AUTH_KEY, exp.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _verify_auth_token(token: Optional[str]) -> bool:
    if not JARVIS_PIN:
        return True
    if not token or "." not in token:
        return False
    try:
        exp_str, sig = token.split(".", 1)
        if int(exp_str) < int(time.time()):
            return False
        expected = hmac.new(_AUTH_KEY, exp_str.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _extract_token_from_request(request: Request) -> Optional[str]:
    cookie_tok = request.cookies.get("jarvis_auth")
    if cookie_tok:
        return cookie_tok
    auth_hdr = request.headers.get("Authorization", "")
    if auth_hdr.startswith("Bearer "):
        return auth_hdr[7:].strip()
    return None


def require_auth(request: Request):
    if not _verify_auth_token(_extract_token_from_request(request)):
        raise HTTPException(status_code=401, detail="Authentication required")


class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.current_state: str = "idle"
        self.current_sentiment: str = "neutral"

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        logger.info("Client connected. Total clients: %d", len(self.active_connections))
        await websocket.send_json({
            "type": "state",
            "state": self.current_state,
            "sentiment": self.current_sentiment,
        })

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        logger.info("Client disconnected. Remaining: %d", len(self.active_connections))

    async def broadcast(self, data: Dict[str, Any]):
        for conn in list(self.active_connections):
            try:
                await conn.send_json(data)
            except Exception as e:
                logger.warning("Broadcast failed: %s", e)
                self.active_connections.discard(conn)

    async def set_state(self, state: str, sentiment: Optional[str] = None, text: Optional[str] = None):
        self.current_state = state
        if sentiment:
            self.current_sentiment = sentiment
        payload: Dict[str, Any] = {
            "type": "state",
            "state": self.current_state,
            "sentiment": self.current_sentiment,
        }
        if text:
            payload["text"] = text
        await self.broadcast(payload)


manager = ConnectionManager()


def broadcast_from_thread(coro):
    if MAIN_LOOP and MAIN_LOOP.is_running():
        return asyncio.run_coroutine_threadsafe(coro, MAIN_LOOP)
    return None


async def reminder_scheduler_loop():
    logger.info("Proactive task reminder scheduler started.")
    while True:
        try:
            due_items = memory.get_due_reminders()
            for item in due_items:
                memory.mark_todo_reminded(
                    item["id"],
                    recurrence=item.get("recurrence", "once"),
                    current_due=item.get("due_time"),
                )
                reminder_text = f"Sir, this is your scheduled reminder: {item['task']}."
                audio_bytes = await voice.generate_tts_bytes(reminder_text, language="en")
                b64 = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None
                await manager.set_state("speaking", sentiment="joy", text=reminder_text)
                await manager.broadcast({
                    "type": "reminder",
                    "text": reminder_text,
                    "audio_base64": b64,
                    "sentiment": "joy",
                })
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Reminder scheduler error: %s", e)
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    scheduler_task = asyncio.create_task(reminder_scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Jarvis AI Assistant", version="2.4.0-enterprise", lifespan=lifespan)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    start_time = time.time()
    response = await call_next(request)
    process_time = (time.time() - start_time) * 1000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-ms"] = f"{process_time:.2f}"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FACE_DIR = os.path.join(BASE_DIR, "face")
CACHE_DIR = os.path.join(BASE_DIR, "temp", "cache")
os.makedirs(FACE_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

app.mount("/face", StaticFiles(directory=FACE_DIR, html=True), name="face")
app.mount("/cache", StaticFiles(directory=CACHE_DIR), name="cache")


class LoginRequest(BaseModel):
    pin: str


class ChatRequest(BaseModel):
    message: str
    language: Optional[str] = "en"


class ChatResponse(BaseModel):
    reply: str
    sentiment: str
    audio_base64: Optional[str] = None


class TodoCreateRequest(BaseModel):
    task: str
    due_time: Optional[str] = None
    recurrence: str = "once"


class ContactCreateRequest(BaseModel):
    name: str
    phone: str = ""
    email: str = ""
    notes: str = ""


@app.get("/api/auth/status")
async def auth_status(request: Request):
    tok = _extract_token_from_request(request)
    return {"auth_required": bool(JARVIS_PIN), "authenticated": _verify_auth_token(tok)}


@app.post("/api/auth/login")
async def auth_login(req: LoginRequest, response: Response):
    if not JARVIS_PIN:
        return {"authenticated": True, "token": ""}
    if not hmac.compare_digest(req.pin.strip(), JARVIS_PIN):
        raise HTTPException(status_code=401, detail="Invalid PIN")
    token = _make_auth_token()
    response.set_cookie(
        key="jarvis_auth",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=30 * 24 * 3600,
    )
    return {"authenticated": True, "token": token}


@app.post("/api/auth/logout")
async def auth_logout(response: Response):
    response.delete_cookie("jarvis_auth")
    return {"authenticated": False}


async def process_user_message(
    websocket: WebSocket,
    user_text: str,
    request_id: str,
    language: Optional[str] = None,
    image_base64: Optional[str] = None,
):
    started_at = time.perf_counter()
    await manager.set_state("thinking", text=user_text)
    memory.save_chat_message("user", user_text)

    tts_queue: asyncio.Queue = asyncio.Queue()
    SENTINEL = object()
    collected_reply = []
    last_open_url = None

    async def _safe_tts(text_seg: str, lang_hint: Optional[str]):
        try:
            return await asyncio.wait_for(
                voice.generate_tts_segments(text_seg, language_hint=lang_hint),
                timeout=6.0,
            )
        except Exception:
            return []

    async def llm_producer():
        try:
            async for chunk in ai_client.jarvis_ai.process_stream(
                user_text, language_hint=language, image_base64=image_base64
            ):
                sent = chunk.get("sentence", "").strip()
                if not sent:
                    continue
                tts_task = asyncio.create_task(_safe_tts(sent, chunk.get("language")))
                await tts_queue.put((chunk, tts_task))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("LLM stream error: %s", e)
        finally:
            await tts_queue.put(SENTINEL)

    producer_task = asyncio.create_task(llm_producer())
    first_audio_sent = False
    sentence_index = 0

    try:
        while True:
            item = await tts_queue.get()
            if item is SENTINEL:
                break

            chunk, tts_task = item
            segments = await tts_task
            full_sent = chunk["sentence"]
            collected_reply.append(full_sent)
            sentiment = chunk.get("sentiment", "neutral")
            action = chunk.get("action", "chat")
            open_url = chunk.get("open_url")
            if open_url:
                last_open_url = open_url
            is_first_sentence = (sentence_index == 0)
            sentence_index += 1

            if not first_audio_sent:
                first_audio_sent = True
                total_ms = (time.perf_counter() - started_at) * 1000
                logger.info("[LATENCY] End-to-first-audio: %.0f ms", total_ms)
                await manager.set_state("speaking", sentiment=sentiment, text=full_sent)

            if segments:
                for idx, (seg_text, audio_data) in enumerate(segments):
                    msg_out = {
                        "type": "audio_sentence",
                        "request_id": request_id,
                        "text": full_sent if idx == 0 else "",
                        "is_first_sentence": is_first_sentence and idx == 0,
                        "spoken_segment": seg_text,
                        "audio_base64": base64.b64encode(audio_data).decode("utf-8"),
                        "sentiment": sentiment,
                        "action": action,
                        "open_url": open_url if idx == 0 else None,
                    }
                    await websocket.send_json(msg_out)
            else:
                msg_out = {
                    "type": "response",
                    "request_id": request_id,
                    "text": full_sent,
                    "is_first_sentence": is_first_sentence,
                    "sentiment": sentiment,
                    "action": action,
                    "open_url": open_url,
                }
                await websocket.send_json(msg_out)

        if collected_reply:
            memory.save_chat_message("assistant", " ".join(collected_reply), action_url=last_open_url)
    except asyncio.CancelledError:
        producer_task.cancel()
        raise
    finally:
        if not producer_task.done():
            producer_task.cancel()


@app.post("/api/voice/note", dependencies=[Depends(require_auth)])
async def voice_note(file: UploadFile = File(...), language: Optional[str] = None):
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty voice note")
    lang_hint = language if language in ("en", "ur") else None
    text = voice.transcribe_audio_groq(audio_bytes, mime_type=file.content_type or "audio/webm", language=lang_hint)
    if not text:
        raise HTTPException(status_code=422, detail="Voice note could not be transcribed")
    return {"text": text}


@app.get("/api/dashboard", dependencies=[Depends(require_auth)])
async def get_dashboard():
    return {
        "todos": memory.get_todos("pending"),
        "contacts": memory.list_contacts(),
        "memories": memory.recall(),
        "skills": memory.get_skills(),
        "chat_history": memory.get_recent_chat_messages(30),
    }


@app.post("/api/chat/clear", dependencies=[Depends(require_auth)])
async def clear_chat_history():
    memory.clear_chat_messages()
    ai_client.jarvis_ai.reset()
    return {"status": "cleared"}


@app.post("/api/todos", dependencies=[Depends(require_auth)])
async def create_todo(req: TodoCreateRequest):
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task cannot be empty")
    return tools.add_todo(req.task.strip(), due_time=req.due_time, recurrence=req.recurrence)


@app.post("/api/todos/{todo_id}/complete", dependencies=[Depends(require_auth)])
async def complete_todo_api(todo_id: int):
    return tools.complete_todo(todo_id)


@app.post("/api/contacts", dependencies=[Depends(require_auth)])
async def create_contact_api(req: ContactCreateRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Contact name required")
    return tools.save_contact(req.name, req.phone, req.email, req.notes)


@app.post("/api/contacts/import", dependencies=[Depends(require_auth)])
async def import_contacts_vcf(file: UploadFile = File(...)):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty VCF file")
    text = raw.decode("utf-8", errors="ignore")
    count = tools.import_vcf_contacts(text)
    return {"imported": count, "message": f"Imported {count} contacts successfully."}


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.jpg", include_in_schema=False)
async def favicon():
    favicon_path = os.path.join(BASE_DIR, "favicon.jpg")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/")
async def root():
    index_file = os.path.join(FACE_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Jarvis Backend running. Access /face/index.html"}


@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "state": manager.current_state,
        "sentiment": manager.current_sentiment,
        "clients": len(manager.active_connections),
        "db": "postgres" if memory.is_postgres else "sqlite",
    }


@app.post("/api/chat", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def api_chat(req: ChatRequest):
    user_msg = req.message.strip()
    if not user_msg:
        raise HTTPException(status_code=400, detail="Empty message")

    await manager.set_state("thinking")
    reply, sentiment = await ai_client.ai_process(user_msg)
    await manager.set_state("speaking", sentiment=sentiment, text=reply)
    audio_bytes = await voice.generate_tts_bytes(reply, language=req.language)
    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None
    await manager.set_state("idle")
    return ChatResponse(reply=reply, sentiment=sentiment, audio_base64=audio_b64)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    tok = websocket.cookies.get("jarvis_auth") or websocket.query_params.get("token")
    if not _verify_auth_token(tok):
        await websocket.close(code=4401, reason="Unauthorized")
        return

    await manager.connect(websocket)
    active_response_task = None
    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
            except Exception:
                payload = {"type": "user_message", "text": data}

            msg_type = payload.get("type", "user_message")

            if msg_type == "user_message":
                user_text = payload.get("text", "").strip()
                if not user_text:
                    continue
                if active_response_task and not active_response_task.done():
                    active_response_task.cancel()
                request_id = str(payload.get("request_id") or "")
                lang_val = payload.get("language")
                language = lang_val if lang_val in ("en", "ur", "mix") else None
                image_base64 = payload.get("image_base64")
                active_response_task = asyncio.create_task(
                    process_user_message(websocket, user_text, request_id, language, image_base64=image_base64)
                )

            elif msg_type == "start_call":
                call_type = payload.get("call_type", "video")
                announcement = (
                    "Video call connected, Sir. I am listening."
                    if call_type == "video"
                    else "Audio call connected, Sir. I am listening."
                )
                await manager.set_state("speaking", sentiment="joy", text=announcement)
                audio_bytes = await voice.generate_tts_bytes(announcement, language="en")
                b64_audio = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None
                await websocket.send_json({
                    "type": "call_status",
                    "status": "connected",
                    "call_type": call_type,
                    "text": announcement,
                    "audio_base64": b64_audio,
                    "sentiment": "joy",
                })

            elif msg_type == "end_call":
                farewell = "Goodbye Sir. Call ended."
                await manager.set_state("speaking", sentiment="neutral", text=farewell)
                audio_bytes = await voice.generate_tts_bytes(farewell, language="en")
                b64_audio = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None
                await websocket.send_json({
                    "type": "call_status",
                    "status": "ended",
                    "text": farewell,
                    "audio_base64": b64_audio,
                    "sentiment": "neutral",
                })

            elif msg_type == "interrupt":
                if active_response_task and not active_response_task.done():
                    active_response_task.cancel()
                    active_response_task = None
                await manager.set_state("listening", text="Interrupted. Listening, Sir...")

            elif msg_type == "test_audio":
                test_phrase = "Audio system online, Sir. Speaker volume is optimal."
                audio_bytes = await voice.generate_tts_bytes(test_phrase, language="en")
                b64_audio = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None
                await websocket.send_json({
                    "type": "audio_sentence",
                    "text": test_phrase,
                    "audio_base64": b64_audio,
                    "sentiment": "joy",
                })

            elif msg_type == "intro_greeting":
                greeting = get_random_greeting()
                audio_bytes = await voice.generate_tts_bytes(greeting, language="en")
                b64_audio = base64.b64encode(audio_bytes).decode("utf-8") if audio_bytes else None
                await manager.set_state("speaking", sentiment="neutral", text=greeting)
                await websocket.send_json({
                    "type": "audio_sentence",
                    "text": greeting,
                    "audio_base64": b64_audio,
                    "sentiment": "neutral",
                    "action": "intro",
                })

            elif msg_type == "set_state":
                await manager.set_state(payload.get("state", "idle"), sentiment=payload.get("sentiment"))

    except WebSocketDisconnect:
        if active_response_task and not active_response_task.done():
            active_response_task.cancel()
        manager.disconnect(websocket)
    except Exception as e:
        if active_response_task and not active_response_task.done():
            active_response_task.cancel()
        logger.error("WebSocket exception: %s", e)
        manager.disconnect(websocket)