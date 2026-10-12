// SPDX-License-Identifier: 0BSD
/* Headless libretro frontend. Audio is discarded unless explicitly enabled.
 * The patched GPL core is loaded from the explicit build path. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#include <libretro.h>
#include "bridge.h"
#include "audio.h"
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <stddef.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
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

static DDHeader working, *h=&working, *published;
static uint32_t *frame_pixels;
static int pixels_dirty;
static int gpu_socket=-1, gpu_render=-1;
static uint64_t gpu_generation;
static int (*gpu_poll_core)(void);
static int (*gpu_ready_core)(void);
static int (*capture_core)(int);
static void (*replan_core)(void);
static int (*nextmap_core)(char *,size_t);
static int (*match_state_core)(char *,size_t);
static int (*map_exists_core)(const char *);
static int (*dm_rejected_core)(void);
static int (*dm_config_core)(const char *,int,int,int);
static void (*hide_chatter_core)(int), (*pin_view_core)(int);
static int capture_active;
static double frame_period=1.0/60;
static unsigned render_rate=60;
static const char *save_dir, *resolution="960x540";
static char game_dir[16]="id1";
static const char *start_map="lq_e0m1";
static char last_dm_map[16];
static const char *dm_maps="dm1,dm2,dm3,dm4,dm5,dm6", *dm_time="6", *dm_frags="25", *dm_bots="4", *edition="original";
static int requested_skill=1;
static int deathmatch_mode;
static volatile sig_atomic_t stopping;
static int paused=1, bot=1, audible=0;
static unsigned char mouse_buttons[5];
static int exit_save=1;
static int load_pending;
static uint64_t load_progress;
static unsigned quit_seen;
static uint64_t frames;
static void (*run_core)(void), (*unload_core)(void), (*deinit_core)(void);
static float (*human_viewsize_core)(void);
static void (*set_human_viewsize_core)(float);
static int (*bot_core)(int);
static void (*state_core)(DDHeader *), (*key_core)(int,int), (*mouse_core)(int,int);
static int (*command_core)(const char *);
static int (*world_ok_core)(void);
static int (*load_ok_core)(void);
static int (*save_ok_core)(void);
static int (*display_ok_core)(void);
static void (*loading_core)(int);
static double now(void);
static int clean_bot_frame;
static int bot_refresh_pending;
static double bot_refresh_retry_at;
static double bot_refresh_started_at, bot_refresh_log_at;
static double bot_refresh_retry_delay=.1;
static int bot_refresh_warned, bot_refresh_logged;
static void reset_bot_refresh(void) {
    bot_refresh_pending=bot_refresh_warned=bot_refresh_logged=0;
    bot_refresh_retry_at=bot_refresh_started_at=bot_refresh_log_at=0;
    bot_refresh_retry_delay=.1;
}
enum WaitFailure { WAIT_NONE, WAIT_STARVED, WAIT_TRANSPORT, WAIT_INTERRUPTED };
static enum WaitFailure wait_failure;
static int wait_gpu_release(void) {
    if(stopping){wait_failure=WAIT_INTERRUPTED;return 0;}
    if(gpu_poll_core()<0){wait_failure=WAIT_TRANSPORT;return 0;}
    struct pollfd release={gpu_socket,POLLIN,0};
    int result=poll(&release,1,5);
    if(stopping){wait_failure=WAIT_INTERRUPTED;return 0;}
    if((result<0 && errno!=EINTR) || (release.revents&(POLLERR|POLLHUP|POLLNVAL))){
        wait_failure=WAIT_TRANSPORT;return 0;
    }
    return 1;
}
static int wait_gpu_ready(double deadline) {
    if(stopping){wait_failure=WAIT_INTERRUPTED;return 0;}
    while(!stopping) {
        int ready=gpu_ready_core();
        if(ready<0){wait_failure=WAIT_TRANSPORT;return 0;}
        if(ready){wait_failure=WAIT_NONE;return 1;}
        wait_failure=WAIT_STARVED;
        if(now()>=deadline || !wait_gpu_release())return 0;
    }
    wait_failure=WAIT_INTERRUPTED;return 0;
}
static int refresh_bot_frame(void) {
    uint64_t before=frames;
    clean_bot_frame=0;wait_failure=WAIT_NONE;
    double deadline=now()+.1;
    /* A paused ownership change needs a fresh clean frame. Wait only for
     * compositor releases, without simulation tics or catch-up. */
    if(!wait_gpu_ready(deadline))return 0;
    int rendered=command_core("lq_refresh");
    if(rendered && display_ok_core() && frames>before){clean_bot_frame=1;reset_bot_refresh();return 1;}
    return 0;
}
static int request_bot_refresh(void) {
    /* Ownership is already revoked. A leased pool delays the clean image,
     * rather than invalidating the game; repeat policy requests stay cheap. */
    if(bot_refresh_pending && bot && world_ok_core())return 1;
    reset_bot_refresh();
    if(refresh_bot_frame())return 1;
    bot_refresh_pending=wait_failure==WAIT_STARVED && bot && world_ok_core();
    if(bot_refresh_pending){bot_refresh_started_at=now();audio_gate(0);}
    return bot_refresh_pending;
}
static int wait_for_world(double duration) {
    double deadline=now()+duration;wait_failure=WAIT_NONE;
    state_core(h);
    while(!world_ok_core() && !stopping && now()<deadline) {
        /* Explicit signon restoration still waits for a drawable target.
         * Stop at actual completion, without any post-ready tics. */
        if(!wait_gpu_ready(deadline))return 0;
        state_core(h);uint64_t before=h->tic;
        run_core();state_core(h);
        if(stopping)break;
        if(load_pending) {
            /* Preserve progress over pending retries, but never simulate an
             * unbounded signon for one parsed checkpoint. Completion at the
             * one-second tick budget is allowed; an incomplete world is not. */
            uint64_t limit=render_rate?render_rate:1;
            uint64_t delta=h->tic>=before?h->tic-before:UINT64_MAX;
            if(load_progress>=limit || delta>limit-load_progress) {
                load_progress=limit;wait_failure=WAIT_NONE;return 0;
            }
            load_progress+=delta;
            if(load_progress==limit && !world_ok_core()){wait_failure=WAIT_NONE;return 0;}
        }
        if(!world_ok_core() && h->tic==before) {
            wait_failure=WAIT_NONE;
            if(!wait_gpu_release())return 0;
        }else wait_failure=WAIT_NONE;
    }
    if(stopping){wait_failure=WAIT_INTERRUPTED;return 0;}
    if(world_ok_core()){wait_failure=WAIT_NONE;return 1;}
    /* Classify the pool at the deadline, rather than remembering an earlier
     * busy interval or treating useful partial progress as a corrupt save. */
    int ready=gpu_ready_core();
    wait_failure=ready<0?WAIT_TRANSPORT:ready?WAIT_NONE:WAIT_STARVED;
    return 0;
}
static int restart_map(const char *map) {
    if(!map[0] || strlen(map)>48 || strspn(map,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")!=strlen(map))return 0;
    char command[80];snprintf(command,sizeof(command),"map %s",map);
    audio_gate(0);
    load_pending=0;
    load_progress=0;
    loading_core(1);
    int ok=command_core(command);
    bot_core(0);if(ok)ok=wait_for_world(1);bot_core(bot);
    loading_core(0);
    int ready=ok && world_ok_core() && (!bot || refresh_bot_frame());
    audio_gate(audible && !paused && !bot_refresh_pending);
    return ready;
}
static int restart_world(void) {return restart_map(deathmatch_mode && last_dm_map[0]?last_dm_map:start_map);}
static int startup_world_exit(void) {
    return deathmatch_mode && wait_failure==WAIT_NONE && !world_ok_core()?3:1;
}

static void die(const char *message) {perror(message); exit(1);}
static void signal_stop(int sig) {(void)sig;stopping=1;}
static double now(void) {struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static double next_frame_deadline(double scheduled) {
    /* Normal timer jitter preserves the clock's phase. After an overrun,
     * start one tick at completion, then resume the phase without replaying
     * expired ticks or adding idle time to a tick that used its full budget. */
    double next=scheduled+frame_period, finished=now();
    return finished>=next?finished:next;
}
static void log_core(enum retro_log_level level,const char *format,...) {
    char message[2048]; va_list args;(void)level;va_start(args,format);
    vsnprintf(message,sizeof(message),format,args);va_end(args);fputs(message,stderr);
}
static bool environment(unsigned cmd,void *data) {
    switch(cmd) {
    case RETRO_ENVIRONMENT_GET_LOG_INTERFACE: ((struct retro_log_callback*)data)->log=log_core;return true;
    case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY:
    case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY: *(const char**)data=save_dir;return true;
    case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT:
        return *(unsigned*)data==RETRO_PIXEL_FORMAT_XRGB8888;
    case RETRO_ENVIRONMENT_GET_VARIABLE: {
        struct retro_variable *v=data;v->value=NULL;
        if(!strcmp(v->key,"tyrquake_resolution"))v->value=resolution;
        else if(!strcmp(v->key,"live_quake_mode"))v->value=deathmatch_mode?"deathmatch":"single";
        else if(!strcmp(v->key,"live_quake_dm_maps"))v->value=dm_maps;
        else if(!strcmp(v->key,"live_quake_dm_timelimit"))v->value=dm_time;
        else if(!strcmp(v->key,"live_quake_dm_fraglimit"))v->value=dm_frags;
        else if(!strcmp(v->key,"live_quake_dm_bots"))v->value=dm_bots;
        else if(!strcmp(v->key,"live_quake_edition"))v->value=edition;
        return v->value!=NULL;
    }
    case RETRO_ENVIRONMENT_GET_VARIABLE_UPDATE: *(bool*)data=false;return true;
    case RETRO_ENVIRONMENT_GET_AUDIO_VIDEO_ENABLE: *(int*)data=3;return true;
    case RETRO_ENVIRONMENT_SET_KEYBOARD_CALLBACK:
    case RETRO_ENVIRONMENT_SET_VARIABLES:
    case RETRO_ENVIRONMENT_SET_INPUT_DESCRIPTORS:
    case RETRO_ENVIRONMENT_SET_CONTROLLER_INFO:
        return true;
    case RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO:
        return true;
    case RETRO_ENVIRONMENT_SET_GEOMETRY:
        return true;
    case RETRO_ENVIRONMENT_SHUTDOWN: stopping=1;return true;
    case RETRO_ENVIRONMENT_GET_MEMORY_STATUS: {
        struct retro_memory_status *m=data;m->total=m->free=512ULL*1024*1024;return true;
    }
    default:return false;
    }
}
static void video(const void *data,unsigned width,unsigned height,size_t pitch) {
    unsigned x,y;
    if(data==RETRO_HW_FRAME_BUFFER_VALID) {
        if(data!=RETRO_HW_FRAME_BUFFER_VALID || !width || !height ||
           width>DD_MAX_WIDTH || height>DD_MAX_HEIGHT)return;
        h->width=width;h->height=height;h->stride=width*4;h->frame=++frames;
        return; /* GPU pixels stay in leased DMA-BUFs. Header only. */
    }
    if(!data || data==RETRO_HW_FRAME_BUFFER_VALID || !capture_active)return;
    if(!width || !height || width>DD_MAX_WIDTH || height>DD_MAX_HEIGHT)return;
    if(bot && !display_ok_core())return;
    h->width=width;h->height=height;h->stride=width*4;
    if(pitch < width*4u)return;
    uint32_t *pixels=frame_pixels;
    for(y=0;y<height;y++)for(x=0;x<width;x++) {
        const uint32_t *row=(const uint32_t*)((const unsigned char*)data+y*pitch);
        pixels[y*width+x]=row[x]&0xffffff;
    }
    h->frame=++frames;
    pixels_dirty=1;
}
static size_t audio_batch(const int16_t *samples,size_t count) {
    unsigned peak=0;size_t i;
    if(audible && !paused)for(i=0;i<count*2;i++){unsigned v=abs(samples[i]);if(v>peak)peak=v;}
    h->audio_peak=peak;return audio_write(samples,count);
}
static void audio_one(int16_t l,int16_t r){int16_t v[2]={l,r};audio_batch(v,1);}
static void input_poll(void){}
static int16_t input_state(unsigned port,unsigned device,unsigned index,unsigned id){
    (void)index;
    if(port || device!=RETRO_DEVICE_MOUSE || bot || paused)return 0;
    const unsigned ids[]={RETRO_DEVICE_ID_MOUSE_LEFT,RETRO_DEVICE_ID_MOUSE_RIGHT,
        RETRO_DEVICE_ID_MOUSE_MIDDLE,RETRO_DEVICE_ID_MOUSE_WHEELUP,RETRO_DEVICE_ID_MOUSE_WHEELDOWN};
    for(unsigned i=0;i<5;i++)if(id==ids[i])return mouse_buttons[i];
    return 0;
}
static int parse_fd(const char *text) {
    if(!text || !*text || strspn(text,"0123456789")!=strlen(text))return -1;
    char *end;errno=0;unsigned long value=strtoul(text,&end,10);
    return errno || *end || value>1048575?-1:(int)value;
}
static uint64_t parse_generation(const char *text) {
    if(!text || !*text || strspn(text,"0123456789")!=strlen(text))return 0;
    char *end;errno=0;unsigned long long value=strtoull(text,&end,10);
    return errno || *end?0:(uint64_t)value;
}
static void publish_state(void){
    state_core(h);
    if(deathmatch_mode && h->map[0])snprintf(last_dm_map,sizeof(last_dm_map),"%s",h->map);
    if(h->human_quits!=quit_seen){quit_seen=h->human_quits;bot=1;audible=0;audio_gate(0);bot_core(1);request_bot_refresh();state_core(h);}
    h->bot=bot;h->owner=bot?0:2;h->paused=paused;h->audible=audible&&!paused&&!bot_refresh_pending;
    if(!h->audible)h->audio_peak=0;
    h->pixel_aspect_num=h->pixel_aspect_den=1;
    h->render_fps=render_rate;
    h->view_width=h->width;h->view_height=h->height;
    /* Rendering and synchronous native saves happen entirely in private
     * memory. The shared sequence is odd only for this bounded publication. */
    __atomic_add_fetch(&published->seq,1,__ATOMIC_SEQ_CST);
    /* Keep the odd marker visible before any shared header/pixel stores on
     * weakly ordered CPUs as well as the tested x86-64 host. */
    __atomic_thread_fence(__ATOMIC_RELEASE);
    memcpy(published,h,offsetof(DDHeader,seq));
    memcpy((char*)published+offsetof(DDHeader,width),(char*)h+offsetof(DDHeader,width),sizeof(*h)-offsetof(DDHeader,width));
    if(pixels_dirty)memcpy((char*)published+DD_HEADER_SIZE,frame_pixels,h->stride*h->height);
    __atomic_add_fetch(&published->seq,1,__ATOMIC_SEQ_CST);
    pixels_dirty=0;
}
static int service_bot_refresh(void) {
    /* Called after release/control dispatch. Busy readiness never renders or
     * advances a tic; successful completion publishes before normal play. */
    if(!bot_refresh_pending)return 0;
    if(!bot){reset_bot_refresh();return 1;}
    double current=now();
    if(!bot_refresh_warned && current-bot_refresh_started_at>=5){
        fprintf(stderr,"Bot display is still waiting for a clean frame\n");
        bot_refresh_warned=1;
    }
    if(now()<bot_refresh_retry_at)return 1;
    if(!world_ok_core()){bot_refresh_retry_at=now()+.1;return 1;}
    int ready=gpu_ready_core();
    if(!ready)return 1;
    if(ready<0){wait_failure=WAIT_TRANSPORT;return 1;}
    if(refresh_bot_frame()){
        audio_gate(audible && !paused);
        publish_state();
    }else if(wait_failure!=WAIT_STARVED){
        current=now();
        bot_refresh_retry_at=current+bot_refresh_retry_delay;
        bot_refresh_retry_delay=fmin(2,bot_refresh_retry_delay*2);
        if(!bot_refresh_logged || current-bot_refresh_log_at>=5){
            fprintf(stderr,"Deferred bot display unavailable; retrying\n");
            bot_refresh_log_at=current;bot_refresh_logged=1;
        }
    }
    return 1;
}
static int save_game(void) {
    char temp[4096],final[4096];struct stat st;int fd;
    if(deathmatch_mode || load_pending || h->health<=0 || !h->map[0] || h->gamestate)return 0;
    snprintf(temp,sizeof(temp),"%s/%s/lq-pending.sav",save_dir,game_dir);
    snprintf(final,sizeof(final),"%s/%s/checkpoint.sav",save_dir,game_dir);
    if(lstat(temp,&st)==0){if(!S_ISREG(st.st_mode)||st.st_uid!=getuid())return 0;unlink(temp);}
    /* A created or sizeable prefix is not a completed save. The core receipt
     * includes every write, final flush and close, before we publish it. */
    if(!command_core("save lq-pending") || !save_ok_core())return 0;
    fd=open(temp,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if(fd<0)return 0;
    int ok=fstat(fd,&st)==0 && S_ISREG(st.st_mode) && st.st_size>100 && st.st_size<16*1024*1024 && fsync(fd)==0;
    close(fd);
    if(!ok || rename(temp,final))return 0;
    char directory[4096];snprintf(directory,sizeof(directory),"%s/%s",save_dir,game_dir);
    fd=open(directory,O_RDONLY|O_DIRECTORY|O_NOFOLLOW);
    if(fd<0)return 0;
    ok=fsync(fd)==0;close(fd);return ok;
}
/* Both HUD controls are required by the pinned core ABI. */
static int load_human_viewsize_exports(void *core) {
    *(void**)&human_viewsize_core=dlsym(core,"retro_lq_human_viewsize");
    *(void**)&set_human_viewsize_core=dlsym(core,"retro_lq_set_human_viewsize");
    return human_viewsize_core && set_human_viewsize_core;
}
/* Strict scalar protocol: no console text, nonfinite values or trailing args. */
static int human_viewsize_argument(const char *line,float *value) {
    const char *start=line+15;char *end;errno=0;
    float parsed=strtof(start,&end);
    if(start==end || *end || errno==ERANGE || !isfinite(parsed) || parsed<30 || parsed>130)return 0;
    *value=parsed;return 1;
}
static void handle(const char *line,char *reply,size_t size) {
    int value,key,down,dx,dy;char extra;
    snprintf(reply,size,"ERR command\n");
    char maps[961];int minutes,frags,bots;
    if(sscanf(line,"dm-config %960s %d %d %d %c",maps,&minutes,&frags,&bots,&extra)==4) {
        if(dm_config_core(maps,minutes,frags,bots)){snprintf(reply,size,"OK next_round\n");}return;
    }
    if(sscanf(line,"hide-chatter %d %c",&value,&extra)==1 && (value==0 || value==1)) {
        hide_chatter_core(value);snprintf(reply,size,"OK\n");return;
    }
    if(!strcmp(line,"match-state")){
        char state[256];
        if(match_state_core(state,sizeof(state)))snprintf(reply,size,"OK %s\n",state);
        return;
    }
    if(deathmatch_mode && (!strcmp(line,"load") || !strcmp(line,"load-preserve"))){
        snprintf(reply,size,"ERR load unavailable in deathmatch\n");return;
    }
    if(!strcmp(line,"human-viewsize")){
        float current=human_viewsize_core();
        if(isfinite(current) && current>=30 && current<=130)snprintf(reply,size,"OK %.9g\n",current);
    }else if(!strncmp(line,"human-viewsize ",15)){
        float requested;
        if(human_viewsize_argument(line,&requested)){
            set_human_viewsize_core(requested);
            float current=human_viewsize_core();
            if(isfinite(current) && current>=30 && current<=130)snprintf(reply,size,"OK %.9g\n",current);
        }
    }else if(sscanf(line,"pause %d %c",&value,&extra)==1 && (value==0||value==1)){
        if(load_pending && !value)snprintf(reply,size,"ERR load retryable\n");
        else {paused=value;audio_gate(audible && !paused && !bot_refresh_pending);snprintf(reply,size,"OK\n");}
    }else if(sscanf(line,"bot %d %c",&value,&extra)==1 && (value==0||value==1)){
        int previous_bot=bot;
        memset(mouse_buttons,0,sizeof(mouse_buttons));
        audible=0;audio_gate(0);
        if(!value)pin_view_core(published->followed_entity);
        if(!bot_core(value)){bot=previous_bot;snprintf(reply,size,"ERR takeover refused\n");publish_state();return;}
        bot=value;
        /* Policy updates repeat bot 1 while pausing or hiding the viewer.
         * Reuse an already clean bot frame: acquiring another compositor
         * lease here can wait on the viewer's pending policy query itself.
         * Human handback still requires a new, HUD-free zero-tic frame. */
        int clean_bot=previous_bot && bot && clean_bot_frame && world_ok_core() && display_ok_core();
        if(!bot){clean_bot_frame=0;reset_bot_refresh();}
        snprintf(reply,size,!bot || clean_bot || request_bot_refresh()?"OK\n":"ERR bot display unavailable\n");
    }else if(sscanf(line,"audio %d %c",&value,&extra)==1 && (value==0||value==1)){
        audible=value;audio_gate(audible && !paused && !bot_refresh_pending);snprintf(reply,size,"OK\n");
    }else if(sscanf(line,"key %d %d %c",&key,&down,&extra)==2 && ((!bot && !paused)||down==0) && key>0 && key<=204 && (down==0||down==1)){
        if(key>=200)mouse_buttons[key-200]=(unsigned char)down;
        key_core(key,down);snprintf(reply,size,"OK\n");
    }else if(sscanf(line,"mouse %d %d %c",&dx,&dy,&extra)==2 && !bot && !paused && dx>=-LQ_MOUSE_DELTA_MAX && dx<=LQ_MOUSE_DELTA_MAX && dy>=-LQ_MOUSE_DELTA_MAX && dy<=LQ_MOUSE_DELTA_MAX){
        mouse_core(dx,dy);snprintf(reply,size,"OK\n");
    }else if(sscanf(line,"skill %d %c",&value,&extra)==1 && value>=0 && value<=3){
        char command[32];snprintf(command,sizeof(command),"skill %d",value);
        if(command_core(command)){requested_skill=value;snprintf(reply,size,"OK next_level\n");}
    }else if(!strcmp(line,"save"))snprintf(reply,size,save_game()?"OK\n":"ERR save unavailable\n");
    else if(!strcmp(line,"load") || (!strcmp(line,"load-preserve") && paused && bot && !exit_save)){
        /* A staged catalogue session must leave both the selected package's
         * save and a verified rollback save untouched if loading fails. */
        int preserve=!strcmp(line,"load-preserve");
        char checkpoint[4096];struct stat st;snprintf(checkpoint,sizeof(checkpoint),"%s/%s/checkpoint.sav",save_dir,game_dir);
        if(lstat(checkpoint,&st)==0 && S_ISREG(st.st_mode) && st.st_uid==getuid() && st.st_size<16*1024*1024){
            audio_gate(0);
            double duration=load_pending?.1:1;
            if(!load_pending)load_progress=0;
            loading_core(1);int ok=load_pending || (command_core("load checkpoint") && load_ok_core());
            /* Native load pauses its server during local-client signon. Run
             * only until it completes; backpressure does not invalidate a save. */
            int ready=0,oldbot=bot;
            if(ok){load_pending=1;bot_core(0);ready=wait_for_world(duration);bot_core(oldbot);}
            char skill_command[32];snprintf(skill_command,sizeof(skill_command),"skill %d",requested_skill);command_core(skill_command);
            if(ok && ready && (!bot || refresh_bot_frame())){
                load_pending=0;load_progress=0;loading_core(0);snprintf(reply,size,"OK\n");
            }else if(ok && wait_failure==WAIT_STARVED){
                paused=1;snprintf(reply,size,"ERR load retryable\n");
            }else if(ok && wait_failure==WAIT_TRANSPORT){
                paused=1;snprintf(reply,size,"ERR load transport unavailable\n");
            }else if(ok && wait_failure==WAIT_INTERRUPTED){
                paused=1;snprintf(reply,size,"ERR load interrupted\n");
            }
            else {
                load_pending=0;load_progress=0;loading_core(0);
                if(!preserve){
                    char invalid[4096];snprintf(invalid,sizeof(invalid),"%s/%s/checkpoint.invalid-XXXXXX",save_dir,game_dir);
                    int bad=mkstemp(invalid);if(bad>=0){close(bad);if(rename(checkpoint,invalid))unlink(invalid);}
                }
                restart_world();snprintf(reply,size,preserve?"ERR load failed; checkpoint retained\n":"ERR load failed; started fresh\n");
            }audio_gate(audible && !paused && !bot_refresh_pending);}
    }else if(!strcmp(line,"replan")){
        if(bot)replan_core();
        snprintf(reply,size,"OK\n");
    }
    else if(!strcmp(line,"restart-map") && bot){
        state_core(h);
        char current[sizeof(h->map)];memcpy(current,h->map,sizeof(current));current[sizeof(current)-1]=0;
        /* The shared ABI clips names at 15 bytes. Refuse an ambiguous name
         * before touching the world rather than restarting its prefix. */
        if(strlen(current)==sizeof(current)-1)snprintf(reply,size,"ERR current map name is too long\n");
        else snprintf(reply,size,restart_map(current)?"OK\n":"ERR map restart unavailable\n");
    }
    else if(!strcmp(line,"skip-map") && bot && paused && !load_pending && !bot_refresh_pending && world_ok_core()){
        state_core(h);
        char current[sizeof(h->map)],destination[49]={0};
        memcpy(current,h->map,sizeof(current));current[sizeof(current)-1]=0;
        if(h->health<=0 || h->gamestate || !current[0] || strlen(current)==sizeof(current)-1){
            snprintf(reply,size,"ERR map advance unavailable\n");
        }else{
            int skip=nextmap_core(destination,sizeof(destination));
            destination[sizeof(destination)-1]=0;
            skip=skip && destination[0] && strlen(destination)<sizeof(h->map)-1 &&
                 strspn(destination,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")==strlen(destination) &&
                 strcmp(destination,current);
            const char *target=skip?destination:current;
            if(restart_map(target))snprintf(reply,size,"OK %s=%s\n",skip?"skipped":"restarted",target);
            else snprintf(reply,size,"ERR map advance unavailable\n");
        }
    }
    else if(!strcmp(line,"commit-session") && !load_pending && paused && bot && world_ok_core() && display_ok_core()){
        exit_save=!deathmatch_mode;snprintf(reply,size,"OK\n");
    }
    else if(!strcmp(line,"discard-session") && paused && bot){exit_save=0;snprintf(reply,size,"OK\n");}
    else if(!strcmp(line,"present") && paused){
        if(refresh_bot_frame())snprintf(reply,size,"OK\n");
        else snprintf(reply,size,"ERR presentation unavailable\n");
    }
    else if(!strcmp(line,"capture") && world_ok_core() && display_ok_core()){
        /* The viewer services real releases before requesting this zero-tic
         * render. Capture does not swap or acquire a producer buffer lease. */
        if(!frame_pixels)frame_pixels=malloc(DD_FRAME_BYTES);
        double began=now();
        pixels_dirty=0;capture_active=1;
        int armed=0;
        /* A live peer may have produced another candidate just before this
         * request. The viewer continues ACKing uncopied raw frames while we
         * wait for readiness; retries never render or perform another copy. */
        while(frame_pixels && now()-began<.45){
            if(capture_core(1)){armed=1;break;}
            if(gpu_poll_core()<0)break;
            struct pollfd release={gpu_socket,POLLIN,0};poll(&release,1,5);
        }
        int ok=armed && command_core("lq_refresh") && pixels_dirty && display_ok_core();
        capture_core(0);capture_active=0;
        if(ok && h->capture_frame!=UINT64_MAX){
            h->capture_frame++;h->capture_width=h->width;h->capture_height=h->height;
            h->capture_elapsed_us=(uint32_t)fmin(UINT32_MAX,fmax(0,(now()-began)*1e6));
            snprintf(reply,size,"OK %llu\n",(unsigned long long)h->capture_frame);
        }else {pixels_dirty=0;snprintf(reply,size,"ERR capture unavailable\n");}
    }
    else if(!strcmp(line,"quit")){stopping=1;snprintf(reply,size,"OK\n");}
    else if(!strcmp(line,"audio-status")){
        AudioStats s=audio_statistics();
        snprintf(reply,size,"OK ready=%d enabled=%d rate=%u buffer=%zu offered=%llu written=%llu discarded=%llu errors=%llu drops=%llu xruns=%llu recoveries=%llu eagain=%llu short_writes=%llu trimmed=%llu queued=%zu delay=%zu buffered=%zu\n",
            s.backend_ready,s.enabled,s.rate,s.buffer_frames,(unsigned long long)s.frames_offered,
            (unsigned long long)s.frames_written,(unsigned long long)s.frames_discarded,
            (unsigned long long)(s.open_errors+s.io_errors),(unsigned long long)s.queue_drops,
            (unsigned long long)s.xruns,(unsigned long long)s.recoveries,(unsigned long long)s.again,
            (unsigned long long)s.short_writes,(unsigned long long)s.trimmed_frames,s.queued_frames,s.delay_frames,s.buffered_frames);
    }else if(!strcmp(line,"status"))snprintf(reply,size,"OK tic=%llu paused=%d bot=%d pid=%u\n",(unsigned long long)h->tic,paused,bot,h->pid);
    publish_state();
}
struct client {int fd;size_t used;double deadline;char line[1024];};
int main(int argc,char **argv) {
    struct rlimit core_limit={1,1};if(setrlimit(RLIMIT_CORE,&core_limit))return 1;
    pid_t parent=getppid();
    if(prctl(PR_SET_PDEATHSIG,SIGKILL) || getppid()!=parent)return 1;
    const char *core_path=NULL,*frame_path=NULL,*socket_path=NULL,*pak=NULL,*map="lq_e0m1";
    AudioMode audio_mode=AUDIO_DISCARD;
    struct client clients[16];struct sockaddr_un address;int listener,fd,i,skill=1;
    for(i=1;i+1<argc;i+=2){
        if(!strcmp(argv[i],"--core"))core_path=argv[i+1];
        else if(!strcmp(argv[i],"--frame"))frame_path=argv[i+1];
        else if(!strcmp(argv[i],"--socket"))socket_path=argv[i+1];
        else if(!strcmp(argv[i],"--pak"))pak=argv[i+1];
        else if(!strcmp(argv[i],"--state"))save_dir=argv[i+1];
        else if(!strcmp(argv[i],"--map"))map=argv[i+1];
        else if(!strcmp(argv[i],"--mode") && (!strcmp(argv[i+1],"single") || !strcmp(argv[i+1],"deathmatch")))deathmatch_mode=!strcmp(argv[i+1],"deathmatch");
        else if(!strcmp(argv[i],"--dm-maps") && strlen(argv[i+1])<961)dm_maps=argv[i+1];
        else if(!strcmp(argv[i],"--dm-timelimit"))dm_time=argv[i+1];
        else if(!strcmp(argv[i],"--dm-fraglimit"))dm_frags=argv[i+1];
        else if(!strcmp(argv[i],"--dm-bots"))dm_bots=argv[i+1];
        else if(!strcmp(argv[i],"--edition") && (!strcmp(argv[i+1],"original") || !strcmp(argv[i+1],"rerelease")))edition=argv[i+1];
        else if(!strcmp(argv[i],"--defer-exit-save") && !strcmp(argv[i+1],"1"))exit_save=0;
        else if(!strcmp(argv[i],"--resolution"))resolution=argv[i+1];
        else if(!strcmp(argv[i],"--gpu-fd")){gpu_socket=parse_fd(argv[i+1]);if(gpu_socket<3)return 2;}
        else if(!strcmp(argv[i],"--render-fd")){gpu_render=parse_fd(argv[i+1]);if(gpu_render<3)return 2;}
        else if(!strcmp(argv[i],"--gpu-generation")){gpu_generation=parse_generation(argv[i+1]);if(!gpu_generation)return 2;}
        else if(!strcmp(argv[i],"--audio-mode") && !strcmp(argv[i+1],"discard"))audio_mode=AUDIO_DISCARD;
        else if(!strcmp(argv[i],"--audio-mode") && !strcmp(argv[i+1],"alsa"))audio_mode=AUDIO_ALSA;
        else if(!strcmp(argv[i],"--skill") && strlen(argv[i+1])==1 && argv[i+1][0]>='0' && argv[i+1][0]<='3')skill=argv[i+1][0]-'0';
        else {fprintf(stderr,"Unknown argument\n");return 2;}
    }
    if(!core_path||!frame_path||!socket_path||!pak||!save_dir||argc%2!=1)return 2;
    if(deathmatch_mode){exit_save=0;if(strlen(map)>14)return 2;}
    if(gpu_socket<3 || gpu_render<3 || gpu_socket==gpu_render || !gpu_generation)return 2;
    if(strlen(socket_path)>=sizeof(address.sun_path)||!map[0]||strspn(map,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")!=strlen(map)||strlen(map)>48)return 2;
    int rw,rh;char trailing;struct stat state_info;
    if(sscanf(resolution,"%dx%d%c",&rw,&rh,&trailing)!=2 || rw<320 || rh<200 || rw>DD_MAX_WIDTH || rh>DD_MAX_HEIGHT || rw%8)return 2;
    if(strlen(save_dir)>512 || lstat(save_dir,&state_info) || !S_ISDIR(state_info.st_mode) || state_info.st_uid!=getuid() || (state_info.st_mode&0777)!=0700)return 2;
    const char *leaf=strrchr(pak,'/'),*parent_dir=leaf;
    if(!leaf)return 2;
    while(parent_dir>pak && parent_dir[-1]!='/')--parent_dir;
    size_t length=leaf-parent_dir;
    if(!length || length>=sizeof(game_dir))return 2;
    memcpy(game_dir,parent_dir,length);game_dir[length]=0;
    if(strcmp(game_dir,"id1") && strcmp(game_dir,"hipnotic") && strcmp(game_dir,"rogue"))return 2;
    start_map=map;requested_skill=skill;
    char state_game[4096];snprintf(state_game,sizeof(state_game),"%s/%s",save_dir,game_dir);
    if(mkdir(state_game,0700) && errno!=EEXIST)return 2;
    if(lstat(state_game,&state_info) || !S_ISDIR(state_info.st_mode) || state_info.st_uid!=getuid() || (state_info.st_mode&0777)!=0700)return 2;
    umask(0077);signal(SIGTERM,signal_stop);signal(SIGINT,signal_stop);signal(SIGPIPE,SIG_IGN);
    fd=open(frame_path,O_CREAT|O_EXCL|O_RDWR|O_NOFOLLOW,0600);if(fd<0)die("frame");
    if(ftruncate(fd,DD_FILE_SIZE))die("frame size");
    int allocation_error=posix_fallocate(fd,0,DD_FILE_SIZE);
    if(allocation_error){errno=allocation_error;die("frame allocation");}
    published=mmap(NULL,DD_FILE_SIZE,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);close(fd);if(published==MAP_FAILED)die("frame mmap");
    memset(published,0,DD_HEADER_SIZE);h->magic=DD_MAGIC;h->version=DD_VERSION;h->pid=getpid();
    void *core=dlopen(core_path,RTLD_NOW|RTLD_LOCAL);if(!core){fprintf(stderr,"%s\n",dlerror());return 1;}
#define LOAD(target,name) do {*(void**)&target=dlsym(core,name);if(!target){fprintf(stderr,"Missing %s\n",name);return 1;}}while(0)
    void (*set_env)(retro_environment_t),(*set_video)(retro_video_refresh_t),(*set_audio)(retro_audio_sample_t);
    void (*set_batch)(retro_audio_sample_batch_t),(*set_poll)(retro_input_poll_t),(*set_input)(retro_input_state_t),(*init)(void);
    bool (*load)(const struct retro_game_info*);void(*device)(unsigned,unsigned);
    LOAD(set_env,"retro_set_environment");LOAD(set_video,"retro_set_video_refresh");LOAD(set_audio,"retro_set_audio_sample");
    LOAD(set_batch,"retro_set_audio_sample_batch");LOAD(set_poll,"retro_set_input_poll");LOAD(set_input,"retro_set_input_state");
    LOAD(init,"retro_init");LOAD(load,"retro_load_game");LOAD(device,"retro_set_controller_port_device");
    LOAD(run_core,"retro_run");LOAD(unload_core,"retro_unload_game");LOAD(deinit_core,"retro_deinit");
    LOAD(state_core,"retro_lq_state");LOAD(bot_core,"retro_lq_bot");LOAD(key_core,"retro_lq_key");
    LOAD(replan_core,"retro_lq_replan");
    LOAD(nextmap_core,"retro_lq_nextmap");
    LOAD(match_state_core,"retro_lq_match_state");
    LOAD(map_exists_core,"retro_lq_map_exists");
    LOAD(dm_rejected_core,"retro_lq_dm_rejected");
    LOAD(dm_config_core,"retro_lq_dm_config");LOAD(hide_chatter_core,"retro_lq_set_hide_chatter");
    LOAD(pin_view_core,"retro_lq_pin_view");
    LOAD(mouse_core,"retro_lq_mouse");LOAD(command_core,"retro_lq_command");
    LOAD(loading_core,"retro_lq_loading");
    if(!load_human_viewsize_exports(core)){fprintf(stderr,"Missing human viewsize exports\n");return 1;}
    LOAD(world_ok_core,"retro_lq_world_ok");
    LOAD(load_ok_core,"retro_lq_load_succeeded");
    LOAD(save_ok_core,"retro_lq_save_succeeded");
    LOAD(display_ok_core,"retro_lq_display_ok");
    int (*gpu_setup)(int,int,uint64_t);LOAD(gpu_setup,"retro_lq_gpu_setup");
    LOAD(gpu_poll_core,"retro_lq_gpu_poll");
    LOAD(gpu_ready_core,"retro_lq_gpu_ready");
    LOAD(capture_core,"retro_lq_capture");
    h->capture_supported=1;
    if(!gpu_setup(gpu_socket,gpu_render,gpu_generation)){fprintf(stderr,"Invalid GPU descriptors\n");return 2;}
    void (*get_av)(struct retro_system_av_info*);LOAD(get_av,"retro_get_system_av_info");
    set_env(environment);set_video(video);set_audio(audio_one);set_batch(audio_batch);set_poll(input_poll);set_input(input_state);
    init();struct retro_game_info game={.path=pak};
    if(!load(&game)){fprintf(stderr,"Core rejected game\n");return deathmatch_mode && dm_rejected_core()?4:1;}
    struct retro_system_av_info av;memset(&av,0,sizeof(av));get_av(&av);
    if(!isfinite(av.timing.sample_rate) || av.timing.sample_rate<8000 || av.timing.sample_rate>192000)return 1;
    if(!isfinite(av.timing.fps) || av.timing.fps<10 || av.timing.fps>72)return 1;
    frame_period=1.0/av.timing.fps; render_rate=(unsigned)lround(av.timing.fps);
    if(audio_open((unsigned)av.timing.sample_rate,audio_mode)<0)fprintf(stderr,"Audio backend unavailable; continuing silently\n");
    device(0,RETRO_DEVICE_KEYBOARD);
    command_core("bind w +forward");command_core("bind s +back");command_core("bind a +moveleft");command_core("bind d +moveright");
    command_core("bind SPACE +jump");command_core("bind CTRL +attack");command_core("bind MOUSE1 +attack");
    command_core("bind SHIFT +speed");
    command_core("bind UPARROW +forward");command_core("bind DOWNARROW +back");
    command_core("bind LEFTARROW +left");command_core("bind RIGHTARROW +right");
    command_core("viewsize 120"); /* HUD off: all screenshots are free world data only. */
    command_core("gamma 0.7");command_core("crosshair 0");
    char command[80];snprintf(command,sizeof(command),"skill %d",skill);command_core(command);
    const float ratios[]={4.0f/3,16.0f/9,8.0f/5,64.0f/27,32.0f/9};
    int aspect_index=0;float diff=100;
    for(i=0;i<5;i++){float value=fabs((float)rw/rh-ratios[i]);if(value<diff){diff=value;aspect_index=i;}}
    snprintf(command,sizeof(command),"r_aspect %d",aspect_index);command_core(command);
    if(deathmatch_mode && !map_exists_core(map)){fprintf(stderr,"DM startup map missing\n");return 3;}
    snprintf(command,sizeof(command),"map %s",map);command_core(command);
    loading_core(1);
    if(!wait_for_world(1)){fprintf(stderr,"Startup sign-on did not finish\n");return startup_world_exit();}
    loading_core(0);
    if(!world_ok_core()){fprintf(stderr,"Startup map has no playable world\n");return startup_world_exit();}
    bot_core(1);
    if(!refresh_bot_frame()){fprintf(stderr,"Startup world did not render cleanly\n");return 1;}
    publish_state();
    listener=socket(AF_UNIX,SOCK_STREAM|SOCK_NONBLOCK|SOCK_CLOEXEC,0);if(listener<0)die("socket");
    memset(&address,0,sizeof(address));address.sun_family=AF_UNIX;strcpy(address.sun_path,socket_path);
    if(bind(listener,(struct sockaddr*)&address,sizeof(address))||listen(listener,16))die("bind/listen");
    for(i=0;i<16;i++)clients[i].fd=-1;
    double deadline=now(),heartbeat=now(),world_missing=0;
    while(!stopping){
        if(gpu_poll_core()<0){fprintf(stderr,"GPU viewer disconnected; stopping engine\n");break;}
        struct pollfd pollfds[18]={{listener,POLLIN,0}};
        for(i=0;i<16;i++)pollfds[i+1]=(struct pollfd){clients[i].fd,POLLIN,0};
        pollfds[17]=(struct pollfd){gpu_socket,POLLIN,0};
        double wait_seconds=bot_refresh_pending?.005:paused?.02:fmax(0,deadline-now());
        struct timespec timeout={.tv_sec=(time_t)wait_seconds,
            .tv_nsec=(long)((wait_seconds-floor(wait_seconds))*1e9)};
        ppoll(pollfds,18,&timeout,NULL);
        if(pollfds[0].revents&POLLIN){
            int peer=accept(listener,NULL,NULL);if(peer>=0){fcntl(peer,F_SETFL,O_NONBLOCK);fcntl(peer,F_SETFD,FD_CLOEXEC);
                for(i=0;i<16;i++)if(clients[i].fd<0){clients[i]=(struct client){.fd=peer,.deadline=now()+1};break;}
                if(i==16)close(peer);
            }
        }
        for(i=0;i<16;i++)if(clients[i].fd>=0){struct client *c=&clients[i];int finished=0;
            if(pollfds[i+1].revents){ssize_t n=read(c->fd,c->line+c->used,sizeof(c->line)-1-c->used);
                if(n<=0){if(n==0 || (errno!=EAGAIN&&errno!=EINTR))finished=1;}
                else {c->used+=n;c->line[c->used]=0;char *nl=memchr(c->line,'\n',c->used);
                    if(nl){*nl=0;char reply[2048];handle(c->line,reply,sizeof(reply));send(c->fd,reply,strlen(reply),MSG_NOSIGNAL);finished=1;}
                    else if(c->used==sizeof(c->line)-1)finished=1;}
            }
            if(now()>c->deadline)finished=1;
            if(finished){close(c->fd);c->fd=-1;}
        }
        double t=now();
        int refreshing=service_bot_refresh();
        if(!paused && !refreshing && t>=deadline){run_core();
            publish_state();deadline=next_frame_deadline(deadline);}
        else if(paused || refreshing){deadline=now()+frame_period; if(t-heartbeat>.2){publish_state();heartbeat=t;}}
        if(bot && !paused && !world_ok_core()) {
            if(!world_missing)world_missing=t;
            if(t-world_missing>2){restart_world();publish_state();world_missing=0;}
        }else world_missing=0;
    }
    paused=1;audio_gate(0);bot_core(1);if(exit_save)save_game();publish_state();
    for(i=0;i<16;i++)if(clients[i].fd>=0)close(clients[i].fd);
    close(listener);unlink(socket_path);unload_core();deinit_core();audio_close();dlclose(core);munmap(published,DD_FILE_SIZE);free(frame_pixels);
    return 0;
}
