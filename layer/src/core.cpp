#include "core.h"
#include "shm.h"

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <map>
#include <sched.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <thread>
#include <time.h>
#include <unistd.h>
#include <vector>
#include <xcb/xcb.h>   // types only; libxcb is dlopen()ed

namespace pacer {

static const float FPS_MAX = 20000.f;

static void logf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void logf(const char *fmt, ...) {
    static int verbose = -1;
    if (verbose < 0) {
        const char *v = getenv("PACER_DEBUG");
        verbose = v && *v && *v != '0';
    }
    if (!verbose) return;
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "[pacer] ");
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

int64_t now_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

const char *mode_name(uint32_t m) {
    switch (m) {
    case MODE_SMOOTH: return "smooth";
    case MODE_LATENCY: return "latency";
    case MODE_BALANCED: return "balanced";
    }
    return "smooth";
}

uint32_t mode_from_string(const std::string &s, uint32_t fb) {
    if (s == "smooth" || s == "0") return MODE_SMOOTH;
    if (s == "latency" || s == "lowlatency" || s == "1") return MODE_LATENCY;
    if (s == "balanced" || s == "predictive" || s == "2") return MODE_BALANCED;
    return fb;
}

// ---------------------------------------------------------------- paths/config
static std::string home_dir() {
    const char *h = getenv("HOME");
    return h && *h ? h : "/tmp";
}

static std::string config_dir() {
    if (const char *o = getenv("PACER_CONFIG_DIR")) return o;
    const char *x = getenv("XDG_CONFIG_HOME");
    return (x && *x ? std::string(x) : home_dir() + "/.config") + "/pacer";
}

static std::string state_dir() {
    // Deliberately under $HOME (not XDG_RUNTIME_DIR / /dev/shm): Steam's
    // pressure-vessel container always shares $HOME with the host.
    if (const char *o = getenv("PACER_STATE_DIR")) return o;
    const char *x = getenv("XDG_STATE_HOME");
    return (x && *x ? std::string(x) : home_dir() + "/.local/state") + "/pacer";
}

static void mkdirs(const std::string &p) {
    std::string cur;
    for (size_t i = 0; i <= p.size(); i++) {
        if (i == p.size() || p[i] == '/') {
            if (!cur.empty()) mkdir(cur.c_str(), 0755);
        }
        if (i < p.size()) cur += p[i];
    }
}

using KV = std::vector<std::pair<std::string, std::string>>;

static std::string trim(const std::string &s) {
    size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}

static bool read_kv(const std::string &path, KV &out) {
    FILE *f = fopen(path.c_str(), "r");
    if (!f) return false;
    char line[512];
    while (fgets(line, sizeof line, f)) {
        std::string l = trim(line);
        if (l.empty() || l[0] == '#' || l[0] == ';' || l[0] == '[') continue;
        size_t eq = l.find('=');
        if (eq == std::string::npos) continue;
        out.emplace_back(trim(l.substr(0, eq)), trim(l.substr(eq + 1)));
    }
    fclose(f);
    return true;
}

static const std::string *kv_get(const KV &kv, const char *k) {
    for (auto &p : kv)
        if (p.first == k) return &p.second;
    return nullptr;
}

static void kv_set(KV &kv, const std::string &k, const std::string &v) {
    for (auto &p : kv)
        if (p.first == k) { p.second = v; return; }
    kv.emplace_back(k, v);
}

static bool parse_bool(const std::string &s) {
    return s == "1" || s == "true" || s == "yes" || s == "on";
}

static bool file_sig(const std::string &p, int64_t &mtime, int64_t &size) {
    struct stat st;
    if (stat(p.c_str(), &st) != 0) { mtime = size = -1; return false; }
    mtime = (int64_t)st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
    size = st.st_size;
    return true;
}

// ---------------------------------------------------------------- process key
static std::string sanitize_key(std::string s) {
    for (auto &c : s) {
        c = (char)tolower((unsigned char)c);
        if (!(isalnum((unsigned char)c) || c == '.' || c == '-' || c == '_' || c == '+')) c = '_';
    }
    if (s.size() > 60) s.resize(60);
    if (s.empty()) s = "unknown";
    return s;
}

static std::string basename_any(const std::string &p) {
    size_t i = p.find_last_of("/\\");
    return i == std::string::npos ? p : p.substr(i + 1);
}

static void process_identity(std::string &key, std::string &exe) {
    char buf[4096];
    ssize_t n = readlink("/proc/self/exe", buf, sizeof buf - 1);
    std::string self = n > 0 ? std::string(buf, n) : "";
    std::string base = basename_any(self);
    std::string argv0;
    if (FILE *f = fopen("/proc/self/cmdline", "rb")) {
        size_t r = fread(buf, 1, sizeof buf - 1, f);
        buf[r] = 0;
        argv0 = buf;
        fclose(f);
    }
    bool wine = base.find("wine") != std::string::npos || base.find("preloader") != std::string::npos;
    if (wine && !argv0.empty()) {
        exe = argv0;
        base = basename_any(argv0);
    } else {
        exe = self.empty() ? argv0 : self;
    }
    if (const char *o = getenv("PACER_KEY")) base = o;
    key = sanitize_key(base);
}

// ---------------------------------------------------------------- X11 hotkeys
typedef void *(*PFN_XOpenDisplay)(const char *);
typedef int (*PFN_XQueryKeymap)(void *, char[32]);
typedef unsigned long (*PFN_XStringToKeysym)(const char *);
typedef unsigned char (*PFN_XKeysymToKeycode)(void *, unsigned long);

struct X11 {
    void *lib = nullptr, *dpy = nullptr;
    PFN_XQueryKeymap query = nullptr;
    PFN_XStringToKeysym str2sym = nullptr;
    PFN_XKeysymToKeycode sym2code = nullptr;
    bool open() {
        const char *d = getenv("DISPLAY");
        if (!d || !*d) return false;
        lib = dlopen("libX11.so.6", RTLD_NOW | RTLD_LOCAL);
        if (!lib) return false;
        auto xopen = (PFN_XOpenDisplay)dlsym(lib, "XOpenDisplay");
        query = (PFN_XQueryKeymap)dlsym(lib, "XQueryKeymap");
        str2sym = (PFN_XStringToKeysym)dlsym(lib, "XStringToKeysym");
        sym2code = (PFN_XKeysymToKeycode)dlsym(lib, "XKeysymToKeycode");
        if (!xopen || !query || !str2sym || !sym2code) return false;
        dpy = xopen(nullptr); // private connection, never shared with the app
        return dpy != nullptr;
    }
    unsigned char code(const char *name) {
        unsigned long s = str2sym(name);
        return s ? sym2code(dpy, s) : 0;
    }
};

struct Hotkey {
    std::vector<std::vector<unsigned char>> mods; // each modifier: any of these codes
    unsigned char key = 0;
    bool down = false;
    int64_t next_repeat = 0;
};

static std::string key_alias(std::string k) {
    std::string l;
    for (char c : k) l += (char)tolower((unsigned char)c);
    static const std::map<std::string, std::string> m = {
        {"pgup", "Prior"}, {"pageup", "Prior"}, {"prior", "Prior"}, {"pgdn", "Next"},
        {"pagedown", "Next"}, {"pgdown", "Next"}, {"next", "Next"}, {"home", "Home"}, {"end", "End"},
        {"up", "Up"}, {"down", "Down"}, {"left", "Left"}, {"right", "Right"},
        {"insert", "Insert"}, {"ins", "Insert"}, {"delete", "Delete"}, {"del", "Delete"},
        {"plus", "plus"}, {"minus", "minus"}, {"equal", "equal"}, {"kp_add", "KP_Add"},
        {"kp_subtract", "KP_Subtract"}, {"kp+", "KP_Add"}, {"kp-", "KP_Subtract"},
        {"pause", "Pause"}, {"scrolllock", "Scroll_Lock"}, {"space", "space"},
        {"backspace", "BackSpace"}, {"tab", "Tab"}, {"[", "bracketleft"}, {"]", "bracketright"},
    };
    auto it = m.find(l);
    if (it != m.end()) return it->second;
    if (l.size() >= 2 && l[0] == 'f' && isdigit((unsigned char)l[1])) return "F" + l.substr(1);
    if (l.size() == 1) return l; // letters/digits
    return k;
}

static bool parse_hotkey(X11 &x, const std::string &spec, Hotkey &hk) {
    hk = Hotkey{};
    std::string s = spec;
    if (s.empty() || s == "none") return false;
    std::vector<std::string> parts;
    size_t st = 0;
    for (size_t i = 0; i <= s.size(); i++) {
        if (i == s.size() || (s[i] == '+' && i > st)) {
            parts.push_back(trim(s.substr(st, i - st)));
            st = i + 1;
        }
    }
    if (parts.empty()) return false;
    for (size_t i = 0; i + 1 < parts.size(); i++) {
        std::string m;
        for (char c : parts[i]) m += (char)tolower((unsigned char)c);
        std::vector<unsigned char> codes;
        auto add = [&](const char *n) { if (auto c = x.code(n)) codes.push_back(c); };
        if (m == "ctrl" || m == "control") { add("Control_L"); add("Control_R"); }
        else if (m == "shift") { add("Shift_L"); add("Shift_R"); }
        else if (m == "alt") { add("Alt_L"); add("Alt_R"); add("ISO_Level3_Shift"); }
        else if (m == "super" || m == "meta" || m == "win") { add("Super_L"); add("Super_R"); }
        else return false;
        if (codes.empty()) return false;
        hk.mods.push_back(codes);
    }
    hk.key = x.code(key_alias(parts.back()).c_str());
    return hk.key != 0;
}

static inline bool bit(const char km[32], unsigned char c) { return km[c >> 3] & (1 << (c & 7)); }

// ---------------------------------------------------------------- X11 focus (xcb: errors are
// returned per request, so a vanished window can never trip the app's Xlib error handler)
struct Focus {
    void *lib = nullptr;
    xcb_connection_t *c = nullptr;
    xcb_window_t root = 0;
    xcb_atom_t a_active = 0, a_pid = 0;
    bool seen_self = false;
    decltype(&xcb_connect) connect = nullptr;
    decltype(&xcb_connection_has_error) has_error = nullptr;
    decltype(&xcb_get_setup) get_setup = nullptr;
    decltype(&xcb_setup_roots_iterator) roots = nullptr;
    decltype(&xcb_intern_atom) intern = nullptr;
    decltype(&xcb_intern_atom_reply) intern_reply = nullptr;
    decltype(&xcb_get_property) get_prop = nullptr;
    decltype(&xcb_get_property_reply) get_prop_reply = nullptr;
    decltype(&xcb_get_property_value) prop_value = nullptr;
    decltype(&xcb_get_property_value_length) prop_len = nullptr;
    decltype(&xcb_query_tree) query_tree = nullptr;
    decltype(&xcb_query_tree_reply) query_tree_reply = nullptr;

