import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import requests

try:
    import hermes_backend
except Exception:
    hermes_backend = None

import re

# Reconfigure stdout/stderr for unbuffered UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# ==============================================================================
# Configuration & Environment Loading
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_dotenv_file(dotenv_path):
    if not os.path.exists(dotenv_path):
        return
    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass

load_dotenv_file(os.path.join(BASE_DIR, ".env"))

PORT = int(os.environ.get("BRIDGE_PORT", "5055"))
PASSPHRASE = os.environ.get("BRIDGE_PASSPHRASE", "CHANGE_ME_PASSPHRASE")
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("INACTIVITY_TIMEOUT_SECONDS", "600"))  # 10 minutes session lock
WAHA_URL = os.environ.get("WAHA_URL", "http://localhost:3001")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "YOUR_WAHA_API_KEY")
AGY_DIR = os.environ.get("AGY_DIR", r"C:\Users\T450\Downloads\AGY-Portable-v1.6.0-All-Platforms")
AGY_STREAMING_ENABLED = os.environ.get("AGY_STREAMING_ENABLED", "True").lower() == "true"
AGY_INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("AGY_INACTIVITY_TIMEOUT_SECONDS", "600"))
AGY_MAX_PROMPT_TIMEOUT_SECONDS = int(os.environ.get("AGY_MAX_PROMPT_TIMEOUT_SECONDS", "1800"))
AGY_TOOL_TIMEOUT_SECONDS = int(os.environ.get("AGY_TOOL_TIMEOUT_SECONDS", "1800"))
AGY_NOTIFY_TOOL_CALLS = os.environ.get("AGY_NOTIFY_TOOL_CALLS", "True").lower() == "true"

# ── Multi-agent routing (AGY primary, Hermes/DeepSeek fallback) ──
DEFAULT_AGENT = os.environ.get("DEFAULT_AGENT", "agy").strip().lower() or "agy"
AGENT_AUTO_FALLBACK = os.environ.get("AGENT_AUTO_FALLBACK", "true").strip().lower() == "true"
AGENT_STATE_FILE = os.path.join(BASE_DIR, "agent_state.json")
AGY_COOLDOWN_DEFAULT_HOURS = int(os.environ.get("AGY_COOLDOWN_DEFAULT_HOURS", "24"))
HERMES_COOLDOWN_DEFAULT_MINUTES = int(os.environ.get("HERMES_COOLDOWN_DEFAULT_MINUTES", "30"))

# DeepSeek models available to the Hermes backend (id, display label).
HERMES_MODELS = [
    ("deepseek-flash", "DeepSeek V4.1 Flash"),
    ("deepseek-v4-pro", "DeepSeek V4 Pro"),
]
PORTABLE_HOME = os.path.join(AGY_DIR, "data", "home")
PORTABLE_DATA = os.path.join(AGY_DIR, "data")
AGY_EXE = os.path.join(AGY_DIR, "Windows", "bin", "agy.exe")
AGY_DANGER_CMD = os.path.join(AGY_DIR, "Windows", "agy-danger.cmd")

def get_agy_env():
    env = os.environ.copy()
    env["USERPROFILE"] = PORTABLE_HOME
    env["HOME"] = PORTABLE_HOME
    env["HOMEDRIVE"] = PORTABLE_DATA[:2]
    env["HOMEPATH"] = PORTABLE_DATA[2:] + r"\home"
    env["APPDATA"] = os.path.join(PORTABLE_DATA, "AppData", "Roaming")
    env["LOCALAPPDATA"] = os.path.join(PORTABLE_DATA, "AppData", "Local")
    bin_dir = os.path.dirname(AGY_EXE)
    env["PATH"] = f"{bin_dir};{env.get('PATH', '')}"
    return env

SESSIONS_FILE = os.path.join(BASE_DIR, "authorized_sessions.json")
DOWNLOADS_DIR = os.path.join(BASE_DIR, "media", "downloads")
MODELS_CACHE_TTL = 3600  # 1 hour
DEFAULT_MODEL = "gemini-3.8-flash-medium"

# Bot signature prefixes to immediately discard from incoming webhooks
BOT_PREFIXES = ("🤖", "🔒", "🔓", "🟢", "⚠️", "🧹", "📋", "⏳", "⚙️", "🔄", "🔁")

# Self-account identifiers (loaded from .env + auto-populated via WAHA /api/sessions/default)
MY_ACCOUNTS = {
    acc.strip()
    for acc in os.environ.get("BRIDGE_MY_ACCOUNTS", "").split(",")
    if acc.strip()
}

# State tracking
active_sessions = {}                  # {session_key: last_active_timestamp}
user_models = {}                      # {session_key: model_name}
chat_history = {}                     # {session_key: bool (has_history)}
session_targets = {}                  # {session_key: target_chat}
bot_sent_ids = deque(maxlen=1000)     # Avoid bot reply feedback loops (FIFO)
bot_sent_texts = deque(maxlen=500)    # Avoid bot reply loops by content match (FIFO)
processed_msg_ids = deque(maxlen=1000)# Deduplicate incoming webhook deliveries (FIFO)

# ── Multi-agent state, media batching and tool-note coalescing ──
agent_state = {"chats": {}, "backends": {}}
media_batches = {}        # chat -> {"items": [(body, sender_name, media_dict)], "timer": Timer}
pending_tool_notes = {}   # chat -> {"text", "count", "ts", "timer"}
last_agy_stderr = []      # tail of most recent AGY stderr lines (for quota detection)
running_chats = set()                 # Prevent concurrent overlapping runs for the same chat
cached_models = []                    # [(model_id, label), ...]
last_models_fetch = 0
lock = threading.RLock()              # Reentrant lock to prevent self-deadlock on nested calls

# Active Turn tracking for instant preemption and interruption
class ActiveTurn:
    def __init__(self, session_key, target_chat, prompt_text):
        self.session_key = session_key
        self.target_chat = target_chat
        self.prompt_text = prompt_text
        self.proc = None
        self.typing_indicator = None
        self.is_interrupted = threading.Event()
        self.is_completed = threading.Event()
        self.lock = threading.Lock()

    def interrupt(self, reason="Interrupted by new prompt"):
        with self.lock:
            if self.is_completed.is_set():
                return
            self.is_interrupted.set()
            if self.typing_indicator:
                try:
                    self.typing_indicator.stop()
                except Exception:
                    pass
            if self.proc and self.proc.pid:
                kill_process_tree(self.proc.pid)

active_turns = {}  # {session_key: ActiveTurn}

def interrupt_active_turn(session_key, reason="Interrupted by user"):
    with lock:
        turn = active_turns.get(session_key)
        if turn and not turn.is_completed.is_set():
            turn.interrupt(reason=reason)
            return turn
    return None

def kill_process_tree(pid):
    """Forcefully kills a process and all its child processes on Windows."""
    if not pid:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10
        )
    except Exception:
        pass

def fetch_dynamic_models(force_refresh=False):
    """Dynamically queries agy.exe models with UTF-8 decoding."""
    global cached_models, last_models_fetch
    now = time.time()
    with lock:
        if cached_models and (now - last_models_fetch < MODELS_CACHE_TTL) and not force_refresh:
            return list(cached_models)

    print("[Bridge] Querying dynamic models directly from AGY CLI...", flush=True)
    try:
        cmd = [AGY_EXE, "--dangerously-skip-permissions", "models"] if os.path.exists(AGY_EXE) else [AGY_DANGER_CMD, "models"]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=get_agy_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=20
        )
        found = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if "\t" in line:
                parts = line.split("\t", 1)
                m_id = parts[0].strip()
                m_label = parts[1].strip()
                if m_id and m_label:
                    found.append((m_id, m_label))

        if found:
            with lock:
                cached_models = found
                last_models_fetch = now
            print(f"[Bridge] Loaded {len(found)} live models from AGY CLI.", flush=True)
            return list(found)
    except Exception as e:
        print(f"[Bridge] Error fetching dynamic models from AGY: {e}", flush=True)

    with lock:
        if not cached_models:
            cached_models = [
                ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
                ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
                ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
                ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
                ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
            ]
        return list(cached_models)

