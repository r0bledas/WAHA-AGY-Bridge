# AGY WhatsApp Bridge: Streaming Architecture & Adaptive Timeout Plan

**Document File:** `C:\waha\AGY_STREAMING_TIMEOUT_ANALYSIS_AND_PLAN.md`  
**Host Machine:** Lenovo ThinkPad T450 (`T450`, Windows 10 IoT Enterprise LTSC Build 19044)  
**Target Repository:** `r0bledas/WAHA-AGY-Bridge`  
**Component:** `agy_bridge.py` (WAHA WhatsApp <-> Google Antigravity CLI Bridge)  
**Date:** October 2026  

---

## 1. Executive Summary

The current implementation of `agy_bridge.py` operates under a **synchronous, monolithic batch architecture**:
1. When a WhatsApp message arrives, `run_agy_prompt()` executes `agy.exe` with redirected temporary files.
2. The bridge process calls `proc.wait(timeout=180)`, blocking the thread completely for up to 3 minutes.
3. During this entire window, the user receives zero feedback beyond a continuous WhatsApp typing indicator.
4. If an agent performs multi-step tool calls, deep reasoning, or browser operations that exceed 180 seconds, the bridge forcibly kills the process tree with `taskkill /F /T` and returns:
   `⚠️ AGY execution timed out after 3 minutes.`
5. All work performed during those 180 seconds is lost.

### Objective
Upgrade `agy_bridge.py` to an **event-driven, real-time streaming pipeline** that:
- Emits intermediate responses (initial thought, tool invocation, observation, and final conclusion) as separated, sequential WhatsApp messages as they happen.
- Replaces the blind, hardcoded 3-minute static timer with an **activity-based adaptive watchdog** that resets whenever AGY makes progress.
- Supports a configurable maximum execution ceiling via `.env` (e.g., 10 minutes) for heavy tasks without premature truncation.

---

## 2. Deep Dive: AGY Multi-Response Lifecycle & Streaming Engine

### 2.1 The Agentic Multi-Step Loop
When Antigravity processes a non-trivial prompt, it does not generate a single static block of text. Instead, it runs an internal multi-turn loop:

```
User Prompt
     │
     ▼
[Step 1: Reasoning & Initial Response]
  - Generates conversational text: "I'll inspect the configuration files in C:\waha..."
     │
     ▼
[Step 2: Tool Invocation]
  - Dispatches tool call (e.g. view_file, run_command, search_web)
  - Tool execution occurs on host machine
     │
     ▼
[Step 3: Tool Result / Observation]
  - Environment returns output to the model
     │
     ▼
[Step 4: Intermediate Synthesis / Next Tool]
  - Model processes output: "Found the configuration, now running git status..."
  - Dispatches next tool or begins final synthesis
     │
     ▼
[Step 5: Final Response / Result]
  - Consolidates final answer and signals turn completion
```

### 2.2 Discovery & Verification of `--output-format stream-json`
Antigravity CLI (`agy.exe`) natively supports streaming execution via the `--output-format stream-json` flag. 

When executed with this flag:
```cmd
agy.exe --dangerously-skip-permissions --output-format stream-json -p "<prompt>"
```
`agy.exe` emits **Newline-Delimited JSON (NDJSON)** lines to `stdout` in real time.

#### Actual Execution Event Trace Captured on T450:
```json
{"event":"init","conversation_id":"0ff9af83-...","init":{"cwd":"C:\\waha","tools":[...]}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":1,"state":"ACTIVE","step_type":"agent_response","text_delta":"I will inspect the workspace files..."}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":1,"state":"DONE","step_type":"agent_response","text_delta":"\n"}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"Get-ChildItem -Path C:\\waha"}}}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":2,"state":"DONE","step_type":"tool","tool_name":"run_command","duration_seconds":0.47,"tool_info":{"output":".env\nagy_bridge.py\n..."}}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":3,"state":"ACTIVE","step_type":"agent_response","text_delta":"Here are the files currently present in C:\\waha..."}}
{"event":"step_update","step_update":{"conversation_id":"0ff9...","step_index":3,"state":"DONE","step_type":"agent_response"}}
{"event":"result","result":{"conversation_id":"0ff9...","status":"SUCCESS","response":"Here are the files currently present in C:\\waha..."}}
```

#### Key Event Types:
1. **`step_type: "agent_response"` (`state: "ACTIVE"` / `"DONE"`)**:
   Contains `text_delta` streaming chunks of natural language text.
