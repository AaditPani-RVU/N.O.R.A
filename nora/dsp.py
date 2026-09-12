"""Signal conditioning applied to microphone audio before anything judges it.

One machine-specific fault motivates this module, but the fix is general: the
internal digital microphone on this laptop (PipeWire node "Digital Microphone",
the ACP dmic behind snd_acp3x_rn) delivers a constant DC offset of about +0.16
on top of an otherwise working capture. Measured over four seconds of a silent
room, the offset drifted by 0.0001 — it is a bias, not a signal.

An offset that size is not cosmetic. It is 16% of full scale, so it eats
headroom and pushes loud speech into clipping. It makes RMS meaningless as a
speech test, because the level never drops below the bias no matter how quiet
the room. It parks a huge spike in the FFT's zero bin, which is what made the
microphone probe report "96% sub-150 Hz rumble" and condemn the only working
input in the machine. And openWakeWord sees it as a constant +5242 added to
every int16 sample it runs its mel frontend over.

Removing the block mean is the whole correction. It is exact for a constant
bias, needs no filter state, and on the 64-1280 sample blocks the capture paths
use it only touches content below about 12 Hz — which no microphone on this
machine produces and no part of NORA listens to.
"""
from __future__ import annotations

import numpy as np


def remove_dc(audio: np.ndarray) -> np.ndarray:
    """Return `audio` with its mean removed, as float32.

    Safe on anything: an empty array comes back untouched, and a capture with no
    offset (a healthy microphone) is changed by nothing worth measuring.
    """
    if audio.size == 0:
        return audio
    out = audio.astype(np.float32, copy=True)
    return out - np.float32(out.mean())


def dc_offset(audio: np.ndarray) -> float:
    """The bias `remove_dc` would take out. For diagnostics and logging."""
    return 0.0 if audio.size == 0 else float(np.mean(audio))


# Where voices live. Telephony has used roughly this band for a century for the
# same reason it is used here: it is where speech is, and it is not where most
# room and electrical noise is.
SPEECH_LOW_HZ = 300.0
SPEECH_HIGH_HZ = 4000.0


def speech_rms(audio: np.ndarray, sample_rate: int = 16000,
               low: float = SPEECH_LOW_HZ, high: float = SPEECH_HIGH_HZ) -> float:
    """RMS of `audio` restricted to the speech band, DC removed first.

    Deciding "is someone talking" on full-band RMS means judging a voice against
    every noise the microphone makes, and on this machine most of that noise is
    low-frequency: the working capture is 86% sub-150 Hz even after its DC
    offset comes off. Measured on it, a signal stands 15.6x above the loudest
    silent block on full-band RMS and 41.1x on this — the same decision with
    nearly three times the headroom, which is the difference between a threshold
    that can be set safely and one that cannot.

    The result is scaled to be comparable with a plain RMS of the same band, so
    thresholds keep their usual units.
    """
    x = remove_dc(np.asarray(audio, dtype=np.float32).reshape(-1))
    if x.size == 0:
        return 0.0
    if x.size < 64:  # too short to resolve a spectrum; fall back to plain RMS
        return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    window = np.hanning(x.size)
    spectrum = np.fft.rfft(x.astype(np.float64) * window)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sample_rate)
    keep = (freqs >= low) & (freqs < high)
    # Parseval for a one-sided spectrum, then divided by the window's mean
    # square so the answer matches the RMS of the band-passed waveform rather
    # than the window's attenuation of it.
    power = float(np.sum(np.abs(spectrum[keep]) ** 2)) * 2.0 / (x.size ** 2)
    power /= float(np.mean(window ** 2))
    return float(np.sqrt(max(power, 0.0)))