def refresh_my_accounts():
    global MY_ACCOUNTS
    try:
        r = requests.get(f"{WAHA_URL}/api/sessions/default", headers={"X-Api-Key": WAHA_API_KEY}, timeout=5)
        if r.status_code == 200:
            me = r.json().get("me", {})
            with lock:
                if me.get("id"):
                    MY_ACCOUNTS.add(me["id"])
                if me.get("lid"):
                    MY_ACCOUNTS.add(me["lid"])
    except Exception as e:
        print(f"[Bridge] Error fetching my accounts: {e}", flush=True)

def canonical_chat_id(cid):
    if cid in MY_ACCOUNTS:
        return "self"
    return cid

def get_target_chat_for_session(session_key):
    with lock:
        target = session_targets.get(session_key)
        if not target and session_key == "self":
            target = next((acc for acc in MY_ACCOUNTS if acc.endswith("@c.us")), None) or "5218135537757@c.us"
        elif not target:
            target = session_key
        return target

def load_sessions():
    global active_sessions, user_models, chat_history, session_targets
    if os.path.exists(SESSIONS_FILE):
        try:
            with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            with lock:
                if "sessions" in data:
                    active_sessions = {k: float(v) for k, v in data.get("sessions", {}).items()}
                    user_models = data.get("models", {})
                    chat_history = data.get("history", {})
                    session_targets = data.get("targets", {})
                else:
                    active_sessions = {k: float(v) for k, v in data.items()}
                print(f"[Bridge] Loaded {len(active_sessions)} saved sessions.", flush=True)
        except Exception as e:
            print(f"[Bridge] Warning loading sessions: {e}", flush=True)

def save_sessions():
    try:
        with lock:
            payload = {
                "sessions": dict(active_sessions),
                "models": dict(user_models),
                "history": dict(chat_history),
                "targets": dict(session_targets)
            }
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        print(f"[Bridge] Warning saving sessions: {e}", flush=True)


# ══════════════════════════════════════════════════════════════════
#  MULTI-AGENT ROUTING (AGY ↔ HERMES) AND RELATED HELPERS
# ══════════════════════════════════════════════════════════════════

