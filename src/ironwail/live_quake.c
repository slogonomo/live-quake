// SPDX-License-Identifier: GPL-2.0-or-later
/* Live Quake session bridge. FrikBot owns movement, combat and navigation. */
#include "quakedef.h"
#include "client.h"
#include "cmd.h"
#include "console.h"
#include "keys.h"
#include "progs.h"
#include "protocol.h"
#include "screen.h"
#include "server.h"
#include "world.h"
#include "render.h"
#include "lq_bridge.h"
#include <math.h>
#include "level_bypass.h"
#include "dm_director.h"
extern int in_impulse;
static edict_t *player(void);
static int bot=1, loading;
static cvar_t lq_e1m1_assist = {"lq_e1m1_assist", "0", CVAR_NONE};

/* Explicit offline diagnostic: real player-hull traces, never bot steering.
 * mode 0 uses current brush states; mode 1 omits operable gates but retains
 * t15, t4 and floor bridges; mode 2 is a bullet point trace in current states.
 * No diagnostic mode moves entities or fires activators. */
extern trace_t SV_ClipMoveToEntity(edict_t *, vec3_t, vec3_t, vec3_t, vec3_t);
int retro_lq_exit_brush(int index, float *out, char *name, size_t capacity) {
    if(!sv.active || index<1 || index>=sv.qcvm.num_edicts || !out || !name || capacity<256)return 0;
    qcvm_t *old=qcvm;PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
    edict_t *e=EDICT_NUM(index);int valid=!e->free && (PR_GetString(e->v.model)[0]=='*' ||
        (!strcmp(PR_GetString(e->v.classname),"trigger_multiple") && !strcmp(PR_GetString(e->v.target),"t4")));
    if(valid) {
        VectorCopy(e->v.origin,out);VectorCopy(e->v.absmin,out+3);VectorCopy(e->v.absmax,out+6);
        eval_t *p=GetEdictFieldValueByName(e,"pos1"),*q=GetEdictFieldValueByName(e,"pos2");
        for(int i=0;i<3;i++){out[9+i]=p?p->vector[i]:0;out[12+i]=q?q->vector[i]:0;}
        eval_t *state=GetEdictFieldValueByName(e,"state");out[15]=state?state->_float:0;
        snprintf(name,capacity,"%s|%s|%s",PR_GetString(e->v.classname),PR_GetString(e->v.targetname),PR_GetString(e->v.model));
    }
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);return valid;
}
int retro_lq_exit_trace(const float *from, const float *to, int mode, float *out) {
    if (!sv.active || !from || !to || !out || mode<0 || mode>2) return 0;
    vec3_t start,end,mins={-16,-16,-24},maxs={16,16,32};
    if(mode==2) {for(int i=0;i<3;i++)mins[i]=maxs[i]=0;}
    for(int i=0;i<3;i++) {
        if(!isfinite(from[i]) || !isfinite(to[i]) || fabsf(from[i])>8192 || fabsf(to[i])>8192)return 0;
        start[i]=from[i];end[i]=to[i];
    }
    qcvm_t *old=qcvm;PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
    trace_t trace;
    if(mode!=1)trace=SV_Move(start,mins,maxs,end,mode==2?MOVE_NORMAL:MOVE_NOMONSTERS,player());
    else {
        trace=SV_ClipMoveToEntity(EDICT_NUM(0),start,mins,maxs,end);
        for(int i=1;i<qcvm->num_edicts;i++) {
            edict_t *e=EDICT_NUM(i);
            if(e->free || e->v.solid!=SOLID_BSP)continue;
            if(!strcmp(PR_GetString(e->v.classname),"door") && strcmp(PR_GetString(e->v.targetname),"t15") && strcmp(PR_GetString(e->v.targetname),"t4") &&
               !(e->v.size[2]<=24 && (e->v.size[0]>64 || e->v.size[1]>64)))continue;
            trace_t candidate=SV_ClipMoveToEntity(e,start,mins,maxs,end);
            if(candidate.allsolid || candidate.startsolid || candidate.fraction<trace.fraction)trace=candidate;
        }
    }
    out[0]=trace.fraction;VectorCopy(trace.endpos,out+1);
    out[4]=trace.startsolid;out[5]=trace.allsolid;
    out[6]=trace.ent?NUM_FOR_EDICT(trace.ent):0;
    out[7]=SV_PointContents(trace.endpos);out[8]=trace.plane.normal[2];
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);return 1;
}

static unsigned native_event;
static int native_step;
static char native_reason[40];
static float view_clearance, view_light;
extern int R_LightPoint(vec3_t p, float ofs, lightcache_t *cache);
static int poor_ambient_view(edict_t *actor) {
    vec3_t eye, end, zero={0,0,0};
    float distances[8];
    lightcache_t cache={0};
    VectorAdd(actor->v.origin,actor->v.view_ofs,eye);
    for(int ray=0;ray<8;ray++) {
        float yaw=(actor->v.v_angle[YAW]-35+ray*10)*M_PI/180;
        VectorCopy(eye,end);end[0]+=cosf(yaw)*512;end[1]+=sinf(yaw)*512;
        trace_t trace=SV_Move(eye,zero,zero,end,MOVE_NOMONSTERS,actor);
        distances[ray]=trace.fraction*512;
    }
    for(int i=1;i<8;i++)for(int j=i;j>0 && distances[j]<distances[j-1];j--) {
        float swap=distances[j];distances[j]=distances[j-1];distances[j-1]=swap;
    }
    view_clearance=(distances[3]+distances[4])*.5f;
    view_light=cl.worldmodel?R_LightPoint(eye,0,&cache):255;
    eval_t *enemy=GetEdictFieldValueByName(actor,"enemy");
    return !cl.intermission && (!enemy || !enemy->edict) && (view_clearance<96 || view_light<4);
}
int retro_lq_nextmap(char *out,size_t size);

