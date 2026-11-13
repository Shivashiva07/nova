"""Hands-free voice for Nova (Step 29): wake phrase → spoken request → agent → spoken reply.

This module has no GUI code. It extends the existing voice pipeline rather than
replacing it:

* wake-phrase detection uses the same local Vosk model as before (now with a
  configurable phrase, sensitivity, an energy gate and a cooldown);
* requests are recorded with ``assistant.record_until_silence`` (its RMS voice
  activity detection decides when you've stopped speaking) and transcribed
  locally with whisper.cpp via ``assistant.transcribe_audio``;
* replies are spoken with ``assistant.speak_text`` (Windows SAPI), which can now
  be stopped mid-sentence.

Privacy: audio never leaves the computer and is never saved. Wake-phrase
audio is processed in memory and discarded; a recorded request lives in a
temporary file only while whisper.cpp transcribes it.

The pieces, all testable without a microphone:

* ``VoiceStateMachine`` — the explicit states and the transitions allowed between them;
* ``WakePhrase`` / ``WakeDetector`` — what to listen for and how readily to accept it;
* ``classify_utterance`` — "stop", "cancel", "yes", "confirm", "no" or a real request;
* ``Speaker`` — interruptible, one-at-a-time speech that keeps the state up to date;
* ``VoiceLoop`` — owns the microphone: listens, captures, hands requests over,
  and captures spoken yes/no answers when the agent asks for approval;
* ``spoken_report`` / ``approval_prompt`` / ``announce`` — what is worth saying aloud.
"""

import json
import logging
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass

from assistant import MicrophoneError, VoiceInputError

log = logging.getLogger("nova.voice")

# ==================== states ====================
IDLE = "IDLE"                          # listening for the wake phrase (or hands-free is off)
WAKE_DETECTED = "WAKE_DETECTED"        # the wake phrase was heard
LISTENING = "LISTENING"                # recording the spoken request
PROCESSING = "PROCESSING"              # transcribing, or the agent / chat is working on it
SPEAKING = "SPEAKING"                  # Nova is talking
WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"   # the agent asked; waiting for yes / no
ERROR = "ERROR"                        # something failed; returns to IDLE
STATES = (IDLE, WAKE_DETECTED, LISTENING, PROCESSING, SPEAKING, WAITING_FOR_APPROVAL, ERROR)

TRANSITIONS = {
    IDLE: {WAKE_DETECTED, LISTENING, PROCESSING, SPEAKING, ERROR},
    WAKE_DETECTED: {LISTENING, IDLE, PROCESSING, ERROR},
    LISTENING: {PROCESSING, IDLE, ERROR},
    PROCESSING: {SPEAKING, WAITING_FOR_APPROVAL, WAKE_DETECTED, IDLE, ERROR},
    SPEAKING: {PROCESSING, IDLE, WAKE_DETECTED, WAITING_FOR_APPROVAL, ERROR},
    WAITING_FOR_APPROVAL: {PROCESSING, SPEAKING, IDLE, ERROR},
    ERROR: {IDLE},
}


class VoiceStateMachine:
    """The current voice state, changed only along TRANSITIONS (thread-safe)."""

    def __init__(self, on_change=None):
        self._state = IDLE
        self._cond = threading.Condition()
        self.on_change = on_change        # called as on_change(old, new) after every change
        self.history = deque(maxlen=50)   # recent (old, new) pairs, for diagnostics and tests

    @property
    def state(self):
        return self._state

    def can(self, new):
        return new in TRANSITIONS.get(self._state, ())

    def to(self, new, expected=None):
        """Move to `new` if allowed (and, with `expected`, only from that state). Returns True if it moved."""
        with self._cond:
            old = self._state
            if expected is not None and old not in ((expected,) if isinstance(expected, str) else expected):
                return False
            if old == new:
                return True
            if new not in TRANSITIONS.get(old, ()):
                log.debug("Voice state change %s → %s isn't allowed", old, new)
                return False
            self._state = new
            self.history.append((old, new))
            self._cond.notify_all()
        self._notify(old, new)
        return True

    def reset(self):
        """Back to IDLE from anything (hands-free switched off, Nova closing, after an error)."""
        with self._cond:
            old, self._state = self._state, IDLE
            if old != IDLE:
                self.history.append((old, IDLE))
            self._cond.notify_all()
        if old != IDLE:
            self._notify(old, IDLE)

    def wait_until(self, predicate, timeout):
        with self._cond:
            return self._cond.wait_for(lambda: predicate(self._state), timeout)

    def _notify(self, old, new):
        if self.on_change is not None:
            try:
                self.on_change(old, new)
            except Exception as exc:          # a UI problem must never break voice handling
                log.warning("Voice state listener failed: %s", exc)


