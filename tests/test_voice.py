"""Tests for hands-free voice (Step 29): the state machine, the wake phrase and
detector, spoken commands, interruption, approvals, microphone failures,
background controls, settings, and the GUI wiring.

Microphone, Vosk recognizer, whisper.cpp and text-to-speech are all fakes; one
test also checks the real offline Vosk model when it's installed.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import json
import sys
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

import assistant  # noqa: E402
import nova_settings  # noqa: E402
import nova_voice as nv  # noqa: E402

WAKE_AMP, OTHER_AMP, QUIET_AMP = 3000, 2000, 5


def block(amplitude, n=4000):
    return np.full(n, amplitude, dtype=np.int16).tobytes()


WAKE, OTHER, QUIET, ZERO = block(WAKE_AMP), block(OTHER_AMP), block(QUIET_AMP), block(0)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeRecognizer:
    """Hears “hey nova” in WAKE blocks and nothing else (like Vosk with a grammar)."""
    created = 0

    def __init__(self, grammar, confidence=0.95):
        FakeRecognizer.created += 1
        self.grammar = json.loads(grammar)
        self.confidence = confidence
        self.fed = 0

    def SetWords(self, value):
        self.words = value

    def AcceptWaveform(self, data):
        self.fed += 1
        return int(np.frombuffer(data, dtype=np.int16)[0]) == WAKE_AMP

    def Result(self):
        return json.dumps({"result": [{"word": "hey", "conf": self.confidence},
                                      {"word": "nova", "conf": self.confidence}], "text": "hey nova"})

    def FinalResult(self):
        return json.dumps({"text": ""})


class FakeMic:
    """Scripted microphone: plays the blocks, then quiet (or nothing at all: a dead device)."""

    def __init__(self, blocks=(), then=QUIET, clock=None, limit=400):
        self.blocks, self.then, self.clock, self.limit = deque(blocks), then, clock, limit
        self.opened, self.reads, self.loop = 0, 0, None

    @contextmanager
    def open(self):
        self.opened += 1
        yield self

    def add(self, *blocks):
        self.blocks.extend(blocks)

    def read(self, timeout):
        self.reads += 1
        if self.clock is not None:
            self.clock.t += 0.25
        if self.reads > self.limit and self.loop is not None:
            self.loop.stop()                      # safety net: no test can hang
        return self.blocks.popleft() if self.blocks else self.then


class Handlers(nv.VoiceHandlers):
    def __init__(self, loop_ref=None):
        self.events, self.busy_flag, self.loop = [], False, None

    def _log(self, *item):
        self.events.append(item)

    def status(self, text):
        self._log("status", text)

    def command(self, text):
        self._log("command", text)
        self.loop.stop()

    def control(self, kind):
        self._log("control", kind)
        self.loop.stop()

    def busy(self, text):
        self._log("busy", text)
        self.loop.stop()

    def approval_answer(self, approval, value):
        self._log("approval", value)
        self.loop.resolve(approval, value)
        self.loop.stop()

    def warning(self, text):
        self._log("warning", text)

    def stop_speaking(self):
        self._log("stop_speaking")

    def is_busy(self):
        return self.busy_flag

    def kinds(self):
        return [e[0] for e in self.events]


def make_loop(blocks=(), heard=("what time is it",), record_errors=(), config=None, then=QUIET, phrase="hey nova",
              handlers=None):
    clock = FakeClock()
    mic = FakeMic(blocks, then=then, clock=clock)
    handlers = handlers or Handlers()
    machine = nv.VoiceStateMachine()
    texts, errors = deque(heard), deque(record_errors)
    recorded = []

    def record(status, stop, **kwargs):
        recorded.append(kwargs)
        if errors:
            raise errors.popleft()
        return "AUDIO"

    def transcribe(audio, status, stop):
        return texts.popleft() if texts else ""

    beep = mock.Mock()
    loop = nv.VoiceLoop(machine, nv.WakePhrase(phrase), config or nv.VoiceConfig(), handlers, open_source=mic.open,
                        make_recognizer=FakeRecognizer, record=record, transcribe=transcribe, beep=beep,
                        clock=clock)
    handlers.loop = mic.loop = loop
    loop.mic, loop.recorded, loop.beeper, loop.fake_clock = mic, recorded, beep, clock
    return loop, handlers, machine


def run(loop, seconds=5):
    thread = threading.Thread(target=loop.run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        loop.stop()
        thread.join(2)
        raise AssertionError("the voice loop didn't stop")


# ==================== states ====================
class StateMachineTests(unittest.TestCase):
    def test_every_state_exists_and_transitions_are_checked(self):
        self.assertEqual(set(nv.STATES), set(nv.TRANSITIONS))
        changes = []
        m = nv.VoiceStateMachine(on_change=lambda old, new: changes.append((old, new)))
        self.assertEqual(m.state, nv.IDLE)
        for state in (nv.WAKE_DETECTED, nv.LISTENING, nv.PROCESSING, nv.SPEAKING, nv.PROCESSING,
                      nv.WAITING_FOR_APPROVAL, nv.PROCESSING, nv.IDLE):
            self.assertTrue(m.to(state), state)
        self.assertEqual(len(changes), 8)
        self.assertFalse(m.to(nv.WAITING_FOR_APPROVAL), "IDLE can't jump to waiting for approval")
        self.assertFalse(m.to(nv.LISTENING) and m.to(nv.SPEAKING), "Nova never speaks over a recording")
        self.assertEqual(m.state, nv.LISTENING)

    def test_expected_state_and_reset(self):
        m = nv.VoiceStateMachine()
        m.to(nv.PROCESSING)
        self.assertFalse(m.to(nv.IDLE, expected=nv.SPEAKING), "only from the expected state")
        self.assertEqual(m.state, nv.PROCESSING)
        m.to(nv.ERROR)
        self.assertFalse(m.to(nv.LISTENING), "an error only returns to IDLE")
        m.reset()
        self.assertEqual(m.state, nv.IDLE)

    def test_a_broken_listener_never_breaks_voice(self):
        m = nv.VoiceStateMachine(on_change=mock.Mock(side_effect=RuntimeError("UI gone")))
        self.assertTrue(m.to(nv.SPEAKING))
        self.assertEqual(m.state, nv.SPEAKING)


# ==================== the wake phrase ====================
class WakePhraseTests(unittest.TestCase):
    def test_checks(self):
        self.assertIsNone(nv.WakePhrase.check("Hey Nova"))
        self.assertIsNone(nv.WakePhrase.check("okay computer"))
        for bad in ("", "a b c d e", "nova", "hey 2", "hi"):
            self.assertIsNotNone(nv.WakePhrase.check(bad), bad)
        known = {"hey", "nova", "computer"}.__contains__
        self.assertIn("doesn't know", nv.WakePhrase.check("hey zorbulon", known))
        self.assertIsNone(nv.WakePhrase.check("hey computer", known))

    def test_grammar_variants_and_matching(self):
        phrase = nv.WakePhrase("Hey  NOVA!")
        self.assertEqual(phrase.label, "Hey Nova")
        self.assertEqual(json.loads(phrase.grammar()), ["hey nova", "hey no va", "[unk]"])
        self.assertTrue(phrase.matches('{"text": "hey nova"}'))
        self.assertTrue(phrase.matches('{"text": "[unk] hey no va"}'))
        self.assertFalse(phrase.matches('{"text": "nova"}'))
        self.assertFalse(phrase.matches("not json"))
        low = {"result": [{"word": "hey", "conf": 0.9}, {"word": "nova", "conf": 0.4}], "text": "hey nova"}
        self.assertTrue(phrase.matches(low, 0.3))
        self.assertFalse(phrase.matches(low, 0.7), "sensitivity: low-confidence words are rejected")
        # a variant whose words the model doesn't know is left out of the grammar
        only = nv.WakePhrase("hey nova", known={"hey", "nova"}.__contains__)
        self.assertEqual(json.loads(only.grammar()), ["hey nova", "[unk]"])

    def test_real_vosk_model_when_installed(self):
        import nova_gui
        if nova_gui.wake_word_support() is not None:
            self.skipTest("Vosk or its model isn't installed")
        model = nova_gui.load_vosk_model(nova_gui.find_vosk_model())
        known = nv.vosk_word_checker(model)
        self.assertIsNotNone(known)
        self.assertTrue(known("nova") and known("hey"))
        self.assertFalse(known("zzqxv"))
        from vosk import KaldiRecognizer
        detector = nv.WakeDetector(nv.WakePhrase("hey nova", known),
                                   lambda grammar: KaldiRecognizer(model, 16000, grammar))
        for _ in range(8):
            self.assertFalse(detector.feed(QUIET))
        self.assertEqual(detector.decoded_blocks, 0, "quiet audio isn't decoded at all")


class WakeDetectorTests(unittest.TestCase):
    def detector(self, **kwargs):
        clock = FakeClock()
        return nv.WakeDetector(nv.WakePhrase(), FakeRecognizer, clock=clock, **kwargs), clock

    def test_quiet_audio_is_not_decoded(self):
        detector, _ = self.detector()
        for _ in range(20):
            self.assertFalse(detector.feed(QUIET))
        self.assertEqual(detector.decoded_blocks, 0)
        self.assertFalse(detector.feed(OTHER))
        self.assertEqual(detector.decoded_blocks, 3, "the pre-roll is decoded with the first loud block")

    def test_detection_and_cooldown_prevent_repeated_triggers(self):
        detector, clock = self.detector(cooldown=2.0)
        self.assertTrue(detector.feed(WAKE))
        self.assertFalse(detector.feed(WAKE), "a second trigger right away is ignored")
        self.assertEqual(detector.ignored, 1)
        clock.t += 2.5
        self.assertTrue(detector.feed(WAKE))

    def test_sensitivity(self):
        with mock.patch.object(FakeRecognizer, "Result", lambda self: json.dumps(
                {"result": [{"word": "hey", "conf": 0.8}, {"word": "nova", "conf": 0.8}]})):
            low, _ = self.detector(sensitivity="low")
            self.assertFalse(low.feed(WAKE))
            high, _ = self.detector(sensitivity="high")
            self.assertTrue(high.feed(WAKE))
        self.assertLess(self.detector(sensitivity="high")[0].gate, self.detector(sensitivity="low")[0].gate)

    def test_silent_input_is_noticed(self):
        detector, _ = self.detector()
        for _ in range(12):
            detector.feed(ZERO)
        self.assertTrue(detector.silent_input)
        detector.feed(QUIET)
        self.assertFalse(detector.silent_input)


# ==================== what was said ====================
class UtteranceTests(unittest.TestCase):
    def test_classify(self):
        phrase = nv.WakePhrase()
        cases = {"Stop.": "stop", "please stop talking": "stop", "Hey Nova, cancel that": "cancel",
                 "never mind": "cancel", "Yes.": "yes", "go ahead": "yes", "Confirm!": "confirm",
                 "no thanks": "no", "": "", "um": "", "stop the music in Spotify": None,
                 "cancel my 9 o'clock reminder": None, "what time is it": None}
        for text, kind in cases.items():
            self.assertEqual(nv.classify_utterance(text, phrase), kind, text)

    def test_duplicate_guard(self):
        clock = FakeClock()
        guard = nv.DuplicateGuard(5, clock)
        self.assertFalse(guard.seen("What time is it?"))
        self.assertTrue(guard.seen("what time is it"))
        clock.t += 6
        self.assertFalse(guard.seen("what time is it"))

    def test_speakable_and_reports(self):
        self.assertEqual(nv.speakable("**Done**: see [Tasks](http://x) or https://example.com"),
                         "Done: see Tasks or a link")
        long = "First sentence here. " * 40
        self.assertTrue(nv.speakable(long, 100).endswith("The rest is on screen."))
        report = SimpleNamespace(status="succeeded", answer="I created notes.txt.", problems=[])
        self.assertEqual(nv.spoken_report(report), "I created notes.txt.")
        rejected = SimpleNamespace(status="rejected", answer="", problems=["The plan didn't pass:", "No such tool."])
        self.assertEqual(nv.spoken_report(rejected), "I didn't do anything. No such tool.")
        self.assertNotIn("Details", nv.spoken_report(SimpleNamespace(status="failed", answer="", problems=[])))

    def test_prompts_and_announcements(self):
        steps = [SimpleNamespace(description=f"Copy file {i}", tool="copy_file", safety="reversible")
                 for i in range(5)]
        prompt = nv.approval_prompt("plan", SimpleNamespace(steps=steps))
        self.assertIn("5 steps", prompt)
        self.assertIn("and 2 more", prompt)
        self.assertIn("Say yes", prompt)
        prepared = SimpleNamespace(description="Move report.pdf to the Recycle Bin")
        self.assertIn("Say confirm", nv.approval_prompt("step", (None, prepared)))
        self.assertIsNone(nv.announce("plan", SimpleNamespace(steps=steps)), "the approval question says it")
        self.assertEqual(nv.announce("plan", SimpleNamespace(steps=[SimpleNamespace(safety="read_only")])),
                         "Working on it.")
        self.assertIsNone(nv.announce("step", steps[0]), "not every operation is announced")
        self.assertIsNotNone(nv.announce("replanning", steps[0]))


# ==================== speaking and interruption ====================
class SpeakerTests(unittest.TestCase):
    def test_states_and_stop(self):
        machine = nv.VoiceStateMachine()
        started, release = threading.Event(), threading.Event()
        seen = {}

        def speak(text, stop_event):
            seen["text"] = text
            started.set()
            release.wait(2)
            seen["stopped"] = stop_event.is_set()

        speaker = nv.Speaker(speak, machine)
        self.assertFalse(speaker.stop(), "nothing to stop")
        thread = threading.Thread(target=speaker.speak, args=("**Hello** there",))
        thread.start()
        started.wait(2)
        self.assertEqual(machine.state, nv.SPEAKING)
        self.assertTrue(speaker.stop())
        release.set()
        thread.join(2)
        self.assertEqual(seen, {"text": "Hello there", "stopped": True})
        self.assertEqual(machine.state, nv.IDLE)

    def test_returns_to_the_right_state(self):
        machine = nv.VoiceStateMachine()
        busy = [True]
        speaker = nv.Speaker(lambda text, stop_event: None, machine, is_busy=lambda: busy[0])
        machine.to(nv.PROCESSING)
        speaker.speak("working")
        self.assertEqual(machine.state, nv.PROCESSING)
        busy[0] = False
        speaker.speak("done")
        self.assertEqual(machine.state, nv.IDLE, "the request finished while Nova spoke")
        machine.to(nv.PROCESSING)
        machine.to(nv.WAITING_FOR_APPROVAL)
        speaker.speak("Say yes or no")
        self.assertEqual(machine.state, nv.WAITING_FOR_APPROVAL)

    def test_failure_is_raised_and_state_restored(self):
        machine = nv.VoiceStateMachine()
        speaker = nv.Speaker(mock.Mock(side_effect=RuntimeError("SAPI died")), machine)
        with self.assertRaises(RuntimeError):
            speaker.speak("hello")
        self.assertEqual(machine.state, nv.IDLE)
        self.assertFalse(speaker.speaking)

    def test_tts_stops_at_the_next_word(self):
        stop = threading.Event()

        class Engine:
            def __init__(self):
                self.said, self.stopped, self.callbacks = [], False, []

            def connect(self, name, callback):
                self.callbacks.append(callback)
                return "token"

            def disconnect(self, token):
                self.callbacks.clear()

            def say(self, text):
                self.said.append(text)

            def runAndWait(self):
                stop.set()                        # “stop speaking” pressed during the first sentence
                for callback in self.callbacks:
                    callback("word", 0, 1)

            def stop(self):
                self.stopped = True

        engine = Engine()
        text = "First sentence. Second sentence. Third."
        assistant.speak_response(engine, text, stop_event=stop)
        # The whole reply in one say/runAndWait: pyttsx3 2.99 on SAPI cuts a second
        # runAndWait short, so splitting into sentences lost everything after the first.
        self.assertEqual(engine.said, [text])
        self.assertTrue(engine.stopped, "stopped at the next word")
        self.assertEqual(engine.callbacks, [], "the word callback is removed")
        plain = Engine()
        assistant.speak_response(plain, "One. Two.")
        self.assertEqual(plain.said, ["One. Two."], "without a stop signal, speech is unchanged")
        already = Engine()
        assistant.speak_response(already, "Never said.", stop_event=stop)     # stop pressed before it began
        self.assertEqual(already.said, [])

    def test_recording_timeouts_are_configurable(self):
        if assistant.sd is None:
            self.skipTest("sounddevice not installed")

        class SilentStream:
            def __init__(self, callback=None, **kwargs):
                self.callback, self.running = callback, True

            def __enter__(self):
                def feed():
                    while self.running:
                        self.callback(np.zeros((1024, 1), dtype=np.int16), 1024, None, None)
                        time.sleep(0.01)
                threading.Thread(target=feed, daemon=True).start()
                return self

            def __exit__(self, *exc):
                self.running = False
                return False

        started = time.monotonic()
        with mock.patch.object(assistant.sd, "InputStream", SilentStream):
            with self.assertRaises(assistant.VoiceInputError) as caught:
                assistant.record_until_silence(lambda _s: None, no_speech_timeout=0.4)
        self.assertIn("No speech", str(caught.exception))
        self.assertLess(time.monotonic() - started, 3)


# ==================== the listening loop ====================
class VoiceLoopTests(unittest.TestCase):
    def test_wake_then_request_goes_to_the_owner(self):
        config = nv.VoiceConfig(listen_timeout=4, end_silence=0.9, max_utterance=12)
        loop, handlers, machine = make_loop([QUIET, WAKE], config=config)
        run(loop)
        self.assertIn(("command", "what time is it"), handlers.events)
        self.assertEqual(loop.recorded, [{"no_speech_timeout": 4, "silence_duration": 0.9, "max_seconds": 12}])
        visited = [new for _old, new in machine.history]
        self.assertEqual(visited[:3], [nv.WAKE_DETECTED, nv.LISTENING, nv.PROCESSING])
        self.assertEqual(machine.state, nv.PROCESSING, "working until the owner says it's finished")
        loop.beeper.assert_called_once()
        loop.finished()
        self.assertEqual(machine.state, nv.IDLE)

    def test_no_speech_after_the_wake_phrase(self):
        loop, handlers, machine = make_loop(
            [WAKE], record_errors=[assistant.VoiceInputError("No speech detected. Try again.")])
        handlers.status = lambda text: (handlers._log("status", text), "Didn't catch" in text and loop.stop())
        run(loop)
        self.assertNotIn("command", handlers.kinds())
        self.assertTrue(any("Didn't catch" in e[1] for e in handlers.events if e[0] == "status"))
        self.assertEqual(machine.state, nv.IDLE)

    def test_cancel_while_busy_and_requests_while_busy(self):
        loop, handlers, machine = make_loop([WAKE], heard=["Cancel."])
        handlers.busy_flag = True
        machine.to(nv.PROCESSING)
        run(loop)
        self.assertIn(("control", "cancel"), handlers.events)
        self.assertEqual(machine.state, nv.PROCESSING, "back to working on the current task")
        loop, handlers, machine = make_loop([WAKE], heard=["open notepad"])
        handlers.busy_flag = True
        run(loop)
        self.assertIn(("busy", "open notepad"), handlers.events)
        self.assertNotIn("command", handlers.kinds())

    def test_barge_in_interrupts_speech(self):
        loop, handlers, machine = make_loop([WAKE], heard=["stop"])
        handlers.stop_speaking = lambda: (handlers._log("stop_speaking"), machine.to(nv.IDLE, expected=nv.SPEAKING))
        handlers.status = lambda text: None
        machine.to(nv.SPEAKING)
        original = loop._settle
        loop._settle = lambda: (original(), loop.stop())
        run(loop)
        self.assertEqual(handlers.kinds().count("stop_speaking"), 2, "interrupted, then “stop” said")
        self.assertNotIn("command", handlers.kinds())
        self.assertEqual(machine.state, nv.IDLE)

    def test_without_barge_in_the_wake_phrase_is_ignored_while_speaking(self):
        loop, handlers, machine = make_loop([WAKE], config=nv.VoiceConfig(barge_in=False))
        machine.to(nv.SPEAKING)
        loop.mic.limit = 20
        run(loop)
        self.assertEqual(handlers.events, [])
        self.assertEqual(machine.state, nv.SPEAKING)

    def test_repeated_requests_are_ignored(self):
        loop, handlers, machine = make_loop([WAKE], heard=["what time is it", "what time is it"])
        run(loop)
        loop.finished()
        handlers.events.clear()
        loop.stop_event.clear()
        loop.fake_clock.t += 2            # past the wake cooldown, inside the duplicate window
        loop.mic.add(QUIET, QUIET, QUIET, QUIET, WAKE)   # (and past the short hold after a reply)
        original = loop._settle
        loop._settle = lambda: (original(), loop.stop())
        run(loop)
        self.assertNotIn("command", handlers.kinds())
        self.assertIn(("status", "Ignored a repeated request"), handlers.events)

    def test_spoken_approval(self):
        loop, handlers, machine = make_loop(heard=["maybe later", "yes"])
        machine.to(nv.PROCESSING)
        approval = loop.ask("I've planned 2 steps. Say yes to run them, or no to cancel.")
        self.assertEqual(machine.state, nv.WAITING_FOR_APPROVAL)
        run(loop)
        self.assertIn(("approval", True), handlers.events)
        self.assertTrue(any("say yes or no" in e[1] for e in handlers.events if e[0] == "status"))
        self.assertTrue(approval.value)
        self.assertEqual(machine.state, nv.PROCESSING)

    def test_consequential_steps_need_confirm(self):
        loop, handlers, machine = make_loop(heard=["yes", "confirm"])
        machine.to(nv.PROCESSING)
        approval = loop.ask("Say confirm to go ahead, or no to skip it.", strong=True)
        run(loop)
        self.assertEqual([e for e in handlers.events if e[0] == "approval"], [("approval", True)])
        self.assertTrue(any("say confirm or no" in e[1] for e in handlers.events if e[0] == "status"))
        self.assertTrue(approval.value)

    def test_first_answer_wins(self):
        loop, handlers, machine = make_loop()
        machine.to(nv.PROCESSING)
        approval = loop.ask("Say yes or no.")
        self.assertTrue(loop.resolve(approval, False))     # clicked
        self.assertFalse(loop.resolve(approval, True))     # a late spoken “yes” changes nothing
        self.assertFalse(approval.value)
        self.assertIsNone(loop.pending_approval())

    def test_microphone_failures(self):
        loop, handlers, machine = make_loop()

        @contextmanager
        def broken():
            raise assistant.MicrophoneError("Couldn't open the microphone (device 1): busy")
            yield
        loop.open_source = broken
        with self.assertRaises(assistant.MicrophoneError):
            loop.run()
        self.assertIn((nv.IDLE, nv.ERROR), list(machine.history))
        self.assertEqual(machine.state, nv.IDLE, "returns to IDLE after the error")
        loop, handlers, machine = make_loop(then=None, config=nv.VoiceConfig(no_audio_timeout=1.0))
        with self.assertRaises(assistant.MicrophoneError) as caught:
            loop.run()
        self.assertIn("stopped sending audio", str(caught.exception))

    def test_microphone_error_while_recording(self):
        loop, handlers, machine = make_loop([WAKE], record_errors=[assistant.MicrophoneError("unplugged")])
        with self.assertRaises(assistant.MicrophoneError):
            loop.run()
        self.assertEqual(machine.state, nv.IDLE)

    def test_silent_microphone_warning(self):
        loop, handlers, machine = make_loop([ZERO] * 14)
        handlers.warning = lambda text: (handlers._log("warning", text), loop.stop())
        run(loop)
        self.assertEqual(handlers.kinds(), ["warning"])
        self.assertIn("Privacy & security", handlers.events[0][1])

    def test_pause_closes_the_microphone_and_resume_reopens_it(self):
        loop, handlers, machine = make_loop()
        loop.pause()
        thread = threading.Thread(target=loop.run, daemon=True)
        thread.start()
        time.sleep(0.4)
        self.assertEqual(loop.mic.opened, 0, "no microphone while paused")
        loop.mic.add(WAKE)
        loop.resume()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(loop.mic.opened, 1)
        self.assertIn(("command", "what time is it"), handlers.events)

    def test_push_to_talk_while_hands_free(self):
        loop, handlers, machine = make_loop(heard=["open notepad"])
        loop.trigger()
        run(loop)
        self.assertIn(("command", "open notepad"), handlers.events)
        self.assertEqual(FakeRecognizer.created and loop.detector.decoded_blocks, 0, "no wake phrase needed")


# ==================== settings ====================
class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        clean, warnings = nova_settings.validate_settings({})
        self.assertEqual((clean["wake_phrase"], clean["wake_sensitivity"], clean["voice_route"]),
                         ("hey nova", "medium", "agent"))
        self.assertFalse(clean["wake_word_enabled"], "hands-free stays off until the user turns it on")
        clean, warnings = nova_settings.validate_settings({
            "wake_phrase": "  Okay   COMPUTER ", "wake_sensitivity": "HIGH", "voice_route": "chat",
            "voice_listen_timeout": 99, "voice_end_silence": "0.1", "voice_barge_in": "false"})
        self.assertEqual(clean["wake_phrase"], "okay computer")
        self.assertEqual((clean["wake_sensitivity"], clean["voice_route"]), ("high", "chat"))
        self.assertEqual((clean["voice_listen_timeout"], clean["voice_end_silence"]), (30, 0.5))
        self.assertFalse(clean["voice_barge_in"])
        self.assertEqual(len(warnings), 2)
        clean, warnings = nova_settings.validate_settings({"wake_phrase": "rm -rf /", "voice_route": "cloud"})
        self.assertEqual((clean["wake_phrase"], clean["voice_route"]), ("hey nova", "agent"))
        self.assertEqual(len(warnings), 2)
        config = nv.VoiceConfig.from_settings(clean)
        self.assertEqual((config.phrase, config.listen_timeout), ("hey nova", 6.0))


# ==================== GUI wiring ====================
class GuiUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import nova_gui
        cls.gui = nova_gui

    def app(self, **attrs):
        app = mock.Mock()
        app.busy = False
        app.settings = {}
        app.voice_loop = None
        app.wake_active = True
        app.voice_paused = threading.Event()
        app._agent_cancel = None
        for key, value in attrs.items():
            setattr(app, key, value)
        return app

    def tearDown(self):
        self.gui.VOICE_STATE.reset()

    def test_spoken_request_while_busy_is_not_sent(self):
        app = self.app(busy=True)
        self.gui.NovaGUI._voice_command(app, "open notepad")
        app.append_message.assert_called_once()
        self.assertIn("wasn't sent", app.append_message.call_args.args[1])
        app._voice_done.assert_called_once()
        app.handle_user_message.assert_not_called()

    def test_spoken_request_is_routed_on_a_worker(self):
        app = self.app()
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI._voice_command(app, "copy my invoice to the tax folder")
        self.assertTrue(app.busy)
        kwargs = thread.call_args.kwargs
        self.assertEqual(kwargs["target"], app._voice_route)
        self.assertEqual(kwargs["args"][1:], ("copy my invoice to the tax folder",))
        self.assertEqual(self.gui.VOICE_STATE.state, nv.PROCESSING)

    def route(self, text, app=None):
        app = app or self.app(_pending_reminder=None)
        with mock.patch.object(self.gui, "with_db", return_value=None):
            self.gui.NovaGUI._voice_route(app, 7, text)
        return app

    def test_spoken_reminder_is_set_directly(self):
        with mock.patch.object(self.gui, "new_reminder_request",
                               return_value=("Reminder set for **5:00 PM**: call Mum.", None)) as parse:
            app = self.route("Remind me to call Mum at 5 p.m.")
        parse.assert_called_once_with("Remind me to call Mum at 5 p.m.")
        app._run_agent.assert_not_called()
        app._finish_reminder_reply.assert_called_once()
        args = app._finish_reminder_reply.call_args.args
        self.assertEqual(args[3], "Reminder set for 5:00 PM: call Mum.", "a short spoken reply, no Markdown")

    def test_reminder_follow_up_answer(self):
        pending = ("clarify", "asked")
        app = self.app(_pending_reminder=pending)
        with mock.patch.object(self.gui, "answer_pending_reminder", return_value=("Reminder set.", None)) as answer:
            self.route("At 5 pm.", app)
        answer.assert_called_once_with("At 5 pm.", pending)
        app._run_agent.assert_not_called()

    def test_built_in_commands_run_directly(self):
        with mock.patch.object(self.gui, "run_local_action", return_value="Opened Notepad.") as action:
            app = self.route("Hey Nova, open Notepad.")
        action.assert_called_once_with("open Notepad")
        app._online_reply.assert_called_once_with(7, "Hey Nova, open Notepad.", "Opened Notepad.", "Opened Notepad.")
        app._run_agent.assert_not_called()
        with mock.patch.object(self.gui, "run_local_action") as action:
            app = self.route("What time is it?")
        action.assert_not_called()
        self.assertTrue(app._online_reply.call_args.args[2].startswith("It's "))
        app._run_agent.assert_not_called()

    def test_everything_else_goes_to_the_agent(self):
        with mock.patch.object(self.gui, "run_local_action", return_value=None):
            app = self.route("copy my invoice to the tax folder")
        app._run_agent.assert_called_once_with(7, "copy my invoice to the tax folder",
                                               "copy my invoice to the tax folder", True, voice=True)
        app._online_reply.assert_not_called()

    def test_quick_answers_and_command_text(self):
        from datetime import datetime
        now = datetime(2026, 11, 8, 17, 5)
        self.assertEqual(nv.quick_answer("What's the time?", now), "It's 5:05 PM.")
        self.assertEqual(nv.quick_answer("what day is it", now), "It's Sunday, 8 November 2026.")
        self.assertIsNone(nv.quick_answer("what time is my meeting", now))
        self.assertEqual(nv.command_text("Hey Nova, show my notes."), "show my notes")

    def confirming(self, app):
        app._transcript_confirmed = lambda text: self.gui.NovaGUI._transcript_confirmed(app, text)
        return app

    def test_transcript_confirmation_toggle(self):
        for setting, answer, sent in ((None, True, True), (True, False, False), (False, False, True)):
            settings = {} if setting is None else {"voice_confirm_transcript": setting}
            app = self.confirming(self.app(settings=settings))
            with mock.patch.object(self.gui.messagebox, "askyesno", return_value=answer) as ask:
                self.gui.NovaGUI._send_transcript(app, "open notepad")
            self.assertEqual(ask.called, setting is not False, f"asked? setting={setting}")
            self.assertEqual(app.handle_user_message.called, sent, f"sent? setting={setting}")
            if sent:
                app.append_message.assert_called_once_with("user", "open notepad")
                app.handle_user_message.assert_called_once_with("open notepad", speak=True)
        clean, _ = nova_settings.validate_settings({})
        self.assertTrue(clean["voice_confirm_transcript"], "confirming stays the default")

    def test_chat_route_without_confirmation(self):
        app = self.confirming(self.app(settings={"voice_route": "chat", "voice_confirm_transcript": False}))
        with mock.patch.object(self.gui.messagebox, "askyesno") as ask:
            self.gui.NovaGUI._voice_command(app, "open notepad")
        ask.assert_not_called()
        app.handle_user_message.assert_called_once_with("open notepad", speak=True)

    def test_chat_route_still_confirms_the_transcript(self):
        app = self.confirming(self.app(settings={"voice_route": "chat"}))
        with mock.patch.object(self.gui.messagebox, "askyesno", return_value=False) as ask:
            self.gui.NovaGUI._voice_command(app, "open notepad")
        ask.assert_called_once()
        app.handle_user_message.assert_not_called()
        app._voice_done.assert_called_once()
        with mock.patch.object(self.gui.messagebox, "askyesno", return_value=True):
            self.gui.NovaGUI._voice_command(app, "open notepad")
        app.handle_user_message.assert_called_once_with("open notepad", speak=True)

    def test_push_to_talk(self):
        loop = mock.Mock()
        app = self.app(voice_loop=loop)
        self.gui.NovaGUI.start_voice(app)
        loop.trigger.assert_called_once()
        app = self.app(wake_active=False)
        app.wake_listener_running.return_value = False
        with mock.patch.object(self.gui.threading, "Thread") as thread:
            self.gui.NovaGUI.start_voice(app)
        self.assertEqual(thread.call_args.kwargs["target"], app._voice_worker, "the classic microphone button")
        self.assertTrue(app.busy)

    def test_finish_action_speaks_the_short_summary(self):
        app = self.app()
        spoken = []
        with mock.patch.object(self.gui, "speak_text", spoken.append), \
                mock.patch.object(self.gui.threading, "Thread", side_effect=lambda target, daemon: SimpleNamespace(
                    start=target)):
            self.gui.NovaGUI.finish_action(app, "**Done**: every step...\n✓ Step 1", "Done.")
        self.assertEqual(spoken, ["Done."])
        app._voice_done.assert_called_once()

    def test_voice_cancel(self):
        cancel = threading.Event()
        app = self.app(busy=True, _agent_cancel=cancel)
        with mock.patch.object(self.gui.SPEAKER, "stop") as stop:
            self.gui.NovaGUI._voice_cancel(app)
        stop.assert_called_once()
        self.assertTrue(cancel.is_set())

    def test_tts_goes_through_the_stoppable_speaker(self):
        calls = []
        with mock.patch.object(assistant, "speak_text", lambda text, stop_event=None: calls.append(
                (text, stop_event))):
            self.gui.speak_text("Hello **there**")
        self.assertEqual(calls[0][0], "Hello there")
        self.assertIs(calls[0][1], self.gui.SPEAKER.stop_event)

    def test_invalid_wake_phrase_is_reported(self):
        if self.gui.wake_word_support() is not None:
            self.skipTest("Vosk or its model isn't installed")
        with self.assertRaises(RuntimeError) as caught:
            self.gui.make_voice_loop({"wake_phrase": "hey zorbulonia"}, nv.VoiceHandlers(), threading.Event(),
                                     threading.Event())
        self.assertIn("doesn't know", str(caught.exception))
        loop = self.gui.make_voice_loop({}, nv.VoiceHandlers(), threading.Event(), threading.Event())
        self.assertEqual(loop.phrase.label, "Hey Nova")


try:
    from test_agent import AgentTest, FakeModel, plan, step
except Exception:                      # pragma: no cover
    AgentTest = None


@unittest.skipIf(AgentTest is None, "agent test helpers unavailable")
class VoiceAgentWindowTests(AgentTest if AgentTest else unittest.TestCase):
    """A hands-free request through the real window and the real agent engine (model and voice mocked)."""

    def setUp(self):
        super().setUp()
        import customtkinter as ctk
        import nova_gui
        self.gui = nova_gui
        for p in (mock.patch.object(nova_gui, "SETTINGS_PATH", self.db / "settings.json"),
                  mock.patch.object(nova_gui, "BACKUP_DIR", self.db / "backups"),
                  mock.patch.object(nova_gui, "load_tray_dependencies", return_value=None),
                  mock.patch.object(nova_gui, "model_capabilities", return_value=["completion"]),
                  mock.patch.object(nova_gui, "model_context_max", return_value=None)):
            p.start()
            self.addCleanup(p.stop)
        nova_gui.initialize_document_tables()
        nova_gui.initialize_context_tables()
        try:
            self.root = ctk.CTk()
        except Exception as exc:
            self.skipTest(f"no display: {exc}")
        self.root.withdraw()
        self.app = nova_gui.NovaGUI(self.root)
        self.app.task_runner.services = self.services
        self.addCleanup(self._close)
        self.spoken = []
        speak = mock.patch.object(assistant, "speak_text",
                                  lambda text, stop_event=None, **kw: self.spoken.append(text))
        speak.start()
        self.addCleanup(speak.stop)
        # A real (not running) loop, so approvals go through ask() / resolve().
        self.loop, _handlers, _machine = make_loop()
        self.loop.machine = nova_gui.VOICE_STATE
        self.app.voice_loop = self.loop
        self.app.wake_active = True

    def _close(self):
        try:
            self.app.voice_loop = None
            self.app.exit_application(force=True)
            self.root.update()
        except Exception:
            pass
        self.gui.VOICE_STATE.reset()

    def pump_until(self, condition, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end and not condition():
            self.root.update()
            time.sleep(0.02)
        self.root.update()

    def test_spoken_request_with_spoken_approval(self):
        model = FakeModel(plan(step("s1", "create_file", {"folder": str(self.allowed), "name": "a.txt",
                                                           "content": "hello"})),
                          "I created a.txt in your Allowed folder.")
        with mock.patch.object(self.gui.ollama, "chat", model):
            self.app._voice_command(f"create a file a.txt in {self.allowed} with hello")
            self.pump_until(lambda: self.app._voice_approvals)
            self.assertEqual(self.gui.VOICE_STATE.state, nv.WAITING_FOR_APPROVAL)
            approval = next(iter(self.app._voice_approvals))
            self.app.events.put(("voice_approval", (approval, True)))       # “yes”, heard by the loop
            self.pump_until(lambda: not self.app.busy and self.gui.VOICE_STATE.state == nv.IDLE)
        self.assertTrue((self.allowed / "a.txt").exists())
        self.assertTrue(any(s.startswith("I've planned 1 step") for s in self.spoken), self.spoken)
        self.assertEqual(self.spoken[-1], "I created a.txt in your Allowed folder.")
        reply = self.gui.fetch_messages(self.app.session_id)[-1][1]
        self.assertIn("**Done**", reply, "the full report is still shown and saved")
        self.assertEqual(self.gui.VOICE_STATE.state, nv.IDLE)

    def test_spoken_no_changes_nothing(self):
        model = FakeModel(plan(step("s1", "create_file", {"folder": str(self.allowed), "name": "b.txt",
                                                           "content": "x"})))
        with mock.patch.object(self.gui.ollama, "chat", model):
            self.app._voice_command(f"create a file b.txt in {self.allowed}")
            self.pump_until(lambda: self.app._voice_approvals)
            approval = next(iter(self.app._voice_approvals))
            self.app.events.put(("voice_approval", (approval, False)))
            self.pump_until(lambda: not self.app.busy)
        self.assertFalse((self.allowed / "b.txt").exists())
        self.assertIn("Stopped", self.spoken[-1])

    def test_indicator_and_pause(self):
        self.app._show_voice_state(nv.IDLE)
        self.root.update()
        self.assertIn("Listening for “Hey Nova”", self.app.voice_state_button.cget("text"))
        self.app.toggle_voice_pause()
        self.root.update()
        self.assertTrue(self.app.voice_paused.is_set())
        self.assertIn("Paused", self.app.voice_state_button.cget("text"))
        self.app.toggle_voice_pause()
        self.assertFalse(self.app.voice_paused.is_set())


if __name__ == "__main__":
    unittest.main()
