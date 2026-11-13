"""
Nova settings: persistence, validation, logging and local device/model discovery.

This module holds no GUI code, so it can be tested on its own. Settings live in
nova_settings.json beside the app. A value of None means "use the default from
assistant.py", so a settings file written by an older Nova keeps working and
unknown keys are preserved.

Everything here is local: the only network call is to the Ollama server on this
PC (http://localhost:11434 unless OLLAMA_HOST says otherwise).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "nova_settings.json"
LOG_PATH = BASE_DIR / "nova.log"

log = logging.getLogger("nova")

WARMUP_CHOICES = ("typing", "startup", "off")
CONTEXT_SIZE_CHOICES = ("compact", "standard", "large")   # 4K, 8K, 16K tokens (see nova_context)
TTS_RATE_MIN = 80
TTS_RATE_MAX = 300
MAX_TEXT_SETTING = 300
# Voice input records 16 kHz mono int16 (assistant.SAMPLE_RATE); only devices
# that accept this format are offered.
RECORD_SAMPLE_RATE = 16000

DEFAULTS = {
    "minimize_to_tray": True,
    "ollama_model": None,        # None: assistant.MODEL
    "tts_voice": None,           # None: Windows' default voice
    "tts_rate": None,            # None: assistant.TTS_RATE (words per minute)
    "tts_volume": 1.0,           # 0.0 – 1.0
    "microphone_name": None,     # None: assistant.MICROPHONE_DEVICE
    "microphone_hostapi": None,  # e.g. "MME"; disambiguates same-named devices
    "wake_word_enabled": False,  # start "Hey Nova" listening when Nova opens
    "memory_suggestions_enabled": False,  # suggest memories from your messages (opt-in)
    "model_warmup": "typing",    # load the model in Ollama: typing | startup | off
    "log_performance": False,    # also write performance timings to nova.log
    "context_size": "compact",   # model context window: compact | standard | large
    "summarize_long_chats": True,  # keep a local summary of long chats for the model
    "project_view": "all",       # sidebar: "all", "none" (chats without a project) or a project id
    "approved_folders": [],      # folders tasks may create, copy, move, rename or recycle files in
    "online_enabled": False,     # optional online integrations (web:, wiki:, weather in …); off by default
    "online_confirm": True,      # ask before every online request (personal-looking text always asks)
    "online_providers": {},      # provider key -> False to switch one off (missing: on)
    "online_contact": "",        # email or web address you choose, sent only to providers whose rules ask for it
    "agent_max_steps": 10,       # agent engine: most steps one request may run (including replanned ones)
    "agent_max_retries": 1,      # retries for a step that failed for a temporary reason
    "agent_max_replans": 1,      # new plans after a step fails
    "agent_time_limit": 300,     # seconds a request may take in total
    "agent_model_timeout": 120,  # seconds to wait for the model to plan or answer
    # Desktop and browser automation (Step 30)
    "desktop_automation": True,  # the agent may focus, read, type into, click in and close approved apps
    "browser_automation": False,  # the agent may drive its own browser window (needs Playwright); off by default
    # Security and autonomy controls (Step 34).
    "tasks_paused": False,          # nothing new starts (typed, spoken, agent, task: or scheduled) until resumed
    "approval_ttl_minutes": 10,     # a plan approval covers changes for this long; later changes ask again
    # Scheduled tasks, watches and notifications (Step 33).
    "automation_enabled": True,     # run scheduled tasks and watches (each one is created and approved by you)
    "quiet_hours_enabled": False,   # hold back sound, speech and pop-ups between quiet_start and quiet_end
    "quiet_start": "22:00",
    "quiet_end": "07:00",
    # Memory and context retrieval (Step 32). Everything stays in nova_memory.db on this PC.
    "memory_recall_enabled": True,   # recall decisions, progress, blockers and other chats' summaries when relevant
    "memory_record_outcomes": True,  # keep what agent tasks verifiably did (and what's left) for next time
    # Visual awareness (Step 31). Screenshots stay in memory on this PC and are never saved.
    "vision_enabled": True,      # Nova may look at the screen when you ask (or an approved agent step needs to)
    "vision_agent_checks": True,  # a quick look at an app's window before typing/clicking and after consequential steps
    "vision_ocr": True,          # read screen text with Windows' built-in OCR
    "vision_use_model": True,    # let a local image model (Ollama, “vision” capability) interpret screenshots
    "vision_model": None,        # None: the chat model if it can read images, else any installed one that can
    "vision_allow_remote": False,  # send screenshots to an Ollama server on another computer (off: never)
    "vision_exclusions": ["KeePass", "1Password", "Bitwarden", "LastPass", "Dashlane", "Keeper",
                          "Credential Manager", "Windows Security", "Password"],   # windows always blacked out
    # Hands-free voice (Step 29). Wake-word detection is always local (Vosk); audio is never saved.
    "wake_phrase": "hey nova",   # 1–4 words the local wake-word model knows
    "wake_sensitivity": "medium",  # low | medium | high: how readily the wake phrase is accepted
    "voice_route": "agent",      # where hands-free requests go: agent | chat
    "voice_barge_in": True,      # saying the wake phrase stops Nova speaking
    "voice_wake_sound": True,    # a short beep when Nova starts listening
    "voice_confirm_transcript": True,  # ask “Send this to Nova?” before a spoken message is sent
    "voice_listen_timeout": 6,   # seconds to start speaking after the wake phrase
    "voice_end_silence": 1.2,    # seconds of silence that end a spoken request
    "voice_max_utterance": 20,   # longest spoken request, in seconds
}
AGENT_LIMITS = {"agent_max_steps": (1, 25), "agent_max_retries": (0, 3), "agent_max_replans": (0, 3),
                "agent_time_limit": (30, 1800), "agent_model_timeout": (20, 600), "approval_ttl_minutes": (1, 120)}
VOICE_LIMITS = {"voice_listen_timeout": (2, 30), "voice_end_silence": (0.5, 4.0), "voice_max_utterance": (5, 60)}
WAKE_SENSITIVITY_CHOICES = ("low", "medium", "high")
VOICE_ROUTE_CHOICES = ("agent", "chat")
MAX_APPROVED_FOLDERS = 50


# -------------------- logging --------------------
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3          # nova.log + nova.log.1..3: at most ~4 MB in total
LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"


def setup_logging(path=LOG_PATH, level=logging.INFO):
    """Log to a rotating file beside the app, with timestamps, levels and thread names.

    Chat, memory and document text is never logged on purpose, and every
    record passes through a redactor that masks keys, tokens, passwords,
    emails and long numbers and caps its length. Nova is often started with
    pythonw.exe (no console), so the file is where errors show up.
    """
    from nova_system import install_exception_hooks

    if any(getattr(h, "_nova_handler", False) for h in log.handlers):
        return log
    log.setLevel(level)
    log.addHandler(make_log_handler(path))
    install_exception_hooks(log)
    return log


def make_log_handler(path, max_bytes=LOG_MAX_BYTES, backups=LOG_BACKUPS):
    """A size-capped, rotating, redacting log file handler."""
    from nova_system import RedactingFormatter

    try:
        handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True)
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT))
    handler._nova_handler = True
    return handler


# -------------------- validation --------------------
def _as_bool(key, value, warnings):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    warnings.append(f"'{key}' must be true or false; using the default.")
    return DEFAULTS[key]


def _as_text(key, value, warnings):
    if value is None:
        return None
    if not isinstance(value, str):
        warnings.append(f"'{key}' must be text; using the default.")
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_TEXT_SETTING or any(ord(ch) < 32 for ch in text):
        warnings.append(f"'{key}' is not a valid value; using the default.")
        return None
    return text


def _as_number(key, value, low, high, warnings, integer=False):
    if value is None:
        return None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except ValueError:
            number = None
    if number is None or number != number:  # NaN check
        warnings.append(f"'{key}' must be a number; using the default.")
        return DEFAULTS[key]
    clamped = min(high, max(low, number))
    if clamped != number:
        warnings.append(f"'{key}' was outside {low}–{high}; adjusted to {clamped:g}.")
    return int(round(clamped)) if integer else round(clamped, 2)


def _approved_folders(value, warnings):
    """Full local folder paths only (checked again, more strictly, whenever a task uses one)."""
    if not isinstance(value, list):
        warnings.append("'approved_folders' must be a list of folder paths; no folders are allowed.")
        return []
    folders = []
    for item in value:
        text = str(item).strip() if isinstance(item, str) else ""
        if (not text or len(text) > 1024 or any(ord(c) < 32 for c in text) or text.startswith(("\\\\", "//"))
                or len(text) < 3 or not text[0].isalpha() or text[1:3] not in (":\\", ":/")):
            warnings.append(f"Ignored an approved folder that isn't a full local path: {text[:80]!r}.")
            continue
        if text.lower() not in (f.lower() for f in folders):
            folders.append(text)
    if len(folders) > MAX_APPROVED_FOLDERS:
        warnings.append(f"Only the first {MAX_APPROVED_FOLDERS} approved folders are used.")
    return folders[:MAX_APPROVED_FOLDERS]


def validate_settings(raw):
    """Return (clean_settings, warnings).

    Known keys are type-checked and clamped; invalid values fall back to their
    defaults. Unknown keys are kept as-is so nothing a user (or a future Nova)
    wrote is lost.
    """
    warnings = []
    if not isinstance(raw, dict):
        return dict(DEFAULTS), ["Settings must be a JSON object; using defaults."]

    clean = {k: v for k, v in raw.items() if k not in DEFAULTS}
    clean.update(DEFAULTS)
    if "model_warmup" in raw:
        value = str(raw["model_warmup"]).strip().lower()
        if value in WARMUP_CHOICES:
            clean["model_warmup"] = value
        else:
            warnings.append("'model_warmup' must be typing, startup or off; using the default.")
    if "context_size" in raw:
        value = str(raw["context_size"]).strip().lower()
        if value in CONTEXT_SIZE_CHOICES:
            clean["context_size"] = value
        else:
            warnings.append("'context_size' must be compact, standard or large; using the default.")
    if "project_view" in raw:
        value = raw["project_view"]
        if value in ("all", "none") or (isinstance(value, int) and not isinstance(value, bool) and value > 0):
            clean["project_view"] = value
        else:
            warnings.append("'project_view' must be all, none or a project number; showing all chats.")
    if "approved_folders" in raw:
        clean["approved_folders"] = _approved_folders(raw["approved_folders"], warnings)
    for key, choices in (("wake_sensitivity", WAKE_SENSITIVITY_CHOICES), ("voice_route", VOICE_ROUTE_CHOICES)):
        if key in raw:
            value = str(raw[key]).strip().lower()
            if value in choices:
                clean[key] = value
            else:
                warnings.append(f"'{key}' must be {', '.join(choices[:-1])} or {choices[-1]}; using the default.")
    if "wake_phrase" in raw:
        phrase = " ".join(str(raw["wake_phrase"] or "").lower().split())
        if phrase and len(phrase) <= 40 and len(phrase.split()) <= 4 and all(w.isalpha() for w in phrase.split()):
            clean["wake_phrase"] = phrase
        else:
            warnings.append("'wake_phrase' must be 1–4 plain words; using “hey nova”.")
    if "online_contact" in raw:
        contact = raw["online_contact"]
        text = contact.strip() if isinstance(contact, str) else ""
        if text and (len(text) > 200 or any(ord(c) < 32 for c in text)):
            warnings.append("'online_contact' must be a short email or web address; it was cleared.")
            text = ""
        clean["online_contact"] = text
    if "online_providers" in raw:
        value = raw["online_providers"]
        if isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, bool) for k, v in value.items()):
            clean["online_providers"] = {k[:40]: v for k, v in list(value.items())[:20]}
        else:
            warnings.append("'online_providers' must map provider names to true or false; using the defaults.")
    for key in ("minimize_to_tray", "wake_word_enabled", "memory_suggestions_enabled", "log_performance",
                "summarize_long_chats", "online_enabled", "online_confirm", "voice_barge_in", "voice_wake_sound",
                "voice_confirm_transcript", "desktop_automation", "browser_automation", "vision_enabled",
                "vision_agent_checks", "vision_ocr", "vision_use_model", "vision_allow_remote",
                "memory_recall_enabled", "memory_record_outcomes", "automation_enabled", "quiet_hours_enabled",
                "tasks_paused"):
        if key in raw:
            clean[key] = _as_bool(key, raw[key], warnings)
    for key in ("quiet_start", "quiet_end"):
        if key in raw:
            value = str(raw[key] or "").strip()
            if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
                clean[key] = value
            else:
                warnings.append(f"'{key}' must be a time like 22:00; using the default.")
    if "vision_exclusions" in raw:
        value = raw["vision_exclusions"]
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            words = [" ".join(v.split())[:60] for v in value if v.strip() and not any(ord(c) < 32 for c in v)]
            clean["vision_exclusions"] = list(dict.fromkeys(words))[:40]
        else:
            warnings.append("'vision_exclusions' must be a list of app or window names; using the defaults.")
    for key in ("ollama_model", "tts_voice", "microphone_name", "microphone_hostapi", "vision_model"):
        if key in raw:
            clean[key] = _as_text(key, raw[key], warnings)
    for key, (low, high) in AGENT_LIMITS.items():
        if key in raw:
            value = _as_number(key, raw[key], low, high, warnings, integer=True)
            clean[key] = DEFAULTS[key] if value is None else value
    for key, (low, high) in VOICE_LIMITS.items():
        if key in raw:
            value = _as_number(key, raw[key], low, high, warnings)
            clean[key] = DEFAULTS[key] if value is None else value
    if "tts_rate" in raw:
        clean["tts_rate"] = _as_number("tts_rate", raw["tts_rate"], TTS_RATE_MIN, TTS_RATE_MAX, warnings, integer=True)
    if "tts_volume" in raw:
        volume = _as_number("tts_volume", raw["tts_volume"], 0.0, 1.0, warnings)
        clean["tts_volume"] = DEFAULTS["tts_volume"] if volume is None else volume
    if clean["microphone_name"] is None:
        clean["microphone_hostapi"] = None
    return clean, warnings


# -------------------- persistence --------------------
def _preserve_unreadable(path):
    """Copy an unreadable settings file aside before Nova ever overwrites it."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.unreadable-{stamp}{path.suffix}")
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError as exc:
        log.error("Could not back up unreadable settings file %s: %s", path, exc)
        return None


