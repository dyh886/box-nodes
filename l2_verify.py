#!/usr/bin/env python3
"""
l2_verify.py — 本机 L2 端到端验证（经节点 → 落地 → 真实外网）

为什么必须在本机跑：
  云机（box）的出口代理只放行 443/22，落地是 9443、多数前置也是非 443，
  所以云机无法建立「前置 → 落地」这一跳，也就无法验证节点是否真能用。
  本机有真实出口，可以完整走通链路，这是唯一可靠的判断方式。

验证方法（对齐原有 update_chain.py 的思路，两个目标都过才算通过）：
  1. https://www.google.com/generate_204   → 证明有真实国际出口（204）
  2. https://github.com/                    → 证明目标站点可打开（200）
  只测 204 会被"国内能通的假节点"骗过，所以必须加 github。

用法:
  python l2_verify.py                  # 验证 state/live_nodes.json 里全部候选
  python l2_verify.py --only-tagged    # 额外要求节点在 mirror 白名单里
  python l2_verify.py --top 60         # 只验证前 N 个

输出: 就地更新 state/live_nodes.json，只保留通过的节点并写入实测延迟。
"""

import argparse
import concurrent.futures as cf
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
LIVE = ROOT / "state" / "live_nodes.json"

# sing-box 可执行文件（优先项目内 vps/bin，其次 PATH）
SB_CANDIDATES = [
    ROOT.parent / "vps" / "bin" / "sing-box-1.12.9-windows-amd64" / "sing-box.exe",
    ROOT.parent / "vps" / "portable" / "bin" / "sing-box.exe",
]

BASE_PORT = 28100
BATCH = 8
STARTUP_WAIT = 4.0