/* Small read-only control reply for private match-continuity probes. */
int retro_lq_match_state(char *out, size_t capacity) {
    if (!out || capacity < 256 || !sv.active || !player()) return 0;
    qcvm_t *old = qcvm;
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(&sv.qcvm);
    edict_t *actor = player();
    if (!strcmp(PR_GetString(actor->v.classname), "botcam") && actor->v.enemy)
        actor = PROG_TO_EDICT(actor->v.enemy);
    int n = snprintf(out, capacity,
        "{\"map\":\"%s\",\"time\":%.6f,\"frags\":%.0f,\"deathmatch\":%.0f,\"slots\":%d}",
        sv.name, sv.qcvm.time, actor->v.frags, deathmatch.value, svs.maxclients);
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(old);
    return n > 0 && (size_t)n < capacity;
}

static int session_call(const char *name);
static float dm_value(edict_t *e,const char *name) {
    eval_t *v=GetEdictFieldValueByName(e,name);return v?v->_float:0;
}
static int pinned_entity;
void retro_lq_pin_view(int entity) { pinned_entity=entity; }
static int director_followed,director_left;
static float director_checked,director_switched,director_idle;
static unsigned director_switches;
typedef struct { LQ_DMBot signal; vec3_t anchor,points[13]; float times[13]; int samples;
    float health,armor,ammo,items,frags; } DMTrack;
static DMTrack dm_tracks[8];
static char director_map[64];
static void dm_director(void) {
    edict_t *human=player();
    if(!deathmatch.value || !bot || loading || sv.paused || cl.intermission || !human)return;
    qcvm_t *old=qcvm;PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
    if(strcmp(PR_GetString(human->v.classname),"botcam"))goto finish;
    float now=(float)sv.qcvm.time;
    if(strcmp(director_map,sv.name) || now<director_checked) {
        memset(dm_tracks,0,sizeof(dm_tracks));q_strlcpy(director_map,sv.name,sizeof(director_map));
        director_followed=director_left=0;director_checked=director_switched=director_idle=0;
    }
    if(now-director_checked<.25)goto finish;
    director_checked=now;
    LQ_DMBot signals[8];int count=0;
    for(int i=2;i<=svs.maxclients;i++) {
        edict_t *e=EDICT_NUM(i);DMTrack *t=&dm_tracks[i-1];
        if(e->free || strcmp(PR_GetString(e->v.classname),"player") || dm_value(e,"ishuman"))continue;
        int alive=e->v.health>0;
        if(!t->signal.entity || (alive && !t->signal.alive)) {
            memset(t,0,sizeof(*t));t->signal.entity=i;t->signal.spawn=now;
            t->signal.still_since=now;VectorCopy(e->v.origin,t->anchor);
            eval_t *spawn=GetEdictFieldValueByName(e,"lq_spawn_time");if(spawn)spawn->_float=now;
        }
        LQ_DMBot *b=&t->signal;
        b->alive=alive;b->dying=e->v.deadflag==1;b->parked=dm_value(e,"lq_parked")!=0;
        b->number=(int)dm_value(e,"b_num");b->fight=dm_value(e,"lq_fight_time");
        b->stuck_since=dm_value(e,"lq_stuck_since");
        if(e->v.waterlevel>=2){if(!b->submerged_since)b->submerged_since=now;}
        else b->submerged_since=0;
        float ammo=e->v.ammo_shells+e->v.ammo_nails+e->v.ammo_rockets+e->v.ammo_cells;
        if(e->v.frags>t->frags || e->v.health>t->health || e->v.armorvalue>t->armor || ammo>t->ammo || ((int)e->v.items & ~(int)t->items))b->event=now;
        eval_t *event=GetEdictFieldValueByName(e,"lq_event_time");if(event)event->_float=b->event;
        t->health=e->v.health;t->armor=e->v.armorvalue;t->ammo=ammo;t->items=e->v.items;t->frags=e->v.frags;
        vec3_t delta;VectorSubtract(e->v.origin,t->anchor,delta);
        /* Hopping or bobbing in place is not camera-worthy route progress. */
        delta[2]=0;
        if(VectorLength(delta)>=160){VectorCopy(e->v.origin,t->anchor);b->still_since=now;}
        if(!t->samples || now-t->times[t->samples-1]>=1) {
            if(t->samples==13){memmove(t->points,t->points+1,12*sizeof(vec3_t));memmove(t->times,t->times+1,12*sizeof(float));t->samples=12;}
            VectorCopy(e->v.origin,t->points[t->samples]);t->times[t->samples++]=now;
            b->path=0;
            for(int n=1;n<t->samples;n++){VectorSubtract(t->points[n],t->points[n-1],delta);b->path+=VectorLength(delta);}
            VectorSubtract(e->v.origin,t->points[0],delta);b->net=VectorLength(delta);
            if(t->samples==13 && b->path>600 && b->net<b->path*.25f){if(!b->loiter_since)b->loiter_since=now;}
            else b->loiter_since=0;
        }
        /* Wall holds use the director's 4 Hz cadence, independently of the
         * one-second path history. Brief corner turns do not satisfy the hold. */
        poor_ambient_view(e);
        if(view_clearance<96){if(!b->wall_since)b->wall_since=now;}else b->wall_since=0;
        signals[count++]=*b;
    }
    int followed=human->v.enemy?NUM_FOR_EDICT(PROG_TO_EDICT(human->v.enemy)):0;
    if(followed!=director_followed){director_followed=followed;director_switched=now;}
    if(!director_idle)director_idle=now;
    int selected=LQ_DMChoose(signals,count,followed,director_left,now,director_switched,director_idle);
    if(selected!=followed && selected>1 && (now-director_switched>=.3 || !followed)) {
        edict_t *next=EDICT_NUM(selected);
        next->v.dmg_take=next->v.dmg_save=0;
        human->v.enemy=EDICT_TO_PROG(next);
        director_left=followed;director_followed=selected;director_switched=now;director_idle=now;
        dm_tracks[selected-1].signal.last_followed=now;director_switches++;
        const char *name=PR_GetString(next->v.netname);
        char message[128];q_snprintf(message,sizeof(message),"Now tracking %s\n",name);
        client_t *saved_client=host_client;host_client=&svs.clients[0];
        SV_ClientPrintf("%s",message);host_client=saved_client;
        if(followed>1 && followed<=svs.maxclients) {
            eval_t *nudge=GetEdictFieldValueByName(human,"lq_nudge_bot");
            if(nudge)nudge->edict=EDICT_TO_PROG(EDICT_NUM(followed));
            session_call("LQ_NudgeBot");
        }
    } else for(int i=0;i<count;i++)if(signals[i].entity==followed && !LQ_DMIdle(&signals[i],now))director_idle=now;
finish:
    if(human->v.enemy)poor_ambient_view(PROG_TO_EDICT(human->v.enemy));
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);
}

