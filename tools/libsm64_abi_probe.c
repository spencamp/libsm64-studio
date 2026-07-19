#include <stddef.h>
#include <stdio.h>

#include "libsm64.h"

#define SIZE_ENTRY(type) "\"size\":" #type

static void verify_phase3a_function_signatures(void)
{
    void (*set_action)(int32_t, uint32_t) = sm64_set_mario_action;
    void (*set_animation)(int32_t, int32_t) = sm64_set_mario_animation;
    void (*set_anim_frame)(int32_t, int16_t) = sm64_set_mario_anim_frame;
    void (*set_state)(int32_t, uint32_t) = sm64_set_mario_state;
    void (*set_position)(int32_t, float, float, float) = sm64_set_mario_position;
    void (*set_faceangle)(int32_t, float) = sm64_set_mario_faceangle;
    void (*set_velocity)(int32_t, float, float, float) = sm64_set_mario_velocity;
    void (*set_forward_velocity)(int32_t, float) = sm64_set_mario_forward_velocity;
    void (*set_health)(int32_t, uint16_t) = sm64_set_mario_health;
    void (*set_invincibility)(int32_t, int16_t) = sm64_set_mario_invincibility;
    (void)set_action;
    (void)set_animation;
    (void)set_anim_frame;
    (void)set_state;
    (void)set_position;
    (void)set_faceangle;
    (void)set_velocity;
    (void)set_forward_velocity;
    (void)set_health;
    (void)set_invincibility;
}

static void verify_phase3b_function_signatures(void)
{
    void (*move_surface_object)(uint32_t, const struct SM64ObjectTransform *) =
        sm64_surface_object_move;
    (void)move_surface_object;
}

static void verify_phase3c_function_signatures(void)
{
    void (*set_water_level)(int32_t, signed int) = sm64_set_mario_water_level;
    void (*set_gas_level)(int32_t, signed int) = sm64_set_mario_gas_level;
    (void)set_water_level;
    (void)set_gas_level;
}

static void verify_phase3d_function_signatures(void)
{
    void (*interact_cap)(int32_t, uint32_t, uint16_t, uint8_t) =
        sm64_mario_interact_cap;
    void (*extend_cap)(int32_t, uint16_t) = sm64_mario_extend_cap;
    (void)interact_cap;
    (void)extend_cap;
}

static void verify_phase3f_function_signatures(void)
{
    void (*audio_init)(const uint8_t *) = sm64_audio_init;
    uint32_t (*audio_tick)(uint32_t, uint32_t, int16_t *) = sm64_audio_tick;
    void (*set_sound_volume)(float) = sm64_set_sound_volume;
    void (*register_play_sound)(SM64PlaySoundFunctionPtr) =
        sm64_register_play_sound_function;
    (void)audio_init;
    (void)audio_tick;
    (void)set_sound_volume;
    (void)register_play_sound;
}

static void verify_phase3g_function_signatures(void)
{
    void (*take_damage)(int32_t, uint32_t, uint32_t, float, float, float) =
        sm64_mario_take_damage;
    void (*heal)(int32_t, uint8_t) = sm64_mario_heal;
    void (*kill)(int32_t) = sm64_mario_kill;
    void (*set_invincibility)(int32_t, int16_t) =
        sm64_set_mario_invincibility;
    float (*find_floor)(float, float, float) = sm64_surface_find_floor_height;
    float (*find_water)(float, float) = sm64_surface_find_water_level;
    float (*find_gas)(float, float) = sm64_surface_find_poison_gas_level;
    void (*register_debug_print)(SM64DebugPrintFunctionPtr) =
        sm64_register_debug_print_function;
    (void)take_damage;
    (void)heal;
    (void)kill;
    (void)set_invincibility;
    (void)find_floor;
    (void)find_water;
    (void)find_gas;
    (void)register_debug_print;
}

