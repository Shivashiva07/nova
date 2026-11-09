"""Tests for Nova's settings: persistence, validation, discovery and startup.

Run from the project folder:  python -m unittest discover -s tests -v

These never touch the real nova_settings.json or the Windows Startup folder:
everything is written to temporary directories.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import nova_settings  # noqa: E402
from nova_settings import DEFAULTS, validate_settings  # noqa: E402


class ValidateSettingsTests(unittest.TestCase):
    def test_empty_gives_defaults(self):
        clean, warnings = validate_settings({})
        self.assertEqual(clean, DEFAULTS)
        self.assertEqual(warnings, [])

    def test_legacy_file_is_preserved_and_extended(self):
        clean, warnings = validate_settings({"minimize_to_tray": False})
        self.assertFalse(clean["minimize_to_tray"])
        self.assertIsNone(clean["ollama_model"])
        self.assertEqual(warnings, [])

    def test_unknown_keys_are_kept(self):
        clean, _ = validate_settings({"future_option": {"a": 1}})
        self.assertEqual(clean["future_option"], {"a": 1})

    def test_rate_and_volume_are_clamped(self):
        clean, warnings = validate_settings({"tts_rate": 9999, "tts_volume": -3})
        self.assertEqual(clean["tts_rate"], nova_settings.TTS_RATE_MAX)
        self.assertEqual(clean["tts_volume"], 0.0)
        self.assertEqual(len(warnings), 2)

    def test_numeric_strings_are_accepted(self):
        clean, warnings = validate_settings({"tts_rate": "150", "tts_volume": "0.5"})
        self.assertEqual((clean["tts_rate"], clean["tts_volume"]), (150, 0.5))
        self.assertEqual(warnings, [])

    def test_invalid_types_fall_back(self):
        clean, warnings = validate_settings({
            "minimize_to_tray": "maybe", "tts_rate": "fast", "tts_volume": True,
            "ollama_model": 42, "wake_word_enabled": None, "tts_voice": "bad\nvalue",
        })
        self.assertTrue(clean["minimize_to_tray"])
        self.assertIsNone(clean["tts_rate"])
        self.assertEqual(clean["tts_volume"], 1.0)
        self.assertIsNone(clean["ollama_model"])
        self.assertFalse(clean["wake_word_enabled"])
        self.assertIsNone(clean["tts_voice"])
        self.assertEqual(len(warnings), 6)

    def test_blank_model_means_default(self):
        clean, warnings = validate_settings({"ollama_model": "   "})
        self.assertIsNone(clean["ollama_model"])
        self.assertEqual(warnings, [])

    def test_hostapi_cleared_without_microphone(self):
        clean, _ = validate_settings({"microphone_name": None, "microphone_hostapi": "MME"})
        self.assertIsNone(clean["microphone_hostapi"])

    def test_non_dict(self):
        clean, warnings = validate_settings(["not", "a", "dict"])
        self.assertEqual(clean, DEFAULTS)
        self.assertTrue(warnings)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nova_settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file(self):
        settings, warnings = nova_settings.load_settings(self.path)
        self.assertEqual(settings, DEFAULTS)
        self.assertEqual(warnings, [])
        self.assertFalse(self.path.exists(), "loading must not create the file")

    def test_save_and_reload_round_trip(self):
        wanted = dict(DEFAULTS, ollama_model="llama3.2:3b", tts_rate=190, tts_volume=0.65,
                      microphone_name="Headset (soundcore Q20i)", microphone_hostapi="MME",
                      wake_word_enabled=True, minimize_to_tray=False, tts_voice="VOICE_ID")
        saved = nova_settings.save_settings(wanted, self.path)
        self.assertEqual(saved, wanted)
        reloaded, warnings = nova_settings.load_settings(self.path)
        self.assertEqual(reloaded, wanted)
        self.assertEqual(warnings, [])
        leftovers = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(leftovers, [], "atomic save must not leave temp files")

    def test_existing_values_survive_migration(self):
        self.path.write_text(json.dumps({"minimize_to_tray": False, "custom": "keep me"}), encoding="utf-8")
        settings, _ = nova_settings.load_settings(self.path)
        settings["tts_rate"] = 160
        nova_settings.save_settings(settings, self.path)
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertFalse(on_disk["minimize_to_tray"])
        self.assertEqual(on_disk["custom"], "keep me")
        self.assertEqual(on_disk["tts_rate"], 160)

    def test_bom_is_tolerated(self):
        self.path.write_text('{"minimize_to_tray": false}', encoding="utf-8-sig")
        settings, warnings = nova_settings.load_settings(self.path)
        self.assertFalse(settings["minimize_to_tray"])
        self.assertEqual(warnings, [])

    def test_corrupt_file_is_backed_up_not_lost(self):
        self.path.write_text("{not json", encoding="utf-8")
        settings, warnings = nova_settings.load_settings(self.path)
        self.assertEqual(settings, DEFAULTS)
        self.assertEqual(len(warnings), 1)
        backups = list(self.path.parent.glob("nova_settings.unreadable-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json", "original stays until a save")

    def test_failed_write_keeps_old_file(self):
        nova_settings.save_settings({"minimize_to_tray": False}, self.path)
        with mock.patch("nova_settings.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                nova_settings.save_settings({"minimize_to_tray": True}, self.path)
        self.assertFalse(json.loads(self.path.read_text(encoding="utf-8"))["minimize_to_tray"])
        self.assertEqual([p.name for p in self.path.parent.iterdir()], [self.path.name])


class FakeModel:
    def __init__(self, name):
        self.model = name


class FakeOllama:
    def __init__(self, names, capabilities=None, fail=False):
        self.names, self.capabilities, self.fail = names, capabilities or {}, fail

    def list(self):
        if self.fail:
            raise ConnectionError("refused")
        return mock.Mock(models=[FakeModel(n) for n in self.names])

    def show(self, name):
        if name not in self.capabilities:
            raise RuntimeError("old server")
        return mock.Mock(capabilities=self.capabilities[name])


class OllamaModelTests(unittest.TestCase):
    def test_lists_sorted_chat_models_only(self):
        client = FakeOllama(
            ["llama3.2:3b", "gemma3:4b", "nomic-embed-text:latest"],
            {"gemma3:4b": ["completion", "vision"], "nomic-embed-text:latest": ["embedding"]},
        )
        # llama3.2 has no capability info (show fails) and is still offered.
        self.assertEqual(nova_settings.list_ollama_models(client=client), ["gemma3:4b", "llama3.2:3b"])

    def test_unreachable(self):
        with self.assertRaises(nova_settings.OllamaUnavailable):
            nova_settings.list_ollama_models(client=FakeOllama([], fail=True))

    def test_none_installed(self):
        self.assertEqual(nova_settings.list_ollama_models(client=FakeOllama([])), [])


class FakeSoundDevice:
    devices = [
        {"name": "Microsoft Sound Mapper - Output", "max_input_channels": 0, "hostapi": 0},
        {"name": "Microphone Array", "max_input_channels": 2, "hostapi": 0},
        {"name": "Headset", "max_input_channels": 1, "hostapi": 0},
        {"name": "Headset", "max_input_channels": 1, "hostapi": 1},
        {"name": "Microphone Array", "max_input_channels": 2, "hostapi": 1},  # rejects 16 kHz
    ]
    hostapis = [{"name": "MME"}, {"name": "Windows WASAPI"}]

    def query_devices(self):
        return self.devices

    def query_hostapis(self):
        return self.hostapis

    def check_input_settings(self, device, **_kwargs):
        if device == 4:
            raise ValueError("Invalid sample rate")


class MicrophoneTests(unittest.TestCase):
    def setUp(self):
        self.devices = nova_settings.list_input_devices(sd=FakeSoundDevice())

    def test_filters_outputs_and_unsupported(self):
        self.assertEqual([d["index"] for d in self.devices], [1, 2, 3])
        self.assertEqual(self.devices[2]["label"], "Headset · WASAPI")

    def test_resolve_prefers_matching_hostapi(self):
        self.assertEqual(nova_settings.resolve_input_device("Headset", "Windows WASAPI", self.devices), 3)
        self.assertEqual(nova_settings.resolve_input_device("Headset", "MME", self.devices), 2)

    def test_resolve_falls_back_to_name(self):
        self.assertEqual(nova_settings.resolve_input_device("Microphone Array", "Windows WASAPI", self.devices), 1)

    def test_resolve_missing(self):
        self.assertIsNone(nova_settings.resolve_input_device("USB Mic", "MME", self.devices))
        self.assertIsNone(nova_settings.resolve_input_device(None, None, self.devices))

    def test_audio_system_error(self):
        broken = FakeSoundDevice()
        broken.query_devices = mock.Mock(side_effect=OSError("PortAudio not initialized"))
        with self.assertRaises(RuntimeError):
            nova_settings.list_input_devices(sd=broken)


class GuiIntegrationTests(unittest.TestCase):
    """Non-visual checks of nova_gui's settings glue (imports the app, opens no window)."""

    @classmethod
    def setUpClass(cls):
        import nova_gui
        cls.gui = nova_gui
        cls.backend = nova_gui.backend

    def setUp(self):
        names = ("MODEL", "MICROPHONE_DEVICE", "TTS_RATE", "TTS_VOLUME", "TTS_VOICE")
        self._saved = {n: getattr(self.backend, n) for n in names}

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.backend, name, value)

    def test_apply_runtime_settings(self):
        devices = nova_settings.list_input_devices(sd=FakeSoundDevice())
        settings = dict(DEFAULTS, ollama_model="llama3.2:3b", tts_rate=150, tts_volume=0.4,
                        tts_voice="VOICE", microphone_name="Headset", microphone_hostapi="Windows WASAPI")
        notes = self.gui.apply_runtime_settings(settings, devices=devices)
        self.assertEqual(notes, [])
        self.assertEqual(self.gui.active_model(), "llama3.2:3b")
        self.assertEqual((self.backend.TTS_RATE, self.backend.TTS_VOLUME, self.backend.TTS_VOICE), (150, 0.4, "VOICE"))
        self.assertEqual(self.backend.MICROPHONE_DEVICE, 3)

    def test_defaults_restore_assistant_values(self):
        self.gui.apply_runtime_settings(dict(DEFAULTS, ollama_model="x"), devices=[])
        self.gui.apply_runtime_settings(dict(DEFAULTS), devices=[])
        self.assertEqual(self.gui.active_model(), self.gui.DEFAULT_MODEL)
        self.assertEqual(self.backend.TTS_RATE, self.gui.DEFAULT_TTS_RATE)
        self.assertEqual(self.backend.MICROPHONE_DEVICE, self.gui.DEFAULT_MICROPHONE)

    def test_missing_microphone_falls_back_with_note(self):
        notes = self.gui.apply_runtime_settings(dict(DEFAULTS, microphone_name="USB Mic"), devices=[])
        self.assertEqual(self.backend.MICROPHONE_DEVICE, self.gui.DEFAULT_MICROPHONE)
        self.assertEqual(len(notes), 1)
        self.assertIn("USB Mic", notes[0])

    def test_startup_launcher_uses_temp_folder_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "Startup" / self.gui.STARTUP_SCRIPT_NAME
            with mock.patch.object(self.gui, "get_startup_script_path", return_value=target):
                self.assertFalse(self.gui.startup_enabled())
                self.gui.set_startup_enabled(True)
                self.assertTrue(self.gui.startup_enabled())
                script = target.read_text(encoding="utf-8")
                self.assertIn("nova_gui.py", script)
                self.assertIn("WshShell.Run", script)
                self.gui.set_startup_enabled(False)
                self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
