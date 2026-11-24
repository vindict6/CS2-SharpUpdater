#!/usr/bin/env python3
"""Apply upstream s2sdk pull requests that aren't merged yet on top of a checked-out SDK. By BONE.

After a CS2 update the SDK fix (a shifted vtable, a new virtual) usually sits in an open pull request on
alliedmodders/s2sdk for a while. Building against cs2 alone gives binaries that crash on the new game build.

    apply_sdk_prs.py <sdk checkout> [pr numbers, comma separated | auto | none]

  auto   the open pull requests into cs2 from the repo's members and past contributors, opened in the last
         30 days, that only touch headers under public/. One that no longer applies is skipped with a warning.
  1,2    exactly these; one that doesn't apply fails the build.

Prints what it applied; with GITHUB_OUTPUT set, also writes applied=<numbers>. GITHUB_TOKEN (optional) raises the
API rate limit.
"""
import datetime, json, os, subprocess, sys, urllib.request

REPO = os.environ.get("SDK_PR_REPO", "alliedmodders/s2sdk")
BASE = os.environ.get("SDK_PR_BASE", "cs2")
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
MAX_AGE_DAYS = 30


def api(path, accept="application/vnd.github+json"):
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{path}", headers={"Accept": accept})
    if os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    return data if accept.endswith("diff") else json.loads(data)


def candidates():
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=MAX_AGE_DAYS)
    picked = []
    for pr in api(f"pulls?state=open&base={BASE}&per_page=50"):
        created = datetime.datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00"))
        if pr.get("draft") or pr.get("author_association") not in TRUSTED or created < since:
            continue
        files = api(f"pulls/{pr['number']}/files?per_page=100")
        if files and all(f["filename"].startswith("public/") and f["filename"].endswith((".h", ".hpp")) for f in files):
            picked.append(pr["number"])
    return sorted(picked)


def apply(sdk, number, required):
    diff = api(f"pulls/{number}", accept="application/vnd.github.v3.diff")
    check = subprocess.run(["git", "-C", sdk, "apply", "--check", "-"], input=diff, capture_output=True)
    if check.returncode != 0:
        msg = f"s2sdk #{number} does not apply on {BASE}: {check.stderr.decode(errors='replace').strip()[:300]}"
        if required:
            sys.exit(f"::error::{msg}")
        print(f"::warning::{msg} (skipped; it may already be merged)")
        return False
    subprocess.run(["git", "-C", sdk, "apply", "-"], input=diff, check=True)
    print(f"applied s2sdk #{number}")
    return True


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sdk, which = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "auto").strip()
    if which in ("", "none"):
        numbers, required = [], True
    elif which == "auto":
        numbers, required = candidates(), False
        print(f"auto: open s2sdk pull requests into {BASE} to apply: {numbers or 'none'}")
    else:
        numbers, required = [int(n) for n in which.replace(" ", "").split(",") if n], True
    applied = [n for n in numbers if apply(sdk, n, required)]
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            out.write(f"applied={','.join(map(str, applied))}\n")


if __name__ == "__main__":
    main()
