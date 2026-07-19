"""Blender 5.2 fake-native regression for explicit moving platforms."""

from pathlib import Path
import math
import os
import sys
from types import SimpleNamespace

import bpy
import mathutils


root = Path(__file__).resolve().parents[1]
if os.environ.get("LIBSM64_TEST_INSTALLED") == "1":
    install_root = Path(os.environ["LIBSM64_EXPECTED_INSTALL_ROOT"]).resolve()
    sys.path.insert(0, str(install_root.parent))
else:
    sys.path.insert(0, str(root))

import libsm64_studio as addon
from libsm64_studio import mario
from libsm64_studio.collision_cache import CollisionCache, PreparedChunk, SurfaceRecord
from libsm64_studio.collision_types import COLLISION_TYPES
from libsm64_studio.recording import bake_shape_keys, discard_baked_take, recorder


class Call:
    def __init__(self, library, name, result=None, failure=None):
        self.library = library
        self.name = name
        self.result = result
        self.failure = failure
        self.calls = []
        self.argtypes = None
        self.restype = object()

    def __call__(self, *args):
        self.calls.append(args)
        self.library.events.append(self.name)
        if self.failure is not None:
            raise self.failure
        return self.result


class SurfaceCreateCall(Call):
    def __call__(self, descriptor_pointer):
        self.calls.append((descriptor_pointer,))
        self.library.events.append(self.name)
        descriptor = mario.ct.cast(
            descriptor_pointer, mario.ct.POINTER(mario.SM64SurfaceObject)
        ).contents
        payload = {
            "position": tuple(descriptor.transform.position),
            "rotation": tuple(descriptor.transform.eulerRotation),
            "surface_count": int(descriptor.surfaceCount),
            "vertices": tuple(
                tuple(tuple(descriptor.surfaces[index].vertices[corner]) for corner in range(3))
                for index in range(descriptor.surfaceCount)
            ),
        }
        self.library.created_payloads.append(payload)
        object_id = self.library.next_surface_id
        self.library.next_surface_id += 1
        return object_id


class SurfaceMoveCall(Call):
    def __call__(self, object_id, transform_pointer):
        self.calls.append((object_id, transform_pointer))
        self.library.events.append(self.name)
        if self.failure is not None:
            raise self.failure
        transform = mario.ct.cast(
            transform_pointer, mario.ct.POINTER(mario.SM64ObjectTransform)
        ).contents
        self.library.move_payloads.append({
            "object_id": int(object_id),
            "position": tuple(transform.position),
            "rotation": tuple(transform.eulerRotation),
        })


class SurfaceDeleteCall(Call):
    def __call__(self, object_id):
        super().__call__(object_id)
        self.library.deleted_ids.append(int(object_id))


class FakeLibrary:
    def __init__(self):
        self.events = []
        self.next_surface_id = 100
        self.created_payloads = []
        self.move_payloads = []
        self.deleted_ids = []
        self.sm64_global_init = Call(self, "global_init")
        self.sm64_global_terminate = Call(self, "global_terminate")
        self.sm64_static_surfaces_load = Call(self, "static_load")
        self.sm64_mario_create = Call(self, "mario_create", 7)
        self.sm64_mario_tick = Call(self, "mario_tick")
        self.sm64_mario_delete = Call(self, "mario_delete")
        self.sm64_set_mario_action = Call(self, "set_action")
        self.sm64_set_mario_animation = Call(self, "set_animation")
        self.sm64_set_mario_anim_frame = Call(self, "set_anim_frame")
        self.sm64_set_mario_state = Call(self, "set_state")
        self.sm64_set_mario_position = Call(self, "set_position")
        self.sm64_set_mario_faceangle = Call(self, "set_faceangle")
        self.sm64_set_mario_velocity = Call(self, "set_velocity")
        self.sm64_set_mario_forward_velocity = Call(self, "set_forward_velocity")
        self.sm64_set_mario_health = Call(self, "set_health")
        self.sm64_set_mario_invincibility = Call(self, "set_invincibility")
        self.sm64_surface_object_create = SurfaceCreateCall(self, "surface_create")
        self.sm64_surface_object_move = SurfaceMoveCall(self, "surface_move")
        self.sm64_surface_object_delete = SurfaceDeleteCall(self, "surface_delete")


