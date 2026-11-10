"""模型 PE 的共同读取规则；旧任务明确沿用均值。"""

from __future__ import annotations

import math


def uses_model_pe(comps: dict) -> bool:
    return comps.get("schema_version") == 2 or any(
        key in comps for key in ("model_pe", "model_pe_reason")
    )


def model_pe_errors(comps: dict, *, required: bool = False) -> list[str]:
    if not required and not uses_model_pe(comps):
        return []
    errors = []
    value = comps.get("model_pe")
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        errors.append("model_pe 须为有限正数，不能回退到同业均值")
    reason = comps.get("model_pe_reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("model_pe_reason 不能为空，须说明模型给定 PE 的依据")
    return errors


def resolve_model_pe(comps: dict) -> tuple[float, str, bool]:
    """返回基础 PE、展示原因、是否由 Agent 独立给定。"""
    errors = model_pe_errors(comps)
    if errors:
        raise ValueError("；".join(errors))
    if uses_model_pe(comps):
        return float(comps["model_pe"]), comps["model_pe_reason"].strip(), True
    pes = [float(item["pe_y1"]) for item in comps.get("core") or []]
    if not pes or any(not math.isfinite(pe) or pe <= 0 for pe in pes):
        raise ValueError("历史模式缺少有效同业 PE")
    return (sum(pes) / len(pes),
            "历史任务沿用核心池均值；重跑同业估值后生成模型独立给定的 PE。", False)
