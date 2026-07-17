# libsm64-blender - Blender client for LibSM64

This add-on integrates [libsm64](https://github.com/libsm64/libsm64) into Blender and provides various additional integrations with [Fast64](https://bitbucket.org/kurethedead/fast64/).
Practically, this means if you're making levels with Fast64 in Blender, you can use this add-on to drop a controller-playable Mario into your scene to run around and test your terrain layout.

**Warning:** This plugin hasn't been battle-tested for very long, save often and use at your own risk!

If you find a way to crash it, please post an issue or otherwise let me know!

![Example map](https://github.com/libsm64/libsm64-blender/raw/master/docs/example.gif)
###### Example map by [Agent-11](https://github.com/agent-11)

### Installation

Only Windows and linux are currently supported, no MacOS support yet unfortunately.

Download the latest release zip [from here](https://github.com/libsm64/libsm64-blender/releases). In Blender, go to Edit -> Preferences -> Add-Ons and click the "Install" button to install the plugin from the zip file. Find the libsm64-blender addon in the addon list and enable it. If it does not show up, go to Edit -> Preferences -> Save&Load and make sure 'Auto Run Python Scripts' is enabled.

### Usage
Before opening Blender make sure you have an XInput controller connected if you want to control Mario with a controller. Alternatively you can use the keyboard to control him. With the add-on enabled there should be a LibSM64 tab in the properties sidebar. Browse to an unmodified SM64 US z64 ROM, and then click the "Insert Mario" button to insert a controllable Mario at the 3D cursor location. To stop the simulation just delete the "LibSM64 Mario" object from the scene.

*Note:* The SM64 US ROM must be the one with the SHA1 checksum of `9bef1128717f958171a4afac3ed78ee2bb4e86ce`.

### Recording short animations

The **Animation Recording** section in the LibSM64 sidebar can turn a short live
performance into a self-contained Blender mesh animation:

1. Set the scene FPS, insert Mario, and control him normally.
2. Move the timeline to the desired output start frame and click **Start Recording**.
3. Perform the take, then click **Stop & Bake**.
4. Scrub or render the selected `LibSM64 Mario Bake` object. The live object is
   stopped and hidden after a successful bake.

The bake has one shape key per 30 Hz libsm64 sample and uses constant
interpolation. Samples are placed at fractional frames when necessary, preserving
the take's real-time duration at 24, 30, 60, or other target frame rates. Each
take owns its mesh, shape-key datablock, and action, so later takes do not modify
earlier ones. The baked object can be saved and reopened without libsm64, the ROM,
a controller, or a frame-change handler.

This MVP is intended for short cinematic takes. A four-second take creates about
120 shape keys, and the panel warns at 300 samples (about ten seconds); there is
no hard sample limit. It records vertex positions only. The copied mesh preserves
the current material, texture image, UV layer, and vertex colors, but later
blinking/facial UV changes, changing vertex colors, simulation, and collision are
not part of baked playback. Blender calculates displayed normals from the
deformed geometry.

Use **Cancel Recording** to discard a pending take without stopping Mario.

### Manual recording smoke test

Run the following for each target FPS you need to validate (especially 24, 30,
and 60):

1. Open Blender with the add-on enabled and set the scene FPS.
2. Add collision ground, place the 3D cursor over it, and insert Mario.
3. Start recording and control Mario for approximately four seconds.
4. Click **Stop & Bake** and confirm the baked object is selected, the live object
   is hidden, and the scene FPS has returned to its original value.
5. Scrub from the recording start frame through the take. Confirm poses are held,
   do not blend, and the duration is about four seconds.
6. Render frames in Eevee and Cycles.
7. Save the `.blend`, close Blender, disconnect the controller or make the ROM
   unavailable, reopen the file, and verify the bake still scrubs and renders.
8. Insert Mario again, record a second take, and verify the first bake and its
   action remain unchanged.
9. Install a temporary unrelated `frame_change_pre` handler, run and stop another
   simulation, and verify that handler remains installed.

### Texture persistence acceptance test

1. Insert Mario and confirm the normal red, blue, and skin colors are visible.
2. Record and bake at least two takes.
3. Save the `.blend` and close Blender completely.
4. Reopen the saved file without clicking **Insert Mario**.
5. Switch the viewport to Material Preview and confirm every baked Mario is fully
   textured rather than black.
6. Press Play and confirm both the animation and texture continue to work.
7. Temporarily disable the add-on, reopen the file if Blender requests it, and
   confirm the baked Marios remain textured and animated without the ROM or
   libsm64 being loaded.

The generated `libsm64_mario_texture` image is shared by all live and baked Mario
objects and is packed into the `.blend`. Inserting Mario again refreshes that
single packed image from the ROM; it does not create a texture per take.

### Current Features
- Insert playable Mario into Blender scene
- Fast64 terrain type and collision surface type support
- Bake short Mario performances to self-contained shape-key animation

### Near-term Features
- Water boxes support
- Toggles to give wing/metal/vanish cap

### Far-term Features
- Moving platform support
- Camera integration
- Linking against custom decomp builds (modified controls/Mario model/etc)
