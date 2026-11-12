// Headless Vulkan smoke test for the pacer layer (no window, no focus).
// Creates a VK_EXT_headless_surface swapchain, presents N frames, reads back
// the OSD region and reports present-to-present pacing as seen by the app.
#include <vulkan/vulkan.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#define CK(x) do { VkResult r_ = (x); if (r_ != VK_SUCCESS) { fprintf(stderr, "FAIL %s = %d (line %d)\n", #x, r_, __LINE__); exit(1); } } while (0)

static double now_ms() {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char **argv) {
    int frames = argc > 1 ? atoi(argv[1]) : 240;
    const char *iext[] = {"VK_KHR_surface", "VK_EXT_headless_surface"};
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ici.pApplicationInfo = &app;
    ici.enabledExtensionCount = 2;
    ici.ppEnabledExtensionNames = iext;
    VkInstance inst;
    CK(vkCreateInstance(&ici, nullptr, &inst));

    uint32_t nl = 0;
    vkEnumerateInstanceLayerProperties(&nl, nullptr);
    std::vector<VkLayerProperties> lp(nl);
    vkEnumerateInstanceLayerProperties(&nl, lp.data());
    bool found = false;
    for (auto &l : lp) if (strcasestr(l.layerName, "pacer")) { found = true; printf("layer: %s\n", l.layerName); }
    if (!found) printf("layer: NOT FOUND in enumeration\n");

    auto createHeadless = (PFN_vkCreateHeadlessSurfaceEXT)vkGetInstanceProcAddr(inst, "vkCreateHeadlessSurfaceEXT");
    VkHeadlessSurfaceCreateInfoEXT hci{VK_STRUCTURE_TYPE_HEADLESS_SURFACE_CREATE_INFO_EXT};
    VkSurfaceKHR surf;
    CK(createHeadless(inst, &hci, nullptr, &surf));

    uint32_t npd = 1;
    VkPhysicalDevice pd;
    vkEnumeratePhysicalDevices(inst, &npd, &pd);
    VkPhysicalDeviceProperties props;
    vkGetPhysicalDeviceProperties(pd, &props);
    printf("gpu: %s\n", props.deviceName);

    float prio = 1;
    VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qci.queueFamilyIndex = 0;
    qci.queueCount = 1;
    qci.pQueuePriorities = &prio;
    const char *dext[] = {"VK_KHR_swapchain"};
    VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    dci.queueCreateInfoCount = 1;
    dci.pQueueCreateInfos = &qci;
    dci.enabledExtensionCount = 1;
    dci.ppEnabledExtensionNames = dext;
    VkDevice dev;
    CK(vkCreateDevice(pd, &dci, nullptr, &dev));
    VkQueue q;
    vkGetDeviceQueue(dev, 0, 0, &q);

    VkSurfaceCapabilitiesKHR caps;
    vkGetPhysicalDeviceSurfaceCapabilitiesKHR(pd, surf, &caps);
    uint32_t nf = 0;
    vkGetPhysicalDeviceSurfaceFormatsKHR(pd, surf, &nf, nullptr);
    std::vector<VkSurfaceFormatKHR> fmts(nf);
    vkGetPhysicalDeviceSurfaceFormatsKHR(pd, surf, &nf, fmts.data());
    VkFormat fmt = fmts[0].format;
    for (auto &f : fmts) if (f.format == VK_FORMAT_B8G8R8A8_UNORM) fmt = f.format;
    printf("format: %d\n", fmt);

    const uint32_t W = 1280, H = 720;
    VkSwapchainCreateInfoKHR sci{VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR};
    sci.surface = surf;
    sci.minImageCount = std::max(3u, caps.minImageCount);
    sci.imageFormat = fmt;
    sci.imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
    sci.imageExtent = {W, H};
    sci.imageArrayLayers = 1;
    sci.imageUsage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    sci.preTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
    sci.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    sci.presentMode = VK_PRESENT_MODE_IMMEDIATE_KHR;
    sci.clipped = VK_TRUE;
    VkSwapchainKHR sc;
    CK(vkCreateSwapchainKHR(dev, &sci, nullptr, &sc));
    uint32_t ni = 0;
    vkGetSwapchainImagesKHR(dev, sc, &ni, nullptr);
    std::vector<VkImage> imgs(ni);
    vkGetSwapchainImagesKHR(dev, sc, &ni, imgs.data());

    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    VkCommandPool pool;
    CK(vkCreateCommandPool(dev, &pci, nullptr, &pool));
    VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cai.commandPool = pool;
    cai.commandBufferCount = 1;
    VkCommandBuffer cb;
    CK(vkAllocateCommandBuffers(dev, &cai, &cb));

    // readback buffer (host visible)
    VkBufferCreateInfo bci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bci.size = (VkDeviceSize)W * H * 4;
    bci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    VkBuffer rb;
    CK(vkCreateBuffer(dev, &bci, nullptr, &rb));
    VkMemoryRequirements mr;
    vkGetBufferMemoryRequirements(dev, rb, &mr);
    VkPhysicalDeviceMemoryProperties mp;
    vkGetPhysicalDeviceMemoryProperties(pd, &mp);
    uint32_t mt = 0;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
        if ((mr.memoryTypeBits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & 6) == 6) { mt = i; break; }
    VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    mai.allocationSize = mr.size;
    mai.memoryTypeIndex = mt;
    VkDeviceMemory rbm;
    CK(vkAllocateMemory(dev, &mai, nullptr, &rbm));
    vkBindBufferMemory(dev, rb, rbm, 0);
    uint32_t *rbp;
    vkMapMemory(dev, rbm, 0, VK_WHOLE_SIZE, 0, (void **)&rbp);

    VkFence acq_fence, sub_fence;
    VkFenceCreateInfo fci{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    vkCreateFence(dev, &fci, nullptr, &acq_fence);
    vkCreateFence(dev, &fci, nullptr, &sub_fence);
    VkSemaphore render_done;
    VkSemaphoreCreateInfo semci{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
    vkCreateSemaphore(dev, &semci, nullptr, &render_done);

    std::vector<int> seen(ni, 0);
    std::vector<double> ft;
    double last = 0;
    int osd_pixels = 0, checked = 0;
    for (int f = 0; f < frames; f++) {
        uint32_t idx;
        CK(vkAcquireNextImageKHR(dev, sc, UINT64_MAX, VK_NULL_HANDLE, acq_fence, &idx));
        vkWaitForFences(dev, 1, &acq_fence, VK_TRUE, UINT64_MAX);
        vkResetFences(dev, 1, &acq_fence);
        bool readback = seen[idx] && f == frames - 1;
        VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        vkBeginCommandBuffer(cb, &bi);
        VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
        b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.image = imgs[idx];
        b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        if (readback) {
            b.oldLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
            b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            b.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
            vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
            VkBufferImageCopy c{};
            c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
            c.imageExtent = {W, H, 1};
            vkCmdCopyImageToBuffer(cb, imgs[idx], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, rb, 1, &c);
            b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
            b.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        } else {
            b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        }
        b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
        VkClearColorValue blue{{0.f, 0.f, 1.f, 1.f}};
        VkImageSubresourceRange rr{VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        vkCmdClearColorImage(cb, imgs[idx], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &blue, 1, &rr);
        b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        b.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
        b.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        b.dstAccessMask = 0;
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, nullptr, 0, nullptr, 1, &b);
        vkEndCommandBuffer(cb);
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        si.commandBufferCount = 1;
        si.pCommandBuffers = &cb;
        si.signalSemaphoreCount = 1;
        si.pSignalSemaphores = &render_done;
        CK(vkQueueSubmit(q, 1, &si, sub_fence));
        vkWaitForFences(dev, 1, &sub_fence, VK_TRUE, UINT64_MAX);
        vkResetFences(dev, 1, &sub_fence);
        if (readback) {
            // the image held the previous frame + OSD; count non-blue pixels in the OSD corner
            for (uint32_t y = 0; y < 80; y++)
                for (uint32_t x = 0; x < 300; x++) {
                    uint32_t p = rbp[y * W + x];
                    checked++;
                    if ((p & 0x00ffffff) != 0x0000ff && (p & 0x00ffffff) != 0xff0000) osd_pixels++;
                }
        }
        VkPresentInfoKHR pi{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR};
        pi.waitSemaphoreCount = 1;
        pi.pWaitSemaphores = &render_done;
        pi.swapchainCount = 1;
        pi.pSwapchains = &sc;
        pi.pImageIndices = &idx;
        CK(vkQueuePresentKHR(q, &pi));
        double t = now_ms();
        if (last) ft.push_back(t - last);
        last = t;
        seen[idx] = 1;
    }
    vkDeviceWaitIdle(dev);
    std::sort(ft.begin(), ft.end());
    double sum = 0;
    for (double x : ft) sum += x;
    printf("frames=%d avg_fps=%.2f median_ft=%.3fms p1=%.3f p99=%.3f\n", frames, 1000.0 / (sum / ft.size()),
           ft[ft.size() / 2], ft[ft.size() / 100], ft[ft.size() * 99 / 100]);
    printf("osd_pixels=%d/%d -> %s\n", osd_pixels, checked, osd_pixels > 200 ? "OSD DRAWN" : "no osd");

    vkDestroySemaphore(dev, render_done, nullptr);
    vkDestroyFence(dev, acq_fence, nullptr);
    vkDestroyFence(dev, sub_fence, nullptr);
    vkUnmapMemory(dev, rbm);
    vkDestroyBuffer(dev, rb, nullptr);
    vkFreeMemory(dev, rbm, nullptr);
    vkDestroyCommandPool(dev, pool, nullptr);
    vkDestroySwapchainKHR(dev, sc, nullptr);
    vkDestroyDevice(dev, nullptr);
    vkDestroySurfaceKHR(inst, surf, nullptr);
    vkDestroyInstance(inst, nullptr);
    printf("clean shutdown OK\n");
    return 0;
}
