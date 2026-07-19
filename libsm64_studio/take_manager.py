"""Persistent metadata and explicit visibility transitions for baked takes."""

import uuid


TAKE_ID = "libsm64_take_id"
TAKE_NUMBER = "libsm64_take_number"
TAKE_DISPOSITION = "libsm64_take_disposition"
TAKE_OWNER = "libsm64_take_owner"
SCENE_CURRENT_TAKE = "libsm64_current_take_id"
SCENE_NEXT_TAKE = "libsm64_next_take_number"
SCENE_SCHEMA_VERSION = "libsm64_take_schema_version"
TAKE_SCHEMA_VERSION = 2
RUNTIME_METADATA_TEXT_PROPERTY = "libsm64_runtime_metadata_text"

REGULAR = "REGULAR"
FAVORITE = "FAVORITE"
REJECTED = "REJECTED"
DISPOSITIONS = {REGULAR, FAVORITE, REJECTED}


class TakeError(RuntimeError):
    pass


def take_label(obj):
    return "Take {:03d}".format(int(obj.get(TAKE_NUMBER, 0)))


def is_take(obj):
    return bool(obj.get("libsm64_is_bake", False)) and bool(obj.get(TAKE_ID, ""))


def iter_takes():
    import bpy
    return [obj for obj in bpy.data.objects if is_take(obj)]


def find_take(take_id):
    if not take_id:
        return None
    for obj in iter_takes():
        if obj.get(TAKE_ID) == take_id:
            return obj
    return None


def current_take(scene):
    """Return the current take without repairing scene metadata."""
    return find_take(scene.get(SCENE_CURRENT_TAKE, ""))


def _set_visible(obj, visible):
    obj.hide_render = not visible
    try:
        obj.hide_set(not visible)
    except (AttributeError, RuntimeError):
        obj.hide_viewport = not visible


def apply_visibility(scene):
    current_id = scene.get(SCENE_CURRENT_TAKE, "")
    for obj in iter_takes():
        disposition = obj.get(TAKE_DISPOSITION, REGULAR)
        visible = disposition == FAVORITE or (
            disposition == REGULAR and obj.get(TAKE_ID) == current_id
        )
        _set_visible(obj, visible)


def reconcile_scene(scene, select_fallback=True, exclude=None):
    """Migrate old bakes and recover counters/references from persistent ID props."""
    import bpy

    had_current_reference = SCENE_CURRENT_TAKE in scene
    bakes = [
        obj for obj in bpy.data.objects
        if obj is not exclude and obj.get("libsm64_is_bake", False)
    ]
    used = {
        int(obj.get(TAKE_NUMBER, 0)) for obj in bakes
        if int(obj.get(TAKE_NUMBER, 0)) > 0
    }
    next_number = max(1, int(scene.get(SCENE_NEXT_TAKE, 1)))
    for obj in sorted(bakes, key=lambda item: item.name):
        if not obj.get(TAKE_ID):
            obj[TAKE_ID] = uuid.uuid4().hex
        if obj.get(TAKE_DISPOSITION) not in DISPOSITIONS:
            obj[TAKE_DISPOSITION] = REGULAR
        number = int(obj.get(TAKE_NUMBER, 0))
        if number <= 0:
            while next_number in used:
                next_number += 1
            number = next_number
            obj[TAKE_NUMBER] = number
            used.add(number)
            next_number += 1
        take_id = obj[TAKE_ID]
        mesh = getattr(obj, "data", None)
        if mesh is not None and mesh.users == 1:
            if not mesh.get(TAKE_OWNER):
                mesh[TAKE_OWNER] = take_id
            key_data = getattr(mesh, "shape_keys", None)
            if key_data is not None and key_data.users == 1:
                if not key_data.get(TAKE_OWNER):
                    key_data[TAKE_OWNER] = take_id
                pose_animation = getattr(key_data, "animation_data", None)
                pose_action = getattr(pose_animation, "action", None)
                if (
                    pose_action is not None
                    and pose_action.users == 1
                    and not pose_action.get(TAKE_OWNER)
                ):
                    pose_action[TAKE_OWNER] = take_id
            object_animation = getattr(obj, "animation_data", None)
            transform_action = getattr(object_animation, "action", None)
            if (
                transform_action is not None
                and transform_action.users == 1
                and not transform_action.get(TAKE_OWNER)
            ):
                transform_action[TAKE_OWNER] = take_id

    scene[SCENE_NEXT_TAKE] = max(
        next_number,
        max(used, default=0) + 1,
        int(scene.get(SCENE_NEXT_TAKE, 1)),
    )
    referenced_id = scene.get(SCENE_CURRENT_TAKE, "")
    if referenced_id and current_take(scene) is None:
        scene[SCENE_CURRENT_TAKE] = ""
        had_current_reference = False
    if (current_take(scene) is None and select_fallback and bakes
            and not had_current_reference):
        active = getattr(bpy.context.view_layer.objects, "active", None)
        candidates = [obj for obj in bakes if obj.get(TAKE_DISPOSITION) != REJECTED]
        fallback = active if active in candidates else max(
            candidates, key=lambda obj: int(obj.get(TAKE_NUMBER, 0)), default=None
        )
        if fallback is not None:
            scene[SCENE_CURRENT_TAKE] = fallback[TAKE_ID]
    apply_visibility(scene)
    scene[SCENE_SCHEMA_VERSION] = TAKE_SCHEMA_VERSION


