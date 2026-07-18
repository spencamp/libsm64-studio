"""Main-thread collision extraction and fixed 2D spatial chunk caching.

Chunk keys use absolute Blender-world X/Y coordinates.  Blender Z is vertical
and remains unbounded inside a chunk.  Cached chunks contain compact world-space
records; temporary ctypes arrays are assembled only for a native create call.
"""

from dataclasses import dataclass
import hashlib
import math
import struct
import time


CACHE_FORMAT_VERSION = 2
COORDINATE_MAPPING_VERSION = 2
CHUNK_SIZE_BLENDER = 256.0
PRELOAD_RADIUS = 1
RETENTION_RADIUS = 2
MAX_ACTIVE_CHUNKS = (RETENTION_RADIUS * 2 + 1) ** 2
BOUNDARY_EPSILON = 1.0e-7
DEGENERATE_EPSILON_SQUARED = 1.0e-16
INT32_MIN = -0x80000000
INT32_MAX = 0x7FFFFFFF


def _finite_values(values):
    return all(math.isfinite(float(value)) for value in values)


def chunk_coordinate(world_position, chunk_size=CHUNK_SIZE_BLENDER):
    """Return a stable ``(Blender X, Blender Y)`` horizontal chunk key."""
    size = float(chunk_size)
    if not math.isfinite(size) or size <= 0.0:
        raise ValueError("Collision chunk size must be finite and positive")
    x, y = float(world_position[0]), float(world_position[1])
    if not _finite_values((x, y)):
        raise ValueError("Collision position must be finite")
    return int(math.floor(x / size)), int(math.floor(y / size))


def chunk_origin(coordinate, chunk_size=CHUNK_SIZE_BLENDER):
    size = float(chunk_size)
    return float(coordinate[0]) * size, float(coordinate[1]) * size


def chunk_bounds(coordinate, chunk_size=CHUNK_SIZE_BLENDER):
    low = chunk_origin(coordinate, chunk_size)
    size = float(chunk_size)
    return low, (low[0] + size, low[1] + size)


def bounds_intersect(first, second, epsilon=BOUNDARY_EPSILON):
    """Test inclusive intersection of two 2D horizontal bounds."""
    return (
        first[0][0] <= second[1][0] + epsilon
        and second[0][0] <= first[1][0] + epsilon
        and first[0][1] <= second[1][1] + epsilon
        and second[0][1] <= first[1][1] + epsilon
    )


def chunks_for_bounds(low, high, chunk_size=CHUNK_SIZE_BLENDER):
    """Return every 2D chunk touched by bounds, including boundary walls."""
    if not _finite_values(tuple(low[:2]) + tuple(high[:2])):
        raise ValueError("Collision bounds must be finite")
    size = float(chunk_size)
    start_x = int(math.floor((float(low[0]) - BOUNDARY_EPSILON) / size))
    end_x = int(math.floor((float(high[0]) + BOUNDARY_EPSILON) / size))
    start_y = int(math.floor((float(low[1]) - BOUNDARY_EPSILON) / size))
    end_y = int(math.floor((float(high[1]) + BOUNDARY_EPSILON) / size))
    return tuple(
        (x, y)
        for x in range(start_x, end_x + 1)
        for y in range(start_y, end_y + 1)
    )


def neighborhood_coordinates(center, radius):
    radius = int(radius)
    if radius < 0:
        raise ValueError("Collision neighborhood radius cannot be negative")
    return frozenset(
        (center[0] + dx, center[1] + dy)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
    )


def preload_coordinates(center):
    return neighborhood_coordinates(center, PRELOAD_RADIUS)


def retention_coordinates(center):
    return neighborhood_coordinates(center, RETENTION_RADIUS)


@dataclass(frozen=True)
class TransitionPlan:
    center: tuple
    required: frozenset
    retained: frozenset
    incoming: tuple
    outgoing: tuple


def plan_transition(active_keys, center):
    active = frozenset(active_keys)
    required = preload_coordinates(center)
    retained = retention_coordinates(center)
    incoming = tuple(sorted(required - active))
    outgoing = tuple(sorted(active - retained))
    if len((active | required) - set(outgoing)) > MAX_ACTIVE_CHUNKS:
        raise RuntimeError("Collision transition would exceed the active chunk limit")
    return TransitionPlan(center, required, retained, incoming, outgoing)