/* Read-only evidence from the real server VM, including FrikBot's goal stack.
 * Explicit diagnostic caller only; never used to steer the bot. */
int retro_lq_frikbot_snapshot(char *out, size_t capacity) {
    qcvm_t *previous = qcvm;
    edict_t *pl = NULL;
    int i, used = 0;
    if (!out || capacity < 4096 || !sv.active) return 0;
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(&sv.qcvm);
    for (i = 1; i <= svs.maxclients; i++) {
        edict_t *candidate = EDICT_NUM(i);
        eval_t *human = GetEdictFieldValueByName(candidate, "ishuman");
        if (!candidate->free && human && !human->_float &&
            !strcmp(PR_GetString(candidate->v.classname), "player")) {
            pl = candidate; break;
        }
    }
    if(player()) {
        pl=player();
        if(!strcmp(PR_GetString(pl->v.classname),"botcam") && pl->v.enemy)pl=PROG_TO_EDICT(pl->v.enemy);
        i=NUM_FOR_EDICT(pl);
    }
    if ((!pl || cl.intermission) && player()) { pl=player();i=NUM_FOR_EDICT(pl); }
    if (pl) {
        int real_clients=0, synthetic_clients=0;
        for(int slot=0;slot<svs.maxclients;slot++)if(svs.clients[slot].active)real_clients++;
        for(int slot=1;slot<=svs.maxclients;slot++) {
            edict_t *candidate=EDICT_NUM(slot);
            eval_t *human=GetEdictFieldValueByName(candidate,"ishuman");
            if(!candidate->free && human && !human->_float && !strcmp(PR_GetString(candidate->v.classname),"player"))synthetic_clients++;
        }
        used = snprintf(out, capacity,
            "{\"time\":%.6f,\"entity\":%d,\"position\":[%.3f,%.3f,%.3f],"
            "\"velocity\":[%.3f,%.3f,%.3f],\"health\":%.0f,\"angles\":[%.3f,%.3f,%.3f]",
            sv.qcvm.time, i, pl->v.origin[0], pl->v.origin[1], pl->v.origin[2],
            pl->v.velocity[0], pl->v.velocity[1], pl->v.velocity[2], pl->v.health,
            pl->v.v_angle[0], pl->v.v_angle[1], pl->v.v_angle[2]);
        used+=snprintf(out+used,capacity-used,",\"waterlevel\":%.0f",pl->v.waterlevel);
        used+=snprintf(out+used,capacity-used,",\"frags\":%.0f,\"deadflag\":%.0f,\"b_num\":%.0f,\"parked\":%.0f,\"lq_fight_time\":%.3f,\"director_switches\":%u",
            pl->v.frags,pl->v.deadflag,dm_value(pl,"b_num"),dm_value(pl,"lq_parked"),dm_value(pl,"lq_fight_time"),director_switches);
        used+=snprintf(out+used,capacity-used,",\"intermission\":%d",cl.intermission);
        used+=snprintf(out+used,capacity-used,",\"assist_enabled\":%.0f",Cvar_VariableValue("lq_e1m1_assist"));
        used+=snprintf(out+used,capacity-used,",\"real_clients\":%d,\"synthetic_clients\":%d,\"tracked_equals_view\":%s",real_clients,synthetic_clients,i==cl.viewentity?"true":"false");
        used+=snprintf(out+used,capacity-used,",\"view_clearance\":%.1f,\"view_light\":%.1f",view_clearance,view_light);
        used += snprintf(out+used, capacity-used,
            ",\"camera_eye\":[%.3f,%.3f,%.3f],\"eye_contents\":%d,\"body_contents\":%d,\"view_entity\":%d",
            r_refdef.vieworg[0],r_refdef.vieworg[1],r_refdef.vieworg[2],
            SV_PointContents(r_refdef.vieworg),SV_PointContents(pl->v.origin),cl.viewentity);
        used += snprintf(out+used, capacity-used,
            ",\"camera_angles\":[%.3f,%.3f,%.3f],\"human_position\":[%.3f,%.3f,%.3f]",
            r_refdef.viewangles[0],r_refdef.viewangles[1],r_refdef.viewangles[2],
            svs.clients[0].edict->v.origin[0],svs.clients[0].edict->v.origin[1],svs.clients[0].edict->v.origin[2]);
        used += snprintf(out+used, capacity-used,
            ",\"inventory\":[%.3f,%.3f,%.3f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f],\"classname\":\"%s\"",
            pl->v.health,pl->v.armorvalue,pl->v.armortype,pl->v.items,pl->v.ammo_shells,pl->v.ammo_nails,
            pl->v.ammo_rockets,pl->v.ammo_cells,pl->v.weapon,pl->v.currentammo,PR_GetString(pl->v.classname));
        used += snprintf(out+used, capacity-used,
            ",\"map\":\"%.15s\",\"ladder_event\":%u,\"ladder_step\":%d,\"ladder_reason\":\"%s\"",
            sv.name,native_event,native_step,native_reason);
        edict_t *terminal=pl;
        const char *reason="qc";
        for(int n=4;n>=1;n--) {
            char key[16];snprintf(key,sizeof(key),"target%d",n);
            eval_t *value=GetEdictFieldValueByName(pl,key);
            if(value && value->edict){terminal=PROG_TO_EDICT(value->edict);break;}
        }
        const char *kind=PR_GetString(terminal->v.classname);
        if(!strcmp(kind,"trigger_changelevel"))reason="exit";
        else if((int)terminal->v.flags & 32)reason="enemy";
        else if((int)terminal->v.flags & 256)reason="item";
        else if(!strcmp(kind,"waypoint")) {
            eval_t *seen=GetEdictFieldValueByName(terminal,"lq_seen");
            eval_t *number=GetEdictFieldValueByName(pl,"b_clientno");
            int slot=number?(int)number->_float:-1;
            if(slot>=0 && slot<16 && (!seen || !((int)seen->_float & (1<<slot))))reason="unseen";
        }
        eval_t *assist_stage=GetEdictFieldValueByName(pl,"lq_e1m1_stage");
        if(assist_stage && assist_stage->_float>0)reason="assist";
        used+=snprintf(out+used,capacity-used,",\"goal_selection_reason\":\"%s\"",reason);
        const char *scalars[] = {"keys", "b_aiflags", "route_failed", "lq_progress_time", "lq_retry_time", "lq_stuck_event", "lq_manoeuvre_count", "lq_checkpoint_needed", "lq_cell_count", "lq_cell_time", "lq_damage_dealt", "lq_e1m1_stage"};
        for (unsigned n=0; n<sizeof(scalars)/sizeof(*scalars); n++) {
            if (used < 0 || (size_t)used >= capacity) goto snapshot_done;
            eval_t *value = GetEdictFieldValueByName(pl, scalars[n]);
            used += snprintf(out+used, capacity-used, ",\"%s\":%.3f", scalars[n], value ? value->_float : 0);
        }
        const char *targets[] = {"target1", "target2", "target3", "target4", "last_way", "current_way", "enemy", "lq_activator"};
        for (unsigned n=0; n<sizeof(targets)/sizeof(*targets); n++) {
            if (used < 0 || (size_t)used >= capacity) goto snapshot_done;
            eval_t *value = GetEdictFieldValueByName(pl, targets[n]);
            int reference = value ? value->edict : 0;
            edict_t *target = reference >= 0 && reference % sv.qcvm.edict_size == 0 &&
                reference / sv.qcvm.edict_size < sv.qcvm.num_edicts ? PROG_TO_EDICT(reference) : EDICT_NUM(0);
            eval_t *number = GetEdictFieldValueByName(target, "count");
            used += snprintf(out+used, capacity-used,
                ",\"%s\":{\"entity\":%d,\"waypoint\":%.0f,\"classname\":\"%.64s\",\"position\":[%.3f,%.3f,%.3f]}",
                targets[n], NUM_FOR_EDICT(target), number ? number->_float : 0,
                PR_GetString(target->v.classname), target->v.origin[0], target->v.origin[1], target->v.origin[2]);
        }
        if (used >= 0 && (size_t)used < capacity) used += snprintf(out+used,capacity-used,",\"bots\":[");
        int entries=0;
        for(int slot=2;slot<=svs.maxclients;slot++) {
            if(used<0 || (size_t)used>=capacity)goto snapshot_done;
            edict_t *e=EDICT_NUM(slot);
            if(e->free || strcmp(PR_GetString(e->v.classname),"player") || dm_value(e,"ishuman"))continue;
            used+=snprintf(out+used,capacity-used,"%s{\"entity\":%d,\"b_num\":%.0f,\"health\":%.0f,\"frags\":%.0f,\"parked\":%.0f,\"fight\":%.3f}",
                entries++?",":"",slot,dm_value(e,"b_num"),e->v.health,e->v.frags,dm_value(e,"lq_parked"),dm_value(e,"lq_fight_time"));
        }
        if(used>=0 && (size_t)used<capacity)used+=snprintf(out+used,capacity-used,"]");
        if (used >= 0 && (size_t)used < capacity) used += snprintf(out+used, capacity-used, "}");
    }
snapshot_done:
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(previous);
    return used > 0 && (size_t)used < capacity;
}

