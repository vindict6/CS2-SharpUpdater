#!/usr/bin/env bash
# run_pipeline.sh - the heart of CS2-SharpUpdater.
#
# Given OLD/NEW libserver.so (already fetched), this:
#   1. clones the official CounterStrikeSharp at a chosen ref (submodules)
#   2. bumps libraries/hl2sdk-cs2 to the current cs2 branch head (native ABI fix)
#   3. bumps libraries/metamod-source to the current master head (plugin API;
#      Metamod:Source dev builds are cut straight from master)
#   4. runs the RE recovery -> writes an updated gamedata.json into the tree
#   5. builds native (Steam Runtime sniper SDK) + managed (.NET) via Docker
#   6. assembles a ready-to-deploy addons/ bundle (native + api + gamedata + runtime)
#      and zips it
#
# This is the "fixed" build/publish: no GitVersion dependency, TFM path globbed,
# fork-friendly (no repo-name gate, no NuGet/Discord), Linux-focused.
#
# Inputs (env):
#   OLD_SO, NEW_SO       paths to the two binaries
#   CSS_REF              git ref/tag/branch of roflmuffin/CounterStrikeSharp (default: main)
#   CSS_REPO             CSS git url (default: official)
#   SDK_REF              hl2sdk-cs2 commit/branch to pin (default: cs2 head)
#   MMS_REF              metamod-source commit/branch to pin (default: master head)
#   SKIP_BUILD           1 = stop after the RE recovery (no Docker needed); the
#                        recovered gamedata.json + report still land in $OUT
#   DOTNET_TAG           dotnet SDK image tag (default: 10.0)
#   ASPNET_RUNTIME       aspnetcore runtime version to bundle (default: 10.0.3)
#   BUNDLE_RUNTIME       1 = bundle the .NET runtime into the zip (default),
#                        0 = skip it (smaller zip; server must supply the runtime)
#   WORK                 work dir
#   OUT                  output dir for zips/report
set -euo pipefail

OLD_SO="${OLD_SO:?set OLD_SO}"
NEW_SO="${NEW_SO:?set NEW_SO}"
WORK="${WORK:?set WORK}"
OUT="${OUT:?set OUT}"
CSS_REPO="${CSS_REPO:-https://github.com/roflmuffin/CounterStrikeSharp.git}"
CSS_REF="${CSS_REF:-main}"
SDK_REF="${SDK_REF:-cs2}"
MMS_REF="${MMS_REF:-master}"
DOTNET_TAG="${DOTNET_TAG:-10.0}"
ASPNET_RUNTIME="${ASPNET_RUNTIME:-10.0.3}"
SNIPER="registry.gitlab.steamos.cloud/steamrt/sniper/sdk:latest"
HERE="$(cd "$(dirname "$0")" && pwd)"
GAMEDATA_REL="configs/addons/counterstrikesharp/gamedata/gamedata.json"

mkdir -p "$WORK" "$OUT"
CSS="$WORK/CounterStrikeSharp"

echo "==> [1/7] clone CounterStrikeSharp @ $CSS_REF"
rm -rf "$CSS"
git clone --depth 1 --branch "$CSS_REF" "$CSS_REPO" "$CSS" 2>/dev/null \
  || git clone "$CSS_REPO" "$CSS"     # fall back for a raw commit sha
( cd "$CSS" && git checkout -q "$CSS_REF" 2>/dev/null || true
  git submodule update --init --recursive --depth 1 )

echo "==> [2/7] bump hl2sdk-cs2 -> $SDK_REF"
( cd "$CSS/libraries/hl2sdk-cs2"
  git fetch --depth 1 origin "$SDK_REF"
  git checkout -q FETCH_HEAD
  git submodule update --init --recursive --depth 1
  echo "    hl2sdk-cs2 now at $(git rev-parse --short HEAD)" )

echo "==> [3/7] bump metamod-source -> $MMS_REF"
( cd "$CSS/libraries/metamod-source"
  git fetch --depth 1 origin "$MMS_REF"
  git checkout -q FETCH_HEAD
  git submodule update --init --recursive --depth 1    # khook, amtl, hl2sdk-manifests
  echo "    metamod-source now at $(git rev-parse --short HEAD)" )
PLAPI="$(grep -oE 'METAMOD_PLAPI_VERSION[[:space:]]+[0-9]+' \
           "$CSS/libraries/metamod-source/core/ISmmPluginExt.h" | grep -oE '[0-9]+$' || true)"
echo "    Metamod plugin API version: ${PLAPI:-unknown}"
# Plugin API 18 (Metamod:Source master from 2026-09-08, dev build ~1454+) dropped
# SourceHook for KHook and raised the minimum load version: a CSS tree that still
# compiles SourceHook cannot build against it, and a plugin built against API 17
# is refused by the new Metamod at load time. Catch that here instead of deep in
# the native build.
if [ "${PLAPI:-0}" -ge 18 ] && grep -q 'sourcehook/sourcehook.cpp' "$CSS/CMakeLists.txt"; then
  echo "ERROR: CounterStrikeSharp ref '$CSS_REF' still uses SourceHook, but metamod-source"
  echo "       '$MMS_REF' is plugin API $PLAPI (KHook only). Use a CSS ref that includes the"
  echo "       KHook migration (roflmuffin/CounterStrikeSharp#1418, main from 2026-09-23),"
  echo "       or set MMS_REF to a metamod-source commit older than 0cc4e200."
  exit 1
