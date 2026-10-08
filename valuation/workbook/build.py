from __future__ import annotations

import math
from copy import copy
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import InlineFont
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from valuation.segment_research.methods import (
    RATE_FIELDS,
    as_growth_rate,
    assume_unit,
    build_unit,
    fx_unit_note,
    method_spec,
)
from valuation.workbook.anchors import (
    cell_ref,
    cell_ref_at,
    find_col_by_header,
    find_row_by_label,
    write_note,
)
from valuation.historical_financials.company_info import (
    market_rows as company_market_rows,
    profile_rows as company_profile_rows,
)
from valuation.historical_financials.subjects import (
    BALANCE_BOLD,
    BALANCE_ORDER,
    FS_LEFT_COL,
    FS_RIGHT_COL,
    HIST_METRICS,
    INCOME_BOLD,
    INCOME_ORDER,
    MARKET_ROWS,
    fs_label,
)
from valuation.shared.io import artifacts_dir, spec_dir, workbook_filename
from valuation.workbook.load_json import load_spec
from valuation.workbook.tree import (
    build_split_groups,
    segment_group_title,
    top_level_names,
)
from valuation.workbook.styles import (
    AMT_FMT,
    MULTIPLE_FMT,
    NUM_FMT,
    PX_FMT,
    SIGNED_PCT_FMT,
    CROSS_GREEN,
    EXTERNAL_RED,
    FORMULA_BLACK,
    INPUT_BLUE,
    MUTED,
    NEAR,
    ROW_LABEL_MARKET,
    ROW_LABEL_NOTE,
    ROW_LABEL_VAL,
    TEXT_FONT,
    apply_number_formats,
    apply_structure_style,
    choose_number_format,
    finish_sheet,
    mark_bold_row,
    mark_input,
    mark_result_cell,
    write_input,
    mark_subtotal,
    paint_stub_column,
    reset_style_marks,
    set_col_widths,
    write_block_header,
    write_column_header_row,
    write_field_label,
    write_first_section_header,
    write_group_row,
    write_page_title,
    write_row_label,
    write_section_row,
    write_sheet_title,
    write_spacer,
)

SHEET_ORDER = [
    "总结",
    "主营业务拆分",
    "收入预测",
    "运营成本预测",
    "利润表预测",
    "估值预测",
    "历史财务数据",
]

