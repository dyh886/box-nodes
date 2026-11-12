#!/usr/bin/env python3
"""
云机侧：只在前置池过期时才发布（兜底角色）。

协调机制（本机优先 + 云机兜底）：
  本机每次成功发布 fronts.yaml 时，同时写入 state/heartbeat.json，
  内含 updateAt 时间戳，并发布到公开地址。
  云机运行前先读该文件：
    - 若 age < STALE_HOURS（默认 8 小时）→ 本机活跃，跳过发布
    - 若 age >= STALE_HOURS 或读不到 → 本机已离线，接管发布

  8 小时 = 本机 4 小时周期的两倍，允许本机错过一次而不误判。

为什么这样设计：
  云机出口只放行 443/22，无法验证节点能否打通落地（新端口 19443）。
  它发布的 295 个节点未经验证，质量低于本机筛过的。
  所以只在必要时接管，平时让本机的验证结果作数。
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "state" / "live_nodes.json"
API = "https://api.github.com/repos/dyh886/box-nodes/contents"
HEARTBEAT_URL = "https://raw.githubusercontent.com/dyh886/box-nodes/main/state/heartbeat.json"
STALE_HOURS = float(os.environ.get("STALE_HOURS", "8"))


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def curl(args, t=90):
    import subprocess
    # 云机无直连出口，必须走代理（不传 --noproxy）
    p = subprocess.run(["curl", "-s", "--max-time", str(t)] + args,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode, p.stdout, p.stderr


def local_age_hours():
    """读取本机心跳，返回距上次更新的小时数；读不到返回 None。"""
    rc, out, _ = curl([HEARTBEAT_URL], 30)
    if rc != 0 or not out.strip():
        return None
    try:
        hb = json.loads(out)
        ts = hb.get("updatedAt")
        if not ts:
            return None
        return (time.time() - float(ts)) / 3600.0
    except Exception:
        return None


def build_fronts(nodes):
    DROP = {"dialer-proxy", "l2Ms", "l2Note", "l2Bonus", "qscore",
            "alive", "latencyMs", "realLatencyMs", "nodeLatencyMs", "probeMode"}
    return {"proxies": [{k: v for k, v in n.items() if k not in DROP} for n in nodes]}


def main():
    # ---- 第 0 步：检查本机是否活跃 ----
    age = local_age_hours()
    if age is None:
        log(f"heartbeat unreadable -> 接管发布")
    elif age < STALE_HOURS:
        log(f"本机 {age:.1f}h 前刚更新过（阈值 {STALE_HOURS}h）→ 跳过，由本机负责")
        return 0
    else:
        log(f"本机已 {age:.1f}h 未更新（阈值 {STALE_HOURS}h）→ 云机接管发布")

    sys.path.insert(0, str(ROOT))
    import build_live as bl

    rc = bl.cmd_fetch()
    if rc != 0:
        log("fetch failed")
        return 1

    data = json.loads(LIVE.read_text(encoding="utf-8"))
    nodes = data.get("nodes") or []
    if not nodes:
        log("no nodes")
        return 1
    log(f"aggregated {len(nodes)} nodes")

    doc = build_fronts(nodes)
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=4096)

    # 安全把关：绝不发布落地特征
    cfg_path = ROOT / "config.json"
    if cfg_path.exists():
        try:
            land = json.loads(cfg_path.read_text(encoding="utf-8"))["landing_node"]
            for label, needle in (("落地密码", land.get("password", "")),
                                  ("落地IP", str(land.get("server", "")))):
                if needle and needle in text:
                    log(f"BLOCK: fronts.yaml 含{label}，拒绝发布")
                    return 1
        except Exception as e:
            log(f"config check skipped: {e}")
    if "dialer-proxy" in text:
        log("BLOCK: fronts.yaml 含 dialer-proxy")
        return 1

    meta = {
        "updatedAt": int(time.time()),
        "updatedAtISO": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "frontCount": len(nodes),
        "generator": "box (fallback: aggregate only, no L2 verify)",
        "note": "Front-only pool. Landing credentials are never published.",
    }

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("no GITHUB_TOKEN — files written locally only")
        for d in ("docs", "dist"):
            p = ROOT / d
            p.mkdir(exist_ok=True)
            (p / "fronts.yaml").write_text(text, encoding="utf-8", newline="\n")
        return 0

    import base64
    import tempfile

    def put(branch, path, data_bytes, msg):
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
                   "content": base64.b64encode(data_bytes).decode(),
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

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    msg = f"chore: front pool refresh (box fallback) @ {stamp}"
    fb = text.encode("utf-8")
    ok = True
    for br, p in (("main", "docs/fronts.yaml"),
                  ("main", "dist/fronts.yaml"),
                  ("gh-pages", "fronts.yaml")):
        ok &= put(br, p, fb, msg)

    mb = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    for br, p in (("main", "docs/meta.json"), ("gh-pages", "meta.json")):
        ok &= put(br, p, mb, msg)

    log("published (fallback)" if ok else "publish failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
