from array import array
import ctypes as ct
import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "collision_cache.py"
SPEC = importlib.util.spec_from_file_location("libsm64_collision_cache", MODULE_PATH)
collision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collision)


class Surface(ct.Structure):
    _fields_ = [
        ('surftype', ct.c_int16), ('force', ct.c_int16), ('terrain', ct.c_uint16),
        ('v0x', ct.c_int16), ('v0y', ct.c_int16), ('v0z', ct.c_int16),
        ('v1x', ct.c_int16), ('v1y', ct.c_int16), ('v1z', ct.c_int16),
        ('v2x', ct.c_int16), ('v2y', ct.c_int16), ('v2z', ct.c_int16),
    ]


class CollisionMathTests(unittest.TestCase):
    def test_chunk_coordinate_handles_negative_space(self):
        self.assertEqual(collision.chunk_coordinate((0, 9.9, -0.1), 10), (0, 0, -1))
        self.assertEqual(collision.chunk_coordinate((-10, 10, 20), 10), (-1, 1, 2))

    def test_safe_dimension_is_derived_from_abi_scale_and_radius(self):
        native = collision.safe_chunk_size_native()
        expected = int((collision.NATIVE_COORD_MAX - collision.NATIVE_SAFETY_MARGIN) // 1.5)
        self.assertEqual(native, expected)
        self.assertAlmostEqual(collision.chunk_size_for_scale(50), native / 50)
        self.assertLessEqual(1.5 * native, collision.NATIVE_COORD_MAX - collision.NATIVE_SAFETY_MARGIN)

    def test_bounds_intersect_zero_one_and_multiple_chunks(self):
        self.assertEqual(collision.chunks_for_bounds((1, 1, 1), (2, 2, 2), 10), ((0, 0, 0),))
        self.assertEqual(len(collision.chunks_for_bounds((1, 1, 1), (21, 2, 2), 10)), 3)
        self.assertFalse(collision.bounds_intersect(((0, 0, 0), (1, 1, 1)), ((2, 2, 2), (3, 3, 3))))

    def test_large_triangle_is_clipped_into_multiple_chunks(self):
        cache = collision.CollisionCache()
        triangle = array('f', (-5, 0, 0, 25, 0, 0, 5, 9, 0))
        entry = collision.ObjectEntry(
            ("large",), "fingerprint", ((-5, 0, 0), (25, 9, 0)),
            triangle, array('H', (0,)), 0,
        )
        chunks = []
        for coordinate in ((-1, 0, 0), (0, 0, 0), (1, 0, 0), (2, 0, 0)):
            chunk = cache._build_chunk(coordinate, [entry], 1, 10, "fp")
            self.assertGreater(chunk.surface_count, 0)
            chunks.append(chunk)
        native, count, _origin = cache._assemble_native(chunks[1:4], (1, 0, 0), 1, Surface)
        self.assertEqual(len(native), count)
        self.assertGreater(count, 3)
        for surface in native:
            for field, _ctype in Surface._fields_[3:]:
                self.assertGreaterEqual(getattr(surface, field), collision.NATIVE_COORD_MIN)
                self.assertLessEqual(getattr(surface, field), collision.NATIVE_COORD_MAX)

    def test_chunk_key_changes_only_with_contributors(self):
        cache = collision.CollisionCache()
        first = collision.ObjectEntry(("a",), "one", ((0, 0, 0), (1, 1, 1)), array('f'), array('H'), 0)
        changed = collision.ObjectEntry(("a",), "two", first.bounds, array('f'), array('H'), 0)
        unrelated = collision.ObjectEntry(("b",), "far", ((100, 0, 0), (101, 1, 1)), array('f'), array('H'), 0)
        key = cache._chunk_fingerprint((0, 0, 0), [first], 50)
        self.assertNotEqual(key, cache._chunk_fingerprint((0, 0, 0), [changed], 50))
        self.assertEqual(key, cache._chunk_fingerprint((0, 0, 0), [first], 50))
        self.assertEqual(key, cache._chunk_fingerprint((0, 0, 0), [first], 50))
        self.assertNotEqual(key, cache._chunk_fingerprint((0, 0, 0), [first, unrelated], 50))


if __name__ == "__main__":
    unittest.main()
