import re

with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

targets = [
    '_handle_search_memory_vector',
    '_handle_list_memory_vector', 
    '_handle_list_records_memory_vector',
    '_handle_remember_memory_vector',
    '_handle_delete_record_memory_vector',
]

for i, line in enumerate(lines, 1):
    stripped = line.strip()
    for t in targets:
        if stripped.startswith('async def ' + t + '('):
            print(f'{t} starts at line {i}')
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
            print(f'{t} ends at line {end - 1}')