def _clip_plane(polygon, axis, boundary, keep_greater):
    if not polygon:
        return []
    output = []
    previous = polygon[-1]

    def inside(vertex):
        if keep_greater:
            return vertex[axis] >= boundary - BOUNDARY_EPSILON
        return vertex[axis] <= boundary + BOUNDARY_EPSILON

    previous_inside = inside(previous)
    for current in polygon:
        current_inside = inside(current)
        if current_inside != previous_inside:
            denominator = current[axis] - previous[axis]
            if abs(denominator) > BOUNDARY_EPSILON:
                factor = (boundary - previous[axis]) / denominator
                output.append(tuple(
                    previous[index] + factor * (current[index] - previous[index])
                    for index in range(3)
                ))
        if current_inside:
            output.append(current)
        previous = current
        previous_inside = current_inside
    return output


def _triangle_area_squared(triangle):
    edge_a = tuple(triangle[1][axis] - triangle[0][axis] for axis in range(3))
    edge_b = tuple(triangle[2][axis] - triangle[0][axis] for axis in range(3))
    cross = (
        edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
        edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
        edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
    )
    return sum(value * value for value in cross)


def clip_triangle_to_chunk(triangle, coordinate, chunk_size=CHUNK_SIZE_BLENDER):
    """Clip against Blender-world X/Y while preserving Blender Z and winding."""
    polygon = [tuple(float(value) for value in vertex) for vertex in triangle]
    if len(polygon) != 3 or not _finite_values(
        value for vertex in polygon for value in vertex
    ):
        raise ValueError("Collision triangle coordinates must be finite")
    low, high = chunk_bounds(coordinate, chunk_size)
    polygon = _clip_plane(polygon, 0, low[0], True)
    polygon = _clip_plane(polygon, 0, high[0], False)
    polygon = _clip_plane(polygon, 1, low[1], True)
    polygon = _clip_plane(polygon, 1, high[1], False)
    if len(polygon) < 3:
        return ()
    triangles = []
    for index in range(1, len(polygon) - 1):
        fragment = (polygon[0], polygon[index], polygon[index + 1])
        if _triangle_area_squared(fragment) > DEGENERATE_EPSILON_SQUARED:
            triangles.append(fragment)
    return tuple(triangles)


@dataclass(frozen=True)
class SurfaceRecord:
    surface_type: int
    force: int
    terrain: int
    vertices: tuple


@dataclass(frozen=True)
class ObjectEntry:
    key: tuple
    source_fingerprint: str
    fingerprint: str
    bounds: tuple
    surfaces: tuple


@dataclass(frozen=True)
class PreparedChunk:
    key: tuple
    fingerprint: str
    surfaces: tuple

    @property
    def surface_count(self):
        return len(self.surfaces)


@dataclass
class CollisionStats:
    objects_scanned_by_bounds: int = 0
    objects_evaluated: int = 0
    object_cache_hits: int = 0
    object_cache_misses: int = 0
    chunk_cache_hits: int = 0
    chunk_cache_misses: int = 0
    chunks_prepared: int = 0
    surfaces_prepared: int = 0
    duration_seconds: float = 0.0

    def as_dict(self):
        return dict(vars(self))


def _matrix_values(matrix):
    return tuple(float(matrix[row][column]) for row in range(4) for column in range(4))


def _transform_point(values, point):
    x, y, z = point
    return (
        values[0] * x + values[1] * y + values[2] * z + values[3],
        values[4] * x + values[5] * y + values[6] * z + values[7],
        values[8] * x + values[9] * y + values[10] * z + values[11],
    )


def _identity(owner):
    original = getattr(owner, "original", owner)
    session_uid = getattr(original, "session_uid", None)
    if session_uid is not None:
        return ("UID", int(session_uid))
    try:
        return ("PTR", int(original.as_pointer()))
    except Exception:
        return ("NAME", getattr(original, "name_full", repr(original)))


def _custom_properties(owner):
    try:
        return tuple(sorted((str(key), repr(value)) for key, value in owner.items()))
    except Exception:
        return ()


def _terrain(obj, collision_types):
    seek = obj
    while seek is not None:
        if getattr(seek, "sm64_obj_type", None) == "Area Root" and hasattr(seek, "terrainEnum"):
            return int(collision_types.get(seek.terrainEnum, collision_types["TERRAIN_GRASS"]))
        seek = getattr(seek, "parent", None)
    return int(collision_types["TERRAIN_GRASS"])


def _surface_types(mesh, collision_types):
    default = int(collision_types["SURFACE_DEFAULT"])
    types = []
    metadata = []
    for material in mesh.materials:
        name = getattr(material, "collision_type_simple", "SURFACE_DEFAULT") if material else "SURFACE_DEFAULT"
        types.append(int(collision_types.get(name, default)))
        metadata.append((getattr(material, "name_full", "") if material else "", name,
                         _custom_properties(material) if material else ()))
    return tuple(types), tuple(metadata)