def load_settings(path=SETTINGS_PATH):
    """Read and validate settings. Never raises; returns (settings, warnings)."""
    path = Path(path)
    if not path.exists():
        return dict(DEFAULTS), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.error("Could not read settings file %s: %s", path, exc)
        backup = _preserve_unreadable(path)
        note = f" A copy was saved as {backup.name}." if backup else ""
        return dict(DEFAULTS), [f"Settings file couldn't be read, so defaults are in use.{note}"]
    settings, warnings = validate_settings(raw)
    for warning in warnings:
        log.warning("Settings: %s", warning)
    return settings, warnings


def save_settings(settings, path=SETTINGS_PATH):
    """Validate and write settings atomically. Returns the saved settings.

    Writes to a temporary file and renames it over the old one, so a crash
    mid-write can't leave a half-written settings file. Raises OSError.
    """
    path = Path(path)
    clean, warnings = validate_settings(settings)
    for warning in warnings:
        log.warning("Settings (save): %s", warning)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    log.info("Settings saved to %s", path)
    return clean


# -------------------- Ollama models --------------------
class OllamaUnavailable(RuntimeError):
    """Ollama couldn't be reached; the message is suitable for the user."""


def list_ollama_models(timeout=5.0, client=None):
    """Return the names of locally installed models that can chat, sorted.

    Models whose metadata says they only produce embeddings are left out.
    Raises OllamaUnavailable if the local Ollama server can't be reached.
    """
    if client is None:
        try:
            import ollama
        except Exception as exc:
            raise OllamaUnavailable(f"The ollama Python package isn't installed: {exc}") from exc
        client = ollama.Client(timeout=timeout)
        try:
            return list_ollama_models(timeout, client)
        finally:
            client.close()   # don't leave its connections open until garbage collection
    try:
        response = client.list()
    except Exception as exc:
        raise OllamaUnavailable(
            "Couldn't reach Ollama on this PC. Start the Ollama app, then try again."
        ) from exc

    models = getattr(response, "models", None)
    if models is None and isinstance(response, dict):
        models = response.get("models")
    names = []
    for item in models or []:
        name = getattr(item, "model", None)
        if name is None and isinstance(item, dict):
            name = item.get("model") or item.get("name")
        if name:
            names.append(str(name))

    chat_models = []
    for name in sorted(set(names), key=str.lower):
        try:
            capabilities = getattr(client.show(name), "capabilities", None)
        except Exception:
            capabilities = None  # unknown: offer it rather than hide it
        if capabilities and "completion" not in capabilities:
            log.info("Skipping non-chat Ollama model %s (%s)", name, capabilities)
            continue
        chat_models.append(name)
    return chat_models


