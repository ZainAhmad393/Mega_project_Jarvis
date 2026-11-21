---
title: JARVIS Assistant
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
---

# 🤖 JARVIS 2.0 - Enterprise AI Assistant with Live 3D VRM Face

An enterprise-ready, personal AI assistant built with **FastAPI**, **Three.js** (@pixiv/three-vrm), **Groq Tool/Function Calling**, **Edge TTS**, and **Groq Whisper STT**. Featuring a 3D animated anime avatar with natural human standing posture, real-time lip-sync, persistent SQLite memory, and a safety confirmation pipeline for sensitive actions.

---

## 🌟 Key Features

1. **Live 3D VRM Avatar (`HairSample_Male.vrm`)**:
   - Natural human standing posture (arms and hands resting naturally along the body sides with subtle breathing motion instead of an unnatural T-pose).
   - Real-time Web Audio `AnalyserNode` driving mouth blendshapes (`aa`, `oh`) synchronized with spoken voice.
   - Idle animations: natural periodic eye-blinking, breathing torso sway, and listening head tilt.
   - Real-time state indicator dot (Green: Idle, Blue: Listening, Orange: Thinking, Purple: Speaking).
   - Dynamic sentiment expressions (`joy`, `sorrow`, `fun`, `neutral`).

2. **Groq Tool / Function Calling Engine**:
   - Model decides all actions dynamically via Groq tool calling schemas.
   - Spoken responses strictly 1–3 sentences in English, Urdu (script), or Roman Urdu to match the user.
   - Model auto-fallback mechanism (defaults to primary and gracefully falls back if a model is unavailable).

3. **Built-in Tools (`tools.py`)**:
   - `play_music(name)`: Plays music from `musicLibrary.py` or searches YouTube.
   - `add_todo(task)`, `get_todo()`, `complete_todo(id)`, `daily_plan()`: Persistent SQLite task management.
   - `remember(key, value)` & `recall()`: Key-value facts and preferences injected dynamically into system prompt.
   - `web_search(query)`: Fast web searches via DuckDuckGo with browser fallback.
   - `send_email(to, subject, body)`: Gmail SMTP with App Password.
   - `send_whatsapp(contact_or_phone, message)`: WhatsApp dispatch with browser wa.me fallback.

4. **Safety Confirmation Guard**:
   - Sensitive actions (`send_email`, `send_whatsapp`) **never execute immediately**.
   - Held in a persistent `pending_actions` queue. Jarvis reads details back and prompts for explicit confirmation (`haan`, `yes`, `kar do` / `nahi`, `cancel`).

5. **Voice Pipeline**:
   - **TTS**: Microsoft `edge-tts` (default `ur-PK-AsadNeural` for Urdu, `en-US-GuyNeural` for English) with audio caching and sentence-level streaming.
   - **STT**: Browser Web Speech API or Groq Whisper (`whisper-large-v3-turbo`).
   - **Wake Word**: Optional background wake-word listener (triggered by "jarvis").

---

## 🚀 Quick Start (Windows)

### 1. Install Dependencies
```powershell
python -m pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and fill in your keys:
```powershell
Copy-Item .env.example .env
```
Ensure your `.env` contains:
```ini
GROQ_API_KEY=gsk_your_actual_key_here
GROQ_MODEL=openai/gpt-oss-120b
GROQ_FAST_MODEL=openai/gpt-oss-20b
PORT=8000
HOST=127.0.0.1
DATABASE_PATH=jarvis.db
WAKE_WORD_ENABLED=false
```

### 3. Run the Application
```powershell
python main.py
```
This launches the FastAPI server and automatically opens the 3D VRM interface in your browser at:
`http://localhost:8000/face/index.html`

### 4. Run Automated Tests
```powershell
python -m pytest tests/test_tools.py -v
```

---

## ⚙️ Manual Configuration Guide

### 1. Groq API Key
1. Go to [https://console.groq.com/keys](https://console.groq.com/keys).
2. Create an API Key and paste it into `.env` as `GROQ_API_KEY=gsk_...`.

### 2. Gmail App Password (for `send_email`)
To enable Jarvis to send emails via Gmail SMTP:
1. Go to your Google Account -> **Security**.
2. Enable **2-Step Verification**.
3. Under *2-Step Verification*, select **App passwords**.
4. Generate a 16-character app password for "Mail".
5. Set in `.env`:
   ```ini
   GMAIL_USER=your_email@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```

### 3. WhatsApp Web (for `send_whatsapp`)
- When requesting to send a WhatsApp message, Jarvis prepares the message and prompts for confirmation.
- Upon confirmation (`haan` / `yes`), it dispatches via `pywhatkit` or opens WhatsApp Web (`https://web.whatsapp.com`) with the pre-filled message and target number. Make sure you are logged in to WhatsApp Web in your default browser.

---

## 🗺️ Enterprise Roadmap (Future Milestones)

- [ ] **Multi-User & Enterprise Auth**: JWT authentication, RBAC (Role-Based Access Control), and per-user isolated memories.
- [ ] **Docker Deployment**: Multi-stage Dockerfile and Docker Compose orchestration for microservice deployment.
- [ ] **WhatsApp Cloud API**: Transition from local browser automation to Meta WhatsApp Business Cloud API.
- [ ] **Google Workspace Integration**: Native Google Calendar OAuth2 sync and Gmail API integration.
- [ ] **Vector Memory (ChromaDB / Milvus)**: Semantic search over past conversations, documents, and user preferences.
- [ ] **Dynamic Plugin Architecture**: Hot-reloadable plugins for custom enterprise tools and CRM connectors.
- [ ] **Automated CI/CD**: GitHub Actions workflows for automated testing, linting, security audits, and continuous deployment.

