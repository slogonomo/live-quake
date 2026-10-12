/* SPDX-License-Identifier: 0BSD */
#define _GNU_SOURCE
#include "bridge.h"
#include "gpu_wayland.h"
#include "viewporter-client-protocol.h"
#include <limits.h>
#include <cairo/cairo.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include <wayland-cursor.h>
#include <xkbcommon/xkbcommon.h>
#include "xdg-output-unstable-v1-client-protocol.h"
#include "wlr-layer-shell-unstable-v1-client-protocol.h"
#include "relative-pointer-unstable-v1-client-protocol.h"
#include "pointer-constraints-unstable-v1-client-protocol.h"

typedef struct App App;
typedef struct Screen Screen;
typedef struct Buffer Buffer;
enum ViewMode { VIEW_HIDDEN, VIEW_PAUSED, VIEW_BOT, VIEW_HUMAN, VIEW_SAVER };
enum SaverRequest { SAVER_REQUEST_NONE, SAVER_REQUEST_STATE, SAVER_REQUEST_READY, SAVER_REQUEST_PRESENTED };
struct Buffer {
    struct wl_buffer *wl;
    void *pixels;
    size_t bytes;
    int width, height, stride;
    bool busy, retired;
    Buffer *next;
};
struct Screen {
    App *app;
    uint32_t global;
    struct wl_output *output;
    struct zxdg_output_v1 *xdg_output;
    struct wl_surface *surface;
    struct wl_surface *gpu_surface;
    struct wl_subsurface *gpu_subsurface;
    struct wp_viewport *background_viewport, *gpu_viewport;
    uint32_t gpu_flags;
    uint64_t gpu_identity, gpu_wait_serial;
    bool gpu_attached, gpu_frozen, gpu_resume_pending, callback_gpu;
    bool gpu_failed_probe, gpu_background_restore;
    bool saver_configured_once;
    uint64_t saver_ready_next, saver_present_next;
    uint64_t saver_callback_cycle, saver_presented_cycle, saver_reported_cycle;
    uint64_t gpu_callback_at;
    uint64_t gpu_frozen_at, gpu_probe_next, gpu_resume_last;
    unsigned gpu_resume_callbacks;
    bool capture_pending, capture_failed, cpu_snapshot;
    uint64_t capture_expected, capture_started_at, capture_copies;
    uint64_t capture_retry_at;
    unsigned capture_retries;
    struct zwlr_layer_surface_v1 *layer;
    struct wl_callback *callback;
    Buffer *buffers[2];
    Buffer *gpu_background;
    Buffer *gpu_probe_parent, *gpu_probe_child;
    char name[128];
    int width, height, scale;
    int output_width, output_height, logical_width, logical_height, output_transform;
    bool configured, redraw, ready, snapshot_valid, audible, removed;
    bool mapped, unmapped;
    struct wl_callback *unmap_barrier;
    bool output_changed;
    bool query_failed;
    enum ViewMode mode, wanted_mode;
    bool wanted_audible;
    uint32_t *snapshot;
    DDHeader header;
    uint64_t last_frame;
    uint64_t last_view_success;
    int blur_strength, blur_cached_strength, blur_width, blur_height;
    uint32_t *blurred;
    bool blur_valid;
    double blur_mix;
    Screen *next;
};
struct App {
    struct wl_display *display;
    struct wl_registry *registry;
    struct wl_compositor *compositor;
    struct wl_shm *shm;
    struct wl_subcompositor *subcompositor;
    struct wp_viewporter *viewporter;
    struct lq_gpu_wayland gpu;
    uint64_t gpu_targets[LQ_GPU_SLOTS][LQ_GPU_CONSUMERS];
    uint64_t gpu_superseded_at[LQ_GPU_SLOTS][LQ_GPU_CONSUMERS];
    uint64_t gpu_screen_serial, gpu_import_at[LQ_GPU_SLOTS];
    uint64_t gpu_generation, gpu_received_serial;
    struct lq_gpu_frame gpu_cached;
    bool gpu_cached_valid;
    int gpu_fd;
    bool capture_reported, capture_bootstrap_failed, capture_rendering;
    uint64_t capture_report_next;
    bool gpu_saver_active, gpu_saver_dismissed, gpu_dismiss_pending;
    unsigned gpu_dismiss_attempts;
    uint64_t gpu_saver_started, gpu_dismiss_next;
    uint64_t saver_generation, saver_cycle, saver_state_next;
    enum SaverRequest saver_request, saver_last_request;
    int saver_control_fd;
    bool saver_connecting;
    char saver_request_bytes[256], saver_reply[128];
    size_t saver_request_used, saver_request_sent, saver_reply_used;
    uint64_t saver_request_deadline, saver_request_generation, saver_request_cycle;
    Screen *saver_request_screen;
    struct wl_seat *seat;
    struct wl_pointer *pointer;
    struct wl_keyboard *keyboard;
    struct wl_cursor_theme *cursor_theme;
    struct wl_cursor *cursor;
    struct wl_surface *cursor_surface;
    struct zwlr_layer_shell_v1 *layer_shell;
    struct zxdg_output_manager_v1 *output_manager;
    struct zwp_relative_pointer_manager_v1 *relative_manager;
    struct zwp_pointer_constraints_v1 *constraints;
    struct zwp_relative_pointer_v1 *relative;
    struct zwp_locked_pointer_v1 *locked;
    struct xkb_context *xkb_context;
    struct xkb_keymap *keymap;
    struct xkb_state *xkb_state;
    Screen *screens, *pointer_focus, *keyboard_focus, *human;
    Buffer *buffers;
    const char *socket_path, *frame_path;
    int frame_fd;
    const DDHeader *shared;
    uint32_t keys[768], refs[256];
    uint32_t cursor_serial;
    uint32_t mouse_buttons;
    bool quit, pointer_seen;
    bool lock_active;
    bool input_active, input_connecting, input_failed, returning_bot;
    int input_fd;
    char input_queue[65536], input_reply[512];
    size_t input_used, input_sent, input_reply_used;
    int64_t input_mouse_x, input_mouse_y;
    int repeat_rate, repeat_delay;
    uint32_t repeat_key;
    uint64_t repeat_at;
    double pointer_x, pointer_y, mouse_remainder_x, mouse_remainder_y;
    uint64_t started, next_query;
};
static volatile sig_atomic_t interrupted;
static uint64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000 + (uint64_t)ts.tv_nsec / 1000000;
}
static void on_signal(int sig) { (void)sig; interrupted = 1; }
static void fail(const char *message) { fprintf(stderr, "quake-viewer: %s\n", message); exit(1); }
static void protect_parent(void) {
    pid_t parent = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGKILL) < 0) fail("cannot protect controller lifetime");
    /* Cover a parent exit between reading its identity and arming the signal. */
    if (getppid() != parent) raise(SIGKILL);
}
static void diagnostic(App *app, const char *event) {
    fprintf(stderr, "quake-viewer [%llu ms]: %s\n", (unsigned long long)(now_ms() - app->started), event);
}
static void set_cursor(App *app);
static void return_bot(App *app, const char *reason);
static void gpu_render(Screen *screen);
static void schedule_geometry_frame(Screen *screen);
static uint32_t gpu_ready(void *context, const struct lq_gpu_frame *frame, struct wl_buffer *const *buffers, uint32_t count);
static void gpu_error(void *context, const char *reason);
static void gpu_service(App *app);
static void gpu_capture_service(App *app);
static void gpu_liveness_service(App *app);
static void gpu_capture_failure(Screen *screen, const char *reason);
static void apply_view(Screen *screen, enum ViewMode mode, bool audible);
static void wallpaper_visibility(Screen *screen);
static void finish_layer_mapping(Screen *screen);
static void gpu_dismiss_saver(App *app);
static void saver_control_service(App *app);
static void saver_control_close(App *app);
static void saver_epoch(App *app, uint64_t generation);
static bool gpu_saver_input(const App *app) {
    return app->gpu_saver_active || app->gpu_saver_dismissed;
}

/* One ordered stream per human lease. Event callbacks only enqueue; neither
 * a stalled controller nor its 35 Hz engine may block Wayland dispatch. EOF
 * revokes the lease in the controller, so an ambiguous send is never replayed. */
static void input_close(App *app) {
    if (app->input_active) close(app->input_fd);
    app->input_active = app->input_connecting = false;
    app->input_used = app->input_sent = app->input_reply_used = 0;
    app->input_mouse_x = app->input_mouse_y = 0;
}
static void input_failed(App *app, const char *reason) {
    input_close(app); app->input_failed = true;
    return_bot(app, reason);
}
static bool input_append(App *app, const char *line) {
    if (gpu_saver_input(app) || !app->human || app->input_failed) return false;
    size_t length = strlen(line);
    if (app->input_sent) {
        memmove(app->input_queue, app->input_queue + app->input_sent, app->input_used - app->input_sent);
        app->input_used -= app->input_sent; app->input_sent = 0;
    }
    if (length > sizeof(app->input_queue) - app->input_used) {
        input_failed(app, "input queue exhausted; releasing takeover"); return false;
    }
    memcpy(app->input_queue + app->input_used, line, length); app->input_used += length;
    return true;
}
static void input_mouse_flush(App *app) {
    while (app->input_mouse_x || app->input_mouse_y) {
        int x = (int)fmax(-LQ_MOUSE_DELTA_MAX, fmin(LQ_MOUSE_DELTA_MAX, app->input_mouse_x));
        int y = (int)fmax(-LQ_MOUSE_DELTA_MAX, fmin(LQ_MOUSE_DELTA_MAX, app->input_mouse_y));
        char line[80]; snprintf(line, sizeof(line), "mouse %d %d\n", x, y);
        app->input_mouse_x -= x; app->input_mouse_y -= y;
        if (!input_append(app, line)) break;
    }
}
static void input_start(App *app) {
    if (gpu_saver_input(app) || !app->human || app->input_active || app->input_failed) return;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (fd < 0 || !app->socket_path || strlen(app->socket_path) >= sizeof(address.sun_path)) {
        if (fd >= 0) close(fd);
        input_failed(app, "cannot open input lease"); return;
    }
    strcpy(address.sun_path, app->socket_path);
    int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) {
        close(fd); input_failed(app, "input controller unavailable"); return;
    }
    app->input_fd = fd; app->input_active = true; app->input_connecting = result < 0;
    char hello[256]; snprintf(hello, sizeof(hello), "input %s\n", app->human->name);
    size_t length = strlen(hello);
    if (length + app->input_used > sizeof(app->input_queue)) {
        input_failed(app, "input queue exhausted"); return;
    }
    memmove(app->input_queue + length, app->input_queue, app->input_used);
    memcpy(app->input_queue, hello, length); app->input_used += length;
}
static void input_service(App *app) {
    input_mouse_flush(app); input_start(app);
    if (!app->input_active) return;
    if (app->input_connecting) {
        struct pollfd pfd = {.fd = app->input_fd, .events = POLLOUT};
        if (poll(&pfd, 1, 0) <= 0) return;
        int error = 0; socklen_t size = sizeof(error);
        if (getsockopt(app->input_fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) {
            input_failed(app, "input connection failed"); return;
        }
        app->input_connecting = false;
    }
    while (app->input_sent < app->input_used) {
        ssize_t sent = send(app->input_fd, app->input_queue + app->input_sent,
            app->input_used - app->input_sent, MSG_NOSIGNAL);
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) { input_failed(app, "input stream disconnected"); return; }
        app->input_sent += (size_t)sent;
    }
    if (app->input_sent == app->input_used) app->input_sent = app->input_used = 0;
    for (;;) {
        char bytes[1024]; ssize_t count = recv(app->input_fd, bytes, sizeof(bytes), 0);
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) break;
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { input_failed(app, "input lease ended"); return; }
        for (ssize_t i = 0; i < count; ++i) {
            if (bytes[i] == '\n') {
                app->input_reply[app->input_reply_used] = 0;
                if (strncmp(app->input_reply, "OK", 2)) {
                    input_failed(app, "controller rejected input lease"); return;
                }
                app->input_reply_used = 0;
            } else if (app->input_reply_used + 1 < sizeof(app->input_reply)) {
                app->input_reply[app->input_reply_used++] = bytes[i];
            } else { input_failed(app, "invalid input response"); return; }
        }
    }
}

/* Each request has its own short-lived connection: a stalled controller must
 * never prevent the Wayland client from servicing compositor events. */
