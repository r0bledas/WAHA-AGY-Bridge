# WhatsApp Antigravity Bridge (`WAHA-AGY-Bridge`)

A lightweight, multi-threaded Python bridge that connects **WhatsApp** (via self-hosted [WAHA — WhatsApp HTTP API](https://waha.devlike.pro/)) to your machine's agents, allowing you to remotely control the machine, switch models and backends, and send/receive documents & images directly from WhatsApp.

Two agent backends, one bridge:

- **AGY** — Google Antigravity CLI (primary, `agy.exe`)
- **Hermes** - [Nous Research Hermes Agent](https://hermes-agent.nousresearch.com) headless CLI running on **DeepSeek** (alternative backend)

`/switchagent` cycles or selects the backend per chat. There is **no automatic router**: you choose the agent explicitly, and a quota/error just tells you to switch. The Hermes backend follows the **same `GEMINI.md` instructions and the same skills** as the AGY setup - they are synced into the Hermes profile before every run.

---

## Features

- **Strict Stealth Passphrase Authentication**:
  - Works in both self-chat ("Message Yourself") and external chats.
  - Unauthenticated chats are **100% silent** (zero feedback) unless the exact passphrase (`BRIDGE_PASSPHRASE`) is sent.
  - Automatic session lock after **10 minutes** of inactivity (`INACTIVITY_TIMEOUT_SECONDS`).
- **Multi-agent routing**:
  - `/switchagent` - cycle to the next agent; `/switchagent agy|hermes` to select; `/switchagent status` for health.
  - Per-chat persistence across bridge restarts (`agent_state.json`).
  - AGY quota detection with parsed reset cooldowns (informational; you switch manually).
  - Hermes sessions continue per chat (named sessions with a generation counter; `/clear` starts a fresh one).
- **Live Dynamic Model Discovery (`/model`)** for AGY:
  - Queries `agy.exe models` directly from the Google CLI with a 1-hour cache and `/model refresh` support.
- **Full Media & Document Support**:
  - Incoming PDFs, code files, documents, and images are automatically downloaded from WAHA and passed to the active agent.
  - **Multi-image batching**: several images/captions sent in a burst are debounced and processed as ONE turn instead of interrupting each other.
  - Files can be sent back to WhatsApp via the `send_file.py` helper / WAHA's `/api/sendFile` endpoint.
- **Adaptive watchdogs (no more premature cutoffs)**:
  - AGY inactivity kill raised to 600 s and suspended while a tool is running (`AGY_TOOL_TIMEOUT_SECONDS`).
  - Hard ceiling raised to 1800 s (`AGY_MAX_PROMPT_TIMEOUT_SECONDS`).
  - Sessions with an active turn are no longer expired by the 10-minute lock.
- **Cumulative-response deduplication**:
  - AGY's final `result.response` is diffed against already-sent intermediates; only the new part is sent.
  - Tool notifications include file/command detail and identical notes are coalesced (`×N`).
- **Continuous WhatsApp Typing Indicators** while an agent is processing.
- **Multi-Threaded & Deadlock-Free Execution**:
  - Built on `ThreadingHTTPServer` with instant `200 OK` webhook acknowledgment, FIFO message deduplication, and Windows process-tree cleanup (`taskkill /F /T`).
- **Automatic `GEMINI.md` + skills synchronization**:
  - Syncs `GEMINI.md` into the portable AGY home directory, and into the Hermes profile + workspace.
  - Copies the bridge's skills (`C:\waha\.agents\skills`) into the Hermes profile so both agents share them.
- **Passphrase never logged**: startup banner and inbound message logging redact `BRIDGE_PASSPHRASE`.

---

## WhatsApp Slash Commands (Once Unlocked)

| Command | Description |
| :--- | :--- |
| `/switchagent` | Cycle to the next agent (agy <-> hermes) |
| `/switchagent agy` | Select AGY |
| `/switchagent hermes` | Select Hermes (DeepSeek) |
| `/switchagent status` | Show current agent and backend health |
| `/model` | Show the active backend's models (AGY dynamic list or DeepSeek) |
| `/model <id>` | Switch model (e.g. `/model deepseek-v4-pro`) |
| `/model <id> <level>` | Switch model + thinking level (e.g. `/model deepseek-v4-pro high`) |
| `/model refresh` | Force refresh the AGY model list |
| `/reasoning` | Show the active model's thinking level (Hermes/DeepSeek) |
| `/reasoning <level>` | Set that model's thinking level: `none`, `low`, `high`, `max` (remembered per model; aliases: `/thinking`, `/effort`) |
| `/clear` | Clear chat context and start a fresh session for the active agent |
| `/status` | Session status, active agent/model, context state, auto-lock countdown |
| `/stop` | Stop the running AGY or Hermes turn |
| `/lock` | Immediately lock the session for the current chat |
| `/help` | Show available commands |

Aliases: `/agent`, `/harness` for `/switchagent`.

---

## Quick Start

1. **Configure Environment**:
   Copy `.env.example` to `.env` and fill in `WAHA_API_KEY`, `BRIDGE_PASSPHRASE`, and `AGY_DIR`:
   ```powershell
   Copy-Item .env.example .env
   ```

2. **Hermes backend (optional)**:
   Create the dedicated profile (CLI-only, no messaging channels):
   ```powershell
   hermes profile create wa --clone
   ```
   Ensure the profile has DeepSeek configured (inherited via `--clone`). Configure `HERMES_PROFILE`, `HERMES_WORKDIR`, and timeouts in `.env` if the defaults don't fit.

3. **Start WAHA Container**:
   ```powershell
   docker-compose up -d
   ```

4. **Start the Bridge**:
   ```powershell
   python -u agy_bridge.py
   ```
   Or double-click `start_bridge.bat`.

---

## Multi-agent configuration (`.env`)

```
DEFAULT_AGENT=agy                # initial agent (no auto router)
AGY_COOLDOWN_DEFAULT_HOURS=24
HERMES_ENABLED=true
HERMES_PROFILE=wa
HERMES_WORKDIR=C:\waha\workspace
HERMES_MODEL=                    # empty = profile default (deepseek-flash)
HERMES_IDLE_TIMEOUT_SECONDS=900
HERMES_MAX_RUNTIME_SECONDS=1800
HERMES_GEMINI_SOURCE=C:\waha\GEMINI.md
HERMES_SKILLS_SOURCE=C:\waha\.agents\skills
HERMES_COOLDOWN_DEFAULT_MINUTES=30
AGY_INACTIVITY_TIMEOUT_SECONDS=600
AGY_MAX_PROMPT_TIMEOUT_SECONDS=1800
AGY_TOOL_TIMEOUT_SECONDS=1800
```
