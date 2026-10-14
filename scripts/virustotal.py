#!/usr/bin/env python3
"""Scan a release file on VirusTotal and print a markdown summary.

    VT_API_KEY=... python scripts/virustotal.py dist/lapsecoin.msi [badge-file]

The key comes from the environment (the VT_API_KEY repo secret in CI). The
summary goes to stdout and, when a badge file is named, the badge (markdown
for the heading of the notes) is written there. Progress goes to stderr. Any
problem (no key, network, timeout) prints nothing and exits 0: a scan must
never block a release.
Files over 32 MB use the one-time upload URL the API hands out.
"""
import hashlib
import json
import os
import sys
import time
import urllib.request
import uuid

API = "https://www.virustotal.com/api/v3"
DIRECT_LIMIT = 32 * 1024 * 1024
POLL_SECONDS = 20
GIVE_UP_AFTER = 15 * 60


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def call(url, key, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers={"x-apikey": key, **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def multipart(path):
    boundary = uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{os.path.basename(path)}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    with open(path, "rb") as f:
        body = head + f.read() + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def scan(path, key):
    url = f"{API}/files"
    if os.path.getsize(path) > DIRECT_LIMIT:
        url = call(f"{API}/files/upload_url", key)["data"]
    body, headers = multipart(path)
    return call(url, key, body, headers)["data"]["id"]


def wait(analysis_id, key):
    deadline = time.monotonic() + GIVE_UP_AFTER
    while time.monotonic() < deadline:
        attrs = call(f"{API}/analyses/{analysis_id}", key)["data"]["attributes"]
        if attrs["status"] == "completed":
            return attrs["stats"]
        log(f"virustotal: {attrs['status']}")
        time.sleep(POLL_SECONDS)
    return None


def render(path, sha, stats):
    """(the summary for the body of the notes, the badge for their heading)."""
    flagged = stats.get("malicious", 0) + stats.get("suspicious", 0)
    total = flagged + stats.get("harmless", 0) + stats.get("undetected", 0)
    color = "brightgreen" if flagged == 0 else "yellow" if flagged <= 3 else "red"
    badge = (f"![VirusTotal](https://img.shields.io/badge/VirusTotal-"
             f"{flagged}%20%2F%20{total}-{color})")
    link = f"https://www.virustotal.com/gui/file/{sha}"
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    summary = (f"**VirusTotal:** [{flagged} of {total} engines flagged "
               f"{os.path.basename(path)}]({link})\n\n"
               f"SHA256: `{sha}`, scanned {when}. Engines update their verdicts over "
               "time, so the link shows the current result.\n")
    return summary, badge


def main():
    key = os.environ.get("VT_API_KEY", "").strip()
    if len(sys.argv) not in (2, 3) or not key:
        log("virustotal: no file or no VT_API_KEY, skipping")
        return
    path = sys.argv[1]
    try:
        with open(path, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        stats = wait(scan(path, key), key)
        if stats is None:
            log("virustotal: gave up waiting")
            return
        summary, badge = render(path, sha, stats)
        print(summary)
        if len(sys.argv) == 3:           # the badge goes in the heading of the notes
            with open(sys.argv[2], "w", encoding="utf-8") as f:
                f.write(badge)
    except Exception as e:  # never block a release on a scan
        log(f"virustotal: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
