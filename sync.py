#!/usr/bin/env python3
"""
box-nodes / sync.py — 全自动更新并发布

流程（全部在云机完成）：
  1. build_live.py fetch   拉订阅（含 mirror 镜像源）
  2. build_live.py rank    按协议质量初筛，挑前 N
  3. build_live.py build   生成四层链式配置
  4. 推 main + gh-pages     双通道发布

注意：云机出口只放行 443/22，无法验证非 443 的落地链路，
所以这里不做"延迟实测"，节点的真实速度由客户端 url-test 决定。

用法:
  python3 sync.py              全流程
  python3 sync.py --dry-run    只生成不发布
  python3 sync.py --top 30     改变精选数量
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
PAGES_BASE = "https://dyh886.github.io/box-nodes"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd, cwd=ROOT, check=True):
    e = dict(os.environ)
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.setdefault("PYTHONUTF8", "1")
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=e)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.returncode}\n{p.stderr[-600:]}")
    return p


def git(*args, check=True, cwd=ROOT):
    return run(["git", *args], cwd=cwd, check=check)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=None)
    args = ap.parse_args()

    py = sys.executable

    top = args.top
    if top is None:
        try:
            with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
                top = json.load(f).get("pick", {}).get("top", 20)
        except Exception:  # noqa: BLE001
            top = 20

    log("step 1/4 fetch")
    p = run([py, "build_live.py", "fetch"], check=False)
    if p.returncode != 0:
        log(f"fetch FAILED: {p.stderr[-300:]}")
        return 1
    for line in p.stderr.strip().splitlines():
        log("  " + line)

    log(f"step 2/4 rank top {top}")
    p = run([py, "build_live.py", "rank", "--top", str(top)], check=False)
    if p.returncode != 0:
        log(f"rank FAILED: {p.stderr[-300:]}")
        return 1
    for line in p.stderr.strip().splitlines():
        log("  " + line)

    log("step 3/4 build")
    p = run([py, "build_live.py", "build"], check=False)
    if p.returncode != 0:
        log(f"build FAILED: {p.stderr[-400:]}")
        return 1
    log("chain.yaml built")

    if args.dry_run:
        log("dry-run: skip publish")
        return 0

    log("step 4/4 publish")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    git("add", "-A")
    if git("status", "--porcelain").stdout.strip():
        git("commit", "-m", f"chore: refresh chain @ {stamp}")
        p = git("push", "origin", "HEAD:main", check=False)
        if p.returncode != 0:
            p = git("push", "--force", "origin", "HEAD:main", check=False)
        log("main pushed" if p.returncode == 0
            else f"main push FAILED: {p.stderr[-200:]}")
    else:
        log("main: no changes")

    publish_pages()
    return 0


def publish_pages():
    work = os.path.join(tempfile.gettempdir(), "box-nodes-pages")
    if os.path.isdir(work):
        subprocess.run(["git", "worktree", "remove", "--force", work],
                       cwd=ROOT, capture_output=True, text=True)
        shutil.rmtree(work, ignore_errors=True)

    p = run(["git", "worktree", "add", "--force", "-B", "gh-pages", work],
            check=False)
    if p.returncode != 0:
        log(f"worktree add -> {p.stderr[-200:]}")
        return

    docs = os.path.join(ROOT, "docs")
    for name in os.listdir(work):
        if name == ".git":
            continue
        full = os.path.join(work, name)
        if os.path.isdir(full):
            shutil.rmtree(full, ignore_errors=True)
        else:
            try:
                os.remove(full)
            except OSError:
                pass

    for name in os.listdir(docs):
        shutil.copy2(os.path.join(docs, name), os.path.join(work, name))

    run(["git", "add", "-A"], cwd=work)
    if run(["git", "status", "--porcelain"], cwd=work).stdout.strip():
        run(["git", "-c", "user.name=box-agent",
             "-c", "user.email=box@simular.local",
             "commit", "-m",
             f"publish: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"],
            cwd=work)
        p = run(["git", "push", "--force", "origin", "gh-pages"],
                cwd=work, check=False)
        log("pages pushed" if p.returncode == 0
            else f"pages push -> {p.stderr[-200:]}")
    else:
        log("pages: no changes")

    subprocess.run(["git", "worktree", "remove", "--force", work],
                   cwd=ROOT, capture_output=True, text=True)
    shutil.rmtree(work, ignore_errors=True)

    time.sleep(6)
    ok = 0
    for f in ("chain.yaml",):
        url = f"{PAGES_BASE}/{f}"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "box-nodes/1.0",
                              "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=25) as r:
                body = r.read()
            log(f"pages {f} -> {r.status} {len(body)}B")
            ok += 1
        except Exception as e:  # noqa: BLE001
            log(f"pages {f} -> FAIL {e}")
    log(f"pages verified {ok}/1")


if __name__ == "__main__":
    sys.exit(main())
