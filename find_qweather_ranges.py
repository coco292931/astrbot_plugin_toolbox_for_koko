with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for name in ['_build_qweather_auth', '_get_geo_host', '_fetch_qweather']:
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if (stripped.startswith('def ') or stripped.startswith('async def ')) and name in stripped:
            indent = len(line) - len(line.lstrip())
            start = i
            end = i + 1
            while end <= len(lines):
                nl = lines[end - 1]
                ns = nl.strip()
                if ns and not ns.startswith('#') and not ns.startswith('"""') and not ns.startswith("'''"):
                    ni = len(nl) - len(nl.lstrip())
                    if ni <= indent and (ns.startswith('def ') or ns.startswith('async def ') or ns.startswith('class ')):
                        break
                end += 1
            print(f'{name}: {start}-{end-1}')
            for j in range(max(0, start-3), min(len(lines), end+2)):
                marker = '>>>' if start <= j+1 <= end-1 else '   '
                print(f'{marker} {j+1}: {lines[j].rstrip()}')
            print()