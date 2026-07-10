# CS2-SharpUpdater https://buymeacoffee.com/theboneman

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

## Prerequisites

Before you touch the workflow, get these in place. Most of the pain people hit is a
missing piece here, not the tool itself.

**Accounts**
- A **GitHub account** to hold your fork.
- A **Steam account that owns Counter-Strike 2.** If you use Steam Guard, make sure
  you have your phone out and ready during the workflows with the Steam app open.

**The build machine (your self-hosted runner)** — one Linux box you control, that
stays on. It does the downloading and building, so it needs:
- **Docker**, with your runner's user able to use it *without sudo* (this is the
  exact thing that bites most people — see Troubleshooting below).
- **Python 3** (3.10+), plus `pip`.
- **git**, **binutils** (`readelf`), **zip**, and **unzip**.

On Ubuntu/Debian that's:
```bash
# Docker
sudo apt-get update && sudo apt-get install -y docker.io
sudo systemctl enable --now docker

# build/runtime tools
sudo apt-get install -y python3 python3-pip git binutils zip unzip

# let your login user run docker without sudo (log out/in afterwards)
sudo usermod -aG docker "$USER"
```
The Python libraries the recovery needs (`pyelftools`, `capstone`) are installed
automatically by the workflow, so you don't have to. You also don't install any C++
or .NET toolchain by hand — those live inside the Docker images the build pulls.

Once the box is ready, register it as a runner (Setup step 4) and confirm
everything with the **setup-check** action (Setup step 5) before your first real
run.

## Setup for non-experts

You need: a GitHub account, a **Steam account that owns CS2**

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

## Troubleshooting

### `docker: permission denied ... /var/run/docker.sock`

The build dies at the "native build" step with something like:
```
docker: permission denied while trying to connect to the Docker daemon socket
at unix:///var/run/docker.sock ... connect: permission denied.
```
This is not a bug in the pipeline. Your self-hosted runner runs as an unprivileged
user, and Docker's socket is only accessible to root and members of the `docker`
group. Your runner user isn't in that group yet.

Fix it on the runner machine:
```bash
# make sure the daemon is installed and running
sudo systemctl enable --now docker
docker --version

# add the runner's user to the docker group
#   run this AS the user the runner service runs as
sudo usermod -aG docker "$(whoami)"
#   (if the runner runs as a different user: sudo usermod -aG docker <that-user>)

# restart the runner service so it picks up the new group membership
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh start
```
Group membership only applies to processes started *after* the change, which is why
the service restart matters — the still-running listener has the old groups. Verify
as the runner user (no `sudo`, or you'd mask the problem):
```bash
docker info      # should print daemon info with no permission error
```
If it still fails after the restart, reboot the box once — a few setups don't hand
the new group to services until a full boot. Then re-run the workflow.

> Heads-up: being in the `docker` group is effectively root on that machine (anyone
> who can reach the Docker socket can mount the host filesystem into a container as
> root). That's normal for a build box, and a good reason to keep this runner
> dedicated to building rather than sharing it with anything sensitive — it already
> holds your Steam login, so treat it as a trusted build machine.

To catch this before it wastes a build, the **setup-check** action tests actual
daemon access (`docker info`), not just that the Docker CLI exists.

### `fatal: detected dubious ownership` / `git describe ... exited with code 128`

You shouldn't see this — the pipeline already whitelists the checkout inside both
build containers (`git config --global --add safe.directory`), which is what
CounterStrikeSharp's version-stamping `git describe` needs when it runs as root
against a repo owned by your host user. It's called out here only so that if you
ever build the tree **by hand** in a container, you know to add that same line
before running `dotnet publish`.

### `NU1903` (Newtonsoft.Json vulnerability) or `NU1510` warnings

These are warnings from CounterStrikeSharp's own projects, not errors, and they
don't stop the build. The pipeline restores only the API project (not the test
project that pulls the flagged package), so the vulnerability warning shouldn't
appear in your build at all; if you see the `NU1510` prune note it's harmless.

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
