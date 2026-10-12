// SPDX-License-Identifier: 0BSD
#define _GNU_SOURCE
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#include "audio.h"
#include <errno.h>
#include <limits.h>
#include <string.h>
#include <pthread.h>
#include <stdlib.h>
#include <signal.h>
#include <time.h>


#ifdef LIVE_QUAKE_AUDIO_TEST
typedef AudioTestOps AudioOps;
static AudioOps ops;
#else
#include <alloca.h>
#include <alsa/asoundlib.h>
typedef struct {
    int (*open)(void **, const char *, int);
    int (*configure)(void *, unsigned, unsigned, size_t *, size_t *);
    int (*prepare)(void *);
    int (*drop)(void *);
    int (*close)(void *);
    long (*avail)(void *);
    long (*write)(void *, const int16_t *, size_t);
    long (*delay)(void *);
} AudioOps;
static int pcm_open(void **handle, const char *name, int nonblocking) {
    snd_pcm_t *pcm = NULL;
    int result = snd_pcm_open(&pcm, name, SND_PCM_STREAM_PLAYBACK,
                              nonblocking ? SND_PCM_NONBLOCK : 0);
    *handle = pcm;
    return result;
}
static int pcm_configure(void *handle, unsigned rate, unsigned latency,
                         size_t *buffer, size_t *period) {
    snd_pcm_t *pcm = handle;
    snd_pcm_uframes_t b, p;
    snd_pcm_sw_params_t *parameters;
    int result = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE,
        SND_PCM_ACCESS_RW_INTERLEAVED, 2, rate, 1, latency);
    if (result < 0) return result;
    result = snd_pcm_get_params(pcm, &b, &p);
    if (result < 0) return result;
    *buffer = b; *period = p;
    snd_pcm_sw_params_alloca(&parameters);
    result = snd_pcm_sw_params_current(pcm, parameters);
    if (result < 0) return result;
    result = snd_pcm_sw_params_set_start_threshold(pcm, parameters, rate / 20);
    if (result < 0) return result;
    result = snd_pcm_sw_params_set_avail_min(pcm, parameters, p);
    if (result < 0) return result;
    return snd_pcm_sw_params(pcm, parameters);
}
static int pcm_prepare(void *pcm) { return snd_pcm_prepare(pcm); }
static int pcm_drop(void *pcm) { return snd_pcm_drop(pcm); }
static int pcm_close(void *pcm) { return snd_pcm_close(pcm); }
static long pcm_avail(void *pcm) { return snd_pcm_avail_update(pcm); }
static long pcm_write(void *pcm, const int16_t *data, size_t frames) {
    return snd_pcm_writei(pcm, data, frames);
}
static long pcm_delay(void *pcm) {
    snd_pcm_sframes_t frames;
    int result=snd_pcm_delay(pcm,&frames);
    return result<0?result:frames>0?frames:0;
}
static const AudioOps ops = {pcm_open, pcm_configure, pcm_prepare, pcm_drop,
                             pcm_close, pcm_avail, pcm_write, pcm_delay};
#endif

/* ALSA plugins can block even with SND_PCM_NONBLOCK (including open/prepare).
 * Only this worker calls ALSA; it never holds the control mutex while doing so.
 * The frontend can always pause, publish frames, report failure and exit. */
#define QUEUE_MS 120
#define PCM_LATENCY_US 100000
#define PCM_MAX_MS 200
#define OWNED_LATENCY_MS 80
#define OP_TIMEOUT 1.0
#define RETRY_SECONDS 2.0
#define RELEASE_SECONDS 1.5
#define CHUNK_FRAMES 4096
typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t wake;
    pthread_t thread;
    AudioStats stats;
    int stop, gate, done, op_kind, op_timed_out;
    uint64_t epoch;
    double op_started;
    size_t capacity, head, queued;
    int16_t *queue;
} AudioWorker;
static AudioWorker *worker, *retired;
static AudioMode mode;
static AudioStats last_stats;