def load_agent_state():
    global agent_state
    try:
        if os.path.exists(AGENT_STATE_FILE):
            with open(AGENT_STATE_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                agent_state = loaded
    except Exception as e:
        print(f"[Bridge] Warning loading agent state: {e}", flush=True)
    agent_state.setdefault("chats", {})
    agent_state.setdefault("backends", {})


def save_agent_state():
    try:
        tmp_path = AGENT_STATE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(agent_state, f, indent=2)
        os.replace(tmp_path, AGENT_STATE_FILE)
    except Exception as e:
        print(f"[Bridge] Warning saving agent state: {e}", flush=True)


def get_chat_agent(session_key):
    with lock:
        chat = agent_state["chats"].setdefault(session_key, {})
        chat.setdefault("pinned", None)
        chat.setdefault("generation", 1)
        return dict(chat)


def backend_on_cooldown(name):
    cd = agent_state["backends"].get(name, {}).get("cooldown_until", 0) or 0
    return time.time() < cd


def set_backend_cooldown(name, seconds):
    with lock:
        entry = agent_state["backends"].setdefault(name, {})
        entry["cooldown_until"] = time.time() + seconds
        save_agent_state()


def parse_reset_seconds(text):
    m = re.search(r"resets?\s+in\s+(\d+)\s*h\s*(\d+)\s*m", str(text), re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    m = re.search(r"(\d+)\s*(?:h|hr|hour)s?", str(text), re.IGNORECASE)
    if m:
        return int(m.group(1)) * 3600
    m = re.search(r"(\d+)\s*(?:m|min|minute)s?", str(text), re.IGNORECASE)
    if m:
        return int(m.group(1)) * 60
    return None


def agy_quota_hit(reply_text):
    blob = " ".join([str(reply_text or "")] + list(last_agy_stderr[-8:]))
    return bool(re.search(r"RESOURCE_EXHAUSTED|Individual quota reached|429", blob, re.IGNORECASE))


def hermes_available():
    if hermes_backend is None:
        return False, "hermes backend module missing"
    return hermes_backend.available()


def resolve_agent(session_key):
    """Return (agent_name, mode)."""
    chat = get_chat_agent(session_key)
    pinned = chat.get("pinned")
    if pinned in ("agy", "hermes"):
        return pinned, "pinned"
    mode = "auto" if AGENT_AUTO_FALLBACK else "manual"
    if backend_on_cooldown("agy"):
        return "hermes", mode
    return (DEFAULT_AGENT if DEFAULT_AGENT in ("agy", "hermes") else "agy"), mode


def switch_agent(session_key, target_chat, arg):
    arg = (arg or "").strip().lower()
    if arg == "":
        agent, mode = resolve_agent(session_key)
        lines = [
            f"*Active agent:* {agent} ({mode})",
            "Available: agy, hermes",
        ]
        if backend_on_cooldown("agy"):
            cd = agent_state["backends"]["agy"].get("cooldown_until", 0)
            lines.append("AGY: cooling down until " + time.strftime("%Y-%m-%d %H:%M", time.localtime(cd)))
        else:
            lines.append("AGY: OK")
        ok, why = hermes_available()
        lines.append("Hermes: " + ("OK" if ok else why))
        send_whatsapp_message(target_chat, "🔄 *Agent Status*\n" + "\n".join(lines))
        return
    if arg == "auto":
        with lock:
            agent_state["chats"].setdefault(session_key, {})["pinned"] = None
            save_agent_state()
        send_whatsapp_message(
            target_chat,
            "🔄 *Auto mode.*\nAGY is primary; Hermes (DeepSeek) takes over on quota/outage.\nUse /switchagent agy or /switchagent hermes to pin one.",
        )
        return
    if arg not in ("agy", "hermes"):
        send_whatsapp_message(
            target_chat,
            "🔄 Usage:\n/switchagent - status\n/switchagent agy\n/switchagent hermes\n/switchagent auto",
        )
        return
    if arg == "hermes":
        ok, why = hermes_available()
        if not ok:
            send_whatsapp_message(target_chat, f"⚠️ Hermes is not available: {why}")
            return
    with lock:
        agent_state["chats"].setdefault(session_key, {})["pinned"] = arg
        save_agent_state()
    label = "AGY" if arg == "agy" else "Hermes (DeepSeek)"
    send_whatsapp_message(
        target_chat,
        f"🔄 Switched this chat to *{label}*.\nNext message uses {label}. Use /switchagent auto for fallback mode.",
    )


def get_hermes_model(session_key):
    chat = get_chat_agent(session_key)
    return chat.get("hermes_model") or os.environ.get("HERMES_MODEL", "").strip()


def format_hermes_models(session_key):
    active = get_hermes_model(session_key) or "deepseek-flash"
    lines = [
        "*DeepSeek Models (Hermes backend)*",
        f"Currently Active: `{active}`",
        "",
    ]
    for m_id, label in HERMES_MODELS:
        marker = "👉 " if m_id == active else "  "
        suffix = " _[ACTIVE]_" if m_id == active else ""
        lines.append(f"{marker}`{m_id}` - {label}{suffix}")
    lines += [
        "",
        "💡 *To switch model:*",
        "Send: `/model <model-id>`",
        "Example: `/model deepseek-v4-pro`",
    ]
    return "\n".join(lines)


def run_hermes_turn(prompt, target_chat, chat_key, generation):
    """Run one Hermes turn; tool events are surfaced as coalesced tool notes."""
    if hermes_backend is None:
        return {"text": None, "error": "hermes backend module missing", "code": "unavailable"}

    model = get_hermes_model(chat_key)

    def on_event(ev):
        if ev.get("type") == "tool":
            send_tool_note(target_chat, "Ejecutando: " + str(ev.get("name", "tool")))

    return hermes_backend.run(prompt, chat_key, generation, model=model, on_event=on_event)


def stop_hermes(chat_key, generation):
    if hermes_backend is not None:
        try:
            hermes_backend.stop(chat_key, generation)
        except Exception:
            pass


def _run_agy_turn(prompt, target_chat, session_key, model, continue_session, turn, typing_indicator):
    """Run one AGY turn (streaming or batch). Returns the reply text."""
    last_agy_stderr.clear()
    if AGY_STREAMING_ENABLED:
        return stream_agy_prompt(prompt, target_chat, model=model, continue_session=continue_session, turn=turn)
    reply = run_agy_prompt(prompt, model=model, continue_session=continue_session, turn=turn)
    if not turn.is_interrupted.is_set():
        send_whatsapp_message(target_chat, f"🤖 AGY:\n{reply}")
    return reply


def send_tool_note(chat_id, text):
    """Send a tool progress note, coalescing identical notes within 1.5s."""
    now = time.time()
    with lock:
        cur = pending_tool_notes.get(chat_id)
        if cur and cur["text"] == text and (now - cur["ts"]) < 1.5:
            cur["count"] += 1
            cur["ts"] = now
            if cur["timer"]:
                cur["timer"].cancel()
            t = threading.Timer(1.2, flush_tool_note, args=(chat_id,))
            t.daemon = True
            cur["timer"] = t
            t.start()
            return
        flush_tool_note(chat_id)
        t = threading.Timer(1.2, flush_tool_note, args=(chat_id,))
        t.daemon = True
        pending_tool_notes[chat_id] = {"text": text, "count": 1, "ts": now, "timer": t}
        t.start()


def flush_tool_note(chat_id):
    with lock:
        cur = pending_tool_notes.pop(chat_id, None)
    if not cur:
        return
    label = cur["text"]
    if cur["count"] > 1:
        label += f" ×{cur['count']}"
    send_whatsapp_message(chat_id, f"⚙️ {label}")


def flush_all_tool_notes():
    for chat_id in list(pending_tool_notes.keys()):
        flush_tool_note(chat_id)


# ── Media message batching (multi-image = one turn) ──

def dispatch_message(target_chat, body, sender_name, media_dict=None):
    """Route an incoming message: media messages are debounced/batched."""
    if media_dict:
        with lock:
            batch = media_batches.setdefault(target_chat, {"items": [], "timer": None})
            batch["items"].append((body, sender_name, media_dict))
            if batch["timer"] is not None:
                batch["timer"].cancel()
            t = threading.Timer(1.8, flush_media_batch, args=(target_chat,))
            t.daemon = True
            batch["timer"] = t
            t.start()
        return
    flush_media_batch(target_chat)
    process_message(target_chat, body, sender_name, None)


def flush_media_batch(target_chat):
    with lock:
        batch = media_batches.pop(target_chat, None)
    if not batch or not batch["items"]:
        return
    items = batch["items"]
    sender_name = items[0][1] or "User"
    bodies = [b.strip() for b, _sn, _m in items if b and str(b).strip()]
    combined = " ".join(bodies)
    media_paths = []
    for _b, _sn, media_dict in items:
        try:
            saved_path, _mime = download_media_file(media_dict)
            if saved_path:
                media_paths.append(saved_path)
        except Exception:
            pass
    if not media_paths and not combined:
        return
    process_message(target_chat, combined, sender_name, None, media_paths=media_paths)


# Load persisted per-chat agent selection at import time.
load_agent_state()


def chunk_message(text, max_chars=3500):
    """Splits long messages at paragraph or sentence breaks without altering markdown."""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        split_idx = remaining.rfind("\n\n", 0, max_chars)
        if split_idx == -1 or split_idx < max_chars // 3:
            split_idx = remaining.rfind("\n", 0, max_chars)
        if split_idx == -1 or split_idx < max_chars // 3:
            split_idx = remaining.rfind(". ", 0, max_chars)
            if split_idx != -1:
                split_idx += 1
        if split_idx == -1 or split_idx < max_chars // 3:
            split_idx = max_chars

        chunk = remaining[:split_idx].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_idx:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks

def _send_whatsapp_single_message(chat_id, text):
    stripped = text.strip()
    if stripped:
        with lock:
            if stripped not in bot_sent_texts:
                bot_sent_texts.append(stripped)

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": WAHA_API_KEY
    }
    payload = {
        "chatId": chat_id,
        "text": text,
        "session": "default"
    }
    try:
        r = requests.post(f"{WAHA_URL}/api/sendText", json=payload, headers=headers, timeout=20)
        if r.status_code in [200, 201]:
            data = r.json()
            raw_id = data.get("id")
            with lock:
                if isinstance(raw_id, dict):
                    if raw_id.get("_serialized"):
                        bot_sent_ids.append(str(raw_id.get("_serialized")))
                    if raw_id.get("id"):
                        bot_sent_ids.append(str(raw_id.get("id")))
                elif raw_id:
                    bot_sent_ids.append(str(raw_id))
            print(f"[Bridge] Sent reply to {chat_id} ({len(text)} chars)", flush=True)
        else:
            print(f"[Bridge] Error sending message to {chat_id}: {r.status_code} {r.text}", flush=True)
    except Exception as e:
        print(f"[Bridge] Exception sending message: {e}", flush=True)

def send_whatsapp_message(chat_id, text):
    chunks = chunk_message(text, max_chars=3500)
    for i, chunk in enumerate(chunks):
        _send_whatsapp_single_message(chat_id, chunk)
        if i < len(chunks) - 1:
            time.sleep(0.35)

def session_inactivity_watchdog():
    """
    Background daemon thread that periodically checks for inactive sessions.
    When a session exceeds INACTIVITY_TIMEOUT_SECONDS without messages,
    it automatically closes the session and sends an expiration notification to WhatsApp.
    """
    print(f"[Bridge] Session inactivity watchdog started (timeout: {INACTIVITY_TIMEOUT_SECONDS}s)", flush=True)
    while True:
        try:
            time.sleep(10)
            now = time.time()
            expired_sessions = []
            with lock:
                for s_key, last_active in list(active_sessions.items()):
                    if s_key in running_chats:
                        # Keep the session alive while an agent turn is running.
                        active_sessions[s_key] = now
                        continue
                    if now - last_active >= INACTIVITY_TIMEOUT_SECONDS:
                        expired_sessions.append(s_key)
                        del active_sessions[s_key]
                if expired_sessions:
                    save_sessions()

            for s_key in expired_sessions:
                print(f"[Bridge] Session expired due to inactivity for {s_key}", flush=True)
                target = get_target_chat_for_session(s_key)
                if target:
                    msg = (
                        "🔒 *Session Expired (10m Inactivity)*\n\n"
                        "Your AGY session has automatically ended due to inactivity.\n"
                        "Send the passphrase to start a new session."
                    )
                    send_whatsapp_message(target, msg)
        except Exception as e:
            print(f"[Bridge] Watchdog exception: {e}", flush=True)

def send_presence_online(http_session=None):
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": WAHA_API_KEY
    }
    req = http_session or requests
    try:
        req.post(f"{WAHA_URL}/api/default/presence", json={"presence": "online"}, headers=headers, timeout=4)
    except Exception:
        pass

def send_seen(chat_id, http_session=None):
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": WAHA_API_KEY
    }
    payload = {
        "chatId": chat_id,
        "session": "default"
    }
    req = http_session or requests
    try:
        req.post(f"{WAHA_URL}/api/sendSeen", json=payload, headers=headers, timeout=4)
    except Exception:
        pass

def send_typing_indicator(chat_id, http_session=None):
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": WAHA_API_KEY
    }
    payload = {
        "chatId": chat_id,
        "session": "default"
    }
    req = http_session or requests
    try:
        req.post(f"{WAHA_URL}/api/startTyping", json=payload, headers=headers, timeout=4)
    except Exception:
        pass

def stop_typing_indicator(chat_id, http_session=None):
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": WAHA_API_KEY
    }
    payload = {
        "chatId": chat_id,
        "session": "default"
    }
    req = http_session or requests
    try:
        req.post(f"{WAHA_URL}/api/stopTyping", json=payload, headers=headers, timeout=4)
    except Exception:
        pass