class CollisionCache:
    """Session-local extraction and clipped-chunk cache; call only on the main thread."""

    def __init__(self):
        self.object_cache = {}
        self.chunk_cache = {}
        self.dirty_object_keys = set()
        self.last_stats = CollisionStats()

    def clear(self):
        counts = len(self.object_cache), len(self.chunk_cache)
        self.object_cache.clear()
        self.chunk_cache.clear()
        self.dirty_object_keys.clear()
        self.last_stats = CollisionStats()
        return counts

    def mark_object_dirty(self, obj):
        self.dirty_object_keys.add(_identity(obj))

    def _eligible_objects(self, scene):
        for obj in scene.collection.all_objects:
            if getattr(obj, "type", None) != "MESH":
                continue
            if obj.get("libsm64_is_bake", False) or obj.get("libsm64_live_role", ""):
                continue
            try:
                hidden = obj.hide_get()
            except Exception:
                hidden = False
            if hidden or getattr(obj, "hide_viewport", False):
                continue
            yield obj

    def _evaluated_bounds(self, evaluated_obj):
        matrix = _matrix_values(evaluated_obj.matrix_world)
        corners = tuple(_transform_point(matrix, corner) for corner in evaluated_obj.bound_box)
        return (
            (min(value[0] for value in corners), min(value[1] for value in corners)),
            (max(value[0] for value in corners), max(value[1] for value in corners)),
        )

    def _cheap_fingerprint(self, obj, evaluated, bounds, scale, chunk_size, collision_types):
        data = getattr(obj, "data", None)
        material_data = []
        for material in getattr(data, "materials", ()):
            material_data.append((
                _identity(material) if material else None,
                getattr(material, "collision_type_simple", "SURFACE_DEFAULT") if material else "SURFACE_DEFAULT",
                _custom_properties(material) if material else (),
            ))
        modifier_data = tuple(
            (modifier.type, modifier.name, _custom_properties(modifier))
            for modifier in getattr(obj, "modifiers", ())
        )
        payload = (
            CACHE_FORMAT_VERSION, COORDINATE_MAPPING_VERSION, _identity(obj),
            _identity(data) if data is not None else None,
            len(getattr(data, "vertices", ())), len(getattr(data, "polygons", ())),
            _matrix_values(evaluated.matrix_world), bounds, modifier_data,
            tuple(material_data), _terrain(obj, collision_types), _custom_properties(obj),
            float(scale), float(chunk_size),
        )
        return hashlib.sha256(repr(payload).encode("utf-8", "backslashreplace")).hexdigest()

    def _extract_object(self, obj, evaluated, depsgraph, fingerprint, bounds, collision_types):
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            mesh.calc_loop_triangles()
            matrix = _matrix_values(evaluated.matrix_world)
            terrain = _terrain(obj, collision_types)
            material_types, _metadata = _surface_types(mesh, collision_types)
            default_surface = int(collision_types["SURFACE_DEFAULT"])
            surfaces = []
            for triangle in mesh.loop_triangles:
                vertices = tuple(
                    _transform_point(matrix, mesh.vertices[index].co)
                    for index in triangle.vertices
                )
                if not _finite_values(value for vertex in vertices for value in vertex):
                    raise ValueError("Object {!r} contains non-finite collision geometry".format(obj.name))
                material_index = int(triangle.material_index)
                surface_type = (
                    material_types[material_index]
                    if 0 <= material_index < len(material_types)
                    else default_surface
                )
                surfaces.append(SurfaceRecord(surface_type, 0, terrain, vertices))
            surfaces = tuple(surfaces)
            geometry_digest = hashlib.sha256()
            geometry_digest.update(fingerprint.encode("ascii"))
            for surface in surfaces:
                geometry_digest.update(struct.pack(
                    "<hhH", surface.surface_type, surface.force, surface.terrain
                ))
                for vertex in surface.vertices:
                    geometry_digest.update(struct.pack("<ddd", *vertex))
            return ObjectEntry(
                _identity(obj), fingerprint, geometry_digest.hexdigest(), bounds, surfaces
            )
        finally:
            evaluated.to_mesh_clear()

    def _chunk_fingerprint(self, key, contributors, scale, chunk_size):
        digest = hashlib.sha256()
        digest.update(struct.pack("<IIdd", CACHE_FORMAT_VERSION, COORDINATE_MAPPING_VERSION,
                                  float(scale), float(chunk_size)))
        digest.update(repr(key).encode("ascii"))
        for entry in sorted(contributors, key=lambda item: repr(item.key)):
            digest.update(repr(entry.key).encode("utf-8", "backslashreplace"))
            digest.update(entry.fingerprint.encode("ascii"))
        return digest.hexdigest()

    def _build_chunk(self, key, contributors, fingerprint, chunk_size):
        records = []
        for entry in contributors:
            for surface in entry.surfaces:
                tri_low = tuple(min(vertex[axis] for vertex in surface.vertices) for axis in (0, 1))
                tri_high = tuple(max(vertex[axis] for vertex in surface.vertices) for axis in (0, 1))
                if not bounds_intersect((tri_low, tri_high), chunk_bounds(key, chunk_size)):
                    continue
                for fragment in clip_triangle_to_chunk(surface.vertices, key, chunk_size):
                    records.append(SurfaceRecord(
                        surface.surface_type, surface.force, surface.terrain, fragment
                    ))
        return PreparedChunk(key, fingerprint, tuple(records))

    def prepare_chunks(self, scene, keys, scale, collision_types, depsgraph=None,
                       chunk_size=CHUNK_SIZE_BLENDER):
        started = time.perf_counter()
        requested = frozenset(keys)
        stats = CollisionStats()
        if depsgraph is None:
            depsgraph = scene.evaluated_depsgraph_get()
        if not requested:
            stats.duration_seconds = time.perf_counter() - started
            self.last_stats = stats
            return {}, stats
        lows_highs = tuple(chunk_bounds(key, chunk_size) for key in requested)
        requested_bounds = (
            (min(item[0][0] for item in lows_highs), min(item[0][1] for item in lows_highs)),
            (max(item[1][0] for item in lows_highs), max(item[1][1] for item in lows_highs)),
        )
        contributors = []
        for obj in self._eligible_objects(scene):
            stats.objects_scanned_by_bounds += 1
            evaluated = obj.evaluated_get(depsgraph)
            bounds = self._evaluated_bounds(evaluated)
            if not bounds_intersect(bounds, requested_bounds):
                continue
            key = _identity(obj)
            fingerprint = self._cheap_fingerprint(
                obj, evaluated, bounds, scale, chunk_size, collision_types
            )
            cached = self.object_cache.get(key)
            if (
                key not in self.dirty_object_keys
                and cached is not None
                and cached.source_fingerprint == fingerprint
            ):
                entry = cached
                stats.object_cache_hits += 1
            else:
                entry = self._extract_object(
                    obj, evaluated, depsgraph, fingerprint, bounds, collision_types
                )
                self.object_cache[key] = entry
                self.dirty_object_keys.discard(key)
                stats.objects_evaluated += 1
                stats.object_cache_misses += 1
            contributors.append(entry)

        prepared = {}
        for key in sorted(requested):
            bounds = chunk_bounds(key, chunk_size)
            relevant = tuple(entry for entry in contributors if bounds_intersect(entry.bounds, bounds))
            fingerprint = self._chunk_fingerprint(key, relevant, scale, chunk_size)
            cache_key = (key, fingerprint)
            chunk = self.chunk_cache.get(cache_key)
            if chunk is None:
                chunk = self._build_chunk(key, relevant, fingerprint, chunk_size)
                self.chunk_cache[cache_key] = chunk
                stats.chunk_cache_misses += 1
            else:
                stats.chunk_cache_hits += 1
            prepared[key] = chunk
            stats.chunks_prepared += 1
            stats.surfaces_prepared += chunk.surface_count
        stats.duration_seconds = time.perf_counter() - started
        self.last_stats = stats
        return prepared, stats


