"""从 verified-first 数据集复制 case 到 data/case_study/

每个 case 有 source（原始架构）和 target（交叉编译目标），我们复制 target（需要修复的）。
按 case-info.json 中的 direction 字段组织目录结构。

支持增量追加：已存在的 case 跳过，只添加新 case。
支持回滚：备份在 temp-guidance/backups/ 下。
"""

import json
import shutil
import os
import sys
import time
from pathlib import Path
from collections import Counter

# ========== 路径配置 ==========
VERIFIED_FIRST_DIR = Path(
    "/home/zhaochenyu/buildbench_competition/shared/case-store/"
    "huang-zihao-tri-isa-1687/docker/batch-inputs/verified-first"
)
BATCH_RESULTS_DIR = Path(
    "/home/zhaochenyu/buildbench_competition/shared/case-store/"
    "huang-zihao-tri-isa-1687/docker/batch-results"
)
CASE_STUDY_DIR = Path("/home/zhaochenyu/buildbench_competition/Build-bench-xuan/data/case_study")
BACKUP_DIR = Path(
    "/home/zhaochenyu/buildbench_competition/temp-guidance/backups"
)

# ========== 工具函数 ==========


def get_case_info(case_dir: Path, role: str = "source") -> dict:
    """读取 case 的 case-info.json"""
    info_path = case_dir / role / "evidence" / "case-info.json"
    if not info_path.is_file():
        return {}
    try:
        return json.loads(info_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def find_build_logs(package_name: str, role: str) -> list:
    """在 batch-results 中查找 case 的构建日志

    batch-results 目录命名: <package_name>-<role>-plan-exact-build-<N>
    返回 build.log 文件路径列表，按 N 降序（最新优先）
    """
    if not BATCH_RESULTS_DIR.is_dir():
        return []
    prefix = f"{package_name}-{role}-plan-exact-build-"
    matches = []
    for d in BATCH_RESULTS_DIR.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            build_log = d / "build.log"
            if build_log.is_file():
                matches.append((int(d.name.split("-")[-1]), build_log))
    # 按 N 降序排列
    matches.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in matches]


