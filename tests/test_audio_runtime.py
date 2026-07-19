import ctypes as ct
import threading
import time
import unittest

from audio_runtime import (
    AUDIO_CHANNELS,
    AUDIO_NATIVE_BLOCK_MAX,
    LiveAudioRuntime,
)


class TrackingLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.depth -= 1
        self._lock.release()


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def __call__(self, *arguments):
        self.calls.append(arguments)
        return self.callback(*arguments)


class FakeLibrary:
    def __init__(self, lock, block_samples=544):
        self.lock = lock
        self.block_samples = block_samples
        self.init_count = 0
        self.tick_count = 0
        self.sm64_audio_init = FakeFunction(self._init)
        self.sm64_audio_tick = FakeFunction(self._tick)

    def _init(self, _rom):
        if self.lock.depth != 1:
            raise AssertionError("audio init was not serialized")
        self.init_count += 1

    def _tick(self, _queued, _desired, pcm):
        if self.lock.depth != 1:
            raise AssertionError("audio tick was not serialized")
        self.tick_count += 1
        value_count = self.block_samples * 2 * AUDIO_CHANNELS
        for index in range(value_count):
            pcm[index] = -32768 if index % 2 else 32767
        return self.block_samples


class FakeBackend:
    instances = []

    def __init__(self, volume, muted):
        self.volume = volume
        self.muted = muted
        self.appended = []
        self.stopped = False
        self.queued = 0
        type(self).instances.append(self)

    def queued_samples(self):
        return self.queued

    def append_int16_stereo(self, values, frames):
        self.appended.append((tuple(values), frames))
        self.queued = 6000

    def retire_consumed(self):
        pass

    def set_output(self, volume, muted):
        self.volume = volume
        self.muted = muted

    def stop(self):
        self.stopped = True


class AudioRuntimeTests(unittest.TestCase):
    def setUp(self):
        FakeBackend.instances.clear()

    def test_initialization_pcm_generation_and_locking(self):
        lock = TrackingLock()
        library = FakeLibrary(lock)
        runtime = LiveAudioRuntime(backend_factory=FakeBackend)
        runtime.initialize_and_start(
            library, bytearray(b"rom"), lock, volume=0.75, muted=False
        )
        deadline = time.monotonic() + 1.0
        while not FakeBackend.instances[0].appended and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(runtime.stop_worker())
        self.assertEqual(library.init_count, 1)
        self.assertGreaterEqual(library.tick_count, 1)
        values, frames = FakeBackend.instances[0].appended[0]
        self.assertEqual(frames, AUDIO_NATIVE_BLOCK_MAX * 2)
        self.assertEqual(len(values), frames * AUDIO_CHANNELS)
        self.assertEqual(min(values), -32768)
        self.assertEqual(max(values), 32767)
        state = runtime.snapshot()
        self.assertTrue(state["audio_initialized"])
        self.assertTrue(state["audio_worker_stopped"])
        self.assertFalse(state["audio_device_opened"])

    def test_worker_restart_does_not_reinitialize_native_audio(self):
        lock = TrackingLock()
        library = FakeLibrary(lock)
        runtime = LiveAudioRuntime(backend_factory=FakeBackend)
        runtime.initialize_and_start(library, bytearray(b"rom"), lock)
        self.assertTrue(runtime.stop_worker())
        runtime.initialize_and_start(library, bytearray(b"rom"), lock, muted=True)
        self.assertTrue(runtime.stop_worker())
        self.assertEqual(library.init_count, 1)
        self.assertEqual(len(FakeBackend.instances), 2)

    def test_invalid_native_block_stops_worker_without_hiding_failure(self):
        lock = TrackingLock()
        library = FakeLibrary(lock, block_samples=100)
        runtime = LiveAudioRuntime(backend_factory=FakeBackend)
        runtime.initialize_and_start(library, bytearray(b"rom"), lock)
        deadline = time.monotonic() + 1.0
        while not runtime.snapshot()["audio_failure"] and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(runtime.stop_worker())
        state = runtime.snapshot()
        self.assertIn("unexpected audio block size", state["audio_failure"])
        self.assertFalse(state["native_failure"])

    def test_backend_open_failure_is_recoverable_after_native_init(self):
        class FailingBackend:
            def __init__(self, _volume, _muted):
                raise RuntimeError("device unavailable")

        lock = TrackingLock()
        library = FakeLibrary(lock)
        runtime = LiveAudioRuntime(backend_factory=FailingBackend)
        with self.assertRaisesRegex(RuntimeError, "device unavailable"):
            runtime.initialize_and_start(library, bytearray(b"rom"), lock)
        state = runtime.snapshot()
        self.assertTrue(state["audio_initialized"])
        self.assertFalse(state["native_failure"])
        self.assertFalse(state["audio_device_opened"])


if __name__ == "__main__":
    unittest.main()
