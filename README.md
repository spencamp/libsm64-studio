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
Before opening Blender make sure you have an XInput controller connected if you want to control Mario with a controller. Alternatively you can use the keyboard to control him. With the add-on enabled there should be a LibSM64 tab in the properties sidebar. Browse to an unmodified SM64 US z64 ROM, and then click the "Insert Mario" button to insert a controllable Mario at the 3D cursor location. Use **End Mario Control** when finished; this also performs deferred rejected-take cleanup.

*Note:* The SM64 US ROM must be the one with the SHA1 checksum of `9bef1128717f958171a4afac3ed78ee2bb4e86ce`.

### Recording performance takes

The **Performance Recording** section is built around one controllable Live Mario
and any number of baked takes:

1. Set the scene FPS and insert Mario. Live Mario immediately enters rehearsal
   mode and remains controllable between takes.
2. Maneuver Mario to the desired position and click **Set Start Mark**. Use
   **Reset to Mark** whenever you want to return there with transient native
   movement state cleared.
3. Move the timeline to the desired output start frame and click **Start
   Recording**. Recording does not move Mario or replace the Start Mark by
   default, so you may intentionally begin a take from somewhere else.
4. Optionally enable **Reset to Mark when recording starts** to reset and resume
   simulation at the mark before capture begins. This option is unavailable
   until the active Live Mario session has a valid mark.
5. Perform the take, then click **Stop & Bake**. Live Mario returns to the
   persistent Start Mark when one exists and pauses for review; without a mark,
   baking still succeeds and Live Mario pauses in place.
6. Scrub or play the result with Blender's normal timeline controls, favorite or
   reject it, click **Reset to Mark** to rehearse, or click **Start Recording**
   for another take without reinserting Mario.

Live simulation runs from one add-on-owned Blender timer at approximately 30 Hz.
It is independent of timeline playback and never changes the scene's render FPS
or FPS base. Timeline playback and scrubbing therefore remain available at the
chosen output rate while Live Mario continues to respond. Idle/rehearsal ticks
update only Live Mario; geometry enters the recorder only between **Start
Recording** and **Stop & Bake**.

Takes appear as `Take 001`, `Take 002`, and so on. Numbers increase monotonically
and are not reused after deletion. The current regular take is visible; selecting
another regular take hides the previous one without changing the current frame or
playback state. Favorites remain visible together, including while another take
is current. Unfavoriting never rejects a take.

Rejecting a regular take hides it and moves it into the collapsed **Rejected**
section. It can be restored until live control ends. Favorites must be unfavorited
before rejection. **End Mario Control** permanently removes rejected take objects
and their exclusively owned animation data while preserving regular takes,
favorites, shared materials, and the packed Mario texture.

Take identity, number, disposition, current selection, and the next number are
stored in the `.blend` as stable metadata, so object renaming and reordering do
not break the take manager. The inline capture confirmation disappears after
about two seconds and does not require dismissing a dialog.

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

Use **Cancel Recording** to discard a pending take and return Live Mario to the
persistent Start Mark when one exists, without creating a take. Stop, cancel,
and repeated recordings never replace the mark. **End Mario Control** clears it,
and a mark from an older native lifecycle generation is never reused.
Live Mario pauses for review; **Reset to Mark** leaves it controllable, and the
next **Start Recording** resumes simulation before capture.
Live Mario remains visible during control; baked-take visibility continues to
follow the current/favorite/regular/rejected rules independently. If the two
overlap after a reset, move Live Mario to continue rehearsing or hide it manually.

### Manual recording smoke test

### Automated Blender CLI tests

From the repository root, build and test the installable add-on with:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_blender_tests.ps1
```

The runner defaults to the Steam Blender 5.2 installation at
`C:\Program Files (x86)\Steam\steamapps\common\Blender\5.2\blender.exe`. Override
it when needed with `-BlenderPath "C:\path\to\blender.exe"`. For Steam layouts
that keep `blender.exe` beside the `5.2` data directory, the runner detects the
sibling executable automatically. Run only the
packaged enable/import/register/unregister check with `-SmokeOnly`. Add
`-KeepTemp` to retain the run directory for diagnosis.

Each invocation creates uniquely named package staging, ZIP, Blender user
configuration, user scripts/add-ons, user data, extension, and `.blend` paths
under the system temporary directory. It copies the complete installable package
and native DLLs before Blender starts, verifies the ZIP contents, and runs with
`--background --factory-startup`. It never installs into the normal Blender
profile, opens an existing `.blend`, or attaches to another Blender process.

The automated suite covers packaged add-on import and lifecycle registration,
timer-driven live control, persistent Start Mark transitions and ownership,
native lifecycle ownership, and the three-take regression. The controller feel,
viewport redraw/appearance, material preview,
Eevee/Cycles rendering, and interactive playback/scrubbing checks below still
require a normal GUI Blender session.

Run the following for each target FPS you need to validate (especially 24, 30,
and 60):

1. Open Blender with the add-on enabled and set the scene FPS to 24.
2. Add collision ground, place the 3D cursor over it, and insert Mario.
3. Confirm Mario moves before recording. Rehearse for at least ten seconds and
   verify no samples or take are created.
4. Stop at a chosen position and click **Set Start Mark**. Move somewhere else,
   leave automatic reset disabled, start recording, and confirm Mario is not
   repositioned. Cancel or bake that take.
5. Click **Reset to Mark**, enable **Reset to Mark when recording starts**, then
   start recording and confirm capture begins from the saved mark. Perform for
   approximately four seconds.
6. Click **Stop & Bake** and confirm `Take 001` is selected and visible, Live
   Mario returns to the persistent Start Mark and pauses for review, while the
   scene stays at 24 FPS with its original FPS base.
7. Scrub from the recording start frame through the take. Confirm poses are held,
   do not blend, and the duration is about four seconds.
8. Complete several more takes and confirm the Start Mark is never replaced.
   Set a new mark, reset, and confirm the replacement position is used.
9. Start another take, cancel it, and confirm Live Mario returns to the persistent
   Start Mark and pauses for review. Click **Reset to Mark** and confirm control resumes.
10. Play and scrub baked takes at 24 FPS while confirming Live Mario's timer does
   not force timeline playback or move the current frame on its own.
11. Render frames in Eevee and Cycles.
12. Save the `.blend`, close Blender, disconnect the controller or make the ROM
   unavailable, reopen the file, and verify the bake still scrubs and renders.
13. Verify `Take 001` is hidden while its mesh and action remain unchanged after
   the later regular take becomes current.
14. Install a temporary unrelated `frame_change_pre` handler, run and stop another
   simulation, and verify that handler remains installed.
15. Favorite a take and record another; confirm both remain visible. Reject two
   regular takes, end Mario control, and confirm only rejected take-owned data is
   deleted.
16. End Mario control, insert Mario again, and confirm the old Start Mark is
   unavailable. Save and reopen a file containing regular, favorite, and rejected takes;
   verify categories, current visibility, and the next take number are restored.
17. Repeat the rehearsal, record, bake, cancel, review, and shutdown checks at
    30 and 60 FPS. Confirm native Mario delete/global terminate occur once at
    **End Mario Control** and no owned timer remains.

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
