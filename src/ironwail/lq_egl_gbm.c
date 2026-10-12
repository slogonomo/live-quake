// SPDX-License-Identifier: GPL-2.0-or-later
/* Explicit-fd Ironwail OpenGL/GBM backend. CPU pixels are read only for an
 * explicit freeze capture; normal presentation remains direct DMA-BUF. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "quakedef.h"
#include "glquake.h"
#include "lq_egl_offscreen.h"
#include "gpu_gbm.h"
#include <EGL/eglext.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <time.h>
#include <unistd.h>

int lq_offscreen_width, lq_offscreen_height, lq_offscreen_done;
static EGLDisplay display=EGL_NO_DISPLAY;
static EGLContext context=EGL_NO_CONTEXT;
static EGLSurface surface=EGL_NO_SURFACE;
static struct gbm_device *device;
static struct gbm_surface *native_surface;
static struct lq_gpu_gbm producer;
static struct lq_gpu_capabilities capabilities;
static void *gl_library;
static lq_frame_callback_t frame_callback;
static int socket_fd=-1, render_fd=-1;
static uint64_t generation;
static int capture_armed;
static byte *capture_pixels;
static size_t capture_capacity;
extern int retro_lq_frame_visible(void);

static double monotonic(void)
{
    struct timespec now;
    if(clock_gettime(CLOCK_MONOTONIC,&now))return 0;
    return now.tv_sec+now.tv_nsec/1e9;
}

/* Called once by the native frontend before retro_load_game. Duplicate only
 * explicit inherited descriptors; the core never opens ambient device paths. */
int retro_lq_gpu_setup(int socket,int render,uint64_t pool_generation)
{
    struct stat info;
    struct ucred peer;
    socklen_t length=sizeof(peer);
    int type;
    socklen_t type_length=sizeof(type);
    if(socket_fd>=0 || display!=EGL_NO_DISPLAY || !pool_generation ||
       getsockopt(socket,SOL_SOCKET,SO_TYPE,&type,&type_length) || type!=SOCK_SEQPACKET ||
       getsockopt(socket,SOL_SOCKET,SO_PEERCRED,&peer,&length) || peer.uid!=getuid() ||
       fstat(render,&info) || !S_ISCHR(info.st_mode) || major(info.st_rdev)!=226 ||
       minor(info.st_rdev)<128 || minor(info.st_rdev)>191)return 0;
    int copy_socket=fcntl(socket,F_DUPFD_CLOEXEC,3);
    int copy_render=fcntl(render,F_DUPFD_CLOEXEC,3);
    if(copy_socket<0 || copy_render<0) {
        if(copy_socket>=0)close(copy_socket);
        if(copy_render>=0)close(copy_render);
        return 0;
    }
    int flags=fcntl(copy_socket,F_GETFL);
    if(flags<0 || fcntl(copy_socket,F_SETFL,flags|O_NONBLOCK)<0) {
        close(copy_socket);close(copy_render);return 0;
    }
    socket_fd=copy_socket;render_fd=copy_render;generation=pool_generation;
    return 1;
}

void LQ_EGL_RequireIsolation(void)
{
    /* This API name is retained while the private pbuffer adapter is being
     * replaced. Explicit descriptors are required in every deployment. */
    if(socket_fd<0 || render_fd<0)Sys_Error("GPU presentation descriptors are required");
}

void LQ_EGL_SetFrameCallback(lq_frame_callback_t callback){frame_callback=callback;}

/* The frontend arms exactly one guarded, render-only lq_refresh and cancels
 * after it returns. No capture survives a failed refresh into normal play. */
int retro_lq_capture(int enabled)
{
    if(!enabled){capture_armed=0;return 1;}
    if(enabled!=1 || capture_armed || !frame_callback || display==EGL_NO_DISPLAY ||
       eglGetCurrentDisplay()!=display || eglGetCurrentContext()!=context ||
       !retro_lq_frame_visible())return 0;
    /* The frontend waits only for genuine producer releases before arming.
     * No other render can consume this buffer on the same native thread. */
    if(!LQ_EGL_Ready())return 0;
    capture_armed=1;
    return 1;
}

