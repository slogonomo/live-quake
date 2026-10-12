/* SPDX-License-Identifier: 0BSD */
#ifndef LIVE_QUAKE_GPU_GBM_H
#define LIVE_QUAKE_GPU_GBM_H
#include "gpu_transport.h"
#include <EGL/egl.h>
#include <gbm.h>

#define LQ_GPU_MODIFIERS 64u
struct lq_gpu_gbm {
    EGLDisplay display;
    EGLSurface egl_surface;
    struct gbm_surface *surface;
    struct gbm_bo *locked[LQ_GPU_SLOTS];
    struct lq_gpu_leases leases;
    uint64_t modifiers[LQ_GPU_MODIFIERS], next_serial;
    uint32_t modifier_count, width, height, format, flags, par_num, par_den;
    int socket_fd;
};
/* No device/display discovery or opening: the engine supplies its explicit GBM
 * device and current EGL OpenGL context. Modifiers must come from compositor
 * negotiation. This baseline rejects implicit/unknown modifiers.
 * EGL_NATIVE_VISUAL_ID must match format when the caller creates EGLSurface. */
struct gbm_surface *lq_gpu_gbm_surface(struct gbm_device *device,uint32_t width,
    uint32_t height,uint32_t format,const uint64_t *modifiers,uint32_t count);
int lq_gpu_gbm_init(struct lq_gpu_gbm *producer,int socket_fd,EGLDisplay display,
    EGLSurface egl_surface,struct gbm_surface *surface,uint64_t generation,
    uint32_t width,uint32_t height,uint32_t format,uint32_t flags,
    uint32_t par_num,uint32_t par_den,const uint64_t *modifiers,uint32_t count);
/* Publish on the context/producer thread after the engine's final GL pass.
 * Returns 1 sent, 0 skipped/backpressure, -1 fatal (retire/stop the engine).
 * This calls GL completion and EGL swap, which can block in a faulty driver;
 * the existing native process deadline remains the recovery boundary. */
int lq_gpu_gbm_publish(struct lq_gpu_gbm *producer);
/* Call before SCR_UpdateScreen to skip rendering when all BOs are leased. */
int lq_gpu_gbm_ready(struct lq_gpu_gbm *producer);
int lq_gpu_gbm_releases(struct lq_gpu_gbm *producer);
/* Release/destroy supplied surface/context only after clean acknowledgements.
 * On viewer disconnect, retire and stop the owning engine process. Do not
 * call gbm_surface_release_buffer on its outstanding BOs before process exit.
 * Compositor DMA-BUF references retain the allocation after producer exit. */
bool lq_gpu_gbm_clean(const struct lq_gpu_gbm *producer);
void lq_gpu_gbm_retire(struct lq_gpu_gbm *producer);
#endif
