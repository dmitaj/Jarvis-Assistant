"""Browser audio shim: mic PCM reaches capture callbacks; output streams play out and end."""

import time

import numpy as np
import pytest

from jarvis_server import browser_audio as sd


def test_mic_pcm_reaches_an_active_input_stream_as_float_mono():
    got = []
    stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32",
                            callback=lambda data, frames, t, s: got.append(data.copy()))
    pcm = np.array([0, 16384, -16384], dtype="<i2").tobytes()

    sd.feed_microphone(pcm)  # not started yet: dropped
    with stream:
        sd.feed_microphone(pcm)
    sd.feed_microphone(pcm)  # stopped: dropped

    assert len(got) == 1
    assert got[0].shape == (3, 1)
    assert got[0][:, 0].tolist() == pytest.approx([0.0, 0.5, -0.5])


def test_input_stream_rejects_formats_other_than_16k_mono_like_portaudio():
    with pytest.raises(sd.PortAudioError) as err:
        sd.InputStream(samplerate=48000, channels=1)
    assert err.value.args[1] == -9997


def test_output_stream_sends_audio_in_real_time_then_ends(monkeypatch):
    sent = []
    monkeypatch.setattr(sd, "audio_sink", lambda rate, pcm: sent.append((rate, pcm)))
    audio = np.arange(1, 3001, dtype=np.int16)  # 3000 samples at 10 kHz = 0.3 s
    pos = [0]

    def callback(out, frames, t, s):
        chunk = audio[pos[0]:pos[0] + frames]
        out[:len(chunk), 0] = chunk
        pos[0] += frames
        if len(chunk) < frames:
            raise sd.CallbackStop()

    stream = sd.OutputStream(samplerate=10000, channels=1, dtype="int16", blocksize=1000, callback=callback)
    started = time.monotonic()
    stream.start()
    while stream.active:
        time.sleep(0.01)

    assert time.monotonic() - started >= 0.25  # paced like real playback
    assert sent[-1] == (None, None)
    played = np.frombuffer(b"".join(pcm for _, pcm in sent[:-1]), dtype="<i2")
    assert played[:3000].tolist() == audio.tolist()
    assert {rate for rate, _ in sent[:-1]} == {10000}


def test_output_is_sent_in_short_chunks_even_for_large_blocks(monkeypatch):
    sent = []
    monkeypatch.setattr(sd, "audio_sink", lambda rate, pcm: sent.append(pcm))

    def callback(out, frames, t, s):
        out[:, 0] = 1
        raise sd.CallbackStop()

    stream = sd.OutputStream(samplerate=10000, channels=1, dtype="int16", blocksize=100000, callback=callback)
    stream.start()
    while stream.active:
        time.sleep(0.01)

    chunks = [pcm for pcm in sent if pcm is not None]
    assert chunks and max(len(pcm) // 2 for pcm in chunks) <= 1000  # <= 0.1 s at 10 kHz


def test_one_browser_owns_the_mic_until_it_goes_quiet(monkeypatch):
    got = []
    stream = sd.InputStream(samplerate=16000, channels=1,
                            callback=lambda data, frames, t, s: got.append(data[0, 0]))
    panel, laptop = object(), object()
    clock = [1000.0]
    monkeypatch.setattr(sd.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sd, "_mic_owner", None)  # restored after, so other tests start clean
    monkeypatch.setattr(sd, "_mic_owner_at", 0.0)
    loud = np.array([16384], dtype="<i2").tobytes()
    quiet = np.array([0], dtype="<i2").tobytes()

    with stream:
        sd.feed_microphone(loud, source=panel)
        clock[0] += 0.5
        sd.feed_microphone(quiet, source=laptop)   # panel still talking: dropped
        sd.feed_microphone(loud, source=panel)
        clock[0] += 1.5
        sd.feed_microphone(quiet, source=laptop)   # panel went quiet: laptop takes over
        sd.feed_microphone(loud, source=panel)     # now the panel waits
        sd.claim_microphone(panel)                 # ...unless it claims the mic
        sd.feed_microphone(loud, source=panel)

    assert got == [0.5, 0.5, 0.0, 0.5]