def mesh_object(name, vertices, faces, location=(0.0, 0.0, 0.0)):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def prepared(keys):
    result = {}
    for key in keys:
        x = key[0] * mario.CHUNK_SIZE_BLENDER
        y = key[1] * mario.CHUNK_SIZE_BLENDER
        triangle = (
            (x + 1.0, y + 1.0, 0.0),
            (x + 40.0, y + 40.0, 0.0),
            (x + 40.0, y + 1.0, 0.0),
        )
        result[key] = PreparedChunk(
            key,
            "chunk-{}-{}".format(*key),
            (SurfaceRecord(0, 0, 0, triangle),),
        )
    return result


def ensure_mario_data(_texture):
    mesh = bpy.data.meshes.get("libsm64_mario_mesh")
    if mesh is None:
        mesh = bpy.data.meshes.new("libsm64_mario_mesh")
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])


def matrix_close(first, second, tolerance=1.0e-5):
    return max(
        abs(float(first[row][column]) - float(second[row][column]))
        for row in range(3) for column in range(3)
    ) <= tolerance


bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
addon.register()

# Pure coordinate/rotation conversion reconstructs native ZXY matrices and
# handles signs, parents, combined rotations, and 180-degree boundaries.
mario.SM64_SCALE_FACTOR = 100.0
mario.origin_offset[:] = (10.0, -20.0, 30.0)
rotation_cases = (
    (0.0, 0.0, 0.0),
    (math.radians(30.0), 0.0, 0.0),
    (0.0, math.radians(-45.0), 0.0),
    (0.0, 0.0, math.radians(90.0)),
    (math.radians(25.0), math.radians(-35.0), math.radians(70.0)),
    (0.0, 0.0, math.radians(179.999)),
    (0.0, 0.0, math.radians(-180.0)),
)
for index, euler_values in enumerate(rotation_cases):
    location = (-12.5 + index, 8.25 - index, 42.0 + index)
    matrix = mathutils.Matrix.LocRotScale(
        mathutils.Vector(location),
        mathutils.Euler(euler_values, 'XYZ'),
        mathutils.Vector((1.0, 1.0, 1.0)),
    )
    converted = mario.blender_matrix_to_native_transform(matrix, 100.0, (10.0, -20.0, 30.0))
    expected_position = (
        (location[0] - 10.0) * 100.0,
        (location[2] - 30.0) * 100.0,
        (-location[1] - 20.0) * 100.0,
    )
    assert all(math.isclose(a, b, abs_tol=1.0e-4) for a, b in zip(converted.position, expected_position))
    native_matrix = mathutils.Matrix((
        converted.rotation_matrix[0:3],
        converted.rotation_matrix[3:6],
        converted.rotation_matrix[6:9],
    ))
    reconstructed = mario.native_zxy_euler_degrees_to_matrix(
        converted.euler_rotation_degrees
    )
    assert matrix_close(native_matrix, reconstructed), (converted, native_matrix, reconstructed)
assert max(abs(value) for value in converted.euler_rotation_degrees) > math.pi

sheared = mathutils.Matrix.Identity(4)
sheared[0][1] = 0.25
try:
    mario.blender_matrix_to_native_transform(sheared)
    raise AssertionError("Sheared Moving Platform transform was accepted")
except ValueError as exc:
    assert "shear" in str(exc).lower()

