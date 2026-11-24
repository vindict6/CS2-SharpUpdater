#!/usr/bin/env bash
# build_metamod.sh - build a Linux Metamod:Source for CS2 against the same SDK fixes as CounterStrikeSharp. By BONE.
#
# Metamod's own dev builds come from master but use plain s2sdk cs2, so right after a CS2 update they can lag the
# SDK fix. This builds MMS_REF (default master) for x86_64 Linux in AlliedModders' own build container, with the
# s2sdk pull requests from SDK_PRS applied (see apply_sdk_prs.py), and writes
# $OUT/mmsource-<version>-cs2-linux.tar.gz (extract into game/csgo/).
#
# Inputs (env): WORK, OUT, MMS_REPO (default upstream), MMS_REF (default master), SDK_PRS (default auto), GITHUB_TOKEN
set -euo pipefail

WORK="${WORK:?set WORK}"
OUT="${OUT:?set OUT}"
MMS_REPO="${MMS_REPO:-https://github.com/alliedmodders/metamod-source.git}"
MMS_REF="${MMS_REF:-master}"
SDK_PRS="${SDK_PRS:-auto}"
IMAGE="${MMS_IMAGE:-ghcr.io/alliedmodders/build-containers/debian11-clang22:latest}"
HERE="$(cd "$(dirname "$0")" && pwd)"
MMS_WORK="$WORK/metamod"

echo "==> [mm 1/3] clone metamod-source @ $MMS_REF"
rm -rf "$MMS_WORK"; mkdir -p "$MMS_WORK/deps"
git clone -q "$MMS_REPO" "$MMS_WORK/metamod-source"
( cd "$MMS_WORK/metamod-source" && git checkout -q "$MMS_REF" && git submodule update -q --init --recursive
  echo "    metamod-source at $(git rev-parse --short HEAD) ($(git log -1 --format=%s | cut -c1-70))" )

echo "==> [mm 2/3] build (Linux x86_64, $IMAGE)"
docker run --rm -v "$MMS_WORK:/w" -v "$HERE:/tools:ro" -e SDK_PRS="$SDK_PRS" -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" "$IMAGE" bash -euc '
  git config --global --add safe.directory "*"
  cd /w/deps
  mkdir -p metamod-source   # checkout-deps.sh wants a sibling folder by that name
  ../metamod-source/support/checkout-deps.sh -s cs2 >/dev/null
  GITHUB_OUTPUT= python3 /tools/apply_sdk_prs.py /w/deps/hl2sdk-cs2 "$SDK_PRS" | tee /w/sdk_prs.log
  cd /w/metamod-source
  rm -rf build && mkdir build && cd build
  python3 ../configure.py --enable-optimize --sdks=cs2 --targets=x86_64 --hl2sdk-root=/w/deps >/dev/null
  ambuild
'

echo "==> [mm 3/3] package"
PKG="$MMS_WORK/metamod-source/build/package"
[ -f "$PKG/addons/metamod/bin/linuxsteamrt64/metamod.2.cs2.so" ] || { echo "ERROR: metamod.2.cs2.so was not built"; exit 1; }
BASE="$(cat "$MMS_WORK/metamod-source/product.version")"; BASE="${BASE%-dev}"
REV="$(git -C "$MMS_WORK/metamod-source" rev-list --count HEAD)"
APPLIED="$(sed -n 's/^applied s2sdk #//p' "$MMS_WORK/sdk_prs.log" | paste -sd, -)"
NAME="mmsource-${BASE}-git${REV}-cs2-linux.tar.gz"
tar -C "$PKG" -czf "$OUT/$NAME" addons
echo "metamod-source $(git -C "$MMS_WORK/metamod-source" rev-parse HEAD) ${BASE}-git${REV}${APPLIED:+ (s2sdk cs2 + #${APPLIED//,/, #})}" > "$OUT/METAMOD"
echo "    wrote $OUT/$NAME"
