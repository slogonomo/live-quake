// SPDX-License-Identifier: GPL-2.0-or-later
/* Ironwail 0.8.2 single-session libretro ABI shim. The diagnostic EGL
 * callback copies pixels; production DMA-BUF transport is separate. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include "quakedef.h"
#include <libretro.h>
#include "lq_egl_offscreen.h"
#include "lq_libretro.h"
#include "lq_session.h"
#include "dm_rotation.h"
#ifdef LQ_GPU
#define LQ_RESOLUTION_MAX_WIDTH 3840
#define LQ_RESOLUTION_MAX_HEIGHT 2160
#else
#define LQ_RESOLUTION_MAX_WIDTH 1920
#define LQ_RESOLUTION_MAX_HEIGHT 1080
#endif
#include <SDL.h>
#include <dlfcn.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdint.h>

static retro_environment_t environment_callback;
static retro_video_refresh_t video_callback;
static retro_audio_sample_t audio_callback;
static retro_audio_sample_batch_t audio_batch_callback;
static retro_input_poll_t input_poll_callback;
static retro_input_state_t input_callback;
static retro_log_printf_t log_callback;
static quakeparms_t parms;
static char core_path[MAX_OSPATH], basedir[MAX_OSPATH], savedir[MAX_OSPATH];
static char width_arg[16],height_arg[16];
static char *arguments[32];
static uint32_t *video_pixels;
static size_t video_capacity;
static uint64_t video_frames;
static unsigned width=960,height=540;
static int attempted,loaded,sdl_started;
static int session_mode;
static int dm_rejected;
int LQ_SessionMode(void) { return session_mode; }
int retro_lq_dm_rejected(void) { return dm_rejected; }
int retro_lq_map_exists(const char *name)
{
    char path[64];
    if (!name || !*name || strlen(name)>48 ||
        strspn(name,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")!=strlen(name)) return 0;
    snprintf(path,sizeof(path),"maps/%s.bsp",name);
    return COM_FileExists(path,NULL);
}
static LQ_DMConfig dm_config, dm_pending;
static int dm_has_pending, dm_edition;
static char dm_next[15];
static void dm_global_number(const char *name,float value) {
    for(int i=1;i<sv.qcvm.progs->numglobaldefs;i++)
        if(!strcmp(PR_GetString(sv.qcvm.globaldefs[i].s_name),name)) {
            G_FLOAT(sv.qcvm.globaldefs[i].ofs)=value;return;
        }
}
void LQ_DmSpawn(void) {
    if(session_mode!=LQ_DEATHMATCH || !sv.qcvm.progs)return;
    if(dm_has_pending){dm_config=dm_pending;dm_has_pending=0;}
    Cvar_SetValue("timelimit",dm_config.minutes);
    Cvar_SetValue("fraglimit",dm_config.frags);
    Cvar_Set("noexit","1");Cvar_Set("samelevel","0");
    const char *next=LQ_DMPick(&dm_config,sv.name,(unsigned)rand());
    q_strlcpy(dm_next,next?next:sv.name,sizeof(dm_next));
    dm_global_number("lq_dm_bots",dm_config.bots);
    dm_global_number("lq_rerelease",dm_edition);
    for(int i=1;i<sv.qcvm.progs->numglobaldefs;i++)
        if(!strcmp(PR_GetString(sv.qcvm.globaldefs[i].s_name),"lq_dm_next"))
            G_INT(sv.qcvm.globaldefs[i].ofs)=PR_SetEngineString(dm_next);
}
int retro_lq_dm_config(const char *maps,int minutes,int frags,int bots) {
    LQ_DMConfig next;
    if(!LQ_DMParse(&next,maps,minutes,frags,bots))return 0;
    for(int i=0;i<next.count;i++)if(!retro_lq_map_exists(next.maps[i]))return 0;
    dm_pending=next;dm_has_pending=1;return 1;
}
void LQ_NewDeathmatchGame(void) {
    if(session_mode!=LQ_DEATHMATCH)return;
    const char *next=LQ_DMPick(dm_has_pending?&dm_pending:&dm_config,sv.name,(unsigned)rand());
    if(next)Cbuf_AddText(va("maxplayers 8\ncoop 0\ndeathmatch 1\nmap %s\n",next));
}
extern cvar_t host_maxfps;
extern int LQ_ExecuteCommand(const char *command);
extern void LQ_SoundAdvance(void),LQ_SoundEmit(void);

static void diagnostic(const char *message)
{
    if(log_callback) log_callback(RETRO_LOG_ERROR,"%s\n",message);
    else fprintf(stderr,"%s\n",message);
}

static int copy_absolute(char *target,size_t capacity,const char *path)
{
    if(!path || path[0]!='/' || strlen(path)>=capacity || strstr(path,"//")) return 0;
    /* No ambiguous or traversing components; per-file PAK symlinks remain
     * permitted because the controller's neutral farm owns those links. */
    for(const char *part=path+1;*part;) {
        const char *end=strchr(part,'/');size_t length=end?(size_t)(end-part):strlen(part);
        if(!length || (length==1 && part[0]=='.') || (length==2 && part[0]=='.' && part[1]=='.'))return 0;
        if(!end)break;
        part=end+1;
    }
    if(path[strlen(path)-1]=='/')return 0;
    memcpy(target,path,strlen(path)+1);return 1;
}

