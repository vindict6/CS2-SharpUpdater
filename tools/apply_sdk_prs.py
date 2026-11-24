#!/usr/bin/env python3
"""Apply upstream s2sdk pull requests that aren't merged yet on top of a checked-out SDK. By BONE.

After a CS2 update the SDK fix (a shifted vtable, a new virtual) usually sits in an open pull request on
alliedmodders/s2sdk for a while. Building against cs2 alone gives binaries that crash on the new game build.

    apply_sdk_prs.py <sdk checkout> <which> [--save <file.patch>]

  which = auto         the open pull requests into cs2 from s2sdk's members and past contributors that only touch
                       headers under public/. Every one must apply, and no two may touch the same file (competing
                       fixes for one update would shift a vtable twice): either stops the build, so a binary is never
                       built quietly without a fix. Pass the numbers you want instead to settle it.
          128,130      exactly these, which must apply
          none         nothing
          patch:FILE   a patch saved earlier with --save (so several builds use identical headers)

--save writes the combined diff of what was applied to FILE and its pull request numbers to FILE.prs.
Prints "applied s2sdk #N (sha)" per pull request; with GITHUB_OUTPUT set, also writes applied=<numbers>.
GITHUB_TOKEN (optional) raises the API rate limit.
"""
import json, os, pathlib, subprocess, sys, urllib.request

REPO = os.environ.get("SDK_PR_REPO", "alliedmodders/s2sdk")
BASE = os.environ.get("SDK_PR_BASE", "cs2")
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}


def api(path, accept="application/vnd.github+json"):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}", headers={"Accept": accept})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if accept.endswith("diff") else json.loads(data)


def files_of(number):
    return [f["filename"] for f in api(f"pulls/{number}/files?per_page=100")]


def candidates():
    picked, touched = [], {}
    for pr in api(f"pulls?state=open&base={BASE}&per_page=100"):
        if pr.get("draft") or pr.get("author_association") not in TRUSTED:
            continue
        files = files_of(pr["number"])
        if not files or not all(f.startswith("public/") and f.endswith((".h", ".hpp")) for f in files):
            continue
        for f in files:
            if f in touched:
                sys.exit(f"::error::s2sdk #{touched[f]} and #{pr['number']} both change {f}; they may be competing fixes "
                         f"for the same update. Pick the right one(s) explicitly (sdk_prs).")
            touched[f] = pr["number"]
        picked.append(pr["number"])
    return sorted(picked)


def git_apply(sdk, diff, what):
    check = subprocess.run(["git", "-C", sdk, "apply", "--check", "-"], input=diff, capture_output=True)
    if check.returncode != 0:
        sys.exit(f"::error::{what} does not apply on {BASE}: {check.stderr.decode(errors='replace').strip()[:300]}. "
                 f"It may have been merged or need a rebase; set sdk_prs explicitly (or none) to build without it.")
    subprocess.run(["git", "-C", sdk, "apply", "-"], input=diff, check=True)


def main():
    args = sys.argv[1:]
    save = None
    if "--save" in args:
        i = args.index("--save")
        save = args[i + 1]
        del args[i:i + 2]
    if not args:
        sys.exit(__doc__)
    sdk, which = args[0], (args[1] if len(args) > 1 else "auto").strip()

    applied, combined = [], b""
    if which.startswith("patch:"):
        path = pathlib.Path(which[6:])
        diff = path.read_bytes()
        if diff.strip():
            git_apply(sdk, diff, f"saved patch {path}")
        prs = pathlib.Path(str(path) + ".prs")
        applied = [n for n in (prs.read_text().split(",") if prs.exists() else []) if n.strip()]
        for n in applied:
            print(f"applied s2sdk #{n} (from {path.name})")
        combined = diff
    else:
        if which in ("", "none"):
            numbers = []
        elif which == "auto":
            numbers = candidates()
            print(f"auto: open s2sdk pull requests into {BASE} to apply: {numbers or 'none'}")
        else:
            numbers = [int(n) for n in which.replace(" ", "").split(",") if n]
        for n in numbers:
            # The diff and the head it came from, read until they agree (a push in between would mislabel the diff)
            for _ in range(3):
                pr = api(f"pulls/{n}")
                diff = api(f"pulls/{n}", accept="application/vnd.github.v3.diff")
                if api(f"pulls/{n}")["head"]["sha"] == pr["head"]["sha"]:
                    break
            else:
                sys.exit(f"::error::s2sdk #{n} keeps changing while being read; try again in a minute")
            git_apply(sdk, diff, f"s2sdk #{n}")
            print(f"applied s2sdk #{n} ({pr['head']['sha'][:8]}: {pr['title'][:60]})")
            applied.append(str(n))
            combined += diff if diff.endswith(b"\n") else diff + b"\n"

    if save:
        pathlib.Path(save).write_bytes(combined)
        pathlib.Path(save + ".prs").write_text(",".join(applied))
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"applied={','.join(applied)}\n")


if __name__ == "__main__":
    main()
