// pacer Vulkan implicit layer.
// Hooks vkQueuePresentKHR for frame limiting and draws a small OSD into the
// swapchain image (transfer copy) when the limit is changed in-game.
#include "core.h"
#include "osd.h"

#include <vulkan/vk_layer.h>
#include <vulkan/vulkan.h>

#include <cmath>
#include <cstring>
#include <mutex>
#include <unordered_map>
#include <vector>

#define EXPORT extern "C" __attribute__((visibility("default")))

using namespace pacer;

template <typename T> static void *dkey(T h) { return *(void **)h; }

struct InstData {
    VkInstance inst;
    PFN_vkGetInstanceProcAddr gipa;
    PFN_vkDestroyInstance DestroyInstance;
    PFN_vkGetPhysicalDeviceMemoryProperties GetMemProps;
    PFN_vkGetPhysicalDeviceQueueFamilyProperties GetQFProps;
    PFN_vkGetPhysicalDeviceSurfacePresentModesKHR GetPresentModes;
    PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR GetSurfCaps;
    PFN_vkVoidFunction CreateXlibSurface, CreateXcbSurface;
};

// VK_KHR_xlib_surface / VK_KHR_xcb_surface create-info layouts (declared here to avoid
// pulling Xlib.h / xcb.h into this translation unit)
struct XlibSurfaceCI { VkStructureType sType; const void *pNext; VkFlags flags; void *dpy; unsigned long window; };
struct XcbSurfaceCI { VkStructureType sType; const void *pNext; VkFlags flags; void *conn; uint32_t window; };
typedef VkResult(VKAPI_PTR *PFN_CreateSurface)(VkInstance, const void *, const VkAllocationCallbacks *, VkSurfaceKHR *);

struct SwapImg {
    VkImage image = VK_NULL_HANDLE;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    VkSemaphore sem = VK_NULL_HANDLE;
    VkBuffer buf = VK_NULL_HANDLE;
    VkDeviceMemory mem = VK_NULL_HANDLE;
    void *map = nullptr;
    bool submitted = false;
};

struct Swap {
    VkDevice dev;
    VkFormat format;
    VkExtent2D extent;
    bool osd_ok = false;
    uint32_t qfam = UINT32_MAX;
    VkCommandPool pool = VK_NULL_HANDLE;
    std::vector<SwapImg> imgs;
    int scale = 1, box_w = 0, box_h = 0;
    uint32_t bpp = 4;
};

struct DevData {
    VkDevice dev;
    VkPhysicalDevice phys;
    InstData *inst;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkSetDeviceLoaderData setLoaderData;
    VkPhysicalDeviceMemoryProperties memprops;
    std::vector<VkQueueFamilyProperties> qfams;
    std::unordered_map<VkQueue, uint32_t> queue_fam;
#define DF(n) PFN_vk##n n = nullptr;
    DF(DestroyDevice) DF(GetDeviceQueue) DF(GetDeviceQueue2) DF(CreateSwapchainKHR)
    DF(DestroySwapchainKHR) DF(GetSwapchainImagesKHR) DF(QueuePresentKHR) DF(QueueSubmit)
    DF(CreateCommandPool) DF(DestroyCommandPool) DF(AllocateCommandBuffers) DF(BeginCommandBuffer)
    DF(EndCommandBuffer) DF(ResetCommandBuffer) DF(CmdPipelineBarrier) DF(CmdCopyBufferToImage)
    DF(CreateFence) DF(DestroyFence) DF(WaitForFences) DF(ResetFences) DF(CreateSemaphore)
    DF(DestroySemaphore) DF(CreateBuffer) DF(DestroyBuffer) DF(GetBufferMemoryRequirements)
    DF(AllocateMemory) DF(FreeMemory) DF(BindBufferMemory) DF(MapMemory) DF(UnmapMemory)
#undef DF
};

static std::mutex g_lock;
static std::unordered_map<void *, InstData *> g_inst;       // key: instance/physdev dispatch
static std::unordered_map<void *, DevData *> g_dev;         // key: device/queue dispatch
static std::unordered_map<VkSwapchainKHR, Swap *> g_swap;

