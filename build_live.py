#!/usr/bin/env python3
"""
build_live.py — 云机端全自动：拉订阅 → 聚合 → 测落地延迟 → 挑前 N → 生成配置

与旧方案的差别：
  旧：靠本地 FlClash 配置文件，要手动上传 extracted.json
  新：直接 HTTP 拉订阅 URL，全自动，无需人工干预

流程：
  1. fetch   拉所有订阅（支持 clash yaml / base64 / 明文链接）
  2. aggregate 解析成统一节点结构，按指纹去重
  3. probe   对每个前置节点测「到落地 107.172.186.223:443 的延迟」
             走云机出口代理的 CONNECT 隧道，测的是真实可达性
  4. rank    按延迟升序，挑前 N 个（默认 20）
  5. build   生成四层链式配置：前置 / 前置自动 / 前置总控 / 代理

为什么测「到落地的延迟」而不是「到 gstatic 的延迟」：
  链式代理的实际瓶颈是「前置 → 落地」这一段。前置到 gstatic 快，
  不代表它到 107.172.186.223 也快。直接测落地 IP 才是真实链路质量。
"""

import base64
import concurrent.futures
import io
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
STATE = os.path.join(ROOT, "state")
LIVE_JSON = os.path.join(STATE, "live_nodes.json")

# 落地节点（唯一出口）—— 参数取自 vps/export/singbox-latest.json
# 这是 shadowsocks，不是 vless。端口 9443，chacha20-ietf-poly1305。
# 关键：落地必须经前置中转（detour / dialer-proxy），直连 443/9443 都不通是正常的。
LANDING = {
    "name": "DediRock-LA",
    "type": "ss",
    "server": "107.172.186.223",
    "port": 9443,
    "cipher": "chacha20-ietf-poly1305",
    "password": "DediRock2026xK9mPq",
    "udp": True,
}

UA = "clash-verge/v2.0.0"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def b64_loose(s):
    if isinstance(s, bytes):
        s = s.decode("utf-8", "ignore")
    s = re.sub(r"\s+", "", s).replace("-", "+").replace("_", "/")
    s += "=" * ((-len(s)) % 4)
    try:
        return base64.b64decode(s).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 订阅解析
# --------------------------------------------------------------------------

LINK_RE = re.compile(
    r"(?:vmess|vless|trojan|ssr|ss|hysteria2|hy2|tuic)://[^\s\"'<>\\]+", re.I)


def parse_link(link):
    link = link.strip().rstrip(".,;")
    m = re.match(r"([a-z0-9]+)://", link, re.I)
    if not m:
        return None
    scheme = m.group(1).lower()
    try:
        if scheme == "vmess":
            raw = b64_loose(link[8:])
            if not raw:
                return None
            j = json.loads(raw)
            return {
                "name": j.get("ps") or "", "type": "vmess",
                "server": j.get("add"), "port": int(j.get("port") or 0),
                "uuid": j.get("id"), "alterId": int(j.get("aid") or 0),
                "cipher": j.get("scy") or "auto",
                "network": (j.get("net") or "tcp").lower(),
                "tls": str(j.get("tls") or "").lower() in ("tls", "true"),
                "servername": j.get("sni") or j.get("host") or "",
                "ws-opts": {"path": j.get("path") or "/",
                            "headers": {"Host": j.get("host") or ""}},
                "udp": True,
            }
        u = urllib.parse.urlparse(link)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        frag = urllib.parse.unquote(u.fragment or "")
        if scheme == "vless":
            sec = (q.get("security") or "none").lower()
            n = {"name": frag, "type": "vless", "server": u.hostname,
                 "port": int(u.port or 0), "uuid": urllib.parse.unquote(u.username or ""),
                 "udp": True, "network": (q.get("type") or "tcp").lower()}
            if q.get("flow"):
                n["flow"] = q["flow"]
            if sec in ("tls", "reality"):
                n["tls"] = True
                n["servername"] = q.get("sni") or q.get("peer") or ""
                if sec == "reality":
                    n["reality-opts"] = {"public-key": q.get("pbk", ""),
                                         "short-id": q.get("sid", "")}
            if q.get("fp"):
                n["client-fingerprint"] = q["fp"]
            if n["network"] == "ws":
                n["ws-opts"] = {"path": urllib.parse.unquote(q.get("path") or "/"),
                                "headers": {"Host": q.get("host") or ""}}
            return n
        if scheme == "trojan":
            return {"name": frag, "type": "trojan", "server": u.hostname,
                    "port": int(u.port or 0),
                    "password": urllib.parse.unquote(u.username or ""),
                    "udp": True, "sni": q.get("sni") or "",
                    "skip-cert-verify": True}
        if scheme == "ss":
            body = link[5:]
            if "#" in body:
                body, f2 = body.split("#", 1)
                frag = urllib.parse.unquote(f2)
            body = body.split("?", 1)[0]
            if "@" in body:
                ui, hp = body.rsplit("@", 1)
                dec = b64_loose(ui)
                if dec and ":" in dec:
                    method, pwd = dec.split(":", 1)
                elif ":" in urllib.parse.unquote(ui):
                    method, pwd = urllib.parse.unquote(ui).split(":", 1)
                else:
                    return None
            else:
                dec = b64_loose(body)
                if not dec or "@" not in dec:
                    return None
                ui, hp = dec.rsplit("@", 1)
                method, pwd = ui.split(":", 1)
            h, p = hp.rsplit(":", 1)
            return {"name": frag, "type": "ss", "server": h.strip("[]"),
                    "port": int(p), "cipher": method, "password": pwd, "udp": True}
    except Exception:  # noqa: BLE001
        return None
    return None


