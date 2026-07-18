"""Headless Blender regression coverage for Mario's bulk coordinate update path."""

from array import array
from pathlib import Path
import inspect
import os
import sys

import bpy


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") == "1":
    expected_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(expected_root.parent))
else:
    sys.path.insert(0, str(root))

from libsm64_studio import mario
from libsm64_studio.recording import recorder


def make_mesh(name, vertex_count):
    vertices = [(float(index), float(index + 1), float(index + 2))
                for index in range(vertex_count)]
    faces = [tuple(range(index, index + 3)) for index in range(0, vertex_count, 3)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, [], faces)
    mesh.uv_layers.new(name="uv0")
    mesh.vertex_colors.new(name="Col")
    material = bpy.data.materials.new(name + " Material")
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name + " Object", mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj["unrelated"] = "preserve me"
    obj.shape_key_add(name="Basis")
    key = obj.shape_key_add(name="Pose")
    key.data[0].co = (101.0, 102.0, 103.0)

    for index, loop in enumerate(mesh.uv_layers.active.data):
        loop.uv = (index / 20.0, index / 30.0)
    for index, item in enumerate(mesh.vertex_colors.active.data):
        item.color = (index / 20.0, 0.25, 0.75, 1.0)
    mesh.update()
    return obj, mesh


def flat_coordinates(mesh):
    values = array('f', [0.0]) * (len(mesh.vertices) * 3)
    mesh.vertices.foreach_get("co", values)
    return tuple(values)


def evaluated_flat_coordinates(obj):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated_object = obj.evaluated_get(depsgraph)
    evaluated_mesh = evaluated_object.to_mesh()
    try:
        return flat_coordinates(evaluated_mesh)
    finally:
        evaluated_object.to_mesh_clear()


def assert_close(actual, expected, tolerance=1e-6):
    assert len(actual) == len(expected), (len(actual), len(expected))
    assert all(abs(left - right) <= tolerance for left, right in zip(actual, expected)), (
        actual, expected
    )


def set_native(vertices):
    mario.mario_geo.numTrianglesUsed = len(vertices) // 3
    for vertex_index, vertex in enumerate(vertices):
        for axis, value in enumerate(vertex):
            mario.mario_geo.position_data[vertex_index * 3 + axis] = value


def apply_reference_coordinates(mesh, vertices):
    """Reproduce the original direct x/z/y property assignments."""
    origin_x, origin_y, origin_z = mario.origin_offset
    scale = mario.SM64_SCALE_FACTOR
    for index, (native_x, native_y, native_z) in enumerate(vertices):
        mesh.vertices[index].co.x = origin_x + native_x / scale
        mesh.vertices[index].co.z = origin_z + native_y / scale
        mesh.vertices[index].co.y = origin_y - native_z / scale
    mesh.update()


source = inspect.getsource(mario)
assert "import bmesh" not in source
assert "bmesh.new" not in source
assert 'mesh.vertices.foreach_set("co", coordinates)' in source

# The destination buffer consumed by foreach_set is literal X, Y, Z order.
# Use fully asymmetric native/origin values so an X, Z, Y destination cannot pass.
mario.SM64_SCALE_FACTOR = 2.0
mario.origin_offset[:] = (10.0, 20.0, 30.0)
mario._invalidate_mesh_coordinate_cache()
assert_close(
    mario.native_position_to_blender(2.0, 6.0, 8.0),
    (11.0, 16.0, 33.0),
)
_, asymmetric_mesh = make_mesh("Asymmetric Mario", 3)
set_native(((2.0, 6.0, 8.0), (12.0, 18.0, 24.0), (-4.0, -10.0, -14.0)))
mario.update_mesh_data_fast(asymmetric_mesh)
asymmetric_actual = array('f', [0.0]) * 9
asymmetric_mesh.vertices.foreach_get("co", asymmetric_actual)
assert_close(asymmetric_actual[:3], (11.0, 16.0, 33.0))
assert tuple(asymmetric_actual[:3]) != (11.0, 33.0, 16.0)

