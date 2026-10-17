"""A ``sounddevice`` stand-in that routes audio through browser WebSockets.

Installed as ``sys.modules["sounddevice"]`` before the daemon loads, so the
voice listener, TTS and thinking tune run unchanged: their input stream is
fed by the browser's microphone and their output streams play in the
browser. Only the calls those modules make are implemented.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import numpy as np

from jarvis.debug import debug_log

MIC_RATE = 16000  # Browsers send 16 kHz mono int16 PCM.


class CallbackStop(Exception):
    pass


class CallbackAbort(Exception):
    pass


class PortAudioError(Exception):
    pass


_DEVICE = {
    "index": 0,
    "name": "Browser microphone",
    "max_input_channels": 1,
    "max_output_channels": 1,
    "default_samplerate": float(MIC_RATE),
}

# Set by the server: receives (sample_rate, int16 bytes) for every output block,
# and None when a stream ends so clients can flush.
audio_sink: Callable[[Optional[int], Optional[bytes]], None] = lambda rate, pcm: None

_inputs: set["InputStream"] = set()
_inputs_lock = threading.Lock()


def query_devices(device=None, kind=None):
    if device is None and kind is None:
        return [dict(_DEVICE)]
    return dict(_DEVICE)


_mic_owner = None
_mic_owner_at = 0.0
MIC_HANDOVER_SEC = 1.0


def feed_microphone(pcm: bytes, source=None) -> None:
    """Deliver browser mic PCM (16 kHz mono int16) to every active input stream.

    One source owns the mic at a time; interleaving two browsers' samples would
    double the apparent sample rate. Another source takes over once the owner
    has sent nothing for ``MIC_HANDOVER_SEC`` (it closed its tab or muted).
    """
    global _mic_owner, _mic_owner_at
    if not pcm:
        return
    now = time.monotonic()
    with _inputs_lock:
        if source is not _mic_owner and now - _mic_owner_at < MIC_HANDOVER_SEC:
            return
        _mic_owner, _mic_owner_at = source, now
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    block = samples.reshape(-1, 1)
    with _inputs_lock:
        streams = list(_inputs)
    for stream in streams:
        stream._deliver(block)


class _Stream:
    def __init__(self, samplerate=None, channels=1, dtype="float32", blocksize=0,
                 callback=None, device=None, **_ignored):
        self.samplerate = float(samplerate or MIC_RATE)
        self.channels = int(channels or 1)
        self.dtype = dtype
        self.blocksize = int(blocksize or 0)
        self.callback = callback
        self.active = False
        self.closed = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        self.close()
        return False

    def stop(self):
        self.active = False

    def abort(self):
        self.stop()

    def close(self):
        self.stop()
        self.closed = True


class InputStream(_Stream):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if int(self.samplerate) != MIC_RATE or self.channels != 1:
            # Same shape as PortAudio's format error, so the caller's
            # format negotiation falls back to 16 kHz mono.
            raise PortAudioError("Invalid sample rate", -9997)

    def start(self):
        self.active = True
        with _inputs_lock:
            _inputs.add(self)

    def stop(self):
        self.active = False
        with _inputs_lock:
            _inputs.discard(self)

    def _deliver(self, block) -> None:
        if not self.active or self.callback is None:
            return
        try:
            self.callback(block, len(block), None, None)
        except (CallbackStop, CallbackAbort):
            self.stop()


class OutputStream(_Stream):
    """Pulls blocks from the callback at real-time pace and sends them to clients.

    Real-time pacing keeps ``stream.active`` true for as long as the audio
    plays, which is what TTS uses to time echo suppression and the hot window.
    """

    def start(self):
        if self.active:
            return
        self.active = True
        threading.Thread(target=self._pump, name="browser-audio-out", daemon=True).start()

    def _pump(self) -> None:
        rate = int(self.samplerate)
        # Small chunks however large the requested block (the thinking tune asks
        # for seconds): the browser then never queues audio far ahead, so a
        # stopped stream goes quiet at once and speech is not stuck behind it.
        frames = max(1, min(self.blocksize or rate // 20, rate // 10))
        dtype = np.int16 if self.dtype == "int16" else np.float32
        period = frames / rate
        next_at = time.monotonic()
        try:
            while self.active:
                out = np.zeros((frames, self.channels), dtype=dtype)
                stop = False
                try:
                    self.callback(out, frames, None, None)
                except CallbackStop:
                    stop = True
                except CallbackAbort:
                    break
                mono = out[:, 0]
                if dtype is np.float32:
                    mono = (np.clip(mono, -1.0, 1.0) * 32767).astype(np.int16)
                audio_sink(rate, mono.astype("<i2").tobytes())
                if stop:
                    # Let the final block play out before reporting inactive.
                    time.sleep(max(0.0, next_at + period - time.monotonic()))
                    break
                next_at += period
                time.sleep(max(0.0, next_at - time.monotonic()))
        except Exception as exc:
            debug_log(f"browser audio output stream failed: {exc}", "audio")
        finally:
            self.active = False
            audio_sink(None, None)
