# WhatsApp Antigravity Bridge (`WAHA-AGY-Bridge`)

A lightweight, multi-threaded Python bridge that connects **WhatsApp** (via self-hosted [WAHA — WhatsApp HTTP API](https://waha.devlike.pro/)) to the **Google Antigravity CLI (`agy`)**, allowing you to remotely control your machine, switch models dynamically, and send/receive documents & images directly from WhatsApp.

---

## Features

- **Strict Stealth Passphrase Authentication**:
  - Works in both self-chat ("Message Yourself") and external chats.
  - Unauthenticated chats are **100% silent** (zero feedback) unless the exact passphrase (`BRIDGE_PASSPHRASE`) is sent.
  - Automatic session lock after **10 minutes** of inactivity (`INACTIVITY_TIMEOUT_SECONDS`).
- **Live Dynamic Model Discovery (`/model`)**:
  - Queries `agy.exe models` directly from the Google CLI with a 1-hour cache and `/model refresh` support.
- **Full Media & Document Support**:
  - Incoming PDFs, code files, documents, and images are automatically downloaded from WAHA and passed to `agy.exe` for inspection and analysis.
  - Can also send generated files/PDFs/images back to WhatsApp via WAHA's `/api/sendFile` endpoint.
- **Continuous WhatsApp Typing Indicators**:
  - Background heartbeat refreshes WhatsApp's `typing...` indicator every 2.5 seconds while AGY is processing.
- **Multi-Threaded & Deadlock-Free Execution**:
  - Built on `ThreadingHTTPServer` with instant `200 OK` webhook acknowledgment, FIFO message deduplication, file-backed subprocess output streams, and Windows process-tree cleanup (`taskkill /F /T`).
- **Automatic `GEMINI.md` Synchronization**:
  - Syncs `GEMINI.md` into the portable AGY home directory before execution so system instructions and host awareness persist across turns.

---

## WhatsApp Slash Commands (Once Unlocked)

| Command | Description |
| :--- | :--- |
| `/model` | List all live models available in AGY CLI and show active model |
| `/model <name>` | Switch active LLM model (e.g. `/model claude-sonnet-5-5-high`) |
| `/model refresh` | Force refresh the live model list from AGY CLI |
| `/clear` | Clear chat context and start a fresh AGY session |
| `/status` | Check session status, active model, context state, and auto-lock countdown |
| `/lock` | Immediately lock the session for the current chat |
| `/help` | Show available commands |

---

## Quick Start

1. **Configure Environment**:
   Copy `.env.example` to `.env` and fill in your `WAHA_API_KEY`, `BRIDGE_PASSPHRASE`, and `AGY_DIR`:
   ```powershell
   Copy-Item .env.example .env
   ```

2. **Start WAHA Container**:
   ```powershell
   docker-compose up -d
   ```

3. **Start the Bridge**:
   ```powershell
   python -u agy_bridge.py
   ```
   Or double-click `start_bridge.bat`.
