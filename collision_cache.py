"""Automatic in-memory collision extraction and spatial chunk caching.

Blender data is read only while preparing a neighborhood on the main thread.
Cached object triangles and chunk fragments are compact ``array`` instances;
the native ctypes array is assembled only for the neighborhood being loaded.
"""

from array import array
import hashlib
import itertools
import math
import struct
import time


CACHE_FORMAT_VERSION = 1
NATIVE_COORD_MIN = -32768
NATIVE_COORD_MAX = 32767
NATIVE_WATER_CLEARANCE = 1000
# Keep collision vertices away from the ABI limit so Mario's collision queries,
# wall radius, and integer rounding cannot step outside the representable range.
NATIVE_SAFETY_MARGIN = 2048
NEIGHBORHOOD_RADIUS = 1
TRIANGLE_STRIDE = 9
CHUNK_RECORD_STRIDE = 11


def safe_chunk_size_native(
    native_limit=NATIVE_COORD_MAX,
    safety_margin=NATIVE_SAFETY_MARGIN,
    neighborhood_radius=NEIGHBORHOOD_RADIUS,
):
    """Largest integer chunk edge whose loaded neighborhood fits the ABI.

    A radius-r neighborhood extends (r + 1/2) chunk widths from its center.
    The shipped libsm64 ABI stores every input surface coordinate in int16.
    """
    usable = int(native_limit) - int(safety_margin)
    if usable <= 0 or neighborhood_radius < 0:
        raise ValueError("Invalid native collision range")
    return max(1, int(math.floor(usable / (float(neighborhood_radius) + 0.5))))


def chunk_size_for_scale(scale):
    scale = float(scale)
    if scale <= 0:
        raise ValueError("Blender-to-libsm64 scale must be positive")
    return safe_chunk_size_native() / scale


def chunk_coordinate(position, chunk_size):
    return tuple(int(math.floor(float(value) / float(chunk_size))) for value in position)


def chunk_center(coordinate, chunk_size):
    return tuple((int(value) + 0.5) * float(chunk_size) for value in coordinate)


def chunk_bounds(coordinate, chunk_size):
    low = tuple(int(value) * float(chunk_size) for value in coordinate)
    high = tuple(value + float(chunk_size) for value in low)
    return low, high


def bounds_intersect(first, second):
    return all(first[0][axis] <= second[1][axis] and second[0][axis] <= first[1][axis]
               for axis in range(3))


def chunks_for_bounds(low, high, chunk_size):
    starts = chunk_coordinate(low, chunk_size)
    ends = chunk_coordinate(high, chunk_size)
    return tuple(itertools.product(*(
        range(starts[axis], ends[axis] + 1) for axis in range(3)
    )))


def neighborhood_coordinates(center, radius=NEIGHBORHOOD_RADIUS):
    return tuple(
        (center[0] + dx, center[1] + dy, center[2] + dz)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        for dz in range(-radius, radius + 1)
    )


def neighborhood_bounds(center, chunk_size, radius=NEIGHBORHOOD_RADIUS):
    low_coord = tuple(value - radius for value in center)
    high_coord = tuple(value + radius for value in center)
    low, _ = chunk_bounds(low_coord, chunk_size)
    _, high = chunk_bounds(high_coord, chunk_size)
    return low, high


def _clip_plane(polygon, axis, boundary, keep_greater):
    if not polygon:
        return []
    result = []
    previous = polygon[-1]
    previous_inside = previous[axis] >= boundary if keep_greater else previous[axis] <= boundary
    for current in polygon:
        current_inside = current[axis] >= boundary if keep_greater else current[axis] <= boundary
        if current_inside != previous_inside:
            denominator = current[axis] - previous[axis]
            factor = 0.0 if denominator == 0.0 else (boundary - previous[axis]) / denominator
            crossing = tuple(previous[i] + factor * (current[i] - previous[i]) for i in range(3))
            result.append(crossing)
        if current_inside:
            result.append(current)
        previous = current
        previous_inside = current_inside
    return result


