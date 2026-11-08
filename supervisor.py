"""
supervisor.py - Ultra-Lightweight Watchdog & Reverse Proxy for AGY Bridge
This process runs independently as a system service.
It listens on PORT 5055 (where WAHA sends webhooks) and manages agy_bridge.py on internal PORT 5056.

Features:
1. Zero heavy dependencies (100% Python standard library).
2. Intercepts '///restart' or '/restart' independently — even if the worker or agy.exe is 100% hung.
3. Automatically recovers and restarts the worker if it crashes or stops responding to health checks.
4. Asynchronously forwards all other webhooks to the worker without blocking WAHA.
"""

import os
import sys
import time
import json
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import subprocess
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Load Environment (.env) ──
def load_env(path):
    env = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return env

CONFIG = load_env(os.path.join(BASE_DIR, ".env"))
SUPERVISOR_PORT = int(CONFIG.get("SUPERVISOR_PORT", "5055"))  # Public webhook port
WORKER_PORT = int(CONFIG.get("WORKER_PORT", "5056"))          # Internal worker port
WAHA_URL = CONFIG.get("WAHA_URL", "http://localhost:3001")
WAHA_API_KEY = CONFIG.get("WAHA_API_KEY", "")
PASSPHRASE = CONFIG.get("BRIDGE_PASSPHRASE", "HolaSoyGaySixSevenXd")
MY_ACCOUNTS = set(acc.strip() for acc in CONFIG.get("BRIDGE_MY_ACCOUNTS", "").split(",") if acc.strip())

worker_process = None
worker_lock = threading.Lock()
unhealthy_count = 0


# ── Direct WhatsApp Message via WAHA REST API ──
def send_whatsapp(target_chat, text):
    """Directly sends a WhatsApp text message through WAHA API without worker dependency."""
    if not target_chat or not text:
        return False
    url = f"{WAHA_URL}/api/sendText"
    payload = json.dumps({
        "session": "default",
        "chatId": target_chat,
        "text": text
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": WAHA_API_KEY
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200 or resp.status == 201
    except Exception as e:
        print(f"[Supervisor] WAHA notification notice: {e}", flush=True)
        return False


# ── Worker Process Management ──
def start_worker():
    """Spawns agy_bridge.py on internal WORKER_PORT."""
    global worker_process
    with worker_lock:
        worker_env = os.environ.copy()
        worker_env["BRIDGE_PORT"] = str(WORKER_PORT)
        worker_env["PYTHONUNBUFFERED"] = "1"

        stdout_log = os.path.join(BASE_DIR, "bridge_stdout.log")
        stderr_log = os.path.join(BASE_DIR, "bridge_stderr.log")

        try:
            out_f = open(stdout_log, "a", encoding="utf-8", errors="replace")
            err_f = open(stderr_log, "a", encoding="utf-8", errors="replace")
        except Exception:
            out_f = subprocess.DEVNULL
            err_f = subprocess.DEVNULL

        cmd = [sys.executable, "-u", os.path.join(BASE_DIR, "agy_bridge.py")]
        print(f"[Supervisor] Spawning worker: {' '.join(cmd)} on internal port {WORKER_PORT}...", flush=True)

        worker_process = subprocess.Popen(
            cmd,
            cwd=BASE_DIR,
            env=worker_env,
            stdout=out_f,
            stderr=err_f,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        print(f"[Supervisor] Worker launched with PID {worker_process.pid}", flush=True)


def stop_worker():
    """Aggressively kills worker and any spawned agy CLI sub-processes."""
    global worker_process
    with worker_lock:
        # 1. Kill any active agy.exe processes
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "agy.exe", "/T"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=5
            )
        except Exception:
            pass

        # 2. Terminate the Python worker
        if worker_process:
            pid = worker_process.pid
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid), "/T"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=5
                )
            except Exception:
                try:
                    worker_process.kill()
                except Exception:
                    pass
            worker_process = None


def restart_worker(reason="Manual restart requested"):
    """Completely reboots the worker."""
    global unhealthy_count
    print(f"[Supervisor] Restarting worker ({reason})...", flush=True)
    stop_worker()
    time.sleep(1.5)
    start_worker()
    unhealthy_count = 0


# ── Health Check / Ping ──
def ping_worker():
    """Checks if worker is responding to HTTP GET on internal port."""
    url = f"http://127.0.0.1:{WORKER_PORT}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def watchdog_loop():
    """Independent background loop monitoring worker health."""
    global unhealthy_count
    time.sleep(5)  # Initial grace period
    while True:
        try:
            time.sleep(15)
            # 1. Check if process exited
            if worker_process is not None:
                code = worker_process.poll()
                if code is not None:
                    print(f"[Supervisor] Worker PID {worker_process.pid} exited with code {code}. Reviving...", flush=True)
                    restart_worker("Process exited")
                    continue

            # 2. Check HTTP health
            if not ping_worker():
                unhealthy_count += 1
                print(f"[Supervisor] Worker health check failed ({unhealthy_count}/3)", flush=True)
                if unhealthy_count >= 3:
                    print("[Supervisor] Worker unresponsive for 45s. Forcing hard restart...", flush=True)
                    restart_worker("Unresponsive health checks")
            else:
                unhealthy_count = 0
        except Exception as e:
            print(f"[Supervisor] Watchdog notice: {e}", flush=True)