class ContinuousTyping:
    """
    Continuously refreshes WhatsApp typing indicator while AGY is processing.
    WhatsApp Web (WAWebChatStateBridge) throttles consecutive `sendChatStateComposing`
    calls for 25s while mobile clients hide the indicator after ~10s.
    By briefly pulsing `stopTyping` -> `startTyping` every 4s and maintaining `online`
    presence, WhatsApp Web is forced to transmit a fresh `<composing/>` packet every cycle.
    """
    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _get_target_ids(self):
        targets = [self.chat_id]
        if self.chat_id in MY_ACCOUNTS:
            for alt_id in list(MY_ACCOUNTS):
                if alt_id != self.chat_id:
                    targets.append(alt_id)
        return targets

    def _run(self):
        with requests.Session() as sess:
            targets = self._get_target_ids()
            send_presence_online(sess)
            for cid in targets:
                send_seen(cid, sess)

            tick = 0
            while not self.stop_event.is_set():
                targets = self._get_target_ids()
                if tick > 0:
                    # Pulse paused -> composing so WhatsApp Web bypasses its 25s internal throttle
                    send_presence_online(sess)
                    for cid in targets:
                        stop_typing_indicator(cid, sess)
                    if self.stop_event.wait(0.15):
                        break
                for cid in targets:
                    send_typing_indicator(cid, sess)
                tick += 1
                # Wait 4 seconds before the next state-refresh pulse
                self.stop_event.wait(4.0)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with requests.Session() as sess:
            for cid in self._get_target_ids():
                stop_typing_indicator(cid, sess)

def download_media_file(media_dict):
    """Downloads media from WAHA into C:\\waha\\media\\downloads."""
    url = media_dict.get("url")
    if not url:
        return None, None

    url = url.replace("localhost:3000", "localhost:3001").replace("127.0.0.1:3000", "127.0.0.1:3001")
    if url.startswith("/api/"):
        url = f"{WAHA_URL}{url}"

    filename = media_dict.get("filename")
    mimetype = media_dict.get("mimetype", "application/octet-stream")
    ext = mimetype.split("/")[-1].split(";")[0] if "/" in mimetype else "bin"
    if ext == "plain":
        ext = "txt"
    elif ext == "jpeg":
        ext = "jpg"

    if not filename:
        filename = f"media_{int(time.time() * 1000)}.{ext}"

    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    clean_filename = "".join([c for c in filename if c.isalnum() or c in "._- "]).strip(" .")
    if not clean_filename:
        clean_filename = f"media_{int(time.time() * 1000)}.{ext}"
    save_path = os.path.join(DOWNLOADS_DIR, clean_filename)

    headers = {"X-Api-Key": WAHA_API_KEY}
    try:
        print(f"[Bridge] Downloading media from {url} to {save_path}...", flush=True)
        r = requests.get(url, headers=headers, timeout=45)
        if r.status_code == 200:
            with open(save_path, "wb") as f:
                f.write(r.content)
            print(f"[Bridge] Downloaded {len(r.content)} bytes -> {save_path}", flush=True)
            return save_path, mimetype
        else:
            print(f"[Bridge] Failed to download media: {r.status_code} {r.text}", flush=True)
    except Exception as e:
        print(f"[Bridge] Error downloading media from {url}: {e}", flush=True)
    return None, None

GEMINI_MD_PATH = os.path.join(BASE_DIR, "GEMINI.md")