static void diagnostic_frame(int frame_width,int frame_height,const unsigned char *rgba,int bottom_up)
{
#ifdef LQ_GPU
    if(video_callback && !rgba && frame_width>=320 && frame_height>=200 &&
       frame_width<=3840 && frame_height<=2160) {
        video_frames++;
        video_callback(RETRO_HW_FRAME_BUFFER_VALID,(unsigned)frame_width,(unsigned)frame_height,0);
        return;
    }
#endif
    if(!video_callback || !rgba || frame_width<320 || frame_height<200 ||
       frame_width>LQ_RESOLUTION_MAX_WIDTH || frame_height>LQ_RESOLUTION_MAX_HEIGHT)return;
    const size_t count=(size_t)frame_width*frame_height;
    if(count>video_capacity) {
        uint32_t *replacement=realloc(video_pixels,count*sizeof(*video_pixels));
        if(!replacement)return;
        video_pixels=replacement;video_capacity=count;
    }
    for(int y=0;y<frame_height;y++) {
        const int source_y=bottom_up?frame_height-1-y:y;
        const unsigned char *source=rgba+(size_t)source_y*frame_width*4;
        uint32_t *row=video_pixels+(size_t)y*frame_width;
        for(int x=0;x<frame_width;x++)
            row[x]=((uint32_t)source[x*4]<<16)|((uint32_t)source[x*4+1]<<8)|source[x*4+2];
    }
    video_frames++;
    video_callback(video_pixels,(unsigned)frame_width,(unsigned)frame_height,(size_t)frame_width*4);
}

int16_t lq_retro_input(unsigned port,unsigned device,unsigned index,unsigned id)
{return loaded && input_callback?input_callback(port,device,index,id):0;}
uint64_t lq_retro_frame_count(void) {return video_frames;}
void lq_retro_audio_frames(const int16_t *samples,size_t frames)
{
    if(!loaded || !samples || !frames || frames>8192)return;
    if(audio_batch_callback)audio_batch_callback(samples,frames);
    else if(audio_callback)for(size_t i=0;i<frames;i++)audio_callback(samples[i*2],samples[i*2+1]);
}

void retro_set_environment(retro_environment_t callback){environment_callback=callback;}
void retro_set_video_refresh(retro_video_refresh_t callback){video_callback=callback;}
void retro_set_audio_sample(retro_audio_sample_t callback){audio_callback=callback;}
void retro_set_audio_sample_batch(retro_audio_sample_batch_t callback){audio_batch_callback=callback;}
void retro_set_input_poll(retro_input_poll_t callback){input_poll_callback=callback;}
void retro_set_input_state(retro_input_state_t callback){input_callback=callback;}
void retro_set_controller_port_device(unsigned port,unsigned device){(void)port;(void)device;}

