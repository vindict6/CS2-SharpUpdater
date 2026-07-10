#!/usr/bin/env bash
# run_pipeline.sh - the heart of CS2-SharpUpdater.
#
# Given OLD/NEW libserver.so (already fetched), this:
#   1. clones the official CounterStrikeSharp at a chosen ref (submodules)
#   2. bumps libraries/hl2sdk-cs2 to the current cs2 branch head (native ABI fix)
#   3. runs the RE recovery -> writes an updated gamedata.json into the tree
#   4. builds native (Steam Runtime sniper SDK) + managed (.NET) via Docker
#   5. assembles a ready-to-deploy addons/ bundle (native + api + gamedata + runtime)
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
#   DOTNET_TAG           dotnet SDK image tag (default: 10.0)
#   ASPNET_RUNTIME       aspnetcore runtime version to bundle (default: 10.0.3)
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
DOTNET_TAG="${DOTNET_TAG:-10.0}"
ASPNET_RUNTIME="${ASPNET_RUNTIME:-10.0.3}"
SNIPER="registry.gitlab.steamos.cloud/steamrt/sniper/sdk:latest"
HERE="$(cd "$(dirname "$0")" && pwd)"
GAMEDATA_REL="configs/addons/counterstrikesharp/gamedata/gamedata.json"

mkdir -p "$WORK" "$OUT"
CSS="$WORK/CounterStrikeSharp"

echo "==> [1/6] clone CounterStrikeSharp @ $CSS_REF"
rm -rf "$CSS"
git clone --depth 1 --branch "$CSS_REF" "$CSS_REPO" "$CSS" 2>/dev/null \
  || git clone "$CSS_REPO" "$CSS"     # fall back for a raw commit sha
( cd "$CSS" && git checkout -q "$CSS_REF" 2>/dev/null || true
  git submodule update --init --recursive --depth 1 )

echo "==> [2/6] bump hl2sdk-cs2 -> $SDK_REF"
( cd "$CSS/libraries/hl2sdk-cs2"
  git fetch --depth 1 origin "$SDK_REF"
  git checkout -q FETCH_HEAD
  git submodule update --init --recursive --depth 1
  echo "    hl2sdk-cs2 now at $(git rev-parse --short HEAD)" )

echo "==> [3/6] RE recovery -> updated gamedata.json"
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

echo "==> [4/6] native build (sniper SDK container)"
docker run --rm -v "$CSS:/src" -w /src "$SNIPER" bash -euc '
  git config --global --add safe.directory /src || true
  mkdir -p build && cd build
  cmake -G Ninja -DCMAKE_BUILD_TYPE=Release ..
  cmake --build . --config Release -- -j"$(nproc)"
'
# CMake copies configs/ (incl. our recovered gamedata) into build/addons.
rm -rf "$WORK/dist"; mkdir -p "$WORK/dist"
cp -r "$CSS/build/addons" "$WORK/dist/addons"

echo "==> [5/6] managed build (.NET $DOTNET_TAG)"
docker run --rm -v "$CSS:/src" -w /src "mcr.microsoft.com/dotnet/sdk:${DOTNET_TAG}" bash -euc '
  dotnet restore managed/CounterStrikeSharp.sln
  dotnet publish -c Release managed/CounterStrikeSharp.API
'
mkdir -p "$WORK/dist/addons/counterstrikesharp/api"
# glob the TFM so this does not break when net10.0 -> net11.0 etc. (upstream bug)
if ls "$CSS"/managed/CounterStrikeSharp.API/bin/Release/*/publish >/dev/null 2>&1; then
  cp -r "$CSS"/managed/CounterStrikeSharp.API/bin/Release/*/publish/* "$WORK/dist/addons/counterstrikesharp/api/"
else
  cp -r "$CSS"/managed/CounterStrikeSharp.API/bin/Release/* "$WORK/dist/addons/counterstrikesharp/api/"
fi

echo "==> [6/6] bundle runtime + zip"
mkdir -p "$WORK/dist/addons/counterstrikesharp/dotnet"
curl -sSL "https://builds.dotnet.microsoft.com/dotnet/aspnetcore/Runtime/${ASPNET_RUNTIME}/aspnetcore-runtime-${ASPNET_RUNTIME}-linux-x64.tar.gz" \
  | tar xz -C "$WORK/dist/addons/counterstrikesharp/dotnet"

NEW_ID="$(readelf -n "$NEW_SO" 2>/dev/null | grep -oiE 'Build ID: [0-9a-f]+' | awk '{print $3}')"
VER="${CSS_REF}+cs2.${NEW_ID:0:8}.$(date -u +%Y%m%d)"
echo "$VER" > "$OUT/VERSION"
ZIP="$OUT/counterstrikesharp-with-runtime-linux-${VER}.zip"
( cd "$WORK/dist" && zip -qq -r "$ZIP" addons )
echo "    wrote $ZIP"
echo "    version $VER"
echo "PIPELINE_OK"
