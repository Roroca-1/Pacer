// pacer core: process-wide limiter state shared by the Vulkan layer and
// the OpenGL preload hook. One instance per process (pacer::core()).
#pragma once
#include <atomic>
#include <stdint.h>
#include <string>
#include <vector>

struct pacer_shm;

namespace pacer {

enum Mode : uint32_t {
    MODE_SMOOTH = 0,   // wait before present on an absolute grid: best frame pacing
    MODE_LATENCY = 1,  // wait after present: game samples input right before rendering
    MODE_BALANCED = 2, // predictive: wait after present, wake just early enough to hit the grid
    MODE_COUNT = 3,
};

const char *mode_name(uint32_t m);
uint32_t mode_from_string(const std::string &s, uint32_t fallback);

int64_t now_ns(); // CLOCK_MONOTONIC

// present-mode override for Vulkan swapchains (values == VkPresentModeKHR, -1 = keep)
enum : int32_t { PM_KEEP = -1 };

struct OsdState {
    bool visible;
    bool enabled;
    uint32_t mode;
    float fps;
    float measured_fps;
};

class Core {
public:
    static Core &get();

    // Called once a swapchain / GL context exists. api: 1 vk, 2 glx, 3 egl.
    void attach(uint32_t api);
    bool active() const { return enabled_.load(std::memory_order_relaxed); }

    // Present hooks. pre_present is called right before the real present,
    // post_present right after it returns.
    void pre_present();
    void post_present();

    int32_t present_mode_override() const { return present_mode_.load(); }
    void set_effective_present_mode(uint32_t pm, uint32_t w, uint32_t h);

    bool osd_enabled() const { return osd_enabled_.load(); }
    // true if this process should get the OSD (it has a preset or limiting is on by default)
    bool wants_osd() const { return osd_enabled_.load() && !ignored_ && (has_preset_.load() || default_enabled_ || osd_all_); }
    bool ignored() const { return ignored_; }
    OsdState osd_state();
    const std::string &key() const { return key_; }
    // X11 window the app presents to (Vulkan xlib/xcb surface or GLX drawable), for focus detection
    void set_window(uint64_t w) { if (w) window_.store(w, std::memory_order_relaxed); }

private:
    Core();
    void ensure_thread();
    void aux_loop();
    void load_settings();
    bool load_preset(bool initial);
    void save_preset();
    void apply_hotkey(int which);
    void wait_until(int64_t deadline);
    void record_frame(int64_t now, int64_t waited);
    void show_osd();
    void effective(bool &en, uint32_t &mode, float &fps) const;
    bool bg_wanted() const;

    std::string key_, exe_;
    std::atomic<bool> enabled_{false};
    std::atomic<uint32_t> mode_{MODE_SMOOTH};
    std::atomic<float> fps_{60.f};
    std::atomic<int32_t> present_mode_{PM_KEEP};
    std::atomic<bool> has_preset_{false};
    std::atomic<bool> osd_enabled_{true};
    std::atomic<int64_t> osd_until_{0};
    std::atomic<bool> attached_{false};
    std::atomic<int64_t> save_at_{0};
    std::atomic<uint64_t> window_{0};
    std::atomic<bool> background_{false};   // window unfocused -> background limit applies
    std::atomic<float> bg_fps_{0.f};        // effective background limit (0 = off)
    float bg_fps_global_ = 0.f;
    float bg_fps_preset_ = -1.f;             // -1: use global, 0: off, >0: value

    // settings
    std::vector<float> values_;
    float step_ = 5.f, default_fps_ = 60.f, osd_seconds_ = 2.5f;
    bool hotkeys_enabled_ = true, osd_always_ = false, default_enabled_ = false;
    uint32_t default_mode_ = MODE_SMOOTH;
    bool ignored_ = false, osd_all_ = false;
    std::atomic<float> measured_fps_{0.f};
    std::string hk_[4]; // up, down, toggle, mode

    // pacing state (present thread only)
    int64_t target_ = 0;      // next grid point (smooth / balanced deadline)
    int64_t last_present_ = 0;
    int64_t pending_wait_ = 0;
    int64_t work_est_ = 0;    // balanced: estimated app work between wake and present
    int64_t wake_ = 0;        // balanced: time we woke after present
    int64_t spin_ns_ = 200000;// adaptive busy-wait window
    uint32_t last_mode_ = 99;
    int64_t pre_dl_ = 0;      // smooth: grid point the last present was released at
    int64_t pc_est_ = 0;      // smooth: upper envelope of the real present call's duration
    int64_t last_start_ = 0;  // time the previous frame started (we returned from present)
    // long-run statistics (present thread only; published in shm)
    bool st_en_ = false;
    uint32_t st_mode_ = 99;
    float st_fps_ = -1.f;
    void stats_frame(int64_t ft, bool en, uint32_t mode, float fps, int64_t interval);
    void stats_start(int64_t start);
    int64_t interval_for(float fps) const;
    std::atomic<float> display_hz_{0.f};   // real refresh rate from the GUI (0 = unknown)

    ::pacer_shm *shm_ = nullptr;
    int shm_fd_ = -1;
    std::string shm_path_;

    // preset file tracking
    std::string preset_path_;
    int64_t preset_mtime_ = -1;
    int64_t preset_size_ = -1;
};

inline Core &core() { return Core::get(); }

} // namespace pacer
