#!/usr/bin/env python3
"""
基于规则的构建失败 case 自动分类工具。

输入：
  temp-guidance/failed_cases/<case_dir>/error_lines.json

输出：
  temp-guidance/classified_failures/ 目录，按分类层次组织

用法：
  cd /home/zhaochenyu/buildbench_competition
  python Build-bench-xuan/tools/classify_failed_cases.py
  python Build-bench-xuan/tools/classify_failed_cases.py --dry-run
  python Build-bench-xuan/tools/classify_failed_cases.py --verbose
  python Build-bench-xuan/tools/classify_failed_cases.py --input-dir /path/to/failed_cases
"""

import argparse
import json
import os
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ─── 分类定义 ─────────────────────────────────────────────────────────

CATEGORY_DEFINITIONS = {
    "E2": {
        "name": "构建超时/资源不足",
        "group": "4_Environment",
        "group_name": "环境/配置类",
        "priority": 1,
    },
    "E1": {
        "name": "环境配置错误",
        "group": "4_Environment",
        "group_name": "环境/配置类",
        "priority": 2,
    },
    "E3": {
        "name": "磁盘空间不足",
        "group": "4_Environment",
        "group_name": "环境/配置类",
        "priority": 3,
    },
    "D1": {
        "name": "依赖缺失/不可用",
        "group": "1_Dependency",
        "group_name": "依赖类",
        "priority": 3,
    },
    "D2": {
        "name": "依赖版本冲突",
        "group": "1_Dependency",
        "group_name": "依赖类",
        "priority": 4,
    },
    "D3": {
        "name": "依赖架构限制",
        "group": "1_Dependency",
        "group_name": "依赖类",
        "priority": 5,
    },
    "C1": {
        "name": "编译器旗标不兼容",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 6,
    },
    "C2": {
        "name": "架构检测失败",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 7,
    },
    "C3": {
        "name": "缺失架构定义",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 8,
    },
    "C4": {
        "name": "汇编/内联汇编不兼容",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 8,
    },
    "C6": {
        "name": "编译器内部错误",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 9,
    },
    "C5": {
        "name": "库路径硬编码",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 10,
    },
    "T1": {
        "name": "测试失败",
        "group": "2_Compilation",
        "group_name": "编译类",
        "priority": 11,
    },
    "P1": {
        "name": "架构字段缺失",
        "group": "3_Packaging",
        "group_name": "打包类",
        "priority": 11,
    },
    "P2": {
        "name": "打包脚本错误",
        "group": "3_Packaging",
        "group_name": "打包类",
        "priority": 14,
    },
    "P3": {
        "name": "构建宏/工具链选择",
        "group": "3_Packaging",
        "group_name": "打包类",
        "priority": 13,
    },
    "0_Unclassified": {
        "name": "未分类",
        "group": "0_Unclassified",
        "group_name": "未分类",
        "priority": 99,
    },
}

# 子目录名映射
CATEGORY_DIR_NAMES = {
    "D1": "D1_dependency_missing",
    "D2": "D2_version_conflict",
    "D3": "D3_arch_restriction",
    "C1": "C1_compiler_flag",
    "C2": "C2_arch_detection",
    "C3": "C3_missing_arch_def",
    "C4": "C4_assembly",
    "C5": "C5_library_path",
    "C6": "C6_internal_compiler",
    "T1": "T1_test_failure",
    "P1": "P1_arch_field",
    "P2": "P2_packaging_script",
    "P3": "P3_build_macro",
    "E1": "E1_environment",
    "E3": "E3_disk_space",
    "E2": "E2_timeout_OOM",
    "0_Unclassified": "0_Unclassified",
}

GROUP_DIR_NAMES = {
    "1_Dependency": "1_Dependency",
    "2_Compilation": "2_Compilation",
    "3_Packaging": "3_Packaging",
    "4_Environment": "4_Environment",
    "0_Unclassified": "0_Unclassified",
}


# ─── 分类规则（按优先级从高到低） ─────────────────────────────────

def _match_any(text: str, patterns: List[str], case_sensitive: bool = False) -> Optional[str]:
    """检查 text 是否匹配 patterns 中的任意一个，返回匹配到的第一个 pattern。"""
    if case_sensitive:
        for p in patterns:
            if p in text:
                return p
    else:
        lower_text = text.lower()
        for p in patterns:
            if p.lower() in lower_text:
                return p
    return None