static edict_t *player(void) {
    return sv.active && svs.clients && svs.clients[0].active ? svs.clients[0].edict : NULL;
}
static int session_call(const char *name) {
    qcvm_t *previous = qcvm;
    edict_t *human = player();
    int found = 0;
    if (!human || !sv.qcvm.progs) return 0;
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(&sv.qcvm);
    for (int i=1;i<qcvm->progs->numfunctions;i++) {
        if (!strcmp(PR_GetString(qcvm->functions[i].s_name), name)) {
            int oldself = pr_global_struct->self;
            pr_global_struct->self = EDICT_TO_PROG(human);
            PR_ExecuteProgram(i);
            pr_global_struct->self = oldself;
            found = 1; break;
        }
    }
    /* Map changes free the client's hunk before server-side bots sign on.
     * Until the new client is ready, cl_entities may point into new map data. */
    if (found && cls.state == ca_connected && cls.signon == SIGNONS) {
        int old_view=cl.viewentity;
        VectorCopy(human->v.v_angle, cl.viewangles);
        int intermission = 0;
        for (int i=0;i<qcvm->progs->numglobaldefs;i++)
            if (!strcmp(PR_GetString(qcvm->globaldefs[i].s_name),"intermission_running")) {
                intermission = G_FLOAT(qcvm->globaldefs[i].ofs) != 0;
                break;
            }
        cl.viewentity = !intermission && !strcmp(PR_GetString(human->v.classname), "botcam") ?
            NUM_FOR_EDICT(PROG_TO_EDICT(human->v.enemy)) : NUM_FOR_EDICT(human);
        if (cl_entities && cl.viewentity > 0 && cl.viewentity < cl_max_edicts &&
            (old_view!=cl.viewentity || intermission || !strcmp(name,"LQ_EnterCamera") || !strcmp(name,"LQ_Takeover"))) {
            edict_t *actor=cl.viewentity==NUM_FOR_EDICT(human)?human:PROG_TO_EDICT(human->v.enemy);
            entity_t *camera=&cl_entities[cl.viewentity];
            VectorCopy(actor->v.origin,camera->origin);
            VectorCopy(actor->v.origin,camera->msg_origins[0]);
            VectorCopy(actor->v.origin,camera->msg_origins[1]);
            VectorCopy(actor->v.angles,camera->angles);
            VectorCopy(actor->v.angles,camera->msg_angles[0]);
            VectorCopy(actor->v.angles,camera->msg_angles[1]);
            camera->forcelink=true;
            camera->lerpflags|=LERP_RESETMOVE|LERP_RESETANIM;
        }
    }
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(previous);
    return found;
}
/* Ambient recovery counts territory and actual game outcomes, never velocity. */
static void ambient_progress(void) {
    static char map[16];
    static int cells[8192][3],count,forced,previous_kills,previous_secrets,previous_items;
    static float progress,last_time,empty_since,previous_health,previous_armor,previous_ammo,previous_damage;
    static float meaningful_time,light_time;
    static float bad_view_since,view_check_time;
    static int poor_view;
    static float assist_started;
    static int assist_highest_stage,skip_issued;
    static vec3_t meaningful_origin;
    qcvm_t *old=qcvm;
    edict_t *human=player(),*actor=NULL;
    int cell[3],fresh=1,pickup=0,kills=cl.stats[STAT_MONSTERS],secrets=cl.stats[STAT_SECRETS];
    float damage=0,ammo=0;
    int action=0;
    int experimental=Cvar_VariableValue("lq_e1m1_assist")!=0;
    if(deathmatch.value || !bot || loading || !human || (experimental && cl.intermission))return;
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(&sv.qcvm);
    if(strcmp(PR_GetString(human->v.classname),"botcam") || !human->v.enemy)goto done;
    actor=PROG_TO_EDICT(human->v.enemy);
    if(actor->v.health<=0)goto done;
    float now=(float)sv.qcvm.time;
    ammo=actor->v.ammo_shells+actor->v.ammo_nails+actor->v.ammo_rockets+actor->v.ammo_cells;
    eval_t *dealt=GetEdictFieldValueByName(actor,"lq_damage_dealt");
    if(dealt)damage=dealt->_float;
    if(strcmp(map,sv.name) || now<last_time) {
        q_strlcpy(map,sv.name,sizeof(map));count=0;forced=0;empty_since=0;progress=now;
        previous_kills=kills;previous_secrets=secrets;previous_items=(int)actor->v.items;
        previous_health=actor->v.health;previous_armor=actor->v.armorvalue;
        previous_ammo=ammo;previous_damage=damage;
        meaningful_time=now;light_time=now-1;bad_view_since=0;view_check_time=now-1;poor_view=0;VectorCopy(actor->v.origin,meaningful_origin);
        assist_started=0;assist_highest_stage=0;
        skip_issued=0;
    }
    if(experimental && skip_issued)goto done;
    last_time=now;
    for(int axis=0;axis<3;axis++)cell[axis]=(int)floorf(actor->v.origin[axis]/128);
    for(int i=0;i<count;i++)if(!memcmp(cell,cells[i],sizeof(cell))){fresh=0;break;}
    if(fresh && count<8192)memcpy(cells[count++],cell,sizeof(cell));
    pickup=actor->v.health>previous_health || actor->v.armorvalue>previous_armor ||
        ammo>previous_ammo || ((int)actor->v.items & ~previous_items) || secrets>previous_secrets;
    int game_progress=pickup || kills>previous_kills || damage>previous_damage;
    if(fresh || pickup || kills>previous_kills || damage>previous_damage){progress=now;forced=0;}
    vec3_t displacement;VectorSubtract(actor->v.origin,meaningful_origin,displacement);
    if(VectorLength(displacement)>=600 || pickup || kills>previous_kills || damage>previous_damage) {
        meaningful_time=now;VectorCopy(actor->v.origin,meaningful_origin);
    }
    previous_health=actor->v.health;previous_armor=actor->v.armorvalue;previous_ammo=ammo;
    previous_items=(int)actor->v.items;previous_kills=kills;previous_secrets=secrets;previous_damage=damage;
    eval_t *goal=GetEdictFieldValueByName(actor,"target1");
    eval_t *stage=GetEdictFieldValueByName(actor,"lq_e1m1_stage");
    int assist_active=experimental && stage && stage->_float>=1 && stage->_float<=3 && !strcmp(sv.name,"e1m1");
    if(assist_active && (int)stage->_float>assist_highest_stage) {
        assist_started=now;assist_highest_stage=(int)stage->_float;
    }
    if(goal && !goal->edict) {
        if(!empty_since)empty_since=now;
        if(now-empty_since>=3){action=1;empty_since=now;}
    } else empty_since=0;
    if(actor->v.waterlevel>=2 && now-progress>=6 && !forced){action=4;forced=1;}
    int assist_grace=assist_active && now-assist_started<45;
    if(!assist_grace && (now-progress>=15 || (actor->v.waterlevel>=2 && now-progress>=12))) {
        if(forced!=2){action=2;forced=2;}
    }
    if((assist_active && now-assist_started>=45) || (!assist_grace && (now>=90 || actor->v.waterlevel>=2) && (now-progress>=30 || now-meaningful_time>=30))){action=3;progress=meaningful_time=now;forced=0;VectorCopy(actor->v.origin,meaningful_origin);}
    if(now-view_check_time>=1){view_check_time=now;poor_view=poor_ambient_view(actor);}
    if(!poor_view || game_progress)bad_view_since=0;
    else if(!bad_view_since)bad_view_since=now;
    else if(0 && now-bad_view_since>=20){action=6;bad_view_since=now;}
    else if(0 && now-bad_view_since>=10 && forced!=2){action=5;forced=2;}
 done:
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(old);
    if(sv.qcvm.time-light_time>=1){light_time=(float)sv.qcvm.time;session_call("LQ_AmbientLight");}
    if(action) {
        native_event++;native_step=action;
        q_strlcpy(native_reason,action==1?"empty_goal_3s":action==2?"no_world_progress_manoeuvre":action==4?"submerged_no_progress_6s":action==5?"near_wall_or_dark_10s":action==6?"near_wall_or_dark_20s":"no_600u_or_game_progress_30s",sizeof(native_reason));
        if(action==3 || action==6) {
            if(experimental)skip_issued=1;
            session_call("LQ_SkipLevel");
        } else if(action==4)session_call("LQ_WaterRecover");
        else session_call("LQ_ForceRecovery");
    }
}

