from array import array
from dataclasses import FrozenInstanceError
import importlib.util
import math
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
    def test_sample_timing_at_common_target_rates(self):
        expected = {24: 12.4, 30: 13.0, 60: 16.0}
        for target_fps, target_frame in expected.items():
            with self.subTest(target_fps=target_fps):
                self.assertAlmostEqual(
                    recording.sample_target_frame(10, 3, target_fps), target_frame
                )

    def test_held_runtime_mapping_at_24_30_60_and_fractional_frames(self):
        cases = (
            (24.0, 10.7999, 0),
            (24.0, 10.8, 1),
            (24.0, 11.5999, 1),
            (30.0, 11.9999, 1),
            (30.0, 12.0, 2),
            (60.0, 13.9999, 1),
            (60.0, 14.0, 2),
        )
        for target_fps, frame, expected in cases:
            with self.subTest(target_fps=target_fps, frame=frame):
                self.assertEqual(
                    recording.held_runtime_sample_index(
                        frame, 10.0, target_fps, 4
                    ),
                    expected,
                )
        self.assertEqual(recording.held_runtime_sample_index(-100, 10, 30, 4), 0)
        self.assertEqual(recording.held_runtime_sample_index(100, 10, 30, 4), 3)


class RuntimeMetadataTests(unittest.TestCase):
    def metadata(self):
        return recording.MarioRuntimeMetadata(
            native_position=(1.25, -2.5, 3.75),
            native_velocity=(-4.5, 5.25, -6.75),
            face_angle=3.125,
            forward_velocity=-12.5,
            health=0x880,
            action=0xFFFFFFFF,
            animation_id=-2147483648,
            animation_frame=-32768,
            flags=0xFEDCBA98,
            particle_flags=0x89ABCDEF,
            invincibility_timer=32767,
        )

    def test_immutable_exact_round_trip(self):
        metadata = self.metadata()
        with self.assertRaises(FrozenInstanceError):
            metadata.action = 0
        record = metadata.to_json_record()
        restored = recording.MarioRuntimeMetadata.from_json_record(record)
        self.assertEqual(restored, metadata)
        self.assertEqual(restored.action, 0xFFFFFFFF)
        self.assertEqual(restored.animation_id, -2147483648)
        self.assertEqual(restored.particle_flags, 0x89ABCDEF)

    def test_float_and_integer_validation(self):
        base = self.metadata().to_json_record()
        for field, value in (
            ("native_position", (1.0, 2.0)),
            ("native_velocity", (1.0, math.nan, 3.0)),
            ("face_angle", math.inf),
            ("forward_velocity", -math.inf),
            ("action", -1),
            ("flags", 0x100000000),
            ("animation_frame", 32768),
            ("health", True),
        ):
            malformed = dict(base)
            malformed[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(recording.RecordingError):
                    recording.MarioRuntimeMetadata.from_json_record(malformed)

    def test_performance_sample_owns_same_tick_metadata(self):
        metadata = self.metadata()
        sample = recording.PerformanceSample(
            array('f', [0.0, 1.0, 2.0]), (4.0, 5.0, 6.0), 0.5, metadata
        )
        self.assertIs(sample.runtime_metadata, metadata)


class TransformMathTests(unittest.TestCase):
    def assert_coordinates_close(self, actual, expected, tolerance=1e-5):
        self.assertEqual(len(actual), len(expected))
        for left, right in zip(actual, expected):
            self.assertLessEqual(abs(left - right), tolerance)

    def assert_round_trip(self, local, location, angle):
        world = recording.mario_local_to_world(local, location, angle)
        reconstructed_local = recording.world_to_mario_local(world, location, angle)
        reconstructed_world = recording.mario_local_to_world(
            reconstructed_local, location, angle
        )
        self.assert_coordinates_close(reconstructed_local, local)
        self.assert_coordinates_close(reconstructed_world, world)

    def test_reconstruction_cases_and_rotation_sign(self):
        local = array('f', [1.0, 0.0, 2.0, -2.0, 3.5, -4.0])
        cases = (
            ((0.0, 0.0, 0.0), 0.0),
            ((7.5, -2.25, 11.0), 0.0),
            ((10.0, 20.0, 30.0), math.pi / 2.0),
            ((10.0, 20.0, 30.0), -math.pi / 2.0),
            ((-3.25, 8.5, 1.75), 0.731),
        )
        for location, angle in cases:
            with self.subTest(location=location, angle=angle):
                self.assert_round_trip(local, location, angle)

        positive = recording.mario_local_to_world(
            array('f', [1.0, 0.0, 0.0]), (10.0, 20.0, 30.0), math.pi / 2.0
        )
        negative = recording.mario_local_to_world(
            array('f', [1.0, 0.0, 0.0]), (10.0, 20.0, 30.0), -math.pi / 2.0
        )
        self.assert_coordinates_close(positive, (10.0, 21.0, 30.0))
        self.assert_coordinates_close(negative, (10.0, 19.0, 30.0))

    def test_localization_reconstructs_original_world_coordinates(self):
        world = array('f', [4.25, -7.5, 2.0, 9.0, 3.25, -5.5])
        location = (1.5, -2.75, 4.0)
        angle = -1.137
        local = recording.world_to_mario_local(world, location, angle)
        reconstructed = recording.mario_local_to_world(local, location, angle)
        self.assert_coordinates_close(reconstructed, world)

    def test_angle_unwrap_uses_nearest_equivalent_across_boundary(self):
        angles = recording.unwrap_face_angles((3.10, -3.10))
        self.assertAlmostEqual(angles[0], 3.10)
        self.assertAlmostEqual(angles[1], 2.0 * math.pi - 3.10)
        self.assertLess(abs(angles[1] - angles[0]), 0.1)

    def test_angle_unwrap_remains_continuous_across_multiple_wraps(self):
        raw = (3.0, -3.0, -1.0, 1.0, 3.0, -3.0, -1.0)
        unwrapped = recording.unwrap_face_angles(raw)
        self.assertTrue(all(right > left for left, right in zip(unwrapped, unwrapped[1:])))
        self.assertTrue(
            all(abs(right - left) <= math.pi for left, right in zip(unwrapped, unwrapped[1:]))
        )
        self.assertGreater(unwrapped[-1], 2.0 * math.pi + unwrapped[0])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.recorder = recording.GeometryRecorder()
        self.mesh = FakeMesh([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        self.location = (10.5, -20.25, 3.75)
        self.angle = 1.125

    def capture(self, sample_id=1, location=None, angle=None):
        return self.recorder.capture_mesh(
            self.mesh,
            sample_id,
            self.location if location is None else location,
            self.angle if angle is None else angle,
        )

    def test_state_transitions_structured_metadata_and_coordinate_copy(self):
        self.recorder.start(42, 24)
        self.assertTrue(self.recorder.active)
        self.assertEqual(self.recorder.status, self.recorder.RECORDING)
        self.assertTrue(self.capture(7))
        sample = self.recorder.samples[0]
        self.assertIsInstance(sample, recording.PerformanceSample)
        self.assertIsInstance(sample.coordinates, array)
        self.assertEqual(sample.coordinates.typecode, 'f')
        self.assertEqual(sample.world_location, self.location)
        self.assertEqual(sample.face_angle, self.angle)
        self.mesh.vertices.coordinates[0] = 99.0
        samples = self.recorder.freeze_for_bake()
        self.assertFalse(self.recorder.active)
        self.assertEqual(self.recorder.status, self.recorder.BAKING)
        self.assertEqual(samples[0].coordinates[0], 0.0)
        self.recorder.complete("LibSM64 Mario Bake")
        self.assertEqual(self.recorder.status, self.recorder.COMPLETE)
        self.assertEqual(self.recorder.sample_count, 1)
        self.assertFalse(self.recorder.has_pending_samples)

    def test_performance_sample_copies_input_array(self):
        source = array('f', [1.0, 2.0, 3.0])
        sample = recording.PerformanceSample(source, (4.0, 5.0, 6.0), 0.25)
        source[0] = 99.0
        self.assertEqual(sample.coordinates[0], 1.0)

    def test_idle_ticks_do_not_capture_samples(self):
        self.assertFalse(self.capture())
        self.assertEqual(self.recorder.sample_count, 0)
        self.assertEqual(self.recorder.status, self.recorder.IDLE)

    def test_duplicate_sample_is_ignored(self):
        self.recorder.start(1, 30)
        self.assertTrue(self.capture(4))
        self.assertFalse(self.capture(4, (99.0, 98.0, 97.0), -2.0))
        self.assertEqual(self.recorder.sample_count, 1)
        self.assertEqual(self.recorder.samples[0].world_location, self.location)

    def test_invalid_or_non_finite_transform_metadata_is_rejected(self):
        malformed = ((1.0, 2.0), (1.0, 2.0, 3.0, 4.0), None)
        for location in malformed:
            with self.subTest(location=location):
                self.recorder.start(1, 30)
                with self.assertRaises(recording.RecordingError):
                    self.recorder.capture_mesh(
                        self.mesh, 1, location, self.angle
                    )
                self.assertEqual(self.recorder.status, self.recorder.ERROR)
        for location in ((math.nan, 0.0, 0.0), (0.0, math.inf, 0.0)):
            with self.subTest(location=location):
                self.recorder.start(1, 30)
                with self.assertRaises(recording.RecordingError):
                    self.capture(1, location=location)
        for angle in (math.nan, math.inf, -math.inf, "not-an-angle"):
            with self.subTest(angle=angle):
                self.recorder.start(1, 30)
                with self.assertRaises(recording.RecordingError):
                    self.capture(1, angle=angle)

    def test_topology_change_is_rejected(self):
        self.recorder.start(1, 30)
        self.capture(1)
        self.mesh = FakeMesh([0.0, 1.0, 2.0])
        with self.assertRaises(recording.RecordingError):
            self.capture(2)
        self.assertEqual(self.recorder.status, self.recorder.ERROR)
        self.assertEqual(self.recorder.sample_count, 1)

    def test_coordinate_only_samples_are_not_accepted(self):
        with self.assertRaises(recording.RecordingError):
            recording.validate_performance_sample(array('f', [0.0, 0.0, 0.0]))

    def test_empty_recording_is_rejected_and_preserved_as_error(self):
        self.recorder.start(1, 30)
        with self.assertRaises(recording.RecordingError):
            self.recorder.freeze_for_bake()
        self.assertEqual(self.recorder.status, self.recorder.ERROR)

    def test_cancel_discards_samples(self):
        self.recorder.start(1, 60)
        self.capture()
        self.recorder.cancel()
        self.assertEqual(self.recorder.status, self.recorder.IDLE)
        self.assertEqual(self.recorder.sample_count, 0)

    def test_second_recording_starts_with_fresh_timing_and_sample_origin(self):
        self.recorder.start(10, 24)
        self.capture(100)
        self.recorder.freeze_for_bake()
        self.recorder.complete("Take 001")

        self.recorder.start(80, 60)
        self.assertEqual(self.recorder.start_frame, 80.0)
        self.assertEqual(self.recorder.target_fps, 60.0)
        self.assertEqual(self.recorder.sample_count, 0)
        self.assertIsNone(self.recorder.last_sample_id)
        self.assertTrue(self.capture(100))

    def test_bake_failure_can_preserve_samples_until_explicit_cancel(self):
        self.recorder.start(1, 30)
        self.capture()
        self.recorder.fail("injected bake failure", preserve_samples=True)
        self.assertFalse(self.recorder.active)
        self.assertTrue(self.recorder.has_pending_samples)
        self.recorder.cancel()
        self.assertFalse(self.recorder.has_pending_samples)
        self.assertEqual(self.recorder.status, self.recorder.IDLE)


if __name__ == "__main__":
    unittest.main()
