"""检查 main.py 中的 _handle_* 方法是否还有外部引用"""
import re

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 检查 main.py 中那些已迁移到 tools/ 的 _handle_* 方法
handles_to_check = [
    '_handle_weather(',
    '_handle_weather_history(',
    '_handle_search(',
    '_handle_fetch_url(',
    '_handle_history(',
]

for name in handles_to_check:
    bare_name = name.rstrip('(')
    matches = list(re.finditer(re.escape(bare_name), content))
    def_count = 0
    call_count = 0
    call_lines = []
    for m in matches:
        ln = content[:m.start()].count('\n') + 1
        ctx = content[max(0,m.start()-15):m.start()]
        if 'def ' in ctx:
            def_count += 1
        else:
            call_count += 1
            call_lines.append(ln)
    
    print(f"{bare_name}:")
    print(f"  def={def_count}, calls={call_count}")
    if call_lines:
        print(f"  被调用行号: {call_lines}")
    
    # 如果只有定义但没有调用（且没有被 run_tool 路由），那就可以删除
    if def_count > 0 and call_count == 0:
        print(f"  >>> 可以安全删除（无外部引用）")

print("\n=== 检查 _collect_forwarded_output_text 的引用情况 ===")
for m in re.finditer(re.escape('_collect_forwarded_output_text'), content):
    ln = content[:m.start()].count('\n') + 1
    ctx = content[max(0,m.start()-15):m.start()]
    if 'def ' in ctx:
        print(f"  定义在行 {ln}")
    else:
        print(f"  被调用在行 {ln}")

print("\n=== 检查 _run_tool_ 方法的路由情况 ===")
# 检查那些已经迁移到 tools/ 的 _run_tool_ 方法是否通过 tools 模块调用
run_tool_pattern = re.finditer(r'async def _run_tool_(\w+)', content)
for m in run_tool_pattern:
    name = m.group(1)
    ln = content[:m.start()].count('\n') + 1
    # 读取该方法的 body
    # 简单地检查后面几行
    body_start = m.end()
    body = content[body_start:body_start+200]
    # 检查是否调用了 tools.xxx
    uses_tools = 'from tools.' in body or 'tools.' in body
    uses_self_handle = '_handle_' in body
    print(f"  _run_tool_{name} (line {ln}): tools_module={uses_tools}, self_handle={uses_self_handle}")