def clip_triangle_to_bounds(triangle, bounds):
    """Clip one triangle to an axis-aligned 3D chunk and return triangles."""
    polygon = [tuple(vertex) for vertex in triangle]
    for axis in range(3):
        polygon = _clip_plane(polygon, axis, bounds[0][axis], True)
        polygon = _clip_plane(polygon, axis, bounds[1][axis], False)
    if len(polygon) < 3:
        return ()
    return tuple((polygon[0], polygon[index], polygon[index + 1])
                 for index in range(1, len(polygon) - 1))


def _matrix_values(matrix):
    return tuple(float(matrix[row][column]) for row in range(4) for column in range(4))


def _transform_vertices(coords, matrix):
    values = _matrix_values(matrix)
    output = array('f', [0.0]) * len(coords)
    for index in range(0, len(coords), 3):
        x, y, z = coords[index:index + 3]
        output[index] = values[0] * x + values[1] * y + values[2] * z + values[3]
        output[index + 1] = values[4] * x + values[5] * y + values[6] * z + values[7]
        output[index + 2] = values[8] * x + values[9] * y + values[10] * z + values[11]
    return output


def _custom_properties(owner):
    try:
        return tuple(sorted((str(key), repr(value)) for key, value in owner.items()))
    except Exception:
        return ()


def _resolved_metadata(obj, mesh, collision_types):
    terrain_name = "TERRAIN_GRASS"
    seek = obj
    while seek is not None:
        if (getattr(seek, "sm64_obj_type", None) == "Area Root"
                and hasattr(seek, "terrainEnum")):
            terrain_name = seek.terrainEnum
            break
        seek = getattr(seek, "parent", None)
    terrain = int(collision_types.get(terrain_name, collision_types["TERRAIN_GRASS"]))
    surface_types = []
    material_metadata = []
    for material in mesh.materials:
        surface_name = getattr(material, "collision_type_simple", "SURFACE_DEFAULT") if material else "SURFACE_DEFAULT"
        surface_types.append(int(collision_types.get(surface_name, collision_types["SURFACE_DEFAULT"])))
        material_metadata.append((
            getattr(material, "name_full", "") if material else "",
            surface_name,
            _custom_properties(material) if material else (),
        ))
    return terrain, tuple(surface_types), (terrain_name, tuple(material_metadata), _custom_properties(obj))


def _object_key(obj):
    try:
        return ("PTR", int(obj.original.as_pointer()))
    except Exception:
        try:
            return ("PTR", int(obj.as_pointer()))
        except Exception:
            return ("NAME", getattr(obj, "name_full", getattr(obj, "name", repr(obj))))


class ObjectEntry:
    __slots__ = ("key", "fingerprint", "bounds", "vertices", "surface_types", "terrain")

    def __init__(self, key, fingerprint, bounds, vertices, surface_types, terrain):
        self.key = key
        self.fingerprint = fingerprint
        self.bounds = bounds
        self.vertices = vertices
        self.surface_types = surface_types
        self.terrain = terrain

    @property
    def triangle_count(self):
        return len(self.surface_types)


class ChunkEntry:
    __slots__ = ("coordinate", "fingerprint", "records")

    def __init__(self, coordinate, fingerprint, records):
        self.coordinate = coordinate
        self.fingerprint = fingerprint
        self.records = records

    @property
    def surface_count(self):
        return len(self.records) // CHUNK_RECORD_STRIDE


class CollisionPreparation:
    __slots__ = ("center", "origin", "surface_array", "surface_count", "stats")

    def __init__(self, center, origin, surface_array, surface_count, stats):
        self.center = center
        self.origin = origin
        self.surface_array = surface_array
        self.surface_count = surface_count
        self.stats = stats