def build_workbook(run_dir: Path) -> Path:
    reset_style_marks()
    try:
        spec = load_spec(run_dir, require_notes=True)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc
    facts = spec["facts"]
    hist = facts["hist_periods"]
    fcst = facts["forecast_periods"]
    periods = hist + fcst

    wb = Workbook()
    default = wb.active
    wb.remove(default)
    for name in SHEET_ORDER:
        wb.create_sheet(name)

    _hist(wb, spec)
    _split(wb, spec)
    _revenue(wb, spec)
    _cost(wb, spec)
    _pnl(wb, spec)
    _valuation(wb, spec)
    _summary(wb, spec)

    wb._sheets.sort(key=lambda ws: SHEET_ORDER.index(ws.title))
    out = artifacts_dir(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / workbook_filename(facts.get("company"), facts.get("ticker"))
    wb.save(path)
    return path


def build_hist_workbook(run_dir: Path) -> Path:
    from valuation.shared.io import load_json

    reset_style_marks()
    facts = load_json(spec_dir(run_dir) / "facts.json")
    wb = Workbook()
    default = wb.active
    default.title = "历史财务数据"
    _hist(wb, {"facts": facts})
    out = artifacts_dir(run_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "historical_financials.xlsx"
    wb.save(path)
    return path


def _hist(wb: Workbook, spec: dict) -> None:
    facts = spec["facts"]
    hist = facts["hist_periods"]
    income = facts.get("income") or {}
    balance = facts.get("balance") or {}
    market = facts.get("market") or {}
    ws = wb["历史财务数据"]
    last = FS_RIGHT_COL + len(hist)

    write_block_header(ws, 2, FS_LEFT_COL, ["利润表（亿元）", *hist])
    data_row = write_block_header(ws, 2, FS_RIGHT_COL, ["资产负债表（亿元）", *hist])

    left = data_row
    for key in INCOME_ORDER:
        left = _write_fs_line(
            ws,
            left,
            FS_LEFT_COL,
            fs_label(key),
            hist,
            income.get(key),
            bold=key in INCOME_BOLD,
            formula=_income_formula(key, hist, ws) if key in {"毛利", "历史加权平均股本_百万股"} else None,
        )

    right = data_row
    for key in BALANCE_ORDER:
        right = _write_fs_line(
            ws,
            right,
            FS_RIGHT_COL,
            fs_label(key),
            hist,
            balance.get(key),
            bold=key in BALANCE_BOLD,
            formula=_wc_formula(hist, ws) if key == "营运资本" else None,
        )

    metric_row = max(left, right) + 1
    row = write_block_header(ws, metric_row, FS_LEFT_COL, ["历史指标", *hist])
    for i, (label, num_key, den_key) in enumerate(HIST_METRICS):
        ws.cell(row=row, column=FS_LEFT_COL, value=label)
        for j, year in enumerate(hist):
            col = FS_LEFT_COL + 1 + j
            if label == "营业收入增速":
                if j == 0:
                    continue
                prev = cell_ref(ws, fs_label(num_key), hist[j - 1], same_sheet=True)
                cur = cell_ref(ws, fs_label(num_key), year, same_sheet=True)
                ws.cell(row=row, column=col, value=f"={cur}/{prev}-1")
            else:
                num = cell_ref(ws, fs_label(num_key), year, same_sheet=True)
                den = cell_ref(ws, fs_label(den_key), year, same_sheet=True)
                ws.cell(row=row, column=col, value=f"={num}/{den}")
        row += 1

    row += 1
    write_section_row(ws, row, ["市场快照"], FS_LEFT_COL + len(hist), start_col=FS_LEFT_COL)
    row += 1
    row = _write_market(ws, row, market, facts.get("as_of"))

    write_sheet_title(ws, facts["company"], last)
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["E"].width = 3
    ws.column_dimensions["F"].width = 32
    for col in range(2, 5):
        ws.column_dimensions[get_column_letter(col)].width = 13
    for col in range(7, last + 1):
        ws.column_dimensions[get_column_letter(col)].width = 13
    _format_hist(ws, last)
    paint_stub_column(ws, FS_LEFT_COL, NEAR)
    paint_stub_column(ws, FS_RIGHT_COL, NEAR)
    finish_sheet(ws)


def _split(wb: Workbook, spec: dict) -> None:
    facts = spec["facts"]
    split = spec["split"]
    hist = facts["hist_periods"]
    ws = wb["主营业务拆分"]
    headers = ["分部名称", *hist, "单位", "备注"]
    last = len(headers)
    write_column_header_row(ws, 2, headers)
    row = 3
    groups, company_res = build_split_groups(split)

    def write_leaf(seg: dict, *, indent: int = 0) -> None:
        nonlocal row
        cell = ws.cell(row=row, column=1, value=seg["name"])
        if indent:
            cell.alignment = Alignment(indent=indent, vertical="center")
        for i, val in enumerate(seg["hist_revenue"]):
            ws.cell(row=row, column=2 + i, value=val)
        ws.cell(row=row, column=2 + len(hist), value=seg["unit"])
        write_note(ws, row, last, seg["note"])
        if len(str(seg["note"])) > 14:
            ws.cell(row=row, column=last).alignment = Alignment(vertical="center", wrap_text=True)
            ws.row_dimensions[row].height = 34
        row += 1

    for group in groups:
        if group.drilled:
            parent_row = row
            ws.cell(row=row, column=1, value=group.parent)
            mark_bold_row(ws, row, 1, 1)
            unit = next((item.get("unit") for item in group.children if item.get("unit")), "亿元")
            ws.cell(row=row, column=2 + len(hist), value=unit)
            row += 1
            for child in group.children:
                write_leaf(child, indent=1)
            for i, year in enumerate(hist):
                parts = "+".join(
                    cell_ref(ws, child["name"], year, same_sheet=True) for child in group.children
                )
                ws.cell(row=parent_row, column=2 + i, value=f"={parts}")
        elif group.leaf:
            write_leaf(group.leaf)
    if company_res:
        write_leaf(company_res)
    ws.cell(row=row, column=1, value="合计")
    for i, year in enumerate(hist):
        parts = "+".join(
            cell_ref(ws, name, year, same_sheet=True) for name in top_level_names(groups, company_res)
        )
        ws.cell(row=row, column=2 + i, value=f"={parts}")
    mark_subtotal(ws, row, 1, 1 + len(hist), line=True)
    row += 2
    for label, text in (
        ("拆分逻辑", split["split_logic"]),
        ("结构变化", split["structure_change"]),
        ("其他业务说明", split["other_note"]),
        ("来源文件", split["source_files"]),
    ):
        first_row = row
        lines = [line.strip() for line in str(text or "").splitlines() if line.strip()] or ["暂无说明。"]
        for index, line in enumerate(lines):
            label_cell = ws.cell(row=row, column=1, value=label if index == 0 else None)
            apply_structure_style(ws, [label_cell], "field_label")
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last)
            write_note(ws, row, 2, line)
            ws.cell(row=row, column=2).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            # B:F 合并后可读宽度约为 70 个英文字符；中文按双宽估算行高。
            visual_width = sum(2 if ord(char) > 127 else 1 for char in line)
            ws.row_dimensions[row].height = min(120, 20 + 16 * max(0, math.ceil(visual_width / 70) - 1))
            row += 1
        if row - first_row > 1:
            ws.merge_cells(start_row=first_row, start_column=1, end_row=row - 1, end_column=1)
        label_cell = ws.cell(row=first_row, column=1)
        label_font = copy(label_cell.font)
        label_font.bold = True
        label_cell.font = label_font
        label_cell.alignment = Alignment(horizontal="left", vertical="center")
        row += 1
    write_sheet_title(ws, facts["company"], last)
    set_col_widths(ws, last, first=32, other=14)
    ws.column_dimensions[get_column_letter(last)].width = 28
    apply_number_formats(ws, set(), last, last)
    paint_stub_column(ws, 1, NEAR)
    finish_sheet(ws)


def _revenue(wb: Workbook, spec: dict) -> None:
    facts = spec["facts"]
    split = spec["split"]
    forecasts = spec["forecasts"]
    hist = facts["hist_periods"]
    fcst = facts["forecast_periods"]
    periods = hist + fcst
    ws = wb["收入预测"]
    last = 2 + len(periods) + 1  # unit + 备注 → periods start at 2, unit = 2+len, 备注 = last
    row = write_first_section_header(ws, [*periods, "单位", "备注"])
    unit_col = last - 1
    note_col = last
    groups, company_res = build_split_groups(split)

    def write_assumption(seg: dict) -> None:
        nonlocal row
        name = seg["name"]
        method = seg["method"]
        fc = forecasts[name]
        spec = method_spec(method)
        unit_meta = fc.get("unit_meta") or {}
        write_group_row(ws, row, segment_group_title(name, method), last)
        row += 1
        rows: dict[str, int] = {}
        if spec.needs_fx:
            ws.cell(row=row, column=1, value="单位换算因子")
            for year in periods:
                col = _pcol(periods, year)
                ws.cell(row=row, column=col, value=unit_meta["fx"])
                if year in fcst:
                    mark_input(ws, row, col)
            ws.cell(row=row, column=unit_col, value=fx_unit_note(unit_meta))
            rows["fx"] = row
            row += 1
        for field in spec.assume:
            ws.cell(row=row, column=1, value=field)
            ws.cell(row=row, column=unit_col, value=assume_unit(field) or build_unit(field, unit_meta))
            if field == spec.assume_anchor:
                write_note(ws, row, note_col, fc.get("final_rationale") or "")
            rows[field] = row
            row += 1
        seg["_rows"] = rows

    for group in groups:
        if group.drilled:
            write_group_row(ws, row, group.parent, last)
            row += 1
            for child in group.children:
                write_assumption(child)
        elif group.leaf:
            write_assumption(group.leaf)
    if company_res:
        write_assumption(company_res)

    write_section_row(ws, row, ["收入构建区"], last)
    row += 1
    top_lines: list[tuple[str, int]] = []
    for group in groups:
        if group.drilled:
            write_group_row(ws, row, group.parent, last)
            row += 1
            parent_rev_row = row
            ws.cell(row=row, column=1, value="分部收入")
            ws.cell(row=row, column=unit_col, value="亿元")
            row += 1
            for child in group.children:
                row = _write_revenue_build(
                    ws, row, child, forecasts, hist, fcst, periods, last, unit_col
                )
            for year in periods:
                parts = "+".join(
                    cell_ref_at(ws, child["_rows"]["分部收入"], year)
                    for child in group.children
                )
                ws.cell(row=parent_rev_row, column=_pcol(periods, year), value=f"={parts}")
            top_lines.append((group.parent, parent_rev_row))
        elif group.leaf:
            row = _write_revenue_build(
                ws, row, group.leaf, forecasts, hist, fcst, periods, last, unit_col
            )
            top_lines.append((group.leaf["name"], group.leaf["_rows"]["分部收入"]))
    if company_res:
        row = _write_revenue_build(
            ws, row, company_res, forecasts, hist, fcst, periods, last, unit_col
        )
        top_lines.append((company_res["name"], company_res["_rows"]["分部收入"]))

    write_section_row(ws, row, ["收入归因"], last)
    row += 1
    ws.cell(row=row, column=1, value="营业收入")
    total_row = row
    for year in periods:
        parts = "+".join(cell_ref_at(ws, rev_row, year) for _, rev_row in top_lines)
        ws.cell(row=row, column=_pcol(periods, year), value=f"={parts}")
    row += 1
    for name, rev_row in top_lines:
        ws.cell(row=row, column=1, value=f"{name}收入占比")
        for year in periods:
            seg = cell_ref_at(ws, rev_row, year)
            total = cell_ref_at(ws, total_row, year)
            ws.cell(row=row, column=_pcol(periods, year), value=f"={seg}/{total}")
        row += 1

    mark_subtotal(ws, total_row, 1, unit_col - 1, line=True)
    write_sheet_title(ws, facts["company"], last)
    set_col_widths(ws, last, first=32, other=13)
    ws.column_dimensions[get_column_letter(note_col)].width = 42
    ratio = set()
    for r in range(3, ws.max_row + 1):
        lab = str(ws.cell(row=r, column=1).value or "")
        if any(token in lab for token in ("增速", "占比", "同比")) or lab.split("_", 1)[0] in RATE_FIELDS:
            ratio.add(r)
    apply_number_formats(ws, ratio, last, note_col)
    paint_stub_column(ws, 1, NEAR)
    finish_sheet(ws)


def _write_revenue_build(ws, row, seg, forecasts, hist, fcst, periods, last, unit_col) -> int:
    name = seg["name"]
    method = seg["method"]
    fc = forecasts[name]
    spec = method_spec(method)
    unit_meta = fc.get("unit_meta") or {}
    rows = seg.setdefault("_rows", {})
    write_group_row(ws, row, segment_group_title(name, method), last)
    row += 1
    if spec.fcst_mode == "rev_grow":
        rev_row = row
        ws.cell(row=row, column=1, value="分部收入")
        for year in hist:
            ws.cell(row=row, column=_pcol(periods, year), value=fc["historical_data"]["分部收入"][year])
        ws.cell(row=row, column=unit_col, value="亿元")
        rows["分部收入"] = row
        row += 1
        grow_rev = rows["收入增速"]
        for i, year in enumerate(hist):
            if i == 0:
                _write_dash(ws, grow_rev, _pcol(periods, year))
                continue
            prev = hist[i - 1]
            cur = cell_ref_at(ws, rev_row, year)
            pri = cell_ref_at(ws, rev_row, prev)
            ws.cell(row=grow_rev, column=_pcol(periods, year), value=f"={cur}/{pri}-1")
        for i, year in enumerate(fcst):
            prev = hist[-1] if i == 0 else fcst[i - 1]
            g = cell_ref_at(ws, grow_rev, year)
            pri = cell_ref_at(ws, rev_row, prev)
            ws.cell(row=rev_row, column=_pcol(periods, year), value=f"={pri}*(1+{g})")
            write_input(
                ws,
                grow_rev,
                _pcol(periods, year),
                as_growth_rate(fc["final_forecast"]["收入增速"][year]),
            )
    else:
        for field in spec.build:
            ws.cell(row=row, column=1, value=field)
            for year in hist:
                ws.cell(
                    row=row,
                    column=_pcol(periods, year),
                    value=fc["historical_data"][field][year],
                )
            ws.cell(row=row, column=unit_col, value=build_unit(field, unit_meta))
            rows[f"构建_{field}"] = row
            row += 1
        rev_row = row
        ws.cell(row=row, column=1, value="分部收入")
        rows["分部收入"] = row
        for year in hist + fcst:
            ws.cell(
                row=row,
                column=_pcol(periods, year),
                value=_driver_revenue_formula(ws, spec, rows, year),
            )
        ws.cell(row=row, column=unit_col, value="亿元")
        row += 1
        if spec.fcst_mode == "grow":
            for i, year in enumerate(fcst):
                prev = hist[-1] if i == 0 else fcst[i - 1]
                for field, grow in spec.grow_from.items():
                    prior = cell_ref_at(ws, rows[f"构建_{field}"], prev)
                    rate = cell_ref_at(ws, rows[grow], year)
                    ws.cell(
                        row=rows[f"构建_{field}"],
                        column=_pcol(periods, year),
                        value=f"={prior}*(1+{rate})",
                    )
            for field, grow in spec.grow_from.items():
                assume_row = rows[grow]
                for i, year in enumerate(hist):
                    if i == 0:
                        _write_dash(ws, assume_row, _pcol(periods, year))
                        continue
                    cur = cell_ref_at(ws, rows[f"构建_{field}"], year)
                    pri = cell_ref_at(ws, rows[f"构建_{field}"], hist[i - 1])
                    ws.cell(row=assume_row, column=_pcol(periods, year), value=f"={cur}/{pri}-1")
                for year in fcst:
                    write_input(
                        ws,
                        assume_row,
                        _pcol(periods, year),
                        as_growth_rate(fc["final_forecast"][grow][year]),
                    )
        else:
            for field in spec.build:
                assume_row = rows[field]
                build_row = rows[f"构建_{field}"]
                for year in hist:
                    src = cell_ref_at(ws, build_row, year)
                    ws.cell(row=assume_row, column=_pcol(periods, year), value=f"={src}")
                for year in fcst:
                    value = fc["final_forecast"][field][year]
                    if field in RATE_FIELDS:
                        value = as_growth_rate(value)
                    write_input(ws, assume_row, _pcol(periods, year), value)
                    ws.cell(
                        row=build_row,
                        column=_pcol(periods, year),
                        value=f"={cell_ref_at(ws, assume_row, year)}",
                    )

    ws.cell(row=row, column=1, value="分部收入增速")
    rows["分部收入增速"] = row
    for i, year in enumerate(periods):
        if i == 0:
            _write_dash(ws, row, _pcol(periods, year))
            continue
        cur = cell_ref_at(ws, rows["分部收入"], year)
        pri = cell_ref_at(ws, rows["分部收入"], periods[i - 1])
        ws.cell(row=row, column=_pcol(periods, year), value=f"={cur}/{pri}-1")
    return row + 1


def _driver_revenue_formula(ws, spec, rows: dict[str, int], year: str) -> str:
    refs = {field: cell_ref_at(ws, rows[f"构建_{field}"], year) for field in spec.build}
    if spec.identity == "qty_price":
        fx = cell_ref_at(ws, rows["fx"], year)
        return f"={refs['销量']}*{refs['单价']}/{fx}"
    if spec.identity == "market":
        return f"={refs['市场规模']}*{refs['渗透率']}*{refs['市占率']}"
    if spec.identity == "order":
        return f"=({refs['期初在手订单']}+{refs['新签订单']})*{refs['转化率']}"
    if spec.identity == "store":
        fx = cell_ref_at(ws, rows["fx"], year)
        return f"={refs['门店数']}*{refs['单店产出']}/{fx}"
    if spec.identity == "user":
        fx = cell_ref_at(ws, rows["fx"], year)
        return f"={refs['订阅用户']}*{refs['单用户收入']}/{fx}"
    raise ValueError(f"没有收入公式: {spec.identity}")


def _cost(wb: Workbook, spec: dict) -> None:
    facts = spec["facts"]
    cost = spec["cost"]
    hist = facts["hist_periods"]
    fcst = facts["forecast_periods"]
    periods = hist + fcst
    ws = wb["运营成本预测"]
    last = 2 + len(periods)
    row = write_first_section_header(ws, [*periods, "备注"])

    def assume(label: str, hist_formula_from: str | None, fcst_map: dict, note: str = "") -> int:
        nonlocal row
        here = row
        ws.cell(row=row, column=1, value=label)
        if hist_formula_from:
            for year in hist:
                src = cell_ref(wb["历史财务数据"], hist_formula_from, year, same_sheet=False)
                if label == "有效税率":
                    den = cell_ref(wb["历史财务数据"], "税前利润", year, same_sheet=False)
                else:
                    den = cell_ref(wb["历史财务数据"], "营业收入", year, same_sheet=False)
                ws.cell(row=row, column=_pcol(periods, year), value=f"={src}/{den}")
        for year in fcst:
            write_input(ws, row, _pcol(periods, year), fcst_map[year])
        if note:
            write_note(ws, row, last, note)
        row += 1
        return here

    assume("合并毛利率", "毛利", cost["gross_margin"], cost["rationale"])
    assume("销售费用率", "销售费用", cost["sales_ratio"])
    assume("管理费用率", "管理费用", cost["admin_ratio"])
    assume("研发费用率", "研发费用", cost["rd_ratio"])
    assume("有效税率", "所得税费用", cost["tax_rate"])
    fs = wb["历史财务数据"]
    ws.cell(row=row, column=1, value="财务费用假设")
    for year in hist:
        ws.cell(
            row=row,
            column=_pcol(periods, year),
            value=f"={cell_ref(fs, '财务费用', year)}",
        )
    for year in fcst:
        write_input(ws, row, _pcol(periods, year), cost["fin_exp"][year])
    row += 1
    ws.cell(row=row, column=1, value="营业外收入")
    for year in hist:
        ws.cell(
            row=row,
            column=_pcol(periods, year),
            value=f"={cell_ref(wb['历史财务数据'], '营业外收入', year)}",
        )
    for year in fcst:
        write_input(ws, row, _pcol(periods, year), cost["nonop_inc"][year])
    row += 1
    ws.cell(row=row, column=1, value="营业外支出")
    for year in hist:
        ws.cell(
            row=row,
            column=_pcol(periods, year),
            value=f"={cell_ref(wb['历史财务数据'], '营业外支出', year)}",
        )
    for year in fcst:
        write_input(ws, row, _pcol(periods, year), cost["nonop_exp"][year])
    row += 1
    ws.cell(row=row, column=1, value="少数股东比率")
    for year in hist:
        minority = cell_ref(fs, "少数股东损益", year)
        parent = cell_ref(fs, "归母净利润", year)
        ws.cell(
            row=row,
            column=_pcol(periods, year),
            value=f"={minority}/({parent}+{minority})",
        )
    for year in fcst:
        write_input(ws, row, _pcol(periods, year), cost["minority"][year])
    row += 1
    ws.cell(row=row, column=1, value="其他经营净收益")
    for year in hist:
        ebit = cell_ref(fs, "息税前利润", year)
        gp = cell_ref(fs, "毛利", year)
        s = cell_ref(fs, "销售费用", year)
        a = cell_ref(fs, "管理费用", year)
        d = cell_ref(fs, "研发费用", year)
        ws.cell(
            row=row,
            column=_pcol(periods, year),
            value=f"={ebit}-({gp}-{s}-{a}-{d})",
        )
    for year in fcst:
        write_input(ws, row, _pcol(periods, year), cost["other_op"][year])
    row += 1

    write_section_row(ws, row, ["成本毛利区"], last)
    row += 1
    ws.cell(row=row, column=1, value="营业收入")
    for year in periods:
        ws.cell(row=row, column=_pcol(periods, year), value=f"={cell_ref(wb['收入预测'], '营业收入', year)}")
    row += 1
    ws.cell(row=row, column=1, value="毛利")
    for year in periods:
        rev = cell_ref(ws, "营业收入", year, same_sheet=True)
        gm = cell_ref(ws, "合并毛利率", year, same_sheet=True)
        ws.cell(row=row, column=_pcol(periods, year), value=f"={rev}*{gm}")
    row += 1
    ws.cell(row=row, column=1, value="营业成本")
    for year in periods:
        rev = cell_ref(ws, "营业收入", year, same_sheet=True)
        gp = cell_ref(ws, "毛利", year, same_sheet=True)
        ws.cell(row=row, column=_pcol(periods, year), value=f"={rev}-{gp}")
    row += 1

    write_section_row(ws, row, ["费用区"], last)
    row += 1
    for label, ratio in (
        ("销售费用", "销售费用率"),
        ("管理费用", "管理费用率"),
        ("研发费用", "研发费用率"),
    ):
        ws.cell(row=row, column=1, value=label)
        for year in periods:
            rev = cell_ref(ws, "营业收入", year, same_sheet=True)
            rt = cell_ref(ws, ratio, year, same_sheet=True)
            ws.cell(row=row, column=_pcol(periods, year), value=f"={rev}*{rt}")
        row += 1
    ws.cell(row=row, column=1, value="总费用")
    for year in periods:
        parts = "+".join(
            cell_ref(ws, fee, year, same_sheet=True)
            for fee in ("销售费用", "管理费用", "研发费用")
        )
        ws.cell(row=row, column=_pcol(periods, year), value=f"={parts}")

    year_end = last - 1
    mark_subtotal(ws, _row(ws, "毛利"), 1, year_end, fill=True)
    mark_bold_row(ws, _row(ws, "营业成本"), 1, year_end)
    mark_subtotal(ws, _row(ws, "总费用"), 1, year_end, fill=True, line=True)
    write_sheet_title(ws, facts["company"], last)
    set_col_widths(ws, last, first=32, other=13)
    ws.column_dimensions[get_column_letter(last)].width = 36
    ratio_rows = set()
    for r in range(3, ws.max_row + 1):
        lab = str(ws.cell(row=r, column=1).value or "")
        if "率" in lab:
            ratio_rows.add(r)
    apply_number_formats(ws, ratio_rows, last, last)
    paint_stub_column(ws, 1, NEAR)
    finish_sheet(ws)


def _pnl(wb: Workbook, spec: dict) -> None:
    facts = spec["facts"]
    hist = facts["hist_periods"]
    fcst = facts["forecast_periods"]
    periods = hist + fcst
    ws = wb["利润表预测"]
    last = 2 + len(periods)
    row = write_first_section_header(ws, [*periods, "备注"])
    labels = [
        "营业收入",
        "营业收入同比",
        "营业成本",
        "合并毛利率",
        "毛利",
        "销售费用",
        "管理费用",
        "研发费用",
        "息税前利润",
        "财务费用",
        "营业外收入",
        "营业外支出",
        "税前利润",
        "所得税费用",
        "少数股东损益",
        "归母净利润",
        "归母净利润同比",
        "EPS",
    ]
    derived = {"营业收入同比", "合并毛利率", "归母净利润同比"}
    for label in labels:
        ws.cell(row=row, column=1, value=label)
        if label not in derived:
            for year in hist:
                src = fs_label(label) if label == "EPS" else label
                ws.cell(
                    row=row,
                    column=_pcol(periods, year),
                    value=f"={cell_ref(wb['历史财务数据'], src, year)}",
                )
        row += 1

    def col(year: str) -> int:
        return _pcol(periods, year)

    for year in periods:
        ws.cell(
            row=_row(ws, "合并毛利率"),
            column=col(year),
            value=f"={cell_ref(wb['运营成本预测'], '合并毛利率', year)}",
        )
    for label, source in (
        ("营业收入同比", "营业收入"),
        ("归母净利润同比", "归母净利润"),
    ):
        r = _row(ws, label)
        for i, year in enumerate(periods):
            if i == 0:
                _write_dash(ws, r, col(year))
                continue
            cur = cell_ref(ws, source, year, same_sheet=True)
            pri = cell_ref(ws, source, periods[i - 1], same_sheet=True)
            ws.cell(row=r, column=col(year), value=f"={cur}/{pri}-1")

    for year in fcst:
        ws.cell(row=_row(ws, "营业收入"), column=col(year), value=f"={cell_ref(wb['收入预测'], '营业收入', year)}")
        ws.cell(row=_row(ws, "营业成本"), column=col(year), value=f"={cell_ref(wb['运营成本预测'], '营业成本', year)}")
        ws.cell(row=_row(ws, "毛利"), column=col(year), value=f"={cell_ref(wb['运营成本预测'], '毛利', year)}")
        for fee in ("销售费用", "管理费用", "研发费用"):
            ws.cell(row=_row(ws, fee), column=col(year), value=f"={cell_ref(wb['运营成本预测'], fee, year)}")
        gp = cell_ref(ws, "毛利", year, same_sheet=True)
        s = cell_ref(ws, "销售费用", year, same_sheet=True)
        a = cell_ref(ws, "管理费用", year, same_sheet=True)
        d = cell_ref(ws, "研发费用", year, same_sheet=True)
        oth = cell_ref(wb["运营成本预测"], "其他经营净收益", year)
        ws.cell(row=_row(ws, "息税前利润"), column=col(year), value=f"={gp}-{s}-{a}-{d}+{oth}")
        ws.cell(row=_row(ws, "财务费用"), column=col(year), value=f"={cell_ref(wb['运营成本预测'], '财务费用假设', year)}")
        ws.cell(row=_row(ws, "营业外收入"), column=col(year), value=f"={cell_ref(wb['运营成本预测'], '营业外收入', year)}")
        ws.cell(row=_row(ws, "营业外支出"), column=col(year), value=f"={cell_ref(wb['运营成本预测'], '营业外支出', year)}")
        ebit = cell_ref(ws, "息税前利润", year, same_sheet=True)
        fin = cell_ref(ws, "财务费用", year, same_sheet=True)
        ni = cell_ref(ws, "营业外收入", year, same_sheet=True)
        ne = cell_ref(ws, "营业外支出", year, same_sheet=True)
        ws.cell(row=_row(ws, "税前利润"), column=col(year), value=f"={ebit}-{fin}+{ni}-{ne}")
        pre = cell_ref(ws, "税前利润", year, same_sheet=True)
        taxr = cell_ref(wb["运营成本预测"], "有效税率", year)
        ws.cell(row=_row(ws, "所得税费用"), column=col(year), value=f"={pre}*{taxr}")
        tax = cell_ref(ws, "所得税费用", year, same_sheet=True)
        mino_ratio = cell_ref(wb["运营成本预测"], "少数股东比率", year)
        ws.cell(
            row=_row(ws, "少数股东损益"),
            column=col(year),
            value=f"=({pre}-{tax})*{mino_ratio}",
        )
        mino = cell_ref(ws, "少数股东损益", year, same_sheet=True)
        ws.cell(row=_row(ws, "归母净利润"), column=col(year), value=f"={pre}-{tax}-{mino}")
        ni2 = cell_ref(ws, "归母净利润", year, same_sheet=True)
        shares = f"'历史财务数据'!B{_row(wb['历史财务数据'], fs_label('当前总股本_百万股'))}"
        ws.cell(row=_row(ws, "EPS"), column=col(year), value=f"={ni2}*100/{shares}")

    write_note(
        ws,
        _row(ws, "税前利润"),
        last,
        "历史列沿用历史财务数据口径。预测列：息税前利润＝毛利－销售费用－管理费用－研发费用＋其他经营净收益；"
        "税前利润＝息税前利润－财务费用＋营业外收入－营业外支出。"
        "历史披露或接口的息税前利润口径可能与上述模型定义不同。",
    )
    for label in ("营业收入同比", "合并毛利率", "归母净利润同比"):
        ws.cell(row=_row(ws, label), column=1).alignment = Alignment(indent=1, vertical="center")
    year_end = last - 1
    mark_bold_row(ws, _row(ws, "营业收入"), 1, year_end)
    mark_bold_row(ws, _row(ws, "毛利"), 1, year_end)
    mark_bold_row(ws, _row(ws, "息税前利润"), 1, year_end)
    mark_subtotal(ws, _row(ws, "归母净利润"), 1, year_end, fill=True)
    mark_subtotal(ws, _row(ws, "EPS"), 1, year_end, fill=True)
    write_sheet_title(ws, facts["company"], last)
    set_col_widths(ws, last, first=32, other=13)
    ws.column_dimensions[get_column_letter(last)].width = 12
    apply_number_formats(
        ws,
        {_row(ws, "营业收入同比"), _row(ws, "合并毛利率"), _row(ws, "归母净利润同比")},
        last,
        last,
    )
    paint_stub_column(ws, 1, NEAR)
    finish_sheet(ws)


def _valuation(wb: Workbook, spec: dict) -> None:
    facts = spec["facts"]
    cons = spec["consensus"]
    comps = spec["comps"]
    fcst = facts["forecast_periods"]
    ws = wb["估值预测"]
    last = 6
    note_col = last
    row = write_first_section_header(ws, ["发布日期", *[f"{y}总营收" for y in fcst]])
    first_inst = row
    for inst in cons["institutions"]:
        ws.cell(row=row, column=1, value=inst["name"])
        ws.cell(row=row, column=2, value=inst["published"])
        ws.cell(row=row, column=2).number_format = "yyyy-mm-dd"
        for i, val in enumerate(inst["revenue"]):
            ws.cell(row=row, column=3 + i, value=val)
        row += 1
    median_row = row
    ws.cell(row=row, column=1, value="一致预期_中位数")
    last_inst = row - 1
    for i in range(3):
        col = get_column_letter(3 + i)
        ws.cell(row=row, column=3 + i, value=consensus_median_formula(first_inst, last_inst, col))
    row += 1
    ws.cell(row=row, column=1, value="本模型预测")
    for i, year in enumerate(fcst):
        ws.cell(row=row, column=3 + i, value=f"={cell_ref(wb['收入预测'], '营业收入', year)}")
    row += 2

    y1 = fcst[0]
    write_section_row(ws, row, ["核心同业池", f"{y1} PE", "PE_TTM", "总市值", "单位", "备注"], last)
    row += 1
    core_start = row
    for name in comps["core"]:
        ws.cell(row=row, column=1, value=name["name"])
        ws.cell(row=row, column=2, value=name["pe_y1"])
        ws.cell(row=row, column=3, value=name["pe_ttm"])
        ws.cell(row=row, column=4, value=name["mcap"])
        ws.cell(row=row, column=5, value=_mcap_unit(name.get("currency")))
        write_note(ws, row, note_col, name.get("note") or "")
        row += 1
    core_end = row - 1
    ws.cell(row=row, column=1, value="核心池均值")
    ws.cell(row=row, column=2, value=f"=AVERAGE(B{core_start}:B{core_end})")
    ws.cell(row=row, column=3, value=f"=AVERAGE(C{core_start}:C{core_end})")
    row += 1
    ws.cell(row=row, column=1, value="核心池中位数")
    ws.cell(row=row, column=2, value=f"=MEDIAN(B{core_start}:B{core_end})")
    row += 1
    mean_pe = f"B{_row(ws, '核心池均值')}"
    eps = cell_ref(wb["利润表预测"], "EPS", y1)
    price = "历史财务数据!B" + str(_row(wb["历史财务数据"], fs_label("当前股价")))
    write_section_row(
        ws,
        row,
        ["综合估值预测", "PE调整系数", "目标PE", "目标价", "较现价空间"],
        last,
    )
    row += 1
    neutral = row + 1
    scenario_rows = []
    for label, coef in (
        ("悲观", f"=B{neutral}*0.85"),
        ("中性", comps["pe_adjust"]),
        ("乐观", f"=B{neutral}*1.15"),
    ):
        scenario_rows.append(row)
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=coef)
        if isinstance(coef, (int, float)) and not isinstance(coef, bool):
            mark_input(ws, row, 2)
        ws.cell(row=row, column=3, value=f"=B{row}*{mean_pe}")
        ws.cell(row=row, column=4, value=f"=C{row}*{eps}")
        ws.cell(row=row, column=5, value=f"=D{row}/{price}-1")
        row += 1
    ws.cell(row=row, column=1, value="目标PE")
    ws.cell(row=row, column=2, value=f"=C{neutral}")
    row += 1
    ws.cell(row=row, column=1, value="合理价值区间")
    bear, _neutral_row, bull = scenario_rows
    ws.cell(
        row=row,
        column=2,
        value=f'=TEXT(D{bear},"{NUM_FMT}")&" - "&TEXT(D{bull},"{NUM_FMT}")',
    )
    row += 1
    ws.cell(row=row, column=1, value="中枢目标价")
    ws.cell(row=row, column=2, value=f"=D{neutral}")
    row += 1
    ws.cell(row=row, column=1, value="较现价空间")
    space_row = row
    ws.cell(row=row, column=2, value=f"=E{neutral}")
    row += 1
    ws.cell(row=row, column=1, value="投资评级")
    ws.cell(row=row, column=2, value=f'=IF(B{space_row}>=0.15,"买入","观察")')

    _ = median_row
    mark_bold_row(ws, _row(ws, "核心池均值"), 1, 3)
    mark_bold_row(ws, _row(ws, "核心池中位数"), 1, 2)
    for scenario in scenario_rows:
        mark_bold_row(ws, scenario, 1, 5)
    for label in ("合理价值区间", "较现价空间", "中枢目标价"):
        result_row = _row(ws, label)
        mark_result_cell(ws, result_row, 1)
        mark_result_cell(ws, result_row, 2)
    write_sheet_title(ws, facts["company"], last)
    set_col_widths(ws, last, first=24, other=14)
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["E"].width = 14
    ws.column_dimensions["F"].width = 36
    apply_number_formats(ws, {_row(ws, "较现价空间")}, last, note_col)
    peer_end = _row(ws, "核心池中位数")
    for peer_row in range(core_start, peer_end + 1):
        for col in (2, 3):
            cell = ws.cell(row=peer_row, column=col)
            if cell.value is not None:
                cell.number_format = MULTIPLE_FMT
        mcap_cell = ws.cell(row=peer_row, column=4)
        if mcap_cell.value is not None:
            mcap_cell.number_format = AMT_FMT
    for scenario in scenario_rows:
        ws.cell(row=scenario, column=2).number_format = AMT_FMT
        ws.cell(row=scenario, column=3).number_format = MULTIPLE_FMT
        ws.cell(row=scenario, column=4).number_format = PX_FMT
        ws.cell(row=scenario, column=5).number_format = SIGNED_PCT_FMT
    paint_stub_column(ws, 1, NEAR)
    finish_sheet(ws)


