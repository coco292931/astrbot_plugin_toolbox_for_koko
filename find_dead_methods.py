with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = [
    '_handle_location',
    '_handle_weather', 
    '_handle_weather_history',
    '_handle_search',
    '_handle_fetch_url',
    '_handle_history',
]

method_starts = {}
for i, line in enumerate(lines):
    stripped = line.strip()
    for t in targets:
        if stripped.startswith(f'async def {t}('):
            method_starts[t] = i  # 0-based
            break

for t in targets:
    if t in method_starts:
        start = method_starts[t]
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = start + 1
        while end < len(lines):
            l = lines[end]
            if l.strip() and not l.strip().startswith('#') and not l.strip().startswith('"""'):
                lindent = len(l) - len(l.lstrip())
                if lindent == indent and l.strip().startswith(('async def ', 'def ')):
                    break
            end += 1
        print(f'{t}: lines {start+1}-{end}')