class CollisionCache:
    def __init__(self):
        self.object_cache = {}
        self.chunk_cache = {}
        self.last_stats = {}

    def clear(self):
        counts = (len(self.object_cache), len(self.chunk_cache))
        self.object_cache.clear()
        self.chunk_cache.clear()
        self.last_stats = {}
        return counts

    def _evaluated_bounds(self, evaluated_obj):
        matrix = _matrix_values(evaluated_obj.matrix_world)
        corners = []
        for x, y, z in evaluated_obj.bound_box:
            corners.append((
                matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
                matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
                matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
            ))
        return (
            tuple(min(corner[axis] for corner in corners) for axis in range(3)),
            tuple(max(corner[axis] for corner in corners) for axis in range(3)),
        )

    def _extract_object(self, obj, depsgraph, scale, collision_types):
        evaluated = obj.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            mesh.calc_loop_triangles()
            coords = array('f', [0.0]) * (len(mesh.vertices) * 3)
            indices = array('i', [0]) * (len(mesh.loop_triangles) * 3)
            material_indices = array('i', [0]) * len(mesh.loop_triangles)
            mesh.vertices.foreach_get("co", coords)
            mesh.loop_triangles.foreach_get("vertices", indices)
            mesh.loop_triangles.foreach_get("material_index", material_indices)
            terrain, material_types, metadata = _resolved_metadata(obj, mesh, collision_types)
            metadata = (terrain, material_types, metadata, _custom_properties(mesh))
            matrix_values = _matrix_values(evaluated.matrix_world)
            digest = hashlib.sha256()
            digest.update(struct.pack("<I", CACHE_FORMAT_VERSION))
            digest.update(struct.pack("<d", float(scale)))
            digest.update(coords.tobytes())
            digest.update(indices.tobytes())
            digest.update(material_indices.tobytes())
            digest.update(struct.pack("<16d", *matrix_values))
            digest.update(repr(metadata).encode("utf-8", "backslashreplace"))
            fingerprint = digest.hexdigest()
            key = _object_key(obj)
            cached = self.object_cache.get(key)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached, True

            transformed = _transform_vertices(coords, evaluated.matrix_world)
            triangles = array('f')
            surfaces = array('H')
            default_surface = int(collision_types["SURFACE_DEFAULT"])
            for triangle_index, material_index in enumerate(material_indices):
                base = triangle_index * 3
                for vertex_index in indices[base:base + 3]:
                    offset = vertex_index * 3
                    triangles.extend(transformed[offset:offset + 3])
                surfaces.append(material_types[material_index] if 0 <= material_index < len(material_types)
                                else default_surface)
            if len(transformed):
                bounds = (
                    tuple(min(transformed[axis::3]) for axis in range(3)),
                    tuple(max(transformed[axis::3]) for axis in range(3)),
                )
            else:
                bounds = ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
            entry = ObjectEntry(key, fingerprint, bounds, triangles, surfaces, terrain)
            self.object_cache[key] = entry
            return entry, False
        finally:
            evaluated.to_mesh_clear()

    def _eligible_objects(self, scene):
        for obj in scene.collection.all_objects:
            if getattr(obj, "type", None) != 'MESH' or obj.get('libsm64_is_bake', False):
                continue
            try:
                hidden = obj.hide_get()
            except Exception:
                hidden = False
            if hidden or getattr(obj, "hide_viewport", False) or getattr(obj, "hide_render", False):
                continue
            yield obj

    def _chunk_fingerprint(self, coordinate, contributors, scale):
        digest = hashlib.sha256()
        digest.update(repr((CACHE_FORMAT_VERSION, coordinate, float(scale),
                            safe_chunk_size_native(), NATIVE_SAFETY_MARGIN,
                            NEIGHBORHOOD_RADIUS)).encode("ascii"))
        for entry in contributors:
            digest.update(repr(entry.key).encode("utf-8", "backslashreplace"))
            digest.update(entry.fingerprint.encode("ascii"))
        return digest.hexdigest()

    def _build_chunk(self, coordinate, contributors, scale, chunk_size, fingerprint):
        bounds = chunk_bounds(coordinate, chunk_size)
        center = chunk_center(coordinate, chunk_size)
        records = array('i')
        for entry in contributors:
            if not bounds_intersect(entry.bounds, bounds):
                continue
            vertices = entry.vertices
            for triangle_index, surface_type in enumerate(entry.surface_types):
                base = triangle_index * TRIANGLE_STRIDE
                triangle = (
                    vertices[base:base + 3],
                    vertices[base + 3:base + 6],
                    vertices[base + 6:base + 9],
                )
                tri_bounds = (
                    tuple(min(vertex[axis] for vertex in triangle) for axis in range(3)),
                    tuple(max(vertex[axis] for vertex in triangle) for axis in range(3)),
                )
                if not bounds_intersect(tri_bounds, bounds):
                    continue
                for clipped in clip_triangle_to_bounds(triangle, bounds):
                    edge_a = tuple(clipped[1][axis] - clipped[0][axis] for axis in range(3))
                    edge_b = tuple(clipped[2][axis] - clipped[0][axis] for axis in range(3))
                    cross = (
                        edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                        edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                        edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
                    )
                    if sum(value * value for value in cross) <= 1e-16:
                        continue
                    records.extend((int(surface_type), int(entry.terrain)))
                    for vertex in clipped:
                        native = (
                            round(scale * (vertex[0] - center[0])),
                            round(scale * (vertex[2] - center[2])),
                            round(scale * (-vertex[1] + center[1])),
                        )
                        records.extend(native)
        return ChunkEntry(coordinate, fingerprint, records)

    def _assemble_native(
        self, chunks, center_coordinate, scale, surface_type, vertical_reference=None,
    ):
        total = sum(chunk.surface_count for chunk in chunks)
        output = (surface_type * total)()
        neighborhood_origin = chunk_center(center_coordinate, chunk_size_for_scale(scale))
        native_chunk_size = safe_chunk_size_native()
        output_index = 0
        for chunk in chunks:
            delta_world_chunks = tuple(chunk.coordinate[axis] - center_coordinate[axis] for axis in range(3))
            delta_native = (
                delta_world_chunks[0] * native_chunk_size,
                delta_world_chunks[2] * native_chunk_size,
                -delta_world_chunks[1] * native_chunk_size,
            )
            records = chunk.records
            for start in range(0, len(records), CHUNK_RECORD_STRIDE):
                target = output[output_index]
                target.surftype = records[start]
                target.force = 0
                target.terrain = records[start + 1]
                values = []
                for vertex in range(3):
                    offset = start + 2 + vertex * 3
                    values.extend(records[offset + axis] + delta_native[axis] for axis in range(3))
                (target.v0x, target.v0y, target.v0z,
                 target.v1x, target.v1y, target.v1z,
                 target.v2x, target.v2y, target.v2z) = values
                if any(value < NATIVE_COORD_MIN or value > NATIVE_COORD_MAX for value in values):
                    raise OverflowError("Prepared collision exceeded the shipped libsm64 int16 ABI")
                output_index += 1

        if vertical_reference is not None:
            # The bundled libsm64 has a fixed native water plane. Centering the
            # vertical origin on a large collision chunk can move ordinary
            # Blender floors below that plane and put Mario into a swim action.
            # Recenter native Y above that plane near the requested world height, while
            # applying the inverse change to the Blender origin so every
            # collision vertex keeps exactly the same world-space position.
            desired_shift = round(
                float(scale) * (neighborhood_origin[2] - float(vertical_reference))
            ) + NATIVE_WATER_CLEARANCE
            if total:
                vertical_values = tuple(
                    value
                    for surface in output
                    for value in (surface.v0y, surface.v1y, surface.v2y)
                )
                minimum_shift = NATIVE_COORD_MIN - min(vertical_values)
                maximum_shift = NATIVE_COORD_MAX - max(vertical_values)
                vertical_shift = max(
                    minimum_shift, min(maximum_shift, desired_shift)
                )
                for surface in output:
                    surface.v0y += vertical_shift
                    surface.v1y += vertical_shift
                    surface.v2y += vertical_shift
            else:
                vertical_shift = desired_shift
            neighborhood_origin = (
                neighborhood_origin[0],
                neighborhood_origin[1],
                neighborhood_origin[2] - vertical_shift / float(scale),
            )
        return output, total, neighborhood_origin

    def prepare(self, scene, world_position, scale, surface_type, collision_types,
                depsgraph=None, status_callback=None):
        started = time.perf_counter()
        if depsgraph is None:
            getter = getattr(scene, "evaluated_depsgraph_get", None)
            if not callable(getter):
                raise ValueError("An evaluated Blender dependency graph is required")
            depsgraph = getter()
        chunk_size = chunk_size_for_scale(scale)
        center = chunk_coordinate(world_position, chunk_size)
        coordinates = neighborhood_coordinates(center)
        requested_bounds = neighborhood_bounds(center, chunk_size)
        stats = {
            "objects_considered": 0, "objects_rejected": 0,
            "object_cache_hits": 0, "object_cache_misses": 0,
            "triangles_extracted": 0, "chunk_cache_hits": 0,
            "chunk_cache_misses": 0, "initial_chunk": center,
        }
        if status_callback:
            status_callback("Preparing nearby collision…")
        entries = []
        live_keys = set()
        for obj in self._eligible_objects(scene):
            stats["objects_considered"] += 1
            evaluated = obj.evaluated_get(depsgraph)
            try:
                evaluated_bounds = self._evaluated_bounds(evaluated)
            except Exception:
                evaluated_bounds = requested_bounds
            if not bounds_intersect(evaluated_bounds, requested_bounds):
                stats["objects_rejected"] += 1
                continue
            entry, reused = self._extract_object(obj, depsgraph, scale, collision_types)
            live_keys.add(entry.key)
            entries.append(entry)
            stats["object_cache_hits" if reused else "object_cache_misses"] += 1
            if not reused:
                stats["triangles_extracted"] += entry.triangle_count

        chunks = []
        for coordinate in coordinates:
            bounds = chunk_bounds(coordinate, chunk_size)
            contributors = [entry for entry in entries if bounds_intersect(entry.bounds, bounds)]
            contributors.sort(key=lambda entry: repr(entry.key))
            fingerprint = self._chunk_fingerprint(coordinate, contributors, scale)
            cache_key = (coordinate, fingerprint)
            chunk = self.chunk_cache.get(cache_key)
            if chunk is None:
                stats["chunk_cache_misses"] += 1
                chunk = self._build_chunk(coordinate, contributors, scale, chunk_size, fingerprint)
                self.chunk_cache[cache_key] = chunk
            else:
                stats["chunk_cache_hits"] += 1
            chunks.append(chunk)

        if status_callback:
            status_callback("Loading nearby collision…")
        surface_array, surface_count, origin = self._assemble_native(
            chunks, center, scale, surface_type, vertical_reference=world_position[2]
        )
        stats["native_surface_count"] = surface_count
        stats["duration_seconds"] = time.perf_counter() - started
        self.last_stats = stats
        print(
            "libsm64 collision: chunk={} objects={} rejected={} object cache {}/{} "
            "triangles rebuilt={} chunk cache {}/{} surfaces={} time={:.3f}s".format(
                center, stats["objects_considered"], stats["objects_rejected"],
                stats["object_cache_hits"], stats["object_cache_misses"],
                stats["triangles_extracted"], stats["chunk_cache_hits"],
                stats["chunk_cache_misses"], surface_count, stats["duration_seconds"],
            )
        )
        return CollisionPreparation(center, origin, surface_array, surface_count, stats)


collision_cache = CollisionCache()
_collision_status = ""


def set_collision_status(message):
    """Store lightweight process-local feedback without importing Blender."""
    global _collision_status
    _collision_status = str(message or "")
    print("libsm64 collision status: {}".format(_collision_status))


def collision_status_message():
    return _collision_status


def collision_diagnostics():
    return dict(collision_cache.last_stats)


def clear_collision_cache():
    """Clear both cache layers and return ``(object_count, chunk_count)``."""
    counts = collision_cache.clear()
    set_collision_status(
        "Collision cache cleared ({} objects, {} chunks)".format(*counts)
    )
    return counts