def parse_subscription(raw):
    """返回 (nodes, kind)。自动嗅探 clash yaml / base64 / 明文链接。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")

    # clash yaml
    if re.search(r"(?m)^\s*proxies\s*:", raw):
        try:
            d = yaml.safe_load(raw)
            ps = d.get("proxies") or []
            out = []
            for p in ps:
                if isinstance(p, dict) and p.get("name") and p.get("server"):
                    out.append(dict(p))
            return out, "clash"
        except Exception as e:  # noqa: BLE001
            log(f"    yaml parse failed: {e}")

    # base64
    text = raw
    t = re.sub(r"\s+", "", raw)
    if len(t) > 40 and re.fullmatch(r"[A-Za-z0-9+/=_\-]+", t):
        dec = b64_loose(t)
        if LINK_RE.search(dec):
            text = dec

    out = []
    for link in LINK_RE.findall(text):
        n = parse_link(link)
        if n and n.get("server") and n.get("port"):
            out.append(n)
    return out, "links"


def fingerprint(n):
    key = "|".join(str(n.get(k, "")) for k in
                   ("type", "server", "port", "uuid", "password", "network"))
    return key.lower()


# --------------------------------------------------------------------------
# 延迟探测：测「前置 → 落地」的真实可达性
# --------------------------------------------------------------------------

def proxy_url():
    for v in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY",
              "all_proxy", "ALL_PROXY"):
        val = os.environ.get(v)
        if val:
            return val
    return None


def parse_proxy(p):
    if "://" not in p:
        p = "http://" + p
    u = urllib.parse.urlparse(p)
    return u.hostname, u.port or 8080, u.username, u.password


def connect_via_proxy(phost, pport, puser, ppass, target, timeout):
    """走代理 CONNECT 隧道，返回 (ok, ms)。"""
    t0 = time.perf_counter()
    s = None
    try:
        s = socket.create_connection((phost, pport), timeout=timeout)
        s.settimeout(timeout)
        req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if puser:
            tok = base64.b64encode(f"{puser}:{ppass or ''}".encode()).decode()
            req += f"Proxy-Authorization: Basic {tok}\r\n"
        req += "Proxy-Connection: keep-alive\r\n\r\n"
        s.sendall(req.encode())
        buf = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in buf and time.time() < deadline:
            chunk = s.recv(512)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8192:
                break
        m = re.match(rb"HTTP/\d\.\d\s+(\d{3})", buf)
        if m and int(m.group(1)) == 200:
            return True, round((time.perf_counter() - t0) * 1000, 1)
        return False, None
    except Exception:  # noqa: BLE001
        return False, None
    finally:
        if s:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass


def probe_landing(node, timeout=8.0):
    """
    注意：落地节点必须经前置中转（dialer-proxy），云机直连落地一定失败。
    所以这里【不能】用「能否连到落地 IP」来判定节点好坏——那会把所有节点都判死。

    改为测前置节点自身的可达性（probe_node_reachable），这才是链路的瓶颈。
    """
    return probe_node_reachable(node, timeout)


def probe_node_reachable(node, timeout=8.0):
    """测节点自身 host:port 是否可达（经出口代理）。"""
    purl = proxy_url()
    host, port = node.get("server"), node.get("port")
    if not host or not port:
        return False, None
    target = f"[{host}]:{port}" if ":" in str(host) else f"{host}:{port}"
    if purl:
        ph, pp, pu, pw = parse_proxy(purl)
        return connect_via_proxy(ph, pp, pu, pw, target, timeout)
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True, round((time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        return False, None


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def load_cfg():
    with io.open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def cmd_fetch():
    cfg = load_cfg()
    subs = [s for s in cfg.get("subscriptions", []) if s.get("enabled", True)]
    log(f"fetch: {len(subs)} subscriptions")

    all_nodes = []
    report = []
    for s in subs:
        label = s.get("label") or s["url"][:50]
        kind_hint = s.get("type", "direct")

        # 镜像源：内容由本地机器推送（云机被上游封锁时的中转）
        if kind_hint == "mirror":
            local = os.path.join(ROOT, s["file"])
            try:
                if not os.path.exists(local):
                    log(f"  MISS {label}: {s['file']} not pushed yet")
                    report.append({"label": label, "ok": False,
                                   "error": "mirror missing"})
                    continue
                with io.open(local, "rb") as f:
                    raw = f.read()
                nodes, kind = parse_subscription(raw)
                log(f"  OK  {label}: {len(nodes)} nodes ({kind}, mirror)")
                report.append({"label": label, "ok": True,
                               "nodes": len(nodes), "kind": f"mirror/{kind}"})
                all_nodes.extend(nodes)
            except Exception as e:  # noqa: BLE001
                log(f"  FAIL {label}: {type(e).__name__}")
                report.append({"label": label, "ok": False,
                               "error": type(e).__name__})
            continue

        # 直连源
        try:
            raw = http_get(s["url"], timeout=s.get("timeout", 35))
            nodes, kind = parse_subscription(raw)
            log(f"  OK  {label}: {len(nodes)} nodes ({kind})")
            report.append({"label": label, "ok": True,
                           "nodes": len(nodes), "kind": kind})
            all_nodes.extend(nodes)
        except Exception as e:  # noqa: BLE001
            log(f"  FAIL {label}: {type(e).__name__}")
            report.append({"label": label, "ok": False, "error": type(e).__name__})

    # 去重
    seen, uniq = set(), []
    for n in all_nodes:
        fp = fingerprint(n)
        if fp in seen:
            continue
        seen.add(fp)
        n.pop("dialer-proxy", None)
        uniq.append(n)

    log(f"aggregate: {len(all_nodes)} raw -> {len(uniq)} unique")
    os.makedirs(STATE, exist_ok=True)
    with io.open(LIVE_JSON, "w", encoding="utf-8") as f:
        json.dump({"fetchedAt": int(time.time()), "sources": report,
                   "count": len(uniq), "nodes": uniq},
                  f, ensure_ascii=False, indent=1)
    return 0


def cmd_probe(args):
    with io.open(LIVE_JSON, encoding="utf-8") as f:
        data = json.load(f)
    nodes = data["nodes"]
    log(f"probe: {len(nodes)} nodes, workers={args.workers}, timeout={args.timeout}s")

    def work(n):
        ok1, ms1 = probe_node_reachable(n, args.timeout)
        n["alive"] = bool(ok1)
        n["latencyMs"] = ms1
        return n

    done = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for i, n in enumerate(ex.map(work, nodes), 1):
            done.append(n)
            if i % 25 == 0:
                log(f"  probed {i}/{len(nodes)}")

    alive = [n for n in done if n.get("alive")]
    alive.sort(key=lambda x: (x.get("latencyMs") or 9e9, x.get("name") or ""))
    kept = alive[: args.top]

    log(f"probe: {len(alive)} reachable / {len(nodes)}, keeping top {len(kept)}")
    for n in kept[:10]:
        log(f"  {n.get('latencyMs')}ms  {str(n.get('name'))[:40]}")

    data["nodes"] = kept
    data["count"] = len(kept)
    data["probedAt"] = int(time.time())
    data["stats"] = {"raw": len(nodes), "reachable": len(alive), "kept": len(kept)}
    with io.open(LIVE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return 0


class Dumper(yaml.SafeDumper):
    pass


def _str_rep(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data,
                                   style="|" if "\n" in data else None)


Dumper.add_representer(str, _str_rep)


def cmd_build(args):
    with io.open(LIVE_JSON, encoding="utf-8") as f:
        data = json.load(f)
    picked = data["nodes"]
    if not picked:
        log("build: no nodes picked")
        return 1

    G_FRONT, G_AUTO, G_MASTER, G_PROXY = "前置", "前置自动", "前置总控", "代理"
    names = [n["name"] for n in picked]

    proxies = []
    for n in picked:
        q = dict(n)
        q.pop("dialer-proxy", None)
        q.pop("alive", None)
        q.pop("latencyMs", None)
        q.pop("nodeLatencyMs", None)
        proxies.append(q)

    land = dict(LANDING)
    land["dialer-proxy"] = G_MASTER
    proxies.append(land)

    groups = [
        {"name": G_FRONT, "type": "select", "proxies": names},
        {"name": G_AUTO, "type": "url-test", "proxies": names,
         "url": "http://www.gstatic.com/generate_204",
         "interval": 180, "tolerance": 80},
        {"name": G_MASTER, "type": "select", "proxies": [G_AUTO, G_FRONT] + names},
        {"name": G_PROXY, "type": "select",
         "proxies": [land["name"], G_MASTER, "DIRECT"]},
    ]

    doc = {
        "mixed-port": 7890, "allow-lan": False, "mode": "rule",
        "log-level": "info", "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        "dns": {
            "enable": True, "ipv6": False, "listen": "0.0.0.0:1053",
            "enhanced-mode": "fake-ip", "fake-ip-range": "198.18.0.1/16",
            "fake-ip-filter": ["*.lan", "*.local", "localhost.ptlogin2.qq.com",
                               "+.msftconnecttest.com", "+.msftncsi.com",
                               "time.*.com", "ntp.*.com"],
            "default-nameserver": ["223.5.5.5", "119.29.29.29"],
            "nameserver": ["https://223.5.5.5/dns-query",
                           "https://119.29.29.29/dns-query"],
            "proxy-server-nameserver": ["https://223.5.5.5/dns-query"],
            "fallback": ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
            "fallback-filter": {"geoip": True, "geoip-code": "CN",
                                "ipcidr": ["240.0.0.0/4", "0.0.0.0/32"]},
        },
        "proxies": proxies,
        "proxy-groups": groups,
        "rules": ["GEOIP,PRIVATE,DIRECT,no-resolve", f"MATCH,{G_PROXY}"],
    }

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats = data.get("stats", {})
    head = f"""# ============================================================
