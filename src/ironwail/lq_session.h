/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef LQ_SESSION_H
#define LQ_SESSION_H
#include <stddef.h>
#include <string.h>

enum { LQ_SINGLE = 0, LQ_DEATHMATCH = 1 };

static inline int LQ_ParseMode(const char *text, int *mode)
{
    if (!strcmp(text, "single")) *mode = LQ_SINGLE;
    else if (!strcmp(text, "deathmatch")) *mode = LQ_DEATHMATCH;
    else return 0;
    return 1;
}

/* Host_Init allocates slots once. The later maxplayers command cannot grow
 * them. UDP stays disabled for every package, including expansions. */
static inline int LQ_SessionArguments(char **out, int expansion, int botpack)
{
    out[0] = "-noudp";
    if (expansion || !botpack) return 1;
    out[1] = "-listen";
    out[2] = "8";
    return 3;
}

#define LQ_SAVE_FOOTER "// live-quake-save-v2 "
#define LQ_SAVE_MAX_BYTES (16 * 1024 * 1024)

static inline int LQ_FooterNumber(char **cursor, int maximum, int *value)
{
    char *p = *cursor;
    int n = 0;
    if (*p < '0' || *p > '9') return 0;
    do {
        int digit = *p++ - '0';
        if (digit > maximum || n > (maximum - digit) / 10) return 0;
        n = n * 10 + digit;
    } while (*p >= '0' && *p <= '9');
    *cursor = p;
    *value = n;
    return 1;
}

/* Validate the terminal marker before any world or edict is restored. This
 * is framing and session compatibility, not a checksum of the world. */
static inline int LQ_SessionSaveFooter(char *text, size_t bytes, int slots,
    int mode, int max_edicts, int *count, const char **body_end)
{
    char *line, *p;
    int saved_slots, saved_count;
    const char *word = mode == LQ_DEATHMATCH ? "deathmatch " : "single ";
    if (bytes < 100 || bytes >= LQ_SAVE_MAX_BYTES ||
        memchr(text, 0, bytes) || text[bytes - 1] != '\n') return 0;
    line = text + bytes - 1;
    while (line > text && line[-1] != '\n') --line;
    if (line == text || strncmp(line, LQ_SAVE_FOOTER, sizeof(LQ_SAVE_FOOTER) - 1)) return 0;
    p = line + sizeof(LQ_SAVE_FOOTER) - 1;
    if (!LQ_FooterNumber(&p, 8, &saved_slots) || saved_slots != slots || *p++ != ' ') return 0;
    if (strncmp(p, word, strlen(word))) return 0;
    p += strlen(word);
    if (!LQ_FooterNumber(&p, max_edicts, &saved_count) ||
        saved_count < slots + 1 || p != text + bytes - 1) return 0;
    *count = saved_count;
    *body_end = line;
    *line = 0;
    return 1;
}
#endif