/* ABI hooks retained for the reviewed upstream patch; old combat is retired. */
void LQ_GrenadeProgram(const void *raw,size_t length) {(void)raw;(void)length;}
void LQ_GrenadeBeforeWeapon(edict_t *pl) {(void)pl;}
void LQ_GrenadeAfterWeapon(edict_t *pl) {(void)pl;}
void LQ_GrenadeWorldReset(void) {}

static unsigned human_quits;
void retro_lq_loading(int enabled) { loading=enabled; }
int LQ_SuspendWorld(void) { return loading; }

/* Automatic hub travel belongs to the session, including checkpoint loads and
 * F12. C statics survive a failed changelevel; QC globals would reset and loop. */
int LQ_LocalFrikSession(void);
int retro_lq_world_ok(void);
static char lq_prev_map[64], lq_hub_target[16];
static int lq_hub_failures;

static int lq_hub_destination(char *out, size_t size) {
    const char *next = !strcmp(lq_prev_map,"e1m7") ? "e2m1" :
                       !strcmp(lq_prev_map,"e2m6") ? "e3m1" :
                       !strcmp(lq_prev_map,"e3m6") ? "e4m1" : "e1m1";
    char path[32];
    if(!COM_FileExists("maps/e1m1.bsp",NULL))return 0;
    snprintf(path,sizeof(path),"maps/%s.bsp",next);
    if(!COM_FileExists(path,NULL))next="e1m1";
    q_strlcpy(out,next,size);
    return 1;
}

