// Synthetic game loop to measure pacing quality and input latency per mode.
//   build/tests/pacing_test [fps] [seconds_per_mode]
// Uses PACER_CONFIG_DIR / PACER_STATE_DIR so it never touches the real config.
#include "core.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

using namespace pacer;

static void write_preset(const std::string &dir, const char *mode, double fps) {
    std::string p = dir + "/presets/pacing_test.conf";
    FILE *f = fopen((p + ".tmp").c_str(), "w");
    fprintf(f, "enabled = 1\nfps = %g\nmode = %s\n", fps, mode);
    fclose(f);
    rename((p + ".tmp").c_str(), p.c_str());
}

static void busy(int64_t ns) {
    int64_t end = now_ns() + ns;
    while (now_ns() < end) {}
}

int main(int argc, char **argv) {
    double fps = argc > 1 ? atof(argv[1]) : 120;
    double secs = argc > 2 ? atof(argv[2]) : 3;
    char cwd[1024];
    if (!getcwd(cwd, sizeof cwd)) return 1;
    std::string root = std::string(cwd) + "/build/tests/sandbox";
    std::string cfg = root + "/config", st = root + "/state";
    mkdir(root.c_str(), 0755); mkdir(cfg.c_str(), 0755); mkdir((cfg + "/presets").c_str(), 0755);
    mkdir(st.c_str(), 0755);
    setenv("PACER_CONFIG_DIR", cfg.c_str(), 1);
    setenv("PACER_STATE_DIR", st.c_str(), 1);
    setenv("PACER_KEY", "pacing_test", 1);
    write_preset(cfg, "smooth", fps);

    Core &c = core();
    c.attach(1);
    std::mt19937 rng(42);
    double ival = 1e3 / fps;
    // game CPU+GPU work: 35% of the frame budget with +-25% noise and occasional spikes
    std::normal_distribution<double> work(ival * 0.35, ival * 0.06);

    const char *modes[] = {"smooth", "latency", "balanced"};
    printf("target %.1f fps (%.3f ms), %.1fs per mode\n", fps, ival, secs);
    printf("%-9s %8s %8s %8s %8s %8s %10s\n", "mode", "avg_fps", "ft_sd", "ft_p99", "ft_max", "jitter",
           "in->pres");
    int bad = 0;
    for (auto m : modes) {
        write_preset(cfg, m, fps);
        usleep(900000); // aux thread reload (400ms poll)
        std::vector<double> ft, lat;
        int64_t t_end = now_ns() + (int64_t)(secs * 1e9), last = 0;
        int64_t frame_start = now_ns();
        while (now_ns() < t_end) {
            // "input sampled" at frame_start, then the game works
            double w = std::max(0.2, work(rng));
            if (rng() % 200 == 0) w *= 1.8; // rare spike
            busy((int64_t)(w * 1e6));
            int64_t before = now_ns();
            c.pre_present();
            int64_t pres = now_ns(); // the moment the frame is handed to the display
            (void)before;
            lat.push_back((pres - frame_start) / 1e6);
            if (last) ft.push_back((pres - last) / 1e6);
            last = pres;
            busy(150000); // driver present cost
            c.post_present();
            frame_start = now_ns();
        }
        double sum = 0, sq = 0, mx = 0, jit = 0;
        for (size_t i = 0; i < ft.size(); i++) {
            sum += ft[i]; sq += ft[i] * ft[i]; mx = std::max(mx, ft[i]);
            if (i) jit += fabs(ft[i] - ft[i - 1]);
        }
        double mean = sum / ft.size(), sd = sqrt(sq / ft.size() - mean * mean);
        std::vector<double> s = ft;
        std::sort(s.begin(), s.end());
        double p99 = s[(size_t)(s.size() * 0.99)];
        double lsum = 0;
        for (double l : lat) lsum += l;
        printf("%-9s %8.2f %8.3f %8.3f %8.3f %8.3f %8.3fms\n", m, 1e3 / mean, sd, p99, mx, jit / (ft.size() - 1),
               lsum / lat.size());
        // loose bound: CI runners are noisy, this only catches a broken limiter
        if (fabs(1e3 / mean - fps) > fps * 0.03) { printf("  FAIL: %s average off target\n", m); bad = 1; }
    }
    return bad;
}