# ── Proxy & Webhook Dispatcher ──
def forward_to_worker(post_data):
    """Forwards raw webhook POST to internal worker on port 5056."""
    url = f"http://127.0.0.1:{WORKER_PORT}/webhook"
    try:
        req = urllib.request.Request(
            url,
            data=post_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        print(f"[Supervisor] Forwarding to worker notice: {e}", flush=True)


class SupervisorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        worker_ok = ping_worker()
        worker_pid = worker_process.pid if worker_process else None
        resp = json.dumps({
            "status": "running",
            "service": "WAHA-Supervisor",
            "worker_pid": worker_pid,
            "worker_alive": worker_ok,
            "external_port": SUPERVISOR_PORT,
            "internal_port": WORKER_PORT
        }).encode("utf-8")
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

        # Immediately reply 200 OK to WAHA
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        resp = b'{"status":"ok"}'
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(resp)

        if not post_data:
            return

        # Check for remote restart command in JSON payload
        try:
            event = json.loads(post_data.decode("utf-8", errors="replace"))
            payload = event.get("payload", {})
            body = (payload.get("body") or "").strip()
            from_chat = payload.get("from", "") or ""
            to_chat = payload.get("to", "") or ""
            sender = from_chat or to_chat
            body_lower = body.lower()

            # Master Remote Hard Restart: "///restart" or "/restart"
            if body_lower in ("///restart", "/restart") or body_lower.startswith(("///restart ", "/restart ")):
                pass_cand = body.split(maxsplit=1)[1].strip() if " " in body else ""
                is_authorized = (
                    from_chat in MY_ACCOUNTS or
                    to_chat in MY_ACCOUNTS or
                    pass_cand == PASSPHRASE
                )

                if is_authorized:
                    print(f"[Supervisor] Remote hard restart requested by {sender}!", flush=True)
                    send_whatsapp(
                        sender,
                        "🔄 *[Supervisor] Reinicio Forzado en Progreso...*\n\n"
                        "• Terminando worker y procesos de AGY colgados\n"
                        "• Limpiando locks y reiniciando puente en puerto interno\n"
                        "• El servicio responderá en 3 segundos..."
                    )

                    def do_restart():
                        restart_worker("WhatsApp ///restart command")
                        # Wait for worker to be healthy
                        for _ in range(10):
                            time.sleep(1)
                            if ping_worker():
                                break
                        send_whatsapp(
                            sender,
                            "🟢 *[Supervisor] Bridge reiniciado y saludable.*\n\n"
                            f"• PID Worker: `{worker_process.pid if worker_process else 'N/A'}`\n"
                            f"• Puerto interno: `{WORKER_PORT}`\n"
                            "Listo para recibir prompts o comandos."
                        )

                    threading.Thread(target=do_restart, daemon=True).start()
                    return
        except Exception as e:
            print(f"[Supervisor] Payload inspection notice: {e}", flush=True)

        # Forward all normal webhooks to worker
        threading.Thread(target=forward_to_worker, args=(post_data,), daemon=True).start()

    def log_message(self, format, *args):
        pass  # Mute normal request logs


class ThreadingSupervisorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    print("=" * 60, flush=True)
    print(f"[Supervisor] AGY Bridge Watchdog & Supervisor starting...", flush=True)
    print(f"[Supervisor] Public Webhook Port: {SUPERVISOR_PORT} (WAHA -> Supervisor)", flush=True)
    print(f"[Supervisor] Internal Worker Port: {WORKER_PORT} (Supervisor -> agy_bridge.py)", flush=True)
    print(f"[Supervisor] Master Accounts: {MY_ACCOUNTS}", flush=True)
    print("=" * 60, flush=True)

    # 1. Start worker child process
    start_worker()

    # 2. Start independent watchdog health monitor
    watchdog_thread = threading.Thread(target=watchdog_loop, daemon=True)
    watchdog_thread.start()

    # 3. Start supervisor HTTP server on port 5055
    server_address = ("", SUPERVISOR_PORT)
    httpd = ThreadingSupervisorServer(server_address, SupervisorHandler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Supervisor] Stopping...", flush=True)
        stop_worker()
        httpd.server_close()


if __name__ == "__main__":
    main()
