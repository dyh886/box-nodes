#!/usr/bin/env python3
"""
sync_split.py — A' 双轨同步（本机版）

职责：
  1. 拉三个机场订阅 → 聚合去重
  2. 端到端验证（GitHub 200）+ 三轮稳定性
  3. 生成公开 fronts.yaml（只含前置节点）+ 私有 private-chain.yaml
  4. 发布公开部分到 GitHub
  5. 可选：把私有配置复制到 FlClash profile 供本机使用

与旧 sync_local.py 的区别：
  不再发布 chain.yaml（含落地密码），只发布 fronts.yaml（纯前置池）。
  落地凭据只存在本机 private/ 与 config.json，永不入库。
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


def run(args, timeout=3600):
    return subprocess.run([sys.executable, *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=ENV, timeout=timeout)


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
        except Exception:
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
                         "--data-binary", f"@{tmp}", f"{API}/{path}"], 120)
    try:
        os.remove(tmp)
    except OSError:
        pass
    ok = bool(rc == 0 and out.strip()
              and ("content" in out or "commit" in out))
    log(f"  {'OK ' if ok else 'ERR'} [{branch}] {path}")
    return ok


def publish(token):
    """只发布不含凭据的公开文件，并写入心跳供云机判断。"""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    msg = f"chore: refresh front pool @ {stamp}"
    ok = True
    fronts = ROOT / "docs" / "fronts.yaml"
    meta = ROOT / "docs" / "meta.json"
    if not fronts.exists():
        log("  fronts.yaml missing")
        return False
    fb = fronts.read_bytes()

    # 安全把关：绝不能含落地凭据
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    land = cfg["landing_node"]
    ftxt = fb.decode("utf-8", "replace")
    for label, needle in (("落地密码", land["password"]),
                          ("落地IP", str(land["server"])),
                          ("dialer-proxy", "dialer-proxy")):
        if needle in ftxt:
            log(f"  BLOCK: fronts.yaml 含{label}，拒绝发布")
            return False

    for br, p in (("main", "docs/fronts.yaml"),
                  ("main", "dist/fronts.yaml"),
                  ("gh-pages", "fronts.yaml")):
        ok &= gh_put(br, p, fb, msg, token)

    if meta.exists():
        mb = meta.read_bytes()
        for br, p in (("main", "docs/meta.json"), ("gh-pages", "meta.json")):
            ok &= gh_put(br, p, mb, msg, token)

    # 心跳：告诉云机「本机活着」，避免它用未验证节点覆盖
    if ok:
        hb = {
            "updatedAt": int(time.time()),
            "updatedAtISO": stamp,
            "frontCount": ftxt.count("- name:"),
            "source": "local (verified)",
            "staleAfterHours": 8,
        }
        hb_bytes = json.dumps(hb, ensure_ascii=False, indent=2).encode("utf-8")
        ok &= gh_put("main", "state/heartbeat.json", hb_bytes,
                     f"chore: heartbeat @ {stamp}", token)
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-verify", action="store_true",
                    help="跳过验证，仅用现有 state 重建")
    ap.add_argument("--top", type=int, default=400)
    args = ap.parse_args()

    if not args.skip_verify:
        log("step 1/5 fetch subscriptions")
        p = run(["build_live.py", "fetch"], timeout=600)
        for ln in (p.stderr or "").strip().splitlines()[-4:]:
            log("  " + ln)

        log("step 2/5 rank candidates")
        p = run(["build_live.py", "rank", "--top", str(args.top)], timeout=300)
        for ln in (p.stderr or "").strip().splitlines()[-2:]:
            log("  " + ln)

        log("step 3/5 L2 verify (github required)")
        p = run(["l2_verify.py", "--batch", "10"], timeout=3600)
        for ln in (p.stderr or "").strip().splitlines()[-2:]:
            log("  " + ln)
        live = json.loads((ROOT / "state" / "live_nodes.json").read_text(encoding="utf-8"))
        if live.get("count", 0) == 0:
            log("  no node passed L2 — abort, keep previous")
            return 1
        log(f"  L2 passed: {live['count']}")

        log("step 4/5 stability (3 rounds)")
        p = run(["stability_check.py"], timeout=3600)
        for ln in (p.stdout or "").strip().splitlines()[-2:]:
            log("  " + ln)

        log("  select stable")
        p = run(["select_and_build.py"], timeout=300)
        for ln in (p.stdout or "").strip().splitlines()[:5]:
            log("  " + ln)
        if p.returncode != 0:
            log("  select failed")
            return 1

    log("step 5/5 build split artifacts")
    p = run(["build_split.py"], timeout=300)
    for ln in (p.stderr or "").strip().splitlines():
        log("  " + ln)
    if p.returncode != 0:
        log("  build_split failed")
        return 1

    if args.dry_run:
        log("dry-run: skip publish")
        return 0

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("no GITHUB_TOKEN")
        return 1

    log("publish (front-only)")
    ok = publish(token)
    log("published" if ok else "publish failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
