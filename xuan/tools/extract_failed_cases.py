#!/usr/bin/env python3
"""
提取 verified-first 中所有失败 case 的原始平台 target 构建日志及相关数据。

数据来源：
  - log-cache/original-platform/ → 原始平台 target 构建日志（gzip）
  - docker/batch-inputs/verified-first/<case_id>/ → manifest.json, input/, config/, dependencies/

输出目录：
  temp-guidance/failed_cases/<case_id>/  → 每个 case 的提取数据

用法：
  cd /home/zhaochenyu/buildbench_competition
  python Build-bench-xuan/tools/extract_failed_cases.py
"""

import json
import os
import gzip
import shutil
import csv
from collections import Counter

# ─── 路径配置 ───────────────────────────────────────────────────────
BASE_DIR = "/home/zhaochenyu/buildbench_competition/shared/case-store/huang-zihao-tri-isa-1687"
LOG_CACHE = os.path.join(BASE_DIR, "log-cache", "original-platform")
BATCH_INPUTS = os.path.join(BASE_DIR, "docker", "batch-inputs", "verified-first")
BATCH_RESULTS = os.path.join(BASE_DIR, "docker", "batch-results")
CSV_PATH = "/home/zhaochenyu/buildbench_competition/temp-guidance/confirmed-case-build-logs.csv"
OUTPUT_DIR = "/home/zhaochenyu/buildbench_competition/temp-guidance/failed_cases"

# ─── 辅助函数 ───────────────────────────────────────────────────────

def parse_case_id(case_id):
    """从 case_id 中提取 package_name、source_arch、target_arch、release"""
    # 格式: launchpad-<release>-<src_arch>-<tgt_arch>-<package_name>-<hash>
    parts = case_id.split('-')
    if len(parts) < 6:
        return {"package_name": case_id, "source_arch": "unknown", "target_arch": "unknown", "release": "unknown"}
    release = parts[1]
    src_arch = parts[2]
    tgt_arch = parts[3]
    # package_name 是中间部分，包含可能的连字符（如 kodi-game-libretro）
    pkg = '-'.join(parts[4:-1])
    return {
        "package_name": pkg,
        "source_arch": src_arch,
        "target_arch": tgt_arch,
        "release": release,
        "direction": f"{src_arch}_to_{tgt_arch}"
    }


def get_log_tail(log_path, num_lines=200):
    """解压 gzip 日志并返回尾部 N 行"""
    try:
        with gzip.open(log_path, 'rt', errors='replace') as f:
            lines = f.readlines()
        tail = lines[-num_lines:] if len(lines) > num_lines else lines
        return ''.join(tail), len(lines)
    except Exception as e:
        return f"Error reading log: {e}", 0


def get_file_listing(dir_path):
    """返回目录的文件清单字符串"""
    if not os.path.isdir(dir_path):
        return "Directory does not exist"
    files = sorted(os.listdir(dir_path))
    if not files:
        return "Directory is empty"
    lines = []
    for f in files:
        path = os.path.join(dir_path, f)
        size = os.path.getsize(path)
        is_dir = os.path.isdir(path)
        lines.append(f"{'[DIR]' if is_dir else '[FILE]'} {f}  ({size / 1024:.1f} KB)")
    return '\n'.join(lines)