/* Control-side query: a failed transport is an error, never evidence that
 * a successfully parsed checkpoint is invalid. Ordinary rendering keeps
 * its fatal connection handling through LQ_EGL_Ready below. */
int retro_lq_gpu_ready(void)
{
    if(display==EGL_NO_DISPLAY)return 1;
    return lq_gpu_gbm_ready(&producer);
}

int LQ_EGL_Ready(void)
{
    int ready=retro_lq_gpu_ready();
    if(ready<0)Sys_Error("GPU presentation connection failed");
    return ready;
}

/* Paused games may retire completed compositor leases without running game
 * tics or OpenGL work. The frontend calls this alongside control I/O. */
int retro_lq_gpu_poll(void)
{
    return display==EGL_NO_DISPLAY?0:lq_gpu_gbm_releases(&producer);
}

void LQ_EGL_Create(int width,int height)
{
    if(width<320 || width>3840 || height<200 || height>2160 ||
       display!=EGL_NO_DISPLAY)Sys_Error("GPU dimensions or session are invalid");
    double deadline=monotonic()+2;
    for(;;) {
        int result=lq_gpu_capabilities_receive(socket_fd,&capabilities);
        if(result<0)Sys_Error("GPU format negotiation failed");
        if(result==1)break;
        if(monotonic()>=deadline)Sys_Error("GPU format negotiation timed out");
        struct pollfd wait={socket_fd,POLLIN,0};
        if(poll(&wait,1,20)<0 && errno!=EINTR)Sys_Error("GPU format negotiation failed");
    }
    if(capabilities.generation!=generation)Sys_Error("GPU pool generation differs");
    uint64_t modifiers[LQ_GPU_MODIFIERS];
    uint32_t format=LQ_GPU_FORMAT_XRGB8888;
    uint32_t count=lq_gpu_capabilities_modifiers(&capabilities,format,modifiers,LQ_GPU_MODIFIERS);
    if(!count) {
        format=LQ_GPU_FORMAT_ARGB8888;
        count=lq_gpu_capabilities_modifiers(&capabilities,format,modifiers,LQ_GPU_MODIFIERS);
    }
    if(!count)Sys_Error("No compatible GPU buffer format");
    device=gbm_create_device(render_fd);
    if(!device)Sys_Error("GPU buffer device unavailable");
    display=eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR,device,NULL);
    EGLint major_version,minor_version;
    if(display==EGL_NO_DISPLAY || !eglInitialize(display,&major_version,&minor_version) ||
       !eglBindAPI(EGL_OPENGL_API))Sys_Error("GPU EGL initialization failed");
    const EGLint attributes[]={EGL_SURFACE_TYPE,EGL_WINDOW_BIT,EGL_RENDERABLE_TYPE,EGL_OPENGL_BIT,
        EGL_RED_SIZE,8,EGL_GREEN_SIZE,8,EGL_BLUE_SIZE,8,EGL_DEPTH_SIZE,24,EGL_STENCIL_SIZE,8,EGL_NONE};
    EGLConfig choices[64],selected=NULL;
    EGLint number;
    if(!eglChooseConfig(display,attributes,choices,64,&number))Sys_Error("GPU EGL config query failed");
    for(EGLint i=0;i<number;++i) {
        EGLint visual;
        if(eglGetConfigAttrib(display,choices[i],EGL_NATIVE_VISUAL_ID,&visual) &&
           (uint32_t)visual==format){selected=choices[i];break;}
    }
    if(!selected)Sys_Error("No EGL config for negotiated GPU format");
    native_surface=lq_gpu_gbm_surface(device,(uint32_t)width,(uint32_t)height,format,modifiers,count);
    if(!native_surface)Sys_Error("GPU render surface allocation failed");
    surface=eglCreatePlatformWindowSurface(display,selected,native_surface,NULL);
    const EGLint core[]={EGL_CONTEXT_MAJOR_VERSION,4,EGL_CONTEXT_MINOR_VERSION,3,
        EGL_CONTEXT_OPENGL_PROFILE_MASK,EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT,EGL_NONE};
    context=eglCreateContext(display,selected,EGL_NO_CONTEXT,core);
    if(surface==EGL_NO_SURFACE || context==EGL_NO_CONTEXT ||
       !eglMakeCurrent(display,surface,surface,context))Sys_Error("OpenGL 4.3 context unavailable");
    if(!eglSwapInterval(display,0))Sys_Error("Cannot set native presentation pacing");
    gl_library=dlopen("libGL.so.1",RTLD_NOW|RTLD_LOCAL);
    if(!gl_library)Sys_Error("Cannot resolve OpenGL functions");
    if(lq_gpu_gbm_init(&producer,socket_fd,display,surface,native_surface,generation,
       (uint32_t)width,(uint32_t)height,format,0,1,1,modifiers,count)<0)
        Sys_Error("GPU presentation pool initialization failed");
    lq_offscreen_width=width;lq_offscreen_height=height;
    Con_Printf("GPU_EGL %d.%d\nGPU_GL_VENDOR %s\nGPU_GL_RENDERER %s\nGPU_GL_VERSION %s\n",
        major_version,minor_version,glGetString(GL_VENDOR),glGetString(GL_RENDERER),glGetString(GL_VERSION));
}