def _mcap_unit(currency: object) -> str:
    code = str(currency or "CNY").strip().upper() or "CNY"
    return {"CNY": "亿元", "HKD": "亿港元", "USD": "亿美元"}.get(code, code)


def _summary(wb: Workbook, spec: dict) -> None:
    from valuation.research_dossier.schema import DISCLAIMER, point_text, point_title

    facts = spec["facts"]
    notes = spec["summary_notes"]
    info = spec.get("company_info") or {}
    hist = facts["hist_periods"]
    fcst = facts["forecast_periods"]
    last_a = hist[-1]
    periods_show = [last_a, *fcst]
    periods_all = hist + fcst
    pnl = wb["利润表预测"]
    cost = wb["运营成本预测"]
    val = wb["估值预测"]
    fs = wb["历史财务数据"]
    ws = wb["总结"]
    _set_summary_widths(ws)
    row = _write_company_intro(ws, info, facts)
    write_spacer(ws, row)
    row += 1

    overview = [
        ("当前股价", f"=历史财务数据!B{_row(fs, fs_label('当前股价'))}"),
        ("中枢目标价", f"=估值预测!B{_row(val, '中枢目标价')}"),
        ("较现价空间", f"=估值预测!B{_row(val, '较现价空间')}"),
        ("合理价值区间", f"=估值预测!B{_row(val, '合理价值区间')}"),
        ("目标PE", f"=估值预测!B{_row(val, '目标PE')}"),
        ("EPS_预测首年", f"={cell_ref(pnl, 'EPS', fcst[0])}"),
        ("EPS_最近实际", f"={cell_ref(pnl, 'EPS', last_a)}"),
        ("投资评级", f"=估值预测!B{_row(val, '投资评级')}"),
    ]
    market = _market_pairs(info, fs)
    write_section_row(ws, row, ["估值概览"], _SUM_LEFT_END, start_col=_SUM_LEFT)
    write_section_row(ws, row, ["当前行情"], _SUM_RIGHT_END, start_col=_SUM_RIGHT)
    left_end, large = _write_value_pairs(ws, row + 1, overview, _SUM_LEFT, ROW_LABEL_VAL)
    right_end, _ = _write_value_pairs(ws, row + 1, market, _SUM_RIGHT, ROW_LABEL_MARKET)
    row = _after_pair(ws, left_end, right_end)

    left_lines = _forecast_lines(pnl, periods_show, periods_all)
    right_lines = [
        (label, [f"={cell_ref(cost, label, year)}" for year in periods_show])
        for label in ("合并毛利率", "销售费用率", "管理费用率", "研发费用率", "有效税率")
    ]
    write_section_row(
        ws, row, ["财务预测", *periods_show], _SUM_LEFT_END, start_col=_SUM_LEFT
    )
    write_section_row(
        ws, row, ["关键经营假设", *periods_show], _SUM_RIGHT_END, start_col=_SUM_RIGHT
    )
    left_end, left_rows = _write_year_block(ws, row + 1, left_lines, _SUM_LEFT, ROW_LABEL_VAL)
    right_end, _right_rows = _write_year_block(ws, row + 1, right_lines, _SUM_RIGHT, ROW_LABEL_MARKET)
    for label in ("营业收入", "毛利", "息税前利润"):
        mark_bold_row(ws, left_rows[label], _SUM_LEFT, _SUM_LEFT_END)
    for label in ("归母净利润", "EPS"):
        mark_subtotal(ws, left_rows[label], _SUM_LEFT, _SUM_LEFT_END, fill=True)
    row = _after_pair(ws, left_end, right_end)

    thesis = list(notes.get("thesis") or [])
    outlook = list(notes.get("outlook") or [])
    write_section_row(ws, row, ["投资逻辑"], _SUM_LEFT_END, start_col=_SUM_LEFT)
    write_section_row(ws, row, ["未来展望"], _SUM_RIGHT_END, start_col=_SUM_RIGHT)
    row = _write_paired_points(ws, row + 1, thesis, outlook, point_title, point_text)
    write_spacer(ws, row)
    row += 1

    write_section_row(ws, row, ["主要风险"], _SUM_LAST, start_col=_SUM_LEFT)
    row += 1
    for index, point in enumerate(notes.get("risks") or [], 1):
        height = _point_height(point, 96, point_title, point_text)
        _place_point(ws, row, index, point, _SUM_LEFT, _SUM_LAST, point_title, point_text)
        ws.row_dimensions[row].height = height
        row += 1
    cell = ws.cell(row=row, column=_SUM_LEFT, value=DISCLAIMER)
    ws.merge_cells(start_row=row, start_column=_SUM_LEFT, end_row=row, end_column=_SUM_LAST)
    apply_structure_style(ws, [cell], "footnote")
    cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[row].height = _wrapped_height(DISCLAIMER, width=110, min_h=24, max_h=80)

    apply_number_formats(ws, set(), _SUM_LAST, None)
    finish_sheet(ws)
    for item_row, item_col in large:
        _enlarge_result(ws.cell(row=item_row, column=item_col))


