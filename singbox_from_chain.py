"""
从已发布的 chain.yaml 直接生成 sing-box 配置。

为什么不复用 build_singbox.py：
  它依赖 state/live_nodes.json，而该文件每轮被 fetch 覆盖、节点名每轮都变。
  实测：已发布 chain.yaml 的 65 个节点，在当前 live_nodes 里只匹配到 4 个。
  所以必须把 chain.yaml 本身当作唯一事实来源，直接转换。

映射关系：
  Clash proxies        → sing-box outbounds
  dialer-proxy         → detour
  proxy-groups select  → selector
  proxy-groups url-test→ urltest
  rules MATCH,代理     → route.final
"""
import io
import json
import os
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CHAIN = ROOT / 'docs' / 'chain.yaml'

G_FRONT, G_AUTO, G_MASTER, G_PROXY = '前置', '前置自动', '前置总控', '代理'
STRICT_URL = 'https://www.google.com/generate_204'


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", file=sys.stderr, flush=True)


def build_tls(n):
    typ = str(n.get('type', '')).lower()
    reality = n.get('reality-opts') or {}
    want = bool(n.get('tls')) or typ in ('trojan', 'hysteria2', 'tuic')
    if not want and not reality:
        return None
    tls = {'enabled': True}
    sni = n.get('servername') or n.get('sni')
    if sni:
        tls['server_name'] = sni
    if n.get('skip-cert-verify') or n.get('insecure'):
        tls['insecure'] = True
    if reality:
        tls['reality'] = {'enabled': True,
                          'public_key': reality.get('public-key', ''),
                          'short_id': reality.get('short-id', '')}
    fp = n.get('client-fingerprint')
    if fp:
        tls['utls'] = {'enabled': True, 'fingerprint': fp}
    return tls


def build_transport(n):
    net = str(n.get('network') or '').lower()
    if net == 'ws':
        o = n.get('ws-opts') or {}
        t = {'type': 'ws', 'path': o.get('path') or '/'}
        host = (o.get('headers') or {}).get('Host') or n.get('host')
        if host:
            t['headers'] = {'Host': host}
        return t
    if net == 'grpc':
        return {'type': 'grpc',
                'service_name': (n.get('grpc-opts') or {}).get('grpc-service-name', '')}
    if net == 'h2':
        return {'type': 'http'}
    return None


def to_outbound(n, tag):
    typ = str(n.get('type', '')).lower()
    server = n.get('server')
    port = int(n.get('port') or 0)
    if not server or not port:
        return None

    o = {'tag': tag}
    if typ == 'vmess':
        o.update({'type': 'vmess', 'server': server, 'server_port': port,
                  'uuid': n.get('uuid'), 'security': n.get('cipher') or 'auto',
                  'alter_id': int(n.get('alterId') or 0)})
    elif typ == 'vless':
        o.update({'type': 'vless', 'server': server, 'server_port': port,
                  'uuid': n.get('uuid')})
        if n.get('flow'):
            o['flow'] = n['flow']
    elif typ == 'trojan':
        o.update({'type': 'trojan', 'server': server, 'server_port': port,
                  'password': n.get('password') or ''})
    elif typ == 'ss':
        o.update({'type': 'shadowsocks', 'server': server, 'server_port': port,
                  'method': n.get('cipher') or 'aes-256-gcm',
                  'password': n.get('password') or ''})
    elif typ == 'hysteria2':
        o.update({'type': 'hysteria2', 'server': server, 'server_port': port,
                  'password': n.get('password') or ''})
    elif typ == 'tuic':
        o.update({'type': 'tuic', 'server': server, 'server_port': port,
                  'uuid': n.get('uuid') or '', 'password': n.get('password') or ''})
    elif typ == 'http':
        o.update({'type': 'http', 'server': server, 'server_port': port})
        if n.get('username'):
            o['username'] = n['username']
        if n.get('password'):
            o['password'] = n['password']
    elif typ in ('socks5', 'socks'):
        o.update({'type': 'socks', 'version': '5', 'server': server,
                  'server_port': port})
        if n.get('username'):
            o['username'] = n['username']
        if n.get('password'):
            o['password'] = n['password']
    else:
        return None

    tls = build_tls(n)
    if tls:
        o['tls'] = tls
    tr = build_transport(n)
    if tr:
        o['transport'] = tr
    return o