unsigned retro_api_version(void){return RETRO_API_VERSION;}
void retro_init(void)
{
    struct retro_log_callback logger={0};
    if(environment_callback && environment_callback(RETRO_ENVIRONMENT_GET_LOG_INTERFACE,&logger))log_callback=logger.log;
}

void retro_get_system_info(struct retro_system_info *info)
{
    memset(info,0,sizeof(*info));
    info->library_name="Ironwail Live Quake";info->library_version="0.8.2";
    info->valid_extensions="pak";info->need_fullpath=true;info->block_extract=true;
}

void retro_get_system_av_info(struct retro_system_av_info *info)
{
    memset(info,0,sizeof(*info));
    info->geometry.base_width=width;info->geometry.base_height=height;
    info->geometry.max_width=LQ_RESOLUTION_MAX_WIDTH;info->geometry.max_height=LQ_RESOLUTION_MAX_HEIGHT;
    info->geometry.aspect_ratio=(float)width/height;
    info->timing.fps=72.0;info->timing.sample_rate=44100.0;
}

bool retro_load_game(const struct retro_game_info *game)
{
    const char *state=NULL;struct stat directory;
    char content[MAX_OSPATH];Dl_info self={0};
    enum retro_pixel_format format=RETRO_PIXEL_FORMAT_XRGB8888;
    if(attempted || !environment_callback || !video_callback || !game || !game->path ||
       !environment_callback(RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY,&state) ||
       !copy_absolute(savedir,sizeof(savedir),state) || lstat(savedir,&directory) ||
       !S_ISDIR(directory.st_mode) || directory.st_uid!=getuid() || (directory.st_mode&0777)!=0700 ||
       !copy_absolute(content,sizeof(content),game->path) ||
       !environment_callback(RETRO_ENVIRONMENT_SET_PIXEL_FORMAT,&format)) {
        diagnostic("Ironwail requires an explicit owned save directory, neutral PAK path and XRGB8888 callback");return false;
    }
    char *leaf=strrchr(content,'/');
    if(!leaf || strcmp(leaf+1,"pak0.pak")){diagnostic("Ironwail requires neutral pak0.pak");return false;}
    *leaf=0;char *game_directory=strrchr(content,'/');
    if(!game_directory || game_directory==content ||
       (strcmp(game_directory+1,"id1") && strcmp(game_directory+1,"hipnotic") && strcmp(game_directory+1,"rogue"))) {
        diagnostic("Ironwail neutral game directory is unsupported");return false;
    }
    const char *expansion=!strcmp(game_directory+1,"hipnotic")?"-hipnotic":
        !strcmp(game_directory+1,"rogue")?"-rogue":NULL;
    *game_directory=0;memcpy(basedir,content,strlen(content)+1);
    if(!dladdr((void*)retro_load_game,&self) || !copy_absolute(core_path,sizeof(core_path),self.dli_fname)) {
        diagnostic("Ironwail shared core path is unavailable");return false;
    }
    char botpack[MAX_OSPATH] = "";
    if (!expansion) {
        q_strlcpy(botpack,core_path,sizeof(botpack));
        char *last=strrchr(botpack,'/'); if(last)*last=0;
        last=strrchr(botpack,'/'); if(last)*last=0;
        if (q_strlcat(botpack,"/frikbot/pak0.pak",sizeof(botpack)) >= sizeof(botpack)) return false;
    }
    int frik_session = !expansion && access(botpack,R_OK)==0;
    struct retro_variable mode={"live_quake_mode",NULL};
    const char *mode_value = environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&mode) && mode.value ? mode.value : "single";
    if (!LQ_ParseMode(mode_value,&session_mode) || (session_mode==LQ_DEATHMATCH && !frik_session)) {
        dm_rejected = !strcmp(mode_value,"deathmatch");
        diagnostic("Deathmatch requires a readable FrikBot pack and non-expansion data");return false;
    }
    if(session_mode==LQ_DEATHMATCH) {
        struct retro_variable maps={"live_quake_dm_maps",NULL}, time={"live_quake_dm_timelimit",NULL},
            frags={"live_quake_dm_fraglimit",NULL}, bots={"live_quake_dm_bots",NULL}, edition={"live_quake_edition",NULL};
        environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&maps);
        environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&time);
        environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&frags);
        environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&bots);
        environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&edition);
        if(!LQ_DMParse(&dm_config,maps.value?maps.value:"dm1,dm2,dm3,dm4,dm5,dm6",
            time.value?atoi(time.value):6,frags.value?atoi(frags.value):25,bots.value?atoi(bots.value):4)) {
            dm_rejected=1;return false;
        }
        dm_edition=edition.value && !strcmp(edition.value,"rerelease");
    }
    struct retro_variable resolution={"tyrquake_resolution",NULL};
    if(environment_callback(RETRO_ENVIRONMENT_GET_VARIABLE,&resolution) && resolution.value) {
        unsigned rw,rh;char extra;
        if(sscanf(resolution.value,"%5ux%5u%c",&rw,&rh,&extra)!=2 || rw<320 || rw>LQ_RESOLUTION_MAX_WIDTH ||
           rh<200 || rh>LQ_RESOLUTION_MAX_HEIGHT || rw%8) {diagnostic("Ironwail resolution is unsupported");return false;}
        width=rw;height=rh;
    }
    snprintf(width_arg,sizeof(width_arg),"%u",width);snprintf(height_arg,sizeof(height_arg),"%u",height);
    int argc=0;
    arguments[argc++]=core_path;arguments[argc++]="-basedir";arguments[argc++]=basedir;
    arguments[argc++]="-lq-savedir";arguments[argc++]=savedir;
    arguments[argc++]="-nocdaudio";arguments[argc++]="-nojoy";
    arguments[argc++]="-nomouse";arguments[argc++]="-nosteamapi";
    argc += LQ_SessionArguments(arguments+argc,expansion!=NULL,frik_session);
    arguments[argc++]="-window";arguments[argc++]="-width";arguments[argc++]=width_arg;
    arguments[argc++]="-height";arguments[argc++]=height_arg;
    if(expansion)arguments[argc++]=(char*)expansion;
    arguments[argc++]=(char*)"+host_maxfps";arguments[argc++]=(char*)"72";
    arguments[argc++]=(char*)"+cl_startdemos";arguments[argc++]=(char*)"0";
    arguments[argc++]=(char*)"+sv_autosave";arguments[argc++]=(char*)"0";
    arguments[argc]=NULL;
    attempted=1; /* Engine globals and shutdown-once statics prohibit reload. */
    memset(&parms,0,sizeof(parms));parms.argc=argc;parms.argv=arguments;
    parms.basedir=basedir;parms.userdir=savedir;parms.memsize=384*1024*1024;
    host_parms=&parms;isDedicated=false;
    LQ_EGL_RequireIsolation();
    SDL_SetHint(SDL_HINT_NO_SIGNAL_HANDLERS,"1");
    if(SDL_Init(0)<0){diagnostic("Ironwail SDL thread initialization failed");return false;}
    sdl_started=1;
    COM_InitArgv(argc,arguments);Sys_Init();parms.numcpus=2;
    parms.membase=malloc((size_t)parms.memsize);
    if(!parms.membase){diagnostic("Ironwail core memory allocation failed");return false;}
    LQ_EGL_SetFrameCallback(diagnostic_frame);
    Host_Init();
    /* Upstream stuffcmds reparses a 256-byte flattened cmdline, which can
     * truncate or interpret characters from otherwise valid filesystem
     * arguments. Its initialization commands are this fixed bounded set;
     * COM's real argument vector still carries every explicit data path. */
    Cvar_SetROM("cmdline","+host_maxfps 72 +cl_startdemos 0 +sv_autosave 0");
    Cvar_Set("cl_startdemos","0");Cvar_Set("sv_autosave","0");
    if(!LQ_ExecuteCommand("lq_startup")){
        diagnostic("Ironwail startup commands failed");Host_Shutdown();return false;
    }
    if (frik_session) {
        extern int LQ_AddBotPack(const char *);
        if(!LQ_AddBotPack(botpack)){dm_rejected=session_mode==LQ_DEATHMATCH;return false;}
        char mappack[MAX_OSPATH];q_strlcpy(mappack,botpack,sizeof(mappack));
        char *packleaf=strrchr(mappack,'/');if(packleaf)q_strlcpy(packleaf+1,"dm-maps.pak",sizeof(mappack)-(size_t)(packleaf+1-mappack));
        if(access(mappack,R_OK)==0 && !LQ_AddBotPack(mappack))return false;
        if(!LQ_ExecuteCommand("maxplayers 8"))return false;
        Cvar_Set("coop",session_mode==LQ_DEATHMATCH?"0":"1");
        Cvar_Set("deathmatch",session_mode==LQ_DEATHMATCH?"1":"0");
    } else {
        Cvar_Set("deathmatch","0");Cvar_Set("coop","0");
    }
    Cvar_SetValueQuick(&host_maxfps,72.0f);
    Cvar_Set("cl_startdemos","0");Cvar_Set("sv_autosave","0");
    if(session_mode==LQ_DEATHMATCH) {
        for(int i=0;i<dm_config.count;i++)if(!retro_lq_map_exists(dm_config.maps[i])){dm_rejected=1;return false;}
        Cvar_SetValue("timelimit",dm_config.minutes);Cvar_SetValue("fraglimit",dm_config.frags);
        Cvar_Set("noexit","1");Cvar_Set("samelevel","0");
    }
    loaded=1;
    struct retro_system_av_info info;retro_get_system_av_info(&info);
    environment_callback(RETRO_ENVIRONMENT_SET_SYSTEM_AV_INFO,&info);
    return true;
}