static bool command(App *app, const char *request, char *reply, size_t size) {
    if (!app->socket_path) return false;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return false;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(app->socket_path) >= sizeof(address.sun_path)) { close(fd); return false; }
    strcpy(address.sun_path, app->socket_path);
    int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) { close(fd); return false; }
    struct pollfd pfd = {.fd = fd, .events = POLLOUT};
    if (result < 0 && poll(&pfd, 1, 35) <= 0) { close(fd); return false; }
    int error = 0; socklen_t error_size = sizeof(error);
    if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &error_size) || error) { close(fd); return false; }
    size_t length = strlen(request), sent = 0;
    while (sent < length) {
        ssize_t count = send(fd, request + sent, length - sent, MSG_NOSIGNAL);
        if (count <= 0) { close(fd); return false; }
        sent += (size_t)count;
    }
    if (send(fd, "\n", 1, MSG_NOSIGNAL) != 1) { close(fd); return false; }
    shutdown(fd, SHUT_WR);
    if (!reply || !size) { close(fd); return true; }
    size_t used = 0;
    bool capturing = !strncmp(request, "capture ", 8);
    uint64_t deadline = now_ms() + (capturing ? 1000 : !strncmp(request, "takeover ", 9) ? 250 : 35);
    pfd.events = POLLIN;
    while (used + 1 < size) {
        uint64_t current = now_ms();
        if (current >= deadline) break;
        if (capturing) {
            /* The engine's capture readiness wait must be able to retire a
             * raced raw candidate while this control request is outstanding.
             * Dispatch also retires real compositor references. Event
             * callbacks never issue another capture request. */
            gpu_service(app);
            if (app->quit || app->gpu.failed) break;
            while (wl_display_prepare_read(app->display) != 0)
                if (wl_display_dispatch_pending(app->display) < 0) { app->quit = true; break; }
            if (app->quit) break;
            int flushed = wl_display_flush(app->display);
            struct pollfd waits[3] = {pfd, {.fd=app->gpu_fd,.events=POLLIN},
                {.fd=wl_display_get_fd(app->display),.events=POLLIN}};
            if (flushed < 0 && errno == EAGAIN) waits[2].events |= POLLOUT;
            else if (flushed < 0) { wl_display_cancel_read(app->display); app->quit = true; break; }
            int ready = poll(waits, 3, (int)fmin(5, deadline-current));
            if (ready > 0 && (waits[2].revents & POLLIN)) {
                if (wl_display_read_events(app->display) < 0 || wl_display_dispatch_pending(app->display) < 0) app->quit = true;
            } else wl_display_cancel_read(app->display);
            if (waits[1].revents & (POLLHUP|POLLERR|POLLNVAL)) {
                gpu_error(app, "GPU producer disconnected during capture"); break;
            }
            if (waits[2].revents & (POLLHUP|POLLERR|POLLNVAL)) app->quit = true;
            if (app->quit) break;
            if (ready < 0) break;
            if (!(waits[0].revents & (POLLIN|POLLHUP|POLLERR))) continue;
        } else if (poll(&pfd, 1, (int)(deadline-current)) <= 0) break;
        ssize_t count = recv(fd, reply + used, size - used - 1, 0);
        if (count <= 0) break;
        used += (size_t)count;
        if (memchr(reply, '\n', used)) break;
    }
    reply[used] = 0;
    close(fd);
    return used > 0;
}
static void key_send_text(App *app, int key, bool down, uint32_t character, bool repeat) {
    if (key <= 0 || key > 204) return;
    char request[64];
    input_mouse_flush(app);
    snprintf(request, sizeof(request), "key %d %d %u %d\n", key, down, character, repeat);
    input_append(app, request);
}
static void key_send(App *app, int key, bool down) { key_send_text(app, key, down, 0, false); }
static void key_ref(App *app, unsigned key, bool down) {
    if (!key || key > 204) return;
    if (down) { if (app->refs[key]++ == 0) key_send(app, (int)key, true); }
    else if (app->refs[key] && --app->refs[key] == 0) key_send(app, (int)key, false);
}
static void release_keys(App *app) {
    for (int key = 1; key <= 204; ++key) if (app->refs[key]) key_send(app, key, false);
    memset(app->keys, 0, sizeof(app->keys));
    memset(app->refs, 0, sizeof(app->refs));
    app->mouse_buttons = 0;
    app->mouse_remainder_x = app->mouse_remainder_y = 0;
    app->repeat_at = 0;
}
static void release_pointer_input(App *app) {
    for (unsigned button = 0; button < 3; ++button)
        if (app->mouse_buttons & (1U << button)) key_ref(app, 200 + button, false);
    app->mouse_buttons = 0;
    app->mouse_remainder_x = app->mouse_remainder_y = 0;
}
static void release_lock(App *app) {
    /* Invalidate the proxy first, so queued stale callbacks cannot act on the
     * next ownership generation. Persistent constraints may also deactivate
     * temporarily without the controller releasing human ownership. */
    struct zwp_locked_pointer_v1 *locked = app->locked;
    struct zwp_relative_pointer_v1 *relative = app->relative;
    app->locked = NULL; app->relative = NULL; app->lock_active = false;
    if (locked) zwp_locked_pointer_v1_destroy(locked);
    if (relative) zwp_relative_pointer_v1_destroy(relative);
}
static void return_bot(App *app, const char *reason) {
    if (!app->human || app->returning_bot) return;
    app->returning_bot = true;
    char event[256]; snprintf(event, sizeof(event), "returning %s to bot: %s", app->human->name, reason);
    diagnostic(app, event);
    release_keys(app);
    input_service(app); input_close(app);
    release_lock(app);
    Screen *screen = app->human;
    app->human = NULL;
    screen->mode = VIEW_BOT; screen->redraw = true;
    schedule_geometry_frame(screen);
    if (screen->layer) {
        if (zwlr_layer_surface_v1_get_version(screen->layer) >= 2) zwlr_layer_surface_v1_set_layer(screen->layer, ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM);
        zwlr_layer_surface_v1_set_keyboard_interactivity(screen->layer, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
        wl_surface_commit(screen->surface);
    }
    set_cursor(app);
    command(app, "bot", NULL, 0);
    app->next_query = 0;
    app->returning_bot = false;
}

static void buffer_release(void *data, struct wl_buffer *wl) {
    (void)wl; ((Buffer *)data)->busy = false;
}
static const struct wl_buffer_listener buffer_listener = {.release = buffer_release};
static void buffer_destroy(Buffer *buffer) {
    wl_buffer_destroy(buffer->wl);
    munmap(buffer->pixels, buffer->bytes);
    free(buffer);
}
static void collect_buffers(App *app) {
    Buffer **link = &app->buffers;
    while (*link) {
        Buffer *buffer = *link;
        if (buffer->retired && !buffer->busy) { *link = buffer->next; buffer_destroy(buffer); }
        else link = &buffer->next;
    }
}
static Buffer *make_buffer_format(App *app, int width, int height, uint32_t format) {
    if (width <= 0 || height <= 0 || width > 16384 || height > 16384) return NULL;
    size_t bytes = (size_t)width * height * 4;
    if (bytes > INT32_MAX) return NULL;
    int fd = memfd_create("quake-desktop-pixels", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, (off_t)bytes)) { if (fd >= 0) close(fd); return NULL; }
    void *pixels = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (pixels == MAP_FAILED) { close(fd); return NULL; }
    struct wl_shm_pool *pool = wl_shm_create_pool(app->shm, fd, (int)bytes);
    Buffer *buffer = calloc(1, sizeof(*buffer));
    if (!buffer) { wl_shm_pool_destroy(pool); munmap(pixels, bytes); close(fd); return NULL; }
    buffer->wl = wl_shm_pool_create_buffer(pool, 0, width, height, width * 4, format);
    buffer->pixels = pixels; buffer->bytes = bytes;
    buffer->width = width; buffer->height = height; buffer->stride = width * 4;
    wl_buffer_add_listener(buffer->wl, &buffer_listener, buffer);
    wl_shm_pool_destroy(pool); close(fd);
    buffer->next = app->buffers; app->buffers = buffer;
    return buffer;
}
static Buffer *make_buffer(App *app, int width, int height) {
    return make_buffer_format(app, width, height, WL_SHM_FORMAT_XRGB8888);
}
static void retire_probe_buffers(Screen *screen) {
    if (screen->gpu_probe_parent) screen->gpu_probe_parent->retired = true;
    if (screen->gpu_probe_child) screen->gpu_probe_child->retired = true;
    screen->gpu_probe_parent = screen->gpu_probe_child = NULL;
    screen->gpu_failed_probe = false;
}
static void retire_screen_buffers(Screen *screen) {
    for (int i = 0; i < 2; ++i) {
        if (screen->buffers[i]) screen->buffers[i]->retired = true;
        screen->buffers[i] = NULL;
    }
}
/* Copy the producer's one-time capture of a paused or frozen head. */
static bool snapshot(Screen *screen) {
    if (!screen->capture_pending || !screen->capture_expected) return false;
    const DDHeader *shared = screen->app->shared;
    if (!shared) return false;
    /* A contested publication is sampled on the next scheduled frame. Avoid
     * repeating a full pixel copy while the publisher is still writing. */
    uint32_t seq = __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE);
    if (seq & 1) return false;
    DDHeader header;
    memcpy(&header, shared, sizeof(header));
    if (!header.capture_supported || header.capture_frame != screen->capture_expected) return false;
    header.width = header.capture_width; header.height = header.capture_height;
    header.stride = header.width * 4; header.frame = header.capture_frame;
    if (header.magic != DD_MAGIC || header.version != DD_VERSION || !header.width || !header.height
        || header.width > DD_MAX_WIDTH || header.height > DD_MAX_HEIGHT) return false;
    if (!screen->snapshot) { screen->snapshot = malloc(DD_FRAME_BYTES); if (!screen->snapshot) return false; }
    const uint8_t *source = (const uint8_t *)shared + DD_HEADER_SIZE;
    for (uint32_t y = 0; y < header.height; ++y)
        memcpy(screen->snapshot + (size_t)y * header.width, source + (size_t)y * header.stride, header.width * 4);
    __atomic_thread_fence(__ATOMIC_ACQUIRE);
    if (seq != __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE)) return false;
    header.map[sizeof(header.map) - 1] = 0;
    screen->header = header; screen->last_frame = header.frame; screen->snapshot_valid = true;
    screen->cpu_snapshot = true;
    screen->blur_valid = false;
    return true;
}
typedef struct { double x, y, width, height, aspect; bool top_cropped; } GameBox;
static GameBox game_box(const Screen *screen) {
    double aspect = 4.0 / 3.0;
    if (screen->snapshot_valid && screen->header.width && screen->header.height) {
        uint32_t num = screen->header.pixel_aspect_num, den = screen->header.pixel_aspect_den;
        if (!num || !den || num > 65535 || den > 65535 ||
            (double)num / den < .25 || (double)num / den > 4) num = den = 1;
        aspect = (double)screen->header.width * num / ((double)screen->header.height * den);
    }
    if (screen->mode != VIEW_HUMAN) {
        /* Wallpaper and saver fill the output without stretching, with the
         * HUD hidden by the engine and both crop directions centered. */
        double width = fmax(screen->width, screen->height * aspect), height = width / aspect;
        return (GameBox){(screen->width - width) / 2, (screen->height - height) / 2,
            width, height, aspect, height > screen->height + .001};
    }
    double width = fmin(screen->width, screen->height * aspect), height = width / aspect;
    return (GameBox){(screen->width - width) / 2, (screen->height - height) / 2, width, height, aspect, false};
}
/* Covered heads can withhold frame callbacks, so a frozen or paused capture is
 * blurred completely at once rather than animated. No new capture is needed. */
static void blur_update(Screen *screen) {
    double target = (screen->mode == VIEW_PAUSED || screen->gpu_frozen) && screen->blur_strength > 0 ? 1 : 0;
    if (screen->blur_mix != target) screen->redraw = true;
    screen->blur_mix = target;
}
static void blur_pass(const uint32_t *source, uint32_t *target, int width, int height, double extent, bool vertical) {
    int lines = vertical ? width : height, length = vertical ? height : width;
    int radius = (int)extent; double fraction = extent - radius;
    for (int line = 0; line < lines; ++line) {
        int sum[3] = {0};
        for (int i = -radius; i <= radius; ++i) {
            int at = i < 0 ? 0 : i;
            uint32_t pixel = source[vertical ? (size_t)at * width + line : (size_t)line * width + at];
            sum[0] += pixel & 255; sum[1] += (pixel >> 8) & 255; sum[2] += (pixel >> 16) & 255;
        }
        double count = radius * 2 + 1 + fraction * 2;
        for (int at = 0; at < length; ++at) {
            int edge_left = at - radius - 1, edge_right = at + radius + 1;
            if (edge_left < 0) edge_left = 0;
            if (edge_right >= length) edge_right = length - 1;
            uint32_t left = source[vertical ? (size_t)edge_left * width + line : (size_t)line * width + edge_left];
            uint32_t right = source[vertical ? (size_t)edge_right * width + line : (size_t)line * width + edge_right];
            uint32_t result = 0;
            for (int color = 0; color < 3; ++color) result |= (uint32_t)((sum[color] + fraction *
                (((left >> (color * 8)) & 255) + ((right >> (color * 8)) & 255))) / count) << (color * 8);
            target[vertical ? (size_t)at * width + line : (size_t)line * width + at] = result;
            int leave = at - radius, enter = at + radius + 1;
            if (leave < 0) leave = 0;
            if (enter >= length) enter = length - 1;
            uint32_t old = source[vertical ? (size_t)leave * width + line : (size_t)line * width + leave];
            uint32_t next = source[vertical ? (size_t)enter * width + line : (size_t)line * width + enter];
            for (int color = 0; color < 3; ++color) sum[color] += (int)((next >> (color * 8)) & 255) - (int)((old >> (color * 8)) & 255);
        }
    }
}
static bool prepare_blur(Screen *screen) {
    if (!screen->snapshot_valid || screen->blur_strength <= 0) return false;
    if (screen->blur_valid && screen->blur_cached_strength == screen->blur_strength) return true;
    int width = (int)screen->header.width, height = (int)screen->header.height;
    double factor = fmin(1, 640.0 / fmax(width, height));
    int bw = fmax(1, ceil(width * factor)), bh = fmax(1, ceil(height * factor));
    size_t bytes = (size_t)bw * bh * 4;
    uint32_t *pixels = malloc(bytes), *scratch = malloc(bytes);
    if (!pixels || !scratch) { free(pixels); free(scratch); return false; }
    for (int y = 0; y < bh; ++y) for (int x = 0; x < bw; ++x)
        pixels[(size_t)y * bw + x] = screen->snapshot[(size_t)(y * height / bh) * width + x * width / bw];
    double radius = screen->blur_strength / 20.0;
    if (radius >= bw) radius = bw - 1;
    if (radius >= bh) radius = bh - 1;
    for (int pass = 0; pass < 3; ++pass) {
        blur_pass(pixels, scratch, bw, bh, radius, false);
        blur_pass(scratch, pixels, bw, bh, radius, true);
    }
    free(scratch); free(screen->blurred); screen->blurred = pixels;
    screen->blur_width = bw; screen->blur_height = bh;
    screen->blur_cached_strength = screen->blur_strength; screen->blur_valid = true;
    return true;
}
static void paint(Screen *screen, Buffer *buffer) {
    cairo_surface_t *target = cairo_image_surface_create_for_data(buffer->pixels, CAIRO_FORMAT_RGB24,
        buffer->width, buffer->height, buffer->stride);
    cairo_t *cr = cairo_create(target);
    cairo_scale(cr, screen->scale, screen->scale);
    cairo_set_source_rgb(cr, 0, 0, 0);
    cairo_paint(cr);
    GameBox box = game_box(screen);
    if (screen->snapshot_valid) {
        cairo_surface_t *source = cairo_image_surface_create_for_data((unsigned char *)screen->snapshot,
            CAIRO_FORMAT_RGB24, (int)screen->header.width, (int)screen->header.height, (int)screen->header.width * 4);
        cairo_save(cr); cairo_translate(cr, box.x, box.y);
        cairo_scale(cr, box.width / screen->header.width, box.height / screen->header.height);
        cairo_set_source_surface(cr, source, 0, 0);
        cairo_pattern_set_filter(cairo_get_source(cr), CAIRO_FILTER_NEAREST);
        cairo_paint(cr); cairo_restore(cr); cairo_surface_destroy(source);
        if (screen->blur_mix > 0 && prepare_blur(screen)) {
            cairo_surface_t *blur = cairo_image_surface_create_for_data((unsigned char *)screen->blurred,
                CAIRO_FORMAT_RGB24, screen->blur_width, screen->blur_height, screen->blur_width * 4);
            cairo_save(cr); cairo_translate(cr, box.x, box.y);
            cairo_scale(cr, box.width / screen->blur_width, box.height / screen->blur_height);
            cairo_set_source_surface(cr, blur, 0, 0);
            cairo_pattern_set_filter(cairo_get_source(cr), CAIRO_FILTER_BILINEAR);
            cairo_paint_with_alpha(cr, screen->blur_mix);
            cairo_restore(cr); cairo_surface_destroy(blur);
        }
    }
    cairo_destroy(cr); cairo_surface_flush(target); cairo_surface_destroy(target);
}
/* A saver receipt describes feedback for a committed Quake image. Configure,
 * focus and the black GPU parent cannot produce this receipt. */
static bool saver_header(const DDHeader *header) {
    return header->magic == DD_MAGIC && header->version == DD_VERSION
        && header->pid && header->frame && header->width && header->height
        && header->width <= DD_MAX_WIDTH && header->height <= DD_MAX_HEIGHT
        && header->map[0] && !header->console_visible && (header->gamestate == 0 || header->gamestate == 2);
}
static bool saver_active(const Screen *screen) {
    const App *app = screen->app;
    return app->saver_generation && app->saver_cycle && !app->quit && !screen->removed
        && app->gpu_saver_active && !app->gpu_saver_dismissed && screen->mode == VIEW_SAVER;
}
static void saver_image_callback(Screen *screen) {
    screen->saver_callback_cycle = saver_active(screen) && screen->snapshot_valid
        && (!screen->layer || (screen->mapped && zwlr_layer_surface_v1_get_version(screen->layer) >= 2))
        && screen->gpu_attached && !screen->gpu_frozen
        && !screen->capture_failed && !screen->gpu_failed_probe
        && saver_header(&screen->header) ? screen->app->saver_cycle : 0;
}
static void frame_done(void *data, struct wl_callback *callback, uint32_t time) {
    (void)time;
    Screen *screen = data;
    if (callback != screen->callback) { wl_callback_destroy(callback); return; }
    if (screen->saver_callback_cycle == screen->app->saver_cycle
        && saver_active(screen) && screen->mapped)
        screen->saver_presented_cycle = screen->saver_callback_cycle;
    screen->saver_callback_cycle = 0;
    if (screen->gpu_frozen && !screen->callback_gpu && !screen->capture_pending &&
        ((screen->cpu_snapshot && !screen->capture_failed) || screen->gpu_failed_probe)) {
        uint64_t current = now_ms();
        if (current - screen->gpu_resume_last > 250) screen->gpu_resume_callbacks = 0;
        screen->gpu_resume_last = current;
        ++screen->gpu_resume_callbacks;
        screen->gpu_resume_pending = screen->gpu_resume_callbacks >= 2 && current - screen->gpu_frozen_at >= 500;
    }
    wl_callback_destroy(callback); screen->callback = NULL; screen->ready = true;
    screen->callback_gpu = false; screen->gpu_callback_at = 0;
}
static const struct wl_callback_listener frame_listener = {.done = frame_done};
static void cancel_frame_callback(Screen *screen) {
    if (screen->callback) { wl_callback_destroy(screen->callback); screen->callback = NULL; }
    screen->saver_callback_cycle = 0;
    screen->callback_gpu = false; screen->gpu_callback_at = 0;
}
/* GPU image pixels stay in producer BOs/compositor textures. Only this immutable
 * black policy parent is SHM; its one pixel is never overwritten or per-frame
 * copied. A child viewport preserves human contain and wallpaper centered cover. */