def main():
    if not CHAIN.exists():
        log('docs/chain.yaml missing')
        return 1

    chain = yaml.safe_load(CHAIN.read_text(encoding='utf-8'))
    proxies = chain.get('proxies') or []
    if not proxies:
        log('chain.yaml has no proxies')
        return 1

    # 找落地节点（带 dialer-proxy 的那个）
    landing = next((p for p in proxies if p.get('dialer-proxy') == G_MASTER), None)
    if not landing:
        log('landing node (dialer-proxy) not found in chain.yaml')
        return 1
    fronts = [p for p in proxies if p['name'] != landing['name']]
    log(f'chain.yaml: {len(fronts)} fronts + 1 landing')

    outbounds, tags = [], []
    for i, n in enumerate(fronts):
        tag = f'n{i:02d}'
        o = to_outbound(n, tag)
        if o:
            outbounds.append(o)
            tags.append(tag)
    log(f'mapped {len(tags)}/{len(fronts)} fronts')

    ltag = landing['name']
    outbounds.append({
        'type': 'shadowsocks', 'tag': ltag,
        'server': landing['server'],
        'server_port': int(landing['port']),
        'method': landing.get('cipher') or 'chacha20-ietf-poly1305',
        'password': landing.get('password') or '',
        'detour': G_MASTER,
    })

    outbounds.append({'type': 'selector', 'tag': G_FRONT,
                      'outbounds': tags, 'default': tags[0]})
    outbounds.append({'type': 'urltest', 'tag': G_AUTO,
                      'outbounds': tags, 'url': STRICT_URL,
                      'interval': '3m', 'tolerance': 80})
    outbounds.append({'type': 'selector', 'tag': G_MASTER,
                      'outbounds': [G_AUTO, G_FRONT] + tags,
                      'default': G_AUTO})
    outbounds.append({'type': 'selector', 'tag': G_PROXY,
                      'outbounds': [ltag, G_MASTER, 'direct'],
                      'default': ltag})
    outbounds.append({'type': 'direct', 'tag': 'direct'})

    doc = {
        'log': {'level': 'warn', 'timestamp': True},
        # sing-box 1.12+ 新式 DNS 格式；不给 dns-direct 写 detour:direct
        # （空 direct 出站当 detour 目标会被拒绝）
        'dns': {
            'servers': [
                {'type': 'https', 'tag': 'dns-proxy',
                 'server': '1.1.1.1', 'detour': G_PROXY},
                {'type': 'https', 'tag': 'dns-direct',
                 'server': '223.5.5.5'},
            ],
            'final': 'dns-proxy',
        },
        'inbounds': [
            {'type': 'tun', 'tag': 'tun-in', 'address': ['172.19.0.1/30'],
             'auto_route': True, 'strict_route': True, 'stack': 'mixed'},
            {'type': 'mixed', 'tag': 'mixed-in', 'listen': '127.0.0.1',
             'listen_port': 2080},
        ],
        'outbounds': outbounds,
        'route': {
            'rules': [{'action': 'route', 'ip_is_private': True,
                       'outbound': 'direct'}],
            'final': G_PROXY,
            'auto_detect_interface': True,
            'default_domain_resolver': 'dns-direct',
        },
        'experimental': {'cache_file': {'enabled': True}},
    }

    text = json.dumps(doc, ensure_ascii=False, indent=2)
    for d in ('docs', 'dist'):
        p = ROOT / d
        p.mkdir(exist_ok=True)
        (p / 'singbox.json').write_text(text, encoding='utf-8', newline='\n')

    # 自检
    back = json.loads(text)
    tagnames = [o['tag'] for o in back['outbounds']]
    ln = next((o for o in back['outbounds'] if o['tag'] == ltag), None)
    checks = [
        ('JSON 可解析', True),
        ('DNS 新格式', all('type' in s and 'address' not in s
                          for s in back['dns']['servers'])),
        ('DNS 无 detour:direct',
         not any(s.get('detour') == 'direct' for s in back['dns']['servers'])),
        ('四组齐备', all(g in tagnames for g in
                        (G_FRONT, G_AUTO, G_MASTER, G_PROXY))),
        ('落地挂 detour', bool(ln) and ln.get('detour') == G_MASTER),
        ('落地端口一致', bool(ln) and ln['server_port'] == int(landing['port'])),
        ('route.final 指向代理', back['route']['final'] == G_PROXY),
        ('有 default_domain_resolver',
         'default_domain_resolver' in back['route']),
        ('节点数与 chain.yaml 一致', len(tags) == len(fronts)),
    ]
    ok = True
    for label, c in checks:
        log(f"  [{'OK' if c else 'FAIL'}] {label}")
        ok &= c

    log(f'written docs/singbox.json + dist/singbox.json '
        f'({len(tags)} nodes, {len(text)}B)')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
