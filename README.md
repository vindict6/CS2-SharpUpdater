# CS2-SharpUpdater

**Keep your CounterStrikeSharp server working the moment Counter-Strike 2 updates —
automatically.**

When Valve ships a CS2 update it breaks server mods in two ways at once, and this
repo fixes both without you touching a disassembler:

1. **Signatures & offsets** in CounterStrikeSharp's `gamedata.json` stop matching,
   because Valve recompiled the game's functions. This tool reverse-engineers the
   new game binary and recovers them.
2. **The native plugin won't load** (`undefined symbol: …`), because the engine's
   exported symbols changed. This tool bumps CounterStrikeSharp's `hl2sdk-cs2`
   submodule to the current version so the plugin links again.

Then it **builds CounterStrikeSharp for you** and publishes a ready-to-deploy zip
as a GitHub Release. Fork this repo, add two secrets, attach a runner, and you get
a fresh working build after every CS2 update.

---

## How it works (the short version)

On a schedule (or on demand) the `update` workflow:

1. **Fetches** the current `libserver.so` from the CS2 dedicated-server depot, and
   uses the previous run's binary as the "before" copy.
2. If the game changed, **clones official CounterStrikeSharp** at a ref you choose.
3. **Bumps `hl2sdk-cs2`** to the current `cs2` branch head (fixes the native ABI).
4. **Reverse-engineers the gamedata**: for every broken signature it re-locates the
   function in the new binary using three mutually-confirming signals — the strings
   the function references, its control-flow-graph fingerprint (block/edge/mnemonic
   shape), and its call graph — then cuts a fresh, verified-unique byte signature.
   For vtable offsets it reads the class vtable out of both binaries via RTTI,
   matches the function, and reads off its new index. Anything it can't recover
   with confidence is **flagged, never guessed**.