# Compare multiple triangles against the exact direct-property behavior used by
# the pre-optimization updater, including the resulting Blender mesh values.
reference_vertices = (
    (2.0, 6.0, 8.0),
    (12.0, 18.0, 24.0),
    (-4.0, -10.0, -14.0),
    (30.0, -22.0, 16.0),
    (-18.0, 26.0, -34.0),
    (42.0, 38.0, -46.0),
)
reference_obj, reference_mesh = make_mesh("Reference Mario", 9)
bulk_obj, bulk_mesh = make_mesh("Reference Bulk Mario", 9)
apply_reference_coordinates(reference_mesh, reference_vertices)
set_native(reference_vertices)
mario.update_mesh_data_fast(bulk_mesh)
assert_close(flat_coordinates(bulk_mesh), flat_coordinates(reference_mesh))
assert_close(
    evaluated_flat_coordinates(bulk_obj),
    evaluated_flat_coordinates(reference_obj),
)

# Exercise Live Mario's full-to-fast transition. The full updater establishes
# the first pose and invalidates the cache; the subsequent bulk update must
# match direct property assignments without reordering its seeded coordinates.
transition_obj, transition_mesh = make_mesh("Transition Mario", 9)
transition_reference_obj, transition_reference = make_mesh(
    "Transition Reference Mario", 9
)
transition_first = reference_vertices[:3]
transition_second = (
    (14.0, -6.0, 28.0),
    (-16.0, 32.0, 40.0),
    (44.0, 50.0, -52.0),
)
set_native(transition_first)
mario.update_mesh_data(transition_mesh)
transition_reference.vertices.foreach_set("co", flat_coordinates(transition_mesh))
transition_reference.update()
assert mario._mesh_coordinate_cache.coordinates is None
set_native(transition_second)
mario.update_mesh_data_fast(transition_mesh)
apply_reference_coordinates(transition_reference, transition_second)
assert_close(flat_coordinates(transition_mesh), flat_coordinates(transition_reference))
assert_close(
    evaluated_flat_coordinates(transition_obj),
    evaluated_flat_coordinates(transition_reference_obj),
)

mario.SM64_SCALE_FACTOR = 50
mario.origin_offset[:] = (10.0, -20.0, 3.0)
mario._invalidate_mesh_coordinate_cache()
obj, mesh = make_mesh("Bulk Mario", 9)

native_vertices = (
    (50.0, 100.0, 150.0),
    (-100.0, 250.0, -50.0),
    (0.0, -100.0, 200.0),
    (25.0, 75.0, 125.0),
    (-25.0, -75.0, -125.0),
    (500.0, 0.0, -500.0),
)
set_native(native_vertices)
inactive_before = flat_coordinates(mesh)[18:]

uv_before = tuple(tuple(item.uv) for item in mesh.uv_layers.active.data)
colors_before = tuple(tuple(item.color) for item in mesh.vertex_colors.active.data)
materials_before = tuple(material.as_pointer() for material in mesh.materials)
topology_before = (
    len(mesh.vertices),
    tuple(tuple(edge.vertices) for edge in mesh.edges),
    tuple(tuple(polygon.vertices) for polygon in mesh.polygons),
)
shape_keys_before = tuple(
    tuple(tuple(point.co) for point in block.data)
    for block in mesh.shape_keys.key_blocks
)

mario.update_mesh_data_fast(mesh)
expected_active = (
    11.0, -23.0, 5.0,
    8.0, -19.0, 8.0,
    10.0, -24.0, 1.0,
    10.5, -22.5, 4.5,
    9.5, -17.5, 1.5,
    20.0, -10.0, 3.0,
)
first_coordinates = flat_coordinates(mesh)
assert_close(first_coordinates[:18], expected_active)
assert_close(first_coordinates[18:], inactive_before)
first_buffer = mario._mesh_coordinate_cache.coordinates
assert isinstance(first_buffer, array)
assert len(first_buffer) == len(mesh.vertices) * 3

