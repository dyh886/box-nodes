#!/usr/bin/env python3
"""
build_chain.py — 生成四层链式代理订阅（复刻用户原有方案）

架构：
    本机 → 前置节点 → 落地 107.172.186.223（唯一出口）→ 外网

四个界面（proxy-groups），名字与用户方案完全一致：
    前置        select   手动挑一个前置
    前置自动    url-test 自动测速选最快前置
    前置总控    select   链式锚点；落地节点的 dialer-proxy 指向它
    代理        select   最终出口（落地 / 静态住宅 / 总控 / DIRECT）

关键：落地节点带 `dialer-proxy: 前置总控`，客户端先用前置出网，
再由前置连到落地，落地是唯一出口 —— 这就是链式代理。
"""

import io
import json
import os
import sys
import time

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
EXTRACTED = os.path.join(ROOT, "extracted.json")

G_FRONT = "前置"
G_FRONT_AUTO = "前置自动"
G_FRONT_MASTER = "前置总控"
G_PROXY = "代理"

AUTO_LIMIT = 60
TEST_URL = "http://www.gstatic.com/generate_204"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr)


class Dumper(yaml.SafeDumper):
    """让中文与 emoji 原样输出，不转成 \\uXXXX。"""


def _str_representer(dumper, data):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _str_representer)


def build(data):
    front = data["front"]
    static = data["static"]
    landing = data["landing"]

    if not front:
        raise SystemExit("no front nodes")
    if not landing:
        raise SystemExit("no landing node")

    front_names = [p["name"] for p in front]

    proxies = []
    for p in front:
        q = dict(p)
        q.pop("dialer-proxy", None)
        proxies.append(q)

    for p in static:
        q = dict(p)
        q["dialer-proxy"] = G_FRONT_MASTER
        proxies.append(q)

    land = dict(landing)
    land["dialer-proxy"] = G_FRONT_MASTER
    proxies.append(land)
    landing_name = land["name"]

    seen, uniq = set(), []
    for p in proxies:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        uniq.append(p)
    proxies = uniq

    auto_pool = [p["name"] for p in proxies if p["name"] in front_names][:AUTO_LIMIT]

    groups = [
        {"name": G_FRONT, "type": "select", "proxies": front_names},
        {"name": G_FRONT_AUTO, "type": "url-test", "proxies": auto_pool,
         "url": TEST_URL, "interval": 180, "tolerance": 80},
        {"name": G_FRONT_MASTER, "type": "select",
         "proxies": [G_FRONT_AUTO, G_FRONT] + front_names},
        {"name": G_PROXY, "type": "select",
         "proxies": [landing_name] + [p["name"] for p in static]
                    + [G_FRONT_MASTER, "DIRECT"]},
    ]

    doc = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True,
            "ipv6": False,
            "listen": "0.0.0.0:1053",
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": [
                "*.lan", "*.local", "localhost.ptlogin2.qq.com",
                "+.msftconnecttest.com", "+.msftncsi.com",
                "time.*.com", "ntp.*.com",
            ],
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": ["https://223.5.5.5/dns-query",
                           "https://119.29.29.29/dns-query"],
            "proxy-server-nameserver": ["https://223.5.5.5/dns-query"],
            "fallback": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
            "fallback-filter": {
                "geoip": True, "geoip-code": "CN",
                "ipcidr": ["240.0.0.0/4", "0.0.0.0/32"],
            },
        },
        "proxies": proxies,
        "proxy-groups": groups,
        "rules": [
            "GEOIP,PRIVATE,DIRECT,no-resolve",
            f"MATCH,{G_PROXY}",
        ],
    }

    meta = {
        "front": len(front_names),
        "static": len(static),
        "landing": landing_name,
        "landingServer": land.get("server"),
        "groups": [g["name"] for g in groups],
        "totalProxies": len(proxies),
    }
    return doc, meta


def render(doc, meta):
    head = f"""# ============================================================
# box-nodes 链式代理订阅（四层结构）
# 生成: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
# 前置: {meta['front']}   静态住宅: {meta['static']}   总节点: {meta['totalProxies']}
# 唯一出口: {meta['landingServer']}
#
#   本机 -> 前置({G_FRONT_MASTER}) -> 落地 {meta['landingServer']} -> 外网
#
# 界面:
#   {G_FRONT}      手动选前置节点
#   {G_FRONT_AUTO} 自动测速选最快前置
#   {G_FRONT_MASTER} 链式锚点（落地的 dialer-proxy 指向它）
#   {G_PROXY}      最终出口，选「{meta['landing']}」走完整链路
# ============================================================
"""
    body = yaml.dump(doc, Dumper=Dumper, allow_unicode=True,
                     sort_keys=False, default_flow_style=False, width=4096)
    return head + body


def main():
    with io.open(EXTRACTED, encoding="utf-8") as f:
        data = json.load(f)

    doc, meta = build(data)
    text = render(doc, meta)

    for d in ("docs", "dist"):
        path = os.path.join(ROOT, d)
        os.makedirs(path, exist_ok=True)
        with io.open(os.path.join(path, "chain.yaml"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(text)

    log(f"front={meta['front']} static={meta['static']} "
        f"landing={meta['landing']}@{meta['landingServer']} "
        f"proxies={meta['totalProxies']}")
    log(f"groups: {' / '.join(meta['groups'])}")

    checks = []
    try:
        back = yaml.safe_load(text)
        checks.append(("YAML 可解析", True))
    except Exception as e:  # noqa: BLE001
        log(f"YAML parse FAIL: {e}")
        return 1

    names = [g["name"] for g in back["proxy-groups"]]
    checks.append(("四个组齐备", all(g in names for g in
                                    (G_FRONT, G_FRONT_AUTO, G_FRONT_MASTER, G_PROXY))))

    lnode = next((p for p in back["proxies"] if p["name"] == meta["landing"]), None)
    checks.append(("落地存在", lnode is not None))
    checks.append(("落地挂链式", bool(lnode) and
                   lnode.get("dialer-proxy") == G_FRONT_MASTER))
    checks.append(("出口唯一", bool(lnode) and
                   str(lnode.get("server")) == str(meta["landingServer"])))

    no_name = [p for p in back["proxies"] if not str(p.get("name") or "").strip()]
    checks.append(("无空名节点", len(no_name) == 0))

    dup = len(back["proxies"]) - len({p["name"] for p in back["proxies"]})
    checks.append(("无重名节点", dup == 0))
    checks.append(("MATCH 到 代理", f"MATCH,{G_PROXY}" in back["rules"]))

    ok_all = True
    for label, ok in checks:
        log(f"  [{'OK' if ok else 'FAIL'}] {label}")
        ok_all &= ok

    log(f"size={len(text)}B  written docs/chain.yaml + dist/chain.yaml")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
