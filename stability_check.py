import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BOX = Path(r'D:\AI_Workstation\Work\日常工作\box-nodes')
SB = Path(r'D:\AI_Workstation\Work\日常工作\vps\bin\sing-box-1.12.9-windows-amd64\sing-box.exe')
sys.path.insert(0, str(BOX))
import importlib.util

spec = importlib.util.spec_from_file_location('l2', BOX / 'l2_verify.py')
l2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(l2)

import yaml
live = json.loads((BOX / 'state' / 'live_nodes.json').read_text(encoding='utf-8'))
cfg_all = json.loads((BOX / 'config.json').read_text(encoding='utf-8'))
land_cfg = cfg_all['landing_node']
landing = {
    'server': land_cfg['server'],
    'port': int(land_cfg['port']),
    'cipher': land_cfg.get('cipher') or land_cfg.get('method'),
    'password': land_cfg.get('password') or '',
}
fronts = live['nodes']

BASE = 28300
inb, obs, rules = [], [], []
for i, n in enumerate(fronts):
    tag = f'n{i}'
    ob = l2.node_to_outbound(n, tag)
    inb.append({'type': 'socks', 'tag': f'i{i}', 'listen': '127.0.0.1',
                'listen_port': BASE + i})
    obs.append(ob)
    obs.append({'type': 'shadowsocks', 'tag': f'l{i}',
                'server': landing['server'],
                'server_port': int(landing.get('port') or landing.get('server_port')),
                'method': landing.get('cipher') or landing.get('method'),
                'password': landing['password'],
                'detour': tag})
    rules.append({'inbound': [f'i{i}'], 'outbound': f'l{i}'})
obs.append({'type': 'direct', 'tag': 'direct'})
cfg = {'log': {'level': 'error'}, 'inbounds': inb, 'outbounds': obs,
       'route': {'rules': rules, 'final': 'direct'}}
cp = Path(os.environ['TEMP']) / 'stab.json'
cp.write_text(json.dumps(cfg), encoding='utf-8')

proc = subprocess.Popen([str(SB), 'run', '-c', str(cp)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(6)

ROUNDS = 3
TARGETS = [('https://www.google.com/generate_204', '204'),
           ('https://github.com/', '200')]


def check(i):
    port = BASE + i
    okc = 0
    worst = 0
    detail = []
    for r in range(ROUNDS):
        allok = True
        for url, exp in TARGETS:
            p = subprocess.run(
                ['curl.exe', '-q', '-sS', '--noproxy', 'localhost',
                 '--proxy', f'socks5h://127.0.0.1:{port}',
                 '--connect-timeout', '6', '--max-time', '14',
                 url, '-o', 'NUL', '-w', '%{http_code}'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=18)
            code = (p.stdout or '').strip()
            if code != exp:
                allok = False
                detail.append(f'r{r}:{url.split("/")[2]}={code or "000"}')
                break
        if allok:
            okc += 1
        time.sleep(0.3)
    return i, okc, '; '.join(detail[:2])


rows = []
with cf.ThreadPoolExecutor(max_workers=len(fronts)) as ex:
    for i, okc, d in ex.map(check, range(len(fronts))):
        rows.append({'name': fronts[i]['name'], 'okRounds': okc,
                     'of': ROUNDS, 'detail': d})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

proc.terminate()
try:
    proc.wait(timeout=4)
except subprocess.TimeoutExpired:
    proc.kill()
cp.unlink(missing_ok=True)

stable = [r for r in rows if r['okRounds'] == ROUNDS]
print(json.dumps({'stable': len(stable), 'total': len(rows),
                  'names': [r['name'] for r in stable]}, ensure_ascii=False))
Path(BOX / 'state' / 'stability.json').write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
