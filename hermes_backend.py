# -*- coding: utf-8 -*-
"""Headless Hermes (DeepSeek) backend for the WAHA <-> AGY WhatsApp bridge.

Runs `hermes -p <profile> chat` as a one-shot subprocess. Before every run it
syncs the bridge's GEMINI.md and skills into the Hermes profile/workdir so the
DeepSeek agent follows the SAME instructions and skills the AGY setup uses.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_active_procs = {}          # session_name -> Popen
_active_procs_lock = threading.Lock()
_stop_events = {}           # session_name -> threading.Event
_stop_events_lock = threading.Lock()


class HermesConfig:
    def __init__(self):
        self.profile = os.environ.get("HERMES_PROFILE", "wa").strip()
        self.exe = os.environ.get(
            "HERMES_EXE", r"C:\Users\T450\AppData\Local\hermes\bin\hermes.cmd"
        ).strip()
        self.home = os.path.dirname(os.path.dirname(self.exe))
        self.profile_dir = os.path.join(self.home, "profiles", self.profile)
        self.workdir = os.environ.get("HERMES_WORKDIR", r"C:\waha\workspace").strip()
        self.model = os.environ.get("HERMES_MODEL", "").strip()
        self.max_runtime = int(os.environ.get("HERMES_MAX_RUNTIME_SECONDS", "1800"))
        self.idle_timeout = int(os.environ.get("HERMES_IDLE_TIMEOUT_SECONDS", "900"))
        self.gemini_source = os.environ.get(
            "HERMES_GEMINI_SOURCE", r"C:\waha\GEMINI.md"
        ).strip()
        self.skills_source = os.environ.get(
            "HERMES_SKILLS_SOURCE", r"C:\waha\.agents\skills"
        ).strip()
        self.enabled = (
            os.environ.get("HERMES_ENABLED", "true").strip().lower() == "true"
        )
        self.stream_text = (
            os.environ.get("HERMES_STREAM_TEXT", "true").strip().lower() == "true"
        )
        self.stream_min_chars = int(os.environ.get("HERMES_STREAM_MIN_CHARS", "400"))
        self.stream_debounce = float(
            os.environ.get("HERMES_STREAM_DEBOUNCE_SECONDS", "2.5")
        )


CONF = HermesConfig()


def available():
    """Return (ok, reason)."""
    if not CONF.enabled:
        return False, "disabled (HERMES_ENABLED=false)"
    if not os.path.isfile(CONF.exe):
        return False, "Hermes CLI not found: {}".format(CONF.exe)
    if not os.path.isdir(CONF.profile_dir):
        return False, "Hermes profile '{}' not found: {}".format(CONF.profile, CONF.profile_dir)
    return True, "ok"


def sanitize_name(text):
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(text)).strip("-")[:48]
    return name or "chat"


def session_name_for(chat_key, generation):
    return "wa-{}-g{}".format(sanitize_name(chat_key), int(generation or 1))


def sync_context():
    """Mirror the bridge's GEMINI.md and skills into the Hermes profile/workdir.

    This is what makes the DeepSeek agent behave like the AGY agent: same
    host instructions (GEMINI.md) and same skills (agentskills.io format).

    Hermes only auto-loads AGENTS.md / CLAUDE.md / .hermes.md / .cursorrules as
    context files (NOT GEMINI.md), so GEMINI.md is mirrored to AGENTS.md in the
    working directory. The mirror is SANITIZED: credentials are redacted because
    the context is sent to the remote DeepSeek API. The tools themselves read
    their secrets from C:\\waha\\.env at runtime.
    """
    for d in (CONF.profile_dir, CONF.workdir):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    if os.path.isfile(CONF.gemini_source):
        try:
            with open(CONF.gemini_source, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            text = ""

        if text:
            # Redact credentials before the context goes to the model provider.
            text = text.replace("5c21c1ce504644a1a527771630209f2f", "[REDACTED_WAHA_API_KEY]")
            text = text.replace("@Alexandro177", "[REDACTED_NEXUS_PASSWORD]")

            # Hermes' terminal uses its own venv python, which lacks yt-dlp /
            # requests. Point the documented commands at the system Python.
            sys_py = r'"C:\Program Files\Python311\python.exe"'
            text = text.replace("python C:\\waha\\send_file.py", sys_py + " C:\\waha\\send_file.py")
            text = text.replace("python -m yt_dlp", sys_py + " -m yt_dlp")
            text = text.replace(
                "python C:\\waha\\NexusAPI\\nexus_tool.py", sys_py + " C:\\waha\\NexusAPI\\nexus_tool.py"
            )

            header = (
                "# WhatsApp Agent Guidelines\n"
                "This is a sanitized mirror of C:\\waha\\GEMINI.md (secrets redacted).\n"
                "It is authoritative for formatting, machine identity, file sending, "
                "video/yt-dlp usage, and integrations.\n\n"
            )
            text = header + text
            for target in (
                os.path.join(CONF.profile_dir, "GEMINI.md"),
                os.path.join(CONF.workdir, "GEMINI.md"),
                os.path.join(CONF.workdir, "AGENTS.md"),
            ):
                try:
                    with open(target, "w", encoding="utf-8") as f:
                        f.write(text)
                except Exception:
                    pass
    if os.path.isdir(CONF.skills_source):
        dst_root = os.path.join(CONF.profile_dir, "skills")
        try:
            os.makedirs(dst_root, exist_ok=True)
        except Exception:
            pass
        for name in os.listdir(CONF.skills_source):
            src = os.path.join(CONF.skills_source, name)
            dst = os.path.join(dst_root, name)
            if os.path.isdir(src):
                try:
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                except Exception:
                    pass
    return True


def _kill_tree(proc):
    if proc is None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def stop(chat_key, generation):
    sname = session_name_for(chat_key, generation)
    with _stop_events_lock:
        ev = _stop_events.get(sname)
    if ev:
        ev.set()
    with _active_procs_lock:
        proc = _active_procs.get(sname)
    if proc is not None and proc.poll() is None:
        _kill_tree(proc)


def classify_error(text):
    t = (text or "").lower()
    if re.search(r"429|resource_exhausted|quota|insufficient.*balance|rate.?limit", t):
        return "quota"
    if re.search(r"401|403|unauthorized|invalid api key|authentication", t):
        return "auth"
    if re.search(r"timed? ?out", t):
        return "timeout"
    return "unknown"


def run(prompt, chat_key, generation, model=None, reasoning=None, on_event=None, on_log=None):
    """Run one Hermes turn. Returns a dict with text/error/code/session."""
    ok, why = available()
    if not ok:
        return {"text": None, "error": why, "code": "unavailable", "session": None}
    try:
        sync_context()
    except Exception:
        pass

    sname = session_name_for(chat_key, generation)
    prompt = (prompt or "").strip() or "."

    try:
        os.makedirs(CONF.workdir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".txt", prefix="hbridge_", dir=CONF.workdir
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)
    except Exception as e:
        return {
            "text": None,
            "error": "temp prompt error: {}".format(e),
            "code": "unknown",
            "session": sname,
        }

    cmd = [
        CONF.exe, "-p", CONF.profile, "chat",
        "--query-file", tmp_path,
        "--oneshot", "-Q", "--format", "stream-json",
        "--continue", sname, "--create-if-missing",
        "--in", CONF.workdir,
        "--yolo",
        "--run-budget", str(CONF.max_runtime),
    ]
    if CONF.model:
        cmd += ["-m", CONF.model]
    elif model:
        cmd += ["-m", model]
    if reasoning:
        cmd += ["--reasoning", str(reasoning)]

    if on_log:
        try:
            on_log("spawn: " + " ".join(cmd[:6]) + " ...")
        except Exception:
            pass

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=CONF.workdir,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return {
            "text": None,
            "error": "spawn error: {}".format(e),
            "code": "unavailable",
            "session": sname,
        }

    with _active_procs_lock:
        _active_procs[sname] = proc
    with _stop_events_lock:
        _stop_events[sname] = threading.Event()
    stop_ev = _stop_events[sname]

    q = queue.Queue()

    def pump(stream, kind):
        try:
            for line in iter(stream.readline, ""):
                q.put((kind, line))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    last_event = time.time()
    final_text = None
    final_tokens = None
    err_tail = []

    while True:
        if stop_ev.is_set():
            _kill_tree(proc)
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return {
                "text": None,
                "error": "Interrupted by user",
                "code": "interrupted",
                "session": sname,
            }
        try:
            kind, line = q.get(timeout=0.4)
        except queue.Empty:
            if proc.poll() is not None and q.empty():
                break
            if time.time() - last_event > CONF.idle_timeout:
                _kill_tree(proc)
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                return {
                    "text": None,
                    "error": "no output for {}s".format(CONF.idle_timeout),
                    "code": "timeout",
                    "session": sname,
                }
            continue
        last_event = time.time()
        line = line.strip()
        if not line:
            continue
        if kind == "err":
            err_tail.append(line)
            if on_log:
                try:
                    on_log("hermes: " + ANSI_RE.sub("", line)[:200])
                except Exception:
                    pass
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        etype = data.get("type")
        if etype in ("tool", "tool_use"):
            tname = (
                data.get("name")
                or data.get("tool_name")
                or data.get("tool")
                or data.get("title")
                or "tool"
            )
            if on_event:
                try:
                    on_event(
                        {
                            "type": "tool",
                            "name": str(tname),
                            "input": data.get("input") or {},
                            "data": data,
                        }
                    )
                except Exception:
                    pass
        elif etype == "text":
            # Token-level deltas: relay them so the bridge can stream to WhatsApp.
            txt = data.get("text") or ""
            if txt and on_event:
                try:
                    on_event({"type": "text", "text": txt})
                except Exception:
                    pass
        elif etype == "result":
            final_text = (data.get("text") or "").strip()
            final_tokens = data.get("tokens") or {}
            if on_event:
                try:
                    on_event(
                        {"type": "result", "text": final_text, "tokens": final_tokens}
                    )
                except Exception:
                    pass

    try:
        proc.wait(timeout=10)
    except Exception:
        pass

    with _active_procs_lock:
        _active_procs.pop(sname, None)
    with _stop_events_lock:
        _stop_events.pop(sname, None)
    try:
        os.remove(tmp_path)
    except Exception:
        pass

    rc = proc.returncode
    err_clean = ANSI_RE.sub("", "\n".join(err_tail[-12:])).strip()
    code = "ok" if rc == 0 else classify_error(err_clean or final_text)
    return {
        "text": final_text,
        "error": (None if rc == 0 else (err_clean or "exit {}".format(rc))),
        "code": code,
        "session": sname,
        "tokens": final_tokens,
    }