static void gpu_render(Screen *screen) {
    if (screen->app->gpu_saver_dismissed || screen->mode == VIEW_HIDDEN || screen->mode == VIEW_PAUSED || screen->gpu_frozen || screen->capture_failed || screen->removed || !screen->configured || !screen->gpu_attached
        || screen->gpu_wait_serial || !screen->redraw || !screen->snapshot_valid) return;
    bool first_mapping = !screen->mapped;
    /* Pending async import reserves this head's readiness. Redrawing its old
     * image would arm a callback before import completion and split the new
     * frame across outputs, leaving distinct producer BOs pinned. */
    bool attach_background = !screen->gpu_background || screen->gpu_background_restore;
    if (!screen->gpu_background) {
        screen->gpu_background = make_buffer(screen->app, 1, 1);
        if (!screen->gpu_background) { gpu_error(screen->app, "cannot allocate immutable GPU background"); return; }
        *(uint32_t *)screen->gpu_background->pixels = 0;
    }
    double width = screen->header.width, height = screen->header.height;
    GameBox box = game_box(screen);
    double source_width = width, source_height = height, x = 0, y = 0;
    int destination_width = screen->width, destination_height = screen->height;
    int position_x = 0, position_y = 0;
    if (screen->mode == VIEW_HUMAN) {
        destination_width = (int)lround(box.width); destination_height = (int)lround(box.height);
        position_x = (int)lround(box.x); position_y = (int)lround(box.y);
    } else {
        source_width = width * screen->width / box.width;
        source_height = height * screen->height / box.height;
        x = (width - source_width) / 2; y = (height - source_height) / 2;
    }
    if (destination_width < 1) destination_width = 1;
    if (destination_height < 1) destination_height = 1;
    wl_fixed_t fx = wl_fixed_from_double(x), fy = wl_fixed_from_double(y);
    wl_fixed_t fw = wl_fixed_from_double(source_width), fh = wl_fixed_from_double(source_height);
    /* Independent 24.8 rounding must never place the crop beyond its BO. */
    if (fx < 0) fx = 0;
    if (fy < 0) fy = 0;
    if (fw > (int32_t)screen->header.width * 256 - fx) fw = (int32_t)screen->header.width * 256 - fx;
    if (fh > (int32_t)screen->header.height * 256 - fy) fh = (int32_t)screen->header.height * 256 - fy;
    if (fw < 1 || fh < 1) { gpu_error(screen->app, "GPU source crop underflow"); return; }
    wp_viewport_set_source(screen->gpu_viewport, fx, fy, fw, fh);
    wp_viewport_set_destination(screen->gpu_viewport, destination_width, destination_height);
    wl_subsurface_set_position(screen->gpu_subsurface, position_x, position_y);
    wl_surface_set_buffer_scale(screen->gpu_surface, 1);
    wl_surface_set_buffer_transform(screen->gpu_surface, screen->gpu_flags & LQ_GPU_FLAG_Y_INVERT
        ? WL_OUTPUT_TRANSFORM_FLIPPED_180 : WL_OUTPUT_TRANSFORM_NORMAL);
    /* The opaque image occludes its one-pixel parent. Pace against the
     * visible child, whose commit owns the requested callback. */
    if (screen->ready && !screen->callback) {
        screen->callback = wl_surface_frame(screen->gpu_surface);
        wl_callback_add_listener(screen->callback, &frame_listener, screen);
        saver_image_callback(screen);
        screen->ready = false;
        screen->callback_gpu = true; screen->gpu_callback_at = now_ms();
    }
    /* A geometry-only redraw must schedule child feedback on an idle output. */
    wl_surface_damage_buffer(screen->gpu_surface, 0, 0, 1, 1);
    wl_surface_commit(screen->gpu_surface);
    wp_viewport_set_destination(screen->background_viewport, screen->width, screen->height);
    wl_surface_set_buffer_scale(screen->surface, 1);
    if (!screen->mapped || attach_background) {
        screen->gpu_background->busy = true;
        wl_surface_attach(screen->surface, screen->gpu_background->wl, 0, 0);
        wl_surface_damage_buffer(screen->surface, 0, 0, 1, 1);
    }
    screen->redraw = false; screen->mapped = true;
    screen->gpu_background_restore = false;
    wl_surface_commit(screen->surface);
    if (first_mapping && screen->layer) finish_layer_mapping(screen);
}
static void gpu_error(void *context, const char *reason) {
    App *app = context; app->gpu.failed = true; app->quit = true;
    diagnostic(app, reason);
}
static bool gpu_eligible(const Screen *screen) {
    return !screen->app->gpu_saver_dismissed && !screen->removed && !screen->gpu_frozen && !screen->capture_failed && screen->configured && screen->ready && !screen->gpu_wait_serial && screen->mode != VIEW_HIDDEN
        && (screen->mode == VIEW_BOT || screen->mode == VIEW_HUMAN || screen->mode == VIEW_SAVER);
}
static uint32_t gpu_ready(void *context, const struct lq_gpu_frame *frame,
        struct wl_buffer *const *buffers, uint32_t count) {
    App *app = context; uint32_t mask = 0;
    for (uint32_t i = 0; i < count; ++i) {
        /* Async imports may complete after an output was removed/remapped.
         * Resolve its retained global identity, never a stale Screen pointer. */
        Screen *screen = app->screens;
        while (screen && screen->gpu_identity != app->gpu_targets[frame->slot][i]) screen = screen->next;
        if (!screen || screen->gpu_wait_serial != frame->serial) continue;
        screen->gpu_wait_serial = 0;
        if (!gpu_eligible(screen)) continue;
        if (app->shared) {
            uint32_t seq = __atomic_load_n(&app->shared->seq, __ATOMIC_ACQUIRE);
            if (!(seq & 1)) {
                DDHeader metadata; memcpy(&metadata, app->shared, sizeof(metadata));
                __atomic_thread_fence(__ATOMIC_ACQUIRE);
                if (seq == __atomic_load_n(&app->shared->seq, __ATOMIC_ACQUIRE) && metadata.magic == DD_MAGIC) {
                    metadata.map[sizeof(metadata.map) - 1] = 0; screen->header = metadata;
                }
            }
        }
        screen->header.width = frame->width; screen->header.height = frame->height;
        screen->header.pixel_aspect_num = frame->par_num; screen->header.pixel_aspect_den = frame->par_den;
        /* The current buffer can remain attached while another head holds
         * the barrier. Its release deadline starts only when replaced. */
        uint64_t replaced_at = now_ms();
        for (unsigned slot = 0; slot < LQ_GPU_SLOTS; ++slot) {
            const struct lq_gpu_wayland_slot *held = &app->gpu.slots[slot];
            if (!held->occupied || held->frame.serial >= frame->serial) continue;
            for (unsigned consumer = 0; consumer < LQ_GPU_CONSUMERS; ++consumer)
                if (held->consumers[consumer].committed &&
                    app->gpu_targets[slot][consumer] == screen->gpu_identity &&
                    !app->gpu_superseded_at[slot][consumer])
                    app->gpu_superseded_at[slot][consumer] = replaced_at;
        }
        screen->gpu_flags = frame->flags; screen->last_frame = frame->serial;
        screen->snapshot_valid = screen->gpu_attached = true; screen->redraw = true;
        wl_surface_attach(screen->gpu_surface, buffers[i], 0, 0);
        wl_surface_damage_buffer(screen->gpu_surface, 0, 0, (int32_t)frame->width, (int32_t)frame->height);
        gpu_render(screen); mask |= 1u << i;
    }
    return mask;
}
/* At most one raw producer lease is retained until a positive view and fresh
 * configure permit import. Initial hidden/unknown policy never displays it.
 * Replacing a raw, never-imported frame needs no compositor release, but its
 * protocol ACK is still queued/confirmed through normal slot accounting. */
static bool gpu_cache_accept(App *app, struct lq_gpu_frame *frame) {
    bool advertised = false;
    for (uint32_t i = 0; i < app->gpu.format_count; ++i)
        if (app->gpu.formats[i].format == frame->format && app->gpu.formats[i].modifier == frame->modifier) advertised = true;
    if (!lq_gpu_frame_valid(frame) || !advertised || frame->generation != app->gpu_generation
        || frame->serial <= app->gpu_received_serial || app->gpu.slots[frame->slot].occupied
        || (app->gpu_cached_valid && frame->slot == app->gpu_cached.slot)) {
        lq_gpu_close_frame(frame); gpu_error(app, "Invalid or overlapping cached GPU producer lease"); return false;
    }
    if (app->gpu_cached_valid) {
        app->gpu_cached_valid = false;
        if (lq_gpu_wayland_import(&app->gpu, &app->gpu_cached, 0) < 0) {
            lq_gpu_close_frame(frame); gpu_error(app, "Cannot acknowledge superseded uncommitted GPU frame"); return false;
        }
    }
    app->gpu_cached = *frame; app->gpu_cached_valid = true; app->gpu_received_serial = frame->serial;
    for (uint32_t i = 0; i < LQ_GPU_PLANES; ++i) frame->fd[i] = -1;
    return true;
}
static void gpu_try_cached(App *app) {
    if (!app->gpu_cached_valid || app->gpu.failed || app->quit) return;
    /* Release a raw candidate before capture. Keeping the second BO here
     * while the first is still live would prevent the producer redraw. */
    bool capturing = app->capture_rendering, live = false, paused = false, receipt = false;
    for (Screen *screen = app->screens; screen; screen = screen->next)
        receipt |= screen->capture_pending && screen->capture_expected && !screen->removed;
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        capturing |= !receipt && screen->capture_pending && !screen->capture_expected && screen->configured
            && !screen->removed && now_ms() - screen->capture_started_at < 1500;
        paused |= !screen->removed && (screen->mode == VIEW_PAUSED || screen->gpu_frozen || screen->capture_failed);
        live |= !screen->removed && !screen->gpu_frozen && !screen->capture_failed && screen->configured
            && (screen->mode == VIEW_BOT || screen->mode == VIEW_HUMAN || screen->mode == VIEW_SAVER);
    }
    if (capturing || (paused && !live)) {
        app->gpu_cached_valid = false;
        if (lq_gpu_wayland_import(&app->gpu, &app->gpu_cached, 0) < 0) gpu_error(app, "Cannot release uncopied capture candidate");
        return;
    }
    /* A direct-attached producer frame must replace all visible configured
     * consumers together. Otherwise differently paced outputs can pin both
     * BOs in a driver-controlled swapchain and prevent the next frame. Wait
     * on positive visible heads only; hidden/removed/unconfigured heads and
     * a dismissed saver cannot impose this readiness barrier. */
    for (Screen *screen = app->screens; screen; screen = screen->next)
        if (!app->gpu_saver_dismissed && !screen->removed && !screen->gpu_frozen && !screen->capture_failed && screen->configured
            && (screen->mode == VIEW_BOT || screen->mode == VIEW_HUMAN || screen->mode == VIEW_SAVER)
            && (!screen->ready || screen->gpu_wait_serial)) return;
    uint32_t consumers = 0;
    for (Screen *screen = app->screens; screen; screen = screen->next) if (gpu_eligible(screen)) {
        if (consumers == LQ_GPU_CONSUMERS) { gpu_error(app, "GPU output consumer limit exceeded"); return; }
        app->gpu_targets[app->gpu_cached.slot][consumers++] = screen->gpu_identity;
    }
    if (!consumers) return; /* No positive eligible policy: retain one lease. */
    for (Screen *screen = app->screens; screen; screen = screen->next) if (gpu_eligible(screen)) screen->gpu_wait_serial = app->gpu_cached.serial;
    memset(app->gpu_superseded_at[app->gpu_cached.slot], 0, sizeof(app->gpu_superseded_at[app->gpu_cached.slot]));
    app->gpu_import_at[app->gpu_cached.slot] = now_ms(); app->gpu_cached_valid = false;
    if (lq_gpu_wayland_import(&app->gpu, &app->gpu_cached, consumers) < 0) gpu_error(app, "GPU cached frame import rejected");
}
static void gpu_close_cached(App *app) {
    if (app->gpu_cached_valid) { lq_gpu_close_frame(&app->gpu_cached); app->gpu_cached_valid = false; }
    /* No ACK: the producer must retire its outstanding pool on disconnect. */
}
static void gpu_service(App *app) {
    if (app->gpu.failed || app->quit) return;
    if (!app->gpu.capabilities_sent) {
        if (now_ms() - app->started >= 5000 || lq_gpu_wayland_send_capabilities(&app->gpu, app->gpu_fd, app->gpu_generation) < 0) {
            gpu_error(app, "GPU capability handshake failed or expired"); return;
        }
        if (!app->gpu.capabilities_sent) return;
    }
    for (unsigned i = 0; i < LQ_GPU_SLOTS; ++i) {
        if (app->gpu.slots[i].occupied && app->gpu.slots[i].pending && now_ms() - app->gpu_import_at[i] >= 2000) {
            gpu_error(app, "GPU asynchronous import deadline expired"); return;
        }
    }
    struct lq_gpu_release release;
    for (unsigned i = 0; i < LQ_GPU_SLOTS && lq_gpu_wayland_next_release(&app->gpu, &release); ++i) {
        int sent = lq_gpu_send_release(app->gpu_fd, &release);
        if (sent < 0) { gpu_error(app, "GPU producer disconnected during release"); return; }
        if (!sent) break;
        if (lq_gpu_wayland_release_sent(&app->gpu, &release) < 0) { gpu_error(app, "GPU release accounting failed"); return; }
    }
    for (unsigned i = 0; i < LQ_GPU_SLOTS; ++i) {
        struct lq_gpu_packet packet; int received = lq_gpu_receive(app->gpu_fd, &packet);
        if (received < 0) { gpu_error(app, "GPU producer disconnected or sent invalid metadata"); return; }
        if (!received) break;
        if (packet.kind != LQ_GPU_FRAME) { gpu_error(app, "Unexpected GPU producer packet"); return; }
        if (!gpu_cache_accept(app, &packet.frame)) return;
    }
    gpu_try_cached(app);
}
static void render(Screen *screen) {
    if (screen->mode != VIEW_PAUSED && !screen->gpu_frozen) { gpu_render(screen); return; }
    if (!screen->cpu_snapshot || screen->capture_pending || screen->capture_failed) return;
    if (!screen->configured || screen->removed || !screen->ready || !screen->redraw) return;
    bool first_mapping = !screen->mapped;
    int width = screen->width * screen->scale, height = screen->height * screen->scale;
    if (screen->buffers[0] && (screen->buffers[0]->width != width || screen->buffers[0]->height != height)) retire_screen_buffers(screen);
    Buffer *buffer = NULL;
    for (int i = 0; i < 2; ++i) {
        if (!screen->buffers[i]) screen->buffers[i] = make_buffer(screen->app, width, height);
        if (screen->buffers[i] && !screen->buffers[i]->busy) { buffer = screen->buffers[i]; break; }
    }
    if (!buffer) return;
    paint(screen, buffer);
    struct wl_surface *target = screen->gpu_surface;
    /* Replace the producer buffer directly with painted SHM on the same
     * visible child. Its callback is also our recovery probe: layer-root
     * feedback can arrive while a covered child is still unrendered. */
    wp_viewport_set_source(screen->gpu_viewport, wl_fixed_from_int(-1), wl_fixed_from_int(-1),
        wl_fixed_from_int(-1), wl_fixed_from_int(-1));
    wp_viewport_set_destination(screen->gpu_viewport, -1, -1);
    wl_subsurface_set_position(screen->gpu_subsurface, 0, 0);
    wl_surface_set_buffer_transform(target, WL_OUTPUT_TRANSFORM_NORMAL);
    if (!screen->gpu_background) {
        screen->gpu_background = make_buffer(screen->app, 1, 1);
        if (!screen->gpu_background) { gpu_capture_failure(screen, "background-allocation"); return; }
        *(uint32_t *)screen->gpu_background->pixels = 0;
    }
    wp_viewport_set_destination(screen->background_viewport, screen->width, screen->height);
    wl_surface_set_buffer_scale(screen->surface, 1);
    if (!screen->mapped || screen->gpu_background_restore) {
        screen->gpu_background->busy = true;
        wl_surface_attach(screen->surface, screen->gpu_background->wl, 0, 0);
        wl_surface_damage_buffer(screen->surface, 0, 0, 1, 1);
    }
    screen->gpu_background_restore = false;
    wl_surface_set_buffer_scale(target, screen->scale);
    buffer->busy = true;
    wl_surface_attach(target, buffer->wl, 0, 0);
    wl_surface_damage_buffer(target, 0, 0, width, height);
    screen->callback = wl_surface_frame(target);
    wl_callback_add_listener(screen->callback, &frame_listener, screen);
    saver_image_callback(screen);
    screen->ready = false; screen->redraw = false;
    screen->callback_gpu = false; screen->gpu_callback_at = now_ms();
    screen->mapped = true;
    wl_surface_commit(target);
    screen->gpu_attached = false; wl_surface_commit(screen->surface);
    if (first_mapping && screen->layer) finish_layer_mapping(screen);
}