void retro_run(void)
{
    if(!loaded)return;
    if(input_poll_callback)input_poll_callback();
    if(host_maxfps.value!=72.0f)Cvar_SetValueQuick(&host_maxfps,72.0f);
    /* No wall-clock catch-up, throttling loop or server/client work outside
     * this call. External pause means the frontend does not invoke us. */
    LQ_SoundAdvance();Host_Frame(1.0/72.0);LQ_SoundEmit();
}

void retro_unload_game(void)
{
    if(loaded){extern void LQ_RestorePresentation(void);LQ_RestorePresentation();loaded=0;Host_Shutdown();}
    LQ_EGL_SetFrameCallback(NULL);
    free(video_pixels);video_pixels=NULL;video_capacity=0;
    free(parms.membase);parms.membase=NULL;
    if(sdl_started){SDL_Quit();sdl_started=0;}
}
void retro_deinit(void){retro_unload_game();audio_callback=NULL;audio_batch_callback=NULL;}
void retro_reset(void){} /* Native frontend exposes its guarded restart-map. */
size_t retro_serialize_size(void){return 0;}
bool retro_serialize(void *data,size_t size){(void)data;(void)size;return false;}
bool retro_unserialize(const void *data,size_t size){(void)data;(void)size;return false;}
void retro_cheat_reset(void){}
void retro_cheat_set(unsigned index,bool enabled,const char *code){(void)index;(void)enabled;(void)code;}
bool retro_load_game_special(unsigned type,const struct retro_game_info *info,size_t count)
{(void)type;(void)info;(void)count;return false;}
unsigned retro_get_region(void){return RETRO_REGION_NTSC;}
void *retro_get_memory_data(unsigned id){(void)id;return NULL;}
size_t retro_get_memory_size(unsigned id){(void)id;return 0;}
