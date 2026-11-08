#!/usr/bin/env python3
"""
extract_source.py — 用 PyYAML 从 FlClash 机场 profile 提取真实节点。

修正了手写解析器的两个致命问题：
  1. `alpn:\n  - http/1.1` 这类列表项被误判成新节点 → 产生空名假节点
  2. 原始块缩进不统一（有的顶格、有的两格）→ 拼接后 YAML 非法

现在只输出结构化 dict，由 build_chain.py 重新序列化，不再复用原始文本。
"""

import io
import json
import os
import sys

import yaml

SRC = os.path.expandvars(
    r"%APPDATA%\com.follow\clash\profiles\377007008550752256.yaml"
)
LANDSRC = os.path.expandvars(
    r"%APPDATA%\com.follow\clash\profiles\999888777666555444.yaml"
)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "extracted.json")

LANDING_IP = "107.172.186.223"


def load_yaml(path):
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return yaml.safe_load(f)


def main():
    if not os.path.exists(SRC):
        raise SystemExit(f"source profile not found: {SRC}")

    doc = load_yaml(SRC)
    proxies = doc.get("proxies") or []
    print(f"airport proxies: {len(proxies)}", file=sys.stderr)

    front, static, bad = [], [], []
    for p in proxies:
        if not isinstance(p, dict):
            bad.append(p)
            continue
        name = str(p.get("name") or "").strip()
        server = p.get("server")
        if not name or not server:
            bad.append(p)
            continue

        rec = dict(p)
        dp = str(p.get("dialer-proxy") or "")
        if dp and "前置" in dp:
            static.append(rec)
        elif name.startswith("US-静态") or (len(name) > 3 and name[2:5] == "-静态"):
            static.append(rec)
        else:
            front.append(rec)

    print(f"  front={len(front)} static={len(static)} bad={len(bad)}", file=sys.stderr)

    landing = None
    if os.path.exists(LANDSRC):
        ldoc = load_yaml(LANDSRC)
        for p in (ldoc.get("proxies") or []):
            if isinstance(p, dict) and str(p.get("server")) == LANDING_IP:
                landing = p
                break
    if not landing:
        landing = {
            "name": "DediRock-LA",
            "type": "vless",
            "server": LANDING_IP,
            "port": 443,
            "uuid": "5dc61111-0287-4105-87b4-861e5f738505",
            "network": "tcp",
            "udp": True,
            "tls": True,
            "flow": "xtls-rprx-vision",
            "servername": "www.apple.com",
            "reality-opts": {
                "public-key": "IOtwseqVpH6GUAOGEdclaVBlVbRzE96G7WCRWLMhEws",
                "short-id": "c9c3225f8a9980ef",
            },
            "client-fingerprint": "chrome",
        }
        print("  landing: rebuilt from template", file=sys.stderr)
    else:
        print(f"  landing: {landing.get('name')}", file=sys.stderr)

    payload = {
        "sourceProfile": os.path.basename(SRC),
        "front": front,
        "static": static,
        "landing": landing,
        "counts": {"front": len(front), "static": len(static), "total": len(proxies)},
    }
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"written: {OUT}", file=sys.stderr)

    m = landing
    for k in ("name", "type", "server", "port", "network", "tls", "flow", "servername"):
        print(f"  {k}: {m.get(k, '')}", file=sys.stderr)


if __name__ == "__main__":
    main()
