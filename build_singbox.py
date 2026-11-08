#!/usr/bin/env python3
"""
build_singbox.py — 由 live_nodes.json 生成 sing-box JSON 订阅

为什么需要：
  chain.yaml 是 Clash/mihomo 格式。iOS 官方 sing-box 客户端只吃 JSON，
  不认 Clash YAML。所以同一套节点要并行产出 sing-box 格式。

映射关系（与 chain.yaml 的四层一一对应）：
  Clash                          sing-box
  ------------------------------  -------------------------------------
  proxies: [前置...]              outbounds: n00..nNN
  proxy-groups 前置 (select)      outbound type=selector  tag=前置
  proxy-groups 前置自动 (urltest) outbound type=urltest   tag=前置自动
  proxy-groups 前置总控 (select)  outbound type=selector  tag=前置总控
  落地 dialer-proxy: 前置总控     落地 outbound detour: 前置总控
  proxy-groups 代理 (select)      outbound type=selector  tag=代理
  rules: MATCH,代理               route.final = 代理
"""

import io
import json
import os
import sys
import time

import yaml  # noqa: F401  (保留依赖一致性；本文件只用 json)

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
LIVE = os.path.join(ROOT, "state", "live_nodes.json")

G_FRONT = "前置"
G_AUTO = "前置自动"
G_MASTER = "前置总控"
G_PROXY = "代理"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def load_cfg():
    with io.open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def build_tls(n):
    """把 Clash 的 tls/servername/reality-opts 映射到 sing-box 的 tls 对象。"""
    typ = str(n.get("type", "")).lower()
    want = bool(n.get("tls")) or typ in ("trojan", "hysteria2", "tuic")
    reality = n.get("reality-opts") or {}
    if not want and not reality:
        return None

    tls = {"enabled": True}
    sni = n.get("servername") or n.get("sni")
    if sni:
        tls["server_name"] = sni

    if n.get("skip-cert-verify") or n.get("insecure"):
        tls["insecure"] = True

    if reality:
        tls["reality"] = {
            "enabled": True,
            "public_key": reality.get("public-key", ""),
            "short_id": reality.get("short-id", ""),
        }

    fp = n.get("client-fingerprint")
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    return tls


def build_transport(n):
    net = str(n.get("network") or "").lower()
    if net == "ws":
        opts = n.get("ws-opts") or {}
        t = {"type": "ws", "path": opts.get("path") or "/"}
        hdrs = opts.get("headers") or {}
        host = hdrs.get("Host") or n.get("host")
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        return {"type": "grpc",
                "service_name": (n.get("grpc-opts") or {}).get("grpc-service-name", "")}
    if net == "h2":
        return {"type": "http"}
    return None


def to_outbound(n, tag):
    """单节点 → sing-box outbound。无法映射时返回 None。"""
    typ = str(n.get("type", "")).lower()
    server = n.get("server")
    port = int(n.get("port") or 0)
    if not server or not port:
        return None

    o = {"tag": tag}

    if typ == "vmess":
        o.update({"type": "vmess", "server": server, "server_port": port,
                  "uuid": n.get("uuid"), "security": n.get("cipher") or "auto",
                  "alter_id": int(n.get("alterId") or 0)})
    elif typ == "vless":
        o.update({"type": "vless", "server": server, "server_port": port,
                  "uuid": n.get("uuid")})
        if n.get("flow"):
            o["flow"] = n["flow"]
    elif typ == "trojan":
        o.update({"type": "trojan", "server": server, "server_port": port,
                  "password": n.get("password") or ""})
    elif typ == "ss":
        o.update({"type": "shadowsocks", "server": server, "server_port": port,
                  "method": n.get("cipher") or "aes-256-gcm",
                  "password": n.get("password") or ""})
    elif typ == "hysteria2":
        o.update({"type": "hysteria2", "server": server, "server_port": port,
                  "password": n.get("password") or ""})
    elif typ == "tuic":
        o.update({"type": "tuic", "server": server, "server_port": port,
                  "uuid": n.get("uuid") or "",
                  "password": n.get("password") or ""})
    elif typ == "http":
        o.update({"type": "http", "server": server, "server_port": port})
        if n.get("username"):
            o["username"] = n["username"]
        if n.get("password"):
            o["password"] = n["password"]
    elif typ == "socks5":
        o.update({"type": "socks", "version": "5", "server": server,
                  "server_port": port})
        if n.get("username"):
            o["username"] = n["username"]
        if n.get("password"):
            o["password"] = n["password"]
    else:
        return None

    tls = build_tls(n)
    if tls:
        o["tls"] = tls
    tr = build_transport(n)
    if tr:
        o["transport"] = tr
    return o