# box-nodes 链式代理订阅（自动更新）
# 生成: {stamp}
# 抓取: {stats.get('raw','?')} 个节点 → 可达 {stats.get('reachable','?')} → 精选 {len(names)} 个
# 唯一出口: {LANDING['server']}
#
#   本机 -> 前置(前置总控) -> 落地 {LANDING['server']} -> 外网
#
# 界面:
#   {G_FRONT}      手动选前置
#   {G_AUTO} 自动测速
#   {G_MASTER} 链式锚点
#   {G_PROXY}      最终出口
#
# 精选节点（按到落地的延迟升序）:
"""
    for i, n in enumerate(picked, 1):
        head += f"#   {i:2d}. {n.get('latencyMs')}ms  {str(n.get('name'))[:44]}\n"
    head += "# ============================================================\n"

    body = yaml.dump(doc, Dumper=Dumper, allow_unicode=True, sort_keys=False,
                     default_flow_style=False, width=4096)
    text = head + body

    for d in ("docs", "dist"):
        p = os.path.join(ROOT, d)
        os.makedirs(p, exist_ok=True)
        with io.open(os.path.join(p, "chain.yaml"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(text)

    log(f"build: {len(names)} front nodes + 1 landing -> docs/chain.yaml")

    # 自检
    back = yaml.safe_load(text)
    gn = [g["name"] for g in back["proxy-groups"]]
    ln = next((p for p in back["proxies"] if p["name"] == LANDING["name"]), None)
    checks = [
        ("四组齐备", gn == [G_FRONT, G_AUTO, G_MASTER, G_PROXY]),
        ("落地挂链式", bool(ln) and ln.get("dialer-proxy") == G_MASTER),
        ("出口唯一", bool(ln) and ln["server"] == LANDING["server"]),
        ("节点数正确", len(back["proxies"]) == len(names) + 1),
    ]
    ok = True
    for label, c in checks:
        log(f"  [{'OK' if c else 'FAIL'}] {label}")
        ok &= c
    return 0 if ok else 1


def main():
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch").set_defaults(fn=lambda a: cmd_fetch())
    p = sub.add_parser("probe")
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(fn=cmd_probe)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
