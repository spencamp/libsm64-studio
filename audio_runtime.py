"""Optional, bounded live-audio worker for Blender's bundled Audaspace.

The worker owns no Blender RNA.  Native audio generation is serialized with
every other libsm64 call by the lifecycle lock supplied by :mod:`mario`, while
Audaspace's internally locked ``Sequence`` accepts in-memory PCM blocks.
"""

from collections import deque
import ctypes as ct
import math
import threading
import time


AUDIO_SAMPLE_RATE = 32000
AUDIO_CHANNELS = 2
AUDIO_NATIVE_BLOCK_MAX = 544
AUDIO_NATIVE_BLOCK_MIN = 528
AUDIO_DESIRED_QUEUED_SAMPLES = 1100
AUDIO_MAX_QUEUED_SAMPLES = 6000
AUDIO_STARTUP_LEAD_SECONDS = 0.05
AUDIO_RETIRED_ENTRY_SECONDS = 0.25
AUDIO_WORKER_WAIT_SECONDS = 0.005
AUDIO_JOIN_TIMEOUT_SECONDS = 2.0


class AudioBackendError(RuntimeError):
    pass


class _AudaspaceSequenceBackend:
    """Dependency-free streaming adapter over Blender 5.2's ``aud`` module."""

    def __init__(self, volume, muted):
        try:
            import aud
            import numpy
        except Exception as exc:
            raise AudioBackendError(
                "Blender's bundled Audaspace/NumPy backend is unavailable: {}".format(
                    exc
                )
            ) from exc
        self._aud = aud
        self._numpy = numpy
        self._sequence = aud.Sequence(
            channels=AUDIO_CHANNELS,
            rate=AUDIO_SAMPLE_RATE,
            fps=30.0,
            muted=False,
        )
        try:
            self._device = aud.Device(
                rate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
                format=aud.FORMAT_FLOAT32,
                buffer_size=512,
            )
            self._handle = self._device.play(self._sequence, keep=True)
        except Exception as exc:
            raise AudioBackendError(
                "Could not open Blender's Audaspace output device: {}".format(exc)
            ) from exc
        if self._handle is None:
            raise AudioBackendError("Audaspace did not return a playback handle")
        self._entries = deque()
        self._scheduled_until = max(
            0.0, float(self._handle.position) + AUDIO_STARTUP_LEAD_SECONDS
        )
        self.set_output(volume, muted)

    @property
    def position(self):
        return max(0.0, float(self._handle.position))

    @property
    def scheduled_until(self):
        return self._scheduled_until

    def queued_samples(self):
        return max(
            0,
            int(round((self._scheduled_until - self.position) * AUDIO_SAMPLE_RATE)),
        )

    def append_int16_stereo(self, samples, frame_count):
        expected = int(frame_count) * AUDIO_CHANNELS
        if expected <= 0 or len(samples) != expected:
            raise AudioBackendError(
                "PCM block has {} values; expected {}".format(len(samples), expected)
            )
        pcm = self._numpy.asarray(samples, dtype=self._numpy.int16)
        # Sound.buffer expects a two-dimensional float array.  The conversion
        # is explicit and bounded; no temporary files or external packages are
        # involved.
        normalized = (
            pcm.astype(self._numpy.float32, copy=True)
            .reshape((int(frame_count), AUDIO_CHANNELS))
        ) / 32768.0
        sound = self._aud.Sound.buffer(normalized, AUDIO_SAMPLE_RATE)
        current = self.position
        if self._scheduled_until < current:
            self._scheduled_until = current + AUDIO_STARTUP_LEAD_SECONDS
        begin = self._scheduled_until
        end = begin + (float(frame_count) / AUDIO_SAMPLE_RATE)
        entry = self._sequence.add(sound, begin, end, 0.0)
        self._entries.append((entry, end, sound, normalized))
        self._scheduled_until = end
        self.retire_consumed()

    def retire_consumed(self):
        threshold = self.position - AUDIO_RETIRED_ENTRY_SECONDS
        while self._entries and self._entries[0][1] < threshold:
            entry, _end, _sound, _normalized = self._entries.popleft()
            self._sequence.remove(entry)

    def set_output(self, volume, muted):
        volume = float(volume)
        if not math.isfinite(volume) or volume < 0.0 or volume > 1.0:
            raise ValueError("Audio volume must be between 0 and 1")
        self._handle.volume = 0.0 if bool(muted) else volume

    def stop(self):
        handle = getattr(self, "_handle", None)
        if handle is not None:
            try:
                handle.stop()
            finally:
                self._handle = None
        self._entries.clear()


