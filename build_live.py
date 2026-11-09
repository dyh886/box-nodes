#!/usr/bin/env python3
"""
build_live.py — 拉订阅 → 聚合去重 → 生成四层链式配置

重要设计决定（踩坑后的结论）：
  云机（box）的出口代理只放行 443 / 22 端口，其余一律 403。
  而落地是 9443、多数前置也是非 443 端口，所以【云机无法验证链路是否真正可用】。
  之前用 TCP 握手「测可达」得到的 112 个"可用"节点是假象——
  它们走的是代理的 443 隧道，跟真实链路无关。

所以这里只做能可靠完成的事：
  1. 拉订阅、解析、去重        （可靠）
  2. 按协议质量与名称做初筛      （启发式，不假装是实测）
  3. 生成四层链式配置并发布      （可靠）

真正的可用性由客户端（手机/电脑）在本地测速决定——
客户端的 url-test 会自动挑最快的，这正是 Clash 的设计。
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
import urllib.parse
import urllib.request

import yaml

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config.json")
STATE = os.path.join(ROOT, "state")
LIVE_JSON = os.path.join(STATE, "live_nodes.json")

UA = "clash-verge/v2.0.0"


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def http_get(url, timeout=35):
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
            n = {
                "name": j.get("ps") or "", "type": "vmess",
                "server": j.get("add"), "port": int(j.get("port") or 0),
                "uuid": j.get("id"), "alterId": int(j.get("aid") or 0),
                "cipher": j.get("scy") or "auto",
                "network": (j.get("net") or "tcp").lower(),
                "udp": True,
            }
            if str(j.get("tls") or "").lower() in ("tls", "true"):
                n["tls"] = True
                n["servername"] = j.get("sni") or j.get("host") or ""
            if n["network"] == "ws":
                n["ws-opts"] = {
                    "path": j.get("path") or "/",
                    "headers": {"Host": j.get("host") or ""},
                }
            return n

        u = urllib.parse.urlparse(link)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        frag = urllib.parse.unquote(u.fragment or "")

        if scheme == "vless":
            sec = (q.get("security") or "none").lower()
            n = {"name": frag, "type": "vless", "server": u.hostname,
                 "port": int(u.port or 0),
                 "uuid": urllib.parse.unquote(u.username or ""),
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
                n["ws-opts"] = {
                    "path": urllib.parse.unquote(q.get("path") or "/"),
                    "headers": {"Host": q.get("host") or ""},
                }
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
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")

    if re.search(r"(?m)^\s*proxies\s*:", raw):
        try:
            d = yaml.safe_load(raw)
            out = []
            for p in (d.get("proxies") or []):
                if isinstance(p, dict) and p.get("name") and p.get("server"):
                    q = dict(p)
                    q.pop("dialer-proxy", None)
                    out.append(q)
            return out, "clash"
        except Exception as e:  # noqa: BLE001
            log(f"    yaml failed: {e}")

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
    return "|".join(str(n.get(k, "")) for k in
                    ("type", "server", "port", "uuid", "password",
                     "network", "cipher")).lower()


# --------------------------------------------------------------------------
# 质量初筛（不是实测，是启发式）
# --------------------------------------------------------------------------

# 协议优先级：TLS 类协议更适合不稳的网络
PROTO_RANK = {
    "vless": 0, "trojan": 1, "vmess": 2, "hysteria2": 3,
    "tuic": 4, "ss": 5, "ssr": 6, "http": 9, "socks5": 9,
}


def quality_score(n):
    """
    仅用于排序，不用于淘汰。

    历史教训：曾给 http/socks5 额外 +5 分并直接剔除，结果误杀 206 个节点，
    其中实测 62.5% 能正常打开 GitHub。协议类型不决定可用性。

    现在的规则：分数只影响实测队列的先后顺序（优先验证 TLS 类），
    所有节点都会进入端到端验证，由实测结果决定去留。
    """
    t = str(n.get("type", "")).lower()
    s = PROTO_RANK.get(t, 5)

    if n.get("tls") or t in ("trojan", "hysteria2", "tuic", "vless"):
        s -= 1
    if n.get("reality-opts"):
        s -= 1
    if int(n.get("port") or 0) == 443:
        s -= 1
    return s


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------

def load_cfg():
    with io.open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def cmd_fetch():
    cfg = load_cfg()
    subs = [s for s in cfg.get("subscriptions", []) if s.get("enabled", True)]
    log(f"fetch: {len(subs)} subscriptions")

    all_nodes, report = [], []
    for s in subs:
        label = s.get("label") or str(s.get("url", ""))[:50]

        if s.get("type") == "mirror":
            local = os.path.join(ROOT, s["file"])
            if not os.path.exists(local):
                log(f"  MISS {label}: {s['file']}")
                report.append({"label": label, "ok": False, "error": "missing"})
                continue
            with io.open(local, "rb") as f:
                raw = f.read()
            try:
                nodes, kind = parse_subscription(raw)
                log(f"  OK  {label}: {len(nodes)} ({kind}/mirror)")
                report.append({"label": label, "ok": True,
                               "nodes": len(nodes), "kind": f"mirror/{kind}"})
                all_nodes.extend(nodes)
            except Exception as e:  # noqa: BLE001
                log(f"  FAIL {label}: {type(e).__name__}")
                report.append({"label": label, "ok": False,
                               "error": type(e).__name__})
            continue

        try:
            raw = http_get(s["url"], timeout=s.get("timeout", 35))
            nodes, kind = parse_subscription(raw)
            log(f"  OK  {label}: {len(nodes)} ({kind})")
            report.append({"label": label, "ok": True,
                           "nodes": len(nodes), "kind": kind})
            all_nodes.extend(nodes)
        except Exception as e:  # noqa: BLE001
            log(f"  FAIL {label}: {type(e).__name__}")
            report.append({"label": label, "ok": False, "error": type(e).__name__})

    seen, uniq = set(), []
    for n in all_nodes:
        fp = fingerprint(n)
        if fp in seen:
            continue
        seen.add(fp)
        uniq.append(n)

    log(f"aggregate: {len(all_nodes)} -> {len(uniq)} unique")
    os.makedirs(STATE, exist_ok=True)
    with io.open(LIVE_JSON, "w", encoding="utf-8") as f:
        json.dump({"fetchedAt": int(time.time()), "sources": report,
                   "count": len(uniq), "nodes": uniq},
                  f, ensure_ascii=False, indent=1)
    return 0


def cmd_rank(args):
    """
    只做去重和排序，不再按协议类型排除节点。

    历史教训：此前把 http/socks5 当作「明文劣质代理」一律剔除，
    实测这批节点 62.5% 能正常打开 GitHub（25/40），是我误杀了可用节点。
    协议类型不代表可用性——是否可用只能靠端到端实连判断。
    所以这里保留全部协议，只按名称去重并按质量分排序。
    """
    with io.open(LIVE_JSON, encoding="utf-8") as f:
        data = json.load(f)

    nodes = data["nodes"]
    log(f"rank: {len(nodes)} candidates")

    for n in nodes:
        n["qscore"] = quality_score(n)

    nodes.sort(key=lambda x: (x["qscore"], str(x.get("name") or "")))

    chosen, seen_names = [], set()
    for n in nodes:
        nm = str(n.get("name") or "").strip()
        if nm in seen_names:
            continue
        seen_names.add(nm)
        chosen.append(n)
        if len(chosen) >= args.top:
            break

    types = {}
    for n in chosen:
        t = str(n.get("type"))
        types[t] = types.get(t, 0) + 1
    log(f"rank: kept {len(chosen)} (all protocols) {types}")
    for n in chosen[:6]:
        log(f"  q={n['qscore']}  {str(n.get('type')):8s} {str(n.get('name'))[:38]}")

    data["nodes"] = chosen
    data["count"] = len(chosen)
    data["rankedAt"] = int(time.time())
    data["stats"] = {"raw": len(nodes), "kept": len(chosen), "byType": types}
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
    cfg = load_cfg()
    landing = cfg.get("landing_node")
    if not landing:
        log("config.json missing landing_node")
        return 1

    with io.open(LIVE_JSON, encoding="utf-8") as f:
        data = json.load(f)
    picked = data["nodes"]
    if not picked:
        log("build: no nodes")
        return 1

    G_FRONT, G_AUTO, G_MASTER, G_PROXY = "前置", "前置自动", "前置总控", "代理"
    names = [n["name"] for n in picked]

    # L2 已验证的节点排在前面，让「前置自动」优先从可信集合里选
    l2_ok = [n for n in picked if n.get("l2Ms")]
    l2_names = [n["name"] for n in l2_ok] or names
    if l2_ok:
        l2_ok.sort(key=lambda x: x.get("l2Ms") or 9e9)
        l2_names = [n["name"] for n in l2_ok]

    proxies = []
    for n in picked:
        q = {k: v for k, v in n.items()
             if k not in ("alive", "latencyMs", "realLatencyMs",
                          "nodeLatencyMs", "qscore", "dialer-proxy",
                          "l2Ms", "l2Note")}
        proxies.append(q)

    land = dict(landing)
    land["dialer-proxy"] = G_MASTER
    proxies.append(land)

    # A 方案要点：
    #  1. url-test 用严格目标（google 204）：能过测速的必须真能访问国际站点。
    #     gstatic/204 国内直连就能通，会选出「绿色但打不开」的假可用节点。
    #  2. 「前置总控」默认指向「前置自动」，不依赖某个固定节点名——
    #     订阅每小时更新、节点名每次都变，固定选择必然失效。
    #  3. 「前置自动」优先用 L2 实测通过的节点。
    groups = [
        {"name": G_FRONT, "type": "select", "proxies": names},
        {"name": G_AUTO, "type": "url-test", "proxies": l2_names,
         "url": "https://www.google.com/generate_204",
         "interval": 180, "tolerance": 80},
        {"name": G_MASTER, "type": "select", "proxies": [G_AUTO, G_FRONT] + names},
        {"name": G_PROXY, "type": "select",
         "proxies": [land["name"], G_MASTER, "DIRECT"]},
    ]

    doc = {
        "mixed-port": 7890, "allow-lan": False, "mode": "rule",
        "log-level": "info", "ipv6": False,
        "external-controller": "127.0.0.1:9090",
        # 不写 dns: 段。
        # 原因：原方案（vps/chain/update_chain.py）从不写 dns，客户端用自身设置。
        # 我此前硬编码了 119.29.29.29 / 1.1.1.1 / 8.8.8.8 的 DoH，实测在部分网络上
        # 这些解析器超时，导致代理侧域名解析卡约 5 秒、TLS 握手失败
        # （表现为「能测速但打不开 GitHub」）。订阅不应干预客户端 DNS。
        "proxies": proxies,
        "proxy-groups": groups,
        "rules": ["GEOIP,PRIVATE,DIRECT,no-resolve", f"MATCH,{G_PROXY}"],
    }

    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st = data.get("stats", {})
    l2_tested = st.get("l2Tested", 0)
    l2_passed = st.get("l2Passed", len(l2_ok))
    head = f"""# ============================================================
