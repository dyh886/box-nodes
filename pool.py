#!/usr/bin/env python3
"""
box-nodes / pool.py — 代理节点池采集 + 健康检查 + 多格式导出

纯标准库实现（云机无 pip3）。三个子命令：

  collect   从多个订阅源拉取节点，去重后写入 state/nodes.json
  probe     并发 TCP 健康检查，打分，只保留可用节点
  export    生成多格式产物（clash / v2ray / singbox / base64 订阅 / 纯链接）

设计要点
--------
* 节点来源可插拔：sources 里配 HTTP 订阅 URL 或本地文件，格式自动嗅探。
* 解析器覆盖 base64 订阅、明文链接列表、Clash YAML（用极简 YAML 子集解析，
  不依赖 PyYAML）。
* 去重按 (protocol, server, port, uuid/password, 传输层关键参数) 的指纹。
* probe 只用 TCP connect 测活性 + 延迟，不握手协议，快且不会污染订阅。
* 出口产物带 stable 排序，避免每次 commit 产生无意义 diff。
"""

import argparse
import base64
import binascii
import concurrent.futures
import hashlib
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
STATE_FILE = os.path.join(STATE_DIR, "nodes.json")
CONFIG_FILE = os.path.join(ROOT, "config.json")

UA = "clash-verge/v2.0.0"
DEFAULT_TIMEOUT = 12
PROBE_TIMEOUT = 5.0


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def http_get(url, timeout=DEFAULT_TIMEOUT, headers=None):
    """GET 一个 URL，返回 bytes。带 UA 伪装和一次重试。"""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt == 0:
                time.sleep(1.5)
    raise RuntimeError(f"GET {url} failed: {last}")


def b64decode_loose(s):
    """宽松 base64 解码：容忍缺 padding、URL-safe、换行。"""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "ignore")
    s = re.sub(r"\s+", "", s)
    s = s.replace("-", "+").replace("_", "/")
    pad = (-len(s)) % 4
    s += "=" * pad
    try:
        return base64.b64decode(s).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        return ""


def b64encode(s):
    if isinstance(s, str):
        s = s.encode()
    return base64.b64encode(s).decode()


def looks_like_subscription(text):
    """判断一段文本是不是 base64 订阅体：去掉空白后全是 base64 字符，且能解出链接。"""
    t = re.sub(r"\s+", "", text)
    if len(t) < 20:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_\-]+", t):
        return False
    dec = b64decode_loose(t)
    return bool(re.search(r"(vmess|vless|trojan|ss|ssr|hysteria2?|tuic)://", dec))


def stable_id(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode())
        h.update(b"\x00")
    return h.hexdigest()[:12]


# --------------------------------------------------------------------------
# 链接解析：vmess / vless / trojan / ss / ssr / hysteria2 / tuic
# --------------------------------------------------------------------------

def parse_query(url):
    q = urllib.parse.urlparse(url).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}


def parse_vmess(link):
    """vmess://<base64 json>"""
    body = link[len("vmess://"):].strip()
    raw = b64decode_loose(body)
    if not raw:
        return None
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return None
    net = (j.get("net") or "tcp").lower()
    tls = str(j.get("tls") or "").lower() in ("tls", "reality", "true")
    node = {
        "protocol": "vmess",
        "name": j.get("ps") or "",
        "server": j.get("add") or "",
        "port": int(j.get("port") or 0),
        "uuid": j.get("id") or "",
        "alterId": int(j.get("aid") or 0),
        "cipher": j.get("scy") or "auto",
        "network": net,
        "tls": tls,
        "sni": j.get("sni") or j.get("host") or "",
        "host": j.get("host") or "",
        "path": j.get("path") or "",
    }
    if j.get("scy") == "none":
        node["cipher"] = "none"
    return node