def _mark_owned(datablock, take_id):
    if datablock is not None:
        datablock[TAKE_OWNER] = take_id


def _property_snapshot(owner, key):
    return key in owner, owner.get(key)


def _restore_property(owner, key, snapshot):
    existed, value = snapshot
    if existed:
        owner[key] = value
    elif key in owner:
        del owner[key]


def register_baked_take(scene, obj, runtime_samples=None):
    """Commit one already-built take without rewriting any earlier take."""
    mesh = getattr(obj, "data", None)
    if mesh is None:
        # Lightweight state-machine tests use metadata-only stand-ins. Runtime
        # Blender bakes always take the ownership-validated branch below.
        number = max(1, int(scene.get(SCENE_NEXT_TAKE, 1)))
        scene[SCENE_NEXT_TAKE] = number + 1
        obj[TAKE_ID] = uuid.uuid4().hex
        obj[TAKE_NUMBER] = number
        obj[TAKE_DISPOSITION] = REGULAR
        scene[SCENE_CURRENT_TAKE] = obj[TAKE_ID]
        apply_visibility(scene)
        return number

    from .recording import (
        RecordingError,
        create_take_runtime_metadata_text,
        remove_take_runtime_metadata_text,
        validate_take_ownership,
        validate_take_runtime_metadata,
    )
    mesh, key_data, pose_action, transform_action = validate_take_ownership(
        obj, require_schema_2=True
    )
    existing_numbers = [int(take.get(TAKE_NUMBER, 0)) for take in iter_takes() if take is not obj]
    number = max(
        1,
        int(scene.get(SCENE_NEXT_TAKE, 1)),
        max(existing_numbers, default=0) + 1,
    )
    take_id = uuid.uuid4().hex
    prior_scene = {
        key: _property_snapshot(scene, key)
        for key in (SCENE_CURRENT_TAKE, SCENE_NEXT_TAKE, SCENE_SCHEMA_VERSION)
    }
    prior_visibility = [
        (take, take.hide_render, take.hide_get()) for take in iter_takes() if take is not obj
    ]
    prior_candidate_visibility = (obj.hide_render, obj.hide_get())
    prior_names = (
        obj.name, mesh.name, key_data.name, transform_action.name, pose_action.name
    )
    object_property_keys = (
        TAKE_ID, TAKE_NUMBER, TAKE_DISPOSITION, RUNTIME_METADATA_TEXT_PROPERTY,
    )
    prior_object_properties = {
        key: _property_snapshot(obj, key) for key in object_property_keys
    }
    owned_datablocks = (mesh, key_data, pose_action, transform_action)
    prior_owners = [
        (datablock, _property_snapshot(datablock, TAKE_OWNER))
        for datablock in owned_datablocks
    ]
    runtime_text = None
    try:
        obj[TAKE_ID] = take_id
        obj[TAKE_NUMBER] = number
        obj[TAKE_DISPOSITION] = REGULAR
        obj.name = "LibSM64 Studio Take {:03d}".format(number)
        mesh.name = "LibSM64 Studio Take {:03d} Mesh".format(number)
        key_data.name = "LibSM64 Studio Take {:03d} Shape Keys".format(number)
        transform_action.name = "LibSM64 Studio Take {:03d} Transform Action".format(number)
        pose_action.name = "LibSM64 Studio Take {:03d} Pose Action".format(number)
        for datablock in owned_datablocks:
            _mark_owned(datablock, take_id)

        if runtime_samples is not None:
            runtime_text = create_take_runtime_metadata_text(
                obj,
                runtime_samples,
                take_id,
                number,
                float(obj.get("libsm64_recording_start_frame", 0.0)),
                float(obj.get("libsm64_target_fps", 0.0)),
            )

        # Ownership and metadata are complete before any earlier take is hidden.
        validate_take_ownership(obj, require_schema_2=True)
        if any(datablock.get(TAKE_OWNER) != take_id for datablock in owned_datablocks):
            raise TakeError("The new take ownership transaction did not commit all datablocks")
        if runtime_samples is not None:
            validated_runtime = validate_take_runtime_metadata(
                obj, expected_take_id=take_id
            )
            if (
                validated_runtime is None
                or validated_runtime["text"] is not runtime_text
                or runtime_text.get(TAKE_OWNER) != take_id
            ):
                raise TakeError("The new take runtime metadata ownership did not commit")
        scene[SCENE_NEXT_TAKE] = number + 1
        scene[SCENE_CURRENT_TAKE] = take_id
        scene[SCENE_SCHEMA_VERSION] = TAKE_SCHEMA_VERSION
        apply_visibility(scene)
        return number
    except Exception:
        if runtime_text is not None:
            try:
                remove_take_runtime_metadata_text(obj, expected_take_id=take_id)
            except RecordingError:
                pass
        for key, snapshot in prior_object_properties.items():
            _restore_property(obj, key, snapshot)
        for datablock, snapshot in prior_owners:
            _restore_property(datablock, TAKE_OWNER, snapshot)
        (
            obj.name,
            mesh.name,
            key_data.name,
            transform_action.name,
            pose_action.name,
        ) = prior_names
        for key, snapshot in prior_scene.items():
            _restore_property(scene, key, snapshot)
        for take, hide_render, hidden in prior_visibility:
            take.hide_render = hide_render
            try:
                take.hide_set(hidden)
            except (AttributeError, RuntimeError):
                take.hide_viewport = hidden
        obj.hide_render = prior_candidate_visibility[0]
        try:
            obj.hide_set(prior_candidate_visibility[1])
        except (AttributeError, RuntimeError):
            obj.hide_viewport = prior_candidate_visibility[1]
        raise