static void lq_leave_start_hub(void) {
    if(deathmatch.value || !sv.active || loading)return;
    if(strcmp(sv.name,"start")) {
        q_strlcpy(lq_prev_map,sv.name,sizeof(lq_prev_map));
        if(!strcmp(sv.name,lq_hub_target)) {
            lq_hub_target[0]=0;
            lq_hub_failures=0;
        }
        return;
    }
    if(!bot || cls.demoplayback || cl.intermission || svs.changelevel_issued ||
       !LQ_LocalFrikSession() || !svs.clients[0].spawned || !retro_lq_world_ok())return;
    qcvm_t *old=qcvm;
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
    edict_t *human=svs.clients[0].edict;
    int ready=!strcmp(PR_GetString(human->v.classname),"botcam") && human->v.enemy &&
              PROG_TO_EDICT(human->v.enemy)->v.health>0;
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);
    if(!ready)return;
    if(lq_hub_failures>=2)return;
    if(lq_hub_target[0] && ++lq_hub_failures>=2)return;
    if(!lq_hub_destination(lq_hub_target,sizeof(lq_hub_target)))return;
    svs.changelevel_issued=true;
    Cbuf_AddText(va("changelevel %s\n",lq_hub_target));
}

void LQ_MaintainCamera(void) {
    if(bot && !loading && player()) {
        dm_director();
        session_call("LQ_MaintainCamera");
    }
    lq_leave_start_hub();
}

/* Client input executes outside the server VM. Scope every edict/string helper
 * to the local server VM and restore the caller, including early bot returns. */
void LQ_BotMove(usercmd_t *cmd, int *buttons) {
    if (!bot) return;
    memset(cmd,0,sizeof(*cmd));
    *buttons=0;
    if (!loading && sv.active && sv.qcvm.time >= 2 && player()) {
        qcvm_t *previous=qcvm;
        PR_SwitchQCVM(NULL); PR_SwitchQCVM(&sv.qcvm);
        int needs_camera=strcmp(PR_GetString(player()->v.classname),"botcam") != 0;
        PR_SwitchQCVM(NULL); PR_SwitchQCVM(previous);
        if (needs_camera) session_call("LQ_EnterCamera");
        ambient_progress();
    }
}

