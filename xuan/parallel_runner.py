"""parallel_runner.py — 并行执行多个 case 的修复流程

与串行模式完全对齐：扫描 info.yaml 中 base_dir 下所有软件包，自动完成全部 case。
每个 case 在独立线程中运行，共享 result_log_res / result_text_res / temp_workspace 等标准目录。

用法：
    # Full 模式（默认），自动扫描全部 case，最多 2 并发
    python parallel_runner.py --mode full --attempts 1 --max-workers 2

    # Patch 模式 + 资源监控
    python parallel_runner.py --mode patch --attempts 1 --max-workers 2 \
        --resource-csv ~/buildbench_competition/temp-guidance/experiments/parallel_resource.csv

    # 指定软件包（而非扫描全部）
    python parallel_runner.py --mode full --packages pybdsf,libsv
"""

import os
import sys
import time
import json
import argparse
import traceback
import concurrent.futures
from typing import Dict, List, Optional

import yaml

# ── 导入资源监控模块（与并行逻辑分离） ──
from resource_monitor import ResourceMonitor

# ── 加载配置 ──
with open("config/info.yaml", "r") as f:
    _INFO = yaml.safe_load(f)

_BASE_DIR = _INFO["paths"]["base_dir"]


def _get_packages(base_dir: str = None) -> List[str]:
    """扫描 base_dir 下所有软件包子目录"""
    base = base_dir or _BASE_DIR
    if not os.path.isdir(base):
        print(f"[ERROR] base_dir 不存在: {base}")
        return []
    packages = sorted([
        d for d in os.listdir(base)
        if os.path.isdir(os.path.join(base, d))
        and not d.startswith(".")
    ])
    return packages


def _create_llm_config():
    """创建 LLMConfig，与 baseline.py / baseline_patch.py 的 main() 逻辑一致"""
    from baseline import LLMConfig

    provider = _INFO["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": os.getenv("LLM_MODEL", "qwen3-max"),
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)
    return LLMConfig(provider=provider, model=default_model)


def run_single_case(package_name: str, mode: str, base_dir: str,
                    max_attempts: int, prebuild: bool = True) -> str:
    """在独立线程中运行一个 case，返回包名。

    每个线程创建独立的 AutoRepairBaseline 实例，使用 info.yaml 默认路径。
    不同 case 使用不同的 package_name，在 temp_workspace/、result_log_res/、
    result_text_res/ 中自动隔离。
    """
    from baseline_patch import AutoRepairBaselinePatch, MODE_PROMPT_MAP

    llm_cfg = _create_llm_config()

    if mode == "full":
        from baseline import AutoRepairBaseline as RepairClass
        prompt_file = "prompts/full_file_generation_json.txt"
        repair = RepairClass(
            llm=llm_cfg,
            base_dir=base_dir,
            max_build_attempts=max_attempts,
            prebuild=prebuild,
            mode="full_file",
        )
    elif mode == "spec":
        from baseline import AutoRepairBaseline as RepairClass
        prompt_file = MODE_PROMPT_MAP.get(mode, "prompts/spec_generation_json.txt")
        repair = RepairClass(
            llm=llm_cfg,
            base_dir=base_dir,
            max_build_attempts=max_attempts,
            prebuild=prebuild,
            mode="spec",
        )
    else:
        # patch / spec_patch 共用 AutoRepairBaselinePatch，仅 mode 不同
        RepairClass = AutoRepairBaselinePatch
        prompt_file = MODE_PROMPT_MAP.get(mode, "prompts/patch_generation_json.txt")
        repair = RepairClass(
            llm=llm_cfg,
            base_dir=base_dir,
            max_build_attempts=max_attempts,
            prebuild=prebuild,
            mode=mode,
        )

    with open(prompt_file, "r", encoding="utf-8") as f:
        system_prompt_tpl = f.read()

    repair.process_one_package(package_name, system_prompt_tpl)
    return package_name


