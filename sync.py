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
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
# GitHub Pages 直出：无 CDN 缓存，提交后秒级生效，这是主入口
PAGES_BASE = "https://dyh886.github.io/box-nodes"
# jsDelivr 镜像：国内快，但分支引用有长缓存，需按 commit sha 取才能拿最新
CDN_BASE = "https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist"
PURGE = "https://purge.jsdelivr.net/gh/dyh886/box-nodes@main"


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
        log("dry-run: skipping publish")
        return 0

    # ---- 主线：main 分支存源码 + dist（jsDelivr 镜像）----
    src_files = ["pool.py", "sync.py", "config.json", "README.md",
                 "dist/clash.yaml", "dist/links.txt", "dist/sub.base64.txt",
                 "dist/singbox.json", "dist/meta.json", "state/nodes.json"]
    git("add", "-A")
    if git("status", "--porcelain").stdout.strip():
        msg = (f"chore: refresh node pool — {meta['count']} nodes, "
               f"avg {meta['avgLatencyMs']}ms @ {meta['updatedAtISO']}")
        git("commit", "-m", msg)
        git("push", "origin", "HEAD:main")
        log("main pushed")
    else:
        log("main: no changes")

    # ---- 分发线：docs/ 推到 gh-pages，由 GitHub Pages 直出（无 CDN 缓存）----
    publish_pages()

    return 0


def publish_pages():
    """
    把 docs/ 的内容作为 gh-pages 分支的根目录发布。

    用 git worktree 建一个临时目录，只把 docs/ 的文件放进去，提交后强推。
    这样 gh-pages 永远只含产物，不含源码；Pages 每次 build 秒级生效，
    不受 jsDelivr 分支引用缓存影响。
    """
    work = os.path.join(tempfile.gettempdir(), "box-nodes-pages")
    if os.path.isdir(work):
        subprocess.run(["git", "worktree", "remove", "--force", work],
                       cwd=ROOT, capture_output=True, text=True)
        shutil.rmtree(work, ignore_errors=True)

    p = run(["git", "worktree", "add", "--force", "-B", "gh-pages", work], check=False)
    if p.returncode != 0:
        log(f"worktree add -> {p.stderr.strip()[:200]}")
        return

    docs = os.path.join(ROOT, "docs")
    for name in os.listdir(work):
        if name == ".git":
            continue
        full = os.path.join(work, name)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            os.remove(full)

    for name in os.listdir(docs):
        shutil.copy2(os.path.join(docs, name), os.path.join(work, name))

    run(["git", "add", "-A"], cwd=work)
    st = run(["git", "status", "--porcelain"], cwd=work).stdout.strip()
    if not st:
        log("pages: no changes")
    else:
        run(["git", "-c", "user.name=box-agent",
             "-c", "user.email=box@simular.local",
             "commit", "-m", f"publish: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"],
            cwd=work)
        p = run(["git", "push", "--force", "origin", "gh-pages"], cwd=work, check=False)
        log("pages pushed" if p.returncode == 0 else f"pages push -> {p.stderr.strip()[:200]}")

    subprocess.run(["git", "worktree", "remove", "--force", work],
                   cwd=ROOT, capture_output=True, text=True)
    shutil.rmtree(work, ignore_errors=True)

    time.sleep(8)
    ok = 0
    for f in ("meta.json", "clash.yaml"):
        url = f"{PAGES_BASE}/{f}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "box-nodes/1.0",
                                                       "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
            log(f"pages {f} -> {r.status} {len(body)}B")
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"pages {f} -> FAIL {e}")
    log(f"pages verified {ok}/2")


if __name__ == "__main__":
    sys.exit(main())