def classify_error_lines(error_lines: List[str]) -> Tuple[str, str, str]:
    """
    基于 error_lines 进行规则分类。

    返回: (category_code, category_name, error_signature)
    """
    # 将所有错误行合并为一个字符串用于匹配
    combined = "\n".join(error_lines)
    combined_lower = combined.lower()

    # ── 优先级 1: E2 - 构建超时/资源不足 ──
    e2_patterns = [
        "killed", "oom", "out of memory", "signal 9", "sigkill",
        "build timed out", "timeout", "timed out",
        "memory exhausted", "cannot allocate memory",
        "std::bad_alloc", "terminate called after throwing",
    ]
    match = _match_any(combined_lower, e2_patterns)
    if match:
        return "E2", CATEGORY_DEFINITIONS["E2"]["name"], match

    # ── 优先级 2: E1 - 环境配置错误 ──
    e1_patterns = [
        "command not found", "not found",  # 注意：这个匹配范围广，需要更精确
        "missing compiler", "cannot find toolchain",
        "permission denied", "no such file or directory",
        "no such file", "cannot execute",
        "compiler not found", "toolchain not found",
    ]
    # E1 需要更精确的匹配，避免误匹配
    e1_compiled = [
        re.compile(r'command\s+not\s+found', re.IGNORECASE),
        re.compile(r'missing\s+compiler', re.IGNORECASE),
        re.compile(r'cannot\s+find\s+toolchain', re.IGNORECASE),
        re.compile(r'permission\s+denied', re.IGNORECASE),
        re.compile(r'no\s+such\s+file\s+or\s+directory', re.IGNORECASE),
        re.compile(r'compiler\s+not\s+found', re.IGNORECASE),
        re.compile(r'toolchain\s+not\s+found', re.IGNORECASE),
        re.compile(r'cannot\s+execute\b', re.IGNORECASE),
    ]
    for line in error_lines:
        line_lower = line.lower()
        for regex in e1_compiled:
            m = regex.search(line_lower)
            if m:
                # 排除 D1 相关的 "not found"（如 package not found）
                if "package" in line_lower and "not found" in line_lower:
                    continue
                if "unable to locate" in line_lower:
                    continue
                return "E1", CATEGORY_DEFINITIONS["E1"]["name"], m.group()

    # ── 优先级 2.5: E3 - 磁盘空间不足 ──
    e3_patterns = [
        "no space left on device",
        "disk quota exceeded",
        "disk space",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in e3_patterns:
            if p in line_lower:
                return "E3", CATEGORY_DEFINITIONS["E3"]["name"], line.strip()[:120]

    # ── 优先级 3: D1 - 依赖缺失/不可用 ──
    d1_patterns = [
        "unmet build dependencies", "unmet build dependency",
        "package not found", "unable to locate package",
        "cannot find -l", "cannot find -l", "no package '",
        "dpkg-checkbuilddeps: unmet build dependencies",
        "build-dependency not found", "cannot find package",
        "E: unable to locate package",
        "could not find package", "no package found",
        "package '.*' has no installation candidate",
        "build dependencies not satisfied",
        "cannot open shared object file",
        "error while loading shared libraries",
        "given.back",
    ]
    match = _match_any(combined_lower, [p.lower() for p in d1_patterns])
    if match:
        # 找到匹配的原始文本中的具体行
        for line in error_lines:
            line_lower = line.lower()
            for p in d1_patterns:
                if p.lower() in line_lower:
                    return "D1", CATEGORY_DEFINITIONS["D1"]["name"], line.strip()[:120]
        return "D1", CATEGORY_DEFINITIONS["D1"]["name"], match

    # ── 优先级 4: D2 - 依赖版本冲突 ──
    d2_patterns = [
        "conflicts with", "version requirement not met",
        "depends: .* but .* is installed",
        "but .* is to be installed",
        "dependency resolution failed",
        "unsatisfied dependency",
        "version conflict",
        "dependency .* cannot be satisfied",
        "no available version of",
    ]
    d2_compiled = [
        re.compile(r'conflicts\s+with', re.IGNORECASE),
        re.compile(r'version\s+requirement\s+not\s+met', re.IGNORECASE),
        re.compile(r'depends:.*but.*is\s+installed', re.IGNORECASE),
        re.compile(r'but.*is\s+to\s+be\s+installed', re.IGNORECASE),
        re.compile(r'dependency\s+resolution\s+failed', re.IGNORECASE),
        re.compile(r'unsatisfied\s+dependency', re.IGNORECASE),
        re.compile(r'version\s+conflict', re.IGNORECASE),
        re.compile(r'dependency.*cannot\s+be\s+satisfied', re.IGNORECASE),
        re.compile(r'no\s+available\s+version\s+of', re.IGNORECASE),
    ]
    for line in error_lines:
        line_lower = line.lower()
        for regex in d2_compiled:
            if regex.search(line_lower):
                return "D2", CATEGORY_DEFINITIONS["D2"]["name"], line.strip()[:120]

    # ── 优先级 5: D3 - 依赖架构限制 ──
    d3_patterns = [
        re.compile(r'build-depends:.*\[.*\]', re.IGNORECASE),
        re.compile(r'architecture.*restriction', re.IGNORECASE),
        re.compile(r'arch.*not.*support', re.IGNORECASE),
        re.compile(r'not.*for.*architecture', re.IGNORECASE),
        re.compile(r'only.*supported.*on.*arch', re.IGNORECASE),
        re.compile(r'\[amd64\]', re.IGNORECASE),
        re.compile(r'\[i386\]', re.IGNORECASE),
    ]
    for line in error_lines:
        for regex in d3_patterns:
            if regex.search(line):
                return "D3", CATEGORY_DEFINITIONS["D3"]["name"], line.strip()[:120]

    # ── 优先级 6: C1 - 编译器旗标不兼容 ──
    c1_patterns = [
        "unrecognized command-line option",
        "unrecognized command line option",
        "is not supported on",
        "is not supported for",
        "cc1: error:",
        "unsupported option",
        # 架构特定标志
        re.compile(r'-mavx\d*\b', re.IGNORECASE),
        re.compile(r'-msse\d*\b', re.IGNORECASE),
        re.compile(r'-march=\w+', re.IGNORECASE),
        re.compile(r'-mfpu=', re.IGNORECASE),
        re.compile(r'-mfloat-abi=', re.IGNORECASE),
        re.compile(r'-mno-\w+', re.IGNORECASE),
        re.compile(r'-mabm\b', re.IGNORECASE),
        re.compile(r'-mpopcnt\b', re.IGNORECASE),
        re.compile(r'-maes\b', re.IGNORECASE),
        re.compile(r'-mrdrnd\b', re.IGNORECASE),
    ]
    for line in error_lines:
        line_lower = line.lower()
        if "unrecognized command-line option" in line_lower or "unrecognized command line option" in line_lower:
            return "C1", CATEGORY_DEFINITIONS["C1"]["name"], line.strip()[:120]
        if "is not supported on" in line_lower or "is not supported for" in line_lower:
            return "C1", CATEGORY_DEFINITIONS["C1"]["name"], line.strip()[:120]
        if "cc1: error:" in line_lower:
            return "C1", CATEGORY_DEFINITIONS["C1"]["name"], line.strip()[:120]
        # 检查架构特定 flag
        for regex in c1_patterns[4:]:  # skip string patterns, check regex ones
            if isinstance(regex, re.Pattern):
                if regex.search(line):
                    return "C1", CATEGORY_DEFINITIONS["C1"]["name"], line.strip()[:120]

    # 通用 CPU 指令集旗标（-m开头的x86特定指令）
    c1_generic_flags = re.compile(r'(-m(mmx|sse\d*|sse4a|avx\d*|fma|rdrnd|aes|sha|popcnt|abm|no-omit-leaf-frame-pointer|no-))', re.IGNORECASE)
    for line in error_lines:
        if c1_generic_flags.search(line):
            return "C1", CATEGORY_DEFINITIONS["C1"]["name"], line.strip()[:120]

    # ── 链接器错误也归为编译类 ──
    linker_compiled = [
        re.compile(r'/usr/bin/ld:', re.IGNORECASE),
        re.compile(r'collect2: error', re.IGNORECASE),
        re.compile(r'ld returned', re.IGNORECASE),
        re.compile(r'ld: cannot find', re.IGNORECASE),
        re.compile(r'undefined reference', re.IGNORECASE),
        re.compile(r'undefined symbol', re.IGNORECASE),
    ]
    for line in error_lines:
        line_lower = line.lower()
        for regex in linker_compiled:
            if regex.search(line_lower):
                return "C1", CATEGORY_DEFINITIONS["C1"]["name"], line.strip()[:120]

    # ── 优先级 7: C2 - 架构检测失败 ──
    c2_patterns = [
        "unsupported arch", "unsupported architecture",
        "uname -m", "architecture detection",
        "unknown architecture", "unknown arch",
        "not recognized architecture",
        "this architecture is not supported",
        "no support for architecture",
        "architecture not supported",
        "uname result",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in c2_patterns:
            if p in line_lower:
                return "C2", CATEGORY_DEFINITIONS["C2"]["name"], line.strip()[:120]

    # ── 优先级 8: C3 - 缺失架构定义 ──
    c3_patterns = [
        re.compile(r'#error.*arch', re.IGNORECASE),
        re.compile(r'#error.*platform', re.IGNORECASE),
        re.compile(r'#error.*unsupported', re.IGNORECASE),
        re.compile(r'no.*definition.*for.*arch', re.IGNORECASE),
        re.compile(r'unknown\s+platform', re.IGNORECASE),
        re.compile(r'not\s+defined\s+for\s+this\s+arch', re.IGNORECASE),
        re.compile(r'missing.*arch.*definition', re.IGNORECASE),
    ]
    for line in error_lines:
        for regex in c3_patterns:
            if regex.search(line):
                return "C3", CATEGORY_DEFINITIONS["C3"]["name"], line.strip()[:120]

    # ── 优先级 9: C4 - 汇编/内联汇编不兼容 ──
    c4_patterns = [
        "unknown mnemonic", "undefined instruction",
        "impossible constraint", "invalid instruction",
        "unknown register", "bad register name",
        "instruction not supported",
        "no instruction", "operand mismatch",
        "cannot use", "bp cannot be used",
        "inline asm", "assembly error",
        "unknown opcode", "undefined mnemonic",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in c4_patterns:
            if p in line_lower:
                # 排除误匹配
                if "asm" in line_lower and "cannot use" in line_lower:
                    return "C4", CATEGORY_DEFINITIONS["C4"]["name"], line.strip()[:120]
                if p in ["unknown mnemonic", "undefined instruction", "impossible constraint"]:
                    return "C4", CATEGORY_DEFINITIONS["C4"]["name"], line.strip()[:120]

    # 更精确的汇编检查
    asm_compiled = [
        re.compile(r'error:.*unknown\s+mnemonic', re.IGNORECASE),
        re.compile(r'error:.*undefined\s+instruction', re.IGNORECASE),
        re.compile(r'impossible\s+constraint', re.IGNORECASE),
        re.compile(r'error:.*invalid\s+instruction', re.IGNORECASE),
        re.compile(r'error:.*unknown\s+register', re.IGNORECASE),
        re.compile(r'error:.*bad\s+register', re.IGNORECASE),
        re.compile(r'error:.*cannot\s+use', re.IGNORECASE),
        re.compile(r'asm.*error', re.IGNORECASE),
    ]
    for line in error_lines:
        for regex in asm_compiled:
            if regex.search(line):
                return "C4", CATEGORY_DEFINITIONS["C4"]["name"], line.strip()[:120]

    # ── 优先级 10: C5 - 库路径硬编码 ──
    c5_compiled = [
        re.compile(r'/usr/lib/x86_64-linux-gnu', re.IGNORECASE),
        re.compile(r'/lib/x86_64-linux-gnu', re.IGNORECASE),
        re.compile(r'x86_64-linux-gnu/', re.IGNORECASE),  # 末尾带斜杠，匹配路径
        re.compile(r'cannot find /usr/lib/x86_64', re.IGNORECASE),
        re.compile(r'usr/lib/x86_64', re.IGNORECASE),
    ]
    for line in error_lines:
        for regex in c5_compiled:
            if regex.search(line):
                # 排除编译器名（如 x86_64-linux-gnu-g++ 是交叉编译器）
                if 'x86_64-linux-gnu-g++' in line or 'x86_64-linux-gnu-gcc' in line:
                    continue
                return "C5", CATEGORY_DEFINITIONS["C5"]["name"], line.strip()[:120]

    # ── 优先级 9: C6 - 编译器内部错误 ──
    c6_patterns = [
        "internal error: builtin function",
        "internal compiler error",
        "ice:",
        "internal compiler error:",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in c6_patterns:
            if p in line_lower:
                return "C6", CATEGORY_DEFINITIONS["C6"]["name"], line.strip()[:120]

    # ── 优先级 11: P1 - 架构字段缺失 ──
    p1_patterns = [
        "architecture mismatch",
        "architecture: amd64",
        "architecture: i386",
        "dpkg-source: error: architecture",
        "architectures: amd64",
        "no architecture specified",
        "architecture field missing",
        "arch missing",
        re.compile(r'architecture:\s+(amd64|i386)\b', re.IGNORECASE),
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in p1_patterns:
            if isinstance(p, str):
                if p in line_lower:
                    return "P1", CATEGORY_DEFINITIONS["P1"]["name"], line.strip()[:120]
            else:
                if p.search(line):
                    return "P1", CATEGORY_DEFINITIONS["P1"]["name"], line.strip()[:120]

    # ── 优先级 11: T1 - 测试失败 ──
    t1_patterns = [
        "fail:",
        "test failed",
        "tests failed",
        "test:.*error",
        "test:.*fail",
        "make.*test.*failed",
        "make check.*failed",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in t1_patterns:
            if p in line_lower or re.search(p.replace('.*', '.*'), line_lower):
                return "T1", CATEGORY_DEFINITIONS["T1"]["name"], line.strip()[:120]

    # ── 优先级 14: P2 - 打包脚本错误 ──
    p2_precise_patterns = [
        "dh_install:", "dh_install: cannot find",
        "debian/tmp", "debian/*.install",
        "packaging error",
        "no binary artifacts",
        "dpkg-deb: error",
        "dpkg-genbuildinfo",
        "error while packaging",
        "rpmbuild error",
        "error: file not found:",
        "file not found by glob",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in p2_precise_patterns:
            if p in line_lower:
                return "P2", CATEGORY_DEFINITIONS["P2"]["name"], line.strip()[:120]

    # 通用包装器错误（如 dpkg-buildpackage: error）仅在最后兜底
    p2_generic_patterns = [
        "debian/rules",
        "dpkg-buildpackage",
        "subprocess returned exit status",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in p2_generic_patterns:
            if p in line_lower:
                # 如果前面没有任何更具体的匹配，才标记为P2
                return "P2", CATEGORY_DEFINITIONS["P2"]["name"], line.strip()[:120]

    # ── 优先级 13: P3 - 构建宏/工具链选择 ──
    p3_patterns = [
        "%configure", "--host=",
        "cross compile", "cross-compile", "cross_compile",
        "checking for c compiler",
        "checking for c++ compiler",
        "wrong compiler",
        "host compiler", "host gcc",
        "build system type",
        "host system type",
        "target system type",
        "config.guess", "config.sub",
        "cannot run c compiled program",
        "toolchain",
        "configure: error:",
    ]
    for line in error_lines:
        line_lower = line.lower()
        for p in p3_patterns:
            if p in line_lower:
                # %configure 相关
                if p == "%configure":
                    return "P3", CATEGORY_DEFINITIONS["P3"]["name"], line.strip()[:120]
                if p == "--host=":
                    return "P3", CATEGORY_DEFINITIONS["P3"]["name"], line.strip()[:120]
                if "cross" in line_lower and ("compile" in line_lower or "compil" in line_lower):
                    return "P3", CATEGORY_DEFINITIONS["P3"]["name"], line.strip()[:120]
                if "configure: error:" in line_lower:
                    return "P3", CATEGORY_DEFINITIONS["P3"]["name"], line.strip()[:120]
                if "toolchain" in line_lower:
                    return "P3", CATEGORY_DEFINITIONS["P3"]["name"], line.strip()[:120]

    # ── 以上都不匹配 → 未分类 ──
    # 尝试提取一个代表性的错误行
    sig = ""
    for line in error_lines:
        stripped = line.strip()
        if stripped and ("error" in stripped.lower() or "fail" in stripped.lower()):
            sig = stripped[:120]
            break
    if not sig and error_lines:
        sig = error_lines[0].strip()[:120]
    return "0_Unclassified", CATEGORY_DEFINITIONS["0_Unclassified"]["name"], sig


# ─── 文件操作 ────────────────────────────────────────────────────────

def load_error_lines(file_path: str) -> Optional[Dict]:
    """加载 error_lines.json 文件。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (FileNotFoundError, json.JSONDecodeError, IOError) as e:
        return None


def _is_fixable(category: str, error_lines: List[str]) -> bool:
    """
    判断该 case 是否可通过修改 inputs（源代码/spec/dsc 文件）来修复。

    可修复性判断规则（基于构建阶段）：
    - Preinstalls 阶段（前缀 `preinstalls:`）→ 基础设施问题，不可修复
    - Expansion 阶段（无前缀，依赖解析）→ 可尝试修复
    - Compilation/Packaging 阶段 → 可修复
    - 环境资源限制（超时/OOM/磁盘空间）→ 不可修复

    各分类具体规则：
    - E2, E3: 始终不可修复（运行环境资源限制）
    - D1: 检查 `preinstalls:` 前缀；有则不可修复，无则可修复
    - E1: 检查 debootstrap 失败或 dpkg 配置错误；有则不可修复，其他保守处理
    - 其他 (C1-C6, T1, D2, D3, P1-P3): 始终可修复
    """
    # E2/E3: 环境资源限制 → 不可修复
    if category in ("E2", "E3"):
        return False

    combined = "\n".join(error_lines).lower()

    # D1: 区分 preinstalls 阶段 vs expansion 阶段
    if category == "D1":
        # preinstalls 前缀 → 基础设施问题，不可修复
        if "preinstalls:" in combined:
            return False
        # expansion 阶段 → 可尝试通过修改 Build-Depends 修复
        return True

    # E1: 区分 debootstrap 失败 vs 其他环境问题
    if category == "E1":
        # debootstrap chroot 配置失败 → 基础设施问题
        if "failed to setup debootstrap chroot" in combined:
            return False
        # dpkg 配置错误（postinst 脚本失败）→ 基础设施问题
        if "dpkg: error processing" in combined:
            return False
        # 其他 E1（command not found 等）保守处理，默认为不可修复
        # 因为大部分是 postinst 脚本或 chroot 环境问题
        return False

    # 编译类 (C1-C6, T1)、依赖版本/架构 (D2, D3)、打包类 (P1-P3) → 均可修复
    return True


def build_case_entry(data: Dict, category: str, category_name: str, error_signature: str, error_lines: List[str] = None) -> Dict:
    """从 error_lines.json 数据构建 case_index.json 条目。"""
    case_id = data.get("case_id", "unknown")
    parsed = _parse_case_id(case_id)

    # 计算可修复性
    if error_lines is None:
        error_lines = []
    fixable = _is_fixable(category, error_lines)

    return {
        "case_id": case_id,
        "package_name": data.get("package_name", parsed.get("package_name", "unknown")),
        "direction": data.get("direction", parsed.get("direction", "unknown")),
        "release": data.get("release", parsed.get("release", "unknown")),
        "source_arch": data.get("source_arch", parsed.get("source_arch", "unknown")),
        "target_arch": data.get("target_arch", parsed.get("target_arch", "unknown")),
        "error_signature": error_signature,
        "fixable": fixable,
        "confidence": "high" if category != "0_Unclassified" else "low",
    }


def _parse_case_id(case_id: str) -> Dict:
    """从 case_id 中提取 package_name、source_arch、target_arch、release。"""
    parts = case_id.split("-")
    if len(parts) < 6:
        return {"package_name": case_id, "source_arch": "unknown", "target_arch": "unknown", "release": "unknown", "direction": "unknown"}
    release = parts[1]
    src_arch = parts[2]
    tgt_arch = parts[3]
    pkg = "-".join(parts[4:-1])
    return {
        "package_name": pkg,
        "source_arch": src_arch,
        "target_arch": tgt_arch,
        "release": release,
        "direction": f"{src_arch}_to_{tgt_arch}",
    }


# ─── 输出 ────────────────────────────────────────────────────────────

def write_classification(output_dir: str, category: str, case_entry: Dict):
    """将 case 写入对应分类目录的 case_index.json。"""
    cat_dir_name = CATEGORY_DIR_NAMES.get(category, "0_Unclassified")
    group = CATEGORY_DEFINITIONS.get(category, CATEGORY_DEFINITIONS["0_Unclassified"])["group"]
    group_dir_name = GROUP_DIR_NAMES.get(group, "0_Unclassified")

    case_dir = os.path.join(output_dir, group_dir_name, cat_dir_name)
    os.makedirs(case_dir, exist_ok=True)

    index_path = os.path.join(case_dir, "case_index.json")

    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            index_data = json.load(f)
    else:
        cat_def = CATEGORY_DEFINITIONS.get(category, CATEGORY_DEFINITIONS["0_Unclassified"])
        index_data = {
            "category": category,
            "category_name": cat_def["name"],
            "cases": [],
            "total": 0,
        }

    # 检查是否已存在相同 case_id，已存在则更新
    existing_ids = {c["case_id"]: i for i, c in enumerate(index_data["cases"])}
    if case_entry["case_id"] in existing_ids:
        # 更新已有条目（保留旧字段的同时添加新字段）
        idx = existing_ids[case_entry["case_id"]]
        index_data["cases"][idx].update(case_entry)
    else:
        index_data["cases"].append(case_entry)
        index_data["total"] = len(index_data["cases"])

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, indent=2, ensure_ascii=False)


def write_report(output_dir: str, stats: Dict):
    """生成 _classification_report.json。"""
    total = stats["total"]
    classified = total - stats["unclassified"]

    distribution = {}
    for code, count in stats["category_counts"].items():
        if total > 0:
            pct = round(count / total * 100, 1)
        else:
            pct = 0.0
        distribution[code] = {"count": count, "percentage": pct}

    # 构建层次结构
    category_hierarchy = OrderedDict()
    for group_key, group_name in [
        ("1_Dependency", "依赖类"),
        ("2_Compilation", "编译类"),
        ("3_Packaging", "打包类"),
        ("4_Environment", "环境/配置类"),
        ("0_Unclassified", "未分类"),
    ]:
        group_cats = [c for c, d in CATEGORY_DEFINITIONS.items() if d["group"] == group_key]
        group_total = sum(stats["category_counts"].get(c, 0) for c in group_cats)
        group_pct = round(group_total / total * 100, 1) if total > 0 else 0.0

        subcategories = {}
        for c in group_cats:
            dir_name = CATEGORY_DIR_NAMES.get(c, c)
            subcategories[f"{dir_name}"] = stats["category_counts"].get(c, 0)

        category_hierarchy[group_key] = {
            "total": group_total,
            "percentage": group_pct,
            "subcategories": subcategories,
        }

    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "total_cases": total,
        "classified": classified,
        "unclassified": stats["unclassified"],
        "fixability_summary": {
            "fixable": stats["fixable_counts"]["fixable"],
            "not_fixable": stats["fixable_counts"]["not_fixable"],
            "fixable_percentage": round(stats["fixable_counts"]["fixable"] / total * 100, 1) if total > 0 else 0.0,
            "not_fixable_percentage": round(stats["fixable_counts"]["not_fixable"] / total * 100, 1) if total > 0 else 0.0,
            "by_category": stats["fixable_by_category"],
        },
        "distribution": distribution,
        "category_hierarchy": category_hierarchy,
    }

    report_path = os.path.join(output_dir, "_classification_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return report


# ─── 主流程 ──────────────────────────────────────────────────────────

def print_verbose(case_id: str, category: str, category_name: str, error_signature: str, error_lines: List[str]):
    """--verbose 模式下输出详细分类信息。"""
    print(f"\n{'='*60}")
    print(f"  Case: {case_id}")
    print(f"  {'='*60}")
    print(f"  分类: {category} - {category_name}")
    print(f"  错误签名: {error_signature[:100]}")
    print(f"  Error lines ({len(error_lines)}):")
    for i, line in enumerate(error_lines[:5]):
        print(f"    [{i+1}] {line.strip()[:150]}")
    if len(error_lines) > 5:
        print(f"    ... and {len(error_lines) - 5} more lines")


def main():
    parser = argparse.ArgumentParser(
        description="基于规则的构建失败 case 自动分类工具"
    )
    parser.add_argument(
        "--input-dir",
        default="/home/zhaochenyu/buildbench_competition/temp-guidance/failed_cases",
        help="error_lines.json 所在目录 (default: temp-guidance/failed_cases)",
    )
    parser.add_argument(
        "--output-dir",
        default="/home/zhaochenyu/buildbench_competition/temp-guidance/classified_failures",
        help="分类结果输出目录 (default: temp-guidance/classified_failures)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅分析不写文件，输出统计信息",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出每个 case 的详细分类判断",
    )
    args = parser.parse_args()

    input_dir = args.input_dir
    output_dir = args.output_dir

    # 检查输入目录
    if not os.path.isdir(input_dir):
        print(f"错误: 输入目录不存在: {input_dir}", file=sys.stderr)
        sys.exit(1)

    # 收集所有 error_lines.json 文件
    case_dirs = sorted([
        d for d in os.listdir(input_dir)
        if os.path.isdir(os.path.join(input_dir, d))
    ])

    if not case_dirs:
        print(f"警告: 输入目录中未找到 case 子目录: {input_dir}", file=sys.stderr)
        sys.exit(0)

    print(f"找到 {len(case_dirs)} 个 case 目录")
    print(f"输入目录: {input_dir}")

    if not args.dry_run:
        print(f"输出目录: {output_dir}")
        os.makedirs(output_dir, exist_ok=True)

    # 统计信息
    stats = {
        "total": 0,
        "unclassified": 0,
        "category_counts": {code: 0 for code in CATEGORY_DEFINITIONS},
        "fixable_counts": {"fixable": 0, "not_fixable": 0},
        "fixable_by_category": {},
        "errors": [],
    }

    # 初始化每个分类的 fixable 统计
    for code in CATEGORY_DEFINITIONS:
        stats["fixable_by_category"][code] = {"fixable": 0, "not_fixable": 0}

    # 处理每个 case
    total_cases = len(case_dirs)
    processed = 0

    for i, case_dir in enumerate(case_dirs):
        # 进度显示
        processed += 1
        pct = processed / total_cases * 100
        bar_len = 40
        filled = int(bar_len * processed / total_cases)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  进度: |{bar}| {processed}/{total_cases} ({pct:.1f}%)", end="", flush=True)

        error_lines_path = os.path.join(input_dir, case_dir, "error_lines.json")

        if not os.path.exists(error_lines_path):
            # 尝试其他可能的文件名
            alt_path = os.path.join(input_dir, case_dir, "1_error_lines.json")
            if os.path.exists(alt_path):
                error_lines_path = alt_path
            else:
                stats["errors"].append(f"{case_dir}: error_lines.json not found")
                continue

        data = load_error_lines(error_lines_path)
        if data is None:
            stats["errors"].append(f"{case_dir}: failed to load error_lines.json")
            continue

        # 获取 error_lines — 支持多种数据格式
        error_lines_raw = data.get("error_lines", [])
        error_lines = []

        if isinstance(error_lines_raw, dict):
            # 格式: {"compilation": [...], "dependency": [...], ...}
            for sub_list in error_lines_raw.values():
                if isinstance(sub_list, list):
                    error_lines.extend(sub_list)
        elif isinstance(error_lines_raw, list):
            error_lines = error_lines_raw
        elif isinstance(error_lines_raw, str):
            error_lines = [error_lines_raw]

        # 支持其他可能的字段名
        if not error_lines:
            for alt_key in ("error_lines_formatted", "errors", "extracted_lines", "log_tail"):
                alt_val = data.get(alt_key, "")
                if isinstance(alt_val, list):
                    error_lines = alt_val
                    break
                elif isinstance(alt_val, str) and alt_val.strip():
                    error_lines = [alt_val[:500]]
                    break

        if not error_lines:
            # 没有错误行，标记为未分类
            error_lines = ["No error lines available"]
            category = "0_Unclassified"
            category_name = CATEGORY_DEFINITIONS["0_Unclassified"]["name"]
            error_signature = "No error lines available"
        else:
            # 执行分类
            category, category_name, error_signature = classify_error_lines(error_lines)

        if category == "0_Unclassified":
            stats["unclassified"] += 1
        stats["total"] += 1
        stats["category_counts"][category] = stats["category_counts"].get(category, 0) + 1

        # 构建 case 条目（传入 error_lines 用于计算可修复性）
        case_entry = build_case_entry(data, category, category_name, error_signature, error_lines)

        # 记录可修复性统计
        fixable = case_entry.get("fixable", True)
        if fixable:
            stats["fixable_counts"]["fixable"] += 1
            stats["fixable_by_category"][category]["fixable"] += 1
        else:
            stats["fixable_counts"]["not_fixable"] += 1
            stats["fixable_by_category"][category]["not_fixable"] += 1

        # verbose 输出
        if args.verbose:
            print_verbose(
                data.get("case_id", case_dir),
                category,
                category_name,
                error_signature,
                error_lines,
            )

        # 写入分类结果
        if not args.dry_run:
            write_classification(output_dir, category, case_entry)

    print()  # 换行（进度条后）

    # ── 生成报告 ──
    print(f"\n{'='*50}")
    print(f"分类完成！")
    print(f"{'='*50}")
    print(f"  总 case:        {stats['total']}")
    print(f"  已分类:          {stats['total'] - stats['unclassified']}")
    print(f"  未分类:          {stats['unclassified']}")
    if stats["errors"]:
        print(f"  处理错误:        {len(stats['errors'])}")
        if args.verbose:
            for err in stats["errors"][:10]:
                print(f"    - {err}")

    print(f"\n  分类分布:")
    # 按优先级排序输出
    sorted_cats = sorted(
        CATEGORY_DEFINITIONS.items(),
        key=lambda x: x[1]["priority"],
    )
    for code, defn in sorted_cats:
        count = stats["category_counts"].get(code, 0)
        if count > 0:
            pct = count / stats["total"] * 100 if stats["total"] > 0 else 0
            print(f"    {code:20s} ({defn['name']:12s}): {count:4d} ({pct:5.1f}%)")

    # 按大类汇总
    print(f"\n  大类汇总:")
    for group_key in ["1_Dependency", "2_Compilation", "3_Packaging", "4_Environment", "0_Unclassified"]:
        group_cats = [c for c, d in CATEGORY_DEFINITIONS.items() if d["group"] == group_key]
        group_total = sum(stats["category_counts"].get(c, 0) for c in group_cats)
        if group_total > 0:
            group_name = CATEGORY_DEFINITIONS[group_cats[0]]["group_name"] if group_cats else group_key
            pct = group_total / stats["total"] * 100 if stats["total"] > 0 else 0
            print(f"    {group_key:20s} ({group_name}): {group_total:4d} ({pct:5.1f}%)")

    if not args.dry_run:
        # 写报告
        report = write_report(output_dir, stats)
        print(f"\n  分类报告: {os.path.join(output_dir, '_classification_report.json')}")
        print(f"  输出目录: {output_dir}")

    if args.dry_run:
        print(f"\n  [DRY RUN] 未写入任何文件。")


if __name__ == "__main__":
    main()