def runtime_metadata_for_take(obj):
    """Return validated persisted metadata, or None for a compatible legacy take."""
    from .recording import RecordingError, validate_take_runtime_metadata

    if not is_take(obj):
        raise TakeError("Runtime metadata requires a registered baked take")
    try:
        return validate_take_runtime_metadata(obj)
    except RecordingError as exc:
        raise TakeError(str(exc)) from exc


def select_take(context, obj):
    if not is_take(obj) or obj.get(TAKE_DISPOSITION) == REJECTED:
        raise TakeError("Rejected takes must be restored before selection")
    context.scene[SCENE_CURRENT_TAKE] = obj[TAKE_ID]
    apply_visibility(context.scene)
    try:
        for selected in list(context.selected_objects):
            selected.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
    except RuntimeError:
        # The take can be current even if its collection is excluded from the
        # active view layer; select it when Blender exposes it there again.
        pass


def favorite_take(scene, obj):
    if obj.get(TAKE_DISPOSITION) == REJECTED:
        raise TakeError("Restore this take before favoriting it")
    obj[TAKE_DISPOSITION] = FAVORITE
    apply_visibility(scene)


def unfavorite_take(scene, obj):
    if obj.get(TAKE_DISPOSITION) != FAVORITE:
        return
    obj[TAKE_DISPOSITION] = REGULAR
    apply_visibility(scene)


def reject_take(scene, obj):
    if obj.get(TAKE_DISPOSITION) == FAVORITE:
        raise TakeError("Unfavorite this take before rejecting it")
    obj[TAKE_DISPOSITION] = REJECTED
    if scene.get(SCENE_CURRENT_TAKE) == obj.get(TAKE_ID):
        scene[SCENE_CURRENT_TAKE] = ""
    apply_visibility(scene)


def restore_take(context, obj):
    if obj.get(TAKE_DISPOSITION) != REJECTED:
        return
    obj[TAKE_DISPOSITION] = REGULAR
    select_take(context, obj)


def cleanup_rejected(scene=None):
    """Delete rejected objects and only datablocks proven to be take-owned."""
    import bpy
    from .recording import RecordingError, remove_take_runtime_metadata_text

    rejected = [obj for obj in iter_takes() if obj.get(TAKE_DISPOSITION) == REJECTED]
    removed = 0
    for obj in rejected:
        take_id = obj.get(TAKE_ID)
        mesh = getattr(obj, "data", None)
        key_data = getattr(mesh, "shape_keys", None) if mesh is not None else None
        pose_animation = getattr(key_data, "animation_data", None)
        pose_action = getattr(pose_animation, "action", None)
        object_animation = getattr(obj, "animation_data", None)
        transform_action = getattr(object_animation, "action", None)
        try:
            remove_take_runtime_metadata_text(obj, expected_take_id=take_id)
        except RecordingError:
            # Never delete a Text whose exclusive take ownership is uncertain.
            pass
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.get(TAKE_OWNER) == take_id and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        # Removing an exclusively owned mesh also removes its shape-key
        # datablock. Shared/duplicated meshes are deliberately left intact.
        actions = []
        for action in (pose_action, transform_action):
            if action is not None and all(action is not existing for existing in actions):
                actions.append(action)
        for action in actions:
            if action.get(TAKE_OWNER) == take_id and action.users == 0:
                bpy.data.actions.remove(action)
        removed += 1
    if scene is not None:
        current_take(scene)
    return removed