void retro_lq_state(DDHeader *h) {
    qcvm_t *previous=qcvm;
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(&sv.qcvm);
    edict_t *pl=player();
    if(pl && !strcmp(PR_GetString(pl->v.classname),"botcam") && pl->v.enemy)
        pl=PROG_TO_EDICT(pl->v.enemy);
    h->gamestate=cl.intermission?2:key_dest==key_game?0:1;
    h->tic=host_framecount; h->leveltime=(int)(sv.qcvm.time*1000);
    snprintf(h->map,sizeof(h->map),"%.15s",sv.active?sv.name:"");
    h->followed_entity=pl?NUM_FOR_EDICT(pl):0;h->followed_bnum=pl?(int)dm_value(pl,"b_num"):0;
    h->health=pl?(int)pl->v.health:0;
    h->x=pl?(int)(pl->v.origin[0]*1000):0; h->y=pl?(int)(pl->v.origin[1]*1000):0;
    h->z=pl?(int)(pl->v.origin[2]*1000):0;
    h->angle=(int)(cl.viewangles[YAW]*1000);
    h->kills=cl.stats[STAT_MONSTERS]; h->secrets=cl.stats[STAT_SECRETS];
    h->armor=cl.stats[STAT_ARMOR]; h->ammo[0]=cl.stats[STAT_SHELLS];
    h->ammo[1]=cl.stats[STAT_NAILS];h->ammo[2]=cl.stats[STAT_ROCKETS];h->ammo[3]=cl.stats[STAT_CELLS];
    h->ready_weapon=cl.stats[STAT_ACTIVEWEAPON]; h->weapons_owned=cl.stats[STAT_ITEMS];
    h->bot_replans=0; h->bot_stuck=0;
    h->deaths=0; h->levels_completed=0;
    h->human_quits=human_quits;
    h->bot_nodes=0;h->bot_visited=0;
    h->bot_goal=-1;h->bot_waypoint=-1;
    if (pl && sv.qcvm.progs) {
        eval_t *cells=GetEdictFieldValueByName(pl,"lq_cell_count");
        eval_t *events=GetEdictFieldValueByName(pl,"lq_stuck_event");
        eval_t *goal=GetEdictFieldValueByName(pl,"target1");
        h->bot_visited=cells?(unsigned)cells->_float:0;
        h->bot_stuck=events?(unsigned)events->_float:0;
        h->bot_goal=goal?goal->edict/sv.qcvm.edict_size:-1;
        h->bot_waypoint=h->bot_goal;
    }
    h->skill=(int)Cvar_VariableValue("skill");
    h->viewsize=(int)Cvar_VariableValue("viewsize");
    h->crosshair=(int)Cvar_VariableValue("crosshair");
    h->console_visible=con_forcedup || scr_con_current>0;
    PR_SwitchQCVM(NULL); PR_SwitchQCVM(previous);
}
int retro_lq_world_ok(void) { return sv.active && !cls.demoplayback && (cls.state==ca_connected && cls.signon==SIGNONS) && player()!=NULL; }
int retro_lq_nextmap(char *out,size_t size) {
    char map[LQ_LEVEL_MAP_MAX+1],path[LQ_LEVEL_MAP_MAX+10];
    if(!out || !size)return 0;
    out[0]=0;
    if(deathmatch.value || !sv.active || !sv.worldmodel)return 0;
    if(lq_level_nextmap(sv.worldmodel->entities,map,sizeof(map)) && strlen(map)<15 && strlen(map)<size) {
        /* The frame ABI clips at 15 bytes; preserve an unambiguous destination. */
        snprintf(path,sizeof(path),"maps/%s.bsp",map);
        if(COM_FileExists(path,NULL)){memcpy(out,map,strlen(map)+1);return 1;}
    }
    /* Unsupported exits return to a real start hub, never a map list. */
    if(strcmp(sv.name,"start") && size>strlen("start") && COM_FileExists("maps/start.bsp",NULL)) {
        memcpy(out,"start",sizeof("start"));return 1;
    }
    return 0;
}
int retro_lq_display_ok(void) {
    return cl.worldmodel && !cls.demoplayback && (cls.state==ca_connected && cls.signon==SIGNONS) && !con_forcedup &&
           scr_con_current<=0 && key_dest!=key_console;
}
int retro_lq_frame_visible(void) { return !bot || retro_lq_display_ok(); }
void retro_lq_replan(void) {
    session_call("LQ_Recover");
}
/* Human preference is separate from the forced full bot view. Keep native
 * float precision: the menu and console permit fractional screen sizes. */
static float human_viewsize=100;
static float human_flatlightstyles, human_powerup_cshift, human_dynamic, human_notifytime;
static int hide_chatter;
static int lightstyles_captured;
static float sanitize_human_viewsize(float value) {
    if(!isfinite(value))return 100;
    return value<30?30:value>130?130:value;
}
static void apply_human_viewsize(void) {
    char text[32];snprintf(text,sizeof(text),"%.9g",human_viewsize);
    Cvar_Set("viewsize",text);
}
float retro_lq_human_viewsize(void) {
    if(!bot)human_viewsize=sanitize_human_viewsize(Cvar_VariableValue("viewsize"));
    return human_viewsize;
}
void retro_lq_set_human_viewsize(float value) {
    human_viewsize=sanitize_human_viewsize(value);
    if(!bot)apply_human_viewsize();
}

