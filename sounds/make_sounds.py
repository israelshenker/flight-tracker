"""Makes the notification sounds for the ntfy app (original, synthesized — no recordings).

  cabin_chime.wav  the two-tone "bing-bong" you hear before a cabin announcement
  jet_flyby.wav    a short jet passing overhead (rising roar, then a falling whoosh)

Run: python sounds/make_sounds.py
"""
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import butter, lfilter

RATE = 44100
HERE = Path(__file__).parent


def tone(freq, seconds, decay):
    """A bell-like tone: the note plus a few soft overtones, fading out."""
    t = np.arange(int(RATE * seconds)) / RATE
    wave = sum(a * np.sin(2 * np.pi * freq * m * t) for m, a in ((1, 1.0), (2, 0.35), (3, 0.12), (4.2, 0.05)))
    attack = np.minimum(1, t / 0.008)
    return wave * attack * np.exp(-t * decay)


def room(x, delays=((0.031, 0.35), (0.053, 0.25), (0.079, 0.18), (0.113, 0.12))):
    """A little echo so it sounds like a cabin, not a phone speaker."""
    out = x.copy()
    for d, g in delays:
        n = int(RATE * d)
        out[n:] += g * x[:-n]
    return out


def save(name, x):
    x = x / np.max(np.abs(x)) * 0.9
    wavfile.write(HERE / name, RATE, (x * 32767).astype(np.int16))
    print(f"{name}: {len(x) / RATE:.1f}s")


# Cabin chime: high note, then a lower one (a sixth apart), each ringing out.
hi, lo = tone(1318.5, 1.6, 2.6), tone(1046.5, 2.2, 2.0)   # E6, C6
gap = int(RATE * 0.55)
chime = np.zeros(gap + len(lo))
chime[:len(hi)] += hi
chime[gap:] += lo
save("cabin_chime.wav", room(chime))

# Jet flyby: filtered noise that swells as it approaches, with a falling pitch (Doppler)
# as it passes, plus a low engine rumble.
seconds = 4.0
n = int(RATE * seconds)
t = np.arange(n) / RATE
rng = np.random.default_rng(7)
noise = rng.standard_normal(n)
# A band of noise whose centre slides down smoothly as the plane passes (a state-variable
# filter updated every sample, so there are no clicks).
centre = 2600 - 1900 / (1 + np.exp(-(t / seconds - 0.5) * 10))
f = 2 * np.sin(np.pi * np.minimum(centre, RATE / 6) / RATE)
q = 1.3
low = band = 0.0
out = np.empty(n)
for i in range(n):
    low += f[i] * band
    high = noise[i] - low - q * band
    band += f[i] * high
    out[i] = band
envelope = np.exp(-((t - seconds * 0.5) / (seconds * 0.22)) ** 2)
rumble_freq = 95 - 45 / (1 + np.exp(-(t - seconds * 0.5) * 3))
rumble = np.sin(2 * np.pi * np.cumsum(rumble_freq) / RATE) * 0.6
b, a = butter(2, 5000 / (RATE / 2))
flyby = lfilter(b, a, out * 1.0 + rumble) * envelope
fade = np.minimum(1, t / 0.3) * np.minimum(1, (seconds - t) / 0.4)
save("jet_flyby.wav", flyby * fade)
