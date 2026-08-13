#!/usr/bin/env python3
"""
从 temp-guidance/failed_cases/ 下每个 case 的构建日志尾部中提取错误特征行。

输出:
  temp-guidance/failed_cases/<case_dir>/error_lines.json

用法:
  cd /home/zhaochenyu/buildbench_competition
  python Build-bench-xuan/tools/extract_error_lines.py
"""

import gzip
import json
import os
import re
import sys

# ─── 路径配置 ───────────────────────────────────────────────────────
FAILED_CASES_DIR = "/home/zhaochenyu/buildbench_competition/temp-guidance/failed_cases"

# ─── 错误模式定义 ───────────────────────────────────────────────────

# 每个类别下的匹配模式列表（每个模式是一个正则表达式字符串）
ERROR_PATTERNS = {
    "compilation": [
        re.compile(r'\berror:'),           # error: 前缀
        re.compile(r'\bError:'),           # Error: 前缀（大小写敏感）
        re.compile(r'\bfatal error\b'),     # fatal error
        re.compile(r'\bundefined reference\b'),  # undefined reference
        re.compile(r'\bundefined symbol\b'),     # undefined symbol
        re.compile(r'-mmmx\b', re.IGNORECASE),
        re.compile(r'-msse4a\b', re.IGNORECASE),
        re.compile(r'-msha\b', re.IGNORECASE),
        re.compile(r'-maes\b', re.IGNORECASE),
        re.compile(r'-mrdrnd\b', re.IGNORECASE),
        re.compile(r'-mavx2\b', re.IGNORECASE),
        re.compile(r'-mfma\b', re.IGNORECASE),
        re.compile(r'mno-omit-leaf-frame-pointer\b', re.IGNORECASE),
        re.compile(r'-mno-', re.IGNORECASE),
    ],
    "dependency": [
        re.compile(r'\bunmet build dependencies\b'),
        re.compile(r'\bpackage not found\b', re.IGNORECASE),
        re.compile(r'\bUnable to locate package\b'),
        re.compile(r'\bcannot find -l\b'),
        re.compile(r'\bNo package\b'),
        re.compile(r'\bconflicts with\b'),
        re.compile(r'build dependencies not satisfied', re.IGNORECASE),
        re.compile(r'cannot open shared object file', re.IGNORECASE),
        re.compile(r'error while loading shared libraries', re.IGNORECASE),
        re.compile(r'given.back', re.IGNORECASE),  # Status: given-back 表示构建被退回
    ],
    "linker": [
        re.compile(r'/usr/bin/ld:'),
        re.compile(r'collect2:\s*error'),
        re.compile(r'\bld returned\b'),
        re.compile(r'\bcannot find\b'),
    ],
    "packaging": [
        re.compile(r'dpkg-source:\s*error'),
        re.compile(r'dh_\w+:\s*'),          # dh_ 开头的失败行（如 dh_install:）
        re.compile(r'\bdpkg-checkbuilddeps\b'),
        re.compile(r'no binary artifacts found', re.IGNORECASE),
        re.compile(r'dpkg-genbuildinfo.*no binary artifacts', re.IGNORECASE),
        re.compile(r'dpkg-deb:.*error.*paste subprocess', re.IGNORECASE),
        re.compile(r'Broken pipe', re.IGNORECASE),
        re.compile(r'no binary artifacts', re.IGNORECASE),
    ],
    "environment": [
        re.compile(r'\bKilled\b', re.IGNORECASE),  # 匹配 "Build killed with signal TERM" 等
        re.compile(r'\bOOM\b'),
        re.compile(r'\btimeout\b', re.IGNORECASE),
        re.compile(r'Build killed with signal', re.IGNORECASE),
        re.compile(r'\bcommand not found\b'),
        re.compile(r'\bPermission denied\b'),
        re.compile(r'\bNo such file or directory\b'),
        re.compile(r'No space left on device', re.IGNORECASE),
        re.compile(r'Disk quota exceeded', re.IGNORECASE),
    ],
    "architecture": [
        re.compile(r'\bunrecognized command-line option\b'),
        re.compile(r'\b-march='),
        re.compile(r'\b-mavx\b'),
        re.compile(r'\b-msse\b'),
        re.compile(r'\b-mfpu='),
        re.compile(r'/usr/lib/x86_64-linux-gnu'),
        re.compile(r'\bUnsupported arch\b'),
        re.compile(r'\bunsupported architecture\b'),
        re.compile(r'/lib/x86_64-linux-gnu', re.IGNORECASE),
        re.compile(r'x86_64-linux-gnu/', re.IGNORECASE),  # 带斜杠的路径，避免匹配编译器名
    ],
    "test": [
        re.compile(r'FAIL:.*test', re.IGNORECASE),
        re.compile(r'test.*failed', re.IGNORECASE),
        re.compile(r'tests failed', re.IGNORECASE),
        re.compile(r'test:.*error', re.IGNORECASE),
    ],
    "compiler_internal": [
        re.compile(r'internal error: builtin function', re.IGNORECASE),
        re.compile(r'internal compiler error', re.IGNORECASE),
        # 注意：不要用 ICE: 匹配，因为会误匹配 NOTICE: 中的 ICE:
        re.compile(r'\bICE\b', re.IGNORECASE),  # 使用\b单词边界避免匹配NOTICE
    ],
}

