/* SPDX-License-Identifier: 0BSD */
#ifndef LIVE_QUAKE_GPU_TRANSPORT_H
#define LIVE_QUAKE_GPU_TRANSPORT_H
#include <stdbool.h>
#include <stdint.h>

#define LQ_GPU_SLOTS 4u
#define LQ_GPU_MAX_IN_FLIGHT 3u
#define LQ_GPU_PLANES 4u
#define LQ_GPU_WIRE_BYTES 160u
#define LQ_GPU_CAPABILITY_LIMIT 64u
#define LQ_GPU_FORMAT_XRGB8888 UINT32_C(0x34325258)
#define LQ_GPU_FORMAT_ARGB8888 UINT32_C(0x34325241)
#define LQ_GPU_FLAG_Y_INVERT 1u
#define LQ_GPU_MODIFIER_INVALID UINT64_C(0x00ffffffffffffff)

enum lq_gpu_kind { LQ_GPU_FRAME = 1, LQ_GPU_RELEASE = 2,
    LQ_GPU_CAPABILITY = 3, LQ_GPU_CAPABILITIES_DONE = 4 };
struct lq_gpu_frame {
    uint64_t generation, serial, modifier;
    uint32_t slot, width, height, format, flags;
    uint32_t par_num, par_den, plane_count;
    uint32_t offset[LQ_GPU_PLANES], stride[LQ_GPU_PLANES];
    int fd[LQ_GPU_PLANES];
};
struct lq_gpu_release { uint64_t generation, serial; uint32_t slot; };
struct lq_gpu_capability { uint64_t generation, modifier; uint32_t index, format; };
struct lq_gpu_capabilities_done { uint64_t generation; uint32_t count; };
struct lq_gpu_packet {
    enum lq_gpu_kind kind;
    struct lq_gpu_frame frame;
    struct lq_gpu_release release;
    struct lq_gpu_capability capability;
    struct lq_gpu_capabilities_done capabilities_done;
};
/* The caller supplies an inherited AF_UNIX SOCK_SEQPACKET socketpair endpoint.
 * All I/O is nonblocking. Send borrows FDs; receive owns FDs until close_frame.
 * Returns 1 on success, 0 on EAGAIN, -1 on failure with errno set. Metadata
 * validation is not a substitute for the compositor's DMA-BUF import check. */
int lq_gpu_send_frame(int socket_fd, const struct lq_gpu_frame *frame);
int lq_gpu_send_release(int socket_fd, const struct lq_gpu_release *release);
int lq_gpu_send_capability(int socket_fd,const struct lq_gpu_capability *capability);
int lq_gpu_send_capabilities_done(int socket_fd,const struct lq_gpu_capabilities_done *done);
int lq_gpu_receive(int socket_fd, struct lq_gpu_packet *packet);
void lq_gpu_close_frame(struct lq_gpu_frame *frame);
bool lq_gpu_frame_valid(const struct lq_gpu_frame *frame);

struct lq_gpu_capabilities {
    uint64_t generation;
    struct lq_gpu_capability records[LQ_GPU_CAPABILITY_LIMIT];
    uint32_t count;
    bool done, failed;
};
/* Producer startup: initialize {0}, collect up to64 ordered unique advertised
 * pairs, then DONE. Returns 1 complete,0 pending,-1 failed. This never waits;
 * the caller owns a monotonic startup deadline and policy pause handling. */
int lq_gpu_capabilities_receive(int socket_fd,struct lq_gpu_capabilities *capabilities);
int lq_gpu_capabilities_accept(struct lq_gpu_capabilities *capabilities,const struct lq_gpu_packet *packet);
uint32_t lq_gpu_capabilities_modifiers(const struct lq_gpu_capabilities *capabilities,
    uint32_t format,uint64_t *modifiers,uint32_t capacity);

struct lq_gpu_leases {
    uint64_t generation, last_serial;
    uint64_t serial[LQ_GPU_SLOTS];
    uint32_t in_flight;
    bool retired;
};
/* Initialize with {0}; subsequent init requires a different generation.
 * Acquire only after a successful send; a failed send has no consumer lease.
 * All calls are from one producer thread. A retired pool may never be reused.
 * Release ACKs must be emitted after ALL wl_buffer consumers are released.
 * A disconnect with outstanding buffers retires the pool; it does not ACK. */
int lq_gpu_leases_init(struct lq_gpu_leases *leases, uint64_t generation);
bool lq_gpu_leases_can_acquire(const struct lq_gpu_leases *leases, uint32_t slot);
int lq_gpu_leases_acquire(struct lq_gpu_leases *leases, uint32_t slot, uint64_t serial);
int lq_gpu_leases_release(struct lq_gpu_leases *leases, const struct lq_gpu_release *release);
void lq_gpu_leases_retire(struct lq_gpu_leases *leases);
#endif