def _checked_int32(value):
    if not math.isfinite(float(value)):
        raise OverflowError("Non-finite native collision coordinate")
    converted = int(value)
    if converted < INT32_MIN or converted > INT32_MAX:
        raise OverflowError("Native collision coordinate exceeds signed 32-bit storage")
    return converted


def native_chunk_payload(chunk, scale, session_origin, surface_class,
                         chunk_size=CHUNK_SIZE_BLENDER):
    """Build temporary ctypes surfaces plus an object translation.

    libsm64 copies the array during ``sm64_surface_object_create``; callers may
    release the returned array immediately after that call returns.
    """
    world_origin = chunk_origin(chunk.key, chunk_size)
    output = (surface_class * chunk.surface_count)()
    for index, record in enumerate(chunk.surfaces):
        surface = output[index]
        surface.type = record.surface_type
        surface.force = record.force
        surface.terrain = record.terrain
        for vertex_index, vertex in enumerate(record.vertices):
            local_native = (
                _checked_int32(float(scale) * (vertex[0] - world_origin[0])),
                _checked_int32(float(scale) * (vertex[2] - float(session_origin[2]))),
                _checked_int32(float(scale) * (-vertex[1] + world_origin[1])),
            )
            for axis_index, coordinate in enumerate(local_native):
                surface.vertices[vertex_index][axis_index] = coordinate
    transform_position = (
        float(scale) * (world_origin[0] - float(session_origin[0])),
        0.0,
        float(scale) * (-world_origin[1] + float(session_origin[1])),
    )
    return output, transform_position


collision_cache = CollisionCache()
