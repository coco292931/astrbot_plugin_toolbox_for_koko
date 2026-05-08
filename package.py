#!/usr/bin/env python3
"""
Toolbox 打包脚本
根据 .gitignore 规则打包源码为 zip 文件，并排除 .gitignore 本身。

用法: python package.py
输出: astrbot_plugin_toolbox_for_koko.zip
"""

import os
import zipfile
import re
from pathlib import Path
from zipfile import ZipInfo

ROOT_DIR = Path(__file__).parent.resolve()
GITIGNORE_FILE = ROOT_DIR / ".gitignore"
OUTPUT_ZIP = ROOT_DIR / "astrbot_plugin_toolbox_for_koko.zip"
IGNORE_SELF = True  # 打包结果中排除 .gitignore

# git 始终忽略的目录（即使 .gitignore 里没写）
IMPLICIT_IGNORE = {".git"}

# 硬编码需要额外排除的文件（打包结果中不包含）
HARDCODED_EXCLUDE = {OUTPUT_ZIP.name}

# zip 内文件包裹的顶层目录名（AstrBot 期望插件文件在子目录中）
ZIP_ROOT_DIR_NAME = "astrbot_plugin_toolbox_for_koko"


def parse_gitignore(gitignore_path):
    """解析 .gitignore 文件，返回 (编译后的正则列表, 是否取反列表)"""
    if not gitignore_path.exists():
        return [], []

    patterns = []       # 编译后的正则
    negations = []      # 对应的否定标记
    with open(gitignore_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            is_negation = False
            raw = line

            if raw.startswith("!"):
                is_negation = True
                raw = raw[1:]

            dir_only = raw.endswith("/")
            if dir_only:
                raw = raw.rstrip("/")

            regex = ""
            i = 0
            while i < len(raw):
                c = raw[i]
                if c == '*':
                    if i + 1 < len(raw) and raw[i + 1] == '*':
                        j = i + 2
                        if j < len(raw) and raw[j] == '/':
                            j += 1
                        i = j
                        regex += "(.*/)?"
                        continue
                    else:
                        regex += "[^/]*"
                elif c == '?':
                    regex += "[^/]"
                elif c == '[':
                    bracket_end = raw.find(']', i)
                    if bracket_end == -1:
                        regex += re.escape(c)
                    else:
                        inner = raw[i+1:bracket_end]
                        regex += "[" + inner + "]"
                        i = bracket_end
                else:
                    regex += re.escape(c)
                i += 1

            if raw.startswith("/"):
                full_regex = "^" + regex[1:]
            else:
                full_regex = "(.*/)?" + regex

            if dir_only:
                full_regex += "(/.*)?$"
            else:
                full_regex += "$"

            try:
                compiled = re.compile(full_regex)
                patterns.append(compiled)
                negations.append(is_negation)
            except re.error as e:
                print(f"  [警告] 忽略无效的 gitignore 规则 '{line}': {e}")

    return patterns, negations


def should_ignore(rel_path_str, patterns, negations, implicit_ignore):
    """
    判断相对路径是否应被忽略。
    返回 True 表示应该排除。
    """
    rel_path_str = rel_path_str.replace("\\", "/")

    # 检查隐式忽略规则（如 .git/）
    for prefix in implicit_ignore:
        if rel_path_str == prefix or rel_path_str.startswith(prefix + "/"):
            return True

    # 检查 .gitignore 规则
    for pattern, is_neg in zip(patterns, negations):
        if pattern.search(rel_path_str):
            if is_neg:
                return False
            return True

    return False


def collect_files(root_dir, patterns, negations, implicit_ignore, exclude_files=None):
    """收集所有应该包含的文件"""
    if exclude_files is None:
        exclude_files = set()

    files_to_pack = []
    root_dir = Path(root_dir)

    for entry in sorted(root_dir.rglob("*")):
        if not entry.is_file():
            continue

        rel_path = entry.relative_to(root_dir)
        rel_str = str(rel_path).replace("\\", "/")

        if rel_str in exclude_files:
            continue

        if should_ignore(rel_str, patterns, negations, implicit_ignore):
            continue

        files_to_pack.append(entry)

    return files_to_pack


def main():
    print("=" * 60)
    print("Toolbox 打包工具")
    print("=" * 60)

    # 解析 .gitignore
    print(f"\n[1] 解析 .gitignore...")
    patterns, negations = parse_gitignore(GITIGNORE_FILE)
    print(f"  → 解析到 {len(patterns)} 条规则")

    # 隐式忽略
    print(f"  → 隐式忽略目录: {', '.join(IMPLICIT_IGNORE)}")

    # 硬编码排除的文件 + .gitignore 本身
    exclude = set(HARDCODED_EXCLUDE)
    if IGNORE_SELF:
        exclude.add(".gitignore")

    # 收集文件
    print(f"\n[2] 扫描文件...")
    files = collect_files(ROOT_DIR, patterns, negations, IMPLICIT_IGNORE, exclude)
    print(f"  → 共发现 {len(files)} 个文件需要打包")

    # 统计文件数
    total_size = 0
    print(f"\n[3] 文件清单：")
    for f in sorted(files):
        rel = f.relative_to(ROOT_DIR)
        sz = f.stat().st_size
        total_size += sz
        print(f"  + {rel} ({sz:,} bytes)")

    print(f"\n[4] 创建压缩包...")
    print(f"  → 目标: {OUTPUT_ZIP.name}")

    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        # 先写入目录条目本身，确保它是 zip 内第一个条目
        # AstrBot 的 unzip_file 取 z.namelist()[0] 作为 update_dir，然后 os.listdir(update_dir)
        dir_entry = ZipInfo(ZIP_ROOT_DIR_NAME + "/")
        dir_entry.date_time = (2025, 1, 1, 0, 0, 0)
        zf.writestr(dir_entry, "")
        for f in files:
            rel = f.relative_to(ROOT_DIR)
            # 文件放入顶层插件目录下，符合 AstrBot 解压期望
            zf.write(f, f"{ZIP_ROOT_DIR_NAME}/{rel}")

    print(f"\n[5] 打包完成!")
    print(f"  → 输出文件: {OUTPUT_ZIP}")
    print(f"  → 文件数: {len(files)}")
    total_compressed = OUTPUT_ZIP.stat().st_size
    print(f"  → 总大小: {total_size:,} bytes → {total_compressed:,} bytes (压缩后)")
    if total_size > 0:
        ratio = (1 - total_compressed / total_size) * 100
        print(f"  → 压缩率: {ratio:.1f}%")
    print("=" * 60)


if __name__ == "__main__":
    main()