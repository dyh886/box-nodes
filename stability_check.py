import concurrent.futures as cf
import json
import os
import subprocess
import time
from pathlib import Path

BOX = Path(r'D:\AI_Workstation\Work\日常工作\box-nodes')
SB = Path(r'D:\AI_Workstation\Work\日常工作\vps\bin\sing-box-1.12.9-windows-amd64\sing-box.exe')
import importlib.util

spec = importlib.util.spec_from_file_location('l2', BOX / 'l2_verify.py')
l2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(l2)

live = json.loads((BOX / 'state' / 'live_nodes.json').read_text(encoding='utf-8'))
cfg_all = json.loads((BOX / 'config.json').read_text(encoding='utf-8'))
lc = cfg_all['landing_node']
landing = {'server': lc['server'], 'port': int(lc['port']),
           'cipher': lc.get('cipher') or lc.get('method'),
           'password': lc.get('password') or ''}
fronts = live['nodes']

ROUNDS = 3
# 与 l2_verify 一致：GitHub 200 必需
REQUIRED = [('https://github.com/', '200')]

# 关键修正：分批起 sing-box 实例。
# 之前把 101 个节点塞进一个实例、101 个监听端口，只等 6 秒，
# 大量端口尚未就绪 → 全部超时 → 结果 0/101（而 5 节点基准却是 5/5）。
BATCH = 12
BASE = 28300


def wait_port(port, timeout=25.0):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def run_batch(batch, bidx):
    inb, obs, rules = [], [], []
    for i, n in enumerate(batch):
        tag = f'n{i}'
        obs.append(l2.node_to_outbound(n, tag))
        inb.append({'type': 'socks', 'tag': f'i{i}', 'listen': '127.0.0.1',
                    'listen_port': BASE + i})
        obs.append({'type': 'shadowsocks', 'tag': f'l{i}',
                    'server': landing['server'], 'server_port': landing['port'],
                    'method': landing['cipher'], 'password': landing['password'],
                    'detour': tag})
        rules.append({'inbound': [f'i{i}'], 'outbound': f'l{i}'})
    obs.append({'type': 'direct', 'tag': 'direct'})
    cfg = {'log': {'level': 'error'}, 'inbounds': inb, 'outbounds': obs,
           'route': {'rules': rules, 'final': 'direct'}}
    cp = Path(os.environ['TEMP']) / f'stab{bidx}.json'
    cp.write_text(json.dumps(cfg), encoding='utf-8')

    proc = subprocess.Popen([str(SB), 'run', '-c', str(cp)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {}
    try:
        # 逐个端口确认就绪，而不是固定 sleep
        ready = [wait_port(BASE + i, 20) for i in range(len(batch))]
        if not all(ready):
            print(f'  [warn] batch{bidx}: {ready.count(False)} ports not ready',
                  flush=True)

        def check(i):
            if not ready[i]:
                return i, 0, 'port-not-ready'
            okc = 0
            detail = []
            for r in range(ROUNDS):
                allok = True
                for url, exp in REQUIRED:
                    p = subprocess.run(
                        ['curl.exe', '-q', '-sS', '--noproxy', 'localhost',
                         '--proxy', f'socks5h://127.0.0.1:{BASE + i}',
                         '--connect-timeout', '7', '--max-time', '16',
                         url, '-o', 'NUL', '-w', '%{http_code}'],
                        capture_output=True, text=True, encoding='utf-8',
                        errors='replace', timeout=20)
                    code = (p.stdout or '').strip()
                    if code != exp:
                        allok = False
                        detail.append(f'r{r}:github={code or "000"}')
                        break
                if allok:
                    okc += 1
                time.sleep(0.2)
            return i, okc, '; '.join(detail[:2])

        with cf.ThreadPoolExecutor(max_workers=min(6, len(batch))) as ex:
            for i, okc, d in ex.map(check, range(len(batch))):
                out[i] = (okc, d)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
        cp.unlink(missing_ok=True)
    return out


rows = []
for start in range(0, len(fronts), BATCH):
    batch = fronts[start:start + BATCH]
    print(f'=== batch {start//BATCH + 1} ({len(batch)} nodes) ===', flush=True)
    res = run_batch(batch, start // BATCH)
    for i, n in enumerate(batch):
        okc, d = res.get(i, (0, 'missing'))
        rows.append({'name': n['name'], 'okRounds': okc, 'of': ROUNDS,
                     'detail': d})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

stable = [r for r in rows if r['okRounds'] == ROUNDS]
partial = [r for r in rows if r['okRounds'] >= 2]
print(json.dumps({'stable': len(stable), 'partial2of3': len(partial),
                  'total': len(rows),
                  'stableNames': [r['name'] for r in stable]},
                 ensure_ascii=False))
Path(BOX / 'state' / 'stability.json').write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