def copy_case(case_dir: Path, case_info: dict) -> tuple:
    """复制单个 case 到 case_study

    Returns:
        (status: str, direction: str, package_name: str)
        status: "copied" | "skipped" | "error"
    """
    direction = case_info.get("direction", "")
    package_name = case_info.get("package_name", "")
    if not direction or not package_name:
        return ("error", direction, package_name)

    dest_dir = CASE_STUDY_DIR / direction / package_name
    src_dir = case_dir / "target"

    if not src_dir.is_dir():
        return ("error", direction, package_name)

    if dest_dir.exists():
        return ("skipped", direction, package_name)

    try:
        # 复制 target 目录到目标位置
        shutil.copytree(src_dir, dest_dir, symlinks=True)

        # 重命名 manifest.draft.json → manifest.json（如果存在）
        draft = dest_dir / "manifest.draft.json"
        manifest = dest_dir / "manifest.json"
        if draft.is_file() and not manifest.exists():
            draft.rename(manifest)

        # 清理 manifest 中的 draft_status 字段（docker-validator 不识别此字段）
        if manifest.is_file():
            try:
                mdata = json.loads(manifest.read_text(encoding="utf-8"))
                if "draft_status" in mdata:
                    del mdata["draft_status"]
                    manifest.write_text(json.dumps(mdata, indent=4), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass

        # 尝试复制构建日志
        target_logs = find_build_logs(package_name, "target")
        source_logs = find_build_logs(package_name, "source")
        logs_dir = dest_dir / "logs"
        logs_dir.mkdir(exist_ok=True)

        # 复制 target 构建日志
        if target_logs:
            # 取最新的（N 最大）
            shutil.copy2(target_logs[0], logs_dir / "docker-target-build.log")
        if source_logs:
            shutil.copy2(source_logs[0], logs_dir / "docker-source-build.log")

        return ("copied", direction, package_name)
    except Exception as e:
        # 复制失败时清理不完整的目录
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        print(f"  [ERROR] 复制失败: {case_dir.name} → {e}", file=sys.stderr)
        return ("error", direction, package_name)


def backup_case_study():
    """备份现有 case_study 到 temp-guidance/backups/

    为避免备份 40GB 数据过慢，使用已有的备份作为回滚点。
    如果已有备份，则直接返回最早的备份路径。
    """
    if not CASE_STUDY_DIR.is_dir():
        print("  case_study 目录不存在，跳过备份")
        return None

    # 检查是否有已有的备份
    existing_backups = sorted(BACKUP_DIR.glob("case_study_backup_*")) if BACKUP_DIR.is_dir() else []
    if existing_backups:
        backup_path = existing_backups[0]
        print(f"  使用已有备份: {backup_path}")
        return backup_path

    # 没有已有备份时才创建新备份
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"case_study_backup_{timestamp}"

    if not BACKUP_DIR.is_dir():
        BACKUP_DIR.mkdir(parents=True)

    # 统计原始大小和数量
    total_size = sum(
        f.stat().st_size for f in CASE_STUDY_DIR.rglob("*") if f.is_file()
    )
    dir_count = len([d for d in CASE_STUDY_DIR.rglob("*") if d.is_dir()])

    print(f"  正在备份 {total_size / 1024 / 1024:.1f} MB 数据...")
    shutil.copytree(CASE_STUDY_DIR, backup_path, symlinks=True)
    print(f"  ✅ 备份完成: {backup_path}")
    print(f"     目录数: {dir_count}, 文件大小: {total_size / 1024 / 1024:.1f} MB")
    return backup_path


def rollback(backup_path: Path):
    """从备份恢复 case_study"""
    if not backup_path or not backup_path.is_dir():
        print("  [ERROR] 备份路径无效，无法回滚")
        return False

    try:
        # 删除现有 case_study
        if CASE_STUDY_DIR.exists():
            shutil.rmtree(CASE_STUDY_DIR)
        # 从备份恢复
        shutil.copytree(backup_path, CASE_STUDY_DIR, symlinks=True)
        print(f"  ✅ 已回滚到: {backup_path}")
        return True
    except Exception as e:
        print(f"  [ERROR] 回滚失败: {e}", file=sys.stderr)
        return False


# ========== 主逻辑 ==========


def main():
    print("=" * 60)
    print("准备 case 数据: verified-first → case_study")
    print("=" * 60)

    # 1. 备份（使用已有备份，跳过耗时备份）
    print("\n[1/4] 备份现有 case_study...")
    backup_path = backup_case_study()

    # 2. 删除现有 case_study
    print("\n[2/4] 删除现有 case_study...")
    if CASE_STUDY_DIR.exists():
        # 先记录删除前的统计数据
        old_dirs = sum(1 for _ in CASE_STUDY_DIR.rglob("*") if _.is_dir())
        old_files = sum(1 for _ in CASE_STUDY_DIR.rglob("*") if _.is_file())
        shutil.rmtree(CASE_STUDY_DIR)
        print(f"  已删除: {old_dirs} 个目录, {old_files} 个文件")
    else:
        print("  case_study 目录不存在，跳过删除")

    # 3. 扫描 verified-first 目录并复制
    print("\n[3/4] 扫描 verified-first 中的 case 并复制...")
    all_cases = sorted([
        d for d in VERIFIED_FIRST_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ])
    print(f"  找到 {len(all_cases)} 个 case 目录")

    direction_counts = Counter()
    copied = []
    errors = []

    for i, case_dir in enumerate(all_cases, 1):
        case_info = get_case_info(case_dir, "source")
        if not case_info:
            errors.append((case_dir.name, "无 case-info.json"))
            continue

        status, direction, pkg = copy_case(case_dir, case_info)
        direction_counts[direction] += 1

        if status == "copied":
            copied.append((direction, pkg))
            if i % 50 == 0 or i == len(all_cases):
                print(f"  [{i}/{len(all_cases)}] 已复制 {len(copied)} 个...")
        else:
            errors.append((case_dir.name, f"复制失败 ({direction}/{pkg})"))

    # 4. 汇总
    print("\n[4/4] 汇总")
    print(f"\n{'=' * 60}")
    print(f"总 case: {len(all_cases)}")
    print(f"成功复制: {len(copied)}")
    print(f"错误: {len(errors)}")
    print()

    print("方向分布:")
    for direction, count in sorted(direction_counts.items()):
        print(f"  {direction}: {count}")

    if errors:
        print(f"\n错误列表:")
        for name, reason in errors:
            print(f"  ❌ {name}: {reason}")

    # 检查是否有重复的 package_name 在同一 direction 下
    print("\n重复检查:")
    for direction in set(d for d, _ in copied):
        dir_path = CASE_STUDY_DIR / direction
        if dir_path.is_dir():
            cases = [d.name for d in dir_path.iterdir() if d.is_dir()]
            dupes = [name for name, count in Counter(cases).items() if count > 1]
            if dupes:
                print(f"  ⚠️  {direction}: 重复的 package: {dupes}")
            else:
                print(f"  ✅ {direction}: 无重复 ({len(cases)} 个 case)")

    # 提供回滚提示
    if errors:
        print(f"\n⚠️  有 {len(errors)} 个错误，如需回滚执行:")
        print(f"   python3 -c \"from prepare_data import rollback; rollback('{backup_path}')\"")
    elif copied:
        print(f"\n✅ 全部完成！如需回滚执行:")
        print(f"   python3 -c \"from prepare_data import rollback; rollback('{backup_path}')\"")

    print(f"\n备份路径: {backup_path}")


if __name__ == "__main__":
    main()