2. **`step_type: "tool"` (`state: "ACTIVE"`)**:
   Contains `tool_name` and `tool_info.parameters` (what tool AGY decided to run).
3. **`step_type: "tool"` (`state: "DONE"`)**:
   Contains execution duration and output.
4. **`event: "result"`**:
   Turn completion summary with full response and token metrics.

---

## 3. Analysis: Root Causes of the 3-Minute Timeout

### 3.1 The Hardcoded Trigger
In `C:\waha\agy_bridge.py` (lines 483-488):
```python
try:
    proc.wait(timeout=180)
except subprocess.TimeoutExpired:
    print(f"[Bridge] AGY process {proc.pid} timed out after 180s, killing process tree...", flush=True)
    kill_process_tree(proc.pid)
    return "⚠️ AGY execution timed out after 3 minutes."
```
This is a rigid, global ceiling. It measures elapsed wall-clock time from process spawn, completely unaware of whether the agent is making continuous progress or is actually deadlocked.

### 3.2 Four Main Reasons Legitimate Prompts Exceed 180 Seconds

#### Factor 1: Multi-Step Tool Chaining
Complex problem-solving requires sequential tool interactions (e.g. searching code, opening multiple files, executing a diagnostic command, and synthesizing).
- Each LLM reasoning turn: 10–25 seconds.
- Each tool execution: 5–20 seconds.
- 4 chained tool steps easily consume:
  $$\text{Total Time} \approx 4 \times (20\text{s LLM} + 15\text{s Tool}) = 140\text{s} - 200\text{s}$$
  The hardcoded 180s cutoff aborts the task right before the final answer is composed.

#### Factor 2: Deep Reasoning Models (Thinking Tokens)
When using reasoning models (such as `gemini-3.8-flash-high` or Claude with extended thinking), the model can produce 500 to 2,500 thinking tokens before emitting the first user-facing character. On complex queries, this pre-response phase alone can take 35 to 60 seconds.

#### Factor 3: Browser Automation & Heavy Document Parsing
Automated browser sessions via Playwright / Chromium (e.g. logging into portals like Nexus UANL, filling form fields, clicking modals, waiting for network idle, capturing DOM screenshots) are inherently high-latency:
- Launching headless Chromium: 5–8 seconds.
- Navigation and Cloudflare / SPA loading: 10–25 seconds per page.
- DOM inspection & interactions: 15–30 seconds.
A complete browser workflow routinely requires 2.5 to 4 minutes.

#### Factor 4: Blind Global Watchdog vs. Active Heartbeat
The static timer makes no distinction between:
- A frozen process stuck in an infinite loop or hanging socket (which *should* be killed).
- A healthy, productive process that just finished Step 4 and is currently typing Step 5 (which *should NOT* be killed).

---

## 4. Full Extensive Planned Solution Architecture

### 4.1 Architectural Diagram

```
                 [ WhatsApp Webhook (WAHA) ]
                              │
                              ▼
                   [ agy_bridge.py ]
                              │
         Spawns: agy.exe --output-format stream-json
                              │
                 ┌────────────┴────────────┐
                 │  stdout NDJSON Stream   │
                 ▼                         ▼
   [ Stream Line Parser ]        [ Activity Heartbeat ]
   - Extracts JSON events        - Resets inactivity timer
   - Reconstructs text chunks    - Checks max ceiling (600s)
                 │
                 ▼
   [ Chunk Aggregator & Debouncer ]
   - Intent Block  ──► send_whatsapp_message(target, "Voy a buscar X...")
   - Tool Event    ──► send_whatsapp_message(target, "⚙️ Ejecutando herramienta Y...")
   - Progress Step ──► send_whatsapp_message(target, "Encontre los datos, analizando...")
   - Final Result  ──► send_whatsapp_message(target, "Respuesta final...")
                 │
                 ▼
     [ WhatsApp (Delivered progressively in real time) ]
```

---

### 4.2 Component Breakdown

#### Component 1: Real-Time Stream Consumer (Non-Blocking Pipe)
Instead of writing to a temporary file and blocking with `proc.wait()`, `agy_bridge.py` creates a sub-process with `stdout=subprocess.PIPE` and `bufsize=1` (line-buffered):
- A dedicated background reader thread reads lines from `proc.stdout` as they arrive.
- Each line is decoded as UTF-8 and parsed with `json.loads()`.

