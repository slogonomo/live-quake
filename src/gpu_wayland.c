/* SPDX-License-Identifier: 0BSD */
#include "gpu_wayland.h"
#include <errno.h>
#include <string.h>

static void report_error(struct lq_gpu_wayland *w,const char *reason) {
    w->failed=true;if(w->error)w->error(w->context,reason);
}
static bool accepted(const struct lq_gpu_wayland *w,uint32_t format,uint64_t modifier) {
    for(uint32_t i=0;i<w->format_count;++i)
        if(w->formats[i].format==format && w->formats[i].modifier==modifier)return true;
    return false;
}
static void format_event(void *data,struct zwp_linux_dmabuf_v1 *dmabuf,uint32_t format) {
    (void)data;(void)dmabuf;(void)format;
    /* A format alone does not advertise an explicit allocation modifier. */
}
static void modifier_event(void *data,struct zwp_linux_dmabuf_v1 *dmabuf,uint32_t format,uint32_t hi,uint32_t lo) {
    (void)dmabuf;struct lq_gpu_wayland *w=data;
    uint64_t modifier=((uint64_t)hi<<32)|lo;
    if(w->closed || w->failed || modifier==LQ_GPU_MODIFIER_INVALID ||
        (format!=LQ_GPU_FORMAT_XRGB8888 && format!=LQ_GPU_FORMAT_ARGB8888) || accepted(w,format,modifier))return;
    if(w->formats_ready){report_error(w,"Unexpected DMA-BUF capability change");return;}
    if(w->format_count==LQ_GPU_FORMAT_PAIRS){report_error(w,"DMA-BUF capability limit exceeded");return;}
    w->formats[w->format_count++]=(struct lq_gpu_format_pair){.format=format,.modifier=modifier};
}
static const struct zwp_linux_dmabuf_v1_listener dmabuf_listener={.format=format_event,.modifier=modifier_event};
static void consumer_destroy(struct lq_gpu_wayland_consumer *c) {
    if(c->params){zwp_linux_buffer_params_v1_destroy(c->params);c->params=NULL;}
    if(c->buffer){wl_buffer_destroy(c->buffer);c->buffer=NULL;}
}
static void buffer_release(void *data,struct wl_buffer *buffer) {
    struct lq_gpu_wayland_consumer *c=data;struct lq_gpu_wayland_slot *slot=c->slot;
    if(buffer!=c->buffer || !c->committed || !slot->committed) {
        report_error(slot->owner,"Unexpected DMA-BUF release");return;
    }
    c->committed=false;--slot->committed;consumer_destroy(c);
    if(!slot->committed)slot->release_ready=true;
}
static const struct wl_buffer_listener buffer_listener={.release=buffer_release};
static void all_created(struct lq_gpu_wayland_slot *slot) {
    if(slot->owner->failed) {
        for(uint32_t i=0;i<slot->count;++i)consumer_destroy(&slot->consumers[i]);
        slot->release_ready=true;return;
    }
    struct wl_buffer *buffers[LQ_GPU_CONSUMERS]={0};
    for(uint32_t i=0;i<slot->count;++i)buffers[i]=slot->consumers[i].buffer;
    uint32_t mask=slot->owner->ready(slot->owner->context,&slot->frame,buffers,slot->count);
    if(mask&~((1u<<slot->count)-1)) {
        /* Caller may have committed already: no fabricated release/ACK. */
        report_error(slot->owner,"Invalid DMA-BUF consumer mask");return;
    }
    for(uint32_t i=0;i<slot->count;++i) {
        struct lq_gpu_wayland_consumer *c=&slot->consumers[i];
        if(mask&(1u<<i)){c->committed=true;++slot->committed;}
        else consumer_destroy(c);
    }
    if(!slot->committed)slot->release_ready=true;
}
static void params_created(void *data,struct zwp_linux_buffer_params_v1 *params,struct wl_buffer *buffer) {
    struct lq_gpu_wayland_consumer *c=data;struct lq_gpu_wayland_slot *slot=c->slot;
    if(params!=c->params || !slot->pending || c->buffer){
        wl_buffer_destroy(buffer);report_error(slot->owner,"Unexpected DMA-BUF import event");return;
    }
    zwp_linux_buffer_params_v1_destroy(c->params);c->params=NULL;c->buffer=buffer;
    if(wl_buffer_add_listener(buffer,&buffer_listener,c)<0){report_error(slot->owner,"Cannot track DMA-BUF release");return;}
    if(!--slot->pending)all_created(slot);
}
static void params_failed(void *data,struct zwp_linux_buffer_params_v1 *params) {
    struct lq_gpu_wayland_consumer *c=data;struct lq_gpu_wayland_slot *slot=c->slot;
    if(params!=c->params){report_error(slot->owner,"Unexpected DMA-BUF import failure");return;}
    /* Ready callback has not run: nothing was attached or committed. */
    for(uint32_t i=0;i<slot->count;++i)consumer_destroy(&slot->consumers[i]);
    slot->pending=0;slot->release_ready=true;
    report_error(slot->owner,"Compositor rejected DMA-BUF import");
}
static const struct zwp_linux_buffer_params_v1_listener params_listener={.created=params_created,.failed=params_failed};
int lq_gpu_wayland_init(struct lq_gpu_wayland *w,struct zwp_linux_dmabuf_v1 *dmabuf,
    lq_gpu_wayland_ready_fn ready,lq_gpu_wayland_error_fn error,void *context) {
    if(!w || !dmabuf || !ready || w->dmabuf ||
       zwp_linux_dmabuf_v1_get_version(dmabuf)!=3){errno=EINVAL;return -1;}
    memset(w,0,sizeof(*w));w->dmabuf=dmabuf;w->ready=ready;w->error=error;w->context=context;
    if(zwp_linux_dmabuf_v1_add_listener(dmabuf,&dmabuf_listener,w)<0){w->dmabuf=NULL;errno=EINVAL;return -1;}
    return 0;
}
int lq_gpu_wayland_formats_done(struct lq_gpu_wayland *w) {
    if(!w || w->closed || w->failed || !w->format_count){errno=ENOTSUP;return -1;}
    w->formats_ready=true;return 0;
}
uint32_t lq_gpu_wayland_modifiers(const struct lq_gpu_wayland *w,uint32_t format,uint64_t *modifiers,uint32_t capacity) {
    if(!w || !w->formats_ready || w->failed || w->closed || !modifiers)return 0;
    uint32_t count=0;
    for(uint32_t i=0;i<w->format_count;++i)if(w->formats[i].format==format){
        if(count==capacity)return 0;
        modifiers[count++]=w->formats[i].modifier;}
    return count;
}
int lq_gpu_wayland_send_capabilities(struct lq_gpu_wayland *w,int socket_fd,uint64_t generation) {
    if(!w || !w->formats_ready || w->failed || w->closed || !generation ||
       (w->capability_generation && w->capability_generation!=generation) ||
       (w->generation && w->generation!=generation)){errno=EINVAL;return -1;}
    w->capability_generation=generation;w->generation=generation;
    if(w->capabilities_sent)return 1;
    while(w->capability_next<w->format_count) {
        struct lq_gpu_format_pair *pair=&w->formats[w->capability_next];
        struct lq_gpu_capability capability={.generation=generation,.index=w->capability_next+1,.format=pair->format,.modifier=pair->modifier};
        int sent=lq_gpu_send_capability(socket_fd,&capability);if(sent!=1)return sent;
        ++w->capability_next;
    }
    struct lq_gpu_capabilities_done done={.generation=generation,.count=w->format_count};
    int sent=lq_gpu_send_capabilities_done(socket_fd,&done);if(sent==1)w->capabilities_sent=true;return sent;
}
int lq_gpu_wayland_import(struct lq_gpu_wayland *w,struct lq_gpu_frame *frame,uint32_t consumers) {
    if(!frame){errno=EINVAL;return -1;}
    if(!w || !w->formats_ready || w->failed || w->closed || consumers>LQ_GPU_CONSUMERS ||
       !lq_gpu_frame_valid(frame) || !accepted(w,frame->format,frame->modifier) ||
       w->slots[frame->slot].occupied || (w->generation && w->generation!=frame->generation) || frame->serial<=w->last_serial) {
        lq_gpu_close_frame(frame);errno=EPROTO;return -1;
    }
    struct lq_gpu_wayland_slot *slot=&w->slots[frame->slot];
    memset(slot,0,sizeof(*slot));slot->owner=w;slot->frame=*frame;slot->count=consumers;slot->pending=consumers;slot->occupied=true;
    w->generation=frame->generation;w->last_serial=frame->serial;
    for(uint32_t i=0;i<consumers;++i) {
        struct lq_gpu_wayland_consumer *c=&slot->consumers[i];c->slot=slot;
        c->params=zwp_linux_dmabuf_v1_create_params(w->dmabuf);
        if(!c->params || zwp_linux_buffer_params_v1_add_listener(c->params,&params_listener,c)<0) {
            for(uint32_t j=0;j<=i;++j)consumer_destroy(&slot->consumers[j]);
            lq_gpu_close_frame(frame);for(uint32_t j=0;j<LQ_GPU_PLANES;++j)slot->frame.fd[j]=-1;
            report_error(w,"Cannot create DMA-BUF import request");errno=ENOMEM;return -1;
        }
        for(uint32_t plane=0;plane<frame->plane_count;++plane)
            zwp_linux_buffer_params_v1_add(c->params,frame->fd[plane],plane,frame->offset[plane],frame->stride[plane],
                (uint32_t)(frame->modifier>>32),(uint32_t)frame->modifier);
        zwp_linux_buffer_params_v1_create(c->params,(int32_t)frame->width,(int32_t)frame->height,frame->format,frame->flags);
    }
    lq_gpu_close_frame(frame);for(uint32_t j=0;j<LQ_GPU_PLANES;++j)slot->frame.fd[j]=-1;
    if(!consumers)slot->release_ready=true;
    return 0;
}
bool lq_gpu_wayland_next_release(const struct lq_gpu_wayland *w,struct lq_gpu_release *release) {
    if(!w || w->closed || !release)return false;
    for(uint32_t i=0;i<LQ_GPU_SLOTS;++i)if(w->slots[i].occupied && w->slots[i].release_ready) {
        *release=(struct lq_gpu_release){.generation=w->slots[i].frame.generation,.serial=w->slots[i].frame.serial,.slot=i};return true;}
    return false;
}
int lq_gpu_wayland_release_sent(struct lq_gpu_wayland *w,const struct lq_gpu_release *release) {
    if(!w || !release || release->slot>=LQ_GPU_SLOTS){errno=EINVAL;return -1;}
    struct lq_gpu_wayland_slot *slot=&w->slots[release->slot];
    if(!slot->occupied || !slot->release_ready || slot->frame.generation!=release->generation || slot->frame.serial!=release->serial){errno=EINVAL;return -1;}
    memset(slot,0,sizeof(*slot));return 0;
}
void lq_gpu_wayland_destroy(struct lq_gpu_wayland *w) {
    if(!w || w->closed)return;
    w->closed=true;
    for(uint32_t slot=0;slot<LQ_GPU_SLOTS;++slot)
        for(uint32_t i=0;i<w->slots[slot].count;++i)consumer_destroy(&w->slots[slot].consumers[i]);
    if(w->dmabuf){zwp_linux_dmabuf_v1_destroy(w->dmabuf);w->dmabuf=NULL;}
}
