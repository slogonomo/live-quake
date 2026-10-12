// SPDX-License-Identifier: GPL-2.0-or-later
#ifndef LQ_LIBRETRO_H
#define LQ_LIBRETRO_H
#include <stdint.h>
#include <stddef.h>
/* Constructor-supplied frontend input only; never polls an ambient device. */
int16_t lq_retro_input(unsigned port, unsigned device, unsigned index, unsigned id);
uint64_t lq_retro_frame_count(void);
void lq_retro_audio_frames(const int16_t *samples, size_t frames);
int LQ_SessionMode(void);
void LQ_NewDeathmatchGame(void);
#endif
