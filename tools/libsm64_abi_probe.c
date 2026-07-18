#include <stddef.h>
#include <stdio.h>

#include "libsm64.h"

#define SIZE_ENTRY(type) "\"size\":" #type

int main(void)
{
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
