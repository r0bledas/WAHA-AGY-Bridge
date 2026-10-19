import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import requests

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
INACTIVITY_TIMEOUT_SECONDS = int(os.environ.get("INACTIVITY_TIMEOUT_SECONDS", "600"))  # 10 minutes
WAHA_URL = os.environ.get("WAHA_URL", "http://localhost:3001")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "YOUR_WAHA_API_KEY")
AGY_DIR = os.environ.get("AGY_DIR", r"C:\Users\T450\Downloads\AGY-Portable-v1.6.0-All-Platforms")
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
BOT_PREFIXES = ("🤖", "🔒", "🔓", "🟢", "⚠️", "🧹", "📋", "⏳")

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
running_chats = set()                 # Prevent concurrent overlapping runs for the same chat
cached_models = []                    # [(model_id, label), ...]
last_models_fetch = 0
lock = threading.RLock()              # Reentrant lock to prevent self-deadlock on nested calls

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


def send_whatsapp_message(chat_id, text):
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

def run_agy_prompt(prompt, model=None, continue_session=False):
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
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                print(f"[Bridge] AGY process {proc.pid} timed out after 180s, killing process tree...", flush=True)
                kill_process_tree(proc.pid)
                return "⚠️ AGY execution timed out after 3 minutes."

            # Ensure any lingering grandchild processes are cleaned up
            kill_process_tree(proc.pid)

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

def process_message(target_chat, body, sender_name, media_dict=None):
    session_key = canonical_chat_id(target_chat)
    now = time.time()
    body_clean = body.strip()

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
        # Command: /lock or /logout
        if body_clean.lower() in ["/lock", "/logout", "/exit"]:
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
            with lock:
                chat_history[session_key] = False
                save_sessions()
            send_whatsapp_message(
                target_chat,
                "🧹 *Chat Context Cleared*\nStarted a fresh AGY session. Your next message will have a clean context."
            )
            return

        # Command: /help
        if body_clean.lower() in ["/help", "/commands"]:
            help_text = (
                "📋 *Antigravity (AGY) WhatsApp Commands:*\n\n"
                "• `/model` - View dynamic live models & active model\n"
                "• `/model <name>` - Switch LLM model (e.g. `/model claude-sonnet-5-5-high`)\n"
                "• `/model refresh` - Pull latest model updates from Google\n"
                "• `/clear` - Reset context and start a clean session\n"
                "• `/status` - Check timeout countdown & active model\n"
                "• `/lock` - Manually lock the session\n"
                "• `/help` - Show this commands list\n\n"
                "📎 *Documents & Images:*\n"
                "Attach any PDF, code, document, or image with your prompt to have AGY analyze it directly!"
            )
            send_whatsapp_message(target_chat, help_text)
            return

        # Command: /model
        if body_clean.lower() == "/model" or body_clean.lower().startswith("/model "):
            parts = body_clean.split(maxsplit=1)
            with lock:
                active_model = user_models.get(session_key, DEFAULT_MODEL)
            if len(parts) == 1:
                send_whatsapp_message(target_chat, format_model_list(active_model))
                return
            else:
                arg = parts[1].strip()
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
            mins = rem // 60
            secs = rem % 60
            send_whatsapp_message(
                target_chat,
                f"🟢 *AGY Status: Active*\n"
                f"• *Model:* `{active_model}`\n"
                f"• *Context Active:* {'Yes (continuing)' if has_hist else 'No (fresh turn)'}\n"
                f"• *Auto-lock in:* {mins}m {secs}s\n\n"
                f"Commands: `/model`, `/clear`, `/lock`, `/help`"
            )
            return

        # Document / Media Handling
        actual_prompt = body_clean
        if media_dict:
            typing_indicator = ContinuousTyping(target_chat)
            typing_indicator.start()
            try:
                saved_path, mime = download_media_file(media_dict)
            finally:
                typing_indicator.stop()

            if saved_path:
                fname = os.path.basename(saved_path)
                if actual_prompt:
                    actual_prompt = f"The user provided an attached file at '{saved_path}' (file: {fname}, MIME: {mime}).\nUser's prompt: {actual_prompt}"
                else:
                    actual_prompt = f"The user provided an attached file at '{saved_path}' (file: {fname}, MIME: {mime}). Please inspect, read, and summarize or analyze this document/image."
            else:
                send_whatsapp_message(target_chat, "⚠️ Failed to download the attached file from WhatsApp.")
                return

        if not actual_prompt:
            return

        # Check concurrency for AGY runs (never call network I/O inside lock!)
        already_busy = False
        with lock:
            if session_key in running_chats:
                already_busy = True
            else:
                running_chats.add(session_key)

        if already_busy:
            print(f"[Bridge] Busy with active AGY task for {session_key}, notifying user", flush=True)
            send_whatsapp_message(target_chat, "⏳ *AGY is currently working on your previous request.* Please wait a moment.")
            return

        try:
            with lock:
                active_model = user_models.get(session_key, DEFAULT_MODEL)
                continue_session = chat_history.get(session_key, False)

            print(f"[Bridge] [{session_key}] Running AGY (model={active_model}, continue={continue_session}): {actual_prompt[:60]}...", flush=True)

            # Continuous typing indicator active for the entire prompt generation
            typing_indicator = ContinuousTyping(target_chat)
            typing_indicator.start()
            try:
                agy_reply = run_agy_prompt(actual_prompt, model=active_model, continue_session=continue_session)
            finally:
                typing_indicator.stop()

            with lock:
                # Refresh inactivity timestamp on completion so long runs don't eat into user timeout
                if session_key in active_sessions:
                    active_sessions[session_key] = time.time()
                if not agy_reply.startswith("⚠️"):
                    chat_history[session_key] = True
                save_sessions()

            formatted_reply = f"🤖 AGY:\n{agy_reply}"
            send_whatsapp_message(target_chat, formatted_reply)
        finally:
            with lock:
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
            "You are now connected to *Google Antigravity (AGY)* in `--dangerously-skip-permissions` mode.\n\n"
            "• Send any prompt, code, question, or attach documents & images.\n"
            "• Commands: `/model`, `/clear`, `/status`, `/lock`, `/help`\n"
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
    print(f"[Bridge] Webhook incoming from {from_chat} (media={has_media}): {body[:50]!r}", flush=True)

    process_message(target_chat, body, sender_name, media_dict)

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

def main():
    sync_gemini_md()
    refresh_my_accounts()
    load_sessions()
    fetch_dynamic_models()  # Pre-fetch models on startup

    watchdog_thread = threading.Thread(target=session_inactivity_watchdog, daemon=True)
    watchdog_thread.start()

    server_address = ("", PORT)
    httpd = BridgeHTTPServer(server_address, WebhookHandler)
    print("=" * 60, flush=True)
    print(f"[Bridge] WAHA <-> AGY Bridge running on port {PORT} (Multi-Threaded)", flush=True)
    print(f"[Bridge] My Accounts: {MY_ACCOUNTS}", flush=True)
    print(f"[Bridge] Security: Passphrase required ('{PASSPHRASE}')", flush=True)
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
