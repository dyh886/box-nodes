#!/usr/bin/env python3
"""
box-nodes / sync.py — 一键更新：collect → probe → export → git commit/push → jsDelivr 预热

用法:
  python sync.py               完整跑一遍并推送
  python sync.py --dry-run     只跑本地管线，不 commit/push
  python sync.py --no-probe    跳过探活（调试用）

环境:
  GH_TOKEN / GITHUB_TOKEN   推送用；已在远端配好 remote 且能免密时可不设
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CDN_BASE = "https://cdn.jsdelivr.net/gh/dyh886/box-nodes@latest"
PURGE = "https://purge.jsdelivr.net/gh/dyh886/box-nodes@latest"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd, cwd=ROOT, check=True, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=e)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def git(*args, check=True):
    return run(["git", *args], check=check)


def purge_cdn(paths):
    """让 jsDelivr 主动回源，避免旧缓存。"""
    for p in paths:
        url = f"{PURGE}/{p}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "box-nodes/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"purge {p} -> {r.status}")
        except Exception as e:  # noqa: BLE001
            log(f"purge {p} -> {e}")


def verify_cdn(files):
    ok = 0
    for f in files:
        url = f"{CDN_BASE}/{f}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "box-nodes/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
            log(f"cdn {f} -> {r.status} {len(body)}B")
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"cdn {f} -> FAIL {e}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--keep", type=int, default=None)
    args = ap.parse_args()

    py = sys.executable
    log("step 1/3 collect")
    run([py, "pool.py", "collect"])

    if not args.no_probe:
        log("step 2/3 probe")
        cmd = [py, "pool.py", "probe"]
        if args.keep:
            cmd += ["--keep", str(args.keep)]
        run(cmd)
    else:
        log("step 2/3 probe skipped")

    log("step 3/3 export")
    run([py, "pool.py", "export"])

    with open(os.path.join(ROOT, "dist", "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    log(f"nodes={meta['count']} avg={meta['avgLatencyMs']}ms protos={meta['byProtocol']}")

    if args.dry_run:
        log("dry-run: skipping git/cdn")
        return 0

    files = ["dist/clash.yaml", "dist/links.txt", "dist/sub.base64.txt",
             "dist/singbox.json", "dist/meta.json", "state/nodes.json"]

    git("add", *files)
    status = git("status", "--porcelain").stdout.strip()
    if not status:
        log("no changes; skipping commit")
    else:
        msg = (f"chore: refresh node pool — {meta['count']} nodes, "
               f"avg {meta['avgLatencyMs']}ms @ {meta['updatedAtISO']}")
        git("commit", "-m", msg)
        git("push", "origin", "HEAD:main")
        log("pushed")

    purge_cdn(["dist/meta.json", "dist/clash.yaml", "dist/links.txt",
               "dist/sub.base64.txt", "dist/singbox.json"])
    time.sleep(2)
    n = verify_cdn(["dist/meta.json", "dist/clash.yaml"])
    log(f"cdn verified {n}/2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
