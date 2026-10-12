// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef LQ_EGL_OFFSCREEN_H
#define LQ_EGL_OFFSCREEN_H
/* Scratch backends: software pbuffer diagnostic or explicit-fd GPU transport. */
extern int lq_offscreen_width, lq_offscreen_height, lq_offscreen_done;
/* Pbuffer: synchronous readback valid only during callback, bottom-up.
 * GPU: normal frames pass NULL and use the separate DMA-BUF transport;
 * an explicitly armed freeze refresh passes bottom-up RGBA once. */
typedef void (*lq_frame_callback_t)(int width,int height,const unsigned char *rgba,int bottom_up);
void LQ_EGL_SetFrameCallback(lq_frame_callback_t callback);
/* GPU backend export: arm one render-only refresh (1), or cancel it (0).
 * The frontend cancels on every command exit, including failed refreshes. */
int retro_lq_capture(int enabled);
int retro_lq_gpu_ready(void);
void LQ_EGL_RequireIsolation(void);
int LQ_EGL_Ready(void);
void LQ_EGL_Create(int width, int height);
void *LQ_EGL_GetProcAddress(const char *name);
void LQ_EGL_EndFrame(void);
void LQ_EGL_Shutdown(void);
#endif
