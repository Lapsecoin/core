#!/usr/bin/env python3
"""Build the folder that the Pages workflow deploys to lapsecoin.github.io/core/.

    python scripts/build_pages_site.py --repo Lapsecoin/core --keep 5 --out site

For each of the newest releases that carry a lapsecoin.msi it downloads the
installer into <out>/downloads/<version>/lapsecoin.msi (served directly, with
no redirect), copies the newest one to <out>/latest/lapsecoin.msi (a stable
address) and writes <out>/releases.json
for the downloads page on lapsecoin.github.io. Nothing here is committed to git.

The VirusTotal result is read from the release notes, where the release job
already put it (scripts/virustotal.py). A release without one simply has none.

--releases-json FILE reads the release list from a file instead of the GitHub
API, and --no-download skips the installers; both are for testing.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

SITE = "https://lapsecoin.github.io/core"
MSI = "lapsecoin.msi"
LINUX = "lapsecoin"
VT_LINE = re.compile(r"\*\*VirusTotal:\*\*\s*\[([^\]]+)\]\((https://www\.virustotal\.com/[^)\s]+)\)")
VT_COUNT = re.compile(r"(\d+)\s+of\s+(\d+)")
VT_SCANNED = re.compile(r"scanned (\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)")


def gh(*args):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def virustotal(body):
    """{"text", "url", "flagged", "engines", "scanned"} from the release notes, or None."""
    m = VT_LINE.search(body or "")
    if not m:
        return None
    text, url = m.group(1), m.group(2)
    count = VT_COUNT.search(text)
    scanned = VT_SCANNED.search(body)
    return {
        "text": text,
        "url": url,
        "flagged": int(count.group(1)) if count else None,
        "engines": int(count.group(2)) if count else None,
        "scanned": scanned.group(1) if scanned else None,
    }


def asset(release, name):
    return next((a for a in release.get("assets", []) if a["name"] == name), None)


def digest(a):
    d = a.get("digest") or ""
    return d.removeprefix("sha256:") if d.startswith("sha256:") else None


def entry(release, msi_sha):
    version = release["tag_name"].removeprefix("v")
    msi = asset(release, MSI)
    linux = asset(release, LINUX)
    return {
        "version": version,
        "tag": release["tag_name"],
        "published": (release.get("published_at") or "")[:10],
        "url": release["html_url"],
        "msi": {
            "url": f"{SITE}/downloads/{version}/{MSI}",
            "size": msi["size"],
            "sha256": msi_sha or digest(msi),
        },
        "linux": None if not linux else {
            "url": linux["browser_download_url"],
            "size": linux["size"],
            "sha256": digest(linux),
        },
        "virustotal": virustotal(release.get("body")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="Lapsecoin/core")
    ap.add_argument("--keep", type=int, default=5)
    ap.add_argument("--out", default="site")
    ap.add_argument("--releases-json")
    ap.add_argument("--no-download", action="store_true")
    args = ap.parse_args()

    if args.releases_json:
        with open(args.releases_json, encoding="utf-8") as f:
            releases = json.load(f)
    else:
        releases = json.loads(gh("api", f"repos/{args.repo}/releases?per_page=30"))

    chosen = [r for r in releases
              if not r.get("draft") and not r.get("prerelease") and asset(r, MSI)][:args.keep]
    if not chosen:
        print("no release with", MSI, file=sys.stderr)
        return 1

    entries = []
    for r in chosen:
        version = r["tag_name"].removeprefix("v")
        sha = None
        if not args.no_download:
            folder = os.path.join(args.out, "downloads", version)
            os.makedirs(folder, exist_ok=True)
            gh("release", "download", r["tag_name"], "--repo", args.repo,
               "--pattern", MSI, "--dir", folder, "--clobber")
            sha = sha256_of(os.path.join(folder, MSI))
            listed = digest(asset(r, MSI))
            if listed and listed != sha:
                print(f"{r['tag_name']}: downloaded {MSI} does not match the release digest", file=sys.stderr)
                return 1
        entries.append(entry(r, sha))

    os.makedirs(args.out, exist_ok=True)
    if not args.no_download:
        newest = entries[0]["version"]
        os.makedirs(os.path.join(args.out, "latest"), exist_ok=True)
        shutil.copyfile(os.path.join(args.out, "downloads", newest, MSI),
                        os.path.join(args.out, "latest", MSI))
    with open(os.path.join(args.out, "releases.json"), "w", encoding="utf-8") as f:
        doc = {"latest_msi": f"{SITE}/latest/{MSI}", "releases": entries}
        json.dump(doc, f, indent=1)
        f.write("\n")
    # The unstyled project root is not a page anyone should land on.
    with open(os.path.join(args.out, "index.html"), "w", encoding="utf-8") as f:
        f.write('<!doctype html><meta charset="utf-8"><title>LapseCoin downloads</title>'
                '<meta http-equiv="refresh" content="0; url=https://lapsecoin.github.io/downloads.html">'
                '<link rel="canonical" href="https://lapsecoin.github.io/downloads.html">'
                '<a href="https://lapsecoin.github.io/downloads.html">LapseCoin downloads</a>\n')
    open(os.path.join(args.out, ".nojekyll"), "w").close()
    print("releases:", ", ".join(e["version"] for e in entries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