static double monotonic(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void count(uint64_t *value, uint64_t addition) {
    *value = UINT64_MAX - *value < addition ? UINT64_MAX : *value + addition;
}
static void clear_queue(AudioWorker *w) {
    count(&w->stats.frames_discarded, w->queued);
    w->queued = w->head = 0;
    w->stats.delay_frames = w->stats.buffered_frames = 0;
}
static void timed_out_locked(AudioWorker *w) {
    if (w->op_kind && !w->op_timed_out && monotonic()-w->op_started >= OP_TIMEOUT) {
        w->op_timed_out = 1;
        w->stats.backend_ready = w->stats.enabled = 0;
        w->stats.last_error = -ETIMEDOUT;
        count(w->op_kind == 1 ? &w->stats.open_errors : &w->stats.io_errors, 1);
        clear_queue(w);
    }
}
static void begin_operation(AudioWorker *w, int kind) {
    pthread_mutex_lock(&w->mutex);
    w->op_kind = kind; w->op_timed_out = 0; w->op_started = monotonic();
    pthread_mutex_unlock(&w->mutex);
}
static int end_operation(AudioWorker *w) {
    pthread_mutex_lock(&w->mutex);
    timed_out_locked(w);
    int timed_out = w->op_timed_out;
    w->op_kind = 0;
    pthread_mutex_unlock(&w->mutex);
    return timed_out;
}
static void error(AudioWorker *w, int code, int opening) {
    pthread_mutex_lock(&w->mutex);
    w->stats.last_error = code;
    count(opening ? &w->stats.open_errors : &w->stats.io_errors, 1);
    w->stats.backend_ready = w->stats.enabled = 0;
    clear_queue(w);
    pthread_mutex_unlock(&w->mutex);
}
static void close_device(AudioWorker *w, void **pcm) {
    if (!*pcm) return;
    begin_operation(w, 2);
    int result = ops.drop(*pcm);
    if (result < 0) error(w, result, 0);
    result = ops.close(*pcm);
    if (result < 0) error(w, result, 0);
    end_operation(w);
    *pcm = NULL;
    pthread_mutex_lock(&w->mutex);
    count(&w->stats.queue_drops, 1);
    w->stats.enabled = 0;
    w->stats.delay_frames = w->stats.buffered_frames = 0;
    pthread_mutex_unlock(&w->mutex);
}
static void wait_worker(AudioWorker *w, double seconds, uint64_t epoch) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    long extra = (long)(seconds * 1e9);
    t.tv_sec += extra/1000000000;
    t.tv_nsec += extra%1000000000;
    if (t.tv_nsec >= 1000000000) {++t.tv_sec; t.tv_nsec -= 1000000000;}
    pthread_mutex_lock(&w->mutex);
    /* A gate change may signal before this lock is acquired. Its epoch must
     * be handled immediately instead of losing the wakeup until the timer. */
    if (!w->stop && w->epoch==epoch) pthread_cond_timedwait(&w->wake, &w->mutex, &t);
    pthread_mutex_unlock(&w->mutex);
}
static void *audio_worker(void *argument) {
    AudioWorker *w = argument;
    void *pcm = NULL;
    size_t buffer = 0, period = 0;
    uint64_t active_epoch = UINT64_MAX;
    int prepared = 0, attempted = 0, ever_ready = 0, recovering = 0;
    double retry_at = 0, muted_at = monotonic();
    int16_t batch[CHUNK_FRAMES*2];
    for (;;) {
        pthread_mutex_lock(&w->mutex);
        int stop = w->stop, gate = w->gate;
        uint64_t epoch = w->epoch;
        pthread_mutex_unlock(&w->mutex);
        if (stop) break;
        if (epoch != active_epoch) {
            prepared = 0;
            active_epoch = epoch;
            if (pcm) {
                begin_operation(w, 2);
                int result = ops.drop(pcm);
                int slow = end_operation(w);
                pthread_mutex_lock(&w->mutex);
                count(&w->stats.queue_drops, 1);
                w->stats.enabled = 0;
                w->stats.delay_frames = w->stats.buffered_frames = 0;
                pthread_mutex_unlock(&w->mutex);
                if (result < 0 || slow) {
                    if (result < 0) error(w, result, 0);
                    close_device(w, &pcm);
                    retry_at = monotonic()+RETRY_SECONDS;
                }
            }
            if (!gate) muted_at = monotonic();
        }
        if (pcm && !gate) {
            if (monotonic()-muted_at >= RELEASE_SECONDS) close_device(w, &pcm);
            wait_worker(w, .02, active_epoch);
            continue;
        }
        if (!pcm) {
            if ((attempted && ever_ready && !gate) || monotonic() < retry_at) {
                wait_worker(w, .02, active_epoch);
                continue;
            }
            attempted = 1;
            begin_operation(w, 1);
            int result = ops.open(&pcm, "default", 1);
            if (result >= 0 && !pcm) result = -EIO;
            if (result >= 0) result = ops.configure(pcm, w->stats.rate,
                PCM_LATENCY_US, &buffer, &period);
            if (result >= 0 && (buffer < w->stats.rate/20 || !period || period > buffer ||
                buffer > ((uint64_t)w->stats.rate * PCM_MAX_MS + 999)/1000)) result = -EINVAL;
            int slow = end_operation(w);
            if (result < 0 || slow) {
                if (result < 0) error(w, result, 1);
                close_device(w, &pcm);
                retry_at = monotonic()+RETRY_SECONDS;
                continue;
            }
            ever_ready = 1;
            pthread_mutex_lock(&w->mutex);
            w->stats.buffer_frames = buffer;
            w->stats.backend_ready = 1;
            w->stats.last_error = 0;
            pthread_mutex_unlock(&w->mutex);
            /* Recheck ownership after opening: the caller may have muted while
             * a slow session was answering. Never prepare using that old gate. */
            continue;
        }
        if (!prepared) {
            begin_operation(w, 2);
            int result = ops.prepare(pcm);
            int slow = end_operation(w);
            if (result < 0 || slow) {
                if (result < 0) error(w, result, 0);
                close_device(w, &pcm);
                retry_at = monotonic()+RETRY_SECONDS;
                continue;
            }
            prepared = 1;
            pthread_mutex_lock(&w->mutex);
            w->stats.enabled = w->gate && w->epoch == active_epoch && !w->stop;
            w->stats.backend_ready = 1;
            w->stats.last_error = 0;
            if (recovering) {count(&w->stats.recoveries, 1); recovering = 0;}
            pthread_mutex_unlock(&w->mutex);
        }
        pthread_mutex_lock(&w->mutex);
        size_t amount = 0;
        if (w->gate && !w->stop && w->epoch == active_epoch) {
            amount = w->queued < CHUNK_FRAMES ? w->queued : CHUNK_FRAMES;
            for (size_t i=0; i<amount; ++i) {
                size_t from = (w->head+i)%w->capacity;
                batch[i*2] = w->queue[from*2]; batch[i*2+1] = w->queue[from*2+1];
            }
            w->head = (w->head+amount)%w->capacity; w->queued -= amount;
        }
        pthread_mutex_unlock(&w->mutex);
        if (!amount) {wait_worker(w, .005, active_epoch); continue;}
        begin_operation(w, 2);
        long available = ops.avail(pcm);
        long device_delay=available>=0?ops.delay(pcm):0;
        int slow = end_operation(w);
        pthread_mutex_lock(&w->mutex);
        int current = w->gate && !w->stop && w->epoch == active_epoch;
        size_t budget=((size_t)w->stats.rate*OWNED_LATENCY_MS)/1000;
        size_t room=0;
        size_t buffered=0;
        if(current && !slow && available>=0 && device_delay>=0) {
            /* PipeWire's delay includes downstream graph/device latency even
             * in PREPARED state. Charging that fixed latency can prevent the
             * 50-ms start threshold from ever being reached. Limit only our
             * PCM ring occupancy plus pending samples; keep total delay as
             * independent diagnostics. Availability is bounded by our ring. */
            size_t free_frames=(size_t)available<buffer?(size_t)available:buffer;
            buffered=buffer-free_frames;
            w->stats.delay_frames=(size_t)device_delay;
            w->stats.buffered_frames=buffered;
            room=buffered<budget?budget-buffered:0;
            size_t pending=amount+w->queued;
            if(pending>room) {
                size_t discard=pending-room;
                size_t from_batch=discard<amount?discard:amount;
                amount-=from_batch;
                memmove(batch,batch+from_batch*2,amount*2*sizeof(*batch));
                size_t from_queue=discard-from_batch;
                w->head=(w->head+from_queue)%w->capacity;w->queued-=from_queue;
                count(&w->stats.frames_discarded,discard);
                count(&w->stats.trimmed_frames,discard);
            }
        }
        pthread_mutex_unlock(&w->mutex);
        long result = 0;
        size_t wanted = 0;
        if (current && !slow && available > 0 && device_delay>=0 && amount) {
            wanted = amount < (size_t)available ? amount : (size_t)available;
            if (wanted > buffer) wanted = buffer;
            if (wanted > room) wanted = room;
            begin_operation(w, 2);
            result = ops.write(pcm, batch, wanted);
            slow = end_operation(w);
        } else if (available < 0) result = available;
        else if (device_delay < 0) result=device_delay;
        size_t written = result > 0 && (size_t)result <= wanted ? (size_t)result : 0;
        pthread_mutex_lock(&w->mutex);
        count(&w->stats.frames_written, written);
        if(w->gate && !w->stop && w->epoch==active_epoch && !slow)
            w->stats.buffered_frames=buffered+written;
        size_t retained = 0;
        /* Partial availability is ordinary clock drift, not a reason to lose
         * the remainder of this tick. Put it before newer queued samples. */
        if (!slow && (result >= 0 || result == -EAGAIN || result == -EINTR) &&
            w->gate && !w->stop && w->epoch == active_epoch) {
            retained = amount-written;
            if (retained > w->capacity-w->queued) retained = w->capacity-w->queued;
            w->head = (w->head+w->capacity-retained)%w->capacity;
            for (size_t i=0; i<retained; ++i) {
                size_t target = (w->head+i)%w->capacity;
                w->queue[target*2] = batch[(written+i)*2];
                w->queue[target*2+1] = batch[(written+i)*2+1];
            }
            w->queued += retained;
        }
        count(&w->stats.frames_discarded, amount-written-retained);
        if (written < wanted && result >= 0) count(&w->stats.short_writes, 1);
        if (result == -EAGAIN) count(&w->stats.again, 1);
        if (result == -EPIPE) count(&w->stats.xruns, 1);
        pthread_mutex_unlock(&w->mutex);
        if (result == -EPIPE || result == -ESTRPIPE) {
            error(w, (int)result, 0);
            prepared = 0;
            recovering = 1;
            /* Drop once, then prefill fresh audio on the next prepare. */
            active_epoch = UINT64_MAX;
        } else if (slow || (result < 0 && result != -EAGAIN && result != -EINTR) ||
                   (result > 0 && (size_t)result > wanted)) {
            if (result < 0 || result > 0) error(w, result < 0 ? (int)result : -EIO, 0);
            close_device(w, &pcm);
            prepared = 0;
            retry_at = monotonic()+RETRY_SECONDS;
        }
        if (written < amount) wait_worker(w, .005, active_epoch);
    }
    close_device(w, &pcm);
    pthread_mutex_lock(&w->mutex);
    clear_queue(w);
    w->stats.backend_ready = w->stats.enabled = 0;
    w->done = 1;
    pthread_mutex_unlock(&w->mutex);
    return NULL;
}
static void free_worker(AudioWorker *w) {
    pthread_cond_destroy(&w->wake); pthread_mutex_destroy(&w->mutex);
    free(w->queue); free(w);
}
void audio_close(void) {
    if (!worker) {
        if (retired && !pthread_tryjoin_np(retired->thread, NULL)) {
            last_stats = retired->stats; free_worker(retired); retired = NULL;
        }
        return;
    }
    AudioWorker *w = worker;
    pthread_mutex_lock(&w->mutex);
    w->stop = 1; w->gate = 0; ++w->epoch; clear_queue(w);
    w->stats.enabled = w->stats.backend_ready = 0;
    last_stats = w->stats;
    pthread_cond_signal(&w->wake);
    pthread_mutex_unlock(&w->mutex);
    struct timespec deadline;
    clock_gettime(CLOCK_REALTIME, &deadline);
    deadline.tv_nsec += 50000000;
    if (deadline.tv_nsec >= 1000000000) {++deadline.tv_sec; deadline.tv_nsec -= 1000000000;}
    if (!pthread_timedjoin_np(w->thread, NULL, &deadline)) {
        last_stats = w->stats; free_worker(w);
    } else {
        /* Do not cancel inside a third-party plugin or free its live state.
         * Keep at most one retired worker; normal process exit terminates it. */
        retired = w;
    }
    worker = NULL;
}
int audio_open(unsigned rate, AudioMode requested_mode) {
    audio_close();
    memset(&last_stats, 0, sizeof(last_stats)); mode = requested_mode; last_stats.rate = rate;
    if (rate < 8000 || rate > 192000 || (mode != AUDIO_DISCARD && mode != AUDIO_ALSA)) {
        last_stats.open_errors = 1; return last_stats.last_error = -EINVAL;
    }
    if (mode == AUDIO_DISCARD) return 0;
    if (retired) {
        if (pthread_tryjoin_np(retired->thread, NULL) != 0) {
            last_stats.open_errors = 1; return last_stats.last_error = -EBUSY;
        }
        free_worker(retired); retired = NULL;
    }
#ifdef LIVE_QUAKE_AUDIO_TEST
    if (!ops.open) {last_stats.open_errors = 1; return last_stats.last_error = -ENOSYS;}
#endif
    AudioWorker *w = calloc(1, sizeof(*w));
    if (!w) return last_stats.last_error = -ENOMEM;
    w->capacity = ((size_t)rate*QUEUE_MS+999)/1000;
    w->queue = calloc(w->capacity*2, sizeof(*w->queue));
    if (!w->queue) {free(w); return last_stats.last_error = -ENOMEM;}
    pthread_mutex_init(&w->mutex, NULL);
    pthread_condattr_t attr;
    pthread_condattr_init(&attr); pthread_condattr_setclock(&attr, CLOCK_MONOTONIC);
    pthread_cond_init(&w->wake, &attr); pthread_condattr_destroy(&attr);
    w->stats.rate = rate;
    /* Process shutdown signals belong to the frontend/control thread. */
    sigset_t blocked,previous;
    sigfillset(&blocked);
    int result=pthread_sigmask(SIG_BLOCK,&blocked,&previous);
    if(!result) {
        result=pthread_create(&w->thread,NULL,audio_worker,w);
        pthread_sigmask(SIG_SETMASK,&previous,NULL);
    }
    if (result) {free_worker(w); last_stats.open_errors = 1; return last_stats.last_error = -result;}
    worker = w;
    return 0;
}
void audio_gate(int enabled) {
    if (!worker) {last_stats.enabled = mode == AUDIO_DISCARD && !!enabled; return;}
    pthread_mutex_lock(&worker->mutex);
    if (worker->gate != !!enabled) {
        worker->gate = !!enabled; ++worker->epoch;
        worker->stats.enabled = 0; clear_queue(worker);
        pthread_cond_signal(&worker->wake);
    }
    pthread_mutex_unlock(&worker->mutex);
}
size_t audio_write(const int16_t *stereo, size_t frames) {
    if (!worker) {
        count(&last_stats.frames_offered, frames); count(&last_stats.frames_discarded, frames);
        if (frames && !stereo) {last_stats.last_error = -EINVAL; count(&last_stats.io_errors, 1);}
        return frames;
    }
    AudioWorker *w = worker;
    pthread_mutex_lock(&w->mutex);
    timed_out_locked(w);
    count(&w->stats.frames_offered, frames);
    size_t queued = 0;
    if (frames && stereo && w->gate && !w->stop && !w->op_timed_out) {
        queued = frames < w->capacity ? frames : w->capacity;
        size_t discard = queued > w->capacity-w->queued ? queued-(w->capacity-w->queued) : 0;
        w->head = (w->head+discard)%w->capacity; w->queued -= discard;
        count(&w->stats.frames_discarded, discard);
        for (size_t i=0; i<queued; ++i) {
            size_t target = (w->head+w->queued+i)%w->capacity;
            size_t from = frames-queued+i;
            w->queue[target*2] = stereo[from*2]; w->queue[target*2+1] = stereo[from*2+1];
        }
        w->queued += queued; pthread_cond_signal(&w->wake);
    } else if (frames && !stereo) {
        w->stats.last_error = -EINVAL; count(&w->stats.io_errors, 1);
    }
    count(&w->stats.frames_discarded, frames-queued);
    pthread_mutex_unlock(&w->mutex);
    return frames;
}
AudioStats audio_statistics(void) {
    if (!worker) return last_stats;
    pthread_mutex_lock(&worker->mutex);
    timed_out_locked(worker);
    AudioStats result = worker->stats;
    result.queued_frames=worker->queued;
    pthread_mutex_unlock(&worker->mutex);
    return result;
}
#ifdef LIVE_QUAKE_AUDIO_TEST
int audio_test_set_ops(const AudioTestOps *replacement) {
    if (worker || retired || !replacement || !replacement->open || !replacement->configure ||
        !replacement->prepare || !replacement->drop || !replacement->close ||
        !replacement->avail || !replacement->write || !replacement->delay) return -EINVAL;
    ops = *replacement;
    return 0;
}
#endif
