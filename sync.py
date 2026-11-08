#!/usr/bin/env python3
"""
box-nodes / sync.py — 一键更新并发布

两条产物线：
  1. 链式代理（主产物）   extract_source.py → build_chain.py → docs/chain.yaml
       本机 → 前置(前置总控) → 落地 107.172.186.223 → 外网
       节点来自本地机场配置，不需要联网探测。
  2. 通用节点池（可选）   pool.py collect/probe/export → dist/*
       只有配了 sources 或 seed 里真有节点时才跑。

发布：
  main 分支   —— 源码 + dist/ + docs/（jsDelivr 镜像）
  gh-pages    —— 只有 docs/ 内容（GitHub Pages 直出，秒级生效）

用法:
  python sync.py              全流程 + 发布
  python sync.py --dry-run    只本地生成，不推
  python sync.py --no-probe   跳过通用池探活
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

# GitHub Pages 直出（主入口，无 CDN 缓存）
PAGES_BASE = "https://dyh886.github.io/box-nodes"
# jsDelivr 镜像（备用，@main 有缓存，purge 可刷新）
CDN_BASE = "https://cdn.jsdelivr.net/gh/dyh886/box-nodes@main/dist"
PURGE = "https://purge.jsdelivr.net/gh/dyh886/box-nodes@main"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(cmd, cwd=ROOT, check=True, env=None):
    """
    跑子进程。显式 UTF-8 解码，避免 Windows 默认 GBK 在遇到
    emoji / 中文 stderr 时抛 UnicodeDecodeError。
    """
    e = dict(os.environ)
    e.setdefault("PYTHONIOENCODING", "utf-8")
    e.setdefault("PYTHONUTF8", "1")
    if env:
        e.update(env)
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=e)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} -> {p.returncode}\n{p.stdout}\n{p.stderr}")
    return p


def git(*args, check=True, cwd=ROOT):
    return run(["git", *args], cwd=cwd, check=check)


def purge_cdn(paths):
    for p in paths:
        url = f"{PURGE}/{p}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "box-nodes/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                log(f"purge {p} -> {r.status}")
        except Exception as e:  # noqa: BLE001
            log(f"purge {p} -> {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--keep", type=int, default=None)
    args = ap.parse_args()

    py = sys.executable

    # ---------- 产物线 1：链式代理（主产物）----------
    log("step 0/4 extract + build chain")
    if os.path.exists(os.path.join(ROOT, "extract_source.py")):
        p = run([py, "extract_source.py"], check=False)
        if p.returncode != 0:
            log(f"extract skipped: {p.stderr.strip()[-200:]}")
    p = run([py, "build_chain.py"], check=False)
    if p.returncode != 0:
        log(f"build_chain FAILED: {p.stderr.strip()[-300:]}")
    else:
        log("chain.yaml built")

    # ---------- 产物线 2：通用节点池（可选）----------
    seed = os.path.join(ROOT, "state", "seed.txt")
    seed_has_nodes = False
    if os.path.exists(seed):
        with open(seed, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if ln.strip() and not ln.lstrip().startswith("#"):
                    seed_has_nodes = True
                    break

    cfg_has_url = False
    cfg_path = os.path.join(ROOT, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg_has_url = any(s.get("url") and s.get("enabled", True)
                          for s in cfg.get("sources", []))

    pool_meta = None
    if seed_has_nodes or cfg_has_url:
        log("step 1/4 collect")
        run([py, "pool.py", "collect"], check=False)

        if not args.no_probe:
            log("step 2/4 probe")
            cmd = [py, "pool.py", "probe"]
            if args.keep:
                cmd += ["--keep", str(args.keep)]
            run(cmd, check=False)
        else:
            log("step 2/4 probe skipped")

        log("step 3/4 export")
        p = run([py, "pool.py", "export"], check=False)
        mp = os.path.join(ROOT, "dist", "meta.json")
        if p.returncode == 0 and os.path.exists(mp):
            with open(mp, encoding="utf-8") as f:
                pool_meta = json.load(f)
            log(f"pool: nodes={pool_meta['count']} "
                f"avg={pool_meta['avgLatencyMs']}ms")
        else:
            log("pool: export skipped (no nodes)")
    else:
        log("pool: no sources configured — skipping (chain.yaml is primary)")

    stamp_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if args.dry_run:
        log("dry-run: skipping publish")
        return 0

    # ---------- 发布：main ----------
    git("add", "-A")
    if git("status", "--porcelain").stdout.strip():
        pool_part = f"pool={pool_meta['count']} nodes" if pool_meta else "pool=n/a"
        git("commit", "-m", f"chore: refresh chain + {pool_part} @ {stamp_iso}")
        p = git("push", "origin", "HEAD:main", check=False)
        log("main pushed" if p.returncode == 0
            else f"main push FAILED: {p.stderr.strip()[-200:]}")
    else:
        log("main: no changes")

    # ---------- 发布：gh-pages（Pages 直出）----------
    publish_pages()
    return 0


def publish_pages():
    """
    把 docs/ 作为 gh-pages 分支根目录发布。

    用临时 worktree：只放 docs/ 的文件，提交后强推。
    gh-pages 永远只含产物，不含源码；Pages 每次 build 秒级生效。
    """
    work = os.path.join(tempfile.gettempdir(), "box-nodes-pages")
    if os.path.isdir(work):
        subprocess.run(["git", "worktree", "remove", "--force", work],
                       cwd=ROOT, capture_output=True, text=True)
        shutil.rmtree(work, ignore_errors=True)

    p = run(["git", "worktree", "add", "--force", "-B", "gh-pages", work],
            check=False)
    if p.returncode != 0:
        log(f"worktree add -> {p.stderr.strip()[-200:]}")
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
            else f"pages push -> {p.stderr.strip()[-200:]}")
    else:
        log("pages: no changes")

    subprocess.run(["git", "worktree", "remove", "--force", work],
                   cwd=ROOT, capture_output=True, text=True)
    shutil.rmtree(work, ignore_errors=True)

    time.sleep(6)
    ok = 0
    for f in ("chain.yaml", "meta.json"):
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
    log(f"pages verified {ok}/2")


if __name__ == "__main__":
    sys.exit(main())
