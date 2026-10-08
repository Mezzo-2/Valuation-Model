"""把历史拆分卡整理成可读的表格说明；金额结论由已校验数据计算。"""

from __future__ import annotations

import re

from valuation.segment_split.split_plan import is_residual_name
from valuation.shared.houses import short_house_name

_QUALITY_LABEL = {
    "直接披露": "年报披露",
    "有据可查": "券商资料",
    "行业推算": "行业推算",
    "推算": "推算",
    "倒推": "倒推",
    "兜底": "估算",
}


def row_remark(seg: dict, periods: list[str]) -> str:
    """备注只说取数方式；混合来源时标出对应年份。"""
    quality = seg.get("data_quality") or {}
    labels = [_QUALITY_LABEL.get(str(quality.get(year) or ""), "") for year in periods]
    if not any(labels):
        tag = re.match(r"^\[([^]]+)\]", str(seg.get("note") or ""))
        return _QUALITY_LABEL.get(tag.group(1), tag.group(1)) if tag else "取数见说明"
    if len(set(labels)) == 1:
        return labels[0]
    return "；".join(f"{year}{label or '待核'}" for year, label in zip(periods, labels))


def split_logic(canonical: dict) -> str:
    text = str((canonical.get("split_explanation") or {}).get("拆分逻辑") or "").strip()
    # 旧卡把逐年对账式塞进逻辑栏；数字已经在上方表格，不再重复。
    text = re.split(r"逐年校验[:：]", text, maxsplit=1)[0].rstrip("；。 ")
    if text and "名单固定为" not in text:
        return text + "。"
    parents = list(canonical.get("official_parents") or [])
    segs = list(canonical.get("final_segments") or [])
    classify = next(
        (str(src.get("summary") or "") for src in canonical.get("sources") or []
         if isinstance(src, dict) and src.get("source_tool") == "get_main_business_segments"),
        "主营构成",
    )
    lines = [f"以年报{classify}口径为对账基准。"]
    direct_names: list[str] = []
    for parent in parents:
        members = [seg for seg in segs if seg.get("parent") == parent]
        names = [str(seg.get("name") or "") for seg in members]
        if names == [parent]:
            direct_names.append(parent)
        elif names:
            lines.append(f"{parent}细分为{'、'.join(names)}，各子项与保留的余量合计对回年报父项。")
    if direct_names:
        lines.insert(1, f"{'、'.join(direct_names)}沿用年报披露名称和金额。")
    if any(not seg.get("parent") and is_residual_name(str(seg.get("name") or ""), official_parents=parents) for seg in segs):
        lines.append("已列分部未覆盖的公司营业收入计入其他。")
    return "\n".join(lines)


def structure_change(canonical: dict, facts: dict) -> str:
    periods = list(facts.get("hist_periods") or [])
    if len(periods) < 2:
        return "历史期间不足两年，无法计算结构变化。"
    previous, latest = periods[-2:]
    revenue = [float(value) for value in facts["income"]["营业收入"]]
    total_previous, total_latest = revenue[-2:]
    total_delta = total_latest - total_previous
    total_growth = total_delta / total_previous if total_previous else None
    lines = [
        f"{latest} 营业收入{total_latest:.2f}亿元，较{previous}"
        f"{_direction(total_delta)}{abs(total_delta):.2f}亿元"
        f"（{_growth_pct(total_growth)}）。"
    ]
    groups: dict[str, list[dict]] = {}
    parents = set(canonical.get("official_parents") or [])
    for seg in canonical.get("final_segments") or []:
        parent = str(seg.get("parent") or "")
        key = parent if parent in parents else str(seg.get("name") or "")
        groups.setdefault(key, []).append(seg)
    for name, members in groups.items():
        old = sum(float((seg.get("historical_revenue") or {}).get(previous) or 0) for seg in members)
        new = sum(float((seg.get("historical_revenue") or {}).get(latest) or 0) for seg in members)
        delta = new - old
        growth = delta / old if old else None
        share = new / total_latest if total_latest else None
        lines.append(
            f"{name}：占比{_pct(share)}，同比{_growth_pct(growth)}，"
            f"对收入变动贡献{_signed(delta)}亿元。"
        )
    return "\n".join(lines)


def other_business(canonical: dict, facts: dict) -> str:
    periods = list(facts.get("hist_periods") or [])
    latest = periods[-1] if periods else ""
    total = float(facts["income"]["营业收入"][-1])
    parents = canonical.get("official_parents") or []
    lines: list[str] = []
    for seg in canonical.get("final_segments") or []:
        name = str(seg.get("name") or "")
        parent = str(seg.get("parent") or "")
        if not is_residual_name(name, parent=parent, official_parents=parents):
            continue
        amount = float((seg.get("historical_revenue") or {}).get(latest) or 0)
        quality = str((seg.get("data_quality") or {}).get(latest) or "")
        scope = f"{parent}项下未单列部分" if parent else "公司营业收入与已列分部之间的差额"
        basis = "年报单列" if quality == "直接披露" else "倒推收口" if quality == "倒推" else _QUALITY_LABEL.get(quality, "估算")
        lines.append(
            f"{name}：{scope}，{latest} 为{amount:.2f}亿元、占总收入{_pct(amount / total if total else None)}；"
            f"取数方式为{basis}。"
        )
    agent_note = str((canonical.get("split_explanation") or {}).get("其他业务说明") or "").strip()
    # 仅保留说明业务边界的文字，不把旧版逐年残差公式再抄一遍。
    if agent_note and not re.search(r"\d{4}A\s*[:：]|=|＝", agent_note):
        lines.append(agent_note)
    if not lines:
        return "无单列的其他业务或残差项；各行均按上方分部口径列示。"
    if any("倒推收口" in line for line in lines):
        lines.append("倒推项是口径差额，不能据此断定具体业务构成。")
    return "\n".join(lines)


def source_files(canonical: dict) -> str:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for source in canonical.get("sources") or []:
        if not isinstance(source, dict):
            continue
        title = str(source.get("source_title") or "").strip()
        if not title:
            continue
        institution = str(source.get("institution") or "").strip()
        date = str(source.get("published") or source.get("report_date") or "").strip()
        if institution and institution not in title:
            title = f"{short_house_name(institution)}－{title}"
        key = (title, date)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{title}｜{date or '日期未提供'}")
    return "\n".join(lines) or "来源文件未记录。"


def _pct(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "不适用"


def _growth_pct(value: float | None) -> str:
    return f"{value:+.1%}" if value is not None else "不适用"


def _direction(value: float) -> str:
    return "增加" if value >= 0 else "减少"


def _signed(value: float) -> str:
    return f"{value:+.2f}"
