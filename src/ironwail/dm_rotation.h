// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef LQ_DM_ROTATION_H
#define LQ_DM_ROTATION_H
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
#define LQ_DM_MAPS_MAX 64
typedef struct { char maps[LQ_DM_MAPS_MAX][15]; int count, minutes, frags, bots; } LQ_DMConfig;
static inline int LQ_DMParse(LQ_DMConfig *out,const char *maps,int minutes,int frags,int bots) {
    LQ_DMConfig value={0};
    if(!maps || !*maps || minutes<1 || minutes>30 || (frags!=0 && (frags<5 || frags>100)) || bots<1 || bots>7)return 0;
    while(*maps) {
        const char *end=strchr(maps,','); size_t n=end?(size_t)(end-maps):strlen(maps);
        if(!n || n>14 || value.count==LQ_DM_MAPS_MAX)return 0;
        for(size_t i=0;i<n;i++)if(!(maps[i]>='a' && maps[i]<='z') && !(maps[i]>='0' && maps[i]<='9') && maps[i]!='_')return 0;
        memcpy(value.maps[value.count],maps,n);
        for(int i=0;i<value.count;i++)if(!strcmp(value.maps[i],value.maps[value.count]))return 0;
        value.count++;
        if(!end)break;
        maps=end+1;if(!*maps)return 0;
    }
    value.minutes=minutes;value.frags=frags;value.bots=bots;*out=value;return 1;
}
static inline const char *LQ_DMPick(const LQ_DMConfig *cfg,const char *current,unsigned random_value) {
    int choices[LQ_DM_MAPS_MAX],count=0;
    for(int i=0;i<cfg->count;i++)if(!current || strcmp(cfg->maps[i],current))choices[count++]=i;
    if(!count)return cfg->count?cfg->maps[0]:NULL;
    return cfg->maps[choices[random_value%(unsigned)count]];
}
#endif