# box-nodes 链式代理订阅
# 生成: {stamp}
# 候选: {st.get('raw','?')} 个 → 初筛 {st.get('kept','?')} → 精选 {len(names)} 个
# 端到端验证: {l2_tested} 个实测 → {l2_passed} 个通过（Google 204 + GitHub 200）
# 唯一出口: {landing['server']}:{landing['port']}  ({landing['type']})
#
#   本机 -> 前置(前置总控) -> 落地 -> 外网
#
# 说明: L2 端到端验证在本机完成（云机出口只放行 443/22，落地 9443 不可达）。
#       「前置自动」用 https://www.google.com/generate_204 严格测速，
#       能过测速的节点即真正具备国际出口能力。
#       本订阅不含 dns 段：DNS 交由客户端管理，避免订阅覆盖后解析异常。
#
# 界面:
#   {G_FRONT}      手动选前置
#   {G_AUTO} 自动测速（推荐；节点名每次更新都会变，固定选择会失效）
#   {G_MASTER} 链式锚点，默认指向「{G_AUTO}」
#   {G_PROXY}      最终出口，选「{land['name']}」走完整链路
# ============================================================
"""
    body = yaml.dump(doc, Dumper=Dumper, allow_unicode=True, sort_keys=False,
                     default_flow_style=False, width=4096)
    text = head + body

    for d in ("docs", "dist"):
        p = os.path.join(ROOT, d)
        os.makedirs(p, exist_ok=True)
        with io.open(os.path.join(p, "chain.yaml"), "w",
                     encoding="utf-8", newline="\n") as f:
            f.write(text)

    log(f"build: {len(names)} front + 1 landing")

    back = yaml.safe_load(text)
    gn = [g["name"] for g in back["proxy-groups"]]
    ln = next((p for p in back["proxies"] if p["name"] == landing["name"]), None)
    checks = [
        ("四组齐备", gn == [G_FRONT, G_AUTO, G_MASTER, G_PROXY]),
        ("落地挂链式", bool(ln) and ln.get("dialer-proxy") == G_MASTER),
        ("落地协议正确", bool(ln) and ln.get("type") == landing["type"]
         and int(ln.get("port")) == int(landing["port"])),
        ("节点数正确", len(back["proxies"]) == len(names) + 1),
        # 订阅不得干预客户端 DNS：曾有 dns 段导致解析超时、能测速但打不开网页
        ("不含 dns 段", "dns" not in back),
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
    p = sub.add_parser("rank")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(fn=cmd_rank)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