5. **Builds** the native plugin (in Valve's Steam Runtime SDK) and the .NET managed
   layer, and assembles a complete `addons/` bundle including the recovered gamedata
   and the .NET runtime.
6. **Publishes** it as a GitHub Release. Download, unzip into your server, done.

Everything the RE produces is checked statically (every signature is confirmed to
match exactly one address; every offset is backed by a high-confidence function
match). Static checks are not the same as a live server, so the release is meant to
be dropped on a **test server first** — but in practice the recovery is accurate.

### What it can and can't recover automatically

- **Recovers:** broken linux signatures (including recompiled prologues and tiny
  stringless wrapper functions), and vtable-index offsets that shifted.
- **Flags for you (rare):** struct *field* offsets (not vtable indices — they need
  the SDK headers) and any match below its confidence bar. These show up in
  `recovery-report.json`.
- **Depends on upstream for:** the native C++ side. The SDK bump fixes the ABI, but
  if a game update also needs CounterStrikeSharp *source* changes (occasionally it
  does), those come from the CSS maintainers. If the build fails to compile after a
  bump, that's the signal to wait for (or merge) an upstream fix.
- **Linux only.** Windows signatures/offsets are not touched.

---

## Setup for non-experts

You need: a GitHub account, a **Steam account that owns CS2** (a cheap throwaway is
fine — but it must not require the phone-app Steam Guard prompt, or automation will
hang; email Guard is OK), and **one Linux machine you control** with Docker
installed to act as the build runner.

### 1. Fork this repo
Click **Fork** (top-right). Everything below happens in *your* fork.

### 2. Add your Steam login as secrets
In your fork: **Settings → Secrets and variables → Actions → New repository secret**.
Add two:
- `STEAM_USER` — your Steam username
- `STEAM_PASS` — your Steam password

(These are only used on your own runner to download the server files. GitHub keeps
them encrypted.)

### 3. Allow the workflow to publish
**Settings → Actions → General → Workflow permissions** → choose **Read and write
permissions** → Save. (This lets it create the Release.)

### 4. Attach your build machine as a runner
**Settings → Actions → Runners → New self-hosted runner**, pick Linux, and run the
commands it shows on your machine. Then start it so it stays online:
```
cd ~/actions-runner && sudo ./svc.sh install && sudo ./svc.sh start
```
Make sure that machine has **Docker** (`docker --version`) and `python3`. The build
runs inside Docker containers, so you don't install compilers yourself.

### 5. Check everything is wired up
**Actions** tab → **setup-check** → **Run workflow**. It confirms your runner,
Docker, Python, the Steam login, and depot access all work — and tells you exactly
what's wrong if something isn't ready. Fix anything it flags before continuing.

### 6. Test it without waiting for an update
You don't have to wait for a real CS2 update to prove it works. Pick two historical
game builds and let the tool "update" between them:

1. Open <https://steamdb.info/depot/2347773/manifests/> — that's the CS2
   dedicated-server depot's build history. Copy two **Manifest IDs**: an older one
   (OLD) and a newer one (NEW).
2. Pick a CounterStrikeSharp version whose gamedata matches the OLD build — an older
   release tag like `v1.0.305` from
   <https://github.com/roflmuffin/CounterStrikeSharp/releases>.
3. **Actions → update → Run workflow**, then set:
   - **mode**: `manifest`
   - **css_ref**: the CSS tag from step 2
   - **old_manifest**: the OLD manifest ID
   - **new_manifest**: the NEW manifest ID
4. Run it. When it finishes, open the **Release** it created (or the run's
   Artifacts) and read `recovery-report.json` — you'll see the signatures and
   offsets it repaired between those two builds.

> Note: Steam only keeps historical manifests around for so long. If a very old
> manifest ID fails to download, pick a more recent pair.

### 7. Let it run for real
Once tested, it's already scheduled to check for updates every 30 minutes in
`baseline` mode. The **first** automatic run just records the current game build as
its baseline and does nothing else; from then on, whenever CS2 updates it fetches
the new build, recovers the gamedata, bumps the SDK, builds, and publishes a
Release. Grab the `with-runtime` zip from the Release and unzip it into your
server's `game/csgo/` folder.

You can also trigger it any time with **mode: baseline** and leave the manifest
fields blank.

### Deploying the result
The release contains an `addons/` folder. On your **test** server first:
```
# stop the server, then (this keeps your metamod/ intact):
cp -r addons/*  <server>/game/csgo/addons/
```
Start the server and check the log — a clean load with no `Failed to find
signature/offset` lines means the recovery worked. Then roll it to production.
`tools/GamedataSmokeTest.cs` is an optional plugin that actively exercises the
recovered functions in-game if you want a stronger check.

---

## Configuration knobs

Set these as workflow inputs (manual runs) or edit `update.yml`:

| Input | Meaning | Default |
|---|---|---|
| `mode` | `baseline` (auto-diff) or `manifest` (pinned test) | `baseline` |
| `css_ref` | CounterStrikeSharp tag/branch to build | `main` |
| `old_manifest` / `new_manifest` | depot manifest IDs (manifest mode) | — |
| `publish_release` | create a GitHub Release | `true` |

Inside `tools/run_pipeline.sh`: `DOTNET_TAG` and `ASPNET_RUNTIME` — match these to
the CSS version you build (upstream `main` currently uses .NET 10; older tags use
.NET 8).

---

## What was fixed versus upstream's build workflow

This pipeline's build is adapted from CounterStrikeSharp's `build-and-publish.yml`
with the things that break forks removed:

- **No `github.repository == 'roflmuffin/…'` gate** — upstream's publish job never
  runs on a fork; here the build always publishes to *your* repo.
- **No GitVersion dependency** — upstream's versioning needs a config file, full
  git history and tags, and is a common cause of failed fork builds. Version here is
  derived simply from the CSS ref + the CS2 build id + date.
- **Target-framework path is globbed** (`bin/Release/*/publish`) instead of the
  hard-coded `net10.0`, so it keeps working when the .NET version bumps.
- **No NuGet push / Discord webhook** — those need maintainer-only secrets and just
  error on a fork; removed.
- **Linux-focused**, single self-hosted job using Docker for both the native (Steam
  Runtime sniper SDK) and managed (.NET SDK) builds — the same environments upstream
  uses, no local toolchain to install.

---

## Files

```
tools/
  cs2_update_gamedata.py   # the RE engine (signatures + vtable offsets + report)
  fetch_libserver.sh       # depot fetch: manifest (test) or baseline (auto) mode
  run_pipeline.sh          # clone CSS, bump SDK, RE, build native+managed, bundle
  GamedataSmokeTest.cs/.csproj   # optional in-game validation plugin
.github/workflows/
  update.yml               # the pipeline (schedule + manual/test)
  setup-check.yml          # one-time configuration validator
```

CounterStrikeSharp is GPL-3.0; this tool builds it unmodified except for the
recovered `gamedata.json` and the `hl2sdk-cs2` submodule bump.
