/* SPDX-License-Identifier: 0BSD */
#include "gpu_gbm.h"
#include <GL/gl.h>
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <unistd.h>

static bool valid_modifiers(const uint64_t *modifiers,uint32_t count) {
    if(!modifiers || !count || count>LQ_GPU_MODIFIERS)return false;
    for(uint32_t i=0;i<count;++i)if(modifiers[i]==LQ_GPU_MODIFIER_INVALID)return false;
    return true;
}
static bool accepted_modifier(const struct lq_gpu_gbm *p,uint64_t modifier) {
    for(uint32_t i=0;i<p->modifier_count;++i)if(p->modifiers[i]==modifier)return true;
    return false;
}
struct gbm_surface *lq_gpu_gbm_surface(struct gbm_device *device,uint32_t width,
    uint32_t height,uint32_t format,const uint64_t *modifiers,uint32_t count) {
    if(!device || width<32 || width>4096 || height<32 || height>4096 ||
        (format!=LQ_GPU_FORMAT_XRGB8888 && format!=LQ_GPU_FORMAT_ARGB8888) ||
        !valid_modifiers(modifiers,count)){errno=EINVAL;return NULL;}
    return gbm_surface_create_with_modifiers2(device,width,height,format,modifiers,count,GBM_BO_USE_RENDERING);
}
int lq_gpu_gbm_init(struct lq_gpu_gbm *p,int socket_fd,EGLDisplay display,
    EGLSurface egl_surface,struct gbm_surface *surface,uint64_t generation,
    uint32_t width,uint32_t height,uint32_t format,uint32_t flags,
    uint32_t par_num,uint32_t par_den,const uint64_t *modifiers,uint32_t count) {
    struct lq_gpu_frame shape={.generation=generation,.serial=1,.width=width,.height=height,.format=format,
        .flags=flags,.par_num=par_num,.par_den=par_den,.plane_count=1,.stride={width*4,0,0,0},.fd={0,-1,-1,-1}};
    if(!p || socket_fd<0 || display==EGL_NO_DISPLAY || egl_surface==EGL_NO_SURFACE || !surface ||
        !valid_modifiers(modifiers,count) || !lq_gpu_frame_valid(&shape)){errno=EINVAL;return -1;}
    /* The caller must supply fresh zeroed storage, never overwrite a live pool. */
    if(p->leases.generation || p->leases.in_flight || p->leases.retired){errno=EBUSY;return -1;}
    memset(p,0,sizeof(*p));p->display=display;p->egl_surface=egl_surface;p->surface=surface;p->socket_fd=socket_fd;
    p->width=width;p->height=height;p->format=format;p->flags=flags;p->par_num=par_num;p->par_den=par_den;
    p->modifier_count=count;memcpy(p->modifiers,modifiers,count*sizeof(*modifiers));p->next_serial=1;
    return lq_gpu_leases_init(&p->leases,generation);
}
void lq_gpu_gbm_retire(struct lq_gpu_gbm *p){if(p)lq_gpu_leases_retire(&p->leases);}
bool lq_gpu_gbm_clean(const struct lq_gpu_gbm *p){return p && !p->leases.retired && !p->leases.in_flight;}
static int fatal(struct lq_gpu_gbm *p,int error){lq_gpu_gbm_retire(p);errno=error;return -1;}
int lq_gpu_gbm_releases(struct lq_gpu_gbm *p) {
    if(!p || p->leases.retired){errno=EINVAL;return -1;}
    int released=0;
    for(unsigned i=0;i<LQ_GPU_SLOTS*2;++i) {
        struct lq_gpu_packet packet;int result=lq_gpu_receive(p->socket_fd,&packet);
        if(result==0)break;
        if(result<0)return fatal(p,errno);
        if(packet.kind!=LQ_GPU_RELEASE){lq_gpu_close_frame(&packet.frame);return fatal(p,EPROTO);}
        uint32_t slot=packet.release.slot;
        if(slot>=LQ_GPU_SLOTS || !p->locked[slot] ||
           lq_gpu_leases_release(&p->leases,&packet.release)<0)return fatal(p,EPROTO);
        gbm_surface_release_buffer(p->surface,p->locked[slot]);p->locked[slot]=NULL;++released;
    }
    return released;
}
int lq_gpu_gbm_ready(struct lq_gpu_gbm *p) {
    if(!p || p->leases.retired){errno=EINVAL;return -1;}
    if(lq_gpu_gbm_releases(p)<0)return -1;
    for(uint32_t slot=0;slot<LQ_GPU_SLOTS;++slot)
        if(lq_gpu_leases_can_acquire(&p->leases,slot))return gbm_surface_has_free_buffers(p->surface)?1:0;
    return 0;
}
int lq_gpu_gbm_publish(struct lq_gpu_gbm *p) {
    if(!p || p->leases.retired){errno=EINVAL;return -1;}
    if(lq_gpu_gbm_releases(p)<0)return -1;
    uint32_t slot;
    for(slot=0;slot<LQ_GPU_SLOTS;++slot)if(lq_gpu_leases_can_acquire(&p->leases,slot))break;
    if(slot==LQ_GPU_SLOTS || !gbm_surface_has_free_buffers(p->surface))return 0;
    if(!p->next_serial || eglGetCurrentDisplay()!=p->display ||
       eglGetCurrentSurface(EGL_DRAW)!=p->egl_surface)return fatal(p,EINVAL);
    /* The initial conservative sync baseline waits before/after swapping;
     * no glReadPixels, mapping, or CPU pixel copy occurs. */
    glFinish();if(glGetError()!=GL_NO_ERROR)return fatal(p,EIO);
    if(!eglSwapBuffers(p->display,p->egl_surface))return fatal(p,EIO);
    glFinish();if(glGetError()!=GL_NO_ERROR)return fatal(p,EIO);
    struct gbm_bo *bo=gbm_surface_lock_front_buffer(p->surface);
    if(!bo)return fatal(p,EIO);
    for(uint32_t i=0;i<LQ_GPU_SLOTS;++i)if(p->locked[i]==bo)return fatal(p,EBUSY);
    struct lq_gpu_frame frame={.generation=p->leases.generation,.serial=p->next_serial,.slot=slot,
        .width=gbm_bo_get_width(bo),.height=gbm_bo_get_height(bo),.format=gbm_bo_get_format(bo),
        .modifier=gbm_bo_get_modifier(bo),.flags=p->flags,.par_num=p->par_num,.par_den=p->par_den,
        .fd={-1,-1,-1,-1}};
    int planes=gbm_bo_get_plane_count(bo);
    if(frame.width!=p->width || frame.height!=p->height || frame.format!=p->format ||
       !accepted_modifier(p,frame.modifier) || planes<1 || planes>(int)LQ_GPU_PLANES){
        gbm_surface_release_buffer(p->surface,bo);return fatal(p,ENOTSUP);}
    frame.plane_count=(uint32_t)planes;
    for(int i=0;i<planes;++i){frame.fd[i]=gbm_bo_get_fd_for_plane(bo,i);
        if(frame.fd[i]>=0 && fcntl(frame.fd[i],F_SETFD,FD_CLOEXEC)<0){close(frame.fd[i]);frame.fd[i]=-1;}
        frame.offset[i]=gbm_bo_get_offset(bo,i);frame.stride[i]=gbm_bo_get_stride_for_plane(bo,i);}
    int sent=lq_gpu_send_frame(p->socket_fd,&frame);int saved_errno=errno;
    lq_gpu_close_frame(&frame);
    if(sent!=1){gbm_surface_release_buffer(p->surface,bo);return sent<0?fatal(p,saved_errno):0;}
    p->locked[slot]=bo;
    if(lq_gpu_leases_acquire(&p->leases,slot,p->next_serial)<0)return fatal(p,EPROTO);
    ++p->next_serial;return 1;
}