# Static chunk extraction includes only Static role meshes.
static = mesh_object("StaticFloor", [(-20, -20, 0), (20, -20, 0), (20, 20, 0)], [(0, 1, 2)])
moving = mesh_object("Elevator", [(-2, -2, 0), (2, -2, 0), (2, 2, 0), (-2, 2, 0)], [(0, 1, 2, 3)], (0, 0, 2))
excluded = mesh_object("Excluded", [(-3, -3, 0), (3, -3, 0), (3, 3, 0)], [(0, 1, 2)])
moving.libsm64_collision_role = mario.COLLISION_ROLE_MOVING_PLATFORM
excluded.libsm64_collision_role = mario.COLLISION_ROLE_EXCLUDED
shared_material = bpy.data.materials.new("InteractiveDirtyMaterial")
other_material = bpy.data.materials.new("OtherMaterial")
static.data.materials.append(shared_material)
moving.data.materials.append(shared_material)
assert mario._object_uses_material(static, shared_material)
assert mario._object_uses_material(moving, shared_material)
assert not mario._object_uses_material(static, other_material)
bpy.context.view_layer.update()
cache = CollisionCache()
chunks, stats = cache.prepare_chunks(
    bpy.context.scene, ((0, 0),), 100.0, COLLISION_TYPES,
    depsgraph=bpy.context.evaluated_depsgraph_get(),
)
assert chunks[(0, 0)].surface_count == 1
assert stats.objects_scanned_by_bounds == 1 and stats.objects_evaluated == 1

# Parent, constraint, and ordinary keyframed evaluation all flow through matrix_world.
parent = bpy.data.objects.new("PlatformParent", None)
bpy.context.scene.collection.objects.link(parent)
moving.parent = parent
constraint_target = bpy.data.objects.new("ConstraintTarget", None)
bpy.context.scene.collection.objects.link(constraint_target)
constrained = mesh_object("ConstrainedPlatform", [(-1, -1, 0), (1, -1, 0), (1, 1, 0)], [(0, 1, 2)], (8, 0, 2))
constrained.libsm64_collision_role = mario.COLLISION_ROLE_MOVING_PLATFORM
constraint = constrained.constraints.new('COPY_LOCATION')
constraint.target = constraint_target
constraint.use_offset = True
driver = constrained.driver_add("rotation_euler", 2).driver
driver.expression = "frame / 10.0"
scale_platform = mesh_object("ScalePlatform", [(-1, -1, 0), (1, -1, 0), (1, 1, 0)], [(0, 1, 2)], (-8, 0, 2))
scale_platform.libsm64_collision_role = mario.COLLISION_ROLE_MOVING_PLATFORM
topology_platform = mesh_object("TopologyPlatform", [(-1, -1, 0), (1, -1, 0), (1, 1, 0)], [(0, 1, 2)], (0, 8, 2))
topology_platform.libsm64_collision_role = mario.COLLISION_ROLE_MOVING_PLATFORM
deleted_platform = mesh_object("DeletedPlatform", [(-1, -1, 0), (1, -1, 0), (1, 1, 0)], [(0, 1, 2)], (0, -8, 2))
deleted_platform.libsm64_collision_role = mario.COLLISION_ROLE_MOVING_PLATFORM
moving.location.x = 0.0
moving.keyframe_insert("location", frame=1)
moving.location.x = 4.0
moving.keyframe_insert("location", frame=2)
bpy.context.scene.frame_set(1)
bpy.context.view_layer.update()

library = FakeLibrary()
real = {
    "read_rom": mario._read_validated_rom,
    "load_library": mario._load_native_library,
    "initialize": mario.initialize_all_data,
    "prepare": mario._prepare_collision_chunks,
    "start_input": mario.start_input_reader,
    "stop_input": mario.stop_input_reader,
    "sample_input": mario.sample_input_reader,
    "prepare_blender": mario._prepare_blender_for_insert,
    "update": mario.update_mesh_data,
    "update_fast": mario.update_mesh_data_fast,
}
mario._read_validated_rom = lambda _path: bytearray(b"fake-rom")
mario._load_native_library = lambda: library
mario.initialize_all_data = ensure_mario_data
mario._prepare_collision_chunks = lambda _session, keys: prepared(keys)
mario.start_input_reader = lambda: None
mario.stop_input_reader = lambda: None
mario.sample_input_reader = lambda _inputs: None
mario._prepare_blender_for_insert = lambda: None
mario.update_mesh_data = lambda _mesh: None
mario.update_mesh_data_fast = lambda _mesh: None

