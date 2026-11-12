#!/usr/bin/env python3
"""
build_split.py — A' 双轨方案：公开前置池 + 私有落地配置

核心设计：
  公开（GitHub Pages）
    fronts.yaml   只含前置节点池，不含任何落地凭据 → 可以安全公开
    meta.json     状态信息

  私有（本机，不入库）
    private-chain.yaml  完整配置，落地凭据在这里
    private-singbox.json  iOS 端完整配置

为什么这样能一次性解决：
  公开仓库里根本不存在落地密码，无从泄露。
  前置节点池仍每 4 小时自动更新，客户端通过 proxy-providers 拉取。

依赖：
  config.json 的 landing_node 提供落地凭据（不入库，已在 .gitignore）
"""

import io
import json
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
LIVE = ROOT / "state" / "live_nodes.json"
PRIVATE = ROOT / "private"      # 私有产物目录，必须在 .gitignore 里

G_FRONT, G_AUTO, G_MASTER, G_PROXY = "前置", "前置自动", "前置总控", "代理"
STRICT_URL = "https://www.google.com/generate_204"
PROVIDER_URL = "https://dyh886.github.io/box-nodes/fronts.yaml"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def load():
    with io.open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    with io.open(LIVE, encoding="utf-8") as f:
        live = json.load(f)
    return cfg, live


# --------------------------------------------------------------------------
# 公开产物：只有前置节点
# --------------------------------------------------------------------------

def build_fronts(nodes):
    """
    只输出前置节点，剔除一切 dialer-proxy / 本地标记字段。
    这份文件可以公开——机场节点本身就在各机场的公开订阅里。
    """
    DROP = {"dialer-proxy", "l2Ms", "l2Note", "l2Bonus", "qscore",
            "alive", "latencyMs", "realLatencyMs", "nodeLatencyMs", "probeMode"}
    out = []
    for n in nodes:
        q = {k: v for k, v in n.items() if k not in DROP}
        out.append(q)
    return {"proxies": out}


def build_meta(nodes, stats):
    return {
        "updatedAt": int(time.time()),
        "updatedAtISO": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "frontCount": len(nodes),
        "stats": stats,
        "note": "Node pool only. Landing credentials are not published.",
        "endpoints": {
            "provider": PROVIDER_URL,
            "pages": "https://dyh886.github.io/box-nodes/",
        },
    }


# --------------------------------------------------------------------------
# 私有产物：完整配置（含落地）
# --------------------------------------------------------------------------

def build_private_clash(landing, verified_names):
    """
    用 proxy-providers 引用公开前置池，落地凭据写在这里。
    这份文件不进仓库，只在本机使用。
    """
    doc = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "proxy-providers": {
            "fronts": {
                "type": "http",
                "url": PROVIDER_URL,
                "interval": 3600,
                "path": "./providers/fronts.yaml",
                # 关键：provider 必须直连拉取。
                # 否则 provider 请求走代理 → 代理需前置 → 前置未加载 = 死锁
                "proxy": "DIRECT",
                "size-limit": 4194304,
                "health-check": {
                    "enable": True,
                    "url": STRICT_URL,
                    "interval": 180,
                    "lazy": False,
                },
            }
        },
        "proxies": [{
            "name": landing["name"],
            "type": "ss",
            "server": landing["server"],
            "port": int(landing["port"]),
            "cipher": landing.get("cipher") or "chacha20-ietf-poly1305",
            "password": landing["password"],
            "udp": True,
            "dialer-proxy": G_MASTER,
        }],
        "proxy-groups": [
            {"name": G_FRONT, "type": "select", "use": ["fronts"]},
            {"name": G_AUTO, "type": "url-test", "use": ["fronts"],
             "url": STRICT_URL, "interval": 180, "tolerance": 80},
            {"name": G_MASTER, "type": "select",
             "proxies": [G_AUTO, G_FRONT]},
            {"name": G_PROXY, "type": "select",
             "proxies": [landing["name"], G_MASTER, "DIRECT"]},
        ],
        "rules": ["GEOIP,PRIVATE,DIRECT,no-resolve", f"MATCH,{G_PROXY}"],
    }
    return doc


def render_clash(doc, landing, count):
    head = f"""# ============================================================
# 私有链式代理配置（勿提交到公开仓库）
# 生成: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
# 前置: 由 provider 自动拉取（{count} 个）
# 唯一出口: {landing['server']}:{landing['port']}
#
#   本机 -> 前置(前置总控) -> 落地 -> 外网
#
# 前置节点池来源: {PROVIDER_URL}
# 该文件不含落地密码以外的敏感信息；落地密码仅存于此文件与 config.json
# ============================================================
"""
    body = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=4096)
    return head + body


def main():
    cfg, live = load()
    landing = cfg.get("landing_node")
    if not landing:
        log("config.json missing landing_node")
        return 1

    nodes = live.get("nodes") or []
    if not nodes:
        log("no nodes in live_nodes.json")
        return 1

    stats = live.get("stats", {})
    PRIVATE.mkdir(exist_ok=True)
    (ROOT / "docs").mkdir(exist_ok=True)

    # ---- 公开：前置池 ----
    fronts = build_fronts(nodes)
    fronts_text = yaml.safe_dump(fronts, allow_unicode=True, sort_keys=False,
                                 default_flow_style=False, width=4096)
    for d in ("docs", "dist"):
        p = ROOT / d
        p.mkdir(exist_ok=True)
        (p / "fronts.yaml").write_text(fronts_text, encoding="utf-8", newline="\n")

    meta = build_meta(nodes, stats)
    for d in ("docs", "dist"):
        (ROOT / d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n")

    log(f"public: fronts.yaml {len(nodes)} nodes "
        f"({len(fronts_text)}B, no landing creds)")

    # ---- 私有：完整配置 ----
    private_clash = build_private_clash(landing, [n["name"] for n in nodes])
    text = render_clash(private_clash, landing, len(nodes))
    (PRIVATE / "private-chain.yaml").write_text(text, encoding="utf-8", newline="\n")
    log(f"private: private-chain.yaml ({len(text)}B)")

    # ---- 自检 ----
    checks = []

    # 1. 公开文件绝不含落地密码
    pw = landing["password"]
    checks.append(("公开 fronts.yaml 无落地密码", pw not in fronts_text))
    checks.append(("公开 fronts.yaml 无落地 IP",
                   str(landing["server"]) not in fronts_text))
    checks.append(("公开 fronts.yaml 无 dialer-proxy",
                   "dialer-proxy" not in fronts_text))

    # 2. provider 配置正确
    prov = private_clash["proxy-providers"]["fronts"]
    checks.append(("provider 直连拉取", prov.get("proxy") == "DIRECT"))
    checks.append(("provider 指向公开地址", prov["url"] == PROVIDER_URL))

    # 3. 落地挂链式
    ln = private_clash["proxies"][0]
    checks.append(("落地挂 前置总控", ln.get("dialer-proxy") == G_MASTER))
    checks.append(("落地端口为新端口", int(ln["port"]) == int(landing["port"])))

    # 4. 四组齐备且用 provider
    names = [g["name"] for g in private_clash["proxy-groups"]]
    checks.append(("四组齐备", names == [G_FRONT, G_AUTO, G_MASTER, G_PROXY]))
    checks.append(("前置组用 provider",
                   private_clash["proxy-groups"][0].get("use") == ["fronts"]))

    # 5. 私有配置确实含凭据（说明分离正确）
    checks.append(("私有配置含落地密码", pw in text))

    ok = True
    for label, passed in checks:
        log(f"  [{'OK' if passed else 'FAIL'}] {label}")
        ok &= passed

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
