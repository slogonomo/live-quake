// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef LQ_DM_DIRECTOR_H
#define LQ_DM_DIRECTOR_H
#include <math.h>
typedef struct {
    int entity, number, alive, dying, parked;
    float fight, event, spawn, still_since, wall_since, stuck_since, submerged_since;
    float path, net, loiter_since, last_followed;
} LQ_DMBot;
/* A persistent wall view may shorten the normal dwell. Keep a brief fight
 * grace, but damage exchanged at a blocked corner must not pin the camera. */
static inline int LQ_DMWallIdle(const LQ_DMBot *b,float now) {
    if(b->wall_since<=0 || now-b->wall_since<2)return 0;
    return now-b->wall_since>=3 || !(b->fight>0 && now-b->fight<3);
}
static inline int LQ_DMWaterIdle(const LQ_DMBot *b,float now) {
    return b->submerged_since>0 && now-b->submerged_since>=3 && now-b->still_since>=3;
}
static inline int LQ_DMIdle(const LQ_DMBot *b,float now) {
    if(!b->alive || b->parked)return 1;
    if(LQ_DMWallIdle(b,now) || LQ_DMWaterIdle(b,now))return 1;
    if(b->fight>0 && now-b->fight<3)return 0;
    return now-b->still_since>=5 ||
        (b->stuck_since>0 && now-b->stuck_since>=3) ||
        (b->loiter_since>0 && now-b->loiter_since>=2);
}
static inline int LQ_DMPriority(const LQ_DMBot *b,float now) {
    if(b->fight>0 && now-b->fight<3)return 4000;
    if(b->fight>0 && now-b->fight<10)return 2000;
    if(b->event>0 && now-b->event<5)return 1000;
    return 500;
}
static inline int LQ_DMChoose(const LQ_DMBot *bots,int count,int followed,int left,float now,float switched,float fallback) {
    const LQ_DMBot *f=NULL;int best=-1,score=-1;float oldest=now+1;
    for(int i=0;i<count;i++)if(bots[i].entity==followed)f=&bots[i];
    int invalid=!f || f->parked;
    int poor=!invalid && (LQ_DMWallIdle(f,now) || LQ_DMWaterIdle(f,now));
    if(!invalid && (f->dying || (!poor && f->fight>0 && now-f->fight<3)))return followed;
    int stalled=!invalid && LQ_DMIdle(f,now) && now-f->still_since>=5;
    if(!invalid && now-switched<((poor || stalled)?2:8) && f->alive)return followed;
    int bored=f && now-fmaxf(f->fight,f->event)>20;
    if(!invalid && !LQ_DMIdle(f,now) && !bored)return followed;
    for(int i=0;i<count;i++) {
        const LQ_DMBot *b=&bots[i];
        if(b->entity==followed || !b->alive || b->parked || now-b->spawn<1.5)continue;
        int idle=LQ_DMIdle(b,now);
        /* If every bot is idle, a clear view beats a wall after a short hold.
         * Active bots are still considered first; the ordinary fallback stays 15 s. */
        if(!invalid && now-fallback<15 && idle &&
           !(poor && (LQ_DMWaterIdle(f,now) || now-f->wall_since>=3) &&
             b->wall_since<=0 && b->submerged_since<=0))continue;
        if(b->entity==left && now-switched<15 && LQ_DMPriority(b,now)<4000)continue;
        if(bored && !LQ_DMIdle(f,now) && !(b->fight>0 && now-b->fight<10))continue;
        int p=idle?0:LQ_DMPriority(b,now);
        /* Active dry views outrank water fights and the idle fallback. */
        if(b->submerged_since>0)p/=10;
        if(!idle)p+=5000;
        if(p>score || (p==score && b->last_followed<oldest)){best=b->entity;score=p;oldest=b->last_followed;}
    }
    return best<0?followed:best;
}
#endif
