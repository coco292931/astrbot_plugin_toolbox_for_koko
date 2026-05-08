with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = ['_build_qweather_auth', '_get_geo_host']
for name in targets:
    for i, line in enumerate(lines):
        stripped = line.strip()
        if (stripped.startswith('def ') or stripped.startswith('async def ')) and name in stripped:
            indent = len(line) - len(line.lstrip())
            start = i
            end = i + 1
            while end < len(lines):
                nl = lines[end]
                ns = nl.strip()
                if ns and not ns.startswith('#') and not ns.startswith('"""') and not ns.startswith("'''"):
                    ni = len(nl) - len(nl.lstrip())
                    if ni <= indent and (ns.startswith('def ') or ns.startswith('async def ') or ns.startswith('class ')):
                        break
                end += 1
            print(f'{name}: lines {start+1}-{end}')
            for j in range(max(0,start-2), min(len(lines), end+2)):
                marker = '>>>' if start <= j < end else '   '
                print(f'{marker} {j+1}: {lines[j].rstrip()}')
            print()