def parse_vless(link):
    """vless://uuid@host:port?params#name"""
    u = urllib.parse.urlparse(link)
    q = parse_query(link)
    if not u.hostname or not u.port:
        return None
    sec = (q.get("security") or "none").lower()
    return {
        "protocol": "vless",
        "name": urllib.parse.unquote(u.fragment or ""),
        "server": u.hostname,
        "port": int(u.port),
        "uuid": urllib.parse.unquote(u.username or ""),
        "flow": q.get("flow") or "",
        "network": (q.get("type") or "tcp").lower(),
        "tls": sec in ("tls", "reality", "xtls"),
        "security": sec,
        "sni": q.get("sni") or q.get("peer") or "",
        "host": q.get("host") or "",
        "path": urllib.parse.unquote(q.get("path") or ""),
        "serviceName": q.get("serviceName") or "",
        "publicKey": q.get("pbk") or "",
        "shortId": q.get("sid") or "",
        "fingerprint": q.get("fp") or "",
        "clientFingerprint": q.get("fp") or "",
    }


def parse_trojan(link):
    """trojan://password@host:port?params#name"""
    u = urllib.parse.urlparse(link)
    q = parse_query(link)
    if not u.hostname or not u.port:
        return None
    return {
        "protocol": "trojan",
        "name": urllib.parse.unquote(u.fragment or ""),
        "server": u.hostname,
        "port": int(u.port),
        "password": urllib.parse.unquote(u.username or ""),
        "network": (q.get("type") or "tcp").lower(),
        "tls": True,
        "sni": q.get("sni") or q.get("peer") or "",
        "host": q.get("host") or "",
        "path": urllib.parse.unquote(q.get("path") or ""),
        "allowInsecure": q.get("allowInsecure") in ("1", "true"),
    }


def parse_ss(link):
    """ss://base64(method:pass)@host:port#name  或  ss://base64(method:pass@host:port)#name"""
    body = link[len("ss://"):]
    frag = ""
    if "#" in body:
        body, frag = body.split("#", 1)
    frag = urllib.parse.unquote(frag)
    body = body.split("?", 1)[0]

    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        dec = b64decode_loose(userinfo)
        if dec and ":" in dec:
            method, password = dec.split(":", 1)
        elif ":" in urllib.parse.unquote(userinfo):
            method, password = urllib.parse.unquote(userinfo).split(":", 1)
        else:
            return None
    else:
        dec = b64decode_loose(body)
        if not dec or "@" not in dec:
            return None
        userinfo, hostport = dec.rsplit("@", 1)
        if ":" not in userinfo:
            return None
        method, password = userinfo.split(":", 1)

    if ":" not in hostport:
        return None
    host, port = hostport.rsplit(":", 1)
    host = host.strip("[]")
    if not port.isdigit():
        return None
    return {
        "protocol": "ss",
        "name": frag,
        "server": host,
        "port": int(port),
        "method": method,
        "password": password,
        "network": "tcp",
        "tls": False,
    }


def parse_ssr(link):
    """ssr://base64(host:port:proto:method:obfs:base64pass/?params)"""
    raw = b64decode_loose(link[len("ssr://"):])
    if not raw or "/?" not in raw:
        return None
    main, qs = raw.split("/?", 1)
    parts = main.split(":")
    if len(parts) < 6:
        return None
    host = parts[0]
    try:
        port = int(parts[1])
    except ValueError:
        return None
    params = {}
    for kv in qs.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = b64decode_loose(v)
    return {
        "protocol": "ssr",
        "name": params.get("remarks", ""),
        "server": host,
        "port": port,
        "method": parts[3],
        "password": b64decode_loose(parts[5]),
        "protocolParam": parts[2],
        "obfs": parts[4],
        "obfsParam": params.get("obfsparam", ""),
        "network": "tcp",
        "tls": False,
    }


def parse_hysteria2(link):
    """hysteria2://auth@host:port?params#name"""
    u = urllib.parse.urlparse(link)
    q = parse_query(link)
    if not u.hostname or not u.port:
        return None
    return {
        "protocol": "hysteria2",
        "name": urllib.parse.unquote(u.fragment or ""),
        "server": u.hostname,
        "port": int(u.port),
        "password": urllib.parse.unquote(u.username or ""),
        "sni": q.get("sni") or "",
        "insecure": q.get("insecure") in ("1", "true"),
        "obfs": q.get("obfs") or "",
        "obfsPassword": q.get("obfs-password") or "",
    }


