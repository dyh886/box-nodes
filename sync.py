#!/usr/bin/env python3
"""
box-nodes / sync.py — 全自动更新并发布

流程（全部在云机上完成，不依赖本地文件）：
  1. build_live.py fetch   拉所有订阅 URL
  2. build_live.py probe   聚合 + 测「到落地 IP」延迟 + 挑前 N
  3. build_live.py build   生成四层链式配置
  4. 推 main + gh-pages    双通道发布

产物：
  docs/chain.yaml  —— GitHub Pages 直出（主入口，秒级生效）
  dist/chain.yaml  —— jsDelivr 镜像

用法:
  python3 sync.py              全流程 + 发布
  python3 sync.py --dry-run    只生成，不推
  python3 sync.py --top 30     改精选数量
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
CDN_BASE = "https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd, cwd=ROOT, check=True, env=None):
    e = dict(os.environ)
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.setdefault("PYTHONUTF8", "1")
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=e)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.returncode}\n{p.stderr[-800:]}")
    return p


def git(*args, check=True, cwd=ROOT):
    return run(["git", *args], cwd=cwd, check=check)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--workers", type=int, default=40)
    ap.add_argument("--timeout", type=float, default=6.0)
    args = ap.parse_args()

    py = sys.executable

    # 读 config 里的精选数量
    top = args.top
    if top is None:
        try:
            with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
                top = json.load(f).get("pick", {}).get("top", 20)
        except Exception:  # noqa: BLE001
            top = 20

    # ---- 1. fetch ----
    log("step 1/4 fetch subscriptions")
    p = run([py, "build_live.py", "fetch"], check=False)
    if p.returncode != 0:
        log(f"fetch FAILED: {p.stderr[-300:]}")
        return 1

    # ---- 2. probe ----
    log(f"step 2/4 probe + pick top {top}")
    p = run([py, "build_live.py", "probe",
             "--workers", str(args.workers),
             "--timeout", str(args.timeout),
             "--top", str(top)], check=False)
    if p.returncode != 0:
        log(f"probe FAILED: {p.stderr[-300:]}")
        return 1
    for line in p.stderr.strip().splitlines()[-8:]:
        log("  " + line)

    # ---- 3. build ----
    log("step 3/4 build chain")
    p = run([py, "build_live.py", "build"], check=False)
    if p.returncode != 0:
        log(f"build FAILED: {p.stderr[-400:]}")
        return 1
    log("chain.yaml built")

    # 统计
    try:
        with open(os.path.join(ROOT, "state", "live_nodes.json"), encoding="utf-8") as f:
            st = json.load(f).get("stats", {})
        log(f"stats: raw={st.get('raw')} reachable={st.get('reachable')} "
            f"kept={st.get('kept')}")
    except Exception:  # noqa: BLE001
        st = {}

    if args.dry_run:
        log("dry-run: skipping publish")
        return 0

    # ---- 4. publish ----
    log("step 4/4 publish")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    git("add", "-A")
    if git("status", "--porcelain").stdout.strip():
        msg = (f"chore: refresh chain — {st.get('kept','?')} nodes "
               f"(from {st.get('reachable','?')}/{st.get('raw','?')}) @ {stamp}")
        git("commit", "-m", msg)
        p = git("push", "origin", "HEAD:main", check=False)
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