# ==================== the wake phrase ====================
DEFAULT_PHRASE = "hey nova"
# Spellings the small Vosk model may produce for a word, all accepted as that word.
VARIANTS = {"nova": (("nova",), ("no", "va"))}
SENSITIVITY = {   # minimum word confidence, and the loudness gate as a share of assistant.SPEECH_THRESHOLD
    "low": {"confidence": 0.9, "gate": 1.0},
    "medium": {"confidence": 0.7, "gate": 0.6},
    "high": {"confidence": 0.5, "gate": 0.35},
}


def normalize(text):
    text = re.sub(r"[^a-z' ]+", " ", str(text or "").lower().replace("’", "'"))
    return " ".join(text.split())


class WakePhrase:
    """A configurable wake phrase, checked against what the local model can recognise."""

    def __init__(self, text=DEFAULT_PHRASE, known=None):
        problem = self.check(text, known)
        if problem:
            raise ValueError(problem)
        self.words = tuple(normalize(text).split())
        expansions = [()]
        for word in self.words:
            options = VARIANTS.get(word, ((word,),))
            if known is not None:
                options = tuple(o for o in options if all(known(w) for w in o)) or ((word,),)
            expansions = [e + o for e in expansions for o in options]
        self.variants = tuple(expansions)

    @staticmethod
    def check(text, known=None):
        """None if usable, else why not (shown in Settings)."""
        words = normalize(text).split()
        if not words or len(words) > 4:
            return "The wake phrase must be 1–4 words."
        if any(not w.replace("'", "").isalpha() for w in words):
            return "The wake phrase can only contain letters."
        if sum(len(w) for w in words) < 5 or (len(words) == 1 and len(words[0]) < 6):
            return "The wake phrase is too short: short phrases go off by accident. Try two words, like “hey nova”."
        if known is not None:
            missing = [w for w in words if not known(w)]
            if missing:
                return (f"The offline wake-word model doesn't know “{' '.join(missing)}”, so it couldn't hear it. "
                        "Choose other words.")
        return None

    @property
    def label(self):
        return " ".join(w.capitalize() for w in self.words)

    def grammar(self):
        """Vosk grammar: the phrase (and its variants) or 'unknown' — nothing else can be recognised."""
        return json.dumps([" ".join(v) for v in self.variants] + ["[unk]"])

    def matches(self, result, min_confidence=0.0):
        """True if a Vosk result (JSON text or dict) contains the phrase with enough confidence."""
        if isinstance(result, str):
            try:
                result = json.loads(result or "{}")
            except ValueError:
                return False
        words = result.get("result")
        if not words:      # no per-word details: treat every word as certain
            words = [{"word": w, "conf": 1.0} for w in str(result.get("text", "")).split()]
        sequence = [str(w.get("word", "")).lower() for w in words]
        for variant in self.variants:
            n = len(variant)
            for i in range(len(sequence) - n + 1):
                if tuple(sequence[i:i + n]) == variant:
                    confidence = min(float(words[i + k].get("conf", 1.0)) for k in range(n))
                    if confidence >= min_confidence:
                        return True
        return False