def main():
    cfg = load_cfg()
    landing_cfg = cfg.get("landing_node")
    if not landing_cfg:
        log("config.json missing landing_node")
        return 1

    with io.open(LIVE, encoding="utf-8") as f:
        data = json.load(f)
    nodes = data["nodes"]
    if not nodes:
        log("no nodes")
        return 1

    outbounds = []
    tags = []
    for i, n in enumerate(nodes):
        tag = f"n{i:02d}"
        o = to_outbound(n, tag)
        if o:
            outbounds.append(o)
            tags.append(tag)

    log(f"mapped {len(tags)}/{len(nodes)} nodes")

    # 落地
    ltag = landing_cfg.get("name") or "落地"
    landing = {
        "type": "shadowsocks",
        "tag": ltag,
        "server": landing_cfg["server"],
        "server_port": int(landing_cfg["port"]),
        "method": landing_cfg.get("cipher") or "chacha20-ietf-poly1305",
        "password": landing_cfg.get("password") or "",
        "detour": G_MASTER,
    }
    outbounds.append(landing)

    # 四层组
    outbounds.append({"type": "selector", "tag": G_FRONT,
                      "outbounds": tags, "default": tags[0]})
    outbounds.append({"type": "urltest", "tag": G_AUTO,
                      "outbounds": tags,
                      "url": "http://www.gstatic.com/generate_204",
                      "interval": "5m", "tolerance": 80})
    outbounds.append({"type": "selector", "tag": G_MASTER,
                      "outbounds": [G_AUTO, G_FRONT] + tags,
                      "default": G_AUTO})
    outbounds.append({"type": "selector", "tag": G_PROXY,
                      "outbounds": [ltag, G_MASTER, "direct"],
                      "default": ltag})
    outbounds.append({"type": "direct", "tag": "direct"})

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    doc = {
        "log": {"level": "warn", "timestamp": True},
        # sing-box 1.12+ 要求新式 DNS server 格式：
        #   旧：{"address": "https://1.1.1.1/dns-query"}  → 1.12 弃用、1.14 移除
        #   新：{"type": "https", "server": "1.1.1.1"}
        #
        # 注意：不要给 DNS server 写 detour: "direct"。
        # sing-box 会报 "detour to an empty direct outbound makes no sense"
        # ——空的 direct 出站没有参数，当 detour 目标无意义。省略即走直连。
        "dns": {
            "servers": [
                {"type": "https", "tag": "dns-proxy",
                 "server": "1.1.1.1", "detour": G_PROXY},
                {"type": "https", "tag": "dns-direct",
                 "server": "223.5.5.5"},
            ],
            "final": "dns-proxy",
        },
        "inbounds": [
            {"type": "tun", "tag": "tun-in",
             "address": ["172.19.0.1/30"], "auto_route": True,
             "strict_route": True, "stack": "mixed"},
            {"type": "mixed", "tag": "mixed-in",
             "listen": "127.0.0.1", "listen_port": 2080},
        ],
        "outbounds": outbounds,
        "route": {
            "rules": [
                {"action": "route", "ip_is_private": True, "outbound": "direct"},
            ],
            "final": G_PROXY,
            "auto_detect_interface": True,
            # 1.12 起 outbound 解析域名要显式指定 resolver，否则域名节点无法解析
            "default_domain_resolver": "dns-direct",
        },
        "experimental": {
            "cache_file": {"enabled": True},
        },
    }

    text = json.dumps(doc, ensure_ascii=False, indent=2)

    for d in ("docs", "dist"):
        p = os.path.join(ROOT, d)
        os.makedirs(p, exist_ok=True)
        with io.open(os.path.join(p, "singbox.json"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(text)

    # 自检
    back = json.loads(text)
    tags_all = [o["tag"] for o in back["outbounds"]]
    lnode = next((o for o in back["outbounds"] if o["tag"] == ltag), None)

    dns_servers = back.get("dns", {}).get("servers", [])
    dns_new_format = all(
        isinstance(s, dict) and "type" in s and "address" not in s
        for s in dns_servers
    )
    # 不能有 detour 指向空 direct
    dns_bad_detour = any(
        isinstance(s, dict) and s.get("detour") == "direct"
        for s in dns_servers
    )

    checks = [
        ("JSON 可解析", True),
        ("DNS 用 1.12+ 新格式", dns_new_format),
        ("DNS 无 detour:direct", not dns_bad_detour),
        ("四组齐备", all(g in tags_all for g in (G_FRONT, G_AUTO, G_MASTER, G_PROXY))),
        ("落地挂 detour", bool(lnode) and lnode.get("detour") == G_MASTER),
        ("落地端口正确", bool(lnode) and lnode["server_port"] == int(cfg["landing_node"]["port"])),
        ("route.final 指向代理", back["route"]["final"] == G_PROXY),
        ("有 default_domain_resolver", "default_domain_resolver" in back["route"]),
        ("无 legacy clash_api", "clash_api" not in back.get("experimental", {})),
    ]
    ok = True
    for label, c in checks:
        log(f"  [{'OK' if c else 'FAIL'}] {label}")
        ok &= c

    log(f"written docs/singbox.json + dist/singbox.json  ({len(tags)} nodes, {len(text)}B)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
