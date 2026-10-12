// SPDX-License-Identifier: GPL-2.0-or-later
/* Scratch proof excludes store discovery and Steam API dynamic loading. */
#include "quakedef.h"
#include "steam.h"
qboolean Steam_IsValidPath(const char *path) {(void)path;return false;}
qboolean Steam_FindGame(steamgame_t *game,int appid) {(void)game;(void)appid;return false;}
qboolean Steam_ResolvePath(char *path,size_t size,const steamgame_t *game) {(void)path;(void)size;(void)game;return false;}
qboolean Steam_Init(const steamgame_t *game) {(void)game;return false;}
qboolean Steam_SetAchievement(const char *name) {(void)name;return false;}
void Steam_ClearStatus(void) {}
void Steam_SetStatus_Menu(void) {}
void Steam_SetStatus_SinglePlayer(const char *map) {(void)map;}
void Steam_SetStatus_Multiplayer(int players,int maximum,const char *map) {(void)players;(void)maximum;(void)map;}
qboolean Steam_SaveScreenshot(const void *rgb,int width,int height) {(void)rgb;(void)width;(void)height;return false;}
void Steam_Shutdown(void) {}
qboolean EGS_FindGame(char *path,size_t size,const char *space,const char *item,const char *app)
{(void)path;(void)size;(void)space;(void)item;(void)app;return false;}
quakeflavor_t ChooseQuakeFlavor(void) {return QUAKE_FLAVOR_ORIGINAL;}