def sync_gemini_md():
    """Ensures GEMINI.md in the bridge directory is synced to the portable AGY home and .gemini directories."""
    if not os.path.exists(GEMINI_MD_PATH):
        return
    try:
        with open(GEMINI_MD_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        targets = [
            os.path.join(PORTABLE_HOME, "GEMINI.md"),
            os.path.join(PORTABLE_HOME, ".gemini", "GEMINI.md"),
        ]
        for t in targets:
            os.makedirs(os.path.dirname(t), exist_ok=True)
            with open(t, "w", encoding="utf-8") as out_f:
                out_f.write(content)
    except Exception as e:
        print(f"[Bridge] Warning syncing GEMINI.md: {e}", flush=True)

def run_agy_prompt(prompt, model=None, continue_session=False, turn=None):
    """Executes agy.exe directly using file-backed stdout/stderr to avoid Windows pipe inheritance hangs."""
    proc = None
    try:
        sync_gemini_md()
        if os.path.exists(AGY_EXE):
            cmd = [AGY_EXE, "--dangerously-skip-permissions"]
        else:
            cmd = [AGY_DANGER_CMD]

        if model:
            cmd.extend(["--model", model])
        if continue_session:
            cmd.append("--continue")
        cmd.extend(["-p", prompt])

        print(f"[Bridge] Command: {os.path.basename(cmd[0])} {'--model ' + model if model else ''} {'--continue' if continue_session else ''} -p ...", flush=True)

        with tempfile.TemporaryFile(mode="w+b") as out_f, tempfile.TemporaryFile(mode="w+b") as err_f:
            proc = subprocess.Popen(
                cmd,
                cwd=BASE_DIR,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                shell=False,
                env=get_agy_env(),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            if turn:
                turn.proc = proc
            try:
                proc.wait(timeout=AGY_MAX_PROMPT_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                print(f"[Bridge] AGY process {proc.pid} timed out after {AGY_MAX_PROMPT_TIMEOUT_SECONDS}s, killing process tree...", flush=True)
                kill_process_tree(proc.pid)
                return f"⚠️ AGY execution timed out after {AGY_MAX_PROMPT_TIMEOUT_SECONDS // 60} minutes."

            # Ensure any lingering grandchild processes are cleaned up
            kill_process_tree(proc.pid)

            if turn and turn.is_interrupted.is_set():
                return "⚠️ Interrupted by new prompt"

            out_f.seek(0)
            err_f.seek(0)
            raw_output = out_f.read().decode("utf-8", errors="replace")
            raw_err = err_f.read().decode("utf-8", errors="replace")

        # Clean loading artifacts if any
        cleaned_lines = []
        for line in raw_output.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith("[Preparing Runner]") or
                stripped.startswith("Reading from USB") or
                stripped.startswith("- Progress:") or
                stripped.startswith("[Ready]") or
                stripped.startswith("===") or
                stripped.startswith("Google Antigravity")
            ):
                continue
            cleaned_lines.append(line)

        clean_text = "\n".join(cleaned_lines).strip()
        if not clean_text and raw_err.strip():
            clean_text = f"AGY Error: {raw_err.strip()}"
        return clean_text or "(No response returned by AGY)"
    except Exception as e:
        if proc and proc.pid:
            kill_process_tree(proc.pid)
        return f"⚠️ Failed to execute AGY: {str(e)}"

def _final_delta(final_norm, sent_norm):
    """Return the portion of final_norm not already sent ("" if fully covered)."""
    if not final_norm:
        return ""
    if final_norm in sent_norm:
        return ""
    joined = " ".join(sent_norm)
    if joined and final_norm.startswith(joined):
        return final_norm[len(joined):].strip()
    max_end = 0
    for s in sent_norm:
        if len(s) >= 40 and s in final_norm:
            idx = final_norm.rfind(s)
            if idx >= 0:
                max_end = max(max_end, idx + len(s))
    if max_end > 0:
        return final_norm[max_end:].strip()
    return final_norm


def stream_agy_prompt(prompt, target_chat, model=None, continue_session=False, turn=None):
    """
    Executes agy.exe with --output-format stream-json, progressively
    parsing NDJSON events and dispatching separated responses to WhatsApp.
    """
    sync_gemini_md()

    if os.path.exists(AGY_EXE):
        cmd = [AGY_EXE, "--dangerously-skip-permissions", "--output-format", "stream-json"]
    else:
        cmd = [AGY_DANGER_CMD, "--output-format", "stream-json"]

    if model:
        cmd.extend(["--model", model])
    if continue_session:
        cmd.append("--continue")
    cmd.extend(["-p", prompt])

    print(f"[Bridge-Stream] Spawning: {' '.join(cmd[:5])} -p ...", flush=True)

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=get_agy_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        if turn:
            turn.proc = proc
    except Exception as e:
        print(f"[Bridge-Stream] Failed to spawn AGY process: {e}", flush=True)
        return f"⚠️ Failed to spawn AGY: {e}"

    line_queue = queue.Queue()
    stderr_lines = []

    def stdout_reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                line_queue.put(("STDOUT", line))
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def stderr_reader():
        try:
            for line in iter(proc.stderr.readline, ""):
                line_queue.put(("STDERR", line))
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    t_out = threading.Thread(target=stdout_reader, daemon=True)
    t_err = threading.Thread(target=stderr_reader, daemon=True)
    t_out.start()
    t_err.start()

    start_time = time.time()
    last_activity = time.time()

    inactivity_timeout = AGY_INACTIVITY_TIMEOUT_SECONDS
    tool_timeout = AGY_TOOL_TIMEOUT_SECONDS
    max_timeout = AGY_MAX_PROMPT_TIMEOUT_SECONDS
    notify_tools = AGY_NOTIFY_TOOL_CALLS

    current_response_buffer = []
    sent_norm = []
    final_response = None
    has_sent_intermediate = False
    tool_active = False
    tool_started = 0.0

    while True:
        if turn and turn.is_interrupted.is_set():
            print(f"[Bridge-Stream] Turn interrupted. Aborting stream...", flush=True)
            kill_process_tree(proc.pid)
            return "⚠️ Interrupted by new prompt"

        now = time.time()

        # Check Watchdogs
        if tool_active:
            if now - tool_started > tool_timeout:
                print(f"[Bridge-Stream] Tool exceeded {tool_timeout}s. Killing...", flush=True)
                kill_process_tree(proc.pid)
                send_whatsapp_message(target_chat, f"⚠️ La herramienta de AGY supero {tool_timeout // 60} minutos y fue cancelada.")
                return "⚠️ Timed out during tool execution"
        elif now - last_activity > inactivity_timeout:
            print(f"[Bridge-Stream] Process hung (no output for {inactivity_timeout}s). Killing...", flush=True)
            kill_process_tree(proc.pid)
            send_whatsapp_message(target_chat, f"⚠️ AGY se congelo ({inactivity_timeout}s sin actividad) y fue cancelado.")
            return "⚠️ Timed out due to inactivity"

        if now - start_time > max_timeout:
            print(f"[Bridge-Stream] Process exceeded maximum ceiling of {max_timeout}s. Killing...", flush=True)
            kill_process_tree(proc.pid)
            send_whatsapp_message(target_chat, f"⚠️ La tarea supero el tiempo maximo de {max_timeout // 60} minutos.")
            return "⚠️ Exceeded max timeout"

        try:
            stream_type, line = line_queue.get(timeout=0.25)
        except queue.Empty:
            if proc.poll() is not None and line_queue.empty():
                break
            continue

        if turn and turn.is_interrupted.is_set():
            kill_process_tree(proc.pid)
            return "⚠️ Interrupted by new prompt"

        last_activity = time.time()
        line = line.strip()
        if not line:
            continue

        if stream_type == "STDERR":
            stderr_lines.append(line)
            last_agy_stderr.append(line)
            if len(last_agy_stderr) > 40:
                last_agy_stderr.pop(0)
            continue

        if stream_type == "STDOUT":
            try:
                data = json.loads(line)
            except Exception:
                continue

            event = data.get("event")

            # 1. Capture Agent Response Text Deltas
            if event == "step_update":
                step = data.get("step_update", {})
                step_type = step.get("step_type")
                state = step.get("state")

                if step_type == "agent_response":
                    delta = step.get("text_delta", "")
                    if delta:
                        current_response_buffer.append(delta)
                        tool_active = False

                    # When an agent conversational turn finishes
                    if state == "DONE":
                        chunk = "".join(current_response_buffer).strip()
                        current_response_buffer = []
                        chunk_norm = " ".join(chunk.split())
                        if chunk_norm and chunk_norm not in sent_norm:
                            sent_norm.append(chunk_norm)
                            has_sent_intermediate = True
                            send_whatsapp_message(target_chat, f"🤖 AGY:\n{chunk}")

                # 2. Tool Invocation Notifications
                elif step_type == "tool" and notify_tools:
                    if state == "ACTIVE":
                        tool_active = True
                        tool_started = now
                        last_activity = now
                    else:
                        tool_active = False
                        last_activity = now

                    tool_name = step.get("tool_name", "herramienta")
                    tool_info = step.get("tool_info", {}) or {}
                    params = tool_info.get("parameters", {}) or {}

                    def _tool_hint(src):
                        for key in ("CommandLine", "TargetFile", "AbsolutePath", "file_path", "path", "query", "file"):
                            if key in src:
                                v = str(src[key]).strip().replace("\r", " ").replace("\n", " ")
                                if v:
                                    return v
                        return ""

                    raw_hint = _tool_hint(params) or _tool_hint(tool_info)
                    param_hint = ""
                    if raw_hint:
                        param_hint = f" ({raw_hint[:40]}...)" if len(raw_hint) > 40 else f" ({raw_hint})"

                    if state == "ACTIVE":
                        send_tool_note(target_chat, f"Ejecutando: {tool_name}{param_hint}")

            # 3. Final Result Event
            elif event == "result":
                res = data.get("result", {})
                final_response = res.get("response", "").strip()

    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    kill_process_tree(proc.pid)

    if turn and turn.is_interrupted.is_set():
        return "⚠️ Interrupted by new prompt"

    flush_tool_note(target_chat)

    # If any remaining text in buffer
    if current_response_buffer:
        leftover = "".join(current_response_buffer).strip()
        leftover_norm = " ".join(leftover.split())
        if leftover_norm and leftover_norm not in sent_norm:
            sent_norm.append(leftover_norm)
            send_whatsapp_message(target_chat, f"🤖 AGY:\n{leftover}")

    # If final response exists, send only the part not already delivered.
    if final_response:
        clean_final = final_response.strip()
        final_norm = " ".join(clean_final.split())
        if final_norm:
            delta_norm = _final_delta(final_norm, sent_norm)
            if delta_norm and delta_norm not in sent_norm:
                sent_norm.append(delta_norm)
                send_whatsapp_message(target_chat, f"🤖 AGY:\n{delta_norm}")
        return clean_final or "OK"
    elif not has_sent_intermediate and not sent_norm:
        if stderr_lines:
            err_msg = "\n".join(stderr_lines[-5:])
            send_whatsapp_message(target_chat, f"⚠️ Error en AGY: {err_msg}")
            return f"⚠️ Error: {err_msg}"
        send_whatsapp_message(target_chat, "🤖 AGY termino la ejecucion sin generar salida de texto.")
        return "(No output generated)"
    return "OK"

def format_model_list(current_model, force_refresh=False):
    models = fetch_dynamic_models(force_refresh=force_refresh)

    gemini_models = []
    claude_models = []
    other_models = []

    for m_id, label in models:
        item = f"`{m_id}` - {label}"
        if m_id == current_model:
            item = f"👉 *{label}* (`{m_id}`) _[ACTIVE]_"

        lower_id = m_id.lower()
        if "gemini" in lower_id:
            gemini_models.append(item)
        elif "claude" in lower_id:
            claude_models.append(item)
        else:
            other_models.append(item)

    lines = [
        "🤖 *Live AGY Models (Direct from Google CLI)*",
        f"Currently Active: `{current_model}`",
        ""
    ]
    if gemini_models:
        lines.append("• *Google Gemini:*")
        lines.extend([f"  {m}" for m in gemini_models])
    if claude_models:
        lines.append("• *Anthropic Claude:*")
        lines.extend([f"  {m}" for m in claude_models])
    if other_models:
        lines.append("• *Other Models:*")
        lines.extend([f"  {m}" for m in other_models])

    lines.extend([
        "",
        "💡 *To switch model:*",
        "Send: `/model <model-name>`",
        "Example: `/model claude-sonnet-5-5-high`",
        "",
        "🔄 *To refresh live list from Google:*",
        "Send: `/model refresh`"
    ])
    return "\n".join(lines)

def handle_tunnel_restart(target_chat):
    """
    Handles remote hard restart of the AGY bridge tunnel.
    Can be invoked via '///restart' from WhatsApp to recover if AGY hangs.
    """
    print(f"[Bridge] Remote hard restart requested by {target_chat}!", flush=True)
    send_whatsapp_message(
        target_chat,
        "🔄 *Restarting AGY Tunnel...*\n\n"
        "• Terminating active AGY tasks & child processes\n"
        "• Clearing locks and resetting bridge state\n"
        "• Rebooting bridge service in 3 seconds..."
    )

    # Save marker file so the new instance notifies the chat upon startup
    try:
        marker_file = os.path.join(BASE_DIR, ".restart_notify")
        with open(marker_file, "w", encoding="utf-8") as f:
            json.dump({"chat_id": target_chat, "timestamp": time.time()}, f)
    except Exception as e:
        print(f"[Bridge] Warning writing restart marker: {e}", flush=True)

    # Kill any active agy.exe processes immediately to unhang CLI
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "agy.exe", "/T"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=5
        )
    except Exception:
        pass

    # Launch detached restart worker via WMI so it outlives this process
    restart_script = os.path.join(BASE_DIR, "restart_tunnel_worker.ps1")
    ps_content = (
        "Start-Sleep -Seconds 3\n"
        "$pids = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*agy_bridge.py*' -and $_.ProcessId -ne $PID } | Select-Object -ExpandProperty ProcessId\n"
        "foreach ($p in $pids) {\n"
        "    try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}\n"
        "}\n"
        "Start-Sleep -Seconds 1\n"
        "Start-Process -FilePath 'C:\\Program Files\\Python311\\python.exe' -ArgumentList '-u C:\\waha\\agy_bridge.py' -WorkingDirectory 'C:\\waha' -WindowStyle Hidden -RedirectStandardOutput 'C:\\waha\\bridge_stdout.log' -RedirectStandardError 'C:\\waha\\bridge_stderr.log'\n"
    )
    try:
        with open(restart_script, "w", encoding="utf-8") as f:
            f.write(ps_content)

        wmi_cmd = f'powershell.exe -ExecutionPolicy Bypass -NoProfile -File "{restart_script}"'
        subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{wmi_cmd}'}}"
            ],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10
        )
    except Exception as e:
        print(f"[Bridge] Exception triggering WMI restart: {e}", flush=True)