_SUM_LEFT = 1
_SUM_LEFT_END = 5
_SUM_RIGHT = 7
_SUM_RIGHT_END = 11
_SUM_LAST = 11
_LEGEND_ROW = 2
_LEGEND_LABEL_COL = 7
_LEGEND_TEXT_COL = 8
_OVERVIEW_RESULTS = {"中枢目标价", "合理价值区间", "较现价空间"}
_OVERVIEW_LARGE = {"中枢目标价", "较现价空间"}
_FS_LINE_LABELS = {"资产总计", "总负债", "负债和股东权益总计"}
_FS_FILL_LABELS = {"归母净利润", "净利润"}


def _set_summary_widths(ws) -> None:
    widths = (28, 12, 12, 12, 12, 3, 20, 10, 10, 10, 10)
    for index, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(index)].width = width


def _after_pair(ws, left_end: int, right_end: int) -> int:
    row = max(left_end, right_end)
    write_spacer(ws, row)
    return row + 1


def _company_heading(facts: dict, info: dict) -> str:
    profile = (info or {}).get("profile") or {}
    name = str(profile.get("short_name") or facts.get("company") or "")
    code = str(profile.get("ticker") or facts.get("ticker") or profile.get("full_code") or "")
    if name and code:
        return f"{name}（{code}）"
    return name or code