def vosk_word_checker(model):
    """A function telling whether the Vosk model knows a word, or None if that can't be checked."""
    try:
        from vosk import _c
        handle = model._handle

        def known(word):
            return _c.vosk_model_find_word(handle, word.encode("utf-8")) != -1
        known("nova")
        return known
    except Exception:
        return None


def block_level(block):
    """Root-mean-square loudness of a block of 16-bit mono samples."""
    import numpy as np
    samples = np.frombuffer(block, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


class WakeDetector:
    """Feeds microphone blocks to a grammar-limited recognizer and reports when the phrase is heard.

    Quiet blocks aren't decoded at all (a short pre-roll is kept so the start of
    a word isn't lost), which keeps idle CPU and battery use low. After a
    detection, more detections are ignored for `cooldown` seconds, so one
    “Hey Nova” — or its echo — can't trigger twice.
    """

    def __init__(self, phrase, make_recognizer, sensitivity="medium", base_gate=450, cooldown=2.0,
                 preroll_blocks=2, hang_blocks=2, silent_blocks=12, clock=time.monotonic, level=block_level):
        self.phrase = phrase
        self.make_recognizer = make_recognizer
        settings = SENSITIVITY.get(sensitivity, SENSITIVITY["medium"])
        self.min_confidence = settings["confidence"]
        self.gate = base_gate * settings["gate"]
        self.cooldown = cooldown
        self.hang_blocks = hang_blocks
        self.silent_blocks = silent_blocks
        self.clock = clock
        self.level = level
        self.preroll = deque(maxlen=preroll_blocks)
        self.recognizer = None
        self.active = False
        self.quiet = 0
        self.zero_run = 0
        self.silent_input = False       # only exact zeros for a while: muted, or blocked by Windows privacy
        self.decoded_blocks = 0         # for tests and diagnostics: how much was actually decoded
        self.ignored = 0                # detections ignored during the cooldown
        self._held_until = 0.0

    def reset(self):
        self.recognizer = None
        self.active = False
        self.quiet = 0
        self.preroll.clear()

    def hold(self, seconds=None):
        """Ignore detections for a while (after handling one, or while Nova's own voice fades)."""
        self._held_until = self.clock() + (self.cooldown if seconds is None else seconds)
        self.reset()

    def feed(self, block):
        level = self.level(block)
        self.zero_run = self.zero_run + 1 if level == 0 else 0
        self.silent_input = self.zero_run >= self.silent_blocks
        if not self.active:
            if level < self.gate:
                self.preroll.append(block)
                return False
            self.active, self.quiet = True, 0
            if self.recognizer is None:
                self.recognizer = self._new_recognizer()
            pending = list(self.preroll) + [block]
            self.preroll.clear()
        else:
            pending = [block]
            self.quiet = self.quiet + 1 if level < self.gate else 0
        for item in pending:
            self.decoded_blocks += 1
            if self.recognizer.AcceptWaveform(item) and self._hit(self.recognizer.Result()):
                return True
        if self.quiet >= self.hang_blocks:       # the sound has ended: finish the utterance
            final = self.recognizer.FinalResult()
            self.recognizer = None
            self.active = False
            return self._hit(final)
        return False

    def _new_recognizer(self):
        recognizer = self.make_recognizer(self.phrase.grammar())
        try:
            recognizer.SetWords(True)          # per-word confidence, used for sensitivity
        except Exception:
            pass
        return recognizer

    def _hit(self, result):
        if not self.phrase.matches(result, self.min_confidence):
            return False
        if self.clock() < self._held_until:
            self.ignored += 1
            return False
        self.hold()
        return True


# ==================== what was said ====================
_FILLERS = {"please", "um", "uh", "er", "hey", "nova", "now", "ok", "okay", "just"}
STOP = {"stop", "stop talking", "stop speaking", "be quiet", "quiet", "shut up", "silence", "enough", "that's enough",
        "thats enough", "stop it", "hush"}
CANCEL = {"cancel", "cancel that", "cancel it", "cancel the task", "cancel this", "stop the task", "abort",
          "never mind", "nevermind", "forget it", "stop working"}
# Step 34: stop every task at once (the agent, scheduled tasks, typing, Nova's browser), like the red button.
EMERGENCY = {"emergency stop", "stop everything", "stop all tasks", "stop all", "abort everything",
             "cancel everything", "cancel all tasks", "halt everything"}
CONFIRM = {"confirm", "i confirm", "yes confirm", "confirmed", "confirm it", "yes i confirm"}
YES = {"yes", "yeah", "yep", "yes please", "sure", "go ahead", "do it", "run it", "approve", "approved", "proceed",
       "go", "run", "yes run it", "yes go ahead", "yes do it", "that's right", "correct", "affirmative"} | CONFIRM
NO = {"no", "nope", "no thanks", "no thank you", "don't", "do not", "decline", "deny", "skip", "skip it",
      "don't do it", "no don't", "no stop", "negative", "not now"}


def classify_utterance(text, wake=None):
    """'stop', 'cancel', 'confirm', 'yes', 'no', '' (nothing said) or None (a real request).

    Only the whole utterance counts, so “stop the music in Spotify” or “cancel my
    9 o'clock reminder” are requests, not commands to Nova.
    """
    words = normalize(text).split()
    if wake is not None and tuple(words[:len(wake.words)]) == wake.words:
        words = words[len(wake.words):]
    while words and words[0] in _FILLERS:
        words = words[1:]
    while words and words[-1] in _FILLERS:
        words = words[:-1]
    said = " ".join(words)
    if not said:
        return ""
    for kind, phrases in (("emergency", EMERGENCY), ("stop", STOP), ("cancel", CANCEL), ("confirm", CONFIRM),
                          ("yes", YES), ("no", NO)):
        if said in phrases:
            return kind
    return None


def command_text(text):
    """A transcript as a typed command: no leading “hey nova” / “please”, no trailing punctuation."""
    text = " ".join(str(text or "").split())
    text = re.sub(r"^(?:(?:hey|ok|okay)\s+)?nova[,.!]?\s+", "", text, flags=re.I)
    return text.strip().rstrip(".!?,").strip()


_TIME_Q = re.compile(r"(?:what(?:'s| is) the time|what time is it)(?: now| right now)?", re.I)
_DATE_Q = re.compile(r"what(?:'s| is) (?:the date|today's date)(?: today)?|what day is (?:it|today)", re.I)


def quick_answer(text, now=None):
    """The time or date, answered directly; None for anything else."""
    from datetime import datetime
    now = now or datetime.now()
    said = command_text(text).lower().replace("’", "'")
    if _TIME_Q.fullmatch(said):
        return f"It's {now.strftime('%I:%M %p').lstrip('0')}."
    if _DATE_Q.fullmatch(said):
        return f"It's {now.strftime('%A, %d %B %Y').replace(' 0', ' ')}."
    return None


class DuplicateGuard:
    """Ignores the same request heard again within `window` seconds (e.g. a repeated trigger)."""

    def __init__(self, window=6.0, clock=time.monotonic):
        self.window, self.clock = window, clock
        self._last, self._when = None, -1e9

    def seen(self, text):
        key = normalize(text)
        now = self.clock()
        if key and key == self._last and now - self._when < self.window:
            return True
        self._last, self._when = key, now
        return False


# ==================== what is worth saying ====================
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"https?://\S+")