fi

echo "==> [4/7] RE recovery -> updated gamedata.json"
python3 "$HERE/cs2_update_gamedata.py" \
  --old "$OLD_SO" --new "$NEW_SO" \
  --gamedata "$CSS/$GAMEDATA_REL" \
  --out "$CSS/$GAMEDATA_REL.new" \
  --report "$OUT/recovery-report.json" || RC=$?
RC="${RC:-0}"
# driver exits 2 when some entries need review; that is not fatal for the build.
if [ ! -s "$CSS/$GAMEDATA_REL.new" ]; then
  echo "ERROR: recovery produced no gamedata"; exit 1
fi
mv "$CSS/$GAMEDATA_REL.new" "$CSS/$GAMEDATA_REL"
cp "$CSS/$GAMEDATA_REL" "$OUT/gamedata.json"
echo "    recovery rc=$RC (2 = some items flagged for review; see report)"

if [ "${SKIP_BUILD:-0}" = "1" ]; then
  echo "    SKIP_BUILD=1: stopping after recovery; gamedata.json + recovery-report.json are in $OUT"
  echo "PIPELINE_OK"
  exit 0
fi

echo "==> [5/7] native build (sniper SDK container)"
docker run --rm -v "$CSS:/src" -w /src "$SNIPER" bash -euc '
  git config --global --add safe.directory /src || true
  git config --global --add safe.directory "*" || true
  mkdir -p build && cd build
  cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
  cmake --build . --config Release -- -j"$(nproc)"
'
# CMake copies configs/ (incl. our recovered gamedata) into build/addons.
rm -rf "$WORK/dist"; mkdir -p "$WORK/dist"
cp -r "$CSS/build/addons" "$WORK/dist/addons"

echo "==> [6/7] managed build (.NET $DOTNET_TAG)"
docker run --rm -v "$CSS:/src" -w /src "mcr.microsoft.com/dotnet/sdk:${DOTNET_TAG}" bash -euc '
  # CSS.API.csproj runs `git describe` to stamp a version; the repo is owned by
  # the host user but this container runs as root, so mark it safe or git aborts
  # with "dubious ownership" (exit 128).
  git config --global --add safe.directory /src || true
  git config --global --add safe.directory "*" || true
  dotnet restore managed/CounterStrikeSharp.API/CounterStrikeSharp.API.csproj
  dotnet publish -c Release --no-restore managed/CounterStrikeSharp.API
'
mkdir -p "$WORK/dist/addons/counterstrikesharp/api"
# glob the TFM so this does not break when net10.0 -> net11.0 etc. (upstream bug)
if ls "$CSS"/managed/CounterStrikeSharp.API/bin/Release/*/publish >/dev/null 2>&1; then
  cp -r "$CSS"/managed/CounterStrikeSharp.API/bin/Release/*/publish/* "$WORK/dist/addons/counterstrikesharp/api/"
else
  cp -r "$CSS"/managed/CounterStrikeSharp.API/bin/Release/* "$WORK/dist/addons/counterstrikesharp/api/"
fi

echo "==> [7/7] bundle runtime + zip"
NEW_ID="$(python3 "$HERE/buildid.py" "$NEW_SO")"
VER="${CSS_REF}+cs2.${NEW_ID:0:8}.$(date -u +%Y%m%d)"
echo "$VER" > "$OUT/VERSION"

if [ "${BUNDLE_RUNTIME:-1}" = "1" ]; then
  echo "    bundling .NET runtime (aspnetcore ${ASPNET_RUNTIME})"
  mkdir -p "$WORK/dist/addons/counterstrikesharp/dotnet"
  curl -sSL "https://builds.dotnet.microsoft.com/dotnet/aspnetcore/Runtime/${ASPNET_RUNTIME}/aspnetcore-runtime-${ASPNET_RUNTIME}-linux-x64.tar.gz" \
    | tar xz -C "$WORK/dist/addons/counterstrikesharp/dotnet"
  ZIP="$OUT/counterstrikesharp-with-runtime-linux-${VER}.zip"
else
  echo "    skipping .NET runtime (BUNDLE_RUNTIME=0) - server must have the runtime installed"
  ZIP="$OUT/counterstrikesharp-linux-${VER}.zip"
fi

if command -v zip >/dev/null; then
  ( cd "$WORK/dist" && zip -qq -r "$ZIP" addons )
else
  python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1][:-4], 'zip', root_dir=sys.argv[2], base_dir='addons')" "$ZIP" "$WORK/dist"
fi
echo "    wrote $ZIP"
echo "    version $VER"
echo "PIPELINE_OK"