void *LQ_EGL_GetProcAddress(const char *name)
{
    void *value=dlsym(gl_library,name);
    return value?value:(void*)eglGetProcAddress(name);
}

static int capture_frame(void)
{
    const int width=lq_offscreen_width,height=lq_offscreen_height;
    if(width<320 || width>3840 || height<200 || height>2160)return 0;
    const size_t bytes=(size_t)width*height*4;
    if(bytes>capture_capacity) {
        byte *replacement=realloc(capture_pixels,bytes);
        if(!replacement)return 0;
        capture_pixels=replacement;capture_capacity=bytes;
    }
    PFNGLBINDBUFFERPROC bind_buffer=(PFNGLBINDBUFFERPROC)eglGetProcAddress("glBindBuffer");
    if(!bind_buffer || !GL_BindFramebufferFunc)return 0;
    /* The engine may leave pack state or a pixel buffer bound. Read into our
     * bounded CPU allocation, then restore every state changed by capture. */
    GLint read_framebuffer,draw_framebuffer,read_buffer,default_read_buffer=GL_NONE,draw_buffer=GL_NONE,pack_buffer,alignment,row_length,skip_rows,skip_pixels,swap_bytes,lsb_first;
    glGetIntegerv(GL_READ_FRAMEBUFFER_BINDING,&read_framebuffer);
    glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING,&draw_framebuffer);
    glGetIntegerv(GL_READ_BUFFER,&read_buffer);
    glGetIntegerv(GL_PIXEL_PACK_BUFFER_BINDING,&pack_buffer);
    glGetIntegerv(GL_PACK_ALIGNMENT,&alignment);
    glGetIntegerv(GL_PACK_ROW_LENGTH,&row_length);
    glGetIntegerv(GL_PACK_SKIP_ROWS,&skip_rows);
    glGetIntegerv(GL_PACK_SKIP_PIXELS,&skip_pixels);
    glGetIntegerv(GL_PACK_SWAP_BYTES,&swap_bytes);
    glGetIntegerv(GL_PACK_LSB_FIRST,&lsb_first);
    if(glGetError()!=GL_NO_ERROR)return 0;
    /* Read the default framebuffer's actual render target. EGL window
     * surfaces may expose only GL_BACK despite DOUBLEBUFFER being false. */
    GL_BindFramebufferFunc(GL_FRAMEBUFFER,0);
    glGetIntegerv(GL_DRAW_BUFFER0,&draw_buffer);
    glGetIntegerv(GL_READ_BUFFER,&default_read_buffer);
    glReadBuffer((GLenum)draw_buffer);
    bind_buffer(GL_PIXEL_PACK_BUFFER,0);
    glPixelStorei(GL_PACK_ALIGNMENT,1);
    glPixelStorei(GL_PACK_ROW_LENGTH,0);
    glPixelStorei(GL_PACK_SKIP_ROWS,0);
    glPixelStorei(GL_PACK_SKIP_PIXELS,0);
    glPixelStorei(GL_PACK_SWAP_BYTES,GL_FALSE);
    glPixelStorei(GL_PACK_LSB_FIRST,GL_FALSE);
    GLenum error=glGetError();
    if(error==GL_NO_ERROR && draw_buffer==GL_NONE)error=GL_INVALID_OPERATION;
    if(error==GL_NO_ERROR) {
        glReadPixels(0,0,width,height,GL_RGBA,GL_UNSIGNED_BYTE,capture_pixels);
        error=glGetError();
    }
    glReadBuffer((GLenum)default_read_buffer);
    GL_BindFramebufferFunc(GL_READ_FRAMEBUFFER,(GLuint)read_framebuffer);
    glReadBuffer((GLenum)read_buffer);
    GL_BindFramebufferFunc(GL_DRAW_FRAMEBUFFER,(GLuint)draw_framebuffer);
    bind_buffer(GL_PIXEL_PACK_BUFFER,(GLuint)pack_buffer);
    glPixelStorei(GL_PACK_ALIGNMENT,alignment);
    glPixelStorei(GL_PACK_ROW_LENGTH,row_length);
    glPixelStorei(GL_PACK_SKIP_ROWS,skip_rows);
    glPixelStorei(GL_PACK_SKIP_PIXELS,skip_pixels);
    glPixelStorei(GL_PACK_SWAP_BYTES,swap_bytes);
    glPixelStorei(GL_PACK_LSB_FIRST,lsb_first);
    if(glGetError()!=GL_NO_ERROR || error!=GL_NO_ERROR)return 0;
    /* GL rows start at the bottom. The libretro shim produces top-down
     * XRGB8888, and the frontend copies those bytes into the seqlock bridge. */
    frame_callback(width,height,capture_pixels,1);
    return 1;
}

