"""
Nova performance benchmark (local, safe, repeatable).

    python tools\\benchmark_nova.py [--skip-model] [--skip-speech] [--out results.json]

Measures startup phases, GUI event-loop lag (how long the window can't
respond) during heavy work, idle CPU, memory, database queries, document
indexing, speech recognition and model latency.

It never touches your data: everything runs against a temporary database
seeded with a large synthetic history. No apps are launched and nothing is
downloaded. The model test only uses a model that is already installed.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
RESULTS = {}


def record(name, value, unit="ms"):
    RESULTS[name] = {"value": round(value, 1) if isinstance(value, float) else value, "unit": unit}
    print(f"  {name:<52} {RESULTS[name]['value']:>10} {unit}")


def rss_mb():
    import nova_system
    if hasattr(nova_system, "working_set_mb"):
        return nova_system.working_set_mb() or 0.0
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t),
                    ("d", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("e", ctypes.c_size_t)]
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return counters.WorkingSetSize / 2 ** 20


# ==================== 1. imports (fresh processes) ====================
def bench_imports():
    print("\n[imports]")
    code = "import time; t=time.perf_counter(); import nova_gui; print((time.perf_counter()-t)*1000)"
    runs = []
    for _ in range(3):
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        runs.append(float(out.stdout.strip().splitlines()[-1]))
    record("import nova_gui (median of 3 fresh processes)", statistics.median(runs))
    out = subprocess.run([sys.executable, "-X", "importtime", "-c", "import nova_gui"], cwd=ROOT,
                         capture_output=True, text=True)
    top = {}
    for line in out.stderr.splitlines():
        parts = line.split("|")
        if len(parts) == 3 and parts[0].startswith("import time:") and parts[1].strip().isdigit():
            name = parts[2].strip()
            if not name.startswith(" ") and "." not in name.strip():
                top[name.strip()] = int(parts[1]) / 1000
    for name, ms in sorted(top.items(), key=lambda kv: -kv[1])[:8]:
        record(f"  import {name} (cumulative)", ms)


# ==================== synthetic data ====================
def seed(db_path):
    import assistant
    import nova_gui
    import nova_memories as nm
    import nova_reminders as nr
    from datetime import datetime, timedelta
    random.seed(7)
    assistant.initialize_db()
    nova_gui.initialize_productivity_tables()
    nova_gui.initialize_document_tables()
    nova_gui.initialize_task_tables()
    words = ("python model reminder memory window search folder report budget meeting travel music recipe "
             "network audio microphone notes project deadline invoice garden coffee library").split()
    con = assistant.connect_db()
    stamp = datetime.now().astimezone()
    with con:
        for s in range(200):
            when = (stamp - timedelta(days=200 - s)).isoformat(timespec="seconds")
            con.execute("INSERT INTO sessions (title, created_at, updated_at) VALUES (?, ?, ?)",
                        (f"Chat {s} about {random.choice(words)}", when, when))
        rows = []
        for i in range(20000):
            session = 200 if i >= 18000 else random.randint(1, 199)   # the newest chat has 2,000 messages
            text = " ".join(random.choice(words) for _ in range(random.randint(10, 120)))
            if i % 2:
                text = f"## Notes\n- {text}\n- **{random.choice(words)}** `{random.choice(words)}`\n\n{text}"
            rows.append((session, "user" if i % 2 == 0 else "assistant", text, stamp.isoformat(timespec="seconds")))
        con.executemany("INSERT INTO messages (session_id, role, content, created_at) VALUES (?, ?, ?, ?)", rows)
        con.execute("UPDATE sessions SET updated_at = ? WHERE id = 200", (stamp.isoformat(timespec="seconds"),))
    for i in range(200):
        nm.add_memory(con, f"Fact {i}: likes {random.choice(words)} and {random.choice(words)}", random.choice(
            ["preference", "profile", "work", "health"]))
    for i in range(50):
        nr.create(con, f"Reminder {i}", nr.Schedule(datetime.now() + timedelta(days=i + 1)))
    con.close()


# ==================== 2. startup + GUI ====================
class LagMeter:
    """Schedules a 10 ms tick on the Tk loop and records how late each tick runs."""

    def __init__(self, root):
        self.root, self.lags, self.running, self.expected = root, [], False, 0

    def start(self):
        self.lags, self.running = [], True
        self.expected = time.perf_counter() + 0.01
        self.root.after(10, self._tick)

    def _tick(self):
        now = time.perf_counter()
        self.lags.append((now - self.expected) * 1000)
        if self.running:
            self.expected = now + 0.01
            self.root.after(10, self._tick)

    def stop(self):
        self.running = False
        lags = sorted(self.lags) or [0]
        return max(lags), lags[int(len(lags) * 0.95) - 1 if len(lags) > 1 else 0]


def run_loop(root, seconds=None, until=None):
    """Run the real Tk mainloop (as Nova does) for some seconds, or until until() is true."""
    started = time.perf_counter()

    def check():
        if (seconds is not None and time.perf_counter() - started >= seconds) or (until is not None and until()):
            root.quit()
        else:
            root.after(20, check)

    root.after(20, check)
    root.mainloop()


def pump(root, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        root.update()
        time.sleep(0.002)


def bench_gui(tmp, skip_model):
    print("\n[startup and responsiveness]")
    import assistant
    db = tmp / "nova_memory.db"
    patches = [mock.patch.object(assistant, "DB_PATH", db)]
    for p in patches:
        p.start()
    seed(db)
    import customtkinter as ctk
    import nova_gui
    for p in (mock.patch.object(nova_gui, "SETTINGS_PATH", tmp / "settings.json"),
              mock.patch.object(nova_gui, "load_tray_dependencies", return_value=None),
              mock.patch.object(nova_gui, "BACKUP_DIR", tmp / "backups"),
              # the GUI section never calls the model (chat summaries would, after the simulated reply)
              mock.patch.object(nova_gui.NovaGUI, "_maybe_summarize", lambda self: None)):
        p.start()

    started = time.perf_counter()
    nova_gui.initialize_database()
    nova_gui.initialize_productivity_tables()
    nova_gui.initialize_task_tables()
    nova_gui.initialize_document_tables()
    record("database setup at startup", (time.perf_counter() - started) * 1000)
    ctk.set_appearance_mode("dark")
    started = time.perf_counter()
    root = ctk.CTk()
    app = nova_gui.NovaGUI(root)
    built = time.perf_counter()
    root.update()
    shown = time.perf_counter()
    record("NovaGUI() construction (2,000-message chat open)", (built - started) * 1000)
    record("window ready (construction + first paint)", (shown - started) * 1000)
    record("memory after startup (working set)", rss_mb(), "MB")

    meter = LagMeter(root)
    cpu, wall = time.process_time(), time.perf_counter()
    run_loop(root, 8)
    record("idle: CPU use over 8 s (real mainloop)", (time.process_time() - cpu) / (time.perf_counter() - wall) * 100,
           "% of one core")
    # The first window includes one-off startup work in background threads
    # (import prewarm, document refresh). The once-a-day automatic backup starts
    # 15 s after startup; wait for it, then measure steady idle.
    backups = tmp / "backups"
    meter.start()
    run_loop(root, until=lambda: time.perf_counter() - started > 60 or (
        backups.exists() and any(backups.glob("*.novabackup")) and not getattr(app, "_backup_running", False)
        and time.perf_counter() - started > 18))
    worst, _p95 = meter.stop()
    record("daily automatic backup (15 s after start): worst event-loop lag", worst)
    run_loop(root, 2)
    cpu, wall = time.process_time(), time.perf_counter()
    run_loop(root, 10)
    record("idle: steady CPU use (10 s, after startup work settles)",
           (time.process_time() - cpu) / (time.perf_counter() - wall) * 100, "% of one core")
    meter.start()
    run_loop(root, 3)
    worst, p95 = meter.stop()
    record("idle: worst event-loop lag", worst)

    # Switching to a long conversation and back
    session_short = 5
    started = time.perf_counter()
    app.session_id = session_short
    app.load_chat()
    root.update()
    record("open a short chat (~90 messages)", (time.perf_counter() - started) * 1000)
    started = time.perf_counter()
    app.session_id = 200
    app.load_chat()
    root.update()
    record("open the 2,000-message chat (UI blocked)", (time.perf_counter() - started) * 1000)

    if hasattr(app, "_show_earlier"):
        started = time.perf_counter()
        app._show_earlier()
        root.update()
        record("'Show earlier messages' (+120 in the long chat)", (time.perf_counter() - started) * 1000)
        app.load_chat()
        root.update()
    con = assistant.connect_db()
    old_id = con.execute("SELECT MIN(id) + 50 FROM messages WHERE session_id = 200").fetchone()[0]
    con.close()
    started = time.perf_counter()
    app.open_search_result(200, old_id, ["budget"])
    root.update()
    record("open a search result 1,900 messages back", (time.perf_counter() - started) * 1000)

    # A reply streaming in at 25 tokens/s for 8 s, into the 2,000-message chat (worst case)
    def feed():
        app.events.put(("assistant_start", None))
        for i in range(200):
            app.events.put(("token", f"word{i} "))
            time.sleep(0.04)
        app.events.put(("generation_done", None))

    app.busy = True
    meter.start()
    cpu, wall = time.process_time(), time.perf_counter()
    feeder = threading.Thread(target=feed)
    feeder.start()
    run_loop(root, until=lambda: not feeder.is_alive() and app.events.empty() and not app.busy)
    worst, p95 = meter.stop()
    record("stream 200 tokens at 25/s into the long chat: total", (time.perf_counter() - wall) * 1000)
    record("  CPU used by the GUI while streaming", (time.process_time() - cpu) / (time.perf_counter() - wall) * 100,
           "% of one core")
    record("  worst event-loop lag while streaming", worst)
    record("  95th percentile lag while streaming", p95)
    started = time.perf_counter()
    app.refresh_sessions(select_current=True)
    root.update()
    record("sidebar refresh after a reply (200 chats)", (time.perf_counter() - started) * 1000)

    # Settings dialog
    started = time.perf_counter()
    app.open_startup_settings()
    root.update()
    record("open Settings (until drawn)", (time.perf_counter() - started) * 1000)
    app._settings_window.destroy()
    app.busy = False

    # Search window typing
    started = time.perf_counter()
    app.open_search()
    root.update()
    record("open Search window", (time.perf_counter() - started) * 1000)
    app._search_window.destroy()

    # Indexing a long document in the background while measuring lag
    from doc_samples import make_pdf
    folder = tmp / "docs"
    folder.mkdir()
    pages = [f"Page {p}: " + " ".join(random.choice(["alpha", "budget", "zephyr", "meeting", "handbook", "leave"])
                                       for _ in range(260)) for p in range(300)]
    make_pdf(folder / "long.pdf", ["\n".join(text[i:i + 90] for i in range(0, len(text), 90)) for text in pages])
    (folder / "big.txt").write_text("\n\n".join(" ".join(random.choice(["lorem", "ipsum", "nova", "budget"])
                                                        for _ in range(150)) for _ in range(6000)), encoding="utf-8")
    meter.start()
    started = time.perf_counter()
    app.indexer.add_paths([folder])
    run_loop(root, until=lambda: not app.indexer.busy)
    elapsed = time.perf_counter() - started
    worst, p95 = meter.stop()
    record("index 300-page PDF + 5 MB text (background)", elapsed * 1000)
    record("  worst event-loop lag while indexing", worst)
    record("  95th percentile lag while indexing", p95)
    record("memory after indexing (working set)", rss_mb(), "MB")

    # Database operations used on every message / view
    con = assistant.connect_db()
    timings = {
        "fetch_sessions (sidebar)": lambda: nova_gui.fetch_sessions(),
        "chat context for one reply": (lambda: nova_gui.recent_messages(200, 12)) if hasattr(nova_gui, "recent_messages")
        else (lambda: nova_gui.fetch_messages(200)[-12:]),
        "memory selection for one reply": lambda: nova_gui.system_prompt("budget meeting python"),
        "search 'budget meeting' (first page)": lambda: nova_gui.search_conversations("budget meeting"),
        "reminder poll (claim_due)": lambda: nova_gui.with_db(__import__("nova_reminders").claim_due),
        "document retrieval for a question": lambda: __import__("nova_documents").retrieve(con, "zephyr budget handbook"),
        "new chat emptiness check": (lambda: nova_gui.has_messages(200)) if hasattr(nova_gui, "has_messages")
        else (lambda: nova_gui.fetch_messages(200)),
    }
    if hasattr(nova_gui, "build_context"):     # Step 24: everything gathered for one reply
        import nova_context
        timings["context for a plain message"] = lambda: nova_gui.build_context(
            200, "Explain how a budget meeting should be run", nova_context.Budget())
        timings["context for a recall question"] = lambda: nova_gui.build_context(
            200, "What did we decide about the garden project last time?", nova_context.Budget())
        timings["context for a document question"] = lambda: nova_gui.build_context(
            200, "According to my documents, what does the handbook say about zephyr leave?", nova_context.Budget())
    for name, fn in timings.items():
        runs = []
        for _ in range(5):
            started = time.perf_counter()
            fn()
            runs.append((time.perf_counter() - started) * 1000)
        record(f"db: {name}", statistics.median(runs))
    con.close()

    # Shutdown
    started = time.perf_counter()
    app.exit_application(force=True)
    record("shutdown (exit_application)", (time.perf_counter() - started) * 1000)
    threads = [t.name for t in threading.enumerate() if t.name.startswith("Nova") and t.is_alive()]
    record("Nova threads still alive after shutdown", len(threads), "threads")


# ==================== 3. speech ====================
def bench_speech(tmp):
    print("\n[speech]")
    import assistant
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import resample_poly
    wav = tmp / "speech.wav"
    import pyttsx3
    engine = pyttsx3.init()
    engine.save_to_file("Remind me tomorrow at nine in the morning to study the chapter on databases, "
                        "and then open my downloads folder and search for the invoice from March.", str(wav))
    engine.runAndWait()
    rate, audio = wavfile.read(wav)
    if audio.ndim > 1:
        audio = audio[:, 0]
    audio = resample_poly(audio.astype(np.float32), 16000, rate).astype(np.int16)
    record("test utterance length", len(audio) / 16000 * 1000)
    started = time.perf_counter()
    text = assistant.transcribe_audio(audio, lambda _s: None)
    record("transcribe (whisper small.en, as configured)", (time.perf_counter() - started) * 1000)
    print("    heard:", text[:90])
    try:
        from vosk import Model
        model_path = __import__("nova_gui").find_vosk_model()
        if model_path:
            started = time.perf_counter()
            Model(str(model_path))
            record("load Vosk wake-word model", (time.perf_counter() - started) * 1000)
    except Exception as exc:
        print("    vosk:", exc)
    started = time.perf_counter()
    import nova_settings
    nova_settings.list_input_devices()
    record("enumerate microphones", (time.perf_counter() - started) * 1000)


# ==================== 4. model ====================
def bench_model():
    print("\n[model]")
    import ollama
    import nova_settings
    models = nova_settings.list_ollama_models(timeout=5)
    if not models:
        print("    no models installed; skipped")
        return
    model = "gemma3:4b" if "gemma3:4b" in models else models[0]
    loaded = [m.model for m in ollama.ps().models]
    messages = [{"role": "user", "content": "In two sentences, what is SQLite good for?"}]
    for label in (("cold (model not loaded)" if model not in loaded else "first request"), "warm"):
        started = time.perf_counter()
        first, tokens = None, 0
        for chunk in ollama.chat(model=model, messages=messages, stream=True, options={"num_ctx": 4096, "temperature": 0}):
            if chunk["message"]["content"]:
                tokens += 1
                if first is None:
                    first = time.perf_counter() - started
            if chunk.get("done"):
                stats = chunk
        total = time.perf_counter() - started
        record(f"model {label}: time to first token", first * 1000)
        record(f"model {label}: total", total * 1000)
        if stats.get("eval_count") and stats.get("eval_duration"):
            record(f"model {label}: generation speed", stats["eval_count"] / (stats["eval_duration"] / 1e9), "tokens/s")
        if stats.get("load_duration"):
            record(f"model {label}: Ollama load time", stats["load_duration"] / 1e6)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-speech", action="store_true")
    parser.add_argument("--only", choices=["imports", "gui", "speech", "model"])
    parser.add_argument("--out")
    args = parser.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="nova_bench_"))
    try:
        if args.only in (None, "imports"):
            bench_imports()
        if args.only in (None, "gui"):
            bench_gui(tmp, args.skip_model)
        if args.only in (None, "speech") and not args.skip_speech:
            bench_speech(tmp)
        if args.only in (None, "model") and not args.skip_model:
            bench_model()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if args.out:
        Path(args.out).write_text(json.dumps(RESULTS, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
