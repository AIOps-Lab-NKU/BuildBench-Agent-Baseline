"""baseline_spec_patch.py — LLM 推理 + spec 规则增强基线（Patch 模式，无 MCP，无 function calling tools）

This baseline inherits all infrastructure from baseline_patch.py, but:
- Uses a different system prompt that explicitly includes cross-architecture
  repair heuristics from the spec-only heuristic baseline.
- Still uses unified diff (patch format) for full file access.
- The spec rules serve as strategy guidance, not as an access restriction.

The repair category mapping follows the spec-only heuristic:
1. Add target architecture to Architecture field (deb)
2. Remove [arch] restrictions that exclude target (deb/rpm)
3. Relax version constraints on Build-Depends / Requires
4. Fix build macro selection (--host, -march, etc.)
"""

from typing import Optional
from baseline_patch import AutoRepairBaselinePatch, LLMConfig

class AutoRepairBaselineSpecPatch(AutoRepairBaselinePatch):
    """LLM baseline with spec rules as strategy guidance.

    This is a thin wrapper that just forces mode='spec_patch' to load the
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
            mode="spec_patch",  # force spec_patch mode prompt
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
    baseline = AutoRepairBaselineSpecPatch(llm=llm_cfg)
    baseline.process_all_packages()


if __name__ == "__main__":
    main()