def parse_tuic(link):
    u = urllib.parse.urlparse(link)
    q = parse_query(link)
    if not u.hostname or not u.port:
        return None
    return {
        "protocol": "tuic",
        "name": urllib.parse.unquote(u.fragment or ""),
        "server": u.hostname,
        "port": int(u.port),
        "uuid": urllib.parse.unquote(u.username or ""),
        "password": urllib.parse.unquote(u.password or ""),
        "sni": q.get("sni") or "",
        "congestion": q.get("congestion_control") or "bbr",
        "udpRelayMode": q.get("udp_relay_mode") or "native",
        "alpn": q.get("alpn") or "h3",
    }


PARSERS = {
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "ss": parse_ss,
    "ssr": parse_ssr,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "tuic": parse_tuic,
}

LINK_RE = re.compile(
    r"(?:vmess|vless|trojan|ssr|ss|hysteria2|hy2|tuic)://[^\s\"'<>\\]+", re.I
)


def parse_link(link):
    link = link.strip().rstrip(".,;")
    m = re.match(r"([a-z0-9]+)://", link, re.I)
    if not m:
        return None
    scheme = m.group(1).lower()
    fn = PARSERS.get(scheme)
    if not fn:
        return None
    try:
        node = fn(link)
    except Exception:  # noqa: BLE001
        return None
    if not node or not node.get("server") or not node.get("port"):
        return None
    node["source_link"] = link
    return node


# --------------------------------------------------------------------------
# Clash YAML 子集解析（proxies: 列表，无 PyYAML 依赖）
# --------------------------------------------------------------------------

def _scalar(v):
    v = v.strip()
    if not v:
        return None
    if (v[0] == v[-1]) and v[0] in "\"'":
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if v in ("null", "~"):
        return None
    return v