    bool open() {
        const char *d = getenv("DISPLAY");
        if (!d || !*d) return false;
        lib = dlopen("libxcb.so.1", RTLD_NOW | RTLD_LOCAL);
        if (!lib) return false;
#define L(v, n) v = (decltype(v))dlsym(lib, #n); if (!v) return false;
        L(connect, xcb_connect) L(has_error, xcb_connection_has_error) L(get_setup, xcb_get_setup)
        L(roots, xcb_setup_roots_iterator) L(intern, xcb_intern_atom) L(intern_reply, xcb_intern_atom_reply)
        L(get_prop, xcb_get_property) L(get_prop_reply, xcb_get_property_reply)
        L(prop_value, xcb_get_property_value) L(prop_len, xcb_get_property_value_length)
        L(query_tree, xcb_query_tree) L(query_tree_reply, xcb_query_tree_reply)
#undef L
        int scr = 0;
        c = connect(nullptr, &scr);
        if (!c || has_error(c)) return false;
        xcb_screen_iterator_t it = roots(get_setup(c));
        for (int i = 0; i < scr && it.rem > 1; i++) { it.data++; it.rem--; }
        root = it.data->root;
        a_active = atom("_NET_ACTIVE_WINDOW");
        a_pid = atom("_NET_WM_PID");
        return a_active != 0;
    }
    xcb_atom_t atom(const char *n) {
        xcb_intern_atom_reply_t *r = intern_reply(c, intern(c, 1, (uint16_t)strlen(n), n), nullptr);
        xcb_atom_t a = r ? r->atom : 0;
        free(r);
        return a;
    }
    // returns false if the property does not exist
    bool card32(xcb_window_t w, xcb_atom_t prop, uint32_t &out) {
        xcb_get_property_reply_t *r = get_prop_reply(c, get_prop(c, 0, w, prop, XCB_GET_PROPERTY_TYPE_ANY, 0, 1), nullptr);
        bool ok = r && r->type != 0 && prop_len(r) >= 4;
        if (ok) memcpy(&out, prop_value(r), 4);
        free(r);
        return ok;
    }
    // 1 focused, 0 not focused, -1 unknown (never throttle)
    int check(uint64_t game_win) {
        if (!c || has_error(c)) return -1;
        uint32_t active = 0;
        if (!card32(root, a_active, active)) return -1;   // WM does not publish focus
        bool mine = false;
        uint32_t pid = 0;
        if (active && a_pid && card32(active, a_pid, pid) && pid == (uint32_t)getpid()) mine = true;
        if (!mine && active && game_win) {
            xcb_window_t w = (xcb_window_t)game_win;
            for (int i = 0; i < 24 && w; i++) {
                if (w == active) { mine = true; break; }
                xcb_query_tree_reply_t *q = query_tree_reply(c, query_tree(c, w), nullptr);
                if (!q) break;
                xcb_window_t parent = q->parent, r = q->root;
                free(q);
                if (!parent || parent == r) break;
                w = parent;
            }
        }
        if (mine) { seen_self = true; return 1; }
        // only throttle once we have positively identified our own window as focused at least
        // once - otherwise an unidentifiable window (native Wayland, helper process) never is
        return seen_self ? 0 : -1;
    }
};

// ---------------------------------------------------------------- Core
Core &Core::get() {
    static Core *c = new Core(); // intentionally leaked: used by detached thread
    return *c;
}

static void pin_library() {
    // The aux thread lives for the whole process; make sure the loader can
    // never dlclose() us underneath it (vkDestroyInstance unloads layers).
    Dl_info info;
    if (dladdr((void *)&pin_library, &info) && info.dli_fname)
        dlopen(info.dli_fname, RTLD_NOW | RTLD_NOLOAD | RTLD_NODELETE);
}

Core::Core() {
    process_identity(key_, exe_);
    hk_[0] = "ctrl+shift+pgup";
    hk_[1] = "ctrl+shift+pgdn";
    hk_[2] = "ctrl+shift+end";
    hk_[3] = "ctrl+shift+home";
    preset_path_ = config_dir() + "/presets/" + key_ + ".conf";
    load_settings();
    load_preset(true);
    logf("key=%s exe=%s preset=%s enabled=%d fps=%.2f mode=%s", key_.c_str(), exe_.c_str(),
         has_preset_.load() ? "yes" : "no", (int)enabled_.load(), fps_.load(), mode_name(mode_.load()));
}

void Core::load_settings() {
    KV kv;
    read_kv(config_dir() + "/settings.conf", kv);
    static const char *names[4] = {"hotkey_up", "hotkey_down", "hotkey_toggle", "hotkey_mode"};
    for (int i = 0; i < 4; i++)
        if (auto v = kv_get(kv, names[i])) hk_[i] = *v;
    if (auto v = kv_get(kv, "hotkeys")) hotkeys_enabled_ = parse_bool(*v);
    if (auto v = kv_get(kv, "step")) step_ = std::max(0.1f, (float)atof(v->c_str()));
    // hotkey_values: comma-separated FPS list the up/down hotkeys cycle through (empty = fixed step)
    values_.clear();
    if (auto v = kv_get(kv, "hotkey_values")) {
        const char *p = v->c_str();
        while (*p) {
            char *end;
            float f = strtof(p, &end);
            if (end == p) { p++; continue; }
            if (f >= 1.f && f <= FPS_MAX) values_.push_back(f);
            p = end;
        }
        std::sort(values_.begin(), values_.end());
        values_.erase(std::unique(values_.begin(), values_.end(),
                                  [](float a, float b) { return std::fabs(a - b) < 1e-3f; }), values_.end());
    }
    if (auto v = kv_get(kv, "default_fps")) default_fps_ = std::max(1.f, (float)atof(v->c_str()));
    if (auto v = kv_get(kv, "osd")) osd_enabled_ = parse_bool(*v);
    if (auto v = kv_get(kv, "osd_seconds")) osd_seconds_ = (float)atof(v->c_str());
    if (auto v = kv_get(kv, "osd_always")) osd_always_ = parse_bool(*v);
    if (auto v = kv_get(kv, "default_enabled")) default_enabled_ = parse_bool(*v);
    if (auto v = kv_get(kv, "default_mode")) default_mode_ = mode_from_string(*v, MODE_SMOOTH);
    if (auto v = kv_get(kv, "osd_all_apps")) osd_all_ = parse_bool(*v);
    bg_fps_global_ = 0.f;
    if (auto v = kv_get(kv, "background_fps")) bg_fps_global_ = std::min(FPS_MAX, std::max(0.f, (float)atof(v->c_str())));
    bg_fps_ = bg_fps_preset_ >= 0.f ? bg_fps_preset_ : bg_fps_global_;
    display_hz_ = 0.f;
    if (auto v = kv_get(kv, "display_hz")) {
        float hz = (float)atof(v->c_str());
        if (hz >= 20.f && hz <= 2000.f) display_hz_ = hz;
    }
    // processes the layer must leave completely alone (desktop shells, browsers, ...)
    std::string ign = "plasmashell,kwin_wayland,kwin_x11,gnome-shell,xwayland,steamwebhelper,"
                      "chrome,chromium,firefox,electron,code,discord,obs,gamescope,pacer";
    if (auto v = kv_get(kv, "ignore")) ign = *v;
    if (const char *e = getenv("DISABLE_PACER")) if (*e && *e != '0') ign = "*";
    ignored_ = false;
    size_t st = 0;
    for (size_t i = 0; i <= ign.size(); i++) {
        if (i == ign.size() || ign[i] == ',') {
            std::string t = trim(ign.substr(st, i - st));
            if (t == "*" || (!t.empty() && t == key_)) ignored_ = true;
            st = i + 1;
        }
    }
}

bool Core::load_preset(bool initial) {
    KV kv;
    bool ok = read_kv(preset_path_, kv);
    file_sig(preset_path_, preset_mtime_, preset_size_);
    bool en, changed;
    uint32_t mode;
    float fps;
    int32_t pm = PM_KEEP;
    if (ok) {
        auto e = kv_get(kv, "enabled");
        en = e ? parse_bool(*e) : true;
        auto f = kv_get(kv, "fps");
        fps = f ? (float)atof(f->c_str()) : default_fps_;
        auto m = kv_get(kv, "mode");
        mode = m ? mode_from_string(*m, MODE_SMOOTH) : MODE_SMOOTH;
        if (auto p = kv_get(kv, "present_mode")) {
            if (*p == "immediate") pm = 0;
            else if (*p == "mailbox") pm = 1;
            else if (*p == "fifo") pm = 2;
            else if (*p == "fifo_relaxed") pm = 3;
        }
    } else {
        en = default_enabled_;
        fps = default_fps_;
        mode = default_mode_;
    }
    if (!(fps > 0.f)) fps = default_fps_;
    fps = std::min(fps, FPS_MAX);
    bg_fps_preset_ = -1.f;
    if (ok) {
        if (auto b = kv_get(kv, "background_fps")) {
            if (*b == "off" || *b == "0") bg_fps_preset_ = 0.f;
            else if (!b->empty() && *b != "global") bg_fps_preset_ = std::min(FPS_MAX, std::max(1.f, (float)atof(b->c_str())));
        }
    }
    bg_fps_ = bg_fps_preset_ >= 0.f ? bg_fps_preset_ : bg_fps_global_;
    changed = en != enabled_.load() || mode != mode_.load() || std::fabs(fps - fps_.load()) > 1e-3f;
    has_preset_ = ok;
    enabled_ = en;
    mode_ = mode;
    fps_ = fps;
    present_mode_ = pm;
    if (changed && !initial) {
        logf("preset reloaded: enabled=%d fps=%.2f mode=%s", (int)en, fps, mode_name(mode));
        show_osd();
    }
    return ok;
}

void Core::save_preset() {
    mkdirs(config_dir() + "/presets");
    KV kv;
    read_kv(preset_path_, kv);
    if (!kv_get(kv, "name")) kv_set(kv, "name", basename_any(exe_));
    kv_set(kv, "enabled", enabled_.load() ? "1" : "0");
    char b[32];
    snprintf(b, sizeof b, "%g", fps_.load());
    kv_set(kv, "fps", b);
    kv_set(kv, "mode", mode_name(mode_.load()));
    std::string tmp = preset_path_ + ".tmp." + std::to_string(getpid());
    FILE *f = fopen(tmp.c_str(), "w");
    if (!f) return;
    fprintf(f, "# pacer preset (edited in-game)\n");
    for (auto &p : kv) fprintf(f, "%s = %s\n", p.first.c_str(), p.second.c_str());
    fclose(f);
    rename(tmp.c_str(), preset_path_.c_str());
    file_sig(preset_path_, preset_mtime_, preset_size_);
    has_preset_ = true;
    logf("preset saved: %s", preset_path_.c_str());
}

void Core::show_osd() {
    osd_until_ = now_ns() + (int64_t)(osd_seconds_ * 1e9);
}

OsdState Core::osd_state() {
    OsdState s;
    s.visible = osd_enabled_ && (osd_always_ || now_ns() < osd_until_.load());
    s.enabled = enabled_;
    s.mode = mode_;
    s.fps = fps_;
    s.measured_fps = measured_fps_.load();
    return s;
}

void Core::apply_hotkey(int which) {
    float f = fps_;
    bool list = !values_.empty() && which < 2;
    switch (which) {
    case 0:
        if (list) {   // next preset above the current limit (stays at the top)
            f = values_.back();
            for (float v : values_) if (v > fps_ + 1e-3f) { f = v; break; }
        } else f = std::floor(f / step_ + 1e-3f) * step_ + step_;
        enabled_ = true;
        break;
    case 1:
        if (list) {   // next preset below (stays at the bottom)
            f = values_.front();
            for (auto it = values_.rbegin(); it != values_.rend(); ++it) if (*it < fps_ - 1e-3f) { f = *it; break; }
        } else f = std::ceil(f / step_ - 1e-3f) * step_ - step_;
        enabled_ = true;
        break;
    case 2: enabled_ = !enabled_; break;
    case 3: mode_ = (mode_ + 1) % MODE_COUNT; break;
    }
    fps_ = list ? f : std::min(FPS_MAX, std::max(step_ < 5 ? 5.f : step_, f));
    logf("hotkey %d -> enabled=%d fps=%.2f mode=%s", which, (int)enabled_.load(), fps_.load(),
         mode_name(mode_.load()));
    show_osd();
    save_at_ = now_ns() + 700000000LL; // debounce disk writes while a key is held
}

void Core::attach(uint32_t api) {
    if (ignored_ || attached_.load()) return;
    attached_ = true;
    pin_library();
    std::string run = state_dir() + "/run";
    mkdirs(run);
    shm_path_ = run + "/" + key_ + "." + std::to_string(getpid()) + ".shm";
    shm_fd_ = open(shm_path_.c_str(), O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (shm_fd_ >= 0 && ftruncate(shm_fd_, sizeof(pacer_shm)) == 0) {
        void *p = mmap(nullptr, sizeof(pacer_shm), PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd_, 0);
        if (p != MAP_FAILED) {
            shm_ = (pacer_shm *)p;
            memset(shm_, 0, sizeof *shm_);
            shm_->version = PACER_SHM_VERSION;
            shm_->pid = (uint32_t)getpid();
            shm_->api = api;
            snprintf(shm_->key, sizeof shm_->key, "%s", key_.c_str());
            snprintf(shm_->exe, sizeof shm_->exe, "%s", exe_.c_str());
            timespec ts;
            clock_gettime(CLOCK_REALTIME, &ts);
            shm_->start_ns = (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
            shm_->present_mode = 0xffffffffu;
            __atomic_store_n(&shm_->magic, PACER_SHM_MAGIC, __ATOMIC_RELEASE);
            flock(shm_fd_, LOCK_EX | LOCK_NB); // liveness marker for the GUI
        }
    }
    static std::string unlink_path = shm_path_;
    atexit([] { unlink(unlink_path.c_str()); });
    std::thread([this] { aux_loop(); }).detach();
    logf("attached api=%u shm=%s", api, shm_path_.c_str());
}

void Core::set_effective_present_mode(uint32_t pm, uint32_t w, uint32_t h) {
    if (!shm_) return;
    shm_->present_mode = pm;
    shm_->width = w;
    shm_->height = h;
}

void Core::aux_loop() {
    X11 x;
    Hotkey hk[4];
    bool have_x = false;
    int64_t last_poll = 0, settings_m = -1, settings_s = -1;
    std::string settings_path = config_dir() + "/settings.conf";
    file_sig(settings_path, settings_m, settings_s);
    auto rebind = [&] {
        if (!have_x) return;
        for (int i = 0; i < 4; i++)
            if (!parse_hotkey(x, hk_[i], hk[i])) logf("hotkey '%s' not usable", hk_[i].c_str());
    };
    if (hotkeys_enabled_ && x.open()) { have_x = true; rebind(); }
    Focus focus;
    bool focus_tried = false, focus_ok = false;
    int64_t last_focus = 0;
    logf("hotkeys: %s", have_x ? "X11 keymap polling" : "unavailable (no X11 display)");

    for (;;) {
        int64_t now = now_ns();
        if (have_x && hotkeys_enabled_) {
            char km[32];
            x.query(x.dpy, km);
            for (int i = 0; i < 4; i++) {
                Hotkey &h = hk[i];
                if (!h.key) continue;
                bool mods = true;
                for (auto &alts : h.mods) {
                    bool any = false;
                    for (auto c : alts) any |= bit(km, c);
                    mods &= any;
                }
                bool pressed = mods && bit(km, h.key);
                if (pressed && !h.down) {
                    apply_hotkey(i);
                    h.next_repeat = now + 400000000LL;
                } else if (pressed && i < 2 && now >= h.next_repeat) {
                    apply_hotkey(i);
                    h.next_repeat = now + 90000000LL;
                }
                h.down = pressed;
            }
        }
        int64_t sa = save_at_.load();
        if (sa && now >= sa) {
            save_at_ = 0;
            save_preset();
        }
        if (bg_wanted() && attached_.load() && now - last_focus > 250000000LL) {
            last_focus = now;
            if (!focus_tried) { focus_tried = true; focus_ok = focus.open(); logf("focus detection: %s", focus_ok ? "on" : "unavailable"); }
            bool bg = focus_ok && focus.check(window_.load(std::memory_order_relaxed)) == 0;
            if (bg != background_.load()) {
                background_ = bg;
                logf("%s -> limit %s", bg ? "background" : "foreground", bg ? "background fps" : "normal");
            }
        } else if (!bg_wanted() && background_.load()) {
            background_ = false;
        }
        if (now - last_poll > 250000000LL) {
            last_poll = now;
            int64_t m, s;
            file_sig(settings_path, m, s);
            if (m != settings_m || s != settings_s) {
                settings_m = m; settings_s = s;
                load_settings();
                rebind();
                load_preset(false);
            }
            file_sig(preset_path_, m, s);
            if ((m != preset_mtime_ || s != preset_size_) && !save_at_.load())
                load_preset(false);
        }
        usleep(have_x ? 12000 : 50000);
    }
}

// ---------------------------------------------------------------- pacing
void Core::wait_until(int64_t deadline) {
    static thread_local bool slack_set = false;
    if (!slack_set) {
        prctl(PR_SET_TIMERSLACK, 1UL, 0, 0, 0); // 1ns slack for this (present) thread
        slack_set = true;
    }
    int64_t now = now_ns();
    if (deadline - now > spin_ns_) {
        int64_t wake = deadline - spin_ns_;
        timespec ts{(time_t)(wake / 1000000000LL), (long)(wake % 1000000000LL)};
        while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, nullptr) == EINTR) {}
        int64_t after = now_ns();
        // adapt the spin window to the observed scheduler wake-up jitter
        if (after > deadline - 20000) spin_ns_ = std::min<int64_t>(spin_ns_ + 100000, 2000000);
        else spin_ns_ = std::max<int64_t>(spin_ns_ - 2000, 80000);
    }
    while (now_ns() < deadline) {
#if defined(__x86_64__) || defined(__i386__)
        __builtin_ia32_pause();
#endif
    }
}

// Frame interval for a limit. When the limit is (within 0.3 %) an integer fraction or multiple of
// the real display refresh rate, lock to that exactly: a "540 Hz" panel really runs at 540.189 Hz,
// and 90 fps against it would otherwise slowly drift through the refresh cycle and periodically
// show one frame for 5 or 7 refreshes instead of 6 (visible as a hitch every ~30 s).
int64_t Core::interval_for(float fps) const {
    double iv = 1e9 / (double)fps;
    float hz = display_hz_.load(std::memory_order_relaxed);
    if (hz > 0.f) {
        double r = (double)hz / fps;                 // refreshes per frame
        if (r >= 1.0) {
            double n = std::round(r);
            if (n >= 1 && std::fabs(r - n) / n < 0.003) iv = n * 1e9 / hz;
        } else {
            double n = std::round(1.0 / r);          // frames per refresh (fps above refresh)
            if (n >= 1 && std::fabs(1.0 / r - n) / n < 0.003) iv = 1e9 / ((double)hz * n);
        }
    }
    return std::max<int64_t>((int64_t)std::llround(iv), 1000);
}

// Long-run pacing statistics in the shared status block (version 2 fields).
void Core::stats_frame(int64_t ft, bool en, uint32_t mode, float fps, int64_t interval) {
    if (!en) { mode = 99; fps = 0.f; }
    if (en != st_en_ || mode != st_mode_ || std::fabs(fps - st_fps_) > 1e-3f) {
        st_en_ = en; st_mode_ = mode; st_fps_ = fps;
        shm_->stat_n = 0;
        shm_->stat_sum = shm_->stat_sumsq = shm_->stat_abs_dev = 0;
        shm_->stat_s_sum = shm_->stat_s_sumsq = 0;
        shm_->stat_s_n = 0;
        last_start_ = 0;
        shm_->stat_hitches = 0;
        shm_->stat_target_ms = (float)(interval / 1e6);
        __atomic_store_n(&shm_->stat_reset_ns, now_ns(), __ATOMIC_RELEASE);
        return;   // the first frame after a change is a transition, not representative
    }
    if (ft <= 0) return;
    double ms = ft / 1e6, tgt = interval / 1e6;
    if (ms > std::max(4.0 * (tgt > 0 ? tgt : ms), 250.0)) { shm_->stat_hitches++; return; }
    shm_->stat_sum += ms;
    shm_->stat_sumsq += ms * ms;
    shm_->stat_abs_dev += tgt > 0 ? std::fabs(ms - tgt) : 0.0;
    shm_->stat_n++;
}

// Called when we hand control back to the game (end of post_present): frame-start cadence.
void Core::stats_start(int64_t start) {
    int64_t prev = last_start_;
    last_start_ = start;
    if (!shm_ || !prev || start <= prev) return;
    double ms = (start - prev) / 1e6, tgt = shm_->stat_target_ms;
    if (ms > std::max(4.0 * (tgt > 0 ? tgt : ms), 250.0)) return;
    shm_->stat_s_sum += ms;
    shm_->stat_s_sumsq += ms * ms;
    shm_->stat_s_n++;
}

void Core::record_frame(int64_t t, int64_t waited) {
    int64_t ft = last_present_ ? t - last_present_ : 0;
    last_present_ = t;
    // measured fps over ~0.5s for the OSD
    if (ft > 0) {
        float inst = 1e9f / (float)ft;
        float m = measured_fps_.load();
        measured_fps_ = m <= 0 ? inst : m + (inst - m) * 0.08f;
    }
    if (!shm_) return;
    {
        bool en;
        uint32_t m;
        float fps;
        effective(en, m, fps);
        stats_frame(ft, en, m, fps, en && fps > 0 ? interval_for(fps) : 0);
    }
    uint32_t h = shm_->ring_head;
    shm_->frametime_ms[h % PACER_RING] = (float)(ft / 1e6);
    shm_->wait_ms[h % PACER_RING] = (float)(waited / 1e6);
    shm_->enabled = enabled_;
    shm_->mode = mode_;
    shm_->fps_target = fps_;
    shm_->has_preset = has_preset_;
    shm_->flags = background_.load(std::memory_order_relaxed) ? 1u : 0u;
    shm_->last_present_ns = t;
    shm_->frames++;
    shm_->heartbeat++;
    __atomic_store_n(&shm_->ring_head, (h + 1) % PACER_RING, __ATOMIC_RELEASE);
}

bool Core::bg_wanted() const {
    return bg_fps_.load() > 0.f && (has_preset_.load() || default_enabled_);
}

void Core::effective(bool &en, uint32_t &m, float &fps) const {
    en = enabled_.load(std::memory_order_relaxed);
    m = mode_.load(std::memory_order_relaxed);
    fps = fps_.load(std::memory_order_relaxed);
    if (background_.load(std::memory_order_relaxed)) {
        float b = bg_fps_.load(std::memory_order_relaxed);
        if (b > 0.f) {
            fps = en ? std::min(fps, b) : b;
            en = true;
            m = MODE_SMOOTH; // power saving: no reason to spin for latency
        }
    }
}

void Core::pre_present() {
    if (!attached_.load(std::memory_order_relaxed)) return;
    int64_t now = now_ns();
    bool en;
    uint32_t m;
    float fps;
    effective(en, m, fps);
    int64_t waited = pending_wait_;
    pending_wait_ = 0;
    if (!en || !(fps > 0)) {
        target_ = 0;
        last_mode_ = 99;
        record_frame(now, 0);
        return;
    }
    int64_t interval = interval_for(fps);
    if (m != last_mode_) { target_ = 0; work_est_ = 0; wake_ = 0; pre_dl_ = 0; pc_est_ = 0; last_mode_ = m; }

    if (m == MODE_SMOOTH || m == MODE_BALANCED) {
        if (m == MODE_BALANCED && wake_) {
            // learn how long the app needs from our wake-up to its next present
            int64_t work = now - wake_;
            if (work > work_est_) work_est_ += (work - work_est_) / 2;   // react fast to spikes
            else work_est_ += (work - work_est_) / 32;                 // decay slowly
            work_est_ = std::min(std::max<int64_t>(work_est_, 0), interval);
        }
        int64_t dl = target_;
        // late by more than 1/8 frame, first frame, or grid far ahead (fps raised): resync
        if (dl == 0 || now - dl > std::max<int64_t>(interval / 8, 250000) || dl - now > interval + interval / 2) dl = now;
        if (dl > now) {
            wait_until(dl);
            waited += now_ns() - now;
        }
        pre_dl_ = dl;
        target_ = dl + interval;
    }
    record_frame(now_ns(), waited);
}

void Core::post_present() {
    if (!attached_.load(std::memory_order_relaxed)) return;
    bool en;
    uint32_t m;
    float fps;
    effective(en, m, fps);
    if (!en || !(fps > 0) || m != last_mode_) { stats_start(now_ns()); return; }
    int64_t interval = interval_for(fps);
    int64_t now = now_ns();
    if (m == MODE_SMOOTH && pre_dl_) {
        // The present call itself (driver queueing, V-Sync back-pressure, DXVK's presenter) takes a
        // varying amount of time, so returning straight away would start the game's next frame -
        // its input / simulation timestamp - with that jitter even though presents are perfectly
        // spaced. Hold the frame start at a constant phase behind the grid point instead: the upper
        // envelope of the present cost. Costs above a quarter frame are left alone (no extra lag).
        int64_t cost = now - pre_dl_;
        if (cost >= 0 && cost < interval / 4) {
            if (cost > pc_est_) pc_est_ += (cost - pc_est_) / 2;         // rise fast
            else pc_est_ -= (pc_est_ - cost) / 64;                         // decay slowly
        }
        pc_est_ = std::min<int64_t>(pc_est_, interval / 4);
        int64_t hold = pre_dl_ + pc_est_;
        if (hold > now && hold - now < interval / 4) {
            wait_until(hold);
            pending_wait_ += now_ns() - now;
        }
        pre_dl_ = 0;
    } else if (m == MODE_LATENCY) {
        // pace the *start* of the next frame: the game polls input after we return
        int64_t dl = target_;
        if (dl == 0 || now - dl > std::max<int64_t>(interval / 8, 250000) || dl - now > interval + interval / 2) dl = now;
        if (dl > now) {
            wait_until(dl);
            pending_wait_ += now_ns() - now;
        }
        target_ = dl + interval;
    } else if (m == MODE_BALANCED) {
        // sleep now (input not yet sampled) and wake just early enough for the
        // app to finish its next frame right on the grid deadline target_
        int64_t margin = std::max<int64_t>(500000, work_est_ / 8);
        int64_t wake = target_ - work_est_ - margin;
        if (target_ && wake > now && wake - now < interval * 2) {
            wait_until(wake);
            pending_wait_ += now_ns() - now;
        }
        wake_ = now_ns();
    }
    stats_start(now_ns());
}

} // namespace pacer
