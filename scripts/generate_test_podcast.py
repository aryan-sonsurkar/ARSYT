"""Generate a local, authorized English test podcast for ClipFree end-to-end tests.

Run with the Python environment that has Kokoro installed:
    python scripts/generate_test_podcast.py

The generated media is written under ``temp/`` and is intentionally ignored by Git.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from kokoro import KPipeline

PARAGRAPHS = [
    "Most people think discipline is about willpower. It is not. Discipline is about designing an environment where doing the right thing is easier than avoiding it. If you want to change your life, stop relying on motivation and start changing the defaults around you.",
    "I learned this the hard way when I tried to train for a marathon. The first week I was excited. The second week I was tired. By the third week I had missed four runs. The problem was not my legs. The problem was that my running shoes were in a closet, my schedule was vague, and I kept telling myself I would run when I felt ready.",
    "So I made one change. I put my shoes beside the door every night and scheduled the run before breakfast. Within two weeks, running stopped being a decision. It became something that happened automatically. That is the real secret: make the behaviour obvious, make it small, and make it repeatable.",
    "Here is a surprising fact. Researchers estimate that nearly forty percent of our daily actions are habits rather than conscious choices. That means almost half of your day is being run by systems you built in the past. If you do not like your results, you have to inspect those systems instead of blaming your personality.",
    "The same idea applies to focus. You do not need more willpower to concentrate. You need fewer open loops. Close the extra browser tabs. Put the phone in another room. Write down the three things that actually matter today. Focus is not a feeling. Focus is the absence of competing options.",
    "Now let me tell you about a founder I interviewed last year. She built a company from her kitchen table and grew it to forty employees. Everyone assumed she had a brilliant five-year plan. She laughed when I asked. She said her plan was simply to talk to ten customers every week and fix the most painful problem she heard. That was it.",
    "Her story stuck with me because we romanticise strategy and ignore repetition. The person who talks to ten customers every week will usually beat the person who spends six months designing the perfect launch. Consistency compounds in a way that genius rarely does.",
    "Of course, consistency without rest is just slow burnout. I used to wear exhaustion like a badge. I thought sleeping six hours proved I cared. It did not. It proved I was bad at prioritisation. The nights I slept seven or eight hours were the nights my work was sharper, my patience was longer, and my ideas were better.",
    "There is a useful debate here about artificial intelligence and creativity. Some people say AI will replace artists. I think that misses the point. Tools change the cost of execution, but they do not remove the need for taste. A camera did not replace painters. It forced painters to ask what only painting could do.",
    "The same will happen with AI. The people who thrive will not be the ones who type the cleverest prompt. They will be the ones who know what is worth making, who can judge quality, and who care enough to revise. Taste is becoming more valuable, not less.",
    "When I think about my father, I remember one piece of advice he gave me. I was nineteen and furious about a failure. He listened for a while and then said, son, you are not upset about the result. You are upset because you expected the result without paying the price. That sentence annoyed me for a decade because it was true.",
    "It is easy to want the outcome. It is much harder to love the boring middle. The middle is where the podcast has no listeners, the business has no revenue, and the training has no applause. If you can stay in the middle without lying to yourself, you are already ahead of most people.",
    "Let us talk about memory for a moment. Your brain is not a recording device. It reconstructs the past every time you remember it. That is why two people can experience the same event and tell completely different stories. It is also why rehearsal matters. The more often you retrieve an idea, the stronger the path becomes.",
    "This has a practical consequence for learning. Highlighting a book feels productive, but it does not build retrieval strength. Closing the book and explaining the idea in your own words does. If you want to remember something, test yourself. Struggle is not a sign that learning failed. Struggle is often the mechanism of learning.",
    "And now a funny story. I once signed up for a gym class called extreme conditioning. I assumed the word extreme was marketing. Twenty minutes later I was lying on the floor while a very cheerful instructor asked if I could feel my legs. I could not. I drove home with the seat heater on and told everyone I had discovered a new level of humility.",
    "Humour matters more than we admit in serious work. Teams that can laugh at themselves recover from mistakes faster. The joke is not the opposite of professionalism. The joke is evidence that the group feels safe enough to be honest.",
    "So here is what I would leave you with. Design your environment before you demand more discipline. Protect your sleep like it is part of the work. Choose one customer, one habit, or one skill and repeat it long enough to learn something real. And when you fail, do not turn it into a story about who you are. Turn it into information about what to change next.",
    "If this conversation helped you, take one idea and apply it today. Not next month. Today. Write it down, make it small, and put it somewhere you cannot ignore. That is how a different life begins. One unglamorous repetition at a time.",
]

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "temp"
WAV_PATH = OUTPUT_DIR / "e2e_podcast.wav"
VIDEO_PATH = OUTPUT_DIR / "e2e_podcast.mp4"
SAMPLE_RATE = 24000


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pipeline = KPipeline(lang_code="a")
    chunks: list[np.ndarray] = []
    silence = np.zeros(int(SAMPLE_RATE * 0.55), dtype=np.float32)
    for index, paragraph in enumerate(PARAGRAPHS, start=1):
        print(f"generating paragraph {index}/{len(PARAGRAPHS)}", flush=True)
        for _, _, audio in pipeline(paragraph, voice="am_michael"):
            chunks.append(np.asarray(audio, dtype=np.float32))
        chunks.append(silence)
    audio = np.concatenate(chunks) if chunks else np.zeros(1, dtype=np.float32)
    peak = float(np.max(np.abs(audio))) or 1.0
    audio = (audio / peak * 0.92).astype(np.float32)
    sf.write(WAV_PATH, audio, SAMPLE_RATE)
    duration = len(audio) / SAMPLE_RATE
    print(f"wrote {WAV_PATH} ({duration:.1f}s)")

    subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
        "-i", str(WAV_PATH),
        "-map", "0:v", "-map", "1:a", "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        str(VIDEO_PATH),
    ], check=True)
    print(f"wrote {VIDEO_PATH}")


if __name__ == "__main__":
    main()