int main(void)
{
    verify_phase3a_function_signatures();
    verify_phase3b_function_signatures();
    verify_phase3c_function_signatures();
    verify_phase3d_function_signatures();
    verify_phase3f_function_signatures();
    verify_phase3g_function_signatures();
    printf(
        "{"
        "\"pointer_size\":%zu,"
        "\"SM64Surface\":{\"size\":%zu,\"type\":%zu,\"force\":%zu,"
        "\"terrain\":%zu,\"vertices\":%zu},"
        "\"SM64MarioInputs\":{\"size\":%zu,\"camLookX\":%zu,\"camLookZ\":%zu,"
        "\"stickX\":%zu,\"stickY\":%zu,\"buttonA\":%zu,\"buttonB\":%zu,"
        "\"buttonZ\":%zu},"
        "\"SM64ObjectTransform\":{\"size\":%zu,\"position\":%zu,"
        "\"eulerRotation\":%zu},"
        "\"SM64SurfaceObject\":{\"size\":%zu,\"transform\":%zu,"
        "\"surfaceCount\":%zu,\"surfaces\":%zu},"
        "\"SM64MarioState\":{\"size\":%zu,\"position\":%zu,\"velocity\":%zu,"
        "\"faceAngle\":%zu,\"forwardVelocity\":%zu,\"health\":%zu,"
        "\"action\":%zu,\"animID\":%zu,\"animFrame\":%zu,\"flags\":%zu,"
        "\"particleFlags\":%zu,\"invincTimer\":%zu},"
        "\"SM64MarioGeometryBuffers\":{\"size\":%zu,\"position\":%zu,"
        "\"normal\":%zu,\"color\":%zu,\"uv\":%zu,\"numTrianglesUsed\":%zu}"
        "}\n",
        sizeof(void *),
        sizeof(struct SM64Surface),
        offsetof(struct SM64Surface, type),
        offsetof(struct SM64Surface, force),
        offsetof(struct SM64Surface, terrain),
        offsetof(struct SM64Surface, vertices),
        sizeof(struct SM64MarioInputs),
        offsetof(struct SM64MarioInputs, camLookX),
        offsetof(struct SM64MarioInputs, camLookZ),
        offsetof(struct SM64MarioInputs, stickX),
        offsetof(struct SM64MarioInputs, stickY),
        offsetof(struct SM64MarioInputs, buttonA),
        offsetof(struct SM64MarioInputs, buttonB),
        offsetof(struct SM64MarioInputs, buttonZ),
        sizeof(struct SM64ObjectTransform),
        offsetof(struct SM64ObjectTransform, position),
        offsetof(struct SM64ObjectTransform, eulerRotation),
        sizeof(struct SM64SurfaceObject),
        offsetof(struct SM64SurfaceObject, transform),
        offsetof(struct SM64SurfaceObject, surfaceCount),
        offsetof(struct SM64SurfaceObject, surfaces),
        sizeof(struct SM64MarioState),
        offsetof(struct SM64MarioState, position),
        offsetof(struct SM64MarioState, velocity),
        offsetof(struct SM64MarioState, faceAngle),
        offsetof(struct SM64MarioState, forwardVelocity),
        offsetof(struct SM64MarioState, health),
        offsetof(struct SM64MarioState, action),
        offsetof(struct SM64MarioState, animID),
        offsetof(struct SM64MarioState, animFrame),
        offsetof(struct SM64MarioState, flags),
        offsetof(struct SM64MarioState, particleFlags),
        offsetof(struct SM64MarioState, invincTimer),
        sizeof(struct SM64MarioGeometryBuffers),
        offsetof(struct SM64MarioGeometryBuffers, position),
        offsetof(struct SM64MarioGeometryBuffers, normal),
        offsetof(struct SM64MarioGeometryBuffers, color),
        offsetof(struct SM64MarioGeometryBuffers, uv),
        offsetof(struct SM64MarioGeometryBuffers, numTrianglesUsed)
    );
    return 0;
}