# ─── 主流程 ─────────────────────────────────────────────────────────

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"输出目录: {OUTPUT_DIR}")

    # ── Step 1: 扫描 log-cache，建立 case_id → target 日志映射 ──
    print("\n=== Step 1: 扫描原始平台日志 ===")
    target_map = {}
    for root, dirs, files in os.walk(LOG_CACHE):
        for f in files:
            if f == 'log-metadata.json':
                meta_path = os.path.join(root, f)
                with open(meta_path) as fh:
                    meta = json.load(fh)
                if meta.get('kind') == 'target':
                    case_id = meta.get('case_id', '')
                    if case_id:
                        log_filename = meta.get('filename', '')
                        log_path = os.path.join(root, log_filename)
                        if os.path.exists(log_path):
                            target_map[case_id] = {
                                'log_path': log_path,
                                'log_bytes': meta.get('bytes', 0),
                                'log_dir': root,
                                'meta': meta
                            }

    print(f"  找到 target 日志: {len(target_map)} 个")

    # ── Step 2: 读取 CSV 确认列表（如果有） ──
    csv_confirmed = set()
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for r in reader:
                csv_confirmed.add(r['case_id'])
        print(f"  CSV 确认 case: {len(csv_confirmed)} 个")

    # ── Step 3: 遍历 verified-first 中的 case ──
    print("\n=== Step 2: 提取失败 case 数据 ===")
    vf_dirs = sorted([d for d in os.listdir(BATCH_INPUTS)
                      if os.path.isdir(os.path.join(BATCH_INPUTS, d))])

    extracted = 0
    skipped_no_target = 0
    skipped_no_input = 0
    errors = []
    stats = {
        'total_vf': len(vf_dirs),
        'extracted': 0,
        'skipped_no_target_log': 0,
        'skipped_no_input_dir': 0,
        'has_docker_log': 0,
        'has_config': 0,
        'has_dependencies': 0,
        'direction_dist': Counter(),
        'log_size_dist': Counter(),
    }

    for case_id in vf_dirs:
        # 只处理有 target 日志的 case
        if case_id not in target_map:
            skipped_no_target += 1
            continue

        entry = target_map[case_id]
        parsed = parse_case_id(case_id)
        package_name = parsed['package_name']
        direction = parsed['direction']

        # 创建输出目录
        case_out_dir = os.path.join(OUTPUT_DIR, case_id)
        os.makedirs(case_out_dir, exist_ok=True)

        input_dir = os.path.join(BATCH_INPUTS, case_id, 'target', 'input')
        config_dir = os.path.join(BATCH_INPUTS, case_id, 'target', 'config')
        deps_dir = os.path.join(BATCH_INPUTS, case_id, 'target', 'dependencies')
        manifest_path = os.path.join(BATCH_INPUTS, case_id, 'target', 'manifest.draft.json')

        # ── 文件 1: case_info.json ──
        has_config = os.path.isdir(config_dir) and len(os.listdir(config_dir)) > 0
        has_deps = os.path.isdir(deps_dir) and len(os.listdir(deps_dir)) > 0
        log_size_kb = entry['log_bytes'] / 1024

        case_info = {
            'case_id': case_id,
            'package_name': package_name,
            'source_arch': parsed['source_arch'],
            'target_arch': parsed['target_arch'],
            'direction': direction,
            'release': parsed['release'],
            'log_size_kb': round(log_size_kb, 1),
            'has_config': has_config,
            'has_dependencies': has_deps,
            'has_docker_log': case_id in csv_confirmed,
            'log_source': 'original-platform',
            'log_platform': entry['meta'].get('url', ''),
        }
        with open(os.path.join(case_out_dir, '1_case_info.json'), 'w') as f:
            json.dump(case_info, f, indent=2)

        # ── 文件 2: original_target.log.gz (复制原始 gzip) ──
        shutil.copy2(entry['log_path'], os.path.join(case_out_dir, '2_original_target.log.gz'))

        # ── 文件 3: original_target.log.tail (解压后尾部 200 行) ──
        tail_content, total_lines = get_log_tail(entry['log_path'], 200)
        with open(os.path.join(case_out_dir, '3_original_target.log.tail'), 'w') as f:
            f.write(f"# Total lines: {total_lines}\n")
            f.write(f"# Tail: last 200 lines\n")
            f.write("#" + "=" * 70 + "\n")
            f.write(tail_content)

        # ── 文件 4: manifest.json ──
        if os.path.exists(manifest_path):
            shutil.copy2(manifest_path, os.path.join(case_out_dir, '4_manifest.json'))
        else:
            # 尝试 source 目录
            src_manifest = os.path.join(BATCH_INPUTS, case_id, 'source', 'manifest.draft.json')
            if os.path.exists(src_manifest):
                shutil.copy2(src_manifest, os.path.join(case_out_dir, '4_manifest.json'))
            else:
                with open(os.path.join(case_out_dir, '4_manifest.json'), 'w') as f:
                    json.dump({"error": "manifest.draft.json not found"}, f)

        # ── 文件 5: input_files.txt ──
        listing = get_file_listing(input_dir)
        with open(os.path.join(case_out_dir, '5_input_files.txt'), 'w') as f:
            f.write(f"# Input directory: {input_dir}\n\n")
            f.write(listing + '\n')

        # ── 文件 6: config_dir.txt ──
        listing = get_file_listing(config_dir)
        with open(os.path.join(case_out_dir, '6_config_dir.txt'), 'w') as f:
            f.write(f"# Config directory: {config_dir}\n\n")
            f.write(listing + '\n')

        # ── 文件 7: dependencies.txt ──
        listing = get_file_listing(deps_dir)
        with open(os.path.join(case_out_dir, '7_dependencies.txt'), 'w') as f:
            f.write(f"# Dependencies directory: {deps_dir}\n\n")
            f.write(listing + '\n')

        # ── 文件 8: docker_build.log (可选，仅 CSV 确认的 case) ──
        if case_id in csv_confirmed:
            # 从 CSV 读取 docker_build_log 路径
            with open(CSV_PATH, encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for r in reader:
                    if r['case_id'] == case_id:
                        docker_log = r['docker_build_log_absolute']
                        if os.path.exists(docker_log):
                            shutil.copy2(docker_log, os.path.join(case_out_dir, '8_docker_build.log'))
                            stats['has_docker_log'] += 1
                        break

        # 更新统计
        extracted += 1
        stats['extracted'] += 1
        stats['direction_dist'][direction] += 1
        if has_config:
            stats['has_config'] += 1
        if has_deps:
            stats['has_dependencies'] += 1

        # 按大小区间统计
        if log_size_kb < 10:
            stats['log_size_dist']['<10KB'] += 1
        elif log_size_kb < 50:
            stats['log_size_dist']['10-50KB'] += 1
        elif log_size_kb < 100:
            stats['log_size_dist']['50-100KB'] += 1
        elif log_size_kb < 500:
            stats['log_size_dist']['100-500KB'] += 1
        else:
            stats['log_size_dist']['>500KB'] += 1

        if extracted % 100 == 0:
            print(f"  已提取 {extracted}/1202...")

    # ── Step 4: 生成汇总统计 ──
    print("\n=== Step 3: 生成汇总统计 ===")
    summary = {
        'total_verified_first': stats['total_vf'],
        'extracted': stats['extracted'],
        'skipped_no_target_log': stats['skipped_no_target_log'],
        'skipped_no_input_dir': stats['skipped_no_input_dir'],
        'has_docker_log': stats['has_docker_log'],
        'direction_distribution': dict(stats['direction_dist'].most_common()),
        'log_size_distribution': dict(stats['log_size_dist']),
        'config_available': stats['has_config'],
        'dependencies_available': stats['has_dependencies'],
        'output_dir': OUTPUT_DIR,
    }

    summary_path = os.path.join(OUTPUT_DIR, '_summary.json')
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)

    # ── 打印结果 ──
    print(f"\n{'='*50}")
    print(f"提取完成！")
    print(f"{'='*50}")
    print(f"  verified-first 总 case:    {stats['total_vf']}")
    print(f"  已提取 (有 target 日志):   {extracted}")
    print(f"  跳过 (无 target 日志):     {skipped_no_target}")
    print(f"  有 Docker 日志 (补充):     {stats['has_docker_log']}")
    print(f"\n  方向分布:")
    for direction, count in stats['direction_dist'].most_common():
        print(f"    {direction}: {count}")
    print(f"\n  日志大小分布:")
    for size_range, count in sorted(stats['log_size_dist'].items()):
        print(f"    {size_range}: {count}")
    print(f"\n  有 config 目录:     {stats['has_config']}/{extracted}")
    print(f"  有 dependencies 目录: {stats['has_dependencies']}/{extracted}")
    print(f"\n  输出目录: {OUTPUT_DIR}")
    print(f"  汇总文件: {summary_path}")


if __name__ == '__main__':
    main()