static void relative_motion(void *data, struct zwp_relative_pointer_v1 *relative, uint32_t hi, uint32_t lo,
        wl_fixed_t dx, wl_fixed_t dy, wl_fixed_t unaccel_x, wl_fixed_t unaccel_y) {
    (void)hi; (void)lo; (void)dx; (void)dy;
    App *app = data;
    if (relative != app->relative || !app->human || app->pointer_focus != app->human || gpu_saver_input(app)) return;
    app->mouse_remainder_x += wl_fixed_to_double(unaccel_x);
    app->mouse_remainder_y += wl_fixed_to_double(unaccel_y);
    int x = (int)app->mouse_remainder_x, y = (int)app->mouse_remainder_y;
    if (x || y) {
        app->input_mouse_x += x; app->input_mouse_y += y;
        app->mouse_remainder_x -= x; app->mouse_remainder_y -= y;
    }
}
static const struct zwp_relative_pointer_v1_listener relative_listener = {.relative_motion = relative_motion};
static void pointer_locked(void *data, struct zwp_locked_pointer_v1 *pointer) {
    App *app = data;
    if (pointer != app->locked) return;
    app->lock_active = true;
    diagnostic(app, "relative mouse constraint activated");
}
static void pointer_unlocked(void *data, struct zwp_locked_pointer_v1 *pointer) {
    App *app = data;
    if (pointer != app->locked) return;
    app->lock_active = false;
    release_pointer_input(app);
    diagnostic(app, "relative mouse constraint deactivated; controller retains ownership authority");
}
static const struct zwp_locked_pointer_v1_listener locked_listener = {.locked = pointer_locked, .unlocked = pointer_unlocked};
static void ensure_lock(App *app) {
    if (gpu_saver_input(app) || !app->human || !app->pointer || app->locked || !app->constraints || !app->relative_manager) return;
    app->relative = zwp_relative_pointer_manager_v1_get_relative_pointer(app->relative_manager, app->pointer);
    zwp_relative_pointer_v1_add_listener(app->relative, &relative_listener, app);
    app->locked = zwp_pointer_constraints_v1_lock_pointer(app->constraints, app->human->surface,
        app->pointer, NULL, ZWP_POINTER_CONSTRAINTS_V1_LIFETIME_PERSISTENT);
    zwp_locked_pointer_v1_add_listener(app->locked, &locked_listener, app);
    wl_surface_commit(app->human->surface);
}
static Screen *screen_for_surface(App *app, struct wl_surface *surface) {
    for (Screen *screen = app->screens; screen; screen = screen->next) if (screen->surface == surface || screen->gpu_surface == surface) return screen;
    return NULL;
}
static void set_cursor(App *app) {
    if (!app->pointer || !app->pointer_focus || !app->cursor_serial) return;
    if (gpu_saver_input(app) || app->human) {
        wl_pointer_set_cursor(app->pointer, app->cursor_serial, NULL, 0, 0);
    } else if (app->cursor && app->cursor_surface) {
        struct wl_cursor_image *image = app->cursor->images[0];
        wl_pointer_set_cursor(app->pointer, app->cursor_serial, app->cursor_surface, image->hotspot_x, image->hotspot_y);
        wl_surface_attach(app->cursor_surface, wl_cursor_image_get_buffer(image), 0, 0);
        wl_surface_damage(app->cursor_surface, 0, 0, image->width, image->height);
        wl_surface_commit(app->cursor_surface);
    }
}
static void pointer_enter(void *data, struct wl_pointer *pointer, uint32_t serial,
        struct wl_surface *surface, wl_fixed_t x, wl_fixed_t y) {
    App *app = data;
    if (gpu_saver_input(app) && app->pointer_seen && now_ms() - app->gpu_saver_started > 500) gpu_dismiss_saver(app);
    app->pointer_focus = screen_for_surface(app, surface);
    char event[256]; snprintf(event, sizeof(event), "pointer entered %s at %.0f,%.0f", app->pointer_focus ? app->pointer_focus->name : "unknown surface", wl_fixed_to_double(x), wl_fixed_to_double(y));
    diagnostic(app, event);
    app->pointer_x = wl_fixed_to_double(x); app->pointer_y = wl_fixed_to_double(y);
    app->pointer_seen = true;
    (void)pointer; app->cursor_serial = serial; set_cursor(app);
    ensure_lock(app);
}
static void pointer_leave(void *data, struct wl_pointer *pointer, uint32_t serial, struct wl_surface *surface) {
    (void)pointer; (void)serial; (void)surface;
    App *app = data;
    release_pointer_input(app);
    app->pointer_focus = NULL; app->cursor_serial = 0;
}
static void pointer_motion(void *data, struct wl_pointer *pointer, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
    (void)pointer; (void)time;
    App *app = data;
    double px = wl_fixed_to_double(x), py = wl_fixed_to_double(y);
    if (gpu_saver_input(app) && app->pointer_seen && now_ms() - app->gpu_saver_started > 500
        && (fabs(px - app->pointer_x) > .25 || fabs(py - app->pointer_y) > .25)) gpu_dismiss_saver(app);
    app->pointer_x = px; app->pointer_y = py; app->pointer_seen = true;
}
static void pointer_button(void *data, struct wl_pointer *pointer, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
    (void)pointer; (void)time;
    App *app = data;
    if (gpu_saver_input(app)) { gpu_dismiss_saver(app); return; }
    Screen *screen = app->pointer_focus;
    char event[256]; snprintf(event, sizeof(event), "pointer button %u state=%u on %s mode=%d", button, state, screen ? screen->name : "no surface", screen ? (int)screen->mode : -1);
    diagnostic(app, event);
    if (!screen || screen->mode == VIEW_HIDDEN) return;
    bool down = state == WL_POINTER_BUTTON_STATE_PRESSED;
    if (button == BTN_RIGHT && down && app->human != screen) {
        char request[256]; snprintf(request, sizeof(request), "settings %s", screen->name);
        command(app, request, NULL, 0); return;
    }
    if (screen->mode == VIEW_PAUSED || screen->gpu_frozen || screen->capture_failed) return;
    if (app->human == screen && (button == BTN_LEFT || button == BTN_RIGHT || button == BTN_MIDDLE)) {
        unsigned identity = button == BTN_LEFT ? 0 : button == BTN_RIGHT ? 1 : 2;
        unsigned mask = 1U << identity;
        if (down != !!(app->mouse_buttons & mask)) {
            if (down) app->mouse_buttons |= mask; else app->mouse_buttons &= ~mask;
            key_ref(app, 200 + identity, down);
        }
    } else if (button == BTN_LEFT && down && screen->mode == VIEW_BOT) {
        char request[256], reply[128];
        snprintf(request, sizeof(request), "takeover %s", screen->name);
        if (command(app, request, reply, sizeof(reply)) && !strncmp(reply, "OK", 2)) {
            /* Hide immediately using the serial from the initiating click. */
            wl_pointer_set_cursor(app->pointer, serial, NULL, 0, 0);
            app->next_query = 0;
        }
    }
}
static void pointer_axis(void *data, struct wl_pointer *pointer, uint32_t time, uint32_t axis, wl_fixed_t value) {
    (void)pointer; (void)time; (void)axis; (void)value;
    App *app = data;
    if (gpu_saver_input(app)) gpu_dismiss_saver(app);
}
static void pointer_frame(void *data, struct wl_pointer *pointer) { (void)data; (void)pointer; }
static void pointer_axis_source(void *data, struct wl_pointer *pointer, uint32_t source) { (void)data; (void)pointer; (void)source; }
static void pointer_axis_stop(void *data, struct wl_pointer *pointer, uint32_t time, uint32_t axis) { (void)data; (void)pointer; (void)time; (void)axis; }
static void pointer_axis_discrete(void *data, struct wl_pointer *pointer, uint32_t axis, int32_t discrete) { (void)data; (void)pointer; (void)axis; (void)discrete; }
static const struct wl_pointer_listener pointer_listener = {
    .enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion, .button = pointer_button,
    .axis = pointer_axis, .frame = pointer_frame, .axis_source = pointer_axis_source,
    .axis_stop = pointer_axis_stop, .axis_discrete = pointer_axis_discrete,
};
static void keyboard_keymap(void *data, struct wl_keyboard *keyboard, uint32_t format, int32_t fd, uint32_t size) {
    (void)keyboard;
    App *app = data;
    if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1 || !size) { close(fd); return; }
    char *mapping = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
    close(fd);
    if (mapping == MAP_FAILED) return;
    struct xkb_keymap *keymap = xkb_keymap_new_from_string(app->xkb_context, mapping,
        XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
    munmap(mapping, size);
    if (!keymap) return;
    struct xkb_state *state = xkb_state_new(keymap);
    if (!state) { xkb_keymap_unref(keymap); return; }
    xkb_state_unref(app->xkb_state); xkb_keymap_unref(app->keymap);
    app->keymap = keymap; app->xkb_state = state;
}
static void keyboard_enter(void *data, struct wl_keyboard *keyboard, uint32_t serial, struct wl_surface *surface, struct wl_array *keys) {
    (void)keyboard; (void)serial; (void)keys;
    App *app = data; app->keyboard_focus = screen_for_surface(app, surface);
    if (app->keyboard_focus) diagnostic(app, "game keyboard focus entered");
}
static void keyboard_leave(void *data, struct wl_keyboard *keyboard, uint32_t serial, struct wl_surface *surface) {
    (void)keyboard; (void)serial; (void)surface;
    App *app = data; app->keyboard_focus = NULL;
    if (app->human) { release_keys(app); diagnostic(app, "human keyboard focus left"); }
}
static unsigned quake_key(xkb_keysym_t symbol) {
    symbol = xkb_keysym_to_lower(symbol);
    switch (symbol) {
    case XKB_KEY_Up: return 128; case XKB_KEY_Down: return 129;
    case XKB_KEY_Left: return 130; case XKB_KEY_Right: return 131;
    case XKB_KEY_Shift_L: case XKB_KEY_Shift_R: return 134;
    case XKB_KEY_Control_L: case XKB_KEY_Control_R: return 133;
    case XKB_KEY_Alt_L: case XKB_KEY_Alt_R: return 132;
    case XKB_KEY_Return: case XKB_KEY_KP_Enter: return 13;
    case XKB_KEY_Tab: return 9; case XKB_KEY_BackSpace: return 127;
    case XKB_KEY_Escape: return 27;
    case XKB_KEY_F1: return 135; case XKB_KEY_F2: return 136; case XKB_KEY_F3: return 137;
    case XKB_KEY_F4: return 138; case XKB_KEY_F5: return 139; case XKB_KEY_F6: return 140;
    case XKB_KEY_F7: return 141; case XKB_KEY_F8: return 142; case XKB_KEY_F9: return 143;
    case XKB_KEY_F10: return 144; case XKB_KEY_F11: return 145;
    case XKB_KEY_Home: return 151; case XKB_KEY_Page_Up: return 150; case XKB_KEY_End: return 152;
    case XKB_KEY_Page_Down: return 149; case XKB_KEY_Insert: return 147;
    case XKB_KEY_Delete: return 148;
    default: return symbol >= 32 && symbol < 127 ? symbol : 0;
    }
}
static unsigned physical_key(App *app, uint32_t key) {
    xkb_layout_index_t layout = xkb_state_key_get_layout(app->xkb_state, key + 8);
    const xkb_keysym_t *symbols = NULL;
    int count = xkb_keymap_key_get_syms_by_level(app->keymap, key + 8, layout, 0, &symbols);
    unsigned identity = count > 0 ? quake_key(symbols[0]) : 0;
    if (identity) return identity;
    /* Non-Latin layouts still steer with the same physical gameplay keys.
     * Text stays in the active group, independently of this ASCII fallback. */
    xkb_layout_index_t groups = xkb_keymap_num_layouts_for_key(app->keymap, key + 8);
    for (xkb_layout_index_t group = 0; group < groups; ++group) {
        if (group == layout) continue;
        count = xkb_keymap_key_get_syms_by_level(app->keymap, key + 8, group, 0, &symbols);
        identity = count > 0 ? quake_key(symbols[0]) : 0;
        if (identity >= 32 && identity <= 126) return identity;
    }
    return 0;
}
static uint32_t key_character(App *app, uint32_t key, unsigned identity, bool repeat) {
    if ((!repeat && identity == '`') ||
        xkb_state_mod_name_is_active(app->xkb_state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0 ||
        xkb_state_mod_name_is_active(app->xkb_state, XKB_MOD_NAME_ALT, XKB_STATE_MODS_EFFECTIVE) > 0 ||
        xkb_state_mod_name_is_active(app->xkb_state, XKB_MOD_NAME_LOGO, XKB_STATE_MODS_EFFECTIVE) > 0) return 0;
    uint32_t character = xkb_state_key_get_utf32(app->xkb_state, key + 8);
    return character >= 32 && character <= 126 ? character : 0;
}
static void repeat_keys(App *app) {
    uint64_t current = now_ms();
    if (!app->repeat_at || current < app->repeat_at || !app->human || gpu_saver_input(app)) return;
    unsigned identity = app->keys[app->repeat_key];
    if (!identity || app->keyboard_focus != app->human) { app->repeat_at = 0; return; }
    key_send_text(app, identity, true, key_character(app, app->repeat_key, identity, true), true);
    if (app->human && !app->input_failed) app->repeat_at = current + (uint64_t)(1000 / app->repeat_rate);
}
static void keyboard_key(void *data, struct wl_keyboard *keyboard, uint32_t serial, uint32_t time, uint32_t key, uint32_t state) {
    (void)keyboard; (void)serial; (void)time;
    App *app = data;
    if (gpu_saver_input(app)) { gpu_dismiss_saver(app); return; }
    if (!app->human || app->keyboard_focus != app->human || !app->xkb_state || key >= 768) return;
    bool down = state == WL_KEYBOARD_KEY_STATE_PRESSED;
    xkb_keysym_t symbol = xkb_state_key_get_one_sym(app->xkb_state, key + 8);
    if (down && symbol == XKB_KEY_F12) { return_bot(app, "F12"); return; }
    if (down && !app->keys[key]) {
        unsigned identity = physical_key(app, key);
        app->keys[key] = identity;
        if (identity && app->refs[identity]++ == 0)
            key_send_text(app, identity, true, key_character(app, key, identity, false), false);
        if (app->human && !app->input_failed && identity && app->repeat_rate > 0 && xkb_keymap_key_repeats(app->keymap, key + 8)) {
            app->repeat_key = key; app->repeat_at = now_ms() + (uint64_t)app->repeat_delay;
        }
    } else if (!down && app->keys[key]) {
        key_ref(app, app->keys[key], false); app->keys[key] = 0;
        if (app->repeat_key == key) app->repeat_at = 0;
    }
}
static void keyboard_modifiers(void *data, struct wl_keyboard *keyboard, uint32_t serial,
        uint32_t depressed, uint32_t latched, uint32_t locked, uint32_t group) {
    (void)keyboard; (void)serial;
    App *app = data;
    if (app->xkb_state) xkb_state_update_mask(app->xkb_state, depressed, latched, locked, 0, 0, group);
}
static void keyboard_repeat(void *data, struct wl_keyboard *keyboard, int32_t rate, int32_t delay) {
    (void)keyboard; App *app = data;
    app->repeat_rate = rate > 0 ? (rate > 100 ? 100 : rate) : 0;
    app->repeat_delay = delay > 0 ? delay : 1;
    if (!app->repeat_rate) app->repeat_at = 0;
}
static const struct wl_keyboard_listener keyboard_listener = {
    .keymap = keyboard_keymap, .enter = keyboard_enter, .leave = keyboard_leave,
    .key = keyboard_key, .modifiers = keyboard_modifiers, .repeat_info = keyboard_repeat,
};
static void seat_capabilities(void *data, struct wl_seat *seat, uint32_t capabilities) {
    App *app = data;
    if ((capabilities & WL_SEAT_CAPABILITY_POINTER) && !app->pointer) {
        app->pointer = wl_seat_get_pointer(seat); wl_pointer_add_listener(app->pointer, &pointer_listener, app); ensure_lock(app);
    } else if (!(capabilities & WL_SEAT_CAPABILITY_POINTER) && app->pointer) {
        release_pointer_input(app); release_lock(app); wl_pointer_release(app->pointer);
        app->pointer = NULL; app->pointer_focus = NULL; app->cursor_serial = 0;
    }
    if ((capabilities & WL_SEAT_CAPABILITY_KEYBOARD) && !app->keyboard) {
        app->keyboard = wl_seat_get_keyboard(seat); wl_keyboard_add_listener(app->keyboard, &keyboard_listener, app);
    } else if (!(capabilities & WL_SEAT_CAPABILITY_KEYBOARD) && app->keyboard) {
        release_keys(app); wl_keyboard_release(app->keyboard); app->keyboard = NULL; app->keyboard_focus = NULL;
    }
}
static void seat_name(void *data, struct wl_seat *seat, const char *name) { (void)data; (void)seat; (void)name; }
static const struct wl_seat_listener seat_listener = {.capabilities = seat_capabilities, .name = seat_name};

static void schedule_geometry_frame(Screen *screen) {
    /* An occluded output may withhold the old frame callback indefinitely.
     * Its callback must not block publishing the newly configured buffer and
     * input bounds. Resume normal frame throttling after that one commit. */
    cancel_frame_callback(screen);
    screen->ready = true; screen->redraw = true;
}
static void configure_geometry(Screen *screen, int width, int height) {
    if (screen->width != width || screen->height != height) {
        screen->width = width; screen->height = height;
        schedule_geometry_frame(screen);
    }
    screen->redraw = true;
}
/* This sync follows the NULL-buffer commit. Configures dispatched before its
 * reply belong to the previous mapping, even if the client has sent a remap. */
static void unmap_barrier_done(void *data, struct wl_callback *callback, uint32_t value) {
    (void)value; Screen *screen = data;
    if (callback == screen->unmap_barrier) screen->unmap_barrier = NULL;
    wl_callback_destroy(callback);
}
static const struct wl_callback_listener unmap_barrier_listener = {.done = unmap_barrier_done};
static void layer_configure(void *data, struct zwlr_layer_surface_v1 *layer, uint32_t serial, uint32_t width, uint32_t height) {
    Screen *screen = data;
    /* Unmapping resets layer configure serials. A previously queued configure
     * cannot be acknowledged or reconfigure this hidden generation. */
    if (screen->unmapped || screen->unmap_barrier) return;
    zwlr_layer_surface_v1_ack_configure(layer, serial);
    if (width && height && width <= 16384 && height <= 16384) {
        configure_geometry(screen, (int)width, (int)height);
        screen->configured = true; screen->redraw = true;
        screen->saver_configured_once = true;
    }
}
static void layer_closed(void *data, struct zwlr_layer_surface_v1 *layer) { (void)layer; ((Screen *)data)->removed = true; }
static const struct zwlr_layer_surface_v1_listener layer_listener = {.configure = layer_configure, .closed = layer_closed};
static void configure_wallpaper_surface(Screen *screen) {
    App *app = screen->app;
    bool probe = screen->gpu_failed_probe;
    bool saver = !probe && screen->mode == VIEW_SAVER;
    /* Reset geometry after an unmap, but leave input disabled until the
     * buffer-bearing map and a separate mapped layer-state commit. Hyprland
     * drops layer changes in both the unmapped and first map commits. */
    zwlr_layer_surface_v1_set_size(screen->layer, 0, 0);
    zwlr_layer_surface_v1_set_anchor(screen->layer, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP
        | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT);
    /* The wallpaper must extend behind panels, including translucent bars.
     * Zone 0 would move it into the reserved work area and make a bar toggle
     * move the whole viewport. */
    zwlr_layer_surface_v1_set_exclusive_zone(screen->layer, -1);
    if (zwlr_layer_surface_v1_get_version(screen->layer) >= 2)
        zwlr_layer_surface_v1_set_layer(screen->layer, saver ? ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY
            : !probe && screen->mode == VIEW_HUMAN ? ZWLR_LAYER_SHELL_V1_LAYER_TOP : ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM);
    zwlr_layer_surface_v1_set_keyboard_interactivity(screen->layer, screen->mapped && (saver || (!probe && screen->mode == VIEW_HUMAN))
        ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
    bool empty_input = !screen->mapped || probe || (!saver && screen->mode == VIEW_HIDDEN);
    struct wl_region *empty = empty_input ? wl_compositor_create_region(app->compositor) : NULL;
    if (empty_input && !empty) fail("cannot allocate empty layer input region");
    wl_surface_set_input_region(screen->surface, empty);
    if (empty) wl_region_destroy(empty);
}
static void finish_layer_mapping(Screen *screen) {
    /* The first non-NULL parent commit maps the role without moving its
     * compositor layer. Resend state while mapped, retaining the same buffer.
     * Only feedback requested after this promotion can certify a saver. */
    bool gpu_callback = screen->callback_gpu;
    struct wl_surface *image = screen->gpu_surface;
    cancel_frame_callback(screen);
    configure_wallpaper_surface(screen);
    wl_surface_commit(screen->surface);
    screen->callback = wl_surface_frame(image);
    wl_callback_add_listener(screen->callback, &frame_listener, screen);
    screen->ready = false;
    screen->callback_gpu = gpu_callback; screen->gpu_callback_at = now_ms();
    saver_image_callback(screen);
    wl_surface_damage_buffer(image, 0, 0, 1, 1);
    wl_surface_commit(image);
}
static void wallpaper_visibility(Screen *screen) {
    if (!screen->layer) return;
    bool hidden = screen->mode == VIEW_HIDDEN || (screen->capture_failed && !screen->gpu_failed_probe);
    if (hidden && screen->mapped) {
        cancel_frame_callback(screen);
        /* A layer change bundled with NULL unmap is also dropped. Demote in
         * its own mapped commit, and remove input before detaching buffers. */
        if (zwlr_layer_surface_v1_get_version(screen->layer) >= 2)
            zwlr_layer_surface_v1_set_layer(screen->layer, ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM);
        zwlr_layer_surface_v1_set_keyboard_interactivity(screen->layer, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
        struct wl_region *empty = wl_compositor_create_region(screen->app->compositor);
        if (!empty) fail("cannot allocate empty layer input region");
        wl_surface_set_input_region(screen->surface, empty); wl_region_destroy(empty);
        wl_surface_commit(screen->surface);
        if (screen->gpu_surface) {
            wl_surface_attach(screen->gpu_surface, NULL, 0, 0);
            wl_surface_commit(screen->gpu_surface); screen->gpu_attached = false;
        }
        wl_surface_attach(screen->surface, NULL, 0, 0);
        wl_surface_commit(screen->surface);
        if (screen->unmap_barrier) wl_callback_destroy(screen->unmap_barrier);
        screen->unmap_barrier = wl_display_sync(screen->app->display);
        wl_callback_add_listener(screen->unmap_barrier, &unmap_barrier_listener, screen);
        screen->mapped = false; screen->unmapped = true;
        screen->configured = false; screen->ready = true;
        screen->redraw = false;
        char event[256]; snprintf(event, sizeof(event), "wallpaper %s unmapped", screen->name);
        diagnostic(screen->app, event);
    } else if (!hidden && screen->unmapped) {
        configure_wallpaper_surface(screen);
        wl_surface_commit(screen->surface);
        screen->unmapped = false; screen->ready = true; screen->redraw = true;
        char event[256]; snprintf(event, sizeof(event), "wallpaper %s remapping; waiting for configure", screen->name);
        diagnostic(screen->app, event);
    }
}
static void create_surface(Screen *screen) {
    App *app = screen->app;
    if (screen->surface || screen->removed || !app->compositor || !app->shm || !app->layer_shell) return;
    screen->surface = wl_compositor_create_surface(app->compositor);
    screen->ready = true; screen->redraw = true;
    screen->layer = zwlr_layer_shell_v1_get_layer_surface(app->layer_shell, screen->surface,
        screen->output, ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM, "quake-desktop");
    zwlr_layer_surface_v1_add_listener(screen->layer, &layer_listener, screen);
    /* The wallpaper spans the complete output behind panels. A saver overlays the output. */
    configure_wallpaper_surface(screen);
    screen->background_viewport = wp_viewporter_get_viewport(app->viewporter, screen->surface);
    screen->gpu_surface = wl_compositor_create_surface(app->compositor);
    screen->gpu_viewport = wp_viewporter_get_viewport(app->viewporter, screen->gpu_surface);
    screen->gpu_subsurface = wl_subcompositor_get_subsurface(app->subcompositor, screen->gpu_surface, screen->surface);
    if (!screen->background_viewport || !screen->gpu_surface || !screen->gpu_viewport || !screen->gpu_subsurface) fail("cannot create GPU image subsurface");
    wl_subsurface_set_desync(screen->gpu_subsurface);
    /* Input and persistent pointer constraints remain on the policy parent. */
    struct wl_region *empty = wl_compositor_create_region(app->compositor);
    wl_surface_set_input_region(screen->gpu_surface, empty); wl_region_destroy(empty);
    wl_surface_commit(screen->surface);
}
static void finish_output_update(Screen *screen) {
    create_surface(screen);
    if (screen->output_changed) {
        screen->saver_ready_next = 0;
        screen->saver_configured_once = false;
        screen->saver_callback_cycle = screen->saver_presented_cycle = screen->saver_reported_cycle = 0;
        /* A hidden layer can negotiate fresh geometry without attaching any
         * pixels. Readiness after wake/hotplug must not paint a black probe. */
        if (screen->unmapped && screen->layer && !screen->removed && screen->app->saver_generation) {
            configure_wallpaper_surface(screen);
            wl_surface_commit(screen->surface);
            screen->unmapped = false;
        }
    }
    if (screen->output_changed && screen->configured && screen->layer && !screen->removed) {
        /* Some compositors report a resized nested output without arranging
         * existing layers. Reassert compositor-sized geometry to request a
         * fresh configure, retaining the full-output policy.
         * Only layer_configure supplies the surface's drawable dimensions. */
        zwlr_layer_surface_v1_set_size(screen->layer, 0, 0);
        wl_surface_commit(screen->surface);
        char event[256]; snprintf(event, sizeof(event), "output %s changed; requesting layer geometry", screen->name);
        diagnostic(screen->app, event);
    }
    screen->output_changed = false;
}
static void output_geometry(void *data, struct wl_output *output, int32_t x, int32_t y, int32_t pw,
        int32_t ph, int32_t subpixel, const char *make, const char *model, int32_t transform) {
    (void)output; (void)x; (void)y; (void)pw; (void)ph; (void)subpixel; (void)make; (void)model;
    Screen *screen = data;
    if (screen->output_transform != transform) { screen->output_transform = transform; screen->output_changed = true; }
}
static void output_mode(void *data, struct wl_output *output, uint32_t flags, int32_t width, int32_t height, int32_t refresh) {
    (void)refresh;
    Screen *screen = data;
    if ((flags & WL_OUTPUT_MODE_CURRENT) && width > 0 && height > 0) {
        if (screen->output_width != width || screen->output_height != height) screen->output_changed = true;
        screen->output_width = width; screen->output_height = height;
        if (!screen->configured) { screen->width = width / screen->scale; screen->height = height / screen->scale; }
        if (output && wl_output_get_version(output) < 2) finish_output_update(screen);
    }
}
static void output_done(void *data, struct wl_output *output) { (void)output; finish_output_update(data); }
static void output_scale(void *data, struct wl_output *output, int32_t scale) {
    (void)output; Screen *screen = data;
    if (scale >= 1 && scale <= 8 && screen->scale != scale) {
        screen->scale = scale; screen->output_changed = true;
        schedule_geometry_frame(screen);
    }
}
static void output_name(void *data, struct wl_output *output, const char *name) {
    (void)output; Screen *screen = data;
    snprintf(screen->name, sizeof(screen->name), "%s", name); screen->app->next_query = 0;
    char event[256]; snprintf(event, sizeof(event), "output named %s (wl_output v%u)", screen->name, wl_output_get_version(screen->output));
    diagnostic(screen->app, event);
}
static void output_description(void *data, struct wl_output *output, const char *description) { (void)data; (void)output; (void)description; }
static const struct wl_output_listener output_listener = {
    .geometry = output_geometry, .mode = output_mode, .done = output_done, .scale = output_scale,
    .name = output_name, .description = output_description,
};
static void xdg_output_position(void *data, struct zxdg_output_v1 *output, int32_t x, int32_t y) { (void)data; (void)output; (void)x; (void)y; }
static void xdg_output_size(void *data, struct zxdg_output_v1 *output, int32_t width, int32_t height) {
    (void)output; Screen *screen = data;
    if (width > 0 && height > 0) {
        if (screen->logical_width != width || screen->logical_height != height) screen->output_changed = true;
        screen->logical_width = width; screen->logical_height = height;
        if (!screen->configured) { screen->width = width; screen->height = height; }
    }
}
static void xdg_output_done(void *data, struct zxdg_output_v1 *output) {
    if (zxdg_output_v1_get_version(output) < 3) finish_output_update(data);
}
static void xdg_output_name(void *data, struct zxdg_output_v1 *output, const char *name) {
    (void)output; output_name(data, NULL, name);
}
static void xdg_output_description(void *data, struct zxdg_output_v1 *output, const char *description) { (void)data; (void)output; (void)description; }
static const struct zxdg_output_v1_listener xdg_output_listener = {
    .logical_position = xdg_output_position, .logical_size = xdg_output_size, .done = xdg_output_done,
    .name = xdg_output_name, .description = xdg_output_description,
};
static void ensure_output_name(Screen *screen) {
    if (screen->xdg_output || !screen->app->output_manager || screen->removed) return;
    screen->xdg_output = zxdg_output_manager_v1_get_xdg_output(screen->app->output_manager, screen->output);
    zxdg_output_v1_add_listener(screen->xdg_output, &xdg_output_listener, screen);
}
static void registry_global(void *data, struct wl_registry *registry, uint32_t name, const char *interface, uint32_t version) {
    App *app = data;
    if (!strcmp(interface, wl_compositor_interface.name))
        app->compositor = wl_registry_bind(registry, name, &wl_compositor_interface, version < 4 ? version : 4);
    else if (!strcmp(interface, wl_shm_interface.name)) app->shm = wl_registry_bind(registry, name, &wl_shm_interface, 1);
    else if (!strcmp(interface, wl_subcompositor_interface.name))
        app->subcompositor = wl_registry_bind(registry, name, &wl_subcompositor_interface, 1);
    else if (!strcmp(interface, wp_viewporter_interface.name))
        app->viewporter = wl_registry_bind(registry, name, &wp_viewporter_interface, 1);
    else if (!strcmp(interface, zwp_linux_dmabuf_v1_interface.name) && version >= 3) {
        struct zwp_linux_dmabuf_v1 *dmabuf = wl_registry_bind(registry, name, &zwp_linux_dmabuf_v1_interface, 3);
        if (lq_gpu_wayland_init(&app->gpu, dmabuf, gpu_ready, gpu_error, app) < 0) fail("cannot initialize DMA-BUF importer");
    }
    else if (!strcmp(interface, zwlr_layer_shell_v1_interface.name))
        app->layer_shell = wl_registry_bind(registry, name, &zwlr_layer_shell_v1_interface, version < 4 ? version : 4);
    else if (!strcmp(interface, zxdg_output_manager_v1_interface.name)) {
        app->output_manager = wl_registry_bind(registry, name, &zxdg_output_manager_v1_interface, version < 3 ? version : 3);
        for (Screen *screen = app->screens; screen; screen = screen->next) ensure_output_name(screen);
    } else if (!strcmp(interface, wl_seat_interface.name) && !app->seat) {
        app->seat = wl_registry_bind(registry, name, &wl_seat_interface, version < 5 ? version : 5);
        wl_seat_add_listener(app->seat, &seat_listener, app);
    } else if (!strcmp(interface, zwp_relative_pointer_manager_v1_interface.name))
        app->relative_manager = wl_registry_bind(registry, name, &zwp_relative_pointer_manager_v1_interface, 1);
    else if (!strcmp(interface, zwp_pointer_constraints_v1_interface.name))
        app->constraints = wl_registry_bind(registry, name, &zwp_pointer_constraints_v1_interface, 1);
    else if (!strcmp(interface, wl_output_interface.name)) {
        Screen *screen = calloc(1, sizeof(*screen)); if (!screen) fail("out of memory");
        screen->app = app; screen->global = name; screen->scale = 1;
        if (app->gpu_screen_serial == UINT64_MAX) fail("GPU output identity exhausted");
        screen->gpu_identity = ++app->gpu_screen_serial;
        screen->width = 1280; screen->height = 720; screen->mode = VIEW_HIDDEN;
        snprintf(screen->name, sizeof(screen->name), "output-%u", name);
        screen->output = wl_registry_bind(registry, name, &wl_output_interface, version < 4 ? version : 4);
        wl_output_add_listener(screen->output, &output_listener, screen);
        screen->next = app->screens; app->screens = screen;
        char event[256]; snprintf(event, sizeof(event), "output global %u advertised v%u (bound v%u)", name, version, wl_output_get_version(screen->output));
        diagnostic(app, event);
        ensure_output_name(screen);
    }
}
static void registry_remove(void *data, struct wl_registry *registry, uint32_t name) {
    (void)registry; App *app = data;
    for (Screen *screen = app->screens; screen; screen = screen->next) if (screen->global == name) screen->removed = true;
}
static const struct wl_registry_listener registry_listener = {.global = registry_global, .global_remove = registry_remove};
static void screen_destroy(Screen *screen) {
    App *app = screen->app;
    if (app->human == screen) { return_bot(app, "output removed"); app->human = NULL; }
    if (app->pointer_focus == screen) app->pointer_focus = NULL;
    if (app->keyboard_focus == screen) app->keyboard_focus = NULL;
    if (app->saver_request_screen == screen) saver_control_close(app);
    if (screen->unmap_barrier) { wl_callback_destroy(screen->unmap_barrier); screen->unmap_barrier = NULL; }
    cancel_frame_callback(screen);
    if (screen->layer) zwlr_layer_surface_v1_destroy(screen->layer);
    if (screen->gpu_viewport) wp_viewport_destroy(screen->gpu_viewport);
    if (screen->background_viewport) wp_viewport_destroy(screen->background_viewport);
    if (screen->gpu_subsurface) wl_subsurface_destroy(screen->gpu_subsurface);
    if (screen->gpu_surface) wl_surface_destroy(screen->gpu_surface);
    if (screen->surface) wl_surface_destroy(screen->surface);
    if (screen->xdg_output) zxdg_output_v1_destroy(screen->xdg_output);
    if (screen->output) {
        if (wl_output_get_version(screen->output) >= 3) wl_output_release(screen->output);
        else wl_output_destroy(screen->output);
    }
    retire_screen_buffers(screen);
    retire_probe_buffers(screen);
    if (screen->gpu_background) screen->gpu_background->retired = true;
    free(screen->snapshot); free(screen->blurred); free(screen);
}
static void remove_screens(App *app) {
    Screen **link = &app->screens;
    while (*link) {
        Screen *screen = *link;
        if (screen->removed) { *link = screen->next; screen_destroy(screen); }
        else link = &screen->next;
    }
}
static void apply_view(Screen *screen, enum ViewMode mode, bool audible) {
    if (mode != screen->mode || audible != screen->audible) {
        if (mode != screen->mode) {
            schedule_geometry_frame(screen);
            retire_probe_buffers(screen);
            screen->capture_pending = screen->capture_failed = false;
            screen->capture_expected = 0;
            screen->capture_retry_at = 0; screen->capture_retries = 0;
            screen->gpu_frozen = screen->gpu_resume_pending = false;
            screen->gpu_resume_callbacks = 0;
            /* Each frozen head owns its pixels; hiding/resuming/removal
             * releases them and retires compositor-owned SHM separately. */
            free(screen->snapshot); screen->snapshot = NULL;
            free(screen->blurred); screen->blurred = NULL;
            screen->cpu_snapshot = screen->snapshot_valid = screen->blur_valid = false;
            if (screen->buffers[0] && screen->buffers[0]->width != 1) retire_screen_buffers(screen);
            if (mode == VIEW_PAUSED) {
                screen->capture_pending = true; screen->capture_started_at = now_ms();
                /* A covered head's missing frame callback cannot pace a
                 * capture or prevent its peer from receiving frames. */
                screen->ready = true;
            }
        }
        screen->mode = mode; screen->audible = audible; screen->redraw = true;
        if (screen->layer && !screen->unmapped && mode != VIEW_HIDDEN) {
            configure_wallpaper_surface(screen);
            wl_surface_commit(screen->surface);
        }
    }
    wallpaper_visibility(screen);
}
/* Dynamic saver ownership is controlled by fresh policy. Dismissal never
 * quits this shared producer/viewer session and never forwards its
 * initiating input. */
static void gpu_saver_sync(App *app, bool active) {
    if (active == app->gpu_saver_active) return;
    app->gpu_saver_active = active;
    if (active) {
        app->gpu_saver_started = now_ms(); app->pointer_seen = false;
        input_close(app); app->input_failed = true; release_keys(app);
        release_lock(app); app->human = NULL; app->input_failed = false;
    }
    set_cursor(app);
}
static void gpu_saver_policy_complete(App *app, bool fresh_non_saver) {
    if (!app->gpu_saver_dismissed || !fresh_non_saver) return;
    /* Explicit hidden is a valid non-saver response: background-off sessions
     * must be able to enter a later idle cycle. Unknown/error never rearms. */
    app->gpu_saver_dismissed = app->gpu_dismiss_pending = false;
    app->gpu_dismiss_attempts = 0;
}
static void gpu_dismiss_saver(App *app) {
    if (app->gpu_saver_dismissed || !app->gpu_saver_active) return;
    app->gpu_saver_dismissed = app->gpu_dismiss_pending = true;
    app->gpu_dismiss_attempts = 0; app->gpu_dismiss_next = 0;
    gpu_saver_sync(app, false);
    for (Screen *screen = app->screens; screen; screen = screen->next)
        if (!screen->removed) apply_view(screen, VIEW_HIDDEN, false);
    app->next_query = 0; set_cursor(app);
    diagnostic(app, "GPU saver dismissed locally; awaiting controller acknowledgement");
}
static void gpu_control_service(App *app) {
    uint64_t current = now_ms();
    if (app->gpu_dismiss_pending && current >= app->gpu_dismiss_next) {
        char reply[128]; ++app->gpu_dismiss_attempts;
        app->gpu_dismiss_next = current + 1000;
        if (command(app, "dismiss-screensaver", reply, sizeof(reply)) && !strncmp(reply, "OK", 2))
            app->gpu_dismiss_pending = false;
        else if (app->gpu_dismiss_attempts >= 3) {
            app->gpu_dismiss_pending = false;
            diagnostic(app, "GPU saver dismissal acknowledgement unavailable; remaining hidden until fresh non-saver policy");
        }
    }
    if (!app->capture_reported && current >= app->capture_report_next) {
        uint32_t seq = 0, supported = 0, magic = 0, version = 0, pid = 0;
        if (app->shared) {
            seq = __atomic_load_n(&app->shared->seq, __ATOMIC_ACQUIRE);
            supported = app->shared->capture_supported;
            magic = app->shared->magic; version = app->shared->version; pid = app->shared->pid;
            __atomic_thread_fence(__ATOMIC_ACQUIRE);
        }
        /* The mapping exists before the engine's first publication. Its
         * zeroed header must not latch capture as unsupported at startup. */
        bool coherent = app->shared && !(seq & 1) && magic == DD_MAGIC && version == DD_VERSION && pid &&
            seq == __atomic_load_n(&app->shared->seq, __ATOMIC_ACQUIRE);
        /* Match the controller's five-second native startup bound. A missing
         * publication fails closed, but a later real header can still report
         * its capability rather than permanently latching the timeout. */
        bool expired = current - app->started >= 5000 && !app->capture_bootstrap_failed;
        if (coherent || expired) {
            char request[128], reply[128];
            app->capture_report_next = current + 1000;
            snprintf(request, sizeof(request), "capture-status %llu %s", (unsigned long long)app->gpu_generation, coherent && supported ? "ready" : "failed");
            bool received = command(app, request, reply, sizeof(reply));
            if (received && !strncmp(reply, "OK", 2)) {
                if (coherent) app->capture_reported = true;
                else {
                    app->capture_bootstrap_failed = true;
                    diagnostic(app, "GPU capture unavailable: no engine publication within startup deadline");
                }
                app->next_query = 0;
            } else if (received && !strncmp(reply, "ERR", 3)) {
                /* An older controller cannot negotiate this additive verb.
                 * Do not repeatedly enqueue requests into its update loop. */
                app->capture_reported = true;
                diagnostic(app, "GPU capture status rejected; retaining unavailable policy");
            }
        }
    }
}
static bool saver_publication(const App *app) {
    if (!app->shared) return false;
    uint32_t seq = __atomic_load_n(&app->shared->seq, __ATOMIC_ACQUIRE);
    if (seq & 1) return false;
    DDHeader header;
    memcpy(&header, app->shared, sizeof(header));
    __atomic_thread_fence(__ATOMIC_ACQUIRE);
    return seq == __atomic_load_n(&app->shared->seq, __ATOMIC_ACQUIRE) && saver_header(&header);
}
/* Saver diagnostics use one independent nonblocking request at a time.
 * Unlike ownership/capture control, no reply is awaited inside a dispatch
 * iteration; the main poll services both Wayland and this control stream. */
static void saver_control_close(App *app) {
    if (app->saver_request != SAVER_REQUEST_NONE) close(app->saver_control_fd);
    app->saver_request = SAVER_REQUEST_NONE;
    app->saver_connecting = false; app->saver_request_screen = NULL;
    app->saver_request_used = app->saver_request_sent = app->saver_reply_used = 0;
}
static void saver_cycle(App *app, uint64_t cycle) {
    if (cycle == app->saver_cycle) return;
    app->saver_cycle = cycle;
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        screen->saver_callback_cycle = screen->saver_presented_cycle = screen->saver_reported_cycle = 0;
        screen->saver_present_next = 0;
        /* Existing wallpaper feedback cannot acknowledge the new cycle. */
        screen->redraw = true;
    }
}
static void saver_epoch(App *app, uint64_t generation) {
    if (app->saver_generation == generation) return;
    saver_control_close(app); saver_cycle(app, 0);
    app->saver_generation = generation; app->saver_state_next = 0;
    app->saver_last_request = SAVER_REQUEST_NONE;
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        screen->saver_ready_next = 0;
        /* OFF output changes may leave a hidden layer awaiting configure.
         * Enabling diagnostics negotiates readiness without an image attach. */
        if (generation && !screen->removed && screen->mode == VIEW_HIDDEN
            && screen->unmapped && screen->layer && !screen->saver_configured_once) {
            configure_wallpaper_surface(screen); wl_surface_commit(screen->surface);
            screen->unmapped = false;
        }
    }
}
static void saver_control_reply(App *app) {
    if (app->saver_request_generation != app->saver_generation) return;
    if (app->saver_request == SAVER_REQUEST_STATE) {
        unsigned long long generation, cycle; char extra;
        if (sscanf(app->saver_reply, "SAVER %llu %llu %c", &generation, &cycle, &extra) == 2) {
            if (generation == app->saver_generation) saver_cycle(app, cycle);
            else saver_cycle(app, 0);
        }
        /* A timeout/ERR is an unknown observation. Keep confirmed cycle tags
         * and deduplicated receipts so the same epoch can recover promptly. */
    } else if (app->saver_request == SAVER_REQUEST_PRESENTED && !strncmp(app->saver_reply, "OK", 2)) {
        for (Screen *screen = app->screens; screen; screen = screen->next)
            if (screen == app->saver_request_screen && saver_active(screen)
                && app->saver_request_cycle == app->saver_cycle
                && screen->saver_presented_cycle == app->saver_request_cycle)
                screen->saver_reported_cycle = app->saver_request_cycle;
    }
}
static void saver_control_io(App *app) {
    if (app->saver_request == SAVER_REQUEST_NONE) return;
    if (now_ms() >= app->saver_request_deadline) { saver_control_close(app); return; }
    int fd = app->saver_control_fd;
    if (app->saver_connecting) {
        struct pollfd pfd = {.fd = fd, .events = POLLOUT};
        if (poll(&pfd, 1, 0) <= 0) return;
        int error = 0; socklen_t size = sizeof(error);
        if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) || error) { saver_control_close(app); return; }
        app->saver_connecting = false;
    }
    while (app->saver_request_sent < app->saver_request_used) {
        ssize_t sent = send(fd, app->saver_request_bytes + app->saver_request_sent,
            app->saver_request_used - app->saver_request_sent, MSG_NOSIGNAL);
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) return;
        if (sent <= 0) { saver_control_close(app); return; }
        app->saver_request_sent += (size_t)sent;
        if (app->saver_request_sent == app->saver_request_used) shutdown(fd, SHUT_WR);
    }
    for (;;) {
        size_t available = sizeof(app->saver_reply) - app->saver_reply_used - 1;
        if (!available) { saver_control_close(app); return; }
        ssize_t count = recv(fd, app->saver_reply + app->saver_reply_used, available, 0);
        if (count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) return;
        if (count <= 0) { saver_control_close(app); return; }
        app->saver_reply_used += (size_t)count;
        char *newline = memchr(app->saver_reply, '\n', app->saver_reply_used);
        if (newline) {
            *newline = 0; saver_control_reply(app); saver_control_close(app); return;
        }
    }
}
static void saver_control_begin(App *app, enum SaverRequest kind, Screen *screen,
        const char *request) {
    if (!app->socket_path || app->saver_request != SAVER_REQUEST_NONE) return;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(app->socket_path) >= sizeof(address.sun_path)) return;
    size_t length = strlen(request);
    if (length + 1 >= sizeof(app->saver_request_bytes)) return;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0) return;
    strcpy(address.sun_path, app->socket_path);
    int result = connect(fd, (struct sockaddr *)&address, sizeof(address));
    if (result < 0 && errno != EINPROGRESS) { close(fd); return; }
    app->saver_request = app->saver_last_request = kind;
    app->saver_control_fd = fd; app->saver_connecting = result < 0;
    memcpy(app->saver_request_bytes, request, length);
    app->saver_request_bytes[length] = '\n'; app->saver_request_used = length + 1;
    app->saver_request_sent = app->saver_reply_used = 0;
    app->saver_request_generation = app->saver_generation;
    app->saver_request_cycle = app->saver_cycle; app->saver_request_screen = screen;
    app->saver_request_deadline = now_ms() + 250;
    saver_control_io(app);
}
static void saver_control_service(App *app) {
    if (!app->saver_generation || app->quit) { saver_control_close(app); return; }
    if (app->saver_request != SAVER_REQUEST_NONE) { saver_control_io(app); return; }
    uint64_t current = now_ms(); char request[256];
    /* Alternate an overdue state poll with pending head reports. A stalled
     * state reply must not starve an already confirmed presentation receipt. */
    bool state_due = current >= app->saver_state_next;
    if (state_due && app->saver_last_request != SAVER_REQUEST_STATE) {
        app->saver_state_next = current + 250;
        snprintf(request, sizeof(request), "saver-state %llu", (unsigned long long)app->saver_generation);
        saver_control_begin(app, SAVER_REQUEST_STATE, NULL, request); return;
    }
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed || !screen->name[0]) continue;
        if (saver_active(screen) && screen->saver_presented_cycle == app->saver_cycle
            && screen->saver_reported_cycle != app->saver_cycle && current >= screen->saver_present_next) {
            screen->saver_present_next = current + 500;
            snprintf(request, sizeof(request), "saver-presented %llu %llu %s",
                (unsigned long long)app->saver_generation, (unsigned long long)app->saver_cycle, screen->name);
            saver_control_begin(app, SAVER_REQUEST_PRESENTED, screen, request); return;
        }
        if (screen->saver_configured_once && screen->width > 0 && screen->height > 0
            && current >= screen->saver_ready_next && saver_publication(app)) {
            screen->saver_ready_next = current + 1000;
            snprintf(request, sizeof(request), "viewer-ready %llu %s", (unsigned long long)app->saver_generation, screen->name);
            saver_control_begin(app, SAVER_REQUEST_READY, screen, request); return;
        }
    }
    if (state_due) {
        app->saver_state_next = current + 250;
        snprintf(request, sizeof(request), "saver-state %llu", (unsigned long long)app->saver_generation);
        saver_control_begin(app, SAVER_REQUEST_STATE, NULL, request);
    }
}
static void gpu_capture_failure(Screen *screen, const char *reason) {
    screen->capture_pending = false; screen->capture_failed = true;
    screen->capture_expected = 0; screen->snapshot_valid = screen->cpu_snapshot = false;
    free(screen->snapshot); screen->snapshot = NULL;
    free(screen->blurred); screen->blurred = NULL; screen->blur_valid = false;
    retire_screen_buffers(screen);
    retire_probe_buffers(screen);
    screen->gpu_resume_pending = false; screen->gpu_resume_callbacks = 0;
    screen->capture_retry_at = screen->capture_retries < 1 ? now_ms() + 1000 : 0;
    char event[256]; snprintf(event, sizeof(event), "GPU_CAPTURE_FAILED output=%s reason=%s", screen->name, reason);
    diagnostic(screen->app, event); wallpaper_visibility(screen);
}
static bool gpu_head_released(const Screen *screen) {
    const App *app = screen->app;
    for (unsigned slot = 0; slot < LQ_GPU_SLOTS; ++slot)
        for (unsigned consumer = 0; consumer < LQ_GPU_CONSUMERS; ++consumer)
            if (app->gpu.slots[slot].consumers[consumer].committed &&
                app->gpu_targets[slot][consumer] == screen->gpu_identity) return false;
    return true;
}
static bool gpu_capture_supported(const App *app) {
    const DDHeader *shared = app->shared;
    if (!shared) return false;
    uint32_t seq = __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE);
    uint32_t magic = shared->magic, version = shared->version, pid = shared->pid;
    bool supported = shared->capture_supported != 0;
    __atomic_thread_fence(__ATOMIC_ACQUIRE);
    return !(seq & 1) && magic == DD_MAGIC && version == DD_VERSION && pid && supported &&
        seq == __atomic_load_n(&shared->seq, __ATOMIC_ACQUIRE);
}
static bool dispatch_wayland_ready(App *app) {
    if (!app->display) return true;
    /* Control requests and CPU painting can take time. Read callbacks already
     * on the socket before deciding that a head has stopped responding. */
    while (wl_display_prepare_read(app->display) != 0)
        if (wl_display_dispatch_pending(app->display) < 0) { app->quit = true; return false; }
    struct pollfd fd = {.fd = wl_display_get_fd(app->display), .events = POLLIN};
    int result = poll(&fd, 1, 0);
    if (result > 0 && (fd.revents & POLLIN)) {
        if (wl_display_read_events(app->display) < 0) { app->quit = true; return false; }
    } else wl_display_cancel_read(app->display);
    if ((result < 0 && errno != EINTR) || (result > 0 && (fd.revents & (POLLERR | POLLHUP | POLLNVAL))) ||
        wl_display_dispatch_pending(app->display) < 0) { app->quit = true; return false; }
    return true;
}
static void gpu_failed_visibility_probe(Screen *screen) {
    App *app = screen->app;
    if (!app->shm || !screen->surface || !screen->gpu_surface || !screen->background_viewport || !screen->gpu_viewport) {
        screen->gpu_probe_next = now_ms() + 1000; return;
    }
    if (!screen->gpu_failed_probe) {
        /* Once both bounded capture attempts failed, transparent immutable
         * SHM keeps child visibility feedback working without a producer BO
         * or a fabricated game image. Separate buffers have separate releases. */
        if (!screen->gpu_probe_parent)
            screen->gpu_probe_parent = make_buffer_format(app, 1, 1, WL_SHM_FORMAT_ARGB8888);
        if (!screen->gpu_probe_child)
            screen->gpu_probe_child = make_buffer_format(app, 1, 1, WL_SHM_FORMAT_ARGB8888);
        if (!screen->gpu_probe_parent || !screen->gpu_probe_child) {
            retire_probe_buffers(screen); screen->gpu_probe_next = now_ms() + 1000; return;
        }
        *(uint32_t *)screen->gpu_probe_parent->pixels = 0;
        *(uint32_t *)screen->gpu_probe_child->pixels = 0;
        screen->gpu_failed_probe = true;
        screen->gpu_resume_callbacks = 0; screen->gpu_resume_last = 0;
        /* A timed-out bufferless capture remap may already be waiting for
         * configure. Replace its input/layer policy before attaching a probe. */
        if (!screen->unmapped && screen->layer) {
            configure_wallpaper_surface(screen); wl_surface_commit(screen->surface);
        }
        wallpaper_visibility(screen);
    }
    if (!screen->configured || screen->width <= 0 || screen->height <= 0) return;
    if (screen->mapped) {
        if (!screen->redraw) return;
        /* Keep visibility coverage on the entire output after a resize. */
        wp_viewport_set_destination(screen->background_viewport, screen->width, screen->height);
        wp_viewport_set_destination(screen->gpu_viewport, screen->width, screen->height);
        wl_surface_damage_buffer(screen->gpu_surface, 0, 0, 1, 1);
        wl_surface_commit(screen->gpu_surface);
        wl_surface_damage_buffer(screen->surface, 0, 0, 1, 1);
        wl_surface_commit(screen->surface);
        screen->redraw = false; return;
    }
    wp_viewport_set_destination(screen->background_viewport, screen->width, screen->height);
    wl_surface_set_buffer_scale(screen->surface, 1);
    screen->gpu_probe_parent->busy = true;
    wl_surface_attach(screen->surface, screen->gpu_probe_parent->wl, 0, 0);
    wl_surface_damage_buffer(screen->surface, 0, 0, 1, 1);
    wp_viewport_set_source(screen->gpu_viewport, wl_fixed_from_int(-1), wl_fixed_from_int(-1),
        wl_fixed_from_int(-1), wl_fixed_from_int(-1));
    wp_viewport_set_destination(screen->gpu_viewport, screen->width, screen->height);
    wl_subsurface_set_position(screen->gpu_subsurface, 0, 0);
    wl_surface_set_buffer_scale(screen->gpu_surface, 1);
    wl_surface_set_buffer_transform(screen->gpu_surface, WL_OUTPUT_TRANSFORM_NORMAL);
    screen->gpu_probe_child->busy = true;
    wl_surface_attach(screen->gpu_surface, screen->gpu_probe_child->wl, 0, 0);
    wl_surface_damage_buffer(screen->gpu_surface, 0, 0, 1, 1);
    wl_surface_commit(screen->gpu_surface); wl_surface_commit(screen->surface);
    screen->mapped = true; screen->gpu_attached = false; screen->redraw = false;
    screen->gpu_background_restore = true;
}
static void gpu_liveness_service(App *app) {
    if (!dispatch_wayland_ready(app)) return;
    uint64_t current = now_ms();
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed || screen->mode == VIEW_HIDDEN || screen->mode == VIEW_PAUSED) continue;
        if (!screen->gpu_frozen && !screen->capture_failed) {
            /* Unsupported/never-published engines keep the existing policy;
             * they cannot supply the copy needed for local demotion. */
            if (!gpu_capture_supported(app)) continue;
            const char *reason = NULL;
            if (screen->callback_gpu && screen->callback && !screen->ready &&
                current - screen->gpu_callback_at >= 250) reason = "frame-callback";
            for (unsigned slot = 0; slot < LQ_GPU_SLOTS && !reason; ++slot) {
                const struct lq_gpu_wayland_slot *held = &app->gpu.slots[slot];
                if (!held->occupied || held->frame.serial >= screen->last_frame) continue;
                for (unsigned consumer = 0; consumer < LQ_GPU_CONSUMERS; ++consumer)
                    if (held->consumers[consumer].committed &&
                        app->gpu_targets[slot][consumer] == screen->gpu_identity &&
                        app->gpu_superseded_at[slot][consumer] &&
                        current - app->gpu_superseded_at[slot][consumer] >= 250) reason = "buffer-release";
            }
            if (!reason) continue;
            schedule_geometry_frame(screen);
            screen->gpu_frozen = true; screen->gpu_resume_pending = false;
            screen->gpu_frozen_at = current; screen->gpu_probe_next = current + 100;
            screen->gpu_resume_callbacks = 0; screen->gpu_resume_last = 0;
            screen->capture_pending = true; screen->capture_expected = 0;
            screen->capture_started_at = current; screen->capture_retry_at = 0; screen->capture_retries = 0;
            free(screen->snapshot); screen->snapshot = NULL;
            free(screen->blurred); screen->blurred = NULL;
            screen->cpu_snapshot = screen->snapshot_valid = screen->blur_valid = false;
            char event[256]; snprintf(event, sizeof(event), "GPU_HEAD_FROZEN output=%s reason=%s", screen->name, reason);
            diagnostic(app, event);
        } else if (screen->gpu_frozen && !screen->capture_pending &&
            ((screen->cpu_snapshot && !screen->capture_failed) || screen->capture_failed)) {
            if (screen->capture_failed && !screen->capture_retry_at && current >= screen->gpu_probe_next)
                gpu_failed_visibility_probe(screen);
            if (current - screen->gpu_resume_last > 250) {
                screen->gpu_resume_pending = false; screen->gpu_resume_callbacks = 0;
            }
            if (screen->gpu_resume_pending && gpu_head_released(screen)) {
                schedule_geometry_frame(screen);
                bool failed_probe = screen->gpu_failed_probe;
                retire_probe_buffers(screen);
                screen->gpu_frozen = screen->gpu_resume_pending = false;
                screen->capture_failed = false; screen->capture_retry_at = 0; screen->capture_retries = 0;
                free(screen->snapshot); screen->snapshot = NULL;
                free(screen->blurred); screen->blurred = NULL;
                screen->cpu_snapshot = screen->snapshot_valid = screen->blur_valid = false;
                retire_screen_buffers(screen);
                if (failed_probe && screen->layer) configure_wallpaper_surface(screen);
                char event[256]; snprintf(event, sizeof(event), "GPU_HEAD_LIVE output=%s", screen->name);
                diagnostic(app, event);
            } else if ((!screen->capture_failed || screen->gpu_failed_probe) && screen->configured && screen->mapped && !screen->callback && current >= screen->gpu_probe_next) {
                /* Probe visibility without repainting SHM or requesting a
                 * GPU lease. Two timely callbacks and a short frozen dwell
                 * keep intermittent occlusion from immediately flapping. */
                screen->callback = wl_surface_frame(screen->gpu_surface);
                wl_callback_add_listener(screen->callback, &frame_listener, screen);
                saver_image_callback(screen);
                screen->callback_gpu = false; screen->gpu_callback_at = current;
                screen->ready = false; screen->gpu_probe_next = current + 100;
                wl_surface_damage_buffer(screen->gpu_surface, 0, 0, 1, 1);
                wl_surface_commit(screen->gpu_surface);
                /* Layer child commits alone need not wake an idle output. */
                wl_surface_damage_buffer(screen->surface, 0, 0, 1, 1);
                wl_surface_commit(screen->surface);
            }
        }
    }
}
static void gpu_capture_service(App *app) {
    if (app->gpu_saver_dismissed || app->gpu.failed || app->quit) return;
    uint64_t current = now_ms();
    Screen *owner = NULL, *candidate = NULL;
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed) continue;
        if (screen->capture_failed && screen->capture_retry_at && current >= screen->capture_retry_at) {
            ++screen->capture_retries; screen->capture_retry_at = 0;
            screen->capture_failed = false; screen->capture_pending = true;
            screen->capture_expected = 0; screen->capture_started_at = current;
            wallpaper_visibility(screen);
        }
        if (!screen->capture_pending) continue;
        if (current - screen->capture_started_at >= 1500) {
            gpu_capture_failure(screen, screen->capture_expected ? "bridge-unavailable" : !screen->configured ? "configure-timeout" : "release-timeout");
            continue;
        }
        /* Copy the existing receipt before issuing any new command, even
         * when its head appears later in the output list. */
        if (screen->capture_expected) owner = screen;
        else if (!candidate && screen->configured) candidate = screen;
    }
    Screen *screen = owner ? owner : candidate;
    if (!screen) return;
    if (!screen->capture_expected) {
        /* Retained current images use at most one BO. Drain other imports,
         * raw candidates and genuine release ACKs before the zero-tic render.
         * No attachment is removed merely to paint a black transition. */
        unsigned committed_slots = 0; bool draining = app->gpu_cached_valid;
        for (unsigned i = 0; i < LQ_GPU_SLOTS; ++i) {
            const struct lq_gpu_wayland_slot *slot = &app->gpu.slots[i];
            if (slot->occupied) {
                draining |= slot->pending || slot->release_ready;
                if (slot->committed) ++committed_slots;
            }
        }
        draining |= committed_slots > 1;
        if (draining || screen->gpu_wait_serial) return;
        char request[128], reply[128], trailing; unsigned long long serial;
        snprintf(request, sizeof(request), "capture %llu", (unsigned long long)app->gpu_generation);
        app->capture_rendering = true;
        bool received = command(app, request, reply, sizeof(reply));
        app->capture_rendering = false;
        if (!received || sscanf(reply, "OK %llu %c", &serial, &trailing) != 1 || !serial) {
            gpu_capture_failure(screen, "render-unavailable"); return;
        }
        screen->capture_expected = serial;
    }
    /* Seqlock contention retries this receipt without another GPU read and
     * without dropping the live peers' raw frames during the CPU copy phase. */
    if (!snapshot(screen)) return;
    screen->capture_pending = false; screen->capture_retry_at = 0; screen->capture_copies++;
    screen->redraw = screen->ready = true;
    if (screen->buffers[0] && screen->buffers[0]->width == 1) retire_screen_buffers(screen);
    char event[256]; snprintf(event, sizeof(event), "GPU_CAPTURE output=%s serial=%llu copies=%llu elapsed_us=%u bytes=%llu",
        screen->name, (unsigned long long)screen->capture_expected, (unsigned long long)screen->capture_copies, screen->header.capture_elapsed_us,
        (unsigned long long)screen->header.width * screen->header.height * 4);
    diagnostic(app, event);
}
static uint64_t view_saver_generation(const char *reply) {
    char word[32]; int sound, blur, used = 0;
    if (sscanf(reply, "STATE %31s %d %d %n", word, &sound, &blur, &used) != 3) return 0;
    const char *text = reply + used;
    if (*text < '0' || *text > '9') return 0;
    errno = 0; char *end; unsigned long long generation = strtoull(text, &end, 10);
    while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') ++end;
    return errno || *end ? 0 : generation;
}
static void update_views(App *app) {
    Screen *human = NULL;
    bool fresh_non_saver = true, saw_screen = false;
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed) continue;
        saw_screen = true;
        enum ViewMode mode = VIEW_HIDDEN; bool audible = false;
        char request[256], reply[256], word[32]; int sound, blur = 20;
        snprintf(request, sizeof(request), "view %s", screen->name);
        bool valid = command(app, request, reply, sizeof(reply)) && sscanf(reply, "STATE %31s %d %d", word, &sound, &blur) >= 2;
        if (valid && strcmp(word, "bot") && strcmp(word, "human") && strcmp(word, "paused") && strcmp(word, "hidden") && strcmp(word, "saver")) valid = false;
        if (valid) {
            saver_epoch(app, view_saver_generation(reply));
            blur = blur < 0 ? 0 : blur > 100 ? 100 : blur;
            if (screen->blur_strength != blur) {
                screen->blur_strength = blur; screen->blur_valid = false; screen->redraw = true;
                if (screen->cpu_snapshot && (screen->mode == VIEW_PAUSED || screen->gpu_frozen)) schedule_geometry_frame(screen);
            }
            if (!strcmp(word, "saver")) { mode = VIEW_SAVER; fresh_non_saver = false; }
            else if (!strcmp(word, "bot")) mode = VIEW_BOT;
            else if (!strcmp(word, "human")) mode = VIEW_HUMAN;
            else if (!strcmp(word, "paused")) mode = VIEW_PAUSED;
            audible = sound != 0;
            if (!screen->last_view_success || mode != screen->mode || audible != screen->audible) {
                char event[256]; snprintf(event, sizeof(event), "view %s -> %s sound=%d", screen->name, word, sound);
                diagnostic(app, event);
            }
            screen->last_view_success = now_ms(); screen->query_failed = false;
        } else {
            fresh_non_saver = false;
            if (!screen->query_failed) diagnostic(app, screen->mode == VIEW_SAVER
                ? "GPU saver policy unavailable; hiding immediately"
                : "view query temporarily unavailable; retaining recent policy for 500 ms");
            screen->query_failed = true;
            if (screen->last_view_success && now_ms() - screen->last_view_success <= 500
                && screen->mode != VIEW_SAVER) {
                mode = screen->mode; audible = screen->audible;
            }
        }
        screen->wanted_mode = mode; screen->wanted_audible = audible;
    }
    gpu_saver_policy_complete(app, saw_screen && fresh_non_saver);
    bool active = false;
    for (Screen *screen = app->screens; screen; screen = screen->next)
        if (!screen->removed && screen->wanted_mode == VIEW_SAVER && !app->gpu_saver_dismissed) active = true;
    gpu_saver_sync(app, active);
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed) continue;
        enum ViewMode mode = app->gpu_saver_dismissed ? VIEW_HIDDEN : screen->wanted_mode;
        bool audible = !app->gpu_saver_dismissed && screen->wanted_audible;
        /* Saver activation consumes all gameplay input, even if a stale
         * per-output human response arrived in the same policy sweep. */
        if (app->gpu_saver_active && mode == VIEW_HUMAN) mode = VIEW_HIDDEN;
        apply_view(screen, mode, audible);
        if (mode == VIEW_HUMAN) human = screen;
    }
    if (human != app->human) {
        char event[384]; snprintf(event, sizeof(event), "controller ownership: %s -> %s",
            app->human ? app->human->name : "bot", human ? human->name : "bot");
        diagnostic(app, event);
        /* The controller has already changed ownership and released native
         * holds. Discard the old lease before clearing local refs; a saturated
         * queue must not reenter return_bot and overwrite this newer policy. */
        input_close(app); app->input_failed = true; release_keys(app);
        release_lock(app); app->human = human;
        app->input_failed = false; set_cursor(app);
    }
    ensure_lock(app);
    app->next_query = now_ms() + 200;
}
static void try_frame_mapping(App *app) {
    if (app->shared) return;
    int fd = open(app->frame_path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return;
    struct stat status;
    if (fstat(fd, &status) || !S_ISREG(status.st_mode) || status.st_uid != getuid()
        || status.st_size != DD_FILE_SIZE) { close(fd); return; }
    void *mapping = mmap(NULL, DD_FILE_SIZE, PROT_READ, MAP_SHARED, fd, 0);
    if (mapping == MAP_FAILED) { close(fd); return; }
    app->shared = mapping; app->frame_fd = fd;
}
static int idle_timeout(App *app) {
    int timeout = 200;
    uint64_t current = now_ms();
    if (app->next_query <= current) return 0;
    if (app->next_query - current < (uint64_t)timeout) timeout = (int)(app->next_query - current);
    if (app->saver_request != SAVER_REQUEST_NONE) {
        int control_timeout = app->saver_request_deadline > current
            ? (int)(app->saver_request_deadline - current) : 0;
        if (control_timeout < timeout) timeout = control_timeout;
    }
    if (app->repeat_at) {
        int repeat_timeout = app->repeat_at > current ? (int)(app->repeat_at - current) : 0;
        if (repeat_timeout < timeout) timeout = repeat_timeout;
    }
    for (Screen *screen = app->screens; screen; screen = screen->next) {
        if (screen->removed) continue;
        if (screen->mode == VIEW_HUMAN) {
            /* The engine publishes its human render rate in the frame header;
             * a missing or invalid rate falls back to 60 Hz. */
            unsigned fps = screen->header.render_fps;
            if (fps < 35 || fps > 120) fps = 60;
            int frame_timeout = 1000 / (int)fps;
            if (timeout > frame_timeout) timeout = frame_timeout;
        } else if (screen->mode == VIEW_BOT || screen->mode == VIEW_SAVER) {
            if (timeout > 16) timeout = 16;
        }
    }
    return timeout;
}
static void cleanup(App *app) {
    if (app->human) return_bot(app, "viewer stopping");
    input_close(app); saver_control_close(app);
    release_lock(app);
    gpu_close_cached(app); lq_gpu_wayland_destroy(&app->gpu);
    if (app->gpu_fd >= 0) close(app->gpu_fd);
    while (app->screens) { Screen *screen = app->screens; app->screens = screen->next; screen_destroy(screen); }
    while (app->buffers) { Buffer *buffer = app->buffers; app->buffers = buffer->next; buffer_destroy(buffer); }
    if (app->keyboard) wl_keyboard_release(app->keyboard);
    if (app->pointer) wl_pointer_release(app->pointer);
    if (app->seat) wl_seat_release(app->seat);
    if (app->cursor_surface) wl_surface_destroy(app->cursor_surface);
    if (app->cursor_theme) wl_cursor_theme_destroy(app->cursor_theme);
    if (app->constraints) zwp_pointer_constraints_v1_destroy(app->constraints);
    if (app->relative_manager) zwp_relative_pointer_manager_v1_destroy(app->relative_manager);
    if (app->layer_shell) {
        if (zwlr_layer_shell_v1_get_version(app->layer_shell) >= 3) zwlr_layer_shell_v1_destroy(app->layer_shell);
        else wl_proxy_destroy((struct wl_proxy *)app->layer_shell);
    }
    if (app->output_manager) zxdg_output_manager_v1_destroy(app->output_manager);
    if (app->viewporter) wp_viewporter_destroy(app->viewporter);
    if (app->subcompositor) wl_subcompositor_destroy(app->subcompositor);
    if (app->shm) wl_shm_destroy(app->shm);
    if (app->compositor) wl_compositor_destroy(app->compositor);
    if (app->registry) wl_registry_destroy(app->registry);
    if (app->display) { wl_display_flush(app->display); wl_display_disconnect(app->display); }
    xkb_state_unref(app->xkb_state); xkb_keymap_unref(app->keymap); xkb_context_unref(app->xkb_context);
    if (app->shared) munmap((void *)app->shared, DD_FILE_SIZE);
    if (app->frame_fd >= 0) close(app->frame_fd);
}
int main(int argc, char **argv) {
    const struct rlimit core_limit = {.rlim_cur = 1, .rlim_max = 1};
    if (setrlimit(RLIMIT_CORE, &core_limit) < 0) fail("cannot limit crash core dumps");
    protect_parent();
    App app = {.frame_fd = -1, .gpu_fd = -1};
    for (int i = 1; i < argc; ++i) {
        if (!strcmp(argv[i], "--frame") && i + 1 < argc) app.frame_path = argv[++i];
        else if (!strcmp(argv[i], "--socket") && i + 1 < argc) app.socket_path = argv[++i];
        else if (!strcmp(argv[i], "--gpu-fd") && i + 1 < argc) {
            char *end; errno = 0; long value = strtol(argv[++i], &end, 10);
            if (errno || *end || !*argv[i] || value < 3 || value > INT_MAX || app.gpu_fd >= 0) fail("invalid inherited GPU socket descriptor");
            app.gpu_fd = (int)value;
        } else if (!strcmp(argv[i], "--gpu-generation") && i + 1 < argc) {
            char *end; errno = 0; const char *text = argv[++i];
            if (*text < '0' || *text > '9' || app.gpu_generation) fail("invalid GPU generation");
            app.gpu_generation = strtoull(text, &end, 10);
            if (errno || *end || !app.gpu_generation) fail("invalid GPU generation");
        }
        else if (!strcmp(argv[i], "--saver-generation") && i + 1 < argc) {
            char *end; errno = 0; const char *text = argv[++i];
            if (*text < '0' || *text > '9' || app.saver_generation) fail("invalid saver epoch");
            app.saver_generation = strtoull(text, &end, 10);
            if (errno || *end || !app.saver_generation) fail("invalid saver epoch");
        }
        else if (!strcmp(argv[i], "--help")) {
            puts("Usage: quake-viewer --frame PATH --socket PATH --gpu-fd FD --gpu-generation GENERATION [--saver-generation GENERATION]"); return 0;
        } else { fprintf(stderr, "quake-viewer: unknown or incomplete argument: %s\n", argv[i]); return 2; }
    }
    if (!app.frame_path || !app.socket_path) fail("--frame and --socket are required");
    if (app.gpu_fd < 0 || !app.gpu_generation) fail("--gpu-fd and --gpu-generation are required");
    int type, domain; socklen_t length = sizeof(type);
    if (getsockopt(app.gpu_fd, SOL_SOCKET, SO_TYPE, &type, &length) || type != SOCK_SEQPACKET) fail("GPU endpoint must be SOCK_SEQPACKET");
    length = sizeof(domain);
    if (getsockopt(app.gpu_fd, SOL_SOCKET, SO_DOMAIN, &domain, &length) || domain != AF_UNIX) fail("GPU endpoint must be local AF_UNIX");
    if (fcntl(app.gpu_fd, F_SETFD, FD_CLOEXEC) < 0) fail("cannot protect GPU endpoint inheritance");
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    app.started = now_ms(); app.xkb_context = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (!app.xkb_context) fail("cannot initialize keyboard support");
    app.display = wl_display_connect(NULL); if (!app.display) fail("cannot connect to the Wayland desktop");
    app.registry = wl_display_get_registry(app.display);
    wl_registry_add_listener(app.registry, &registry_listener, &app);
    if (wl_display_roundtrip(app.display) < 0 || wl_display_roundtrip(app.display) < 0) fail("compositor disconnected during setup");
    if (!app.compositor || !app.shm || !app.layer_shell) fail("required Wayland protocols are unavailable");
    if (!app.subcompositor || !app.viewporter || !app.gpu.dmabuf || lq_gpu_wayland_formats_done(&app.gpu) < 0) fail("required GPU viewport/DMA-BUF protocols unavailable");
    diagnostic(&app, "GPU direct attachment; frozen heads use producer capture and SHM blur");
    if (!app.constraints || !app.relative_manager) fail("relative mouse and pointer-lock protocols are required for takeover");
    app.cursor_theme = wl_cursor_theme_load(NULL, 24, app.shm);
    if (app.cursor_theme) app.cursor = wl_cursor_theme_get_cursor(app.cursor_theme, "left_ptr");
    if (app.cursor) app.cursor_surface = wl_compositor_create_surface(app.compositor);
    for (Screen *screen = app.screens; screen; screen = screen->next) create_surface(screen);
    int display_fd = wl_display_get_fd(app.display);
    int status = 0;
    while (!app.quit && !interrupted) {
        if (wl_display_dispatch_pending(app.display) < 0) { status = 1; break; }
        remove_screens(&app); collect_buffers(&app);
        if (now_ms() >= app.next_query) { try_frame_mapping(&app); update_views(&app); }
        saver_control_service(&app);
        repeat_keys(&app); input_service(&app);
        gpu_liveness_service(&app); gpu_service(&app); gpu_control_service(&app); gpu_capture_service(&app);
        for (Screen *screen = app.screens; screen; screen = screen->next) {
            if (screen->cpu_snapshot) blur_update(screen);
            render(screen);
        }
        int prepared;
        while ((prepared = wl_display_prepare_read(app.display)) != 0) {
            if (wl_display_dispatch_pending(app.display) < 0) { status = 1; app.quit = true; break; }
        }
        if (app.quit) { if (prepared == 0) wl_display_cancel_read(app.display); break; }
        int flushed = wl_display_flush(app.display);
        struct pollfd pfds[4] = {{.fd = display_fd, .events = POLLIN},
            {.fd = app.input_active ? app.input_fd : -1, .events = POLLIN},
            {.fd = app.gpu_fd, .events = POLLIN},
            {.fd = app.saver_request != SAVER_REQUEST_NONE ? app.saver_control_fd : -1, .events = POLLIN}};
        if (app.saver_connecting || app.saver_request_sent < app.saver_request_used) pfds[3].events |= POLLOUT;
        if (!app.gpu.capabilities_sent || lq_gpu_wayland_next_release(&app.gpu, &(struct lq_gpu_release){0})) pfds[2].events |= POLLOUT;
        if (app.input_connecting || app.input_sent < app.input_used) pfds[1].events |= POLLOUT;
        if (flushed < 0 && errno == EAGAIN) pfds[0].events |= POLLOUT;
        else if (flushed < 0) { wl_display_cancel_read(app.display); status = 1; break; }
        int result = poll(pfds, 4, idle_timeout(&app));
        if (result > 0 && (pfds[0].revents & POLLIN)) {
            if (wl_display_read_events(app.display) < 0) { status = 1; break; }
        } else wl_display_cancel_read(app.display);
        if (result < 0 && errno != EINTR) { status = 1; break; }
        if (result > 0 && (pfds[0].revents & (POLLERR | POLLHUP | POLLNVAL))) { status = 1; break; }
        if (result > 0 && pfds[1].revents) input_service(&app);
        if (result > 0 && pfds[2].revents) gpu_service(&app);
        if (result > 0 && pfds[3].revents) saver_control_io(&app);
    }
    if (app.gpu.failed) status = 1;
    cleanup(&app);
    return status;
}
