// SPDX-License-Identifier: GPL-2.0-or-later
/* Exit lookup from the entity text actually loaded by either engine.
 * The first trigger_changelevel in textual entity order wins, even when its
 * map is missing or invalid. Never choose a later exit as a fallback.
 * No engine parser globals, edicts, VM state or filesystem state are changed. */
#ifndef LIVE_QUAKE_LEVEL_BYPASS_H
#define LIVE_QUAKE_LEVEL_BYPASS_H
#include <stddef.h>
#include <string.h>

#define LQ_LEVEL_MAP_MAX 48
#define LQ_LEVEL_ENTITIES_MAX (2u * 1024u * 1024u)
#define LQ_LEVEL_TOKEN_MAX 1023

/* Entity strings are NUL-terminated by both native model loaders. Bound the
 * scan anyway, so oversized text cannot monopolize a controller request. */
static int lq_level_token(const char **cursor,const char *end,char *out) {
    const char *p=*cursor;size_t used=0;
    for(;;) {
        while(p<end && (unsigned char)*p<=32)++p;
        if(end-p>=2 && p[0]=='/' && p[1]=='/') {
            p+=2;while(p<end && *p!='\n')++p;
        } else break;
    }
    if(p==end){*cursor=p;out[0]=0;return 0;}
    if(*p=='{' || *p=='}')out[used++]=*p++;
    else if(*p=='"') {
        ++p;
        while(p<end && *p!='"') {
            if(used==LQ_LEVEL_TOKEN_MAX)return -1;
            out[used++]=*p++;
        }
        if(p==end)return -1;
        ++p;
    } else {
        while(p<end && (unsigned char)*p>32 && *p!='{' && *p!='}') {
            if(used==LQ_LEVEL_TOKEN_MAX)return -1;
            out[used++]=*p++;
        }
    }
    out[used]=0;*cursor=p;return 1;
}

static int lq_level_nextmap(const char *entities,char *out,size_t size) {
    const char *p,*end;size_t length=0;
    char key[LQ_LEVEL_TOKEN_MAX+1],value[LQ_LEVEL_TOKEN_MAX+1];
    char classname[LQ_LEVEL_TOKEN_MAX+1],map[LQ_LEVEL_TOKEN_MAX+1];
    if(!out || !size)return 0;
    out[0]=0;
    if(!entities)return 0;
    while(length<LQ_LEVEL_ENTITIES_MAX && entities[length])++length;
    if(length==LQ_LEVEL_ENTITIES_MAX)return 0;
    p=entities;end=p+length;
    while(lq_level_token(&p,end,key)==1) {
        if(strcmp(key,"{"))return 0;
        classname[0]=map[0]=0;
        for(;;) {
            if(lq_level_token(&p,end,key)!=1)return 0;
            if(!strcmp(key,"}"))break;
            if(!strcmp(key,"{") || lq_level_token(&p,end,value)!=1 ||
               !strcmp(value,"{") || !strcmp(value,"}"))return 0;
            if(!strcmp(key,"classname"))memcpy(classname,value,strlen(value)+1);
            else if(!strcmp(key,"map"))memcpy(map,value,strlen(value)+1);
        }
        if(!strcmp(classname,"trigger_changelevel")) {
            size_t n=strlen(map);
            if(!n || n>LQ_LEVEL_MAP_MAX || n>=size ||
               strspn(map,"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")!=n)return 0;
            memcpy(out,map,n+1);return 1;
        }
    }
    return 0;
}
#endif
