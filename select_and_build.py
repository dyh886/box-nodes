import json
import subprocess
import sys
from pathlib import Path

BOX = Path(r'D:\AI_Workstation\Work\日常工作\box-nodes')
ENV = {k: v for k, v in __import__('os').environ.items()
       if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
ENV['PYTHONIOENCODING'] = 'utf-8'
ENV['NO_PROXY'] = '*'
ENV['no_proxy'] = '*'

stab = json.loads((BOX / 'state' / 'stability.json').read_text(encoding='utf-8'))
live = json.loads((BOX / 'state' / 'live_nodes.json').read_text(encoding='utf-8'))

# 入池标准：三轮全通，或至少两轮通过（剩下的交给客户端 url-test 淘汰）
stable = {s['name'] for s in stab if s['okRounds'] == s['of']}
partial = {s['name'] for s in stab if s['okRounds'] >= 2}

kept = [n for n in live['nodes'] if n['name'] in stable]
if len(kept) < 4:
    kept = [n for n in live['nodes'] if n['name'] in partial]

order = {s['name']: (0 if s['name'] in stable else 1, s['okRounds'] * -1,
                     json.loads(json.dumps(n.get('l2Ms') or 9e9)))
         for n in [] for s in []}
kept.sort(key=lambda n: (
    0 if n['name'] in stable else 1,
    n.get('l2Ms') or 9e9,
))

print(f'stable={len(stable)} partial={len(partial)} -> keep {len(kept)}')
for n in kept:
    r = next(s for s in stab if s['name'] == n['name'])
    mark = 'STABLE' if n['name'] in stable else 'partial'
    print(f"  [{mark:7s}] {r['okRounds']}/3  {n['name']}")

live['nodes'] = kept
live['count'] = len(kept)
st = dict(live.get('stats', {}))
st['stablePassed'] = len(stable)
st['poolKept'] = len(kept)
live['stats'] = st
(BOX / 'state' / 'live_nodes.json').write_text(
    json.dumps(live, ensure_ascii=False, indent=1), encoding='utf-8')

p = subprocess.run([sys.executable, 'build_live.py', 'build'], cwd=BOX,
                   capture_output=True, text=True, encoding='utf-8',
                   errors='replace', env=ENV)
print((p.stderr or '').strip()[-500:])
sys.exit(p.returncode)
