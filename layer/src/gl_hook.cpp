// pacer OpenGL hook (LD_PRELOAD). Covers native GLX / EGL games.
// Vulkan, DXVK and VKD3D titles are handled by the Vulkan layer instead.
#include "core.h"

#include <dlfcn.h>
#include <string.h>

#define EXPORT extern "C" __attribute__((visibility("default")))

using namespace pacer;

typedef void (*PFN_glXSwapBuffers)(void *, unsigned long);
typedef unsigned (*PFN_eglSwapBuffers)(void *, void *);
typedef unsigned (*PFN_eglSwapBuffersWithDamage)(void *, void *, const int *, int);
typedef void *(*PFN_getproc)(const char *);
typedef void *(*PFN_dlsym)(void *, const char *);

static PFN_dlsym real_dlsym() {
    static PFN_dlsym f = (PFN_dlsym)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.34");
    if (!f) f = (PFN_dlsym)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.2.5");
    if (!f) f = (PFN_dlsym)dlvsym(RTLD_NEXT, "dlsym", "GLIBC_2.0");
    return f;
}

static void *find_real(const char *name) {
    void *p = real_dlsym()(RTLD_NEXT, name);
    if (p) return p;
    static const char *libs[] = {"libGLX.so.0", "libGL.so.1", "libEGL.so.1"};
    for (auto l : libs)
        if (void *h = dlopen(l, RTLD_LAZY | RTLD_NOLOAD))
            if ((p = real_dlsym()(h, name))) return p;
    for (auto l : libs)
        if (void *h = dlopen(l, RTLD_LAZY))
            if ((p = real_dlsym()(h, name))) return p;
    return nullptr;
}

static PFN_glXSwapBuffers r_glXSwapBuffers;
static PFN_eglSwapBuffers r_eglSwapBuffers;
static PFN_eglSwapBuffersWithDamage r_eglSwapBuffersWithDamageKHR, r_eglSwapBuffersWithDamageEXT;
static PFN_getproc r_glXGetProcAddress, r_glXGetProcAddressARB, r_eglGetProcAddress;

static inline bool gl_disabled() {
    static int d = -1;
    if (d < 0) { const char *e = getenv("DISABLE_PACER"); d = e && *e && *e != '0'; }
    return d;
}

EXPORT void glXSwapBuffers(void *dpy, unsigned long drawable) {
    if (!r_glXSwapBuffers) r_glXSwapBuffers = (PFN_glXSwapBuffers)find_real("glXSwapBuffers");
    if (gl_disabled()) { r_glXSwapBuffers(dpy, drawable); return; }
    Core &c = core();
    c.attach(2);
    c.set_window(drawable);
    c.pre_present();
    r_glXSwapBuffers(dpy, drawable);
    c.post_present();
}

EXPORT unsigned eglSwapBuffers(void *dpy, void *surf) {
    if (!r_eglSwapBuffers) r_eglSwapBuffers = (PFN_eglSwapBuffers)find_real("eglSwapBuffers");
    if (gl_disabled()) return r_eglSwapBuffers(dpy, surf);
    Core &c = core();
    c.attach(3);
    c.pre_present();
    unsigned r = r_eglSwapBuffers(dpy, surf);
    c.post_present();
    return r;
}

EXPORT unsigned eglSwapBuffersWithDamageKHR(void *dpy, void *surf, const int *rects, int n) {
    if (!r_eglSwapBuffersWithDamageKHR)
        r_eglSwapBuffersWithDamageKHR = (PFN_eglSwapBuffersWithDamage)find_real("eglSwapBuffersWithDamageKHR");
    if (gl_disabled()) return r_eglSwapBuffersWithDamageKHR(dpy, surf, rects, n);
    Core &c = core();
    c.attach(3);
    c.pre_present();
    unsigned r = r_eglSwapBuffersWithDamageKHR(dpy, surf, rects, n);
    c.post_present();
    return r;
}