# 用于 error_summary 的中文分类名
CATEGORY_NAMES = {
    "compilation": "编译错误",
    "dependency": "依赖错误",
    "linker": "链接错误",
    "packaging": "打包错误",
    "environment": "环境错误",
    "architecture": "架构错误",
    "test": "测试错误",
    "compiler_internal": "编译器内部错误",
}


def is_comment_or_header(line):
    """判断是否为文件头部的注释行（以 # 开头）"""
    return line.startswith('#')


def extract_errors_from_tail(tail_path):
    """从 tail 文件中提取所有类别的错误行。

    返回:
        (error_lines_dict, has_errors)
    """
    error_lines = {cat: [] for cat in ERROR_PATTERNS}

    try:
        with open(tail_path, 'r', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        # 文件无法读取，返回空
        return error_lines, False

    for line in lines:
        # 跳过注释/头部行
        if is_comment_or_header(line):
            continue

        stripped = line.rstrip('\n')
        if not stripped:
            continue

        # 对每一行，检查每个类别的模式
        for category, patterns in ERROR_PATTERNS.items():
            for pattern in patterns:
                if pattern.search(stripped):
                    error_lines[category].append(stripped)
                    break  # 一行只计入一个类别，避免重复

    has_errors = any(len(v) > 0 for v in error_lines.values())
    return error_lines, has_errors


def build_error_summary(error_lines):
    """根据提取的错误行生成中文摘要字符串。"""
    parts = []
    for category, lines in error_lines.items():
        if lines:
            cn_name = CATEGORY_NAMES.get(category, category)
            parts.append(f"{cn_name}: {len(lines)}条")
    return ", ".join(parts) if parts else "无错误"


def extract_errors_from_gzip(gzip_path, max_lines=5000):
    """从 gzip 压缩的完整构建日志中提取错误行。

    为避免内存问题，只读取日志末尾的 max_lines 行。
    使用与 tail 文件相同的错误模式匹配。

    返回:
        (error_lines_dict, has_errors)
    """
    error_lines = {cat: [] for cat in ERROR_PATTERNS}

    if not os.path.isfile(gzip_path):
        return error_lines, False

    try:
        with gzip.open(gzip_path, 'rt', errors='replace') as f:
            # 只保留末尾 max_lines 行
            lines = f.readlines()
            tail_lines = lines[-max_lines:] if len(lines) > max_lines else lines
    except Exception:
        return error_lines, False

    for line in tail_lines:
        stripped = line.rstrip('\n')
        if not stripped:
            continue

        for category, patterns in ERROR_PATTERNS.items():
            for pattern in patterns:
                if pattern.search(stripped):
                    # 去重：避免重复添加相同的行
                    if stripped not in error_lines[category]:
                        error_lines[category].append(stripped)
                    break

    has_errors = any(len(v) > 0 for v in error_lines.values())
    return error_lines, has_errors


def process_case(case_dir):
    """处理单个 case 目录。

    优先从 tail 文件中提取错误行；如果 tail 中没有错误，
    则回退到 gzip 完整日志（末尾 5000 行）。

    返回:
        (success, skip_reason)
        success: True 表示成功提取，False 表示跳过
        skip_reason: 如果跳过，说明原因
    """
    tail_path = os.path.join(case_dir, "3_original_target.log.tail")
    gzip_path = os.path.join(case_dir, "2_original_target.log.gz")
    info_path = os.path.join(case_dir, "1_case_info.json")
    output_path = os.path.join(case_dir, "error_lines.json")

    # 读取 case_info
    case_info = {}
    if os.path.isfile(info_path):
        try:
            with open(info_path, 'r') as f:
                case_info = json.load(f)
        except (json.JSONDecodeError, OSError):
            case_info = {}

    # 优先从 tail 文件提取
    tail_available = os.path.isfile(tail_path)
    has_tail_errors = False

    if tail_available:
        try:
            if os.path.getsize(tail_path) > 0:
                error_lines, has_tail_errors = extract_errors_from_tail(tail_path)
        except OSError:
            has_tail_errors = False

    # 如果 tail 没有错误，回退到 gzip 完整日志
    if not has_tail_errors and os.path.isfile(gzip_path):
        error_lines, has_gzip_errors = extract_errors_from_gzip(gzip_path)
        log_source = "gzip_fallback"
        has_errors = has_gzip_errors
    elif has_tail_errors:
        log_source = "tail"
        has_errors = True
    else:
        error_lines = {cat: [] for cat in ERROR_PATTERNS}
        log_source = "none"
        has_errors = False

    error_summary = build_error_summary(error_lines)

    # 组装输出
    output = {
        "case_id": case_info.get("case_id", os.path.basename(case_dir)),
        "package_name": case_info.get("package_name", ""),
        "direction": case_info.get("direction", ""),
        "release": case_info.get("release", ""),
        "source_arch": case_info.get("source_arch", ""),
        "target_arch": case_info.get("target_arch", ""),
        "error_lines": error_lines,
        "error_summary": error_summary,
        "has_errors": has_errors,
        "log_source": log_source,
    }

    # 写入输出文件
    try:
        with open(output_path, 'w') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
    except OSError as e:
        return False, f"写入失败: {e}"

    return True, None


def main():
    # 确保目录存在
    if not os.path.isdir(FAILED_CASES_DIR):
        print(f"错误: 目录不存在 {FAILED_CASES_DIR}", file=sys.stderr)
        sys.exit(1)

    # 遍历所有 case 目录
    case_dirs = sorted([
        d for d in os.listdir(FAILED_CASES_DIR)
        if os.path.isdir(os.path.join(FAILED_CASES_DIR, d))
    ])

    total = len(case_dirs)
    success_count = 0
    skip_count = 0
    skip_reasons = {}

    for idx, case_name in enumerate(case_dirs, 1):
        case_dir = os.path.join(FAILED_CASES_DIR, case_name)

        try:
            success, reason = process_case(case_dir)
            if success:
                success_count += 1
            else:
                skip_count += 1
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        except (BrokenPipeError, IOError):
            # 处理 broken pipe 等异常
            skip_count += 1
            skip_reasons.get("broken_pipe", 0)
            skip_reasons["broken_pipe"] = skip_reasons.get("broken_pipe", 0) + 1
        except Exception as e:
            skip_count += 1
            reason = f"异常: {type(e).__name__}"
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        # 进度输出
        if idx % 200 == 0 or idx == total:
            print(f"  进度: {idx}/{total}...", flush=True)

    # ── 输出统计 ──
    print(f"\n{'='*50}")
    print("处理完成!")
    print(f"{'='*50}")
    print(f"总计: {total} cases")
    print(f"成功提取错误行: {success_count} cases")
    print(f"跳过: {skip_count} cases")
    if skip_reasons:
        print(f"\n跳过原因分布:")
        for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")


if __name__ == '__main__':
    main()