int retro_lq_bot(int enabled) {
    int i, previous=bot;
    if (enabled && (!previous || !lightstyles_captured)) {
        if(!previous)retro_lq_human_viewsize();
        human_flatlightstyles=Cvar_VariableValue("r_flatlightstyles");
        human_powerup_cshift=Cvar_VariableValue("gl_cshiftpercent_powerup");
        human_dynamic=Cvar_VariableValue("r_dynamic");human_notifytime=Cvar_VariableValue("con_notifytime");
        lightstyles_captured=1;
    }
    if (!enabled && previous && player()) {
        if(pinned_entity>1 && pinned_entity<=svs.maxclients) {
            qcvm_t *old=qcvm;PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
            edict_t *p=EDICT_NUM(pinned_entity);
            if(p->v.health<=0 || dm_value(p,"lq_parked")){PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);pinned_entity=0;return 0;}
            player()->v.enemy=EDICT_TO_PROG(p);PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);
        }
        pinned_entity=0;
        session_call("LQ_Takeover");
        qcvm_t *old=qcvm;
        PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
        int still_camera=!strcmp(PR_GetString(player()->v.classname),"botcam");
        PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);
        if(still_camera)return 0;
    }
    bot=enabled;
    extern qboolean keydown[MAX_KEYS];
    Cvar_Set("crosshair",enabled?"0":"1");
    if(enabled) {
        Cvar_Set("r_dynamic","0");
        Cvar_SetValue("con_notifytime",hide_chatter?0:human_notifytime);
        Cvar_Set("r_flatlightstyles","1");
        Cvar_Set("gl_cshiftpercent_powerup","20");
        Cvar_Set("viewsize","120");
        Cvar_Set("gamma","0.7");sv.paused=false;scr_con_current=0;key_dest=key_game;
        if(sv.qcvm.time>=2)session_call("LQ_EnterCamera");
        if(!previous){director_switched=(float)sv.qcvm.time;director_followed=cl.viewentity;director_idle=0;}
    } else if(previous) {
        Cvar_SetValue("r_dynamic",human_dynamic);Cvar_SetValue("con_notifytime",human_notifytime);
        Cvar_SetValue("r_flatlightstyles",human_flatlightstyles);
        Cvar_SetValue("gl_cshiftpercent_powerup",human_powerup_cshift);
        apply_human_viewsize();
    }
    for(i=1;i<MAX_KEYS;i++)if(keydown[i])Key_Event(i,false);
    Key_ClearStates();in_impulse=0;
    if(player())player()->v.impulse=0;
    return 1;
}
void retro_lq_set_hide_chatter(int value) { hide_chatter=value!=0; if(bot && lightstyles_captured)Cvar_SetValue("con_notifytime",hide_chatter?0:human_notifytime); }
void LQ_RestorePresentation(void) {
    if(!lightstyles_captured)return;
    Cvar_SetValue("r_flatlightstyles",human_flatlightstyles);Cvar_SetValue("gl_cshiftpercent_powerup",human_powerup_cshift);
    Cvar_SetValue("r_dynamic",human_dynamic);Cvar_SetValue("con_notifytime",human_notifytime);
}

/* Keep the public bridge key numbers independent of the engine key enum. */
void retro_lq_key(int key,int down) {
    static const int special[]={K_UPARROW,K_DOWNARROW,K_LEFTARROW,K_RIGHTARROW,K_ALT,K_CTRL,K_SHIFT};
    if(key>=128 && key<=134) key=special[key-128];
    else if(key==127)key=K_BACKSPACE;
    else if(key>=135 && key<=146) key=K_F1+key-135;
    else if(key==147) key=K_INS; else if(key==148) key=K_DEL;
    else if(key==149) key=K_PGDN; else if(key==150) key=K_PGUP;
    else if(key==151) key=K_HOME; else if(key==152) key=K_END;
    else if(key>=200 && key<=204) key=K_MOUSE1+key-200;
    if(!bot && key>0 && key<MAX_KEYS) {
        Key_Event(key,down!=0);
        /* Ironwail receives text separately from physical key presses. Our
         * public bridge already supplies ASCII; no ambient SDL text input. */
        if(down && key>=32 && key<=126) {
            extern qboolean keydown[MAX_KEYS];
            int character=key;
            if(keydown[K_SHIFT] && character>='a' && character<='z')character+='A'-'a';
            Char_Event(character);
        }
    }
}
void retro_lq_mouse(int dx,int dy) {
    if(bot || key_dest!=key_game) return;
    cl.viewangles[YAW]-=dx*.12f; cl.viewangles[PITCH]+=dy*.12f;
    if(cl.viewangles[PITCH]>80) cl.viewangles[PITCH]=80;
    if(cl.viewangles[PITCH]<-70) cl.viewangles[PITCH]=-70;
}
int retro_lq_command(const char *command) {
    /* Called only with trusted, enumerated native commands by engine.c. */
    extern int LQ_ExecuteCommand(const char *);
    if (!Cvar_FindVar("lq_e1m1_assist")) Cvar_RegisterVariable(&lq_e1m1_assist);
    return LQ_ExecuteCommand(command);
}
void LQ_RequestQuit(void) {
    ++human_quits;retro_lq_bot(1);key_dest=key_game;m_state=0;
}

/* FrikBot's synthetic slots are not network connections. Preserve vanilla's
 * rejection for every true multiplayer session. */
static int local_frik_support=-1;
void LQ_ResetSessionSupport(void) { extern void LQ_DmSpawn(void); local_frik_support=-1; LQ_DmSpawn(); }
int LQ_LocalFrikSession(void) {
    if(!sv.active || !svs.clients || !svs.clients[0].active || !sv.qcvm.progs)return 0;
    for(int i=1;i<svs.maxclients;i++)if(svs.clients[i].active)return 0;
    qcvm_t *old=qcvm;
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(&sv.qcvm);
    if(local_frik_support<0) {
        local_frik_support=0;
        for(int i=1;i<qcvm->progs->numfunctions;i++)
            if(!strcmp(PR_GetString(qcvm->functions[i].s_name),"LQ_EnterCamera")){local_frik_support=1;break;}
    }
    eval_t *human=GetEdictFieldValueByName(svs.clients[0].edict,"ishuman");
    int supported=local_frik_support && human && human->_float==1;
    PR_SwitchQCVM(NULL);PR_SwitchQCVM(old);return supported;
}

int LQ_AmbientCamera(void) { return bot && LQ_LocalFrikSession(); }
