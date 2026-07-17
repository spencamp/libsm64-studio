"""Headless create/reopen test for the packed shared Mario texture.

Create:
  blender --background --factory-startup --python this_file.py -- create output.blend
Reopen:
  blender output.blend --background --python this_file.py -- reopen
"""

import importlib.util
from pathlib import Path
import sys

import bpy


EXPECTED_RGBA = (201, 101, 51, 255)


def script_arguments():
    if "--" not in sys.argv:
        return []
    return sys.argv[sys.argv.index("--") + 1:]


def assert_packed_texture():
    image = bpy.data.images.get("libsm64_mario_texture")
    assert image is not None
    assert tuple(image.size) == (64 * 11, 64)
    assert image.packed_file is not None
    expected = tuple(channel / 255.0 for channel in EXPECTED_RGBA)
    actual = tuple(image.pixels[index] for index in range(4))
    assert all(abs(left - right) < 1e-5 for left, right in zip(actual, expected))

    material = bpy.data.materials.get("libsm64_mario_material")
    assert material is not None
    image_nodes = [node for node in material.node_tree.nodes if node.type == 'TEX_IMAGE']
    assert image_nodes
    assert all(node.image is image for node in image_nodes)

    takes = [bpy.data.objects.get("Texture Bake A"), bpy.data.objects.get("Texture Bake B")]
    assert all(take is not None for take in takes)
    assert all(take.data.materials[0] is material for take in takes)


def create_test_file(output_path):
    root = Path(__file__).resolve().parents[1]
    package_name = "libsm64_studio_texture_test"
    spec = importlib.util.spec_from_file_location(
        package_name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    addon = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = addon
    spec.loader.exec_module(addon)

    pixel_count = addon.mario.SM64_TEXTURE_WIDTH * addon.mario.SM64_TEXTURE_HEIGHT
    first_buffer = bytearray((9, 19, 29, 255)) * pixel_count
    final_buffer = bytearray(EXPECTED_RGBA) * pixel_count

    try:
        addon.mario.initialize_texture_image(bytearray(4))
        raise AssertionError("Invalid texture buffer length was accepted")
    except ValueError:
        pass

    # Begin with the wrong dimensions, then initialize twice. The second call
    # specifically exercises replacing an existing packed payload.
    invalid = bpy.data.images.new("libsm64_mario_texture", width=2, height=2)
    invalid.pack()
    addon.mario.initialize_all_data(first_buffer)
    addon.mario.initialize_all_data(final_buffer)

    mesh = bpy.data.meshes["libsm64_mario_mesh"]
    for name in ("Texture Bake A", "Texture Bake B"):
        take = bpy.data.objects.new(name, mesh.copy())
        bpy.context.scene.collection.objects.link(take)

    assert len([image for image in bpy.data.images if image.name == "libsm64_mario_texture"]) == 1
    assert_packed_texture()
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(output_path).resolve()))
    print("LIBSM64_TEXTURE_CREATE_PASSED")


arguments = script_arguments()
if not arguments:
    raise RuntimeError("Expected create/reopen mode after --")
if arguments[0] == "create":
    if len(arguments) != 2:
        raise RuntimeError("Create mode requires an output .blend path")
    create_test_file(arguments[1])
elif arguments[0] == "reopen":
    assert_packed_texture()
    print("LIBSM64_TEXTURE_REOPEN_PASSED")
else:
    raise RuntimeError("Unknown mode: {}".format(arguments[0]))
