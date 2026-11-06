# Nova

A personal AI assistant for Windows that runs entirely on your own PC. Chat by typing or by voice, say "Hey Nova" to wake it, and keep notes, reminders and long-term memories. No cloud APIs and no accounts: your conversations never leave your computer.

- **Chat** with a local language model ([Ollama](https://ollama.com), `gemma3:4b` by default)
- **Voice input** transcribed locally with [whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- **"Hey Nova" wake word**, detected offline with [Vosk](https://alphacephei.com/vosk/)
- **Spoken replies** using the Windows built-in voices
- **Memory**: facts you choose to save are given to the model in future chats
- **Notes and reminders**, with pop-up alerts at the due time
- **Quick actions**: open apps, websites and folders, and find files by name
- **System tray and start with Windows**, so reminders keep working in the background

All data is stored in a single local SQLite file, `nova_memory.db`.

## Requirements

- Windows 10 or 11 (developed on Windows 11 ARM64)
- Python 3.10 or newer
- [Ollama](https://ollama.com)
- A microphone, for voice features
- About 5 GB of free disk space for the models

## Setup

### 1. Get the code and install Python packages

```powershell
git clone https://github.com/Shivashiva07/nova.git
cd nova
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Install the language model

Install Ollama, then download the model:

```powershell
ollama pull gemma3:4b
```

Ollama must be running whenever you use Nova (it starts automatically after installation).

### 3. Install whisper.cpp (voice input)

1. Download a Windows build from the [whisper.cpp releases page](https://github.com/ggml-org/whisper.cpp/releases), choosing the one that matches your CPU (x64 for most Intel/AMD PCs).
2. Extract it so that `whisper-cli.exe` ends up inside `whisper\bin\` (for example `whisper\bin\Release\whisper-cli.exe`). **Keep the `.dll` files that come with it in the same folder**: without them Whisper fails with error `0xC0000135`.
3. Download the speech model [`ggml-small.en.bin`](https://huggingface.co/ggerganov/whisper.cpp/tree/main) into the `whisper\` folder.
   Optionally also download `ggml-base.en.bin`: it's used as a fallback, and is faster but less accurate.

### 4. Install the Vosk model (optional, for "Hey Nova")

Download [`vosk-model-small-en-us-0.15`](https://alphacephei.com/vosk/models) and extract it into the project folder. To keep it somewhere else, set the `VOSK_MODEL_PATH` environment variable to the extracted folder.

When you're done, the folder should look like this:

```
nova/
├── assistant.py
├── nova_gui.py
├── nova_widgets.py
├── record_audio.py
├── requirements.txt
├── vosk-model-small-en-us-0.15/
└── whisper/
    ├── ggml-small.en.bin
    └── bin/
        └── Release/
            ├── whisper-cli.exe
            └── *.dll
```

### 5. Choose your microphone

Nova uses audio input device `1` by default. List your devices:

```powershell
python -m sounddevice
```

If your microphone has a different number, change `MICROPHONE_DEVICE` near the top of `assistant.py`. To check that recording works, set the same number as `INPUT_DEVICE` in `record_audio.py` and run it: it records 10 seconds to `whisper\test.wav`.

## Running Nova

**Desktop app** (recommended):

```powershell
python nova_gui.py
```

**Terminal version**: chat and voice input only, without notes, reminders, quick actions or the wake word.

```powershell
python assistant.py
```

## Using the desktop app

- **Type** a message and press Enter to send (Shift+Enter for a new line). Nova's replies are formatted (lists, bold, code blocks) and can be selected and copied; right-click a message of yours to copy it.
- **Conversations** are listed on the left, grouped by date. **New chat** starts one. To delete a chat, hover over it and click the trash icon, or right-click it. Deleting asks for confirmation and can't be undone.
- **Microphone button**: speak after the microphone opens. Recording stops when you pause, and you confirm the transcript before it's sent. Nova speaks its reply.
- **Hey Nova switch** listens continuously for the wake phrase, then records your command. While it's on, the microphone button is disabled because both need the microphone.
- **Memories**: save facts about yourself (for example category "preference", "I prefer short answers"). Only memories you add are stored.
- **Notes & reminders**: create, edit and delete notes, and schedule reminders. Reminders only pop up while Nova is running, so use the tray option below.
- **Settings**: start Nova when you sign in to Windows, and keep it running in the system tray when you close the window.
- The status pill in the top-right shows what Nova is doing: green when ready, amber while working or listening, red on errors.

### Commands

These messages are handled by Nova itself, instantly, without the language model:

| What | Example |
|---|---|
| Open a website | `open youtube` (also google, gmail, github, chatgpt) |
| Open an app | `open notepad` (also calculator, paint, task manager, vs code) |
| Open a folder | `open downloads` (also documents, desktop, pictures) |
| Find files by name | `find file invoice in downloads` (downloads, documents or desktop; documents if omitted) |
| Save a note | `note: Shopping | eggs, milk, bread` |
| List notes | `show notes` |
| Reminder after a delay | `remind me to call home in 20 minutes` (minutes, hours or days, in digits) |
| Reminder at a time | `remind me to submit the report at 2026-11-07 09:30` |
| List reminders | `show reminders` |

Everything else goes to the language model. Nova can't delete, move or edit files, or control other settings on your PC.

### Terminal commands

In `assistant.py`, type `/help` for the full list: `/voice`, `/new`, `/sessions`, `/switch ID`, `/history`, `/clear`, `/remember [CATEGORY |] TEXT`, `/memories`, `/forget ID` and `/exit`.

## Configuration

Settings are constants near the top of `assistant.py`:

| Setting | Default | Purpose |
|---|---|---|
| `MODEL` | `gemma3:4b` | Ollama model to chat with |
| `MICROPHONE_DEVICE` | `1` | Audio input device number |
| `SPEECH_THRESHOLD` | `450` | Loudness that counts as speech; raise it in noisy rooms |
| `SILENCE_DURATION` | `1.3` | Seconds of silence that end a recording |
| `MIN_SPEECH_SECONDS` | `0.35` | Shorter recordings are ignored as noise |
| `NO_SPEECH_TIMEOUT` | `10` | Give up if no speech starts within this many seconds |
| `MAX_RECORD_SECONDS` | `30` | Longest single recording |
| `MAX_CONTEXT_MESSAGES` | `12` | Recent messages sent to the model with each request |
| `TTS_RATE` | `175` | Speaking speed |

To use a different model, run `ollama pull <name>` and change `MODEL`. Larger models give better answers but need more memory and respond more slowly.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Check that Ollama is running" | Start Ollama, and check `ollama list` shows `gemma3:4b`. |
| Whisper fails with `0xC0000135` | A DLL is missing: keep all `.dll` files from the whisper.cpp download next to `whisper-cli.exe`. |
| "Whisper executable not found" | `whisper-cli.exe` must be somewhere inside `whisper\bin\`. |
| "No speech detected" | Check `MICROPHONE_DEVICE`, speak closer to the mic, or lower `SPEECH_THRESHOLD`. |
| Recording cuts off after a short word | Pausing for `SILENCE_DURATION` seconds ends the recording; raise it slightly. |
| "Hey Nova" setup fails | Install the Vosk model (step 4). |
| No tray icon option | Run `pip install pystray pillow` and restart Nova. |

## Privacy

Chats, memories, notes and reminders are stored only in `nova_memory.db` in the project folder. Speech is transcribed on your PC and recordings are deleted right after transcription. The only network access is to Ollama on your own machine, plus any website you ask Nova to open in your browser.

`nova_memory.db`, `nova_settings.json`, audio recordings and the models are excluded from git by `.gitignore`.
