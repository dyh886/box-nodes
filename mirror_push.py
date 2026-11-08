#!/usr/bin/env python3
"""
mirror_push.py — 本机侧中转：拉被封锁的订阅，推到 GitHub 供云机读取

背景：
  云机出口 IP 是 136.71.23.57（Google Cloud），alwaysdata 对云厂商网段封锁，
  7x24.alwaysdata.net 在云机上 80/443 全部超时。本机（中国住宅 IP）可以正常拉取。

做法：
  本机拉取 → 写入 mirror/7x24.yaml → 推到 main 分支
  云机 fetch 时把 mirror/7x24.yaml 当成本地源读取，无需访问 alwaysdata

用法:
  python mirror_push.py              拉取 + 推送
  python mirror_push.py --no-push    只拉取到本地
  python mirror_push.py --check      只检查上游是否有变化

建议在本机挂个定时（如每 2 小时），与云机的 sync 错开。
"""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
MIRROR_DIR = os.path.join(ROOT, "mirror")

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
REPO = "dyh886/box-nodes"
API = f"https://api.github.com/repos/{REPO}/contents"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def http_get(url, timeout=40):
    req = urllib.request.Request(url, headers={
        "User-Agent": "clash-verge/v2.0.0", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_cfg():
    with io.open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def targets():
    cfg = load_cfg()
    out = []
    for s in cfg.get("subscriptions", []):
        if s.get("type") == "mirror" and s.get("enabled", True):
            out.append(s)
    return out


def fetch_one(spec):
    url = spec.get("url") or spec.get("upstream")
    if not url:
        log(f"  {spec.get('label')}: no upstream url configured")
        return None
    try:
        raw = http_get(url, timeout=spec.get("timeout", 45))
        log(f"  {spec.get('label')}: {len(raw)} bytes")
        return raw
    except Exception as e:  # noqa: BLE001
        log(f"  {spec.get('label')}: FAIL {type(e).__name__} {e}")
        return None


def count_nodes(raw):
    """粗数节点数，用于判断是否有变化。"""
    import re
    text = raw.decode("utf-8", "ignore")
    n = len(re.findall(r"(?m)^\s*-\s*name\s*:", text))
    if n:
        return n
    m = len(re.findall(r"(?:vmess|vless|trojan|ss|hysteria2|tuic)://", text, re.I))
    return m


def gh_put(path, content_bytes, branch="main", message=None):
    if not TOKEN:
        log("no GITHUB_TOKEN in env; skipping push")
        return False

    def req(method, p, payload=None):
        r = urllib.request.Request(
            f"{API}/{p}",
            data=json.dumps(payload).encode() if payload else None,
            method=method,
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "box-nodes/1.0",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {"error": e.read().decode("utf-8", "replace")[:250]}

    st, j = req("GET", f"{path}?ref={branch}")
    sha = j.get("sha") if st == 200 else None
    payload = {"message": message or f"mirror: update {path}",
               "content": base64.b64encode(content_bytes).decode(),
               "branch": branch}
    if sha:
        payload["sha"] = sha
    st, j = req("PUT", path, payload)
    ok = st in (200, 201)
    log(f"  {'OK ' if ok else 'ERR'} {path} -> {st}")
    if not ok:
        log(f"      {str(j)[:200]}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    os.makedirs(MIRROR_DIR, exist_ok=True)
    specs = targets()
    if not specs:
        log("no mirror sources in config.json")
        return 1

    log(f"mirror: {len(specs)} source(s)")
    changed, failed = [], []
    for spec in specs:
        raw = fetch_one(spec)
        if raw is None:
            failed.append(spec.get("label"))
            continue
        local = os.path.join(ROOT, spec["file"])
        os.makedirs(os.path.dirname(local), exist_ok=True)

        old = None
        if os.path.exists(local):
            with io.open(local, "rb") as f:
                old = f.read()

        same = old == raw
        n = count_nodes(raw)
        log(f"  {spec.get('label')}: {n} nodes, "
            f"{'unchanged' if same else 'CHANGED'}")

        if args.check:
            continue
        if same:
            continue

        with io.open(local, "wb") as f:
            f.write(raw)
        changed.append(spec["file"])

    if args.check:
        log("check only; nothing written")
        return 0

    if not changed:
        log("no changes; nothing to push")
        return 0

    if args.no_push:
        log(f"written locally: {changed}")
        return 0

    ok_all = True
    for rel in changed:
        local = os.path.join(ROOT, rel)
        with io.open(local, "rb") as f:
            data = f.read()
        ok_all &= gh_put(rel, data, message=f"mirror: refresh {rel}")

    log("pushed" if ok_all else "push had errors")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