try:
    assert mario.insert_mario("fake-rom", 100.0, False) is None
    session = mario._lifecycle
    mario_id = session.mario_id
    assert len(session.moving_platform_objects) == 5
    assert len(library.sm64_mario_create.calls) == 1
    assert library.events.index("surface_create") < library.events.index("mario_create")
    platform_ids = tuple(sorted(record.object_id for record in session.moving_platform_objects.values()))
    assert set(platform_ids).isdisjoint(
        record.object_id for record in session.native_surface_objects.values()
    )
    assert len(session.native_surface_id_registry) == len(session.native_surface_objects) + 5

    # Blender 5.2's bpy_prop_collection membership operator accepts material
    # names, not Material objects. The real handler uses pointer identity and
    # safely marks both static and moving owners when a material updates.
    session.collision_changed_while_live = False
    session.dirty_moving_platforms.clear()
    session.collision_update_handler(
        bpy.context.scene,
        SimpleNamespace(updates=(SimpleNamespace(id=shared_material),)),
    )
    assert session.collision_changed_while_live
    assert mario._blender_object_identity(moving) in session.dirty_moving_platforms

    elevator_key = mario._blender_object_identity(moving)
    elevator_record = session.moving_platform_objects[elevator_key]
    elevator_id = elevator_record.object_id
    moves = len(library.move_payloads)
    mario._update_moving_platforms(session)
    assert len(library.move_payloads) == moves  # no floating-noise update

    # Object-keyframed horizontal move, vertical elevator, rotation, and combined motion.
    bpy.context.scene.frame_set(2)
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert library.move_payloads[-1]["object_id"] == elevator_id
    moving.location.z += 3.0
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert math.isclose(library.move_payloads[-1]["position"][1], 500.0, abs_tol=1.0e-4)
    moving.rotation_euler.z = math.radians(45.0)
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert elevator_key in session.moving_platform_objects, session.last_platform_error
    assert max(abs(value) for value in library.move_payloads[-1]["rotation"]) > math.pi, (
        library.move_payloads[-1], session.last_platform_error
    )
    moving.location.x += 2.0
    moving.rotation_euler.z = math.radians(-30.0)
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)

    # Parent- and constraint-driven evaluated world transforms.
    parent.rotation_euler.z = math.radians(20.0)
    parent.location.x = 3.0
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert library.move_payloads[-1]["object_id"] == elevator_id
    constrained_key = mario._blender_object_identity(constrained)
    constrained_id = session.moving_platform_objects[constrained_key].object_id
    constraint_target.location = (2.0, -1.0, 4.0)
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert any(payload["object_id"] == constrained_id for payload in library.move_payloads[-2:])

    # Deterministic tick order: move, Mario tick, mesh/record sample.
    parent.location.x += 1.0
    bpy.context.view_layer.update()
    library.events.clear()
    mario.tick_mario(bpy.context.scene)
    assert library.events.index("surface_move") < library.events.index("mario_tick")
    assert session.mario_id == mario_id

    # Recording remains continuous while the platform crosses static chunks.
    recorder.start(1.0, 30.0)
    session.control_state = mario.RECORDING
    for index in range(3):
        parent.location.x = 300.0 * (index + 1)
        mario.mario_state.position[:] = (float(index * 100), 0.0, 0.0)
        bpy.context.view_layer.update()
        mario.tick_mario(bpy.context.scene)
    assert recorder.sample_count == 3
    assert session.mario_id == mario_id
    samples = recorder.freeze_for_bake()
    baked = bake_shape_keys(bpy.context, session.live_object, samples, 1.0, 30.0)
    assert int(baked.get("libsm64_sample_count", 0)) == 3
    discard_baked_take(baked)
    recorder.complete("Moving platform route")
    session.control_state = mario.LIVE_IDLE

    # Start Mark recreation does not recreate or unload moving platforms.
    mario.mario_state.health = 0x880
    mark = mario.set_persistent_start_mark()
    platform_create_count = len(library.sm64_surface_object_create.calls)
    mario.reset_to_persistent_start_mark()
    assert mark.position == tuple(float(value) for value in mark.position)
    assert len(library.sm64_surface_object_create.calls) == platform_create_count
    assert session.moving_platform_objects[elevator_key].object_id == elevator_id

    # Animated scale and evaluated-geometry changes are disabled explicitly.
    scale_key = mario._blender_object_identity(scale_platform)
    scale_id = session.moving_platform_objects[scale_key].object_id
    scale_platform.scale.x = 1.5
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert scale_key not in session.moving_platform_objects
    assert scale_id in library.deleted_ids
    assert "animated scale" in session.disabled_moving_platforms[scale_key]

    topology_key = mario._blender_object_identity(topology_platform)
    topology_id = session.moving_platform_objects[topology_key].object_id
    topology_platform.data.vertices[0].co.x -= 0.5
    topology_platform.data.update()
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert topology_key not in session.moving_platform_objects
    assert topology_id in library.deleted_ids
    assert "geometry changed" in session.disabled_moving_platforms[topology_key]

    # Deleting a platform object deletes its native owner exactly once.
    deleted_key = mario._blender_object_identity(deleted_platform)
    deleted_id = session.moving_platform_objects[deleted_key].object_id
    bpy.data.objects.remove(deleted_platform, do_unlink=True)
    bpy.context.view_layer.update()
    mario._update_moving_platforms(session)
    assert deleted_key not in session.moving_platform_objects
    assert library.deleted_ids.count(deleted_id) == 1

    # A native move exception poisons ticking but preserves exact IDs for safe teardown.
    library.sm64_surface_object_move.failure = RuntimeError("injected platform move failure")
    parent.location.x += 1.0
    bpy.context.view_layer.update()
    try:
        mario._update_moving_platforms(session)
        raise AssertionError("Failed platform move should poison the session")
    except mario.MarioLifecycleError:
        pass
    assert session.control_state == mario.POISONED
    assert "move failed" in session.last_platform_error
    library.sm64_surface_object_move.failure = None

    remaining_ids = tuple(session.native_surface_id_registry)
    assert not mario.stop_tick_mario(_session=session, _cleanup_rejected=False)
    for object_id in remaining_ids:
        assert library.deleted_ids.count(object_id) == 1
    delete_count = len(library.deleted_ids)
    mario.stop_tick_mario(_session=session, _cleanup_rejected=False)
    assert len(library.deleted_ids) == delete_count
    assert library.events.index("mario_delete") < library.events.index("global_terminate")
finally:
    try:
        mario.stop_tick_mario(_cleanup_rejected=False)
    except Exception:
        pass
    recorder.cancel()
    mario._read_validated_rom = real["read_rom"]
    mario._load_native_library = real["load_library"]
    mario.initialize_all_data = real["initialize"]
    mario._prepare_collision_chunks = real["prepare"]
    mario.start_input_reader = real["start_input"]
    mario.stop_input_reader = real["stop_input"]
    mario.sample_input_reader = real["sample_input"]
    mario._prepare_blender_for_insert = real["prepare_blender"]
    mario.update_mesh_data = real["update"]
    mario.update_mesh_data_fast = real["update_fast"]
    addon.unregister()

print("libsm64 moving-platform regression passed")
