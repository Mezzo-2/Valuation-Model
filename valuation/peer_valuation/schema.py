"""同业卡规范表。pe_y1 以代码结果为准。"""

from __future__ import annotations

from typing import Any
import math

from valuation.peer_valuation.fetch import _norm_ticker
from valuation.segment_split.schema import SchemaError
from valuation.peer_valuation.judgment import model_pe_errors, uses_model_pe

CORE_MIN = 4


def require_comps(payload: dict, facts: dict, filled: list[dict] | None = None, *, require_model_pe: bool = False) -> dict:
    canonical = normalize_comps(payload, facts, filled)
    errors = validate_comps(canonical, require_model_pe=require_model_pe)
    from valuation.shared.markets import validate_pe_basis
    try:
        validate_pe_basis(facts, canonical)
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise SchemaError(errors)
    return canonical


def normalize_comps(raw: dict, facts: dict, filled: list[dict] | None = None) -> dict:
    by_ticker = {
        str(item.get("ticker") or ""): item
        for item in (filled or [])
        if item.get("ticker")
    }
    core = [_norm_core(item, by_ticker) for item in (raw.get("core") or [])]
    adjust = raw.get("pe_adjust")
    try:
        pe_adjust = 1.0 if adjust is None or adjust == "" else float(adjust)
    except (TypeError, ValueError):
        pe_adjust = adjust
    canonical = {
        "ts": raw.get("ts") or facts.get("as_of") or "",
        "core": [item for item in core if item.get("name")],
        "pe_adjust": pe_adjust,
        "rationale": str(raw.get("rationale") or "").strip(),
        "open_gaps": [
            str(item).strip()
            for item in (raw.get("open_gaps") or [])
            if str(item).strip()
        ],
    }
    if uses_model_pe(raw):
        canonical.update(schema_version=2, model_pe=raw.get("model_pe"),
                         model_pe_reason=raw.get("model_pe_reason"),
                         model_pe_sources=raw.get("model_pe_sources") or [])
    return canonical


def validate_comps(payload: dict, *, require_model_pe: bool = False) -> list[str]:
    errors = model_pe_errors(payload, required=require_model_pe)
    core = payload.get("core") or []
    if len(core) < CORE_MIN:
        errors.append(f"核心同业池至少 {CORE_MIN} 家，实际 {len(core)}")
    seen: set[str] = set()
    for item in core:
        name = str(item.get("name") or "")
        tick = str(item.get("ticker") or "")
        if tick and tick in seen:
            errors.append(f"核心池代码重复 {tick}")
        if tick:
            seen.add(tick)
        if not _is_number(item.get("pe_y1")):
            errors.append(f"{name or tick or '?'} 缺代码算出的预测首年 PE")
        if not str(item.get("note") or "").strip():
            errors.append(f"{name or tick or '?'} 核心池缺备注")
    if not _is_number(payload.get("pe_adjust")):
        errors.append("pe_adjust 须为数字")
    elif not 0 < float(payload["pe_adjust"]) <= 3:
        errors.append(f"pe_adjust 须为 (0, 3]，实际 {payload.get('pe_adjust')}")
    if not str(payload.get("rationale") or "").strip():
        errors.append("rationale 为空")
    return errors


def _norm_core(raw: Any, filled: dict[str, dict]) -> dict:
    if not isinstance(raw, dict):
        return {}
    ticker = _ticker_of(raw)
    src = filled.get(ticker) if ticker else None
    name = str(raw.get("name") or (src or {}).get("name") or "").strip()
    if src:
        name = src.get("name") or name
    return {
        "name": name,
        "ticker": ticker,
        "pe_y1": src.get("pe_y1") if src else raw.get("pe_y1"),
        "pe_ttm": src.get("pe_ttm") if src else raw.get("pe_ttm"),
        "mcap": src.get("mcap") if src else raw.get("mcap"),
        "currency": src.get("currency") if src else raw.get("currency"),
        "note": str(raw.get("note") or raw.get("why") or "").strip(),
        "eps_scope": (src or raw).get("eps_scope"),
        "forecast_period": (src or raw).get("forecast_period"),
        "pe_source": (src or raw).get("pe_source"),
        "basis_gap": (src or raw).get("basis_gap"),
    }


def _ticker_of(raw: dict) -> str:
    text = _norm_ticker(raw.get("ticker") or raw.get("code") or "")
    if text:
        return text
    name = str(raw.get("name") or "")
    if "（" in name and name.endswith("）"):
        return name[name.rfind("（") + 1 : -1]
    if "(" in name and name.endswith(")"):
        return name[name.rfind("(") + 1 : -1]
    parts = name.split()
    if parts and parts[-1].isdigit():
        return parts[-1]
    return ""


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number)