# Repeated calls reuse the same full-size buffer, while a lower active count
# leaves both previously active and never-active vertices unchanged.
set_native(((100.0, 200.0, 300.0), (150.0, 250.0, 350.0), (200.0, 300.0, 400.0)))
mario.update_mesh_data_fast(mesh)
assert mario._mesh_coordinate_cache.coordinates is first_buffer
second_coordinates = flat_coordinates(mesh)
assert_close(second_coordinates[:9], (
    12.0, -26.0, 7.0,
    13.0, -27.0, 8.0,
    14.0, -28.0, 9.0,
))
assert_close(second_coordinates[9:], first_coordinates[9:])

# The position-only path must not disturb any adjacent Blender mesh/object data.
assert tuple(tuple(item.uv) for item in mesh.uv_layers.active.data) == uv_before
assert tuple(tuple(item.color) for item in mesh.vertex_colors.active.data) == colors_before
assert tuple(material.as_pointer() for material in mesh.materials) == materials_before
assert (
    len(mesh.vertices),
    tuple(tuple(edge.vertices) for edge in mesh.edges),
    tuple(tuple(polygon.vertices) for polygon in mesh.polygons),
) == topology_before
assert tuple(
    tuple(tuple(point.co) for point in block.data)
    for block in mesh.shape_keys.key_blocks
) == shape_keys_before
assert obj["unrelated"] == "preserve me"

# Recording observes coordinates only after the bulk assignment has completed.
recorder.cancel()
recorder.start(1.0, 30.0)
set_native(((300.0, 400.0, 500.0), (350.0, 450.0, 550.0), (400.0, 500.0, 600.0)))
mario.update_mesh_data_fast(mesh)
recorded_location = mario.native_position_to_blender(125.0, -75.0, 225.0)
recorded_face_angle = -1.375
assert recorder.capture_mesh(mesh, 1, recorded_location, recorded_face_angle)
recorded_sample = recorder.samples[-1]
assert_close(recorded_sample.coordinates, flat_coordinates(mesh))
assert_close(recorded_sample.world_location, recorded_location)
assert recorded_sample.face_angle == recorded_face_angle
recorder.cancel()

# A full UV/color update invalidates the buffer. The next fast call seeds a new
# buffer from the current mesh, so an independently changed inactive vertex
# cannot be overwritten with an older cached value.
mesh.vertices[4].co = (91.0, 92.0, 93.0)
set_native(((5.0, 10.0, 15.0), (20.0, 25.0, 30.0), (35.0, 40.0, 45.0)))
mario.update_mesh_data(mesh)
assert mario._mesh_coordinate_cache.coordinates is None
set_native(((50.0, 55.0, 60.0), (65.0, 70.0, 75.0), (80.0, 85.0, 90.0)))
mario.update_mesh_data_fast(mesh)
assert_close(tuple(mesh.vertices[4].co), (91.0, 92.0, 93.0))
assert mario._mesh_coordinate_cache.coordinates is not first_buffer

# Mesh replacement and vertex-count changes both create and synchronize a new
# correctly sized buffer, preserving each replacement mesh's inactive suffix.
_, replacement = make_mesh("Replacement Mario", 9)
replacement_inactive = flat_coordinates(replacement)[9:]
old_buffer = mario._mesh_coordinate_cache.coordinates
mario.update_mesh_data_fast(replacement)
assert mario._mesh_coordinate_cache.coordinates is not old_buffer
assert_close(flat_coordinates(replacement)[9:], replacement_inactive)

_, resized = make_mesh("Resized Mario", 12)
resized_inactive = flat_coordinates(resized)[9:]
mario.update_mesh_data_fast(resized)
assert len(mario._mesh_coordinate_cache.coordinates) == 36
assert_close(flat_coordinates(resized)[9:], resized_inactive)

mario.mario_geo.numTrianglesUsed = 5
try:
    mario.update_mesh_data_fast(resized)
    raise AssertionError("Oversized active Mario geometry should fail")
except ValueError as exc:
    assert "15 active vertices" in str(exc)
    assert "capacity for 12" in str(exc)

print("libsm64 bulk mesh update Blender test passed")