#### Component 2: Step State Machine & Message Chunking
To prevent flooding WhatsApp with token-by-token noise, events are filtered into **Logical Conversational Chunks**:

| Chunk Type | Trigger Condition | Content Sent to WhatsApp |
| :--- | :--- | :--- |
| **1. Agent Intent** | `step_type == "agent_response"` completes (`DONE`) before a tool call | The natural language explanation of what AGY plans to do |
| **2. Tool Action** | `step_type == "tool"` initiates (`ACTIVE`) | Brief status banner: `⚙️ [Herramienta]: <tool_name>` (e.g., `view_file: C:\waha\agy_bridge.py`) |
| **3. Intermediate Reasoning** | `step_type == "agent_response"` completes after a tool call | The intermediate synthesis or next action plan |
| **4. Final Synthesis** | `event == "result"` or final response completion | The comprehensive answer, diff, or summary |

#### Component 3: Anti-Spam & Debouncing Guard
WhatsApp is a mobile messaging app. To maintain an excellent user experience:
- **Minimum Character Threshold:** Tiny 2-word deltas are buffered until sentence boundaries (`.`, `\n`) or step completion.
- **Minimum Interval Throttle:** A minimum delay of 1.2 seconds between consecutive WhatsApp messages prevents client notification spam.
- **Continuous Typing Synchronization:** `ContinuousTyping` remains active throughout the entire session so the chat header shows "typing..." uninterruptedly between messages.

#### Component 4: Adaptive Activity-Based Watchdog
The rigid 180s static timer is replaced by a dual-tier dynamic watchdog:

1. **Inactivity Watchdog (`INACTIVITY_LIMIT = 90s`):**
   - Every time a line is read from `proc.stdout`, `last_activity_time = time.time()` is updated.
   - If **no output or event** is received for 90 consecutive seconds, AGY is considered genuinely hung/deadlocked, and the process tree is killed.
2. **Global Maximum Ceiling (`MAX_TOTAL_TIMEOUT = 600s`):**
   - Configurable in `.env` (`AGY_MAX_PROMPT_TIMEOUT=600`).
   - Caps the absolute maximum execution time of any single prompt to 10 minutes to prevent infinite tasks.

---

## 5. Technical Implementation Blueprint

### 5.1 Configuration (`.env`)
```env
# Streaming & Execution Watchdog
AGY_STREAMING_ENABLED=True
AGY_INACTIVITY_TIMEOUT_SECONDS=90
AGY_MAX_PROMPT_TIMEOUT_SECONDS=600
AGY_NOTIFY_TOOL_CALLS=True
```

### 5.2 Python Implementation for `agy_bridge.py`

