/* SPDX-License-Identifier: 0BSD */
#ifndef LIVE_QUAKE_GPU_WAYLAND_H
#define LIVE_QUAKE_GPU_WAYLAND_H
#include "gpu_transport.h"
#include <wayland-client.h>
#include "linux-dmabuf-v1-client-protocol.h"

#define LQ_GPU_FORMAT_PAIRS 64u
#define LQ_GPU_CONSUMERS 16u
struct lq_gpu_wayland;
struct lq_gpu_wayland_slot;
struct lq_gpu_format_pair { uint32_t format; uint64_t modifier; };
struct lq_gpu_wayland_consumer {
    struct lq_gpu_wayland_slot *slot;
    struct zwp_linux_buffer_params_v1 *params;
    struct wl_buffer *buffer;
    bool committed;
};
struct lq_gpu_wayland_slot {
    struct lq_gpu_wayland *owner;
    struct lq_gpu_frame frame;
    struct lq_gpu_wayland_consumer consumers[LQ_GPU_CONSUMERS];
    uint32_t count, pending, committed;
    bool occupied, release_ready;
};
/* Called only after every consumer's asynchronous import succeeds. Each
 * wl_buffer must be attached/committed to AT MOST ONE wl_surface. Return a
 * mask of buffers actually committed; never attach any uncommitted buffer.
 * The callback must not recursively dispatch the Wayland display. Cropping
 * and dimensions are GPU wl_surface/wp_viewporter state set by the caller.
 * A hidden/destroyed output returns no bit and receives no committed buffer. */
typedef uint32_t (*lq_gpu_wayland_ready_fn)(void *context,const struct lq_gpu_frame *frame,
    struct wl_buffer *const *buffers,uint32_t count);
typedef void (*lq_gpu_wayland_error_fn)(void *context,const char *reason);
struct lq_gpu_wayland {
    struct zwp_linux_dmabuf_v1 *dmabuf;
    struct lq_gpu_format_pair formats[LQ_GPU_FORMAT_PAIRS];
    struct lq_gpu_wayland_slot slots[LQ_GPU_SLOTS];
    uint32_t format_count;
    uint64_t generation, last_serial;
    uint64_t capability_generation;
    uint32_t capability_next;
    bool capabilities_sent;
    bool formats_ready, failed, closed;
    lq_gpu_wayland_ready_fn ready;
    lq_gpu_wayland_error_fn error;
    void *context;
};
/* Takes ownership of a newly bound version-3 dmabuf global. Binding version 3
 * deliberately obtains format/modifier events even on newer servers; a later
 * version-4 feedback adapter can use the same format-pair interface. No DRM
 * devices are discovered or opened here. Call formats_done after wl_display
 * sync confirms initial advertisements. Explicit modifiers only, no guessed
 * linear allocation or silent software fallback. Initialize storage with {0}. */
int lq_gpu_wayland_init(struct lq_gpu_wayland *importer,struct zwp_linux_dmabuf_v1 *dmabuf,
    lq_gpu_wayland_ready_fn ready,lq_gpu_wayland_error_fn error,void *context);
int lq_gpu_wayland_formats_done(struct lq_gpu_wayland *importer);
uint32_t lq_gpu_wayland_modifiers(const struct lq_gpu_wayland *importer,uint32_t format,
    uint64_t *modifiers,uint32_t capacity);
/* After formats_done, advertise capabilities to producer before any allocation.
 * Returns1 complete,0 needs writable socket,-1 failed; resume with same generation.
 * The caller owns the monotonic startup deadline. Generation comes from an
 * explicit parent argument (unique per pool, never an environment hook). */
int lq_gpu_wayland_send_capabilities(struct lq_gpu_wayland *importer,int socket_fd,uint64_t generation);
/* Takes/closes every FD in frame on both success and failure. consumer_count
 * includes only output surfaces eligible for this frame (zero safely ACKs).
 * The ready callback's surfaces must use implicit synchronization. Do not add
 * linux-drm-syncobj/explicit-sync surface objects: they make wl_buffer.release
 * undefined and need a separate release-timeline implementation.
 * Return 0 accepted async, -1 rejected; a rejected producer packet is fatal
 * to that connection, because it has no trustworthy release accounting. */
int lq_gpu_wayland_import(struct lq_gpu_wayland *importer,struct lq_gpu_frame *frame,uint32_t consumer_count);
/* Poll/retry this ACK nonblocking; confirm only after successful socket send.
 * Holds the slot until confirmation so backpressure cannot lose an ACK. */
bool lq_gpu_wayland_next_release(const struct lq_gpu_wayland *importer,struct lq_gpu_release *release);
int lq_gpu_wayland_release_sent(struct lq_gpu_wayland *importer,const struct lq_gpu_release *release);
/* Drops protocol objects WITHOUT sending release ACKs. The owning producer
 * must retire and stop, because committed BO storage cannot be reused. */
void lq_gpu_wayland_destroy(struct lq_gpu_wayland *importer);
#endif