static InstData *inst_of(void *k) {
    std::lock_guard<std::mutex> l(g_lock);
    auto it = g_inst.find(k);
    return it == g_inst.end() ? nullptr : it->second;
}
static DevData *dev_of(void *k) {
    std::lock_guard<std::mutex> l(g_lock);
    auto it = g_dev.find(k);
    return it == g_dev.end() ? nullptr : it->second;
}

// ------------------------------------------------------------------ OSD helpers
static bool fmt_info(VkFormat f, uint32_t &bpp) {
    switch (f) {
    case VK_FORMAT_R8G8B8A8_UNORM: case VK_FORMAT_R8G8B8A8_SRGB:
    case VK_FORMAT_B8G8R8A8_UNORM: case VK_FORMAT_B8G8R8A8_SRGB:
    case VK_FORMAT_A2B10G10R10_UNORM_PACK32: case VK_FORMAT_A2R10G10B10_UNORM_PACK32:
        bpp = 4; return true;
    case VK_FORMAT_R16G16B16A16_SFLOAT: bpp = 8; return true;
    default: return false;
    }
}

static uint16_t to_half(float f) {
    uint32_t x;
    memcpy(&x, &f, 4);
    uint32_t sign = (x >> 16) & 0x8000, e = (x >> 23) & 0xff, m = x & 0x7fffff;
    if (e < 113) return (uint16_t)sign; // flush tiny to 0
    if (e > 142) return (uint16_t)(sign | 0x7c00);
    return (uint16_t)(sign | ((e - 112) << 10) | (m >> 13));
}

static void convert(const Canvas &cv, VkFormat f, uint8_t *dst) {
    size_t n = (size_t)cv.w * cv.h;
    for (size_t i = 0; i < n; i++) {
        uint32_t c = cv.px[i];
        uint32_t r = c & 0xff, g = (c >> 8) & 0xff, b = (c >> 16) & 0xff;
        switch (f) {
        case VK_FORMAT_R8G8B8A8_UNORM: case VK_FORMAT_R8G8B8A8_SRGB: {
            uint32_t v = r | g << 8 | b << 16 | 0xffu << 24; memcpy(dst + i * 4, &v, 4); break; }
        case VK_FORMAT_B8G8R8A8_UNORM: case VK_FORMAT_B8G8R8A8_SRGB: {
            uint32_t v = b | g << 8 | r << 16 | 0xffu << 24; memcpy(dst + i * 4, &v, 4); break; }
        case VK_FORMAT_A2B10G10R10_UNORM_PACK32: {
            uint32_t v = (r * 1023 / 255) | (g * 1023 / 255) << 10 | (b * 1023 / 255) << 20 | 3u << 30;
            memcpy(dst + i * 4, &v, 4); break; }
        case VK_FORMAT_A2R10G10B10_UNORM_PACK32: {
            uint32_t v = (b * 1023 / 255) | (g * 1023 / 255) << 10 | (r * 1023 / 255) << 20 | 3u << 30;
            memcpy(dst + i * 4, &v, 4); break; }
        case VK_FORMAT_R16G16B16A16_SFLOAT: {
            uint16_t h[4] = {to_half(powf(r / 255.f, 2.2f)), to_half(powf(g / 255.f, 2.2f)),
                             to_half(powf(b / 255.f, 2.2f)), to_half(1.f)};
            memcpy(dst + i * 8, h, 8); break; }
        default: break;
        }
    }
}

static int32_t find_memtype(DevData *d, uint32_t bits, VkMemoryPropertyFlags want) {
    for (uint32_t i = 0; i < d->memprops.memoryTypeCount; i++)
        if ((bits & (1u << i)) && (d->memprops.memoryTypes[i].propertyFlags & want) == want) return (int32_t)i;
    return -1;
}

static void destroy_swap(DevData *d, Swap *s) {
    for (auto &im : s->imgs) {
        if (im.fence) {
            if (im.submitted) d->WaitForFences(d->dev, 1, &im.fence, VK_TRUE, 1000000000ull);
            d->DestroyFence(d->dev, im.fence, nullptr);
        }
        if (im.sem) d->DestroySemaphore(d->dev, im.sem, nullptr);
        if (im.map) d->UnmapMemory(d->dev, im.mem);
        if (im.buf) d->DestroyBuffer(d->dev, im.buf, nullptr);
        if (im.mem) d->FreeMemory(d->dev, im.mem, nullptr);
    }
    if (s->pool) d->DestroyCommandPool(d->dev, s->pool, nullptr); // frees command buffers
    delete s;
}

