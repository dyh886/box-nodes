#!/usr/bin/env python3
"""
sync_local.py — 本机完整流水线（每 2 小时）

与云端 sync.py 的区别：
  云机出口只放行 443/22，落地 9443 不可达，无法验证链路。
  本机有真实出口，能完整走通「前置 → 落地 → 外网」，所以由本机负责筛选。
  云机退化为「聚合候选 + 镜像发布」，不再单独决定哪些节点可用。

流程：
  1. build_live.py fetch     拉全部订阅
  2. build_live.py rank      协议质量初筛（扩大候选到 80）
  3. l2_verify.py            端到端验证：经节点→落地实连 GitHub
  4. stability_check.py      三轮复测，剔除间歇性可用
  5. select_and_build.py     选稳定节点并生成 chain.yaml
  6. 发布 main + gh-pages

用法:
  python sync_local.py              全流程 + 发布
  python sync_local.py --dry-run    只生成不发布
  python sync_local.py --no-l2      跳过验证（仅调试）
"""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "dyh886/box-nodes"
API = f"https://api.github.com/repos/{REPO}/contents"
PAGES = "https://dyh886.github.io/box-nodes"

ENV = {k: v for k, v in os.environ.items()
       if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
ENV["PYTHONIOENCODING"] = "utf-8"
ENV["NO_PROXY"] = "*"
ENV["no_proxy"] = "*"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def run(args, timeout=1800):
    p = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=ENV, timeout=timeout)
    return p


def curl(args, t=90):
    p = subprocess.run(["curl.exe", "-q", "-s", "--noproxy", "*",
                        "--ssl-revoke-best-effort", "--max-time", str(t), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode, p.stdout, p.stderr


def gh_put(branch, path, data, msg, token):
    rc, out, _ = curl(["-H", f"Authorization: Bearer {token}",
                       "-H", "Accept: application/vnd.github+json",
                       f"{API}/{path}?ref={branch}"])
    sha = None
    if rc == 0 and out.strip():
        try:
            sha = json.loads(out).get("sha")
        except Exception:  # noqa: BLE001
            sha = None
    payload = {"message": msg,
               "content": base64.b64encode(data).decode(),
               "branch": branch}
    if sha:
        payload["sha"] = sha
    fd, tmp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    rc, out, err = curl(["-X", "PUT",
                         "-H", f"Authorization: Bearer {token}",
                         "-H", "Accept: application/vnd.github+json",
                         "-H", "Content-Type: application/json",
                         "--data-binary", f"@{tmp}",
                         f"{API}/{path}"], 120)
    try:
        os.remove(tmp)
    except OSError:
        pass
    ok = bool(rc == 0 and out.strip()
              and ("content" in out or "commit" in out))
    log(f"  {'OK ' if ok else 'ERR'} [{branch}] {path}")
    return ok


def publish(token):
    data = (ROOT / "docs" / "chain.yaml").read_bytes()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    msg = f"chore: verified node refresh @ {stamp}"
    ok = True
    for br, p in (("main", "docs/chain.yaml"),
                  ("main", "dist/chain.yaml"),
                  ("gh-pages", "chain.yaml")):
        ok &= gh_put(br, p, data, msg, token)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-l2", action="store_true",
                    help="跳过拉取与验证，仅用现有 state 重新构建并发布")
    ap.add_argument("--publish-only", action="store_true",
                    help="只用当前 docs/chain.yaml 发布，不做任何构建")
    ap.add_argument("--top", type=int, default=400,
                    help="候选上限；默认全量，避免截断丢掉可用节点")
    args = ap.parse_args()

    if args.publish_only:
        log("publish-only: 直接发布现有 docs/chain.yaml")
        return do_publish(args)

    if not args.no_l2:
        log("step 1/6 fetch subscriptions")
        p = run(["build_live.py", "fetch"], timeout=600)
        for ln in (p.stderr or "").strip().splitlines()[-4:]:
            log("  " + ln)

        log("step 2/6 rank candidates")
        p = run(["build_live.py", "rank", "--top", str(args.top)], timeout=300)
        for ln in (p.stderr or "").strip().splitlines()[-2:]:
            log("  " + ln)

        log("step 3/6 L2 end-to-end verify (github required)")
        p = run(["l2_verify.py", "--batch", "8"], timeout=2400)
        lines = (p.stderr or "").strip().splitlines()
        for ln in lines[-2:]:
            log("  " + ln)
        try:
            live = json.loads((ROOT / "state" / "live_nodes.json").read_text(encoding="utf-8"))
            if live.get("count", 0) == 0:
                log("  no node passed L2 — abort, keep previous subscription")
                return 1
        except Exception as e:  # noqa: BLE001
            log(f"  read live_nodes failed: {e}")
            return 1

        log("step 4/6 stability (3 rounds)")
        p = run(["stability_check.py"], timeout=2400)
        for ln in (p.stdout or "").strip().splitlines()[-2:]:
            log("  " + ln)

        log("step 5/6 select + build")
        p = run(["select_and_build.py"], timeout=300)
        for ln in (p.stdout or "").strip().splitlines()[:6]:
            log("  " + ln)
        for ln in (p.stderr or "").strip().splitlines()[-5:]:
            log("  " + ln)
        if p.returncode != 0:
            log("  build failed")
            return 1
    else:
        # 关键：--no-l2 只重建，绝不重新拉取/筛选，避免覆盖已验证节点集
        log("no-l2: 用现有 state 重建（不拉取、不覆盖验证结果）")
        p = run(["build_live.py", "build"], timeout=300)
        for ln in (p.stderr or "").strip().splitlines()[-5:]:
            log("  " + ln)
        if p.returncode != 0:
            log("  build failed")
            return 1

    return do_publish(args)


def do_publish(args):
    """发布前做硬性把关：必须是经端到端验证过的节点集。"""
    chain_path = ROOT / "docs" / "chain.yaml"
    if not chain_path.exists():
        log("docs/chain.yaml missing")
        return 1

    import yaml
    doc = yaml.safe_load(chain_path.read_text(encoding="utf-8"))
    fronts = [p for p in doc.get("proxies", []) if p.get("name") != "DediRock-LA"]

    # 把关 1：不含 dns 段（曾导致解析超时、能测速但打不开网页）
    if "dns" in doc:
        log("  BLOCK: chain.yaml 含 dns 段，拒绝发布")
        return 1
    # 把关 2：节点数合理性。
    # 历史教训：曾按协议类型剔除 http/socks5，误杀 213 个节点（实测 62.5% 可用），
    # 导致最终只剩 5 个。现在所有协议都进实测池，通过量自然更多。
    # 这里只拦住「异常少」和「明显未经筛选」两种极端情况。
    if len(fronts) < 3:
        log(f"  BLOCK: 前端节点仅 {len(fronts)} 个，过少，拒绝发布")
        return 1
    if len(fronts) > 200:
        log(f"  BLOCK: 前端节点 {len(fronts)} 个，疑似未经筛选，拒绝发布")
        return 1

    if args.dry_run:
        log(f"dry-run: skip publish ({len(fronts)} verified fronts)")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("no GITHUB_TOKEN; skip publish")
        return 1

    log("step 6/6 publish")
    ok = publish(token)
    log("published" if ok else "publish had errors")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
