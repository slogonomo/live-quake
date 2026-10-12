// SPDX-License-Identifier: 0BSD
#ifndef LIVE_QUAKE_AUDIO_H
#define LIVE_QUAKE_AUDIO_H
#include <stddef.h>
#include <stdint.h>

typedef enum { AUDIO_DISCARD = 0, AUDIO_ALSA = 1 } AudioMode;
typedef struct {
    uint64_t frames_offered, frames_written, frames_discarded;
    uint64_t open_errors, io_errors, xruns, recoveries, queue_drops;
    uint64_t short_writes, again;
    uint64_t trimmed_frames;
    unsigned rate;
    size_t buffer_frames, delay_frames, buffered_frames, queued_frames;
    /* delay_frames is observed total sink delay; buffered_frames counts our
     * PCM ring only. Downstream graph/device delay is outside our budget. */
    /* backend_ready is the last successful validation, retained while a muted
     * device is deliberately released. enabled also requires a live prepared
     * PCM and the current owner gate; failures clear both immediately. */
    int enabled, backend_ready, last_error;
} AudioStats;

/* One sink per frontend. Public calls use the simulation/control thread;
 * a private worker alone calls ALSA and never holds that thread's mutex.
 * S16 stereo interleaved input is native little-endian on our x86_64 target.
 * Open starts gated off. DISCARD is silent and never opens a PCM.
 * Write always consumes the supplied frame count, including dropped frames;
 * this prevents libretro from replaying backpressure on its next callback.
 * Open is asynchronous. A 120-ms bounded queue absorbs normal tick jitter;
 * the PCM starts after a 50-ms prefill. Our PCM ring and pending queue
 * are kept near an 80-ms budget (downstream device latency is independent)
 * by dropping their oldest pending samples; no already-written PCM is replayed.
 * Muting clears pending samples, drops the PCM, and releases it after 1.5 s.
 * Failed devices retry every 2 s.
 * Stalled plugin calls are reported after 1 s; control calls never wait on
 * them. Close waits at most 50 ms, retaining live worker state if necessary. */
int audio_open(unsigned rate, AudioMode mode);
void audio_gate(int enabled);
size_t audio_write(const int16_t *stereo, size_t frames);
void audio_close(void);
AudioStats audio_statistics(void);

#ifdef LIVE_QUAKE_AUDIO_TEST
/* Compile-only test seam. Production code has no device or ops override. */
typedef struct {
    int (*open)(void **handle, const char *name, int nonblocking);
    int (*configure)(void *handle, unsigned rate, unsigned latency_us,
                     size_t *buffer_frames, size_t *period_frames);
    int (*prepare)(void *handle);
    int (*drop)(void *handle);
    int (*close)(void *handle);
    long (*avail)(void *handle);
    long (*write)(void *handle, const int16_t *stereo, size_t frames);
    long (*delay)(void *handle);
} AudioTestOps;
int audio_test_set_ops(const AudioTestOps *ops);
#endif
#endif