def main():
    parser = argparse.ArgumentParser(
        description="并行执行多个 case 的修复流程（对齐串行逻辑）"
    )
    parser.add_argument(
        "--mode", type=str, default="full", choices=["full", "patch", "spec", "spec_patch"],
        help="修复模式: full（完整文件替换）/ patch（unified diff）/ spec（spec-only 策略引导）/ spec_patch（spec 增强 LLM），默认 full",
    )
    parser.add_argument(
        "--attempts", type=int, default=1,
        help="每个 case 的最大构建尝试次数，默认 1",
    )
    parser.add_argument(
        "--max-workers", type=int, default=2,
        help="最大并行 worker 数，默认 2",
    )
    parser.add_argument(
        "--packages", type=str, default=None,
        help="逗号分隔的包名列表（不指定则自动扫描 base_dir 下所有 case）",
    )
    parser.add_argument(
        "--no-prebuild", action="store_true",
        help="跳过预构建（attempt 前不先构建原始 case 获取初始失败日志）",
    )
    parser.add_argument(
        "--resource-csv", type=str, default=None,
        help="资源监控 CSV 输出路径（不指定则不开启监控）",
    )
    parser.add_argument(
        "--base-dir", type=str, default=None,
        help=f"覆盖 base_dir（默认: {_BASE_DIR}）",
    )
    args = parser.parse_args()

    base_dir = os.path.abspath(args.base_dir or _BASE_DIR)

    # 获取待处理包列表
    if args.packages:
        packages = [p.strip() for p in args.packages.split(",") if p.strip()]
    else:
        packages = _get_packages(base_dir)

    if not packages:
        print("没有可处理的 case，退出。")
        sys.exit(1)

    print("=" * 60)
    print(f"并行修复模式: {args.mode}")
    print(f"数据目录: {base_dir}")
    print(f"包列表 ({len(packages)}): {', '.join(packages)}")
    print(f"最大尝试次数/包: {args.attempts}")
    print(f"最大并发 worker: {args.max_workers}")
    if args.resource_csv:
        print(f"资源监控: {args.resource_csv}")
    print("=" * 60)
    print()

    # ── 启动资源监控（可选） ──
    monitor = None
    if args.resource_csv:
        monitor = ResourceMonitor(args.resource_csv, interval=5.0)
        monitor.start()
        print(f"[监控] 资源监控已启动 → {args.resource_csv}\n")

    # ── 队列式并发执行 ──
    # 固定 worker 池大小，case 自动排队，跑完一个立即拉取下一个
    start_ts = time.time()
    results: Dict[str, str] = {}
    errors: Dict[str, str] = {}

    prebuild = not args.no_prebuild
    n_workers = min(args.max_workers, len(packages))
    total = len(packages)
    done_count = 0

    print(f"\n▶ 启动 {n_workers} 个 worker，队列共 {total} 个 case\n")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
        # 一次性提交所有任务，executor 自动排队
        future_map = {
            executor.submit(
                run_single_case, pkg, args.mode, base_dir, args.attempts, prebuild
            ): pkg
            for pkg in packages
        }

        # 每完成一个，立即打印进度并启动下一个
        for future in concurrent.futures.as_completed(future_map):
            pkg_name = future_map[future]
            done_count += 1
            elapsed = time.time() - start_ts
            try:
                future.result()
                results[pkg_name] = "DONE"
                status = "✅"
            except Exception as e:
                errors[pkg_name] = f"{e}\n{traceback.format_exc()}"
                status = "❌"
            print(f"  [{done_count:3d}/{total}] {status} {pkg_name}  "
                  f"(运行 {elapsed:.0f}s)")

    total_time = time.time() - start_ts

    # ── 停止监控 ──
    if monitor:
        monitor.stop()

    # ── 汇总 ──
    print(f"\n{'=' * 60}")
    print(f"队列模式测试完成")
    print(f"  Workers: {n_workers}  |  Cases: {total}  |  总耗时: {total_time:.1f}s ({total_time / 60:.1f} 分钟)")
    print(f"  成功: {len(results)}  |  失败: {len(errors)}")
    if errors:
        print(f"  失败的包: {', '.join(errors.keys())}")
        for pkg, err in errors.items():
            print(f"    [{pkg}]: {err[:200]}")
    # 效率评估
    if total > n_workers:
        ideal_time = total_time / n_workers  # 每个 worker 平均处理时间
        print(f"  效率: 平均每个 worker 忙碌 {total_time:.0f}s, 实际处理 {total} 个 case")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