static bool init_img(DevData *d, Swap *s, SwapImg &im) {
    if (!s->pool) {
        VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
        pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
        pci.queueFamilyIndex = s->qfam;
        if (d->CreateCommandPool(d->dev, &pci, nullptr, &s->pool) != VK_SUCCESS) return false;
    }
    VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    ai.commandPool = s->pool;
    ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    ai.commandBufferCount = 1;
    if (d->AllocateCommandBuffers(d->dev, &ai, &im.cmd) != VK_SUCCESS) return false;
    if (d->setLoaderData(d->dev, im.cmd) != VK_SUCCESS) return false;
    VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    if (d->CreateFence(d->dev, &fci, nullptr, &im.fence) != VK_SUCCESS) return false;
    VkSemaphoreCreateInfo sci{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
    if (d->CreateSemaphore(d->dev, &sci, nullptr, &im.sem) != VK_SUCCESS) return false;
    VkBufferCreateInfo bci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bci.size = (VkDeviceSize)s->box_w * s->box_h * s->bpp;
    bci.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (d->CreateBuffer(d->dev, &bci, nullptr, &im.buf) != VK_SUCCESS) return false;
    VkMemoryRequirements mr;
    d->GetBufferMemoryRequirements(d->dev, im.buf, &mr);
    int32_t mt = find_memtype(d, mr.memoryTypeBits,
                              VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    if (mt < 0) return false;
    VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    mai.allocationSize = mr.size;
    mai.memoryTypeIndex = (uint32_t)mt;
    if (d->AllocateMemory(d->dev, &mai, nullptr, &im.mem) != VK_SUCCESS) return false;
    if (d->BindBufferMemory(d->dev, im.buf, im.mem, 0) != VK_SUCCESS) return false;
    if (d->MapMemory(d->dev, im.mem, 0, VK_WHOLE_SIZE, 0, &im.map) != VK_SUCCESS) return false;
    return true;
}

// Records + submits the OSD copy for one image. On success, *out_sem must be
// waited on by the present instead of the app's semaphores.
static bool draw_osd(DevData *d, VkQueue q, Swap *s, uint32_t idx, const VkPresentInfoKHR *pi,
                     VkSemaphore *out_sem) {
    if (!s->osd_ok || idx >= s->imgs.size()) return false;
    auto qf = d->queue_fam.find(q);
    if (qf == d->queue_fam.end()) return false;
    if (s->qfam == UINT32_MAX) {
        if (qf->second >= d->qfams.size() ||
            !(d->qfams[qf->second].queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT | VK_QUEUE_TRANSFER_BIT))) {
            s->osd_ok = false;
            return false;
        }
        s->qfam = qf->second;
    }
    if (qf->second != s->qfam) return false;
    SwapImg &im = s->imgs[idx];
    if (!im.cmd && !init_img(d, s, im)) { s->osd_ok = false; return false; }
    if (im.submitted) {
        d->WaitForFences(d->dev, 1, &im.fence, VK_TRUE, UINT64_MAX);
        d->ResetFences(d->dev, 1, &im.fence);
        im.submitted = false;
    }

    OsdState st = core().osd_state();
    char l0[32], l1[32];
    if (st.enabled && std::fabs(st.fps - std::round(st.fps)) < 0.005f) snprintf(l0, sizeof l0, "LIMIT %.0f FPS", st.fps);
    else if (st.enabled) snprintf(l0, sizeof l0, "LIMIT %.2f FPS", st.fps);
    else snprintf(l0, sizeof l0, "LIMIT OFF");
    snprintf(l1, sizeof l1, "%s %.0f FPS", mode_name(st.mode), st.measured_fps);
    std::string lines[2] = {l0, l1};
    uint32_t colors[2] = {st.enabled ? 0xFF7CD42Bu : 0xFF8080FFu, 0xFFD0D0D0u};
    Canvas cv;
    osd_render(cv, s->scale, lines, colors);
    convert(cv, s->format, (uint8_t *)im.map);

    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    d->ResetCommandBuffer(im.cmd, 0);
    d->BeginCommandBuffer(im.cmd, &bi);
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = 0;
    b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.oldLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = im.image;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    d->CmdPipelineBarrier(im.cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                          nullptr, 0, nullptr, 1, &b);
    uint32_t cw = std::min<uint32_t>(s->box_w, s->extent.width > 16 ? s->extent.width - 16 : 0);
    uint32_t ch = std::min<uint32_t>(s->box_h, s->extent.height > 16 ? s->extent.height - 16 : 0);
    if (cw && ch) {
        VkBufferImageCopy c{};
        c.bufferRowLength = (uint32_t)s->box_w;
        c.bufferImageHeight = (uint32_t)s->box_h;
        c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        int32_t m = 8 * s->scale;
        c.imageOffset = {m, m, 0};
        c.imageExtent = {std::min<uint32_t>(cw, s->extent.width - m), std::min<uint32_t>(ch, s->extent.height - m), 1};
        d->CmdCopyBufferToImage(im.cmd, im.buf, im.image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &c);
    }
    b.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.dstAccessMask = 0;
    b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    b.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    d->CmdPipelineBarrier(im.cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0,
                          nullptr, 0, nullptr, 1, &b);
    d->EndCommandBuffer(im.cmd);

    std::vector<VkPipelineStageFlags> stages(pi->waitSemaphoreCount, VK_PIPELINE_STAGE_TRANSFER_BIT);
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.waitSemaphoreCount = pi->waitSemaphoreCount;
    si.pWaitSemaphores = pi->pWaitSemaphores;
    si.pWaitDstStageMask = stages.data();
    si.commandBufferCount = 1;
    si.pCommandBuffers = &im.cmd;
    si.signalSemaphoreCount = 1;
    si.pSignalSemaphores = &im.sem;
    if (d->QueueSubmit(q, 1, &si, im.fence) != VK_SUCCESS) { s->osd_ok = false; return false; }
    im.submitted = true;
    *out_sem = im.sem;
    return true;
}

// ------------------------------------------------------------------ device hooks
static VKAPI_ATTR VkResult VKAPI_CALL pacer_QueuePresentKHR(VkQueue q, const VkPresentInfoKHR *pi) {
    DevData *d = dev_of(dkey(q));
    Core &c = core();
    c.pre_present();
    VkPresentInfoKHR copy = *pi;
    VkSemaphore osd_sem;
    if (c.osd_enabled() && !c.ignored() && pi->swapchainCount == 1 && c.osd_state().visible) {
        Swap *s = nullptr;
        {
            std::lock_guard<std::mutex> l(g_lock);
            auto it = g_swap.find(pi->pSwapchains[0]);
            if (it != g_swap.end()) s = it->second;
        }
        if (s && draw_osd(d, q, s, pi->pImageIndices[0], pi, &osd_sem)) {
            copy.waitSemaphoreCount = 1;
            copy.pWaitSemaphores = &osd_sem;
        }
    }
    VkResult r = d->QueuePresentKHR(q, &copy);
    c.post_present();
    return r;
}

static void note_queue(DevData *d, VkQueue q, uint32_t fam) {
    if (!q) return;
    std::lock_guard<std::mutex> l(g_lock);
    d->queue_fam[q] = fam;
}

static VKAPI_ATTR void VKAPI_CALL pacer_GetDeviceQueue(VkDevice dev, uint32_t fam, uint32_t idx, VkQueue *q) {
    DevData *d = dev_of(dkey(dev));
    d->GetDeviceQueue(dev, fam, idx, q);
    note_queue(d, *q, fam);
}

static VKAPI_ATTR void VKAPI_CALL pacer_GetDeviceQueue2(VkDevice dev, const VkDeviceQueueInfo2 *info, VkQueue *q) {
    DevData *d = dev_of(dkey(dev));
    d->GetDeviceQueue2(dev, info, q);
    note_queue(d, *q, info->queueFamilyIndex);
}

static VKAPI_ATTR VkResult VKAPI_CALL pacer_CreateSwapchainKHR(VkDevice dev, const VkSwapchainCreateInfoKHR *ci,
                                                             const VkAllocationCallbacks *alloc, VkSwapchainKHR *out) {
    DevData *d = dev_of(dkey(dev));
    Core &c = core();
    c.attach(1);
    VkSwapchainCreateInfoKHR info = *ci;

    int32_t pm = c.present_mode_override();
    if (pm >= 0 && d->inst->GetPresentModes) {
        uint32_t n = 0;
        d->inst->GetPresentModes(d->phys, ci->surface, &n, nullptr);
        std::vector<VkPresentModeKHR> modes(n);
        d->inst->GetPresentModes(d->phys, ci->surface, &n, modes.data());
        for (auto m : modes)
            if ((int32_t)m == pm) info.presentMode = m;
    }

    uint32_t bpp = 4;
    bool osd = c.wants_osd() && fmt_info(ci->imageFormat, bpp) && ci->imageArrayLayers == 1;
    if (osd && d->inst->GetSurfCaps) {
        VkSurfaceCapabilitiesKHR caps{};
        if (d->inst->GetSurfCaps(d->phys, ci->surface, &caps) != VK_SUCCESS ||
            !(caps.supportedUsageFlags & VK_IMAGE_USAGE_TRANSFER_DST_BIT))
            osd = false;
    }
    if (osd) info.imageUsage |= VK_IMAGE_USAGE_TRANSFER_DST_BIT;

    VkResult r = d->CreateSwapchainKHR(dev, &info, alloc, out);
    if (r != VK_SUCCESS && osd) { // be conservative: retry exactly as the app asked
        info.imageUsage = ci->imageUsage;
        osd = false;
        r = d->CreateSwapchainKHR(dev, &info, alloc, out);
    }
    if (r != VK_SUCCESS) return r;

    Swap *s = new Swap{dev, ci->imageFormat, ci->imageExtent};
    s->bpp = bpp;
    s->osd_ok = osd;
    osd_dims(ci->imageExtent.height, s->scale, s->box_w, s->box_h);
    if (osd) {
        uint32_t n = 0;
        d->GetSwapchainImagesKHR(dev, *out, &n, nullptr);
        std::vector<VkImage> imgs(n);
        d->GetSwapchainImagesKHR(dev, *out, &n, imgs.data());
        s->imgs.resize(n);
        for (uint32_t i = 0; i < n; i++) s->imgs[i].image = imgs[i];
    }
    c.set_effective_present_mode((uint32_t)info.presentMode, ci->imageExtent.width, ci->imageExtent.height);
    std::lock_guard<std::mutex> l(g_lock);
    g_swap[*out] = s;
    return r;
}

static VKAPI_ATTR void VKAPI_CALL pacer_DestroySwapchainKHR(VkDevice dev, VkSwapchainKHR sc,
                                                          const VkAllocationCallbacks *alloc) {
    DevData *d = dev_of(dkey(dev));
    Swap *s = nullptr;
    {
        std::lock_guard<std::mutex> l(g_lock);
        auto it = g_swap.find(sc);
        if (it != g_swap.end()) { s = it->second; g_swap.erase(it); }
    }
    if (s) destroy_swap(d, s);
    d->DestroySwapchainKHR(dev, sc, alloc);
}

static VKAPI_ATTR void VKAPI_CALL pacer_DestroyDevice(VkDevice dev, const VkAllocationCallbacks *alloc) {
    DevData *d = dev_of(dkey(dev));
    if (!d) return;
    std::vector<Swap *> mine;
    {
        std::lock_guard<std::mutex> l(g_lock);
        for (auto it = g_swap.begin(); it != g_swap.end();) {
            if (it->second->dev == dev) { mine.push_back(it->second); it = g_swap.erase(it); }
            else ++it;
        }
        g_dev.erase(dkey(dev));
    }
    for (auto s : mine) destroy_swap(d, s);
    d->DestroyDevice(dev, alloc);
    delete d;
}

static PFN_vkVoidFunction dev_hook(const char *n);

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL pacer_GetDeviceProcAddr(VkDevice dev, const char *n) {
    if (PFN_vkVoidFunction f = dev_hook(n)) return f;
    DevData *d = dev_of(dkey(dev));
    return d ? d->gdpa(dev, n) : nullptr;
}

static VKAPI_ATTR VkResult VKAPI_CALL pacer_CreateDevice(VkPhysicalDevice phys, const VkDeviceCreateInfo *ci,
                                                       const VkAllocationCallbacks *alloc, VkDevice *out) {
    InstData *id = inst_of(dkey(phys));
    auto *link = (VkLayerDeviceCreateInfo *)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerDeviceCreateInfo *)link->pNext;
    if (!link || !id) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    auto create = (PFN_vkCreateDevice)gipa(id->inst, "vkCreateDevice");
    VkResult r = create(phys, ci, alloc, out);
    if (r != VK_SUCCESS) return r;

    auto *ld = (VkLayerDeviceCreateInfo *)ci->pNext;
    while (ld && !(ld->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && ld->function == VK_LOADER_DATA_CALLBACK))
        ld = (VkLayerDeviceCreateInfo *)ld->pNext;

    DevData *d = new DevData();
    d->dev = *out;
    d->phys = phys;
    d->inst = id;
    d->gdpa = gdpa;
    d->setLoaderData = ld ? ld->u.pfnSetDeviceLoaderData : nullptr;
#define DF(n) d->n = (PFN_vk##n)gdpa(*out, "vk" #n);
    DF(DestroyDevice) DF(GetDeviceQueue) DF(GetDeviceQueue2) DF(CreateSwapchainKHR)
    DF(DestroySwapchainKHR) DF(GetSwapchainImagesKHR) DF(QueuePresentKHR) DF(QueueSubmit)
    DF(CreateCommandPool) DF(DestroyCommandPool) DF(AllocateCommandBuffers) DF(BeginCommandBuffer)
    DF(EndCommandBuffer) DF(ResetCommandBuffer) DF(CmdPipelineBarrier) DF(CmdCopyBufferToImage)
    DF(CreateFence) DF(DestroyFence) DF(WaitForFences) DF(ResetFences) DF(CreateSemaphore)
    DF(DestroySemaphore) DF(CreateBuffer) DF(DestroyBuffer) DF(GetBufferMemoryRequirements)
    DF(AllocateMemory) DF(FreeMemory) DF(BindBufferMemory) DF(MapMemory) DF(UnmapMemory)
#undef DF
    id->GetMemProps(phys, &d->memprops);
    uint32_t nq = 0;
    id->GetQFProps(phys, &nq, nullptr);
    d->qfams.resize(nq);
    id->GetQFProps(phys, &nq, d->qfams.data());
    std::lock_guard<std::mutex> l(g_lock);
    g_dev[dkey(*out)] = d;
    return r;
}

