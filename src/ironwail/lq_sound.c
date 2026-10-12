// SPDX-License-Identifier: GPL-2.0-or-later
/* Deterministic virtual DMA. Ironwail mixes on the simulation thread; the
 * frontend alone decides whether PCM is discarded or sent to its audio sink.
 * No SDL audio subsystem, ambient device or wall-clock sound advancement. */
#include "quakedef.h"
#include <stdint.h>
#define LQ_SOUND_RATE 44100u
#define LQ_SOUND_FPS 72u
#define LQ_SOUND_SAMPLES 32768u
static dma_t *device;
static uint64_t consumed;
static unsigned fractional, block, frame_start, frame_count;
static int16_t output[(LQ_SOUND_RATE/LQ_SOUND_FPS+1)*2];
extern void lq_retro_audio_frames(const int16_t *, size_t);

qboolean SNDDMA_Init(dma_t *dma)
{
    memset(dma, 0, sizeof(*dma));
    dma->channels=2;dma->samples=LQ_SOUND_SAMPLES;dma->samplebits=16;
    dma->submission_chunk=1;dma->speed=LQ_SOUND_RATE;
    dma->buffer=calloc(LQ_SOUND_SAMPLES, sizeof(int16_t));
    if(!dma->buffer)return false;
    device=dma;shm=dma;consumed=fractional=block=frame_start=frame_count=0;
    return true;
}
int SNDDMA_GetDMAPos(void) {return device?device->samplepos:0;}
void SNDDMA_Shutdown(void)
{
    if(device){free(device->buffer);device->buffer=NULL;device=NULL;shm=NULL;}
    frame_count=0;
}
void SNDDMA_LockBuffer(void) {}
void SNDDMA_Submit(void) {}
void SNDDMA_BlockSound(void) {block=1;}
void SNDDMA_UnblockSound(void) {block=0;}

/* Called exactly once before Host_Frame(1/72), never during external pause.
 * Fractional carry yields exactly 44100 stereo frames per72 simulation calls. */
void LQ_SoundAdvance(void)
{
    frame_count=0;
    if(!device || block)return;
    frame_start=(unsigned)(consumed%(LQ_SOUND_SAMPLES/2));
    fractional+=LQ_SOUND_RATE;
    frame_count=fractional/LQ_SOUND_FPS;
    fractional%=LQ_SOUND_FPS;
    consumed+=frame_count;
    device->samplepos=(int)((consumed%(LQ_SOUND_SAMPLES/2))*2);
}
/* Emit the newly consumed segment after the mixer has filled its ring.
 * Final gate and queue flush are owned by the frontend, not volume cvars. */
void LQ_SoundEmit(void)
{
    if(!device || !frame_count)return;
    const int16_t *ring=(const int16_t *)device->buffer;
    for(unsigned i=0;i<frame_count;i++){
        unsigned index=(frame_start+i)%(LQ_SOUND_SAMPLES/2);
        output[i*2]=block?0:ring[index*2];
        output[i*2+1]=block?0:ring[index*2+1];
    }
    lq_retro_audio_frames(output,frame_count);
    frame_count=0;
}
