#!/usr/bin/bash
# SPDX-License-Identifier: 0BSD
# Build only; never install or connect to a desktop. Prints the binary path.
set -euo pipefail
umask 077
root=$(cd -- "$(/usr/bin/dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build=
if [[ $# == 2 && $1 == --build-dir ]]; then
  build=$2
elif [[ $# != 0 ]]; then
  echo 'Usage: scripts/build-viewer.sh [--build-dir PRIVATE_DIRECTORY]' >&2
  exit 2
fi
if [[ -z $build ]]; then
  build=$(/usr/bin/mktemp -d /tmp/live-quake-dev.XXXXXX)
fi
build=$(/usr/bin/python3 -E -s -B - "$build" <<'PY'
import os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
if not path.is_absolute() or path.resolve() != path:
    raise SystemExit('Build directory must be absolute and canonical')
if path.is_symlink() or not path.is_dir():
    raise SystemExit('Build directory must be an existing private directory')
info = path.stat()
if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit('Build directory must be owned by this user with mode0700')
print(path)
PY
)
protocols="$build/viewer-protocols"
if [[ -L $protocols || -L $build/quake-viewer ]]; then
  echo 'Refusing symlink build destinations' >&2
  exit 1
fi
/usr/bin/mkdir -p -- "$protocols"
for protocol in xdg-shell xdg-output-unstable-v1 wlr-layer-shell-unstable-v1 relative-pointer-unstable-v1 pointer-constraints-unstable-v1 linux-dmabuf-v1 viewporter; do
  if [[ -L $protocols/$protocol-client-protocol.h || -L $protocols/$protocol-protocol.c ]]; then
    echo 'Refusing symlink protocol output' >&2
    exit 1
  fi
  /usr/bin/wayland-scanner client-header "$root/src/protocols/$protocol.xml" "$protocols/$protocol-client-protocol.h"
  /usr/bin/wayland-scanner private-code "$root/src/protocols/$protocol.xml" "$protocols/$protocol-protocol.c"
done
temporary=$(/usr/bin/mktemp "$build/.quake-viewer-XXXXXX")
trap '/usr/bin/rm -f -- "$temporary"' EXIT
/usr/bin/cc -std=c11 -O2 -g -Wall -Wextra -Werror -Wpedantic \
  $(/usr/bin/pkg-config --cflags wayland-client wayland-cursor xkbcommon cairo) \
  -I"$root/src" -I"$protocols" "$root/src/viewer.c" "$root/src/gpu_wayland.c" "$root/src/gpu_transport.c" "$protocols"/*-protocol.c \
  $(/usr/bin/pkg-config --libs wayland-client wayland-cursor xkbcommon cairo) -lm -o "$temporary"
/usr/bin/chmod 700 -- "$temporary"
/usr/bin/mv -f -- "$temporary" "$build/quake-viewer"
printf '%s\n' "$build/quake-viewer"