TARGETS = [
    ("https://www.google.com/generate_204", "204"),
    ("https://github.com/", "200"),
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def find_singbox():
    for c in SB_CANDIDATES:
        if c.exists():
            return str(c)
    return shutil.which("sing-box")


def load_cfg():
    with io.open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def node_to_outbound(n, tag):
    """节点 → sing-box outbound。与 build_singbox.py 保持一致的最小实现。"""
    typ = str(n.get("type", "")).lower()
    server, port = n.get("server"), int(n.get("port") or 0)
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

    # TLS
    reality = n.get("reality-opts") or {}
    want_tls = bool(n.get("tls")) or typ in ("trojan", "hysteria2", "tuic")
    if want_tls or reality:
        tls = {"enabled": True}
        sni = n.get("servername") or n.get("sni")
        if sni:
            tls["server_name"] = sni
        if n.get("skip-cert-verify") or n.get("insecure"):
            tls["insecure"] = True
        if reality:
            tls["reality"] = {"enabled": True,
                              "public_key": reality.get("public-key", ""),
                              "short_id": reality.get("short-id", "")}
        fp = n.get("client-fingerprint")
        if fp:
            tls["utls"] = {"enabled": True, "fingerprint": fp}
        o["tls"] = tls

    # transport
    net = str(n.get("network") or "").lower()
    if net == "ws":
        opts = n.get("ws-opts") or {}
        tr = {"type": "ws", "path": opts.get("path") or "/"}
        host = (opts.get("headers") or {}).get("Host") or n.get("host")
        if host:
            tr["headers"] = {"Host": host}
        o["transport"] = tr
    elif net == "grpc":
        o["transport"] = {"type": "grpc",
                          "service_name": (n.get("grpc-opts") or {}).get("grpc-service-name", "")}

    return o


def wait_port(port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def curl_via(port, url, expect, max_time=13):
    """经本地 socks5 端口请求 url，返回 (ok, ms, note)。"""
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            ["curl.exe", "-q", "-sS", "--noproxy", "localhost",
             "--proxy", f"socks5h://127.0.0.1:{port}",
             "--connect-timeout", "6", "--max-time", str(max_time),
             url, "-o", "NUL", "-w", "%{http_code}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=max_time + 3)
        code = (p.stdout or "").strip()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        return (code == expect), ms, f"HTTP{code or '-'}"
    except Exception as e:  # noqa: BLE001
        return False, None, type(e).__name__


def verify_batch(batch, landing, sb, widx):
    """一批节点起一个 sing-box 实例，逐端口验证。"""
    inbounds, outbounds, rules = [], [], []
    for i, n in enumerate(batch):
        tag = f"n{i}"
        ob = node_to_outbound(n, tag)
        if not ob:
            continue
        inbounds.append({"type": "socks", "tag": f"i{i}",
                         "listen": "127.0.0.1", "listen_port": BASE_PORT + i})
        outbounds.append(ob)
        # 落地挂在该节点上：detour → 节点
        outbounds.append({"type": "shadowsocks", "tag": f"l{i}",
                          "server": landing["server"],
                          "server_port": int(landing["port"]),
                          "method": landing.get("cipher") or "chacha20-ietf-poly1305",
                          "password": landing.get("password") or "",
                          "detour": tag})
        rules.append({"inbound": [f"i{i}"], "outbound": f"l{i}"})
    outbounds.append({"type": "direct", "tag": "direct"})

    cfg = {"log": {"level": "error"}, "inbounds": inbounds,
           "outbounds": outbounds,
           "route": {"rules": rules, "final": "direct"}}

    cfg_path = Path(tempfile.gettempdir()) / f"l2cfg{widx}.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.Popen([sb, "run", "-c", str(cfg_path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = {}
    try:
        if not wait_port(BASE_PORT, timeout=STARTUP_WAIT + 8):
            return {i: (False, None, "no-port") for i in range(len(batch))}
        time.sleep(0.6)

        def one(i):
            port = BASE_PORT + i
            best = None
            for url, expect in TARGETS:
                ok, ms, note = curl_via(port, url, expect)
                if not ok:
                    return i, (False, ms, f"{url.split('/')[2]}:{note}")
                if best is None or ms < best:
                    best = ms
            return i, (True, best, "google+github")

        with cf.ThreadPoolExecutor(max_workers=len(batch)) as ex:
            for i, r in ex.map(one, range(len(batch))):
                results[i] = r
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            cfg_path.unlink()
        except OSError:
            pass
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=9999)
    ap.add_argument("--batch", type=int, default=BATCH)
    args = ap.parse_args()

    sb = find_singbox()
    if not sb:
        log("sing-box not found")
        return 1

    cfg = load_cfg()
    landing = cfg.get("landing_node")
    if not landing:
        log("config.json missing landing_node")
        return 1

    with io.open(LIVE, encoding="utf-8") as f:
        data = json.load(f)
    nodes = data["nodes"][: args.top]
    log(f"L2 verify: {len(nodes)} candidates, landing={landing['server']}:{landing['port']}")

    passed = []
    total = len(nodes)
    for start in range(0, total, args.batch):
        chunk = nodes[start:start + args.batch]
        widx = start // args.batch
        res = verify_batch(chunk, landing, sb, widx)
        for i, n in enumerate(chunk):
            ok, ms, note = res.get(i, (False, None, "missing"))
            tag = "OK  " if ok else "FAIL"
            log(f"  {tag} {str(ms) if ms else '-':>7}ms  {str(n.get('name'))[:40]:42s} {note}")
            if ok:
                q = dict(n)
                q["l2Ms"] = ms
                q["l2Note"] = note
                passed.append(q)

    passed.sort(key=lambda x: x.get("l2Ms") or 9e9)
    log(f"L2 result: {len(passed)}/{total} pass (google 204 + github 200)")

    data["nodes"] = passed
    data["count"] = len(passed)
    data["l2VerifiedAt"] = int(time.time())
    st = dict(data.get("stats", {}))
    st["l2Tested"] = total
    st["l2Passed"] = len(passed)
    data["stats"] = st
    with io.open(LIVE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
