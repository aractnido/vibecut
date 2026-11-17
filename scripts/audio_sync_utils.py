"""Shared GCC-PHAT audio cross-correlation helpers for precise_audio_sync.py and
sync_by_song_window.py, which each carried a byte-for-byte copy of these. Worth keeping in one
place specifically because this project already found and fixed a real sign-convention bug here
once (measured_offset = lag vs -lag) -- a copy-pasted fix to one file silently leaves the other
one wrong, which is exactly the kind of regression this project's own SKILL.md warns about."""
import subprocess
from datetime import datetime

import numpy as np

try:
    import cupy as xp
    _GPU = True
except ImportError:
    xp = np
    _GPU = False

SR = 44100


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt(dt):
    return dt.isoformat().replace("+00:00", "Z")


def aud(path, ss=None, t=None):
    cmd = ["ffmpeg", "-v", "error"] + (["-ss", str(ss)] if ss is not None else []) + \
          (["-t", str(t)] if t else []) + ["-i", path, "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.float32).copy()


def gcc_phat(sig, ref):
    """GPU (CuPy/cuFFT) when available, falls back to NumPy otherwise -- same algorithm either
    way, just swap the array module. Note: these clips' FFTs (tens of thousands of samples) are
    small enough that host<->device transfer can dominate over the FFT itself; the GPU path wins
    clearly once many clips are checked back-to-back in one process (this project's own batch
    checks), not necessarily on a single isolated call."""
    if _GPU:
        sig, ref = xp.asarray(sig), xp.asarray(ref)
    n = len(sig) + len(ref)
    nf = 1 << (n - 1).bit_length()
    S = xp.fft.rfft(sig, nf) * xp.conj(xp.fft.rfft(ref, nf))
    cc = xp.fft.irfft(S / (xp.abs(S) + 1e-9), nf)
    cc = xp.concatenate([cc[-len(ref) + 1:], cc[:len(sig)]])
    k = int(xp.argmax(cc))
    peak, std = cc[k], xp.std(cc)
    if _GPU:
        peak, std = float(peak), float(std)
    return (k - len(ref) + 1) / SR, float(peak / (std + 1e-12))


def prep(x, sr=SR, highpass_hz=80.0, taper=0.1):
    """Condition a window before gcc_phat: remove DC, high-pass, Tukey-taper the edges.

    Raw GoPro/phone windows made GCC-PHAT peak at the window EDGES (lag = -(len(ref) - len(sig)), i.e. one signal
    aligned to the end of the other) instead of at the real content match: PHAT whitening turns each window's hard
    start/stop and the rumble below ~80 Hz into the strongest "feature". Found at a second show; with this, three
    independent windows of the same clip agreed within a sample (z 108-141) where raw windows disagreed by +-6 s."""
    from scipy.signal import butter, sosfilt, windows
    sos = butter(4, highpass_hz, "hp", fs=sr, output="sos")
    y = sosfilt(sos, x - np.mean(x))
    return (y * windows.tukey(len(y), taper)).astype(np.float32)


def gcc_phat_robust(sig, ref):
    """gcc_phat on prep()-conditioned windows -- prefer this for any new sync code."""
    return gcc_phat(prep(sig), prep(ref))