def process_message(target_chat, body, sender_name, media_dict=None, media_paths=None):
    session_key = canonical_chat_id(target_chat)
    now = time.time()
    body_clean = body.strip()

    # Step 0: Remote Hard Restart Command ("///restart")
    # Allows recovering the tunnel remotely even if a task hangs or session expires
    if body_clean.lower() == "///restart" or body_clean.lower().startswith("///restart "):
        passphrase_arg = body_clean.split(maxsplit=1)[1].strip() if " " in body_clean else ""
        is_self = (session_key == "self")
        with lock:
            last_active = active_sessions.get(session_key)
            is_authed = (last_active is not None and (now - last_active <= INACTIVITY_TIMEOUT_SECONDS))

        if is_self or is_authed or passphrase_arg == PASSPHRASE:
            handle_tunnel_restart(target_chat)
            return

    # Step 1: Authentication & Inactivity Check
    with lock:
        last_active = active_sessions.get(session_key)
        is_authenticated = False

        if last_active is not None:
            time_since = now - last_active
            if time_since <= INACTIVITY_TIMEOUT_SECONDS:
                is_authenticated = True
                active_sessions[session_key] = now
                session_targets[session_key] = target_chat
                save_sessions()
            else:
                print(f"[Bridge] Session expired for {session_key} ({int(time_since)}s > {INACTIVITY_TIMEOUT_SECONDS}s)", flush=True)
                del active_sessions[session_key]
                save_sessions()

    # If session was detected as expired on this incoming message
    if last_active is not None and not is_authenticated:
        send_whatsapp_message(
            target_chat,
            "🔒 *Session Expired (10m Inactivity)*\n\n"
            "Your AGY session has automatically ended due to inactivity.\n"
            "Send the passphrase to start a new session."
        )
        return

    # If already authenticated -> Check bridge slash commands first (NEVER blocked by busy lock!)
    if is_authenticated:
        # Sending the passphrase while already unlocked should never reach the agent.
        if body_clean == PASSPHRASE:
            send_whatsapp_message(target_chat, "🔓 *Already unlocked.* Send a command or prompt.")
            return

        # Command: /stop, /cancel, /halt
        if body_clean.lower() in ["/stop", "/cancel", "/halt"]:
            interrupted = interrupt_active_turn(session_key, reason="Stopped by user command")
            with lock:
                gen = int(agent_state["chats"].setdefault(session_key, {}).get("generation", 1))
            stop_hermes(session_key, gen)
            if interrupted:
                send_whatsapp_message(target_chat, "🛑 *Tarea detenida por el usuario.*")
            else:
                send_whatsapp_message(target_chat, "ℹ️ No hay ninguna tarea activa ejecutándose.")
            return

        # Command: /lock or /logout
        if body_clean.lower() in ["/lock", "/logout", "/exit"]:
            interrupt_active_turn(session_key, reason="Locked by user")
            with lock:
                if session_key in active_sessions:
                    del active_sessions[session_key]
                    save_sessions()
            send_whatsapp_message(
                target_chat,
                "🔒 *Session Locked*\nAGY access has been locked for this chat. Send the passphrase to unlock again."
            )
            return

        # Command: /clear or /reset
        if body_clean.lower() in ["/clear", "/reset", "/new"]:
            interrupt_active_turn(session_key, reason="Cleared by user")
            with lock:
                chat_history[session_key] = False
                chat = agent_state["chats"].setdefault(session_key, {})
                chat["generation"] = int(chat.get("generation", 1)) + 1
                save_sessions()
                save_agent_state()
            agent_name, _mode = resolve_agent(session_key)
            label = "AGY" if agent_name == "agy" else "Hermes"
            send_whatsapp_message(
                target_chat,
                f"🧹 *Chat Context Cleared*\nStarted a fresh {label} session. Your next message will have a clean context."
            )
            return

        # Command: /switchagent (aliases: /agent, /harness)
        if body_clean.lower() in ["/switchagent", "/agent", "/harness"] or body_clean.lower().startswith(("/switchagent ", "/agent ", "/harness ")):
            arg = ""
            for prefix in ("/switchagent", "/agent", "/harness"):
                if body_clean.lower().startswith(prefix + " "):
                    arg = body_clean[len(prefix):].strip()
                    break
            switch_agent(session_key, target_chat, arg)
            return

        # Command: /help
        if body_clean.lower() in ["/help", "/commands"]:
            help_text = (
                "📋 *WhatsApp Agent Commands:*\n\n"
                "• `/switchagent` - Show or switch the active agent (agy / hermes / auto)\n"
                "• `/stop` - Detener inmediatamente la tarea activa\n"
                "• `/model` - View dynamic live models & active model\n"
                "• `/model <name>` - Switch LLM model (e.g. `/model claude-sonnet-5-5-high`)\n"
                "• `/model refresh` - Pull latest model updates from Google\n"
                "• `/clear` - Reset context and start a clean session\n"
                "• `/status` - Check timeout countdown & active model\n"
                "• `/lock` - Manually lock the session\n"
                "• `///restart` - Hard reset & reboot bridge tunnel if unresponsive\n"
                "• `/help` - Show this commands list\n\n"
                "📎 *Documents & Images:*\n"
                "Attach any PDF, code, document, or image with your prompt to have the agent analyze it directly!"
            )
            send_whatsapp_message(target_chat, help_text)
            return

        # Command: /model (alias: /models)
        if body_clean.lower() in ("/model", "/models") or body_clean.lower().startswith("/model "):
            agent_name, _mode = resolve_agent(session_key)
            parts = body_clean.split(maxsplit=1)
            arg = parts[1].strip() if len(parts) > 1 else ""

            if agent_name == "hermes":
                if not arg:
                    send_whatsapp_message(target_chat, format_hermes_models(session_key))
                    return
                matched = None
                for m_id, label in HERMES_MODELS:
                    if arg.lower() == m_id.lower() or arg.lower() == label.lower() or arg.lower() in m_id.lower():
                        matched = m_id
                        break
                if not matched:
                    avail = ", ".join(m_id for m_id, _ in HERMES_MODELS)
                    send_whatsapp_message(target_chat, f"⚠️ *Unknown Hermes model:* `{arg}`\nAvailable: {avail}")
                    return
                with lock:
                    agent_state["chats"].setdefault(session_key, {})["hermes_model"] = matched
                    save_agent_state()
                send_whatsapp_message(target_chat, f"🤖 *Hermes model switched to:* `{matched}`\nTakes effect on the next message.")
                return

            # AGY path
            with lock:
                active_model = user_models.get(session_key, DEFAULT_MODEL)
            if not arg:
                send_whatsapp_message(target_chat, format_model_list(active_model))
                return
            if arg.lower() == "refresh":
                typing_indicator = ContinuousTyping(target_chat)
                typing_indicator.start()
                try:
                    reply = format_model_list(active_model, force_refresh=True)
                finally:
                    typing_indicator.stop()
                send_whatsapp_message(target_chat, reply)
                return

            # Find matching model from dynamic list
            models = fetch_dynamic_models()
            matched_id = None
            matched_label = None

            for m_id, label in models:
                if arg.lower() == m_id.lower() or arg.lower() == label.lower():
                    matched_id = m_id
                    matched_label = label
                    break

            if not matched_id:
                for m_id, label in models:
                    if arg.lower() in m_id.lower() or arg.lower() in label.lower():
                        matched_id = m_id
                        matched_label = label
                        break

            if matched_id:
                with lock:
                    user_models[session_key] = matched_id
                    save_sessions()
                send_whatsapp_message(
                    target_chat,
                    f"🤖 *Model Switched Successfully!*\nActive model is now: *{matched_label}* (`{matched_id}`)"
                )
            else:
                send_whatsapp_message(
                    target_chat,
                    f"⚠️ *Unknown Model:* `{arg}`\nSend `/model` to see the live list of models provided by AGY."
                )
            return

        # Command: /status
        if body_clean.lower() == "/status":
            with lock:
                rem = max(0, int(INACTIVITY_TIMEOUT_SECONDS - (time.time() - active_sessions.get(session_key, time.time()))))
                active_model = user_models.get(session_key, DEFAULT_MODEL)
                has_hist = chat_history.get(session_key, False)
            agent_name, agent_mode = resolve_agent(session_key)
            mins = rem // 60
            secs = rem % 60
            send_whatsapp_message(
                target_chat,
                f"🟢 *Agent Status: Active*\n"
                f"• *Agent:* {agent_name} ({agent_mode})\n"
                f"• *Model:* `{active_model}`\n"
                f"• *Context Active:* {'Yes (continuing)' if has_hist else 'No (fresh turn)'}\n"
                f"• *Auto-lock in:* {mins}m {secs}s\n\n"
                f"Commands: `/switchagent`, `/model`, `/clear`, `/lock`, `/help`"
            )
            return

        # Document / Media Handling (single or multiple attachments)
        actual_prompt = body_clean
        media_paths = list(media_paths or [])
        if media_dict:
            typing_indicator = ContinuousTyping(target_chat)
            typing_indicator.start()
            try:
                saved_path, _mime = download_media_file(media_dict)
            finally:
                typing_indicator.stop()

            if saved_path:
                media_paths.append(saved_path)
            else:
                send_whatsapp_message(target_chat, "⚠️ Failed to download the attached file from WhatsApp.")
                return

        if media_paths:
            unique_paths = []
            seen = set()
            for p in media_paths:
                key = os.path.normcase(os.path.abspath(str(p)))
                if key not in seen:
                    seen.add(key)
                    unique_paths.append(p)
            file_block = "\n".join(f"- {p}" for p in unique_paths)
            if actual_prompt:
                actual_prompt = f"The user provided the following attached file(s):\n{file_block}\n\nUser's prompt: {actual_prompt}"
            else:
                actual_prompt = f"The user provided the following attached file(s):\n{file_block}\n\nPlease inspect, read, and summarize or analyze them."
            print(f"[Bridge] [{session_key}] Attachments: {len(unique_paths)} file(s)", flush=True)

        if not actual_prompt:
            return

        # Instant Preemption: if a previous task is active for this chat, interrupt it immediately
        had_interrupted_turn = False
        old_turn = interrupt_active_turn(session_key, reason="Interrupted by new prompt")
        if old_turn:
            had_interrupted_turn = True
            send_whatsapp_message(target_chat, "⚠️ *[Interrumpido por nuevo mensaje]*")
            time.sleep(0.35)

        new_turn = ActiveTurn(session_key, target_chat, actual_prompt)
        with lock:
            active_turns[session_key] = new_turn
            running_chats.add(session_key)

        try:
            with lock:
                active_model = user_models.get(session_key, DEFAULT_MODEL)
                continue_session = chat_history.get(session_key, False)
                # If we interrupted a running turn, always use --continue to preserve context
                if had_interrupted_turn:
                    continue_session = True
                chat = get_chat_agent(session_key)
                generation = chat.get("generation", 1)

            agent_name, agent_mode = resolve_agent(session_key)
            if agent_name == "agy":
                model_label = active_model
            else:
                hm = get_hermes_model(session_key)
                model_label = ("deepseek:" + hm) if hm else "deepseek:default"
            print(f"[Bridge] [{session_key}] agent={agent_name}/{agent_mode} model={model_label}: {actual_prompt[:60].replace(PASSPHRASE, '***')}...", flush=True)

            # Continuous typing indicator active for the entire prompt generation
            typing_indicator = ContinuousTyping(target_chat)
            new_turn.typing_indicator = typing_indicator
            typing_indicator.start()
            final_reply = None
            try:
                if agent_name == "hermes":
                    hermes_res = run_hermes_turn(actual_prompt, target_chat, session_key, generation)
                    if hermes_res.get("code") == "ok" and hermes_res.get("text"):
                        final_reply = hermes_res["text"]
                        send_whatsapp_message(target_chat, f"🤖 Hermes:\n{final_reply}")
                    else:
                        err = hermes_res.get("error") or "unknown error"
                        set_backend_cooldown("hermes", HERMES_COOLDOWN_DEFAULT_MINUTES * 60)
                        send_whatsapp_message(target_chat, f"⚠️ Hermes error: {err}")
                        if agent_mode == "auto" and not backend_on_cooldown("agy"):
                            send_whatsapp_message(target_chat, "🔁 Falling back to AGY...")
                            final_reply = _run_agy_turn(actual_prompt, target_chat, session_key, active_model, continue_session, new_turn, typing_indicator)
                else:
                    final_reply = _run_agy_turn(actual_prompt, target_chat, session_key, active_model, continue_session, new_turn, typing_indicator)
                    if final_reply and agy_quota_hit(final_reply):
                        reset_secs = parse_reset_seconds(final_reply) or (AGY_COOLDOWN_DEFAULT_HOURS * 3600)
                        set_backend_cooldown("agy", reset_secs)
                        if agent_mode == "auto":
                            ok, why = hermes_available()
                            if ok:
                                send_whatsapp_message(target_chat, "🔁 AGY quota reached. Falling back to Hermes (DeepSeek) for this turn.")
                                hermes_res = run_hermes_turn(actual_prompt, target_chat, session_key, generation)
                                if hermes_res.get("code") == "ok" and hermes_res.get("text"):
                                    final_reply = hermes_res["text"]
                                    send_whatsapp_message(target_chat, f"🤖 Hermes:\n{final_reply}")
                                else:
                                    final_reply = None
                                    send_whatsapp_message(target_chat, f"⚠️ Hermes fallback failed: {hermes_res.get('error')}")
                            else:
                                final_reply = None
                                send_whatsapp_message(target_chat, f"⚠️ AGY quota reached and Hermes unavailable ({why}). Try /switchagent later.")
                        else:
                            final_reply = None
                            send_whatsapp_message(target_chat, "💡 AGY quota reached. Use /switchagent hermes or /switchagent auto.")
            finally:
                typing_indicator.stop()

            if new_turn.is_interrupted.is_set():
                print(f"[Bridge] [{session_key}] Turn was preempted/interrupted, skipping post-run state update.", flush=True)
                return

            with lock:
                # Refresh inactivity timestamp on completion so long runs don't eat into user timeout
                if session_key in active_sessions:
                    active_sessions[session_key] = time.time()
                if final_reply and not str(final_reply).startswith("⚠️"):
                    chat_history[session_key] = True
                save_sessions()
        finally:
            new_turn.is_completed.set()
            with lock:
                if active_turns.get(session_key) is new_turn:
                    del active_turns[session_key]
                    running_chats.discard(session_key)
        return

    # Not authenticated: check for passphrase
    if body_clean == PASSPHRASE:
        with lock:
            active_sessions[session_key] = now
            session_targets[session_key] = target_chat
            save_sessions()
        welcome_msg = (
            "🔓 *Access Granted!*\n\n"
            "You are now connected to your machine's agents:\n"
            "• *AGY* (Google Antigravity) - primary, `--dangerously-skip-permissions`\n"
            "• *Hermes* (DeepSeek) - fallback, full terminal access\n\n"
            "• Send any prompt, code, question, or attach documents & images.\n"
            "• Commands: `/switchagent`, `/model`, `/clear`, `/status`, `/lock`, `/help`\n"
            "• Session auto-locks after *10 minutes* of inactivity."
        )
        print(f"[Bridge] Authorized access for {session_key} ({sender_name})", flush=True)
        send_whatsapp_message(target_chat, welcome_msg)
    else:
        # STRICT STEALTH MODE: Absolutely NO feedback if passphrase is not sent.
        # This ensures personal chats, external contacts, and expired sessions remain 100% silent and private.
        print(f"[Bridge] Silently ignoring message from unauthenticated {session_key} (stealth mode: no feedback)", flush=True)
        return