class LiveAudioRuntime:
    """One generation's explicit optional-audio lifecycle state."""

    def __init__(self, backend_factory=None):
        self.audio_requested = False
        self.audio_init_attempted = False
        self.audio_initialized = False
        self.audio_worker_started = False
        self.audio_worker_stopped = False
        self.audio_device_opened = False
        self.audio_failure = ""
        self.native_failure = False
        self.generated_blocks = 0
        self.generated_frames = 0
        self.underruns = 0
        self.peak_queued_samples = 0
        self.last_queued_samples = 0
        self.last_native_block_samples = 0
        self.worker_iterations = 0
        self._backend_factory = backend_factory or _AudaspaceSequenceBackend
        self._backend = None
        self._library = None
        self._native_lock = None
        self._stop_event = threading.Event()
        self._thread = None
        self._state_lock = threading.RLock()
        self._volume = 1.0
        self._muted = False

    def initialize_and_start(self, library, rom_bytes, native_lock, volume=1.0, muted=False):
        self.audio_requested = True
        if self.audio_initialized and self.audio_worker_started:
            self.set_output(volume, muted)
            return
        if self.audio_init_attempted and not self.audio_initialized:
            raise AudioBackendError(
                self.audio_failure or "Audio initialization already failed for this session"
            )
        self._library = library
        self._native_lock = native_lock
        self._volume = float(volume)
        self._muted = bool(muted)
        if not self.audio_initialized:
            self.audio_init_attempted = True
            rom_buffer = (ct.c_uint8 * len(rom_bytes)).from_buffer(rom_bytes)
            try:
                with native_lock:
                    library.sm64_audio_init(rom_buffer)
                self.audio_initialized = True
            except Exception as exc:
                self.native_failure = True
                self.audio_failure = "Native audio initialization failed: {}".format(exc)
                raise
        try:
            self._backend = self._backend_factory(self._volume, self._muted)
            self.audio_device_opened = True
            self._stop_event.clear()
            self.audio_worker_stopped = False
            self._thread = threading.Thread(
                target=self._worker_main,
                name="LibSM64 Studio Audio",
                daemon=True,
            )
            self._thread.start()
            self.audio_worker_started = True
        except Exception as exc:
            self.audio_failure = str(exc)
            self._close_backend()
            raise

    def _generate_once(self):
        backend = self._backend
        if backend is None:
            raise AudioBackendError("Audio backend is not open")
        queued = int(backend.queued_samples())
        with self._state_lock:
            self.last_queued_samples = queued
            self.peak_queued_samples = max(self.peak_queued_samples, queued)
            if queued <= 0 and self.generated_blocks > 0:
                self.underruns += 1
        if queued >= AUDIO_MAX_QUEUED_SAMPLES:
            backend.retire_consumed()
            return False
        pcm = (ct.c_int16 * (AUDIO_NATIVE_BLOCK_MAX * 2 * AUDIO_CHANNELS))()
        try:
            with self._native_lock:
                block_samples = int(
                    self._library.sm64_audio_tick(
                        queued, AUDIO_DESIRED_QUEUED_SAMPLES, pcm
                    )
                )
        except Exception:
            self.native_failure = True
            raise
        if block_samples not in (AUDIO_NATIVE_BLOCK_MIN, AUDIO_NATIVE_BLOCK_MAX):
            raise AudioBackendError(
                "libsm64 returned unexpected audio block size {}".format(block_samples)
            )
        # libsm64 produces two stereo blocks per tick; its return value is the
        # per-channel frame count of one block.
        frame_count = block_samples * 2
        value_count = frame_count * AUDIO_CHANNELS
        backend.append_int16_stereo(pcm[:value_count], frame_count)
        with self._state_lock:
            self.generated_blocks += 2
            self.generated_frames += frame_count
            self.last_native_block_samples = block_samples
        return True

    def _worker_main(self):
        try:
            while not self._stop_event.is_set():
                self.worker_iterations += 1
                generated = self._generate_once()
                wait = AUDIO_WORKER_WAIT_SECONDS if generated else 0.01
                self._stop_event.wait(wait)
        except Exception as exc:
            with self._state_lock:
                self.audio_failure = "Live audio worker failed: {}".format(exc)
                self.audio_requested = False
            self._stop_event.set()
        finally:
            self.audio_worker_started = False
            self.audio_worker_stopped = True

    def set_output(self, volume, muted):
        volume = float(volume)
        if not math.isfinite(volume) or volume < 0.0 or volume > 1.0:
            raise ValueError("Audio volume must be between 0 and 1")
        self._volume = volume
        self._muted = bool(muted)
        backend = self._backend
        if backend is not None:
            backend.set_output(volume, muted)

    def stop_worker(self):
        self.audio_requested = False
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(AUDIO_JOIN_TIMEOUT_SECONDS)
            if thread.is_alive():
                self.native_failure = True
                self.audio_failure = (
                    "Live audio worker did not stop before native teardown"
                )
                return False
        self.audio_worker_started = False
        self.audio_worker_stopped = True
        self._thread = None
        self._close_backend()
        return True

    def _close_backend(self):
        backend = self._backend
        self._backend = None
        if backend is not None:
            try:
                backend.stop()
            except Exception as exc:
                if not self.audio_failure:
                    self.audio_failure = "Audio backend shutdown failed: {}".format(exc)
        self.audio_device_opened = False

    def snapshot(self):
        with self._state_lock:
            return {
                "audio_requested": bool(self.audio_requested),
                "audio_init_attempted": bool(self.audio_init_attempted),
                "audio_initialized": bool(self.audio_initialized),
                "audio_worker_started": bool(self.audio_worker_started),
                "audio_worker_stopped": bool(self.audio_worker_stopped),
                "audio_device_opened": bool(self.audio_device_opened),
                "audio_failure": self.audio_failure,
                "native_failure": bool(self.native_failure),
                "generated_blocks": int(self.generated_blocks),
                "generated_frames": int(self.generated_frames),
                "underruns": int(self.underruns),
                "last_queued_samples": int(self.last_queued_samples),
                "peak_queued_samples": int(self.peak_queued_samples),
                "last_native_block_samples": int(self.last_native_block_samples),
                "worker_iterations": int(self.worker_iterations),
                "sample_rate": AUDIO_SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
                "volume": self._volume,
                "muted": self._muted,
            }