void LQ_EGL_EndFrame(void)
{
    const int capture=capture_armed;
    capture_armed=0;
    if(!retro_lq_frame_visible())return;
    if(capture) {
        /* A freeze refresh does not swap or acquire another producer BO.
         * Failed or timed-out captures remain undisplayed in the viewer. */
        capture_frame();
        return;
    }
    int sent=lq_gpu_gbm_publish(&producer);
    if(sent<0)Sys_Error("GPU frame publication failed");
    /* NULL carries dimensions/frame count only during normal presentation. */
    if(sent && frame_callback)frame_callback(lq_offscreen_width,lq_offscreen_height,NULL,0);
}

void LQ_EGL_Shutdown(void)
{
    capture_armed=0;
    free(capture_pixels);capture_pixels=NULL;capture_capacity=0;
    if(display==EGL_NO_DISPLAY)return;
    if(!lq_gpu_gbm_clean(&producer)) {
        /* A disconnected or stopped viewer cannot acknowledge outstanding
         * compositor consumers. Retire the pool until this engine exits. */
        lq_gpu_gbm_retire(&producer);
        return;
    }
    eglMakeCurrent(display,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);
    eglDestroySurface(display,surface);eglDestroyContext(display,context);
    gbm_surface_destroy(native_surface);eglTerminate(display);gbm_device_destroy(device);
    close(socket_fd);close(render_fd);socket_fd=render_fd=-1;display=EGL_NO_DISPLAY;
    if(gl_library)dlclose(gl_library);
    gl_library=NULL;
}