def speakable(text, limit=None):
    """Text without Markdown and links, shortened at a sentence end when `limit` is given."""
    text = _MD_LINK.sub(r"\1", str(text or ""))
    text = _URL.sub("a link", text)
    text = re.sub(r"```.*?```", " (code shown on screen) ", text, flags=re.S)
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[✓✗✔✘⟳⏭⏹·•]", "", text)
    text = " ".join(text.split())
    if limit and len(text) > limit:
        cut = max(text.rfind(". ", 0, limit), text.rfind("? ", 0, limit), text.rfind("! ", 0, limit))
        text = (text[:cut + 1] if cut > limit // 3 else text[:limit].rsplit(" ", 1)[0] + "…") + \
            " The rest is on screen."
    return text


SPOKEN_STATUS = {
    "succeeded": "Done.",
    "failed": "Not everything worked. The details are on screen.",
    "cancelled": "Stopped. Nothing more was done.",
    "timed_out": "I stopped at the time or step limit.",
    "rejected": "I didn't do anything.",
}


def spoken_report(report, limit=400):
    """A short spoken summary of an agent Report: the answer, or what happened — not every step."""
    status = getattr(report, "status", "")
    answer = getattr(report, "answer", "")
    if answer:
        text = answer if status == "succeeded" else f"{answer} {SPOKEN_STATUS.get(status, '')}"
    else:
        text = SPOKEN_STATUS.get(status, "Finished.")
        problems = [p for p in getattr(report, "problems", []) if not p.rstrip().endswith(":")]
        if status in ("rejected", "failed") and problems:
            text += " " + problems[0]
    return speakable(text, limit)


def _join(items):
    items = [i.rstrip(".") for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _lower_first(text):
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def approval_prompt(kind, payload):
    """What Nova says when the agent needs a decision. Consequential steps need “confirm”."""
    if kind == "plan":
        steps = list(getattr(payload, "steps", []))
        names = [_lower_first(s.description or s.tool) for s in steps[:3]]
        more = f", and {len(steps) - 3} more" if len(steps) > 3 else ""
        plural = "step" if len(steps) == 1 else "steps"
        return speakable(f"I've planned {len(steps)} {plural}: {_join(names)}{more}. "
                         "Say yes to run them, or no to cancel.")
    if kind == "step":
        _step, prepared = payload
        return speakable(f"Before I {_lower_first(prepared.description).rstrip('.')}: this step needs your "
                         "approval. Say confirm to go ahead, or no to skip it.")
    if kind == "folders":
        _step, folders = payload
        return speakable(f"The task needs a folder that isn't on your allowed list: {_join(list(folders)[:2])}. "
                         "Say confirm to allow it for this task, or no.")
    return ""


def announce(kind, data):
    """What to say aloud while the agent works: important moments only, never every operation."""
    if kind == "plan":
        steps = getattr(data, "steps", [])
        if all(getattr(s, "safety", "") == "read_only" for s in steps):
            return "Working on it."
        return None                       # the approval question says it
    if kind == "replanning":
        return "That didn't work. I'm trying another safe way."
    if kind == "pick":
        return "Please choose it on screen."
    return None


# ==================== speaking ====================
class Speaker:
    """Speaks one thing at a time, can be stopped, and keeps the voice state up to date."""

    def __init__(self, speak, machine=None, is_busy=None):
        self._speak = speak              # speak(text, stop_event=...)
        self.machine = machine
        self.is_busy = is_busy           # after speaking, PROCESSING ends if nothing is busy any more
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self._active = 0

    @property
    def speaking(self):
        return self._active > 0

    def speak(self, text):
        """Speak text (blocking). Raises like the underlying engine does on failure."""
        text = speakable(text)
        if not text:
            return
        with self._lock:
            self._active += 1
            if self._active == 1:
                self.stop_event.clear()     # a stop applies to what was speaking then, not to this
        machine = self.machine
        before = machine.state if machine else None
        entered = bool(machine and before in (IDLE, PROCESSING, WAITING_FOR_APPROVAL) and machine.to(SPEAKING))
        try:
            self._speak(text, stop_event=self.stop_event)
        finally:
            with self._lock:
                self._active -= 1
            if entered:
                after = before
                if before == PROCESSING and self.is_busy is not None and not self.is_busy():
                    after = IDLE                        # the request finished while Nova was speaking
                machine.to(after, expected=SPEAKING)    # unless something else moved on meanwhile

    def stop(self):
        """Stop speaking at the next word. Returns True if Nova was speaking."""
        if self.speaking:
            self.stop_event.set()
            return True
        return False


# ==================== the listening loop ====================
@dataclass
class VoiceConfig:
    phrase: str = DEFAULT_PHRASE
    sensitivity: str = "medium"
    listen_timeout: float = 6.0       # seconds to start speaking after the wake phrase
    end_silence: float = 1.2          # silence that ends a request
    max_utterance: float = 20.0       # longest request
    barge_in: bool = True             # the wake phrase stops Nova speaking
    wake_sound: bool = True
    cooldown: float = 2.0             # ignore repeated wake triggers for this long
    duplicate_window: float = 6.0     # ignore the same request heard again this soon
    no_audio_timeout: float = 4.0     # no audio blocks at all for this long: the device has gone

    @classmethod
    def from_settings(cls, settings):
        get = settings.get
        return cls(phrase=get("wake_phrase") or DEFAULT_PHRASE, sensitivity=get("wake_sensitivity") or "medium",
                   listen_timeout=float(get("voice_listen_timeout", 6)),
                   end_silence=float(get("voice_end_silence", 1.2)),
                   max_utterance=float(get("voice_max_utterance", 20)),
                   barge_in=bool(get("voice_barge_in", True)), wake_sound=bool(get("voice_wake_sound", True)))


class AnyEvent:
    """is_set() when any of the given events is set (for one recording's stop signal)."""

    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(e.is_set() for e in self.events)


class Approval:
    """A question the agent is waiting on. The first answer wins — spoken or clicked."""

    def __init__(self, prompt, strong=False):
        self.prompt, self.strong = prompt, strong
        self.resolved = threading.Event()
        self.value = None
        self._lock = threading.Lock()

    def resolve(self, value):
        with self._lock:
            if self.resolved.is_set():
                return False
            self.value = bool(value)
            self.resolved.set()
            return True


class VoiceHandlers:
    """What the loop tells its owner (the GUI). Every method is called on the loop's thread."""

    def status(self, text):
        pass

    def command(self, text):            # a new spoken request; call VoiceLoop.finished() when it's done
        pass

    def control(self, kind):            # "cancel": stop the current task or reply
        pass

    def busy(self, text):               # a request heard while Nova was busy
        pass

    def approval_answer(self, approval, value):
        pass

    def warning(self, text):
        pass

    def stop_speaking(self):
        pass

    def is_busy(self):
        return False


MIC_SILENT = ("The microphone is sending only silence. It may be muted, or Windows may be blocking it: "
              "check Settings › Privacy & security › Microphone and allow desktop apps to use it.")


class VoiceLoop:
    """Owns the microphone while hands-free is on.

    IDLE / PROCESSING / SPEAKING: listen for the wake phrase (energy-gated; while
    processing, so “Hey Nova, cancel” works; while speaking, so it can interrupt).
    After the wake phrase: record the request (LISTENING), transcribe it
    (PROCESSING) and hand it to the owner. WAITING_FOR_APPROVAL: record yes / no.
    Paused: the microphone is closed.
    """

    def __init__(self, machine, phrase, config, handlers, *, open_source, make_recognizer, record, transcribe,
                 beep=None, base_gate=450, clock=time.monotonic, stop_event=None, paused=None):
        self.machine, self.phrase, self.config, self.handlers = machine, phrase, config, handlers
        self.open_source = open_source            # context manager yielding .read(timeout) -> bytes | None
        self.record, self.transcribe = record, transcribe
        self.beep = beep or (lambda: None)
        self.clock = clock
        self.stop_event = stop_event or threading.Event()
        self.paused = paused or threading.Event()
        self.detector = WakeDetector(phrase, make_recognizer, config.sensitivity, base_gate, config.cooldown,
                                     clock=clock)
        self.duplicates = DuplicateGuard(config.duplicate_window, clock)
        self._approval = None
        self._warned_silent = False
        self._hold = None                # a cooldown requested from another thread
        self._manual = threading.Event()   # the microphone button: start listening without the wake phrase

    # ---- called by the owner ----
    def stop(self):
        self.stop_event.set()

    def pause(self):
        self.paused.set()

    def resume(self):
        self.paused.clear()

    def finished(self):
        """The request handed over by command() is done: listen for the wake phrase again."""
        self.machine.to(IDLE, expected=PROCESSING)
        self._hold = 0.8             # don't let the tail of Nova's own voice trigger it (applied by the loop)

    def trigger(self):
        """Push-to-talk while hands-free is on: listen for a request now, as if the wake phrase was heard."""
        self._manual.set()

    def ask(self, prompt, strong=False):
        """The agent needs a decision: wait for a spoken (or clicked) answer."""
        approval = Approval(prompt, strong)
        self._approval = approval
        self.machine.to(WAITING_FOR_APPROVAL)
        return approval

    def resolve(self, approval, value):
        """Record an answer (from either voice or a click). Only the first one counts."""
        if not approval.resolve(value):
            return False
        if self._approval is approval:
            self._approval = None
        self.machine.to(PROCESSING, expected=(WAITING_FOR_APPROVAL, SPEAKING))
        return True

    def pending_approval(self):
        approval = self._approval
        return approval if approval is not None and not approval.resolved.is_set() else None

    # ---- the loop ----
    def run(self):
        try:
            while not self.stop_event.is_set():
                if self.paused.is_set():
                    self.stop_event.wait(0.25)          # microphone closed while paused
                    continue
                approval = self.pending_approval()
                if approval is not None:
                    self._answer(approval)
                elif self._listen():
                    self._after_wake()
        except MicrophoneError:
            self.machine.reset()
            self.machine.to(ERROR)
            raise
        finally:
            if self.machine.state in (WAKE_DETECTED, LISTENING, ERROR):
                self.machine.reset()

    def _interrupted(self):
        return self.stop_event.is_set() or self.paused.is_set() or self.pending_approval() is not None

    def _listen(self):
        """Listen until the wake phrase is heard (True) or the loop has something else to do (False)."""
        self.detector.reset()
        with self.open_source() as source:
            last_audio = self.clock()
            while not self._interrupted():
                block = source.read(0.25)
                if not block:
                    if self.clock() - last_audio > self.config.no_audio_timeout:
                        raise MicrophoneError("The microphone stopped sending audio. It may have been unplugged "
                                              "or taken by another app.")
                    continue
                last_audio = self.clock()
                if self._hold is not None:
                    self.detector.hold(self._hold)
                    self._hold = None
                if self._manual.is_set():
                    self._manual.clear()
                    if self.machine.state in (IDLE, SPEAKING):
                        return True
                heard = self.detector.feed(block)
                if self.detector.silent_input and not self._warned_silent:
                    self._warned_silent = True
                    self.handlers.warning(MIC_SILENT)
                elif not self.detector.silent_input:
                    self._warned_silent = False
                if heard:
                    if self.machine.state == SPEAKING and not self.config.barge_in:
                        continue
                    if self.machine.state in (IDLE, PROCESSING, SPEAKING):
                        return True
        return False

    def _settle(self):
        """Back to the right state after a wake-up that didn't start a new request."""
        self.machine.to(PROCESSING if self.handlers.is_busy() else IDLE)
        self.detector.hold()

    def _capture(self, stop):
        """Record and transcribe one utterance. Returns (text, problem); text is None if nothing usable."""
        try:
            audio = self.record(self.handlers.status, stop, no_speech_timeout=self.config.listen_timeout,
                                silence_duration=self.config.end_silence, max_seconds=self.config.max_utterance)
        except MicrophoneError:
            raise
        except VoiceInputError as exc:
            return None, str(exc)
        try:
            text = (self.transcribe(audio, self.handlers.status, stop) or "").strip()
        except MicrophoneError:
            raise
        except VoiceInputError as exc:
            return None, str(exc)
        finally:
            del audio                    # nothing is kept
        return (text or None), ("" if text else "Nothing was recognised.")

    def _after_wake(self):
        if self.machine.state == SPEAKING:
            self.handlers.stop_speaking()
            self.machine.wait_until(lambda s: s != SPEAKING, 3)
        if not self.machine.to(WAKE_DETECTED):
            self.detector.hold()
            return
        if self.config.wake_sound:
            try:
                self.beep()
            except Exception:
                pass
        self.machine.to(LISTENING)
        self.handlers.status("Listening • say your request")
        text, problem = self._capture(AnyEvent(self.stop_event, self.paused))
        self.machine.to(PROCESSING, expected=LISTENING)
        if text is None:
            if not self.stop_event.is_set():
                self.handlers.status(f"Didn't catch a request • say “{self.phrase.label}” to try again")
                log.info("Hands-free capture ended without a request: %s", problem)
            self._settle()
            return
        kind = classify_utterance(text, self.phrase)
        if kind == "stop" or kind == "":
            self.handlers.stop_speaking()
            self._settle()
            return
        if kind in ("cancel", "emergency"):
            self.handlers.control(kind)
            self._settle()
            return
        if kind in ("yes", "no", "confirm"):
            self.handlers.status("Nothing is waiting for an answer")
            self._settle()
            return
        if self.handlers.is_busy():
            self.handlers.busy(text)
            self._settle()
            return
        if self.duplicates.seen(text):
            self.handlers.status("Ignored a repeated request")
            self._settle()
            return
        self.detector.hold()
        self.handlers.command(text)              # stays PROCESSING until finished()

    def _answer(self, approval):
        """Capture a spoken answer to the pending question (the question is spoken first)."""
        if not getattr(approval, "prompted", False):
            approval.prompted = True          # give the spoken question a moment to start
            self.machine.wait_until(lambda s: s == SPEAKING or approval.resolved.is_set(), 1.5)
        while self.machine.state == SPEAKING and not self.stop_event.is_set() and not approval.resolved.is_set():
            self.machine.wait_until(lambda s: s != SPEAKING, 0.25)
        if self.stop_event.is_set() or approval.resolved.is_set() or self.paused.is_set():
            return
        text, _problem = self._capture(AnyEvent(self.stop_event, self.paused, approval.resolved))
        if text is None or approval.resolved.is_set():
            return                                   # keep waiting (a click still works)
        kind = classify_utterance(text, self.phrase)
        if kind == "stop":
            self.handlers.stop_speaking()
            return
        if kind == "emergency":                       # answers “no”, then stops everything
            self.handlers.approval_answer(approval, False)
            self.handlers.control("emergency")
            approval.resolved.wait(2)
            return
        if approval.strong:
            value = True if kind == "confirm" else False if kind in ("no", "cancel") else None
        else:
            value = True if kind in ("yes", "confirm") else False if kind in ("no", "cancel") else None
        if value is None:
            self.handlers.status(f"Heard “{text[:60]}” • say {'confirm' if approval.strong else 'yes'} or no")
            return
        self.handlers.approval_answer(approval, value)
        approval.resolved.wait(2)             # the owner records it; don't start listening again meanwhile


# ==================== the real microphone ====================
class _QueueReader:
    def __init__(self, q):
        self.q = q

    def read(self, timeout):
        import queue
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


@contextmanager
def microphone_source(device, samplerate=16000, blocksize=4000):
    """Raw 16 kHz mono int16 blocks from the microphone, in memory only (a ~0.25 s block at a time)."""
    import queue
    import sounddevice as sd

    blocks = queue.Queue(maxsize=40)

    def callback(indata, frames, time_info, status):
        try:
            blocks.put_nowait(bytes(indata))
        except queue.Full:
            pass

    try:
        stream = sd.RawInputStream(samplerate=samplerate, blocksize=blocksize, device=device, dtype="int16",
                                   channels=1, callback=callback)
    except (sd.PortAudioError, ValueError) as exc:
        raise MicrophoneError(f"Couldn't open the microphone (device {device}): {exc}. Check that it's connected, "
                              "not used by another app, and allowed in Windows Settings › Privacy & security › "
                              "Microphone.") from None
    with stream:
        yield _QueueReader(blocks)


def wake_beep():
    import winsound
    winsound.Beep(880, 110)