def parse_clash_yaml(text):
    """只解析 proxies: 下的节点列表，够用且不引依赖。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"^proxies\s*:\s*$", ln):
            start = i + 1
            break
    if start is None:
        return []

    items, cur, cur_indent = [], None, None
    for ln in lines[start:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        indent = len(ln) - len(ln.lstrip())
        if indent == 0 and not ln.lstrip().startswith("-"):
            break  # 下一个顶层键
        stripped = ln.strip()

        if stripped.startswith("- "):
            if cur:
                items.append(cur)
            cur = {}
            cur_indent = indent
            body = stripped[2:]
            if ":" in body:
                k, v = body.split(":", 1)
                cur[k.strip()] = _scalar(v)
        elif stripped == "-":
            if cur:
                items.append(cur)
            cur = {}
            cur_indent = indent
        elif cur is not None and ":" in stripped and indent > (cur_indent or 0):
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = _scalar(v)
            if v is not None:
                cur[k] = v
    if cur:
        items.append(cur)

    out = []
    for p in items:
        proto = (p.get("type") or "").lower()
        if proto in ("vmess", "vless", "trojan", "ss", "ssr", "hysteria2", "tuic"):
            ws = p.get("ws-opts") or {}
            node = {
                "protocol": proto,
                "name": p.get("name") or "",
                "server": p.get("server") or "",
                "port": int(p.get("port") or 0),
                "uuid": p.get("uuid") or "",
                "password": p.get("password") or "",
                "cipher": p.get("cipher") or "",
                "alterId": int(p.get("alterId") or 0),
                "network": (p.get("network") or "tcp").lower(),
                "tls": bool(p.get("tls")),
                "sni": p.get("servername") or p.get("sni") or "",
                "host": ws.get("headers", {}).get("Host", "") if isinstance(ws, dict) else "",
                "path": ws.get("path", "") if isinstance(ws, dict) else "",
                "skipCertVerify": bool(p.get("skip-cert-verify")),
            }
            if node["server"] and node["port"]:
                out.append(node)
    return out


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------

def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def fetch_source(src):
    """拉一个源，返回解析出的节点列表。src: {url|file, kind}"""
    label = src.get("url") or src.get("file")
    try:
        if src.get("file"):
            path = os.path.join(ROOT, src["file"])
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        else:
            raw = http_get(src["url"], timeout=src.get("timeout", DEFAULT_TIMEOUT))
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "ignore")
    except Exception as e:  # noqa: BLE001
        log(f"  ! {label}: {e}")
        return []

    nodes = []
    kind = src.get("kind") or "auto"

    if kind in ("auto", "clash") and re.search(r"(?m)^proxies\s*:", raw):
        nodes = parse_clash_yaml(raw)
        log(f"  · {label}: clash yaml -> {len(nodes)}")
        return nodes

    text = raw
    if kind in ("auto", "base64") and looks_like_subscription(raw):
        text = b64decode_loose(raw)
        log(f"  · {label}: base64 subscription decoded")

    for link in LINK_RE.findall(text):
        n = parse_link(link)
        if n:
            nodes.append(n)
    log(f"  · {label}: links -> {len(nodes)}")
    return nodes


def fingerprint(n):
    key = [
        n.get("protocol"), n.get("server"), n.get("port"),
        n.get("uuid") or n.get("password") or "",
        n.get("network") or "", n.get("path") or "", n.get("sni") or "",
        n.get("security") or "", n.get("publicKey") or "",
    ]
    return stable_id(*key)


def cmd_collect(_args):
    cfg = load_config()
    os.makedirs(STATE_DIR, exist_ok=True)
    all_nodes, seen = [], set()

    log(f"collect: {len(cfg['sources'])} sources")
    for src in cfg["sources"]:
        if not src.get("enabled", True):
            continue
        for n in fetch_source(src):
            fp = fingerprint(n)
            if fp in seen:
                continue
            seen.add(fp)
            n["id"] = fp
            n["collectedAt"] = int(time.time())
            all_nodes.append(n)

    all_nodes.sort(key=lambda x: x["id"])
    payload = {
        "updatedAt": int(time.time()),
        "count": len(all_nodes),
        "nodes": all_nodes,
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    log(f"collect done: {len(all_nodes)} unique nodes -> {STATE_FILE}")
    return 0


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------

def _proxy_url():
    for v in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        val = os.environ.get(v)
        if val:
            return val
    return None


def _parse_proxy(purl):
    if "://" not in purl:
        purl = "http://" + purl
    u = urllib.parse.urlparse(purl)
    return u.hostname, u.port or 8080, u.username, u.password


def _probe_via_proxy(server, port, timeout=PROBE_TIMEOUT):
    """
    走出口代理的 CONNECT 隧道，打到节点的【真实 host:port】判活。

    沙箱化的云机（如 box）没有直连出口，DNS 也在代理侧解析。CONNECT 由代理
    完成 DNS + TCP，所以这既能验证域名可解析，也能验证目标端口真的开着，
    且不发送任何协议数据。
    """
    purl = _proxy_url()
    if not purl:
        return _probe_direct(server, port, timeout)

    phost, pport, puser, ppass = _parse_proxy(purl)
    t0 = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((phost, pport), timeout=timeout)
        sock.settimeout(timeout)
        target = f"[{server}]:{port}" if ":" in server else f"{server}:{port}"
        req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
        if puser:
            tok = base64.b64encode(f"{puser}:{ppass or ''}".encode()).decode()
            req += f"Proxy-Authorization: Basic {tok}\r\n"
        req += "Proxy-Connection: keep-alive\r\n\r\n"
        sock.sendall(req.encode())

        buf = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in buf and time.time() < deadline:
            chunk = sock.recv(512)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8192:
                break
        m = re.match(rb"HTTP/\d\.\d\s+(\d{3})", buf)
        if not m:
            return False, None
        code = int(m.group(1))
        # 200 = 隧道建立成功；403/407 是代理策略，非节点问题
        if code == 200:
            return True, round((time.perf_counter() - t0) * 1000, 1)
        return False, None
    except Exception:  # noqa: BLE001
        return False, None
    finally:
        if sock:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


def _probe_direct(server, port, timeout=PROBE_TIMEOUT):
    """真正的 TCP connect 测活（仅在有直连出口的机器上有意义）。"""
    t0 = time.perf_counter()
    try:
        infos = socket.getaddrinfo(server, port, proto=socket.IPPROTO_TCP)
        if not infos:
            return False, None
        fam, typ, proto, _, addr = infos[0]
        s = socket.socket(fam, typ, proto)
        s.settimeout(timeout)
        try:
            s.connect(addr)
        finally:
            s.close()
        return True, round((time.perf_counter() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        return False, None


_PROXY_VARS = ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY")


def has_egress_proxy():
    return any(os.environ.get(v) for v in _PROXY_VARS)


def tcp_probe(server, port, timeout=PROBE_TIMEOUT, mode="auto"):
    """
    mode: auto | direct | proxy
      auto   有出口代理就用代理隧道，否则直连
      direct 强制直连（本机有真实出口时用）
      proxy  强制走代理隧道
    """
    if mode == "proxy" or (mode == "auto" and has_egress_proxy()):
        return _probe_via_proxy(server, port, timeout)
    return _probe_direct(server, port, timeout)


def cmd_probe(args):
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    nodes = state["nodes"]
    mode = args.mode
    if mode == "auto":
        mode = "proxy" if has_egress_proxy() else "direct"
    log(f"probe: {len(nodes)} nodes, workers={args.workers}, "
        f"timeout={args.timeout}s, mode={mode}")

    def work(n):
        ok, ms = tcp_probe(n["server"], n["port"], args.timeout, mode)
        n["alive"] = ok
        n["latencyMs"] = ms
        n["probedAt"] = int(time.time())
        n["probeMode"] = mode
        return n

    alive = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for n in ex.map(work, nodes):
            if n["alive"]:
                alive.append(n)

    # 按延迟升序，截断到上限
    alive.sort(key=lambda x: (x.get("latencyMs") or 9e9, x["id"]))
    kept = alive[: args.keep]
    log(f"probe: {len(alive)} alive / {len(nodes)}, keeping {len(kept)}")

    state["nodes"] = kept
    state["count"] = len(kept)
    state["probedAt"] = int(time.time())
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    return 0


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def safe_name(n, idx):
    base = (n.get("name") or "").strip()
    base = re.sub(r"[\r\n\t|,]+", " ", base).strip()
    tag = n.get("latencyMs")
    suffix = f" {int(tag)}ms" if isinstance(tag, (int, float)) else ""
    if not base:
        base = f"{n.get('protocol')}-{n.get('server')}"
    name = f"{base}{suffix}"
    if len(name) > 60:
        name = name[:60]
    return f"{idx:02d} {name}".strip()


def to_clash(n, name):
    proto = n["protocol"]
    common = {"name": name, "server": n["server"], "port": int(n["port"])}

    if proto == "vmess":
        p = {**common, "type": "vmess", "uuid": n["uuid"], "alterId": n.get("alterId", 0),
             "cipher": n.get("cipher") or "auto", "udp": True}
        if n.get("tls"):
            p["tls"] = True
            if n.get("sni"):
                p["servername"] = n["sni"]
        if n.get("network") == "ws":
            opts = {"path": n.get("path") or "/"}
            if n.get("host"):
                opts["headers"] = {"Host": n["host"]}
            p["network"] = "ws"
            p["ws-opts"] = opts
        elif n.get("network") in ("grpc", "h2"):
            p["network"] = n["network"]
            p[f"{n['network']}-opts"] = {"grpc-service-name": n.get("path", "")}
        return p

    if proto == "vless":
        p = {**common, "type": "vless", "uuid": n["uuid"], "udp": True}
        if n.get("flow"):
            p["flow"] = n["flow"]
        sec = n.get("security") or ("tls" if n.get("tls") else "none")
        if sec in ("tls", "reality"):
            p["tls"] = True
            if n.get("sni"):
                p["servername"] = n["sni"]
            if sec == "reality":
                p["reality-opts"] = {"public-key": n.get("publicKey", ""),
                                     "short-id": n.get("shortId", "")}
            if n.get("clientFingerprint"):
                p["client-fingerprint"] = n["clientFingerprint"]
        if n.get("network") == "ws":
            opts = {"path": n.get("path") or "/"}
            if n.get("host"):
                opts["headers"] = {"Host": n["host"]}
            p["network"] = "ws"
            p["ws-opts"] = opts
        elif n.get("network") == "grpc":
            p["network"] = "grpc"
            p["grpc-opts"] = {"grpc-service-name": n.get("serviceName") or n.get("path", "")}
        return p

    if proto == "trojan":
        p = {**common, "type": "trojan", "password": n["password"], "udp": True}
        if n.get("sni"):
            p["sni"] = n["sni"]
        if n.get("skipCertVerify") or n.get("allowInsecure"):
            p["skip-cert-verify"] = True
        if n.get("network") == "ws":
            opts = {"path": n.get("path") or "/"}
            if n.get("host"):
                opts["headers"] = {"Host": n["host"]}
            p["network"] = "ws"
            p["ws-opts"] = opts
        return p

    if proto == "ss":
        p = {**common, "type": "ss", "cipher": n["method"], "password": n["password"], "udp": True}
        return p

    if proto == "hysteria2":
        p = {**common, "type": "hysteria2", "password": n["password"]}
        if n.get("sni"):
            p["sni"] = n["sni"]
        if n.get("insecure"):
            p["skip-cert-verify"] = True
        return p

    if proto == "tuic":
        p = {**common, "type": "tuic", "uuid": n["uuid"], "password": n["password"],
             "congestion-controller": n.get("congestion", "bbr")}
        if n.get("sni"):
            p["sni"] = n["sni"]
        return p

    return None


def build_clash(nodes):
    proxies, names = [], []
    for i, n in enumerate(nodes, 1):
        name = safe_name(n, i)
        p = to_clash(n, name)
        if p:
            proxies.append(p)
            names.append(name)
    doc = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {"name": "AUTO", "type": "url-test", "proxies": names,
             "url": "http://www.gstatic.com/generate_204", "interval": 300, "tolerance": 50},
            {"name": "SELECT", "type": "select", "proxies": ["AUTO"] + names},
        ],
        "rules": ["MATCH,SELECT"],
    }
    # 手写 YAML：不引依赖，输出可控
    out = ["# generated by box-nodes — do not edit by hand"]
    for k in ("mixed-port", "allow-lan", "mode", "log-level"):
        out.append(f"{k}: {str(doc[k]).lower() if isinstance(doc[k], bool) else doc[k]}")
    out.append("proxies:")
    for p in proxies:
        out.append(f"  - name: {json.dumps(p['name'], ensure_ascii=False)}")
        for k, v in p.items():
            if k == "name":
                continue
            if isinstance(v, bool):
                out.append(f"    {k}: {str(v).lower()}")
            elif isinstance(v, dict):
                out.append(f"    {k}:")
                for kk, vv in v.items():
                    if isinstance(vv, dict):
                        out.append(f"      {kk}:")
                        for k3, v3 in vv.items():
                            out.append(f"        {k3}: {json.dumps(v3, ensure_ascii=False)}")
                    else:
                        out.append(f"      {kk}: {json.dumps(vv, ensure_ascii=False)}")
            elif isinstance(v, int):
                out.append(f"    {k}: {v}")
            else:
                out.append(f"    {k}: {json.dumps(v, ensure_ascii=False)}")
    out.append("proxy-groups:")
    for g in doc["proxy-groups"]:
        out.append(f"  - name: {json.dumps(g['name'], ensure_ascii=False)}")
        out.append(f"    type: {g['type']}")
        if g.get("url"):
            out.append(f"    url: {g['url']}")
            out.append(f"    interval: {g['interval']}")
            out.append(f"    tolerance: {g['tolerance']}")
        out.append("    proxies:")
        for nm in g["proxies"]:
            out.append(f"      - {json.dumps(nm, ensure_ascii=False)}")
    out.append("rules:")
    for r in doc["rules"]:
        out.append(f"  - {r}")
    return "\n".join(out) + "\n"


def build_links(nodes):
    """还原成标准分享链接，供 v2rayN / Shadowrocket 等直接导入。"""
    links = []
    for n in nodes:
        proto = n["protocol"]
        if proto == "vmess":
            j = {"v": "2", "ps": n.get("name", ""), "add": n["server"], "port": str(n["port"]),
                 "id": n["uuid"], "aid": str(n.get("alterId", 0)),
                 "scy": n.get("cipher") or "auto", "net": n.get("network", "tcp"),
                 "type": "none", "host": n.get("host", ""), "path": n.get("path", ""),
                 "tls": "tls" if n.get("tls") else "", "sni": n.get("sni", "")}
            links.append("vmess://" + b64encode(json.dumps(j, ensure_ascii=False)))
        elif proto == "vless":
            q = {"type": n.get("network", "tcp"),
                 "security": n.get("security") or ("tls" if n.get("tls") else "none")}
            if n.get("sni"):
                q["sni"] = n["sni"]
            if n.get("flow"):
                q["flow"] = n["flow"]
            if n.get("path"):
                q["path"] = n["path"]
            if n.get("host"):
                q["host"] = n["host"]
            if n.get("publicKey"):
                q["pbk"] = n["publicKey"]
            if n.get("shortId"):
                q["sid"] = n["shortId"]
            links.append(
                f"vless://{n['uuid']}@{n['server']}:{n['port']}?"
                f"{urllib.parse.urlencode(q)}#{urllib.parse.quote(n.get('name', ''))}")
        elif proto == "trojan":
            q = {}
            if n.get("sni"):
                q["sni"] = n["sni"]
            if n.get("path"):
                q["path"] = n["path"]
                q["type"] = n.get("network", "tcp")
            links.append(
                f"trojan://{urllib.parse.quote(n['password'])}@{n['server']}:{n['port']}"
                f"?{urllib.parse.urlencode(q)}#{urllib.parse.quote(n.get('name', ''))}")
        elif proto == "ss":
            userinfo = b64encode(f"{n['method']}:{n['password']}")
            links.append(
                f"ss://{userinfo}@{n['server']}:{n['port']}#{urllib.parse.quote(n.get('name', ''))}")
        elif proto == "hysteria2":
            q = {}
            if n.get("sni"):
                q["sni"] = n["sni"]
            links.append(
                f"hysteria2://{urllib.parse.quote(n['password'])}@{n['server']}:{n['port']}"
                f"?{urllib.parse.urlencode(q)}#{urllib.parse.quote(n.get('name', ''))}")
        elif proto == "tuic":
            links.append(
                f"tuic://{n['uuid']}:{urllib.parse.quote(n['password'])}@{n['server']}:{n['port']}"
                f"#{urllib.parse.quote(n.get('name', ''))}")
    return links


def build_singbox(nodes):
    outbounds = []
    for i, n in enumerate(nodes, 1):
        name = safe_name(n, i)
        proto = n["protocol"]
        if proto == "vmess":
            o = {"type": "vmess", "tag": name, "server": n["server"], "server_port": n["port"],
                 "uuid": n["uuid"], "security": n.get("cipher") or "auto",
                 "alter_id": n.get("alterId", 0)}
            if n.get("tls"):
                o["tls"] = {"enabled": True, "server_name": n.get("sni") or n["server"]}
            if n.get("network") == "ws":
                o["transport"] = {"type": "ws", "path": n.get("path") or "/",
                                  "headers": {"Host": n.get("host") or ""}}
        elif proto == "vless":
            o = {"type": "vless", "tag": name, "server": n["server"], "server_port": n["port"],
                 "uuid": n["uuid"]}
            if n.get("flow"):
                o["flow"] = n["flow"]
            if n.get("tls"):
                o["tls"] = {"enabled": True, "server_name": n.get("sni") or n["server"]}
            if n.get("network") == "ws":
                o["transport"] = {"type": "ws", "path": n.get("path") or "/"}
        elif proto == "trojan":
            o = {"type": "trojan", "tag": name, "server": n["server"], "server_port": n["port"],
                 "password": n["password"],
                 "tls": {"enabled": True, "server_name": n.get("sni") or n["server"]}}
        elif proto == "ss":
            o = {"type": "shadowsocks", "tag": name, "server": n["server"],
                 "server_port": n["port"], "method": n["method"], "password": n["password"]}
        elif proto == "hysteria2":
            o = {"type": "hysteria2", "tag": name, "server": n["server"],
                 "server_port": n["port"], "password": n["password"],
                 "tls": {"enabled": True, "server_name": n.get("sni") or n["server"]}}
        elif proto == "tuic":
            o = {"type": "tuic", "tag": name, "server": n["server"], "server_port": n["port"],
                 "uuid": n["uuid"], "password": n["password"],
                 "tls": {"enabled": True, "server_name": n.get("sni") or n["server"]}}
        else:
            continue
        outbounds.append(o)

    doc = {
        "log": {"level": "info"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1",
                      "listen_port": 7890}],
        "outbounds": outbounds + [{"type": "direct", "tag": "direct"},
                                  {"type": "block", "tag": "block"}],
        "route": {"rules": [{"outbound": "mixed-in", "type": "default"}]},
    }
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


def cmd_export(_args):
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    nodes = [n for n in state["nodes"] if n.get("alive", True)]
    if not nodes:
        log("export: no nodes, nothing written")
        return 1

    dist = os.path.join(ROOT, "dist")
    os.makedirs(dist, exist_ok=True)
    stamp = int(time.time())

    clash = build_clash(nodes)
    with open(os.path.join(dist, "clash.yaml"), "w", encoding="utf-8") as f:
        f.write(clash)

    links = build_links(nodes)
    with open(os.path.join(dist, "links.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(links) + "\n")

    with open(os.path.join(dist, "sub.base64.txt"), "w", encoding="utf-8") as f:
        f.write(b64encode("\n".join(links)))

    with open(os.path.join(dist, "singbox.json"), "w", encoding="utf-8") as f:
        f.write(build_singbox(nodes))

    meta = {
        "updatedAt": stamp,
        "updatedAtISO": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
        "count": len(nodes),
        "avgLatencyMs": round(
            sum(n.get("latencyMs") or 0 for n in nodes) / max(len(nodes), 1), 1),
        "byProtocol": {},
        "files": {
            "clash": "dist/clash.yaml",
            "links": "dist/links.txt",
            "base64": "dist/sub.base64.txt",
            "singbox": "dist/singbox.json",
        },
    }
    for n in nodes:
        p = n["protocol"]
        meta["byProtocol"][p] = meta["byProtocol"].get(p, 0) + 1
    with open(os.path.join(ROOT, "dist", "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    log(f"export done: {len(nodes)} nodes -> dist/ ({', '.join(meta['byProtocol'])})")
    return 0


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(prog="pool.py", description="proxy node pool pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("collect").set_defaults(fn=cmd_collect)

    p = sub.add_parser("probe")
    p.add_argument("--workers", type=int, default=64)
    p.add_argument("--timeout", type=float, default=PROBE_TIMEOUT)
    p.add_argument("--keep", type=int, default=60)
    p.add_argument("--mode", choices=("auto", "direct", "proxy"), default="auto",
                   help="auto: 有出口代理就用代理隧道（沙箱云机必需）")
    p.set_defaults(fn=cmd_probe)

    sub.add_parser("export").set_defaults(fn=cmd_export)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
