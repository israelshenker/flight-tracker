"""Lower, warmer cabin chimes (the first version was too high and thin, per the user).

What makes the real one sound rich: a lower two-note "bing-bong", a soft mallet attack
instead of a click, several overtones that ring at slightly different rates, a faint
lower octave underneath, a little shimmer from two slightly detuned copies, and the
echo of a long metal tube (the cabin).

Three versions to pick by ear:
  cabin_chime_mid.wav     G5 then E5
  cabin_chime_low.wav     E5 then C#5
  cabin_chime_lower.wav   C5 then A4
Run: python sounds/make_chimes.py
"""
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, lfilter

RATE = 44100
HERE = Path(__file__).parent
NOTE = {"A4": 440.0, "C5": 523.25, "C#5": 554.37, "E5": 659.26, "G5": 783.99}


def bell(freq, seconds=3.2):
    t = np.arange(int(RATE * seconds)) / RATE
    # (overtone, level, how fast it fades): the fundamental rings longest, upper ones fade first
    partials = [(0.5, 0.18, 1.1), (1.0, 1.0, 1.3), (2.0, 0.42, 2.2), (3.0, 0.16, 3.4), (4.07, 0.07, 5.0)]
    wave = np.zeros_like(t)
    for ratio, level, fade in partials:
        for detune in (-0.0012, 0.0012):  # two copies a hair apart: a gentle shimmer
            wave += level * np.sin(2 * np.pi * freq * ratio * (1 + detune) * t) * np.exp(-t * fade)
    attack = 1 - np.exp(-t / 0.012)      # soft mallet, no click
    return wave * attack


def cabin(x):
    """Echoes of a long narrow cabin, then soften the top end."""
    out = x.copy()
    for d, g in ((0.019, 0.42), (0.037, 0.33), (0.061, 0.26), (0.089, 0.2), (0.131, 0.15), (0.187, 0.1), (0.26, 0.07)):
        n = int(RATE * d)
        out[n:] += g * x[:-n]
    b, a = butter(2, 3800 / (RATE / 2))
    return lfilter(b, a, out)


def chime(hi, lo, name):
    first, second = bell(NOTE[hi]), bell(NOTE[lo], 3.8)
    gap = int(RATE * 0.62)
    x = np.zeros(gap + len(second))
    x[:len(first)] += first
    x[gap:] += second
    x = cabin(x)
    x *= np.minimum(1, (len(x) / RATE - np.arange(len(x)) / RATE) / 0.5)  # gentle tail
    x = x / np.max(np.abs(x)) * 0.9
    wavfile.write(HERE / name, RATE, (x * 32767).astype(np.int16))
    print(f"{name}: {len(x) / RATE:.1f}s")


chime("G5", "E5", "cabin_chime_mid.wav")
chime("E5", "C#5", "cabin_chime_low.wav")
chime("C5", "A4", "cabin_chime_lower.wav")