// ------------------------------------------------------------------ instance hooks
static VKAPI_ATTR VkResult VKAPI_CALL pacer_CreateInstance(const VkInstanceCreateInfo *ci,
                                                         const VkAllocationCallbacks *alloc, VkInstance *out) {
    auto *link = (VkLayerInstanceCreateInfo *)ci->pNext;
    while (link && !(link->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && link->function == VK_LAYER_LINK_INFO))
        link = (VkLayerInstanceCreateInfo *)link->pNext;
    if (!link) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    auto create = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
    VkResult r = create(ci, alloc, out);
    if (r != VK_SUCCESS) return r;
    InstData *id = new InstData();
    id->inst = *out;
    id->gipa = gipa;
    id->DestroyInstance = (PFN_vkDestroyInstance)gipa(*out, "vkDestroyInstance");
    id->GetMemProps = (PFN_vkGetPhysicalDeviceMemoryProperties)gipa(*out, "vkGetPhysicalDeviceMemoryProperties");
    id->GetQFProps = (PFN_vkGetPhysicalDeviceQueueFamilyProperties)gipa(*out, "vkGetPhysicalDeviceQueueFamilyProperties");
    id->GetPresentModes = (PFN_vkGetPhysicalDeviceSurfacePresentModesKHR)gipa(*out, "vkGetPhysicalDeviceSurfacePresentModesKHR");
    id->GetSurfCaps = (PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR)gipa(*out, "vkGetPhysicalDeviceSurfaceCapabilitiesKHR");
    id->CreateXlibSurface = gipa(*out, "vkCreateXlibSurfaceKHR");
    id->CreateXcbSurface = gipa(*out, "vkCreateXcbSurfaceKHR");
    std::lock_guard<std::mutex> l(g_lock);
    g_inst[dkey(*out)] = id;
    return r;
}

