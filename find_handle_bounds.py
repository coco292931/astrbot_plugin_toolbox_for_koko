import re

with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = {
    '_handle_weather': {'cnt': 0, 'ranges': []},
    '_handle_weather_history': {'cnt': 0, 'ranges': []},
    '_handle_search': {'cnt': 0, 'ranges': []},
    '_handle_fetch_url': {'cnt': 0, 'ranges': []},
    '_handle_history': {'cnt': 0, 'ranges': []},
}

for i, line in enumerate(lines, 1):
    stripped = line.strip()
    for t in targets:
        if stripped.startswith('async def ' + t + '(') or stripped.startswith('def ' + t + '('):
            targets[t]['cnt'] += 1
            indent = len(line) - len(line.lstrip())
            end = i + 1
            while end <= len(lines):
                nl = lines[end - 1]
                ns = nl.strip()
                if ns and not ns.startswith('#') and not ns.startswith('"""') and not ns.startswith("'''"):
                    ni = len(nl) - len(nl.lstrip())
                    if ni <= indent and (ns.startswith('async def ') or ns.startswith('def ') or ns.startswith('class ')):
                        break
                end += 1
            targets[t]['ranges'].append((i, end - 1))

for t, info in targets.items():
    if info['cnt'] > 0:
        print(f'{t}:')
        for j, (start, end) in enumerate(info['ranges'], 1):
            print(f'  定义 {j}: 行 {start}-{end} ({end-start+1} 行)')