EXPORT unsigned eglSwapBuffersWithDamageEXT(void *dpy, void *surf, const int *rects, int n) {
    if (!r_eglSwapBuffersWithDamageEXT)
        r_eglSwapBuffersWithDamageEXT = (PFN_eglSwapBuffersWithDamage)find_real("eglSwapBuffersWithDamageEXT");
    if (gl_disabled()) return r_eglSwapBuffersWithDamageEXT(dpy, surf, rects, n);
    Core &c = core();
    c.attach(3);
    c.pre_present();
    unsigned r = r_eglSwapBuffersWithDamageEXT(dpy, surf, rects, n);
    c.post_present();
    return r;
}

static void *hooked(const char *name) {
    if (!name) return nullptr;
    if (!strcmp(name, "glXSwapBuffers")) return (void *)glXSwapBuffers;
    if (!strcmp(name, "eglSwapBuffers")) return (void *)eglSwapBuffers;
    if (!strcmp(name, "eglSwapBuffersWithDamageKHR")) return (void *)eglSwapBuffersWithDamageKHR;
    if (!strcmp(name, "eglSwapBuffersWithDamageEXT")) return (void *)eglSwapBuffersWithDamageEXT;
    return nullptr;
}

EXPORT void *glXGetProcAddress(const char *n) {
    if (!r_glXGetProcAddress) r_glXGetProcAddress = (PFN_getproc)find_real("glXGetProcAddress");
    if (void *h = hooked(n)) { if (!r_glXSwapBuffers) r_glXSwapBuffers = (PFN_glXSwapBuffers)r_glXGetProcAddress(n); return h; }
    return r_glXGetProcAddress ? r_glXGetProcAddress(n) : nullptr;
}

EXPORT void *glXGetProcAddressARB(const char *n) {
    if (!r_glXGetProcAddressARB) r_glXGetProcAddressARB = (PFN_getproc)find_real("glXGetProcAddressARB");
    if (void *h = hooked(n)) return h;
    return r_glXGetProcAddressARB ? r_glXGetProcAddressARB(n) : nullptr;
}

EXPORT void *eglGetProcAddress(const char *n) {
    if (!r_eglGetProcAddress) r_eglGetProcAddress = (PFN_getproc)find_real("eglGetProcAddress");
    if (void *h = hooked(n)) return h;
    return r_eglGetProcAddress ? r_eglGetProcAddress(n) : nullptr;
}

// SDL & friends dlopen libGL and dlsym() the swap function from that handle,
// bypassing symbol interposition. Redirect those lookups to our hooks.
EXPORT void *dlsym(void *handle, const char *name) {
    PFN_dlsym rd = real_dlsym();
    if (void *h = hooked(name)) {
        if (handle != RTLD_NEXT && handle != RTLD_DEFAULT) {
            void *real = rd(handle, name);
            if (!real) return nullptr;
            // remember the real implementation from the handle the app used
            if (!strcmp(name, "glXSwapBuffers") && !r_glXSwapBuffers) r_glXSwapBuffers = (PFN_glXSwapBuffers)real;
            else if (!strcmp(name, "eglSwapBuffers") && !r_eglSwapBuffers) r_eglSwapBuffers = (PFN_eglSwapBuffers)real;
        }
        return h;
    }
    if ((!strcmp(name, "glXGetProcAddress") || !strcmp(name, "glXGetProcAddressARB") ||
                 !strcmp(name, "eglGetProcAddress"))) {
        void *real = rd(handle, name);
        if (!real) return nullptr;
        if (name[0] == 'e') { if (!r_eglGetProcAddress) r_eglGetProcAddress = (PFN_getproc)real; return (void *)eglGetProcAddress; }
        if (!strcmp(name, "glXGetProcAddressARB")) { if (!r_glXGetProcAddressARB) r_glXGetProcAddressARB = (PFN_getproc)real; return (void *)glXGetProcAddressARB; }
        if (!r_glXGetProcAddress) r_glXGetProcAddress = (PFN_getproc)real;
        return (void *)glXGetProcAddress;
    }
    return rd(handle, name);
}
