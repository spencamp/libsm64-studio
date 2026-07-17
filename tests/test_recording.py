import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "recording.py"
SPEC = importlib.util.spec_from_file_location("libsm64_recording", MODULE_PATH)
recording = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recording)


class FakeVertices:
    def __init__(self, coordinates):
        self.coordinates = coordinates

    def __len__(self):
        return len(self.coordinates) // 3

    def foreach_get(self, attribute, output):
        assert attribute == "co"
        for index, value in enumerate(self.coordinates):
            output[index] = value


class FakeMesh:
    def __init__(self, coordinates):
        self.vertices = FakeVertices(coordinates)


class FrameMappingTests(unittest.TestCase):
    def test_30_to_30(self):
        self.assertEqual(recording.sample_target_frame(10, 3, 30), 13.0)

    def test_30_to_24(self):
        self.assertAlmostEqual(recording.sample_target_frame(10, 3, 24), 12.4)

    def test_30_to_60(self):
        self.assertEqual(recording.sample_target_frame(10, 3, 60), 16.0)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.recorder = recording.GeometryRecorder()
        self.mesh = FakeMesh([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    def test_state_transitions_and_coordinate_copy(self):
        self.recorder.start(42, 24)
        self.assertTrue(self.recorder.active)
        self.assertEqual(self.recorder.status, self.recorder.RECORDING)
        self.assertTrue(self.recorder.capture_mesh(self.mesh, 7))
        self.mesh.vertices.coordinates[0] = 99.0
        samples = self.recorder.freeze_for_bake()
        self.assertFalse(self.recorder.active)
        self.assertEqual(self.recorder.status, self.recorder.BAKING)
        self.assertEqual(samples[0][0], 0.0)
        self.recorder.complete("LibSM64 Mario Bake")
        self.assertEqual(self.recorder.status, self.recorder.COMPLETE)
        self.assertEqual(self.recorder.sample_count, 1)
        self.assertFalse(self.recorder.has_pending_samples)

    def test_idle_ticks_do_not_capture_samples(self):
        self.assertFalse(self.recorder.capture_mesh(self.mesh, 1))
        self.assertEqual(self.recorder.sample_count, 0)
        self.assertEqual(self.recorder.status, self.recorder.IDLE)

    def test_duplicate_sample_is_ignored(self):
        self.recorder.start(1, 30)
        self.assertTrue(self.recorder.capture_mesh(self.mesh, 4))
        self.assertFalse(self.recorder.capture_mesh(self.mesh, 4))
        self.assertEqual(self.recorder.sample_count, 1)

    def test_empty_recording_is_rejected_and_preserved_as_error(self):
        self.recorder.start(1, 30)
        with self.assertRaises(recording.RecordingError):
            self.recorder.freeze_for_bake()
        self.assertEqual(self.recorder.status, self.recorder.ERROR)

    def test_cancel_discards_samples(self):
        self.recorder.start(1, 60)
        self.recorder.capture_mesh(self.mesh, 1)
        self.recorder.cancel()
        self.assertEqual(self.recorder.status, self.recorder.IDLE)
        self.assertEqual(self.recorder.sample_count, 0)

    def test_second_recording_starts_with_fresh_timing_and_sample_origin(self):
        self.recorder.start(10, 24)
        self.recorder.capture_mesh(self.mesh, 100)
        self.recorder.freeze_for_bake()
        self.recorder.complete("Take 001")

        self.recorder.start(80, 60)
        self.assertEqual(self.recorder.start_frame, 80.0)
        self.assertEqual(self.recorder.target_fps, 60.0)
        self.assertEqual(self.recorder.sample_count, 0)
        self.assertIsNone(self.recorder.last_sample_id)
        self.assertTrue(self.recorder.capture_mesh(self.mesh, 100))

    def test_bake_failure_can_preserve_samples_until_explicit_cancel(self):
        self.recorder.start(1, 30)
        self.recorder.capture_mesh(self.mesh, 1)
        self.recorder.fail("injected bake failure", preserve_samples=True)
        self.assertFalse(self.recorder.active)
        self.assertTrue(self.recorder.has_pending_samples)
        self.recorder.cancel()
        self.assertFalse(self.recorder.has_pending_samples)
        self.assertEqual(self.recorder.status, self.recorder.IDLE)


if __name__ == "__main__":
    unittest.main()