# -------------------- microphones --------------------
def list_input_devices(sd=None):
    """Return input devices that accept Nova's recording format.

    Each item is a dict with index, name, hostapi and label. Devices that
    would fail at 16 kHz mono (common for WASAPI/WDM-KS endpoints) are skipped.
    Raises RuntimeError if the audio system can't be queried.
    """
    if sd is None:
        try:
            import sounddevice as sd
        except Exception as exc:
            raise RuntimeError(f"The sounddevice package isn't available: {exc}") from exc
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception as exc:
        raise RuntimeError(f"Couldn't list audio devices: {exc}") from exc

    result = []
    for index, device in enumerate(devices):
        try:
            if int(device["max_input_channels"]) < 1:
                continue
            sd.check_input_settings(device=index, samplerate=RECORD_SAMPLE_RATE, channels=1, dtype="int16")
        except Exception:
            continue
        name = str(device["name"]).strip()
        try:
            hostapi = str(hostapis[device["hostapi"]]["name"])
        except Exception:
            hostapi = ""
        short_api = hostapi.replace("Windows ", "")
        result.append({
            "index": index,
            "name": name,
            "hostapi": hostapi,
            "label": f"{name} · {short_api}" if short_api else name,
        })
    return result


def resolve_input_device(name, hostapi, devices):
    """Find the saved microphone's current device index, or None.

    Device numbers change when hardware is plugged in or removed, so the
    device is saved by name (plus host API) and looked up each time.
    """
    if not name:
        return None
    for device in devices:
        if device["name"] == name and (not hostapi or device["hostapi"] == hostapi):
            return device["index"]
    for device in devices:
        if device["name"] == name:
            return device["index"]
    return None
