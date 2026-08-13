"""baseline_spec.py — LLM 推理 + spec-only 策略引导基线（Full 文件替换模式，无 MCP，无 function calling tools）

This baseline inherits all infrastructure from baseline_patch.py, but:
- Uses a different system prompt that explicitly includes cross-architecture
  repair heuristics from the spec-only heuristic baseline.
- Still allows full-file replacement (similar to full mode), but with
  spec rules guidance to help LLM focus on the right patterns.

The repair category mapping follows the spec-only heuristic:
1. Add target architecture to Architecture field (deb)
2. Remove [arch] restrictions that exclude target (deb/rpm)
3. Relax version constraints on Build-Depends / Requires
4. Fix build macro selection (--host, -march, etc.)
"""

from typing import Optional
from baseline_patch import AutoRepairBaselinePatch, LLMConfig

class AutoRepairBaselineSpec(AutoRepairBaselinePatch):
    """LLM baseline with spec-only rules guidance.

    This is a thin wrapper that just forces mode='spec' to load the
    spec-enhanced prompt. All other behavior is inherited.
    """
    def __init__(
        self,
        llm: Optional[LLMConfig] = None,
        base_dir: Optional[str] = None,
        max_build_attempts: int = 3,
        prebuild: Optional[bool] = None,
    ) -> None:
        super().__init__(
            llm=llm,
            base_dir=base_dir,
            max_build_attempts=max_build_attempts,
            prebuild=prebuild,
            mode="spec",  # force spec mode prompt
        )

def main():
    import os
    from baseline_patch import info as cfg_info

    provider = cfg_info["LLM_PROVIDER"].lower()
    default_model = {
        "openai": "gpt-5",
        "qwen": os.getenv("LLM_MODEL", "qwen3-max"),
        "claude": "claude-sonnet-4-5-20250929",
        "deepseek": "deepseek-v3",
    }.get(provider)

    llm_cfg = LLMConfig(provider=provider, model=default_model)
    baseline = AutoRepairBaselineSpec(llm=llm_cfg)
    baseline.process_all_packages()


if __name__ == "__main__":
    main()