def _wrapped_height(text: str, *, width: float, min_h: float, max_h: float = 96) -> float:
    chars = max(8, int(width / 2.1))
    lines = 0
    for para in str(text or "").splitlines() or [""]:
        lines += max(1, -(-max(len(para), 1) // chars))
    return min(max_h, max(min_h, 6 + lines * 16))


def _write_company_intro(ws, info: dict, facts: dict) -> int:
    write_page_title(ws, _company_heading(facts, info), _SUM_LAST, start_col=_SUM_LEFT)
    row = 2
    profile = [(label, value) for label, value in company_profile_rows(info) if label != "A股代码"]
    business = [value for label, value in profile if label == "主营业务"]
    _write_color_legend(ws, _LEGEND_ROW)
    pending: tuple[str, str] | None = None
    for label, value in profile:
        if label == "主营业务":
            continue
        if len(value) > 18:
            if pending is not None:
                row = _write_profile_pair(ws, row, pending, None)
                pending = None
            _write_profile_span(ws, row, label, value)
            row += 1
            continue
        if pending is None:
            pending = (label, value)
            continue
        if _intro_value_end(row) == _SUM_LEFT_END:
            row = _write_profile_pair(ws, row, pending, None)
            pending = (label, value)
            continue
        row = _write_profile_pair(ws, row, pending, (label, value))
        pending = None
    if pending is not None:
        row = _write_profile_pair(ws, row, pending, None)
    for value in business:
        _write_profile_span(ws, row, "主营业务", value)
        row += 1
    return max(row, _LEGEND_ROW + len(_COLOR_LEGEND))


def _intro_value_end(row: int) -> int:
    """颜色说明占右侧四行时，公司字段停在左栏。"""
    if _LEGEND_ROW <= row < _LEGEND_ROW + len(_COLOR_LEGEND):
        return _SUM_LEFT_END
    return _SUM_LAST


def _write_profile_span(ws, row: int, label: str, value: str) -> None:
    write_field_label(ws, row, _SUM_LEFT, label)
    cell = ws.cell(row=row, column=_SUM_LEFT + 1, value=value)
    cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.merge_cells(
        start_row=row,
        start_column=_SUM_LEFT + 1,
        end_row=row,
        end_column=_intro_value_end(row),
    )
    ws.row_dimensions[row].height = _wrapped_height(value, width=70, min_h=22, max_h=72)


def _write_profile_pair(ws, row: int, left: tuple[str, str], right: tuple[str, str] | None) -> int:
    write_field_label(ws, row, _SUM_LEFT, left[0])
    left_value = ws.cell(row=row, column=_SUM_LEFT + 1, value=left[1])
    left_value.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    if right is None or _intro_value_end(row) == _SUM_LEFT_END:
        ws.merge_cells(
            start_row=row,
            start_column=_SUM_LEFT + 1,
            end_row=row,
            end_column=_intro_value_end(row),
        )
    else:
        ws.merge_cells(
            start_row=row,
            start_column=_SUM_LEFT + 1,
            end_row=row,
            end_column=_SUM_LEFT_END,
        )
        write_field_label(ws, row, _SUM_RIGHT, right[0])
        right_value = ws.cell(row=row, column=_SUM_RIGHT + 1, value=right[1])
        right_value.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.merge_cells(
            start_row=row,
            start_column=_SUM_RIGHT + 1,
            end_row=row,
            end_column=_SUM_LAST,
        )
    ws.row_dimensions[row].height = 20
    return row + 1


_COLOR_LEGEND = (
    (INPUT_BLUE, "蓝字", "手动输入"),
    (FORMULA_BLACK, "黑字", "本表公式"),
    (CROSS_GREEN, "深蓝字", "跨表引用"),
    (EXTERNAL_RED, "红字", "负数"),
)


def _write_color_legend(ws, row: int) -> None:
    """公司介绍右侧，四行两列说明数值颜色。"""
    muted = InlineFont(rFont=TEXT_FONT, sz=10.5, color=MUTED)
    for offset, (color, name, meaning) in enumerate(_COLOR_LEGEND):
        here = row + offset
        name_cell = ws.cell(row=here, column=_LEGEND_LABEL_COL)
        name_cell.value = CellRichText(TextBlock(InlineFont(rFont=TEXT_FONT, sz=10.5, color=color, b=True), name))
        name_cell.alignment = Alignment(horizontal="left", vertical="center")
        text_cell = ws.cell(row=here, column=_LEGEND_TEXT_COL)
        text_cell.value = CellRichText(TextBlock(muted, meaning))
        text_cell.alignment = Alignment(horizontal="left", vertical="center")
        ws.merge_cells(
            start_row=here,
            start_column=_LEGEND_TEXT_COL,
            end_row=here,
            end_column=_SUM_LAST,
        )
        ws.row_dimensions[here].height = 20


def _write_value_pairs(
    ws,
    row: int,
    pairs: list[tuple[str, object]],
    origin: int,
    label_fill: str,
) -> tuple[int, list[tuple[int, int]]]:
    large: list[tuple[int, int]] = []
    index = 0
    while index < len(pairs):
        for slot, (label, value) in enumerate(pairs[index : index + 2]):
            if slot == 0:
                label_col = origin
                label_end = origin
                value_col = origin + 1
            else:
                # 第二列标签横跨两个窄列，避免「流通市值（亿元）」被截断。
                label_col = origin + 2
                label_end = origin + 3
                value_col = origin + 4
            write_row_label(ws, row, label_col, label, label_fill)
            if label_end > label_col:
                span_fill = PatternFill("solid", fgColor=label_fill)
                for col in range(label_col + 1, label_end + 1):
                    ws.cell(row=row, column=col).fill = span_fill
                ws.merge_cells(
                    start_row=row,
                    start_column=label_col,
                    end_row=row,
                    end_column=label_end,
                )
            ws.cell(row=row, column=value_col, value=value)
            if label in _OVERVIEW_RESULTS:
                mark_result_cell(ws, row, label_col)
                mark_result_cell(ws, row, value_col)
            if label in _OVERVIEW_LARGE:
                large.append((row, value_col))
        ws.row_dimensions[row].height = 20
        row += 1
        index += 2
    return row, large


def _market_pairs(info: dict, fs) -> list[tuple[str, object]]:
    shown: list[tuple[str, object]] = []
    for label, value, _is_pct in company_market_rows(info):
        if label == "当前股价（元）":
            continue
        if label == "总市值（亿元）":
            value = f"=历史财务数据!B{_row(fs, fs_label('总市值_亿'))}"
        elif label == "流通市值（亿元）":
            value = f"=历史财务数据!B{_row(fs, fs_label('流通市值_亿'))}"
        shown.append((label, value))
    return shown


def _forecast_lines(pnl, periods_show: list[str], periods_all: list[str]) -> list[tuple[str, list[str]]]:
    lines = [
        (label, [f"={cell_ref(pnl, label, year)}" for year in periods_show])
        for label in ("营业收入", "毛利", "息税前利润", "归母净利润", "EPS")
    ]
    growth = []
    for year in periods_show:
        prev = periods_all[periods_all.index(year) - 1]
        cur = cell_ref(pnl, "营业收入", year)
        pri = cell_ref(pnl, "营业收入", prev)
        growth.append(f"={cur}/{pri}-1")
    lines.append(("营业收入同比增速", growth))
    for label in ("息税前利润", "归母净利润"):
        formulas = []
        for year in periods_show:
            num = cell_ref(pnl, label, year)
            rev = cell_ref(pnl, "营业收入", year)
            formulas.append(f"={num}/{rev}")
        rate = "息税前利润率" if label == "息税前利润" else "归母净利润率"
        lines.append((rate, formulas))
    return lines


def _write_year_block(
    ws,
    row: int,
    lines: list[tuple[str, list[str]]],
    label_col: int,
    label_fill: str,
) -> tuple[int, dict[str, int]]:
    rows: dict[str, int] = {}
    for label, formulas in lines:
        write_row_label(ws, row, label_col, label, label_fill)
        for offset, formula in enumerate(formulas):
            ws.cell(row=row, column=label_col + 1 + offset, value=formula)
        rows[label] = row
        row += 1
    return row, rows


def _point_height(point, width: float, title_of, text_of) -> float:
    title = title_of(point)
    text = text_of(point)
    return max(
        _wrapped_height(f"1. {title}", width=28, min_h=22, max_h=80),
        _wrapped_height(text, width=width, min_h=36, max_h=320),
    )


def _place_point(ws, row: int, index: int, point, label_col: int, end_col: int, title_of, text_of) -> None:
    title = f"{index}. {title_of(point)}".strip()
    write_row_label(ws, row, label_col, title, ROW_LABEL_NOTE)
    body_col = label_col + 1
    write_note(ws, row, body_col, text_of(point))
    ws.cell(row=row, column=body_col).alignment = Alignment(wrap_text=True, vertical="center")
    if end_col > body_col:
        ws.merge_cells(start_row=row, start_column=body_col, end_row=row, end_column=end_col)


def _write_paired_points(ws, row: int, left: list, right: list, title_of, text_of) -> int:
    count = max(len(left), len(right))
    for index in range(count):
        height = 22.0
        if index < len(left):
            height = max(height, _point_height(left[index], 48, title_of, text_of))
            _place_point(ws, row, index + 1, left[index], _SUM_LEFT, _SUM_LEFT_END, title_of, text_of)
        if index < len(right):
            height = max(height, _point_height(right[index], 40, title_of, text_of))
            _place_point(ws, row, index + 1, right[index], _SUM_RIGHT, _SUM_RIGHT_END, title_of, text_of)
        ws.row_dimensions[row].height = height
        row += 1
    return row


def _enlarge_result(cell) -> None:
    cell.font = Font(
        name="Arial",
        size=12,
        color=cell.font.color,
        bold=True,
    )


def _bold_block(ws, row: int, start: int, width: int) -> None:
    label = str(ws.cell(row=row, column=start).value or "")
    line = label in _FS_LINE_LABELS
    fill = label in _FS_FILL_LABELS
    if line or fill:
        mark_subtotal(ws, row, start, start + width - 1, line=line, fill=fill)
        return
    mark_bold_row(ws, row, start, start + width - 1)


def _write_fs_line(
    ws,
    row: int,
    label_col: int,
    label: str,
    hist: list[str],
    series,
    *,
    bold: bool = False,
    formula: list[str] | None = None,
) -> int:
    ws.cell(row=row, column=label_col, value=label)
    for i, _year in enumerate(hist):
        col = label_col + 1 + i
        if formula and i < len(formula) and formula[i] is not None:
            ws.cell(row=row, column=col, value=formula[i])
        elif series and i < len(series) and series[i] is not None:
            ws.cell(row=row, column=col, value=series[i])
    if bold:
        _bold_block(ws, row, label_col, 1 + len(hist))
    return row + 1


def _income_formula(key: str, hist: list[str], ws) -> list[str] | None:
    if key == "毛利":
        try:
            find_row_by_label(ws, "营业收入")
            find_row_by_label(ws, "营业成本")
        except ValueError:
            return None
        return [
            f"={cell_ref(ws, '营业收入', year, same_sheet=True)}-{cell_ref(ws, '营业成本', year, same_sheet=True)}"
            for year in hist
        ]
    if key == "历史加权平均股本_百万股":
        ni = fs_label("归母净利润")
        eps = fs_label("EPS")
        return [
            f"={cell_ref(ws, ni, year, same_sheet=True)}*100/{cell_ref(ws, eps, year, same_sheet=True)}"
            for year in hist
        ]
    return None


def _wc_formula(hist: list[str], ws) -> list[str] | None:
    try:
        find_row_by_label(ws, "流动资产合计")
        find_row_by_label(ws, "流动负债合计")
    except ValueError:
        return None
    return [
        f"={cell_ref(ws, '流动资产合计', year, same_sheet=True)}-{cell_ref(ws, '流动负债合计', year, same_sheet=True)}"
        for year in hist
    ]


def _write_market(ws, row: int, market: dict, as_of) -> int:
    written: dict[str, int] = {}
    for key in MARKET_ROWS:
        label = fs_label(key)
        if key == "总市值_亿":
            price_row = written.get("当前股价")
            share_row = written.get("当前总股本_百万股")
            if price_row and share_row:
                ws.cell(row=row, column=1, value=label)
                ws.cell(row=row, column=2, value=f"=B{price_row}*B{share_row}/100")
                written[key] = row
                row += 1
            continue
        if key == "流通市值_亿":
            price_row = written.get("当前股价")
            float_row = written.get("流通股本_百万股")
            if price_row and float_row:
                ws.cell(row=row, column=1, value=label)
                ws.cell(row=row, column=2, value=f"=B{price_row}*B{float_row}/100")
                written[key] = row
                row += 1
            continue
        value = market.get(key)
        if key == "快照日期" and not value:
            value = str(as_of)[:10] if as_of else None
        if value is None or value == "":
            continue
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=value)
        written[key] = row
        row += 1
    return row


def _format_hist(ws, last: int) -> None:
    for r in range(3, ws.max_row + 1):
        for label_col in (FS_LEFT_COL, FS_RIGHT_COL):
            label = str(ws.cell(row=r, column=label_col).value or "")
            if not label:
                continue
            for col in range(label_col + 1, min(label_col + 4, last + 1)):
                cell = ws.cell(row=r, column=col)
                if cell.value is None:
                    continue
                if isinstance(cell.value, str) and not str(cell.value).startswith("="):
                    continue
                if cell.number_format == "yyyy-mm-dd":
                    continue
                cell.number_format = choose_number_format(label)


def _col(ws, header: str) -> int:
    return find_col_by_header(ws, header)


def _write_dash(ws, row: int, column: int) -> None:
    cell = ws.cell(row=row, column=column, value="-")
    cell.data_type = "s"
    cell.alignment = Alignment(horizontal="right", vertical="center")


def _row(ws, label: str) -> int:
    return find_row_by_label(ws, label)


def _pcol(periods: list[str], year: str) -> int:
    return 2 + periods.index(year)


def consensus_median_formula(first_inst: int, last_inst: int, col: str) -> str | None:
    if last_inst < first_inst:
        return None
    return f"=MEDIAN({col}{first_inst}:{col}{last_inst})"
