/* SPDX-License-Identifier: 0BSD */
#define _GNU_SOURCE
#include "gpu_transport.h"
#include <errno.h>
#include <stddef.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define MAGIC UINT32_C(0x5047514c)
#define VERSION 1u
static void put32(unsigned char *p, uint32_t v) {
    for (unsigned i=0; i<4; ++i) p[i]=(unsigned char)(v>>(8*i));
}
static void put64(unsigned char *p, uint64_t v) {
    for (unsigned i=0; i<8; ++i) p[i]=(unsigned char)(v>>(8*i));
}
static uint32_t get32(const unsigned char *p) {
    uint32_t v=0; for (unsigned i=0; i<4; ++i) v|=(uint32_t)p[i]<<(8*i); return v;
}
static uint64_t get64(const unsigned char *p) {
    uint64_t v=0; for (unsigned i=0; i<8; ++i) v|=(uint64_t)p[i]<<(8*i); return v;
}
static bool zero_bytes(const unsigned char *p, size_t n) {
    for (size_t i=0;i<n;++i) if (p[i]) return false;
    return true;
}
static int socket_valid(int fd) {
    int type=0; socklen_t length=sizeof(type);
    struct sockaddr_storage address; socklen_t address_length=sizeof(address);
    if (getsockopt(fd,SOL_SOCKET,SO_TYPE,&type,&length)<0 ||
        getsockname(fd,(struct sockaddr *)&address,&address_length)<0) return -1;
    if (type!=SOCK_SEQPACKET || address.ss_family!=AF_UNIX) {errno=EPROTOTYPE;return -1;}
    return 0;
}
bool lq_gpu_frame_valid(const struct lq_gpu_frame *f) {
    if (!f || !f->generation || !f->serial || f->slot>=LQ_GPU_SLOTS ||
        f->width<32 || f->width>4096 || f->height<32 || f->height>4096 ||
        (f->format!=LQ_GPU_FORMAT_XRGB8888 && f->format!=LQ_GPU_FORMAT_ARGB8888) ||
        (f->flags&~LQ_GPU_FLAG_Y_INVERT) ||
        !f->par_num || f->par_num>65535 || !f->par_den || f->par_den>65535 ||
        (uint64_t)f->par_num*4<f->par_den || (uint64_t)f->par_den*4<f->par_num ||
        !f->plane_count || f->plane_count>LQ_GPU_PLANES) return false;
    for (unsigned i=0;i<LQ_GPU_PLANES;++i) {
        if (i<f->plane_count) {
            /* Modifiers can add auxiliary planes. Do not infer their layouts.
             * Bounds limit the metadata; the compositor validates actual BOs. */
            if (f->fd[i]<0 || !f->stride[i] || f->stride[i]>(1u<<24) ||
                f->offset[i]>(1u<<30)) return false;
        } else if (f->offset[i] || f->stride[i] || f->fd[i]!=-1) return false;
    }
    if (f->modifier==0 && (f->plane_count!=1 || f->stride[0]<f->width*4)) return false;
    return true;
}
static bool release_valid(const struct lq_gpu_release *r) {
    return r && r->generation && r->serial && r->slot<LQ_GPU_SLOTS;
}
static bool capability_valid(const struct lq_gpu_capability *c) {
    return c && c->generation && c->index && c->index<=LQ_GPU_CAPABILITY_LIMIT &&
        c->modifier!=LQ_GPU_MODIFIER_INVALID &&
        (c->format==LQ_GPU_FORMAT_XRGB8888 || c->format==LQ_GPU_FORMAT_ARGB8888);
}
static bool done_valid(const struct lq_gpu_capabilities_done *done) {
    return done && done->generation && done->count<=LQ_GPU_CAPABILITY_LIMIT;
}
void lq_gpu_close_frame(struct lq_gpu_frame *frame) {
    if (!frame) return;
    for (unsigned i=0;i<LQ_GPU_PLANES;++i) { if(frame->fd[i]>=0) close(frame->fd[i]); frame->fd[i]=-1; }
    frame->plane_count=0;
}
static int send_packet(int fd, unsigned char *wire, const int *fds, unsigned count) {
    if(socket_valid(fd)<0)return -1;
    struct iovec iov={.iov_base=wire,.iov_len=LQ_GPU_WIRE_BYTES};
    union { struct cmsghdr alignment; unsigned char bytes[CMSG_SPACE(sizeof(int)*LQ_GPU_PLANES)]; } control;
    memset(&control,0,sizeof(control));
    struct msghdr message={.msg_iov=&iov,.msg_iovlen=1};
    if(count) {
        message.msg_control=control.bytes;message.msg_controllen=CMSG_SPACE(sizeof(int)*count);
        struct cmsghdr *cmsg=CMSG_FIRSTHDR(&message);
        cmsg->cmsg_level=SOL_SOCKET;cmsg->cmsg_type=SCM_RIGHTS;cmsg->cmsg_len=CMSG_LEN(sizeof(int)*count);
        memcpy(CMSG_DATA(cmsg),fds,sizeof(int)*count);
    }
    ssize_t sent=sendmsg(fd,&message,MSG_DONTWAIT|MSG_NOSIGNAL);
    if(sent<0)return (errno==EAGAIN || errno==EWOULDBLOCK)?0:-1;
    if(sent!=LQ_GPU_WIRE_BYTES){errno=EIO;return -1;}
    return 1;
}
static void prefix(unsigned char *wire, enum lq_gpu_kind kind, uint64_t generation,
                   uint64_t serial,uint32_t slot) {
    memset(wire,0,LQ_GPU_WIRE_BYTES);put32(wire,MAGIC);put32(wire+4,VERSION);
    put32(wire+8,(uint32_t)kind);put32(wire+12,LQ_GPU_WIRE_BYTES);
    put64(wire+16,generation);put64(wire+24,serial);put32(wire+32,slot);
}
int lq_gpu_send_frame(int socket_fd,const struct lq_gpu_frame *f) {
    if(!lq_gpu_frame_valid(f)){errno=EINVAL;return -1;}
    unsigned char wire[LQ_GPU_WIRE_BYTES];prefix(wire,LQ_GPU_FRAME,f->generation,f->serial,f->slot);
    put32(wire+36,f->width);put32(wire+40,f->height);put32(wire+44,f->format);
    put32(wire+48,f->flags);put32(wire+52,f->par_num);put32(wire+56,f->par_den);
    put32(wire+60,f->plane_count);put64(wire+64,f->modifier);
    for(unsigned i=0;i<LQ_GPU_PLANES;++i){put32(wire+72+i*8,f->offset[i]);put32(wire+76+i*8,f->stride[i]);}
    return send_packet(socket_fd,wire,f->fd,f->plane_count);
}
int lq_gpu_send_release(int socket_fd,const struct lq_gpu_release *r) {
    if(!release_valid(r)){errno=EINVAL;return -1;}
    unsigned char wire[LQ_GPU_WIRE_BYTES];prefix(wire,LQ_GPU_RELEASE,r->generation,r->serial,r->slot);
    return send_packet(socket_fd,wire,NULL,0);
}
int lq_gpu_send_capability(int socket_fd,const struct lq_gpu_capability *c) {
    if(!capability_valid(c)){errno=EINVAL;return -1;}
    unsigned char wire[LQ_GPU_WIRE_BYTES];prefix(wire,LQ_GPU_CAPABILITY,c->generation,c->index,0);
    put32(wire+36,c->format);put64(wire+40,c->modifier);return send_packet(socket_fd,wire,NULL,0);
}
int lq_gpu_send_capabilities_done(int socket_fd,const struct lq_gpu_capabilities_done *done) {
    if(!done_valid(done)){errno=EINVAL;return -1;}
    unsigned char wire[LQ_GPU_WIRE_BYTES];prefix(wire,LQ_GPU_CAPABILITIES_DONE,done->generation,0,0);
    put32(wire+36,done->count);return send_packet(socket_fd,wire,NULL,0);
}
int lq_gpu_receive(int socket_fd,struct lq_gpu_packet *packet) {
    if(!packet){errno=EINVAL;return -1;}
    memset(packet,0,sizeof(*packet));for(unsigned i=0;i<LQ_GPU_PLANES;++i)packet->frame.fd[i]=-1;
    if(socket_valid(socket_fd)<0)return -1;
    unsigned char wire[LQ_GPU_WIRE_BYTES];
    struct iovec iov={.iov_base=wire,.iov_len=sizeof(wire)};
    /* Room for extra FDs allows explicit closure. MSG_CTRUNC rejects any larger
     * ancillary data; Linux closes SCM_RIGHTS descriptors beyond this buffer. */
    union { struct cmsghdr alignment; unsigned char bytes[CMSG_SPACE(sizeof(int)*16)]; } control;
    memset(&control,0,sizeof(control));
    struct msghdr message={.msg_iov=&iov,.msg_iovlen=1,.msg_control=control.bytes,.msg_controllen=sizeof(control)};
    ssize_t received=recvmsg(socket_fd,&message,MSG_DONTWAIT|MSG_CMSG_CLOEXEC);
    if(received<0)return (errno==EAGAIN || errno==EWOULDBLOCK)?0:-1;
    bool bad=(received!=LQ_GPU_WIRE_BYTES || (message.msg_flags&(MSG_TRUNC|MSG_CTRUNC)));
    int fds[16];unsigned count=0;unsigned rights=0;
    for(struct cmsghdr *c=CMSG_FIRSTHDR(&message);c;c=CMSG_NXTHDR(&message,c)) {
        if(c->cmsg_level!=SOL_SOCKET || c->cmsg_type!=SCM_RIGHTS){bad=true;continue;}
        ++rights;
        if(c->cmsg_len<CMSG_LEN(0)){bad=true;continue;}
        size_t bytes=c->cmsg_len-CMSG_LEN(0);if(bytes%sizeof(int))bad=true;
        unsigned n=(unsigned)(bytes/sizeof(int));
        for(unsigned i=0;i<n;++i) {
            int received_fd;memcpy(&received_fd,CMSG_DATA(c)+i*sizeof(int),sizeof(int));
            if(count<16)fds[count++]=received_fd;else{close(received_fd);bad=true;}
        }
    }
    if(rights>1)bad=true;
    if(!bad && (get32(wire)!=MAGIC || get32(wire+4)!=VERSION || get32(wire+12)!=LQ_GPU_WIRE_BYTES))bad=true;
    if(!bad) {
        packet->kind=(enum lq_gpu_kind)get32(wire+8);
        if(packet->kind==LQ_GPU_FRAME) {
            struct lq_gpu_frame *f=&packet->frame;
            f->generation=get64(wire+16);f->serial=get64(wire+24);f->slot=get32(wire+32);
            f->width=get32(wire+36);f->height=get32(wire+40);f->format=get32(wire+44);
            f->flags=get32(wire+48);f->par_num=get32(wire+52);f->par_den=get32(wire+56);
            f->plane_count=get32(wire+60);f->modifier=get64(wire+64);
            for(unsigned i=0;i<LQ_GPU_PLANES;++i){f->offset[i]=get32(wire+72+i*8);f->stride[i]=get32(wire+76+i*8);}
            if(count!=f->plane_count || count>LQ_GPU_PLANES || !zero_bytes(wire+104,56))bad=true;
            else {
                for(unsigned i=0;i<count;++i)f->fd[i]=fds[i];
                if(!lq_gpu_frame_valid(f))bad=true;
            }
        } else if(packet->kind==LQ_GPU_RELEASE) {
            packet->release=(struct lq_gpu_release){.generation=get64(wire+16),.serial=get64(wire+24),.slot=get32(wire+32)};
            if(count || !zero_bytes(wire+36,124) || !release_valid(&packet->release))bad=true;
        } else if(packet->kind==LQ_GPU_CAPABILITY) {
            packet->capability=(struct lq_gpu_capability){.generation=get64(wire+16),.index=(uint32_t)get64(wire+24),
                .format=get32(wire+36),.modifier=get64(wire+40)};
            if(count || get64(wire+24)>LQ_GPU_CAPABILITY_LIMIT || get32(wire+32) ||
               !zero_bytes(wire+48,112) || !capability_valid(&packet->capability))bad=true;
        } else if(packet->kind==LQ_GPU_CAPABILITIES_DONE) {
            packet->capabilities_done=(struct lq_gpu_capabilities_done){.generation=get64(wire+16),.count=get32(wire+36)};
            if(count || get64(wire+24) || get32(wire+32) || !zero_bytes(wire+40,120) ||
               !done_valid(&packet->capabilities_done))bad=true;
        } else bad=true;
    }
    if(bad) {
        for(unsigned i=0;i<count;++i)close(fds[i]);
        memset(packet,0,sizeof(*packet));for(unsigned i=0;i<LQ_GPU_PLANES;++i)packet->frame.fd[i]=-1;
        errno=received==0?ECONNRESET:EPROTO;return -1;
    }
    return 1;
}
static int capabilities_fail(struct lq_gpu_capabilities *c,int error) {c->failed=true;errno=error;return -1;}
int lq_gpu_capabilities_accept(struct lq_gpu_capabilities *c,const struct lq_gpu_packet *packet) {
    if(!c || !packet){errno=EINVAL;return -1;}
    if(c->failed || c->done || c->count>LQ_GPU_CAPABILITY_LIMIT)return capabilities_fail(c,EPROTO);
    if(packet->kind==LQ_GPU_CAPABILITY) {
        const struct lq_gpu_capability *r=&packet->capability;
        if(!capability_valid(r) || c->count==LQ_GPU_CAPABILITY_LIMIT || r->index!=c->count+1 ||
           (c->generation && r->generation!=c->generation))return capabilities_fail(c,EPROTO);
        for(uint32_t i=0;i<c->count;++i)if(c->records[i].format==r->format && c->records[i].modifier==r->modifier)
            return capabilities_fail(c,EPROTO);
        c->generation=r->generation;c->records[c->count++]=*r;return 0;
    }
    if(packet->kind==LQ_GPU_CAPABILITIES_DONE) {
        const struct lq_gpu_capabilities_done *done=&packet->capabilities_done;
        if(!done_valid(done) || done->count!=c->count || (c->generation && done->generation!=c->generation))
            return capabilities_fail(c,EPROTO);
        c->generation=done->generation;
        if(!c->count)return capabilities_fail(c,ENOTSUP);
        c->done=true;return 1;
    }
    return capabilities_fail(c,EPROTO);
}
int lq_gpu_capabilities_receive(int socket_fd,struct lq_gpu_capabilities *c) {
    if(!c || c->failed || c->count>LQ_GPU_CAPABILITY_LIMIT){errno=EINVAL;return -1;}if(c->done)return 1;
    for(uint32_t i=0;i<=LQ_GPU_CAPABILITY_LIMIT;++i) {
        struct lq_gpu_packet packet;int got=lq_gpu_receive(socket_fd,&packet);
        if(!got)return 0;
        if(got<0)return capabilities_fail(c,errno);
        int accepted=lq_gpu_capabilities_accept(c,&packet);
        if(packet.kind==LQ_GPU_FRAME)lq_gpu_close_frame(&packet.frame);
        if(accepted)return accepted;
    }
    return capabilities_fail(c,EPROTO);
}
uint32_t lq_gpu_capabilities_modifiers(const struct lq_gpu_capabilities *c,uint32_t format,uint64_t *modifiers,uint32_t capacity) {
    if(!c || !c->done || c->failed || c->count>LQ_GPU_CAPABILITY_LIMIT || !modifiers)return 0;
    uint32_t count=0;
    for(uint32_t i=0;i<c->count;++i)if(c->records[i].format==format) {
        if(count==capacity)return 0;
        modifiers[count++]=c->records[i].modifier;
    }
    return count;
}
int lq_gpu_leases_init(struct lq_gpu_leases *leases,uint64_t generation) {
    if(!leases || !generation){errno=EINVAL;return -1;}
    /* Initialize only a fresh or fully released nonretired pool. */
    if(leases->in_flight || leases->retired){errno=EBUSY;return -1;}
    if(leases->generation==generation){errno=EALREADY;return -1;}
    memset(leases,0,sizeof(*leases));leases->generation=generation;return 0;
}
bool lq_gpu_leases_can_acquire(const struct lq_gpu_leases *l,uint32_t slot) {
    return l && l->generation && !l->retired && slot<LQ_GPU_SLOTS &&
           !l->serial[slot] && l->in_flight<LQ_GPU_MAX_IN_FLIGHT;
}
int lq_gpu_leases_acquire(struct lq_gpu_leases *l,uint32_t slot,uint64_t serial) {
    if(!lq_gpu_leases_can_acquire(l,slot) || !serial || serial<=l->last_serial){errno=EINVAL;return -1;}
    l->serial[slot]=serial;l->last_serial=serial;++l->in_flight;return 0;
}
int lq_gpu_leases_release(struct lq_gpu_leases *l,const struct lq_gpu_release *r) {
    if(!l || !release_valid(r) || l->retired || r->generation!=l->generation || l->serial[r->slot]!=r->serial){errno=EINVAL;return -1;}
    l->serial[r->slot]=0;--l->in_flight;return 0;
}
void lq_gpu_leases_retire(struct lq_gpu_leases *l) {if(l)l->retired=true;}