```python
import json
import os
import queue
import subprocess
import threading
import time

def stream_agy_prompt(prompt, target_chat, model=None, continue_session=False):
    """
    Executes agy.exe with --output-format stream-json, progressively
    parsing NDJSON events and dispatching separated responses to WhatsApp.
    """
    sync_gemini_md()
    
    cmd = [
        AGY_EXE if os.path.exists(AGY_EXE) else AGY_DANGER_CMD,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json"
    ]
    if model:
        cmd.extend(["--model", model])
    if continue_session:
        cmd.append("--continue")
    cmd.extend(["-p", prompt])

    print(f"[Bridge-Stream] Spawning: {' '.join(cmd[:5])} -p ...", flush=True)

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

    line_queue = queue.Queue()
    
    def stdout_reader():
        try:
            for line in iter(proc.stdout.readline, ""):
                line_queue.put(("STDOUT", line))
        except Exception:
            pass
        finally:
            proc.stdout.close()

    def stderr_reader():
        try:
            for line in iter(proc.stderr.readline, ""):
                line_queue.put(("STDERR", line))
        except Exception:
            pass
        finally:
            proc.stderr.close()

    t_out = threading.Thread(target=stdout_reader, daemon=True)
    t_err = threading.Thread(target=stderr_reader, daemon=True)
    t_out.start()
    t_err.start()

    start_time = time.time()
    last_activity = time.time()
    
    inactivity_timeout = int(os.environ.get("AGY_INACTIVITY_TIMEOUT_SECONDS", "90"))
    max_timeout = int(os.environ.get("AGY_MAX_PROMPT_TIMEOUT_SECONDS", "600"))
    notify_tools = os.environ.get("AGY_NOTIFY_TOOL_CALLS", "True").lower() == "true"

    current_response_buffer = []
    sent_messages = set()
    final_response = None
    has_sent_intermediate = False

    while True:
        now = time.time()
        
        # Check Watchdogs
        if now - last_activity > inactivity_timeout:
            print(f"[Bridge-Stream] Process hung (no output for {inactivity_timeout}s). Killing...", flush=True)
            kill_process_tree(proc.pid)
            send_whatsapp_message(target_chat, "⚠️ AGY se congelo (90s sin actividad) y fue cancelado.")
            return

        if now - start_time > max_timeout:
            print(f"[Bridge-Stream] Process exceeded maximum ceiling of {max_timeout}s. Killing...", flush=True)
            kill_process_tree(proc.pid)
            send_whatsapp_message(target_chat, f"⚠️ La tarea supero el tiempo maximo de {max_timeout // 60} minutos.")
            return

        try:
            stream_type, line = line_queue.get(timeout=0.25)
        except queue.Empty:
            if proc.poll() is not None and line_queue.empty():
                break
            continue

        last_activity = time.time()
        line = line.strip()
        if not line:
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

                    # When an agent conversational turn finishes
                    if state == "DONE":
                        chunk = "".join(current_response_buffer).strip()
                        current_response_buffer = []
                        if chunk and chunk not in sent_messages:
                            sent_messages.add(chunk)
                            has_sent_intermediate = True
                            send_whatsapp_message(target_chat, f"🤖 AGY:\n{chunk}")

                # 2. Tool Invocation Notifications
                elif step_type == "tool" and state == "ACTIVE" and notify_tools:
                    tool_name = step.get("tool_name", "herramienta")
                    tool_info = step.get("tool_info", {})
                    params = tool_info.get("parameters", {})
                    
                    # Create short human-readable summary
                    param_hint = ""
                    if "CommandLine" in params:
                        param_hint = f" ({params['CommandLine'][:40]}...)"
                    elif "TargetFile" in params:
                        param_hint = f" ({os.path.basename(params['TargetFile'])})"
                    elif "query" in params:
                        param_hint = f" ('{params['query'][:35]}')"

                    send_whatsapp_message(target_chat, f"⚙️ Ejecutando: {tool_name}{param_hint}")

            # 3. Final Result Event
            elif event == "result":
                res = data.get("result", {})
                final_response = res.get("response", "").strip()

    proc.wait(timeout=5)
    kill_process_tree(proc.pid)

    # If no intermediate chunks were sent, send the final response
    if final_response and final_response not in sent_messages:
        send_whatsapp_message(target_chat, f"🤖 AGY:\n{final_response}")
    elif not has_sent_intermediate and not final_response:
        send_whatsapp_message(target_chat, "🤖 AGY termino la ejecucion sin generar salida de texto.")
```

---

## 6. Comparison: Current vs. Planned Architecture

| Dimension | Current Architecture | Planned Streaming Architecture |
| :--- | :--- | :--- |
| **Execution Mode** | Monolithic blocking (`proc.wait(180)`) | Event-driven NDJSON streaming |
| **Intermediate Feedback** | None (only typing indicator) | Progressive messages (Intent, Tool, Output, Conclusion) |
| **Timeout Model** | Rigid 180-second static ceiling | Adaptive 90s inactivity heartbeat + 10m ceiling |
| **Heavy Task Handling** | Fails on multi-tool or browser workflows | Seamlessly supports 5–10 minute complex workflows |
| **User Transparency** | Black box until completion or crash | Full visibility into agent thoughts and tool actions |
| **Crash Recovery** | `///restart` hard kill | Native progress stream + `///restart` backup |

---

## 7. Rollout & Validation Strategy (When Ready)

1. **Branch / Test Verification:**
   - Implement the `stream_agy_prompt()` logic in `C:\waha\agy_bridge.py`.
   - Compile cleanly with `python -m py_compile C:\waha\agy_bridge.py`.
2. **Single-Tool Verification:**
   - Send prompt: `"Busca que archivos hay en C:\waha y listalos"`
   - Verify WhatsApp receives:
     1. Preliminary explanation.
     2. `⚙️ Ejecutando: run_command...`
     3. Formatted file listing.
3. **Multi-Step & Heavy Task Verification:**
   - Send prompt requiring 3+ tools or browser work.
   - Verify execution extends beyond 180 seconds cleanly without timeout termination.
4. **Git Commit & Deployment:**
   - Commit changes to `origin main`.
   - Reload via `///restart`.
