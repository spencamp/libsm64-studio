import ctypes as ct
import importlib.util
import math
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("collision_cache", ROOT / "collision_cache.py")
collision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collision)


class Surface(ct.Structure):
    _fields_ = [
        ("type", ct.c_int16), ("force", ct.c_int16), ("terrain", ct.c_uint16),
        ("vertices", (ct.c_int32 * 3) * 3),
    ]


def normal(triangle):
    a = tuple(triangle[1][i] - triangle[0][i] for i in range(3))
    b = tuple(triangle[2][i] - triangle[0][i] for i in range(3))
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


class CollisionSpatialTests(unittest.TestCase):
    def test_collision_roles_default_static_and_are_explicit(self):
        class FakeObject(dict):
            pass

        default = FakeObject()
        self.assertEqual(collision.collision_role(default), collision.COLLISION_ROLE_STATIC)
        default[collision.COLLISION_ROLE_PROPERTY] = collision.COLLISION_ROLE_MOVING_PLATFORM
        self.assertEqual(
            collision.collision_role(default), collision.COLLISION_ROLE_MOVING_PLATFORM
        )
        default[collision.COLLISION_ROLE_PROPERTY] = collision.COLLISION_ROLE_EXCLUDED
        self.assertEqual(collision.collision_role(default), collision.COLLISION_ROLE_EXCLUDED)
        default[collision.COLLISION_ROLE_PROPERTY] = "INVALID"
        self.assertEqual(collision.collision_role(default), collision.COLLISION_ROLE_STATIC)

    def test_chunk_coordinates_and_negative_space(self):
        self.assertEqual(collision.chunk_coordinate((0.0, 255.9, 99.0)), (0, 0))
        self.assertEqual(collision.chunk_coordinate((-0.01, -256.0, 0.0)), (-1, -1))
        self.assertEqual(collision.chunk_coordinate((256.0, -256.01, 0.0)), (1, -2))

    def test_preload_retention_hysteresis_and_limit(self):
        plan = collision.plan_transition({(0, 0)}, (1, 0))
        self.assertEqual(len(plan.required), 9)
        self.assertEqual(len(plan.retained), 25)
        self.assertNotIn((0, 0), plan.outgoing)
        self.assertLessEqual(len((set(plan.required) | {(0, 0)}) - set(plan.outgoing)), 25)
        stable = collision.plan_transition(plan.required, (1, 0))
        self.assertEqual(stable.incoming, ())
        self.assertEqual(stable.outgoing, ())

    def test_spanning_triangle_clips_every_chunk_without_winding_change(self):
        triangle = ((-10.0, 10.0, 0.0), (530.0, 20.0, 80.0), (10.0, 220.0, 10.0))
        source_normal = normal(triangle)
        touched = collision.chunks_for_bounds((-10.0, 10.0), (530.0, 220.0))
        populated = 0
        for key in touched:
            fragments = collision.clip_triangle_to_chunk(triangle, key)
            for fragment in fragments:
                populated += 1
                clipped_normal = normal(fragment)
                self.assertGreaterEqual(
                    sum(source_normal[i] * clipped_normal[i] for i in range(3)), 0.0
                )
                low, high = collision.chunk_bounds(key)
                for vertex in fragment:
                    self.assertGreaterEqual(vertex[0], low[0] - 1e-6)
                    self.assertLessEqual(vertex[0], high[0] + 1e-6)
                    self.assertGreaterEqual(vertex[1], low[1] - 1e-6)
                    self.assertLessEqual(vertex[1], high[1] + 1e-6)
        self.assertGreater(populated, 3)

    def test_boundary_wall_is_present_on_both_sides(self):
        wall = ((256.0, 0.0, 0.0), (256.0, 100.0, 0.0), (256.0, 0.0, 50.0))
        self.assertTrue(collision.clip_triangle_to_chunk(wall, (0, 0)))
        self.assertTrue(collision.clip_triangle_to_chunk(wall, (1, 0)))

    def test_degenerate_and_nonfinite_geometry(self):
        flat = ((0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (2.0, 2.0, 2.0))
        self.assertEqual(collision.clip_triangle_to_chunk(flat, (0, 0)), ())
        with self.assertRaises(ValueError):
            collision.clip_triangle_to_chunk(((0, 0, 0), (math.nan, 1, 0), (1, 0, 0)), (0, 0))

    def test_local_native_payload_axis_mapping_metadata_and_overflow(self):
        record = collision.SurfaceRecord(
            7, -3, 2,
            ((256.0, -256.0, 10.0), (257.0, -255.0, 12.0), (258.0, -254.0, 11.0)),
        )
        chunk = collision.PreparedChunk((1, -1), "fp", (record,))
        surfaces, translation = collision.native_chunk_payload(
            chunk, 50.0, (10.0, 20.0, 5.0), Surface
        )
        self.assertEqual(translation, (12300.0, 0.0, 13800.0))
        self.assertEqual((surfaces[0].type, surfaces[0].force, surfaces[0].terrain), (7, -3, 2))
        self.assertEqual(tuple(surfaces[0].vertices[0]), (0, 250, 0))
        self.assertEqual(tuple(surfaces[0].vertices[1]), (50, 350, -50))
        huge = collision.SurfaceRecord(0, 0, 0, (((2**31) + 256.0, -256.0, 0.0),) * 3)
        with self.assertRaises(OverflowError):
            collision.native_chunk_payload(
                collision.PreparedChunk((1, -1), "huge", (huge,)), 50.0,
                (0.0, 0.0, 0.0), Surface,
            )

    def test_transition_order_is_deterministic(self):
        active = {(x, 0) for x in range(-3, 4)}
        plan = collision.plan_transition(active, (1, 0))
        self.assertEqual(plan.incoming, tuple(sorted(plan.incoming)))
        self.assertEqual(plan.outgoing, tuple(sorted(plan.outgoing)))


if __name__ == "__main__":
    unittest.main()
