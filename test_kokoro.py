from kokoro import KPipeline
import soundfile as sf

print("Loading pipeline...")

pipeline = KPipeline(lang_code="a")

print("Generating audio...")

generator = pipeline(
    "Hello Aryan. This is a Kokoro speech generation test.",
    voice="af_heart"
)

for _, (_, _, audio) in enumerate(generator):
    sf.write("test.wav", audio, 24000)
    break

print("Generated test.wav successfully")