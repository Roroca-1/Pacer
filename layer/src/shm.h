// Shared-memory status block published by every hooked process.
// Layout must stay identical between the 32-bit and 64-bit layer builds and
// the Python GUI (pacer/shm.py) -> only fixed-size fields, explicit padding.
#pragma once
#include <stdint.h>

#define PACER_SHM_MAGIC 0x52434150u /* "PACR" */
#define PACER_SHM_VERSION 2u
#define PACER_RING 256

#pragma pack(push, 4)
struct pacer_shm {
    uint32_t magic;          // 0
    uint32_t version;        // 4
    uint32_t pid;            // 8   pid as seen inside the process (may be namespaced)
    uint32_t api;            // 12  1 = vulkan, 2 = opengl(glx), 3 = opengl(egl)
    char     key[64];        // 16  preset key (lower-case exe name)
    char     exe[128];       // 80  full exe / argv0 for display
    int64_t  start_ns;       // 208 CLOCK_REALTIME at attach
    // live state written by the layer
    uint32_t enabled;        // 216
    uint32_t mode;           // 220
    float    fps_target;     // 224
    uint32_t has_preset;     // 228
    uint32_t present_mode;   // 232 effective VkPresentModeKHR (or 0xffffffff)
    uint32_t width;          // 236
    uint32_t height;         // 240
    uint32_t heartbeat;      // 244 increments ~every frame
    uint64_t frames;         // 248 total presents
    int64_t  last_present_ns;// 256 CLOCK_MONOTONIC
    uint32_t ring_head;      // 264 index of next write (mod PACER_RING)
    uint32_t flags;          // 268 bit0: background limit active (window unfocused)
    float    frametime_ms[PACER_RING]; // 272  present-to-present interval
    float    wait_ms[PACER_RING];      // 1296 time the limiter slept that frame
    // ---- version 2: long-run pacing statistics, reset whenever limit / method / on-off changes.
    // Frames longer than max(4 x target, 250 ms) (loading screens) are counted as hitches only.
    uint64_t stat_n;         // 2320 frames in the statistics
    double   stat_sum;       // 2328 sum of present-to-present frametimes (ms)
    double   stat_sumsq;     // 2336 sum of squares (ms^2)
    double   stat_abs_dev;   // 2344 sum of |frametime - target| (ms); target 0 = unlimited
    double   stat_s_sum;     // 2352 sum of frame-start intervals (ms): time between present returns,
    double   stat_s_sumsq;   // 2360   i.e. when the game samples input / advances its simulation
    int64_t  stat_reset_ns;  // 2368 CLOCK_MONOTONIC of the last reset
    float    stat_target_ms; // 2376 target interval the stats were taken against (0 = unlimited)
    uint32_t stat_hitches;   // 2380
    uint32_t stat_s_n;       // 2384 frame-start intervals in stat_s_*
    uint32_t _pad2;          // 2388
};                           // 2392
#pragma pack(pop)

static_assert(sizeof(struct pacer_shm) == 2392, "pacer_shm layout changed");
