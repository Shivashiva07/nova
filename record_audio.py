
import sounddevice as sd
from scipy.io.wavfile import write

SAMPLE_RATE = 16000
DURATION = 10
INPUT_DEVICE = 1

print("Recording for 10 seconds. Speak clearly...")
audio = sd.rec(
    int(DURATION * SAMPLE_RATE),
    samplerate=SAMPLE_RATE,
    channels=1,
    dtype="int16",
    device=INPUT_DEVICE,
)
sd.wait()

write("whisper/test.wav", SAMPLE_RATE, audio)
print("Saved recording to whisper/test.wav")
