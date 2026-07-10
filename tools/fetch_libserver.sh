#!/usr/bin/env bash
# fetch_libserver.sh - obtain OLD and NEW libserver.so for the RE diff.
#
# CS2 dedicated server = Steam app 730, depot 2347773. That depot needs a real
# Steam account (anonymous can't access it), hence STEAM_USER / STEAM_PASS.
# We download only one file: game/csgo/bin/linuxsteamrt64/libserver.so
#
# Two modes:
#   MODE=manifest  (TESTING)  - download two explicit historical builds by their
#                               depot manifest IDs. Deterministic, repeatable,
#                               needs no prior state. Use this to prove the
#                               pipeline works without waiting for a real update.
#       requires: OLD_MANIFEST, NEW_MANIFEST
#
#   MODE=baseline  (PRODUCTION) - download the CURRENT build; treat the previous
#                               run's binary (STATE_DIR/libserver.prev.so) as OLD.
#                               Detects updates by BuildID change.
#
# Outputs into $WORK: libserver.old.so, libserver.new.so
# Emits to $GITHUB_OUTPUT: UPDATE=(true|false), OLD_BUILDID, NEW_BUILDID [, SEEDED]
set -euo pipefail

WORK="${WORK:?set WORK (work dir)}"
STATE="${STATE:-$WORK/state}"
MODE="${MODE:-baseline}"
mkdir -p "$WORK" "$STATE"

APP=730
DEPOT=2347773
FILE="game/csgo/bin/linuxsteamrt64/libserver.so"
: "${STEAM_USER:?set STEAM_USER}"
: "${STEAM_PASS:?set STEAM_PASS}"

out() { echo "$1" >> "${GITHUB_OUTPUT:-/dev/stdout}"; }
buildid() { readelf -n "$1" 2>/dev/null | grep -oiE 'Build ID: [0-9a-f]+' | awk '{print $3}'; }

# ---- locate / install DepotDownloader ----
DD="${DEPOTDOWNLOADER:-}"
if [ -z "$DD" ]; then
  if command -v DepotDownloader >/dev/null 2>&1; then DD="DepotDownloader"
  elif [ -x "$STATE/depotdownloader/DepotDownloader" ]; then DD="$STATE/depotdownloader/DepotDownloader"
  else
    echo "[fetch] installing DepotDownloader (self-contained)..."
    mkdir -p "$STATE/depotdownloader"
    URL=$(curl -fsSL https://api.github.com/repos/SteamRE/DepotDownloader/releases/latest \
      | grep -oE 'https://[^"]*DepotDownloader-linux-x64\.zip' | head -1)
    [ -n "$URL" ] || { echo "ERROR: cannot find DepotDownloader release"; exit 1; }
    curl -fsSL "$URL" -o "$WORK/dd.zip"
    unzip -o "$WORK/dd.zip" -d "$STATE/depotdownloader" >/dev/null
    chmod +x "$STATE/depotdownloader/DepotDownloader" 2>/dev/null || true
    DD="$STATE/depotdownloader/DepotDownloader"
  fi
fi
echo "[fetch] DepotDownloader: $DD"
echo "$FILE" > "$WORK/filelist.txt"

# download one specific manifest into $1, copy the .so to $2
dl_manifest() {
  local manifest="$1" dest="$2" dir
  dir="$WORK/dl_${manifest}"
  rm -rf "$dir"; mkdir -p "$dir"
  echo "[fetch] downloading depot $DEPOT manifest $manifest ..."
  "$DD" -app "$APP" -depot "$DEPOT" -manifest "$manifest" \
    -username "$STEAM_USER" -password "$STEAM_PASS" -remember-password \
    -filelist "$WORK/filelist.txt" -dir "$dir" \
    || { echo "ERROR: DepotDownloader failed for manifest $manifest"; exit 1; }
  [ -f "$dir/$FILE" ] || { echo "ERROR: $FILE missing after download (manifest $manifest)"; exit 1; }
  cp "$dir/$FILE" "$dest"
}

# download the CURRENT build (latest manifest) into $1
dl_current() {
  local dest="$1" dir="$WORK/dl_current"
  rm -rf "$dir"; mkdir -p "$dir"
  echo "[fetch] downloading current depot $DEPOT ..."
  "$DD" -app "$APP" -depot "$DEPOT" \
    -username "$STEAM_USER" -password "$STEAM_PASS" -remember-password \
    -filelist "$WORK/filelist.txt" -dir "$dir" \
    || { echo "ERROR: DepotDownloader failed (current)"; exit 1; }
  [ -f "$dir/$FILE" ] || { echo "ERROR: $FILE missing after current download"; exit 1; }
  cp "$dir/$FILE" "$dest"
}

if [ "$MODE" = "manifest" ]; then
  : "${OLD_MANIFEST:?MODE=manifest requires OLD_MANIFEST}"
  : "${NEW_MANIFEST:?MODE=manifest requires NEW_MANIFEST}"
  dl_manifest "$OLD_MANIFEST" "$WORK/libserver.old.so"
  dl_manifest "$NEW_MANIFEST" "$WORK/libserver.new.so"
  OLD_ID=$(buildid "$WORK/libserver.old.so"); NEW_ID=$(buildid "$WORK/libserver.new.so")
  echo "[fetch] OLD manifest $OLD_MANIFEST BuildID=$OLD_ID | NEW manifest $NEW_MANIFEST BuildID=$NEW_ID"
  out "UPDATE=true"; out "OLD_BUILDID=$OLD_ID"; out "NEW_BUILDID=$NEW_ID"
  exit 0
fi

# ---- baseline mode ----
dl_current "$WORK/libserver.new.so"
NEW_ID=$(buildid "$WORK/libserver.new.so")
echo "[fetch] current BuildID=$NEW_ID"
if [ -f "$STATE/libserver.prev.so" ]; then
  cp "$STATE/libserver.prev.so" "$WORK/libserver.old.so"
  OLD_ID=$(buildid "$WORK/libserver.old.so")
  out "OLD_BUILDID=$OLD_ID"; out "NEW_BUILDID=$NEW_ID"
  if [ "$OLD_ID" = "$NEW_ID" ]; then
    echo "[fetch] no change since last run."; out "UPDATE=false"; exit 0
  fi
  echo "[fetch] update detected: $OLD_ID -> $NEW_ID"; out "UPDATE=true"
else
  echo "[fetch] no baseline yet; seeding and skipping recovery this run."
  cp "$WORK/libserver.new.so" "$STATE/libserver.prev.so"
  out "UPDATE=false"; out "SEEDED=true"; out "NEW_BUILDID=$NEW_ID"
fi
