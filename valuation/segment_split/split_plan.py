"""拆分步出口：照搬券商拆法，挂回年报父口径。不含数字。"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from valuation.segment_split.schema import SchemaError, unsupported_zero_history

PLAN_CALIBERS = frozenset({"official", "drilled"})
RESIDUAL_NAMES = frozenset({"其他", "其他业务", "其他主营业务", "其他主营"})
_BARE_RESIDUALS = RESIDUAL_NAMES | frozenset({"其余", "其余部分", "其他部分"})
_RESIDUAL_TAILS = ("其他部分", "其余部分", "其他业务", "其余", "其他")


def parent_residual_name(parent: str) -> str:
    return f"{str(parent or '').strip()}其他"


def _matches_parent_residual(name: str, parent: str) -> bool:
    text = str(name or "").strip()
    host = str(parent or "").strip()
    if not text or not host or not text.startswith(host):
        return False
    return text[len(host):].strip() in _RESIDUAL_TAILS


def is_residual_name(
    name: str,
    *,
    parent: str = "",
    official_parents: list[str] | tuple[str, ...] | None = None,
) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if text in _BARE_RESIDUALS:
        return True
    host = str(parent or "").strip()
    if host and _matches_parent_residual(text, host):
        return True
    for official in official_parents or ():
        official = str(official or "").strip()
        if official and _matches_parent_residual(text, official):
            return True
    return False


def _rename_residual_row(name: str, parent: str, official_parents: list[str]) -> tuple[str, str]:
    text = str(name or "").strip()
    host = str(parent or "").strip()
    parents = [str(item).strip() for item in official_parents if str(item).strip()]
    if host and host in parents and (text in _BARE_RESIDUALS or _matches_parent_residual(text, host)):
        return parent_residual_name(host), host
    if text in RESIDUAL_NAMES:
        return text, ""
    if text in _BARE_RESIDUALS:
        return "其他", ""
    for official in parents:
        if _matches_parent_residual(text, official):
            return parent_residual_name(official), official
    return text, host


def money(value: float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money_sum(values: list[float]) -> Decimal:
    return sum((money(value) for value in values), Decimal(0))


def same_money(left: float | Decimal, right: float | Decimal) -> bool:
    return money(left) == money(right)


def has_company_residual(
    segs: list[dict[str, Any]],
    official_parents: list[str] | tuple[str, ...] | None = None,
) -> bool:
    parents = [str(item).strip() for item in (official_parents or []) if str(item).strip()]
    for seg in segs:
        parent = str(seg.get("parent") or "").strip()
        if parent:
            continue
        name = str(seg.get("name") or "").strip()
        if is_residual_name(name, parent="", official_parents=parents):
            return True
    return False


def resolve_unsupported_children(
    segments: list[dict[str, Any]],
    periods: list[str],
    official_rows: list[dict[str, Any]],
    official_parents: list[str],
    *,
    last_total: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """删去全历史兜底零；整个父项无有效具名子项时恢复年报父项。"""
    unresolved = [seg for seg in segments if unsupported_zero_history(seg, periods)]
    if not unresolved:
        return segments, []
    parents = set(official_parents)
    for seg in unresolved:
        parent = str(seg.get("parent") or "")
        if parent not in parents or seg["name"] == parent or is_residual_name(seg["name"], parent=parent):
            raise SchemaError([f"{seg['name']} 历史全为兜底零且无法退回有效年报父项，须补齐证据"])
    out = list(segments)
    decisions = []
    for parent in official_parents:
        dropped = [seg for seg in unresolved if seg.get("parent") == parent]
        if not dropped:
            continue
        amounts = {year: _official_row_amount(official_rows, year, parent) for year in periods}
        if any(amount is None for amount in amounts.values()):
            raise SchemaError([f"{parent} 缺少年报父项历史收入，不能撤销无依据下钻后以零补数"])
        members = [seg for seg in out if seg.get("parent") == parent]
        drop_names = {seg["name"] for seg in dropped}
        supported = [seg for seg in members if seg["name"] not in drop_names
                     and seg["name"] != parent and not is_residual_name(seg["name"], parent=parent)]
        collapse = not supported
        replacement = {
            "name": parent, "parent": parent, "预测方法": "",
            "historical_revenue": amounts,
            "data_quality": {year: "直接披露" for year in periods},
            "source_refs": list(dict.fromkeys(ref for seg in members for ref in seg.get("source_refs") or [])),
            "revenue_share_latest": amounts[periods[-1]] / last_total if last_total else None,
            "note": "[直接披露] 子项缺少历史拆分依据，恢复年报父项",
        }
        next_rows = []
        inserted = False
        for seg in out:
            if collapse and seg.get("parent") == parent:
                if not inserted:
                    next_rows.append(replacement)
                    inserted = True
            elif seg["name"] not in drop_names:
                next_rows.append(seg)
        out = next_rows
        decisions.append({"parent": parent, "removed": sorted(drop_names), "collapsed": collapse})
    return out, decisions


def plug_parent_residuals(
    segments: list[dict[str, Any]],
    periods: list[str],
    official_rows: list[dict[str, Any]],
    official_parents: list[str] | tuple[str, ...] | None = None,
    *,
    last_total: float = 0.0,
) -> tuple[list[dict[str, Any]], list[str]]:
    """具名子项不等于年报父项时，按精确差额补一行父口径其他。相等则不留这行。"""
    years = [str(period) for period in periods]
    parents = [str(item).strip() for item in (official_parents or []) if str(item).strip()]
    plugs: dict[str, dict[str, Any]] = {}
    drop_names: set[str] = set()
    added: list[str] = []
    for parent in parents:
        members = [seg for seg in segments if str(seg.get("parent") or "").strip() == parent]
        named = [
            seg
            for seg in members
            if str(seg.get("name") or "").strip() != parent
            and not is_residual_name(
                str(seg.get("name") or ""),
                parent=parent,
                official_parents=parents,
            )
        ]
        if not named:
            continue
        gaps: dict[str, float] = {}
        uncovered = False
        complete = True
        has_official = False
        for year in years:
            revenues = [(seg.get("historical_revenue") or {}).get(year) for seg in named]
            if any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in revenues):
                complete = False
                break
            official_amt = _official_row_amount(official_rows, year, parent)
            if official_amt is None:
                gaps[year] = 0.0
                continue
            has_official = True
            gap = money(official_amt) - money_sum([float(item) for item in revenues])
            gaps[year] = float(gap)
            if gap != 0:
                uncovered = True
        if not complete or not has_official:
            continue
        for seg in members:
            name = str(seg.get("name") or "").strip()
            if name != parent and is_residual_name(name, parent=parent, official_parents=parents):
                drop_names.add(name)
        if not uncovered:
            continue
        name = parent_residual_name(parent)
        if not any(str(seg.get("name") or "").strip() == name for seg in members):
            added.append(name)
        last = years[-1]
        amount = gaps.get(last)
        plugs[parent] = {
            "name": name,
            "parent": parent,
            "预测方法": "",
            "historical_revenue": gaps,
            "data_quality": {year: "倒推" for year in years},
            "source_refs": list(named[0].get("source_refs") or []),
            "revenue_share_latest": float(amount) / last_total if last_total and amount is not None else None,
            "note": "[倒推]",
        }
    if not plugs and not drop_names:
        return segments, []
    kept = [seg for seg in segments if str(seg.get("name") or "").strip() not in drop_names]
    if not plugs:
        return kept, []
    last_index = {
        parent: max(i for i, seg in enumerate(kept) if str(seg.get("parent") or "").strip() == parent)
        for parent in plugs
    }
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(kept):
        out.append(seg)
        for parent, row in plugs.items():
            if last_index[parent] == i:
                out.append(row)
    return out, added


def _official_row_amount(rows: list[dict[str, Any]], year: str, name: str) -> float | None:
    year_key = str(year)[:4]
    for row in rows:
        if row.get("is_total"):
            continue
        period = str(row.get("period") or "")
        if period != year and period[:4] != year_key:
            continue
        if str(row.get("name") or "").strip() != name:
            continue
        raw = row.get("revenue_yi")
        if raw is None:
            continue
        return float(raw)
    return None


def _ensure_company_residual(
    segs: list[dict[str, Any]],
    official_parents: list[str],
) -> list[dict[str, Any]]:
    if not official_parents or has_company_residual(segs, official_parents):
        return segs
    return [*segs, {"name": "其他", "parent": ""}]


def _normalize_residual_segments(segs: list[dict[str, Any]], official_parents: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for seg in segs:
        row = dict(seg)
        name, parent = _rename_residual_row(row.get("name") or "", row.get("parent") or "", official_parents)
        row["name"] = name
        row["parent"] = parent
        row.pop("method", None)
        row.pop("why_method", None)
        out.append(row)
    return out


QTY_HINTS = ("出货", "单价", "ASP", "销量", "万只", "shipment")
GROWTH_HINTS = ("增速", "同比")
MARKET_HINTS = ("市场规模", "渗透率", "市占率")
ORDER_HINTS = ("在手订单", "新签订单", "转化率")
STORE_HINTS = ("门店", "单店", "坪效", "店效")
USER_HINTS = ("订阅用户", "用户数", "单用户", "ARPU", "付费用户")
TRACE_GROUPS = (
    ("qty", QTY_HINTS),
    ("growth", GROWTH_HINTS),
    ("market", MARKET_HINTS),
    ("order", ORDER_HINTS),
    ("store", STORE_HINTS),
    ("user", USER_HINTS),
)
CALIBER_RULES = (
    "先照搬近期券商怎么拆，几家接近就合成一套；"
    "若一家拆得更细且与年报历史子项一致，优先跟更细的那套；"
    "若将多家近似表述合并命名，在 source_terms 填入切片原文出现过的原词以便回指。"
    "每条挂回一个年报父口径。若近期研报对某个年报父口径有下钻，结合研报与近期年报明细，"
    "将该父口径下当前仍在延续披露的同级子业务完整列出（例如某父项由 A、B、C 三项子业务构成，不要只列 A 而漏掉同级的 B、C）；"
    "仅在早期年份出现、近期年报和研报已合并并不再单列的旧子项不要拆出。"
    "下探时只留具名子项，不要预留父口径其他；"
    "公司营业收入和父口径之间的差额进「其他」。"
    "券商没拆就停在年报行。"
    "这一拍只锁名单。"
)
BLOCKED_KEYS = (
    "historical_revenue",
    "final_forecast",
    "fill_method",
    "consensus",
    "institution_estimates",
)


def require_split_plan(
    payload: dict,
    *,
    hits: list[dict[str, Any]] | None = None,
    official_rows: list[dict[str, Any]] | None = None,
) -> dict:
    errors = [f"拆分步不得出口 {key}" for key in BLOCKED_KEYS if key in payload]
    canonical = normalize_split_plan(payload)
    errors.extend(validate_split_plan(canonical, hits=hits, official_rows=official_rows))
    if errors:
        raise SchemaError(errors)
    return canonical


def normalize_split_plan(raw: dict) -> dict:
    official_parents = [
        str(name).strip() for name in (raw.get("official_parents") or []) if str(name).strip()
    ]
    segs = []
    for item in raw.get("segments") or []:
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {
            "name": str(item.get("name") or "").strip(),
            "parent": str(item.get("parent") or "").strip(),
        }
        terms = [str(t).strip() for t in (item.get("source_terms") or []) if str(t).strip()]
        if terms:
            row["source_terms"] = terms
        segs.append(row)
    segs = _ensure_company_residual(
        _normalize_residual_segments(segs, official_parents),
        official_parents,
    )
    return {
        "company": raw.get("company") or "",
        "ticker": raw.get("ticker") or "",
        "ts": raw.get("ts") or "",
        "caliber": raw.get("caliber") or "official",
        "official_parents": official_parents,
        "segments": segs,
    }


def validate_split_plan(
    payload: dict,
    *,
    hits: list[dict[str, Any]] | None = None,
    official_rows: list[dict[str, Any]] | None = None,
) -> list[str]:
    errors: list[str] = []
    if payload.get("caliber") not in PLAN_CALIBERS:
        errors.append(f"caliber 必须是 official 或 drilled，实际 {payload.get('caliber')}")
    segs = payload.get("segments") or []
    if len(segs) < 2:
        errors.append("拆分名单至少保留两个分部（含其他），历史收入回填需要至少两行")
    names = [str(seg.get("name") or "") for seg in segs]
    if names and len(names) != len(set(names)):
        errors.append("分部名称重复")
    parents = list(payload.get("official_parents") or [])
    parent_set = set(parents)
    hist_official = {
        str(row.get("name") or "").strip()
        for row in (official_rows or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip() and not row.get("is_total")
    }
    if segs and all(
        is_residual_name(str(seg.get("name") or ""), parent=str(seg.get("parent") or ""), official_parents=parents)
        for seg in segs
    ):
        errors.append("拆分名单不能只有其他，至少保留一条具名主营业务")
    for seg in segs:
        name = str(seg.get("name") or "")
        parent = str(seg.get("parent") or "")
        terms = [str(t).strip() for t in (seg.get("source_terms") or []) if str(t).strip()]
        residual = is_residual_name(name, parent=parent, official_parents=parents)
        if not name:
            errors.append("分部缺 name")
            continue
        if hits is not None and not residual and name not in parent_set and name not in hist_official:
            if not _name_mentioned(name, hits, source_terms=terms):
                errors.append(f"{name} 切片里没有出现，不能写入拆分")
        if parent and parent_set and parent not in parent_set and name not in parent_set:
            errors.append(f"{name} parent {parent} 不在 official_parents")
    if parents and not has_company_residual(segs, parents):
        errors.append("缺公司级残差（其他），年报父项合计对不上营业收入时无处收口")
    if payload.get("caliber") == "drilled" and all(
        name in parent_set or is_residual_name(name, parent=str(seg.get("parent") or ""), official_parents=parents)
        for name, seg in zip(names, segs)
    ):
        errors.append("caliber=drilled 但名单仍停在年报父口径")
    return errors


def line_traces(name: str, hits: list[dict[str, Any]]) -> list[str]:
    """已确认口径回查时，这一行在切片里碰到过哪些痕迹。"""
    found: list[str] = []
    if _hint_trace(name, hits, ("收入", "营收", "亿元", "占比")):
        found.append("income")
    for key, tokens in TRACE_GROUPS:
        if _hint_trace(name, hits, tokens):
            found.append(key)
    return found


def _name_mentioned(
    name: str,
    hits: list[dict[str, Any]],
    *,
    source_terms: list[str] | None = None,
) -> bool:
    candidates = [name.strip(), *(str(t).strip() for t in (source_terms or []) if str(t).strip())]
    candidates = [c for c in candidates if c]
    if not candidates:
        return False
    for hit in hits:
        blob = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        specs = hit.get("specs") or []
        if any(needle in blob or needle in specs for needle in candidates):
            return True
    return False


def _hint_trace(name: str, hits: list[dict[str, Any]], tokens: tuple[str, ...]) -> bool:
    needle = name.strip()
    if not needle:
        return False
    for hit in hits:
        blob = f"{hit.get('title') or ''} {hit.get('snippet') or ''}"
        specs = hit.get("specs") or []
        if needle not in blob and needle not in specs:
            continue
        if any(token in blob for token in tokens):
            return True
    return False


def hit_traces(text: str) -> list[str]:
    blob = str(text or "")
    return [key for key, tokens in TRACE_GROUPS if any(token in blob for token in tokens)]