def handle_webhook_payload(post_data):
    """Processes incoming WAHA webhook JSON asynchronously off the HTTP server thread."""
    try:
        event_data = json.loads(post_data.decode("utf-8", errors="replace"))
    except Exception:
        return

    event_type = event_data.get("event")
    if event_type not in ["message", "message.any"]:
        return

    payload = event_data.get("payload", {})
    msg_id = payload.get("id")
    from_me = payload.get("fromMe", False)
    from_chat = payload.get("from", "") or ""
    to_chat = payload.get("to", "") or ""
    body = payload.get("body", "") or ""
    has_media = payload.get("hasMedia", False)
    media_dict = payload.get("media") if has_media else None

    # Ignore if both body and media are empty
    if not body and not has_media:
        return

    body_stripped = body.strip() if isinstance(body, str) else ""

    # 1. ANTI-RECURSION: Ignore any message starting with a known bot prefix
    if body_stripped.startswith(BOT_PREFIXES):
        return

    # Extract canonical message ID string
    msg_id_str = ""
    if isinstance(msg_id, dict):
        msg_id_str = str(msg_id.get("_serialized") or msg_id.get("id") or "")
    elif msg_id:
        msg_id_str = str(msg_id)

    # 2. ANTI-RECURSION & DEDUPLICATION: Check bot sent caches and already-processed webhook IDs
    with lock:
        if body_stripped and body_stripped in bot_sent_texts:
            return
        if msg_id_str and msg_id_str in bot_sent_ids:
            return
        if isinstance(msg_id, dict):
            if str(msg_id.get("_serialized")) in bot_sent_ids or str(msg_id.get("id")) in bot_sent_ids:
                return
        if msg_id_str:
            if msg_id_str in processed_msg_ids:
                return
            processed_msg_ids.append(msg_id_str)

    # 3. Ignore group chats and status broadcasts
    if from_chat.endswith("@g.us") or to_chat.endswith("@g.us") or "status@broadcast" in (from_chat, to_chat):
        return

    # 4. Detect self-messages vs external messages
    is_self = (from_chat in MY_ACCOUNTS) and (to_chat in MY_ACCOUNTS)
    if not is_self and from_chat and from_chat == to_chat:
        is_self = True

    if is_self:
        # In self-chat, messages generated by WAHA API always originate from @lid.
        # Messages sent by the physical phone user originate from @c.us.
        if from_chat.endswith("@lid"):
            return
        target_chat = to_chat or from_chat
    elif from_me:
        # Outgoing message sent by the user/API to an external contact -> ignore
        return
    else:
        # Incoming message from an external contact
        target_chat = from_chat

    if not target_chat:
        return

    sender_name = payload.get("_data", {}).get("notifyName", "User")
    safe_body = body.replace(PASSPHRASE, "***") if isinstance(body, str) else ""
    print(f"[Bridge] Webhook incoming from {from_chat} (media={has_media}): {safe_body[:50]!r}", flush=True)

    dispatch_message(target_chat, body, sender_name, media_dict)

class BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        resp = b'{"status":"running","service":"WAHA-AGY-Bridge"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(resp)

    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b""
        except Exception:
            post_data = b""

        # Respond immediately to WAHA with 200 OK before any locking or processing
        resp = b'{"status":"ok"}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(resp)
            self.wfile.flush()
        except Exception:
            pass

        if post_data:
            t = threading.Thread(target=handle_webhook_payload, args=(post_data,), daemon=True)
            t.start()

    def log_message(self, format, *args):
        # Mute standard HTTP request access logging
        pass

def check_and_notify_restart():
    restart_marker = os.path.join(BASE_DIR, ".restart_notify")
    if os.path.exists(restart_marker):
        try:
            with open(restart_marker, "r", encoding="utf-8") as rf:
                rdata = json.load(rf)
            notify_chat = rdata.get("chat_id")
            if notify_chat:
                time.sleep(2)
                with lock:
                    active_mod = user_models.get(canonical_chat_id(notify_chat), DEFAULT_MODEL)
                msg = (
                    "🟢 *AGY Tunnel Restarted Successfully*\n\n"
                    "Bridge is online, locks cleared, and service is operational.\n"
                    f"• *Active Model:* `{active_mod}`\n"
                    "• *Timeout Watchdog:* 10 minutes\n\n"
                    "Send any prompt or command to continue."
                )
                send_whatsapp_message(notify_chat, msg)
        except Exception as e:
            print(f"[Bridge] Error handling restart notify: {e}", flush=True)
        finally:
            try:
                if os.path.exists(restart_marker):
                    os.remove(restart_marker)
            except Exception:
                pass

def main():
    sync_gemini_md()
    refresh_my_accounts()
    load_sessions()
    load_agent_state()
    fetch_dynamic_models()  # Pre-fetch models on startup

    try:
        ok, _why = hermes_available()
        if ok:
            hermes_backend.sync_context()
            print("[Bridge] Hermes context synced (GEMINI.md + skills).", flush=True)
        else:
            print(f"[Bridge] Hermes unavailable at startup: {_why}", flush=True)
    except Exception as e:
        print(f"[Bridge] Hermes sync error at startup: {e}", flush=True)

    watchdog_thread = threading.Thread(target=session_inactivity_watchdog, daemon=True)
    watchdog_thread.start()

    threading.Thread(target=check_and_notify_restart, daemon=True).start()

    server_address = ("", PORT)
    httpd = BridgeHTTPServer(server_address, WebhookHandler)
    print("=" * 60, flush=True)
    print(f"[Bridge] WAHA <-> AGY Bridge running on port {PORT} (Multi-Threaded)", flush=True)
    print(f"[Bridge] My Accounts: {MY_ACCOUNTS}", flush=True)
    print("[Bridge] Security: Passphrase required (********)", flush=True)
    print(f"[Bridge] Inactivity Timeout: {INACTIVITY_TIMEOUT_SECONDS // 60} minutes", flush=True)
    print(f"[Bridge] AGY Executable: {AGY_EXE if os.path.exists(AGY_EXE) else AGY_DANGER_CMD}", flush=True)
    print("=" * 60, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping bridge server...", flush=True)
        httpd.server_close()

if __name__ == "__main__":
    main()
