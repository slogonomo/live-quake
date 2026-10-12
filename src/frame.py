# SPDX-License-Identifier: 0BSD
"""Bounded, nofollow, seqlock reads of the native framebuffer ABI."""
import ctypes
import os
from pathlib import Path
import stat
import struct
import time

MAGIC = 0x4c51554b
MOUSE_DELTA_MAX = 2048  # Pinned against LQ_MOUSE_DELTA_MAX in bridge.h by tests.
FILE_SIZE = 4096 + 3840 * 2160 * 4
U = ctypes.c_uint32
I = ctypes.c_int32

class Header(ctypes.Structure):
    _fields_ = [(x, U) for x in 'magic version seq width height stride bot paused audible gamestate'.split()] + [
        ('frame', ctypes.c_uint64), ('tic', ctypes.c_uint64)] + [(x, I) for x in
        'leveltime health x y kills items secrets'.split()] + [('map', ctypes.c_char * 16)] + [
        (x, U) for x in 'pid campaign audio_peak'.split()] + [('angle', I)] + [
        (x, U) for x in 'pixel_aspect_num pixel_aspect_den'.split()] + [
        (x, I) for x in 'fov view_width view_height hud_layout'.split()] + [
        (x, U) for x in 'bot_replans render_fps render_lerp'.split()] + [('render_angle', I)] + [
        (x, U) for x in 'human_quits owner agent_gen'.split()] + [('armor', I), ('ammo', I * 4),
        ('ready_weapon', I), ('weapons_owned', U), ('keys', U)] + [
        (x, I) for x in 'z momx momy damage_count'.split()] + [
        (x, U) for x in 'deaths levels_completed bot_stuck bot_nodes bot_visited'.split()] + [
        (x, I) for x in 'bot_goal bot_waypoint skill viewsize crosshair'.split()] + [('console_visible', U)] + [
        (x, U) for x in 'capture_supported capture_width capture_height capture_elapsed_us'.split()] + [
        ('capture_frame', ctypes.c_uint64)] + [(x, I) for x in 'followed_entity followed_bnum'.split()]

def read_frame(path, pixels=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size != FILE_SIZE:
            raise ValueError('Invalid owned frame file')
        for _ in range(12):
            raw = os.pread(fd, ctypes.sizeof(Header), 0)
            h = Header.from_buffer_copy(raw)
            if h.seq & 1:
                time.sleep(.001)
                continue
            if h.magic != MAGIC or h.version != 4 or not 1 <= h.width <= 3840 or not 1 <= h.height <= 2160 or h.stride != h.width * 4:
                raise ValueError('Invalid native frame header')
            data = os.pread(fd, h.stride * h.height, 4096) if pixels else None
            seq = struct.unpack('<I', os.pread(fd, 4, 8))[0]
            if seq == h.seq:
                result = {name: (getattr(h, name).decode('ascii', 'replace') if name == 'map'
                                  else list(h.ammo) if name == 'ammo' else getattr(h, name))
                          for name, _ in Header._fields_}
                return (result, data) if pixels else result
        raise BlockingIOError('Native frame is changing')
    finally:
        os.close(fd)