static VKAPI_ATTR void VKAPI_CALL pacer_DestroyInstance(VkInstance inst, const VkAllocationCallbacks *alloc) {
    InstData *id = inst_of(dkey(inst));
    if (!id) return;
    {
        std::lock_guard<std::mutex> l(g_lock);
        g_inst.erase(dkey(inst));
    }
    id->DestroyInstance(inst, alloc);
    delete id;
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL pacer_GetInstanceProcAddr(VkInstance inst, const char *n);

// remember the X11 window for background (focus) detection
static VKAPI_ATTR VkResult VKAPI_CALL pacer_CreateXlibSurfaceKHR(VkInstance inst, const void *ci,
                                                                const VkAllocationCallbacks *a, VkSurfaceKHR *s) {
    InstData *id = inst_of(dkey(inst));
    if (!id || !id->CreateXlibSurface) return VK_ERROR_EXTENSION_NOT_PRESENT;
    VkResult r = ((PFN_CreateSurface)id->CreateXlibSurface)(inst, ci, a, s);
    if (r == VK_SUCCESS && ci) core().set_window(((const XlibSurfaceCI *)ci)->window);
    return r;
}

static VKAPI_ATTR VkResult VKAPI_CALL pacer_CreateXcbSurfaceKHR(VkInstance inst, const void *ci,
                                                               const VkAllocationCallbacks *a, VkSurfaceKHR *s) {
    InstData *id = inst_of(dkey(inst));
    if (!id || !id->CreateXcbSurface) return VK_ERROR_EXTENSION_NOT_PRESENT;
    VkResult r = ((PFN_CreateSurface)id->CreateXcbSurface)(inst, ci, a, s);
    if (r == VK_SUCCESS && ci) core().set_window(((const XcbSurfaceCI *)ci)->window);
    return r;
}

static PFN_vkVoidFunction dev_hook(const char *n) {
#define H(x) if (!strcmp(n, "vk" #x)) return (PFN_vkVoidFunction)pacer_##x;
    H(GetDeviceProcAddr) H(QueuePresentKHR) H(CreateSwapchainKHR) H(DestroySwapchainKHR)
    H(GetDeviceQueue) H(GetDeviceQueue2) H(DestroyDevice)
#undef H
    return nullptr;
}

static VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL pacer_GetInstanceProcAddr(VkInstance inst, const char *n) {
#define H(x) if (!strcmp(n, "vk" #x)) return (PFN_vkVoidFunction)pacer_##x;
    H(GetInstanceProcAddr) H(CreateInstance) H(DestroyInstance) H(CreateDevice)
#undef H
    if (PFN_vkVoidFunction f = dev_hook(n)) return f;
    if (!inst) return nullptr;
    InstData *id = inst_of(dkey(inst));
    if (!id) return nullptr;
    // only expose the surface hooks if the driver chain actually has them
    if (!strcmp(n, "vkCreateXlibSurfaceKHR")) return id->CreateXlibSurface ? (PFN_vkVoidFunction)pacer_CreateXlibSurfaceKHR : nullptr;
    if (!strcmp(n, "vkCreateXcbSurfaceKHR")) return id->CreateXcbSurface ? (PFN_vkVoidFunction)pacer_CreateXcbSurfaceKHR : nullptr;
    return id->gipa(inst, n);
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL pacer_NegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *v) {
    if (v->loaderLayerInterfaceVersion > 2) v->loaderLayerInterfaceVersion = 2;
    v->pfnGetInstanceProcAddr = pacer_GetInstanceProcAddr;
    v->pfnGetDeviceProcAddr = pacer_GetDeviceProcAddr;
    v->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *v) {
    return pacer_NegotiateLoaderLayerInterfaceVersion(v);
}
