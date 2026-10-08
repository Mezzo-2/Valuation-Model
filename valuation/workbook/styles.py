"""工作簿版式：深蓝页头、浅灰蓝模块、两位小数、负数红字。"""

from __future__ import annotations

import os
import re
from datetime import date, datetime

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

# 页头最深，一级分区次深，分部中灰，行标题最浅
PAGE = "0E2744"
SECTION = "2A4A70"
GROUP = "C5D0DE"
NEAR = "F4F7FB"
HEADER = "D3DEEA"
PALE = "EAF0F7"
WHITE = "FFFFFF"
INK = "152033"
MUTED = "647084"
HAIRLINE = "D0D8E4"
ON_SECTION = "0E2744"

INPUT_BLUE = "0000FF"
FORMULA_BLACK = "000000"
CROSS_GREEN = "1D4E89"
EXTERNAL_RED = "FF0000"

YAHEI = "微软雅黑"
PINGFANG = "PingFang SC"
NUMBER_FONT = "Arial"

_YAHEI_PATHS = (
    "/Library/Fonts/Microsoft YaHei.ttf",
    "/Library/Fonts/msyh.ttc",
    "/Library/Fonts/msyh.ttf",
    os.path.expanduser("~/Library/Fonts/Microsoft YaHei.ttf"),
    os.path.expanduser("~/Library/Fonts/msyh.ttc"),
)


def _pick_text_font() -> str:
    if any(os.path.exists(path) for path in _YAHEI_PATHS):
        return YAHEI
    return PINGFANG


TEXT_FONT = _pick_text_font()

BODY_SIZE = 10.5
NUMBER_SIZE = 10
SECTION_SIZE = 11
TITLE_SIZE = 16
HEADER_ROW_HEIGHT = 22
BODY_ROW_HEIGHT = 18
TITLE_ROW_HEIGHT = 32
SPACER_ROW_HEIGHT = 12

# 单元格显示格式。NUM_FMT 只留给「合理价值区间」TEXT() 公式，保持原公式文本不变。
NUM_FMT = "#,##0.00"
AMT_FMT = '#,##0.00;[Red](#,##0.00);0.00;@'
PX_FMT = "0.00;[Red](0.00);0.00;@"
MULTIPLE_FMT = '0.00"x";[Red](0.00"x");0.00"x";@'
PCT_FMT = "0.00%;[Red](0.00%);0.00%;@"
SIGNED_PCT_FMT = "+0.00%;[Red]-0.00%;0.00%;@"
DATE_FMT = "yyyy-mm-dd"

ROW_LABEL_VAL = NEAR
ROW_LABEL_MARKET = NEAR
ROW_LABEL_NOTE = NEAR

SHEET_TITLE_SUFFIX = {
    "总结": "估值模型总结",
    "主营业务拆分": "主营业务拆分",
    "收入预测": "收入预测",
    "运营成本预测": "运营成本预测",
    "利润表预测": "利润表预测",
    "估值预测": "估值预测",
    "历史财务数据": "财务报表",
}

FIRST_SECTION_TITLE = {
    "收入预测": "收入驱动假设区",
    "运营成本预测": "成本费用假设区",
    "利润表预测": "利润表预测",
    "估值预测": "机构一致性预期",
    "历史财务数据": "利润表（亿元）",
}

_HIST_PERIOD = re.compile(r"^\d{4}(?:A|Q[1-4]A)$")
_FCST_PERIOD = re.compile(r"^\d{4}(?:E|Q[1-4]E)$")
_EXTERNAL_REF = re.compile(r"\[[^\]]+\]")

_STRUCTURE_FILLS = {PAGE, SECTION, GROUP, NEAR, HEADER}

_STYLES = {
    "page_title": {"fill": PAGE, "font": WHITE, "size": TITLE_SIZE, "bold": True, "italic": False, "name": TEXT_FONT},
    "section": {"fill": SECTION, "font": WHITE, "size": SECTION_SIZE, "bold": True, "italic": False, "name": TEXT_FONT},
    "group": {"fill": GROUP, "font": ON_SECTION, "size": BODY_SIZE, "bold": True, "italic": False, "name": TEXT_FONT},
    "col_header": {"fill": HEADER, "font": ON_SECTION, "size": BODY_SIZE, "bold": True, "italic": False, "name": TEXT_FONT},
    "col_header_hist": {"fill": NEAR, "font": ON_SECTION, "size": NUMBER_SIZE, "bold": True, "italic": False, "name": NUMBER_FONT},
    "col_header_fcst": {"fill": HEADER, "font": ON_SECTION, "size": NUMBER_SIZE, "bold": True, "italic": False, "name": NUMBER_FONT},
    "field_label": {"fill": NEAR, "font": ON_SECTION, "size": BODY_SIZE, "bold": False, "italic": False, "name": TEXT_FONT},
    "row_label": {"fill": NEAR, "font": ON_SECTION, "size": BODY_SIZE, "bold": False, "italic": False, "name": TEXT_FONT},
    "result": {"fill": PALE, "font": None},
    "footnote": {"fill": None, "font": MUTED, "size": BODY_SIZE, "bold": False, "italic": True, "name": TEXT_FONT},
}

_HEADER_ROWS: dict[int, set[int]] = {}
_INPUTS: dict[int, set[tuple[int, int]]] = {}


def reset_style_marks() -> None:
    _HEADER_ROWS.clear()
    _INPUTS.clear()


def mark_header(ws: Worksheet, row: int) -> None:
    _HEADER_ROWS.setdefault(id(ws), set()).add(row)


def header_rows(ws: Worksheet) -> set[int]:
    return set(_HEADER_ROWS.get(id(ws), ()))


def mark_input(ws: Worksheet, row: int, col: int) -> None:
    _INPUTS.setdefault(id(ws), set()).add((row, col))


def input_cells(ws: Worksheet) -> set[tuple[int, int]]:
    return set(_INPUTS.get(id(ws), ()))


def write_input(ws: Worksheet, row: int, col: int, value):
    cell = ws.cell(row=row, column=col, value=value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        mark_input(ws, row, col)
    return cell


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", fgColor=color)


def _font(style: dict) -> Font:
    return Font(
        name=style["name"],
        size=style["size"],
        color=style["font"],
        bold=style["bold"],
        italic=style["italic"],
    )


def _rgb(color) -> str:
    if color is None:
        return ""
    rgb = getattr(color, "rgb", None)
    if not isinstance(rgb, str):
        return ""
    return rgb[-6:].upper()


def _fill_rgb(cell) -> str:
    fill = cell.fill
    if fill is None or fill.fill_type != "solid":
        return ""
    return _rgb(fill.fgColor)


def _is_structure_fill(cell) -> bool:
    return _fill_rgb(cell) in _STRUCTURE_FILLS


def _locked_font(cell) -> bool:
    if _is_structure_fill(cell):
        return True
    size = cell.font.size
    return size is not None and size >= TITLE_SIZE


def apply_structure_style(ws: Worksheet, row_cells, style_key: str) -> None:
    style = _STYLES[style_key]
    if style_key == "result":
        for cell in row_cells:
            cell.fill = _fill(PALE)
        return
    for cell in row_cells:
        if style["fill"]:
            cell.fill = _fill(style["fill"])
        cell.font = _font(style)


def _period_kind(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _HIST_PERIOD.match(text):
        return "hist"
    if _FCST_PERIOD.match(text):
        return "fcst"
    return None


def _header_style(value) -> str:
    kind = _period_kind(value)
    if kind == "hist":
        return "col_header_hist"
    if kind == "fcst":
        return "col_header_fcst"
    return "col_header"


def _paint_header_cell(cell, style_key: str) -> None:
    apply_structure_style(cell.parent, [cell], style_key)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    cell.border = Border()


def write_page_title(ws: Worksheet, text: str, last_col: int, *, start_col: int = 1) -> None:
    for merged in list(ws.merged_cells.ranges):
        if merged.min_row == merged.max_row == 1:
            ws.unmerge_cells(str(merged))
    for col in range(start_col, last_col + 1):
        cell = ws.cell(row=1, column=col, value=text if col == start_col else None)
        apply_structure_style(ws, [cell], "page_title")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border()
    if last_col > start_col:
        ws.merge_cells(start_row=1, start_column=start_col, end_row=1, end_column=last_col)
        for col in range(start_col, last_col + 1):
            cell = ws.cell(row=1, column=col)
            apply_structure_style(ws, [cell], "page_title")
            cell.border = Border()
        ws.cell(row=1, column=start_col).alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = TITLE_ROW_HEIGHT
    mark_header(ws, 1)


def write_sheet_title(ws: Worksheet, company_name: str, last_col: int) -> None:
    write_page_title(ws, f"{company_name}-{SHEET_TITLE_SUFFIX[ws.title]}", last_col)


def write_section_row(
    ws: Worksheet,
    row: int,
    values: list,
    last_col: int,
    *,
    style: str = "section",
    start_col: int = 1,
) -> None:
    for col in range(start_col, last_col + 1):
        value = values[col - start_col] if col - start_col < len(values) else None
        cell = ws.cell(row=row, column=col, value=value)
        apply_structure_style(ws, [cell], style)
        cell.alignment = Alignment(
            horizontal="left" if col == start_col else "center",
            vertical="center",
            indent=0,
        )
        cell.border = Border()
    texts = [value for value in values if value not in (None, "")]
    if len(texts) == 1 and last_col > start_col:
        ws.merge_cells(start_row=row, start_column=start_col, end_row=row, end_column=last_col)
        for col in range(start_col, last_col + 1):
            cell = ws.cell(row=row, column=col)
            apply_structure_style(ws, [cell], style)
            cell.border = Border()
    ws.row_dimensions[row].height = HEADER_ROW_HEIGHT
    mark_header(ws, row)


def write_first_section_header(ws: Worksheet, headers_after_a: list[str], row: int = 2) -> int:
    """模块名与列表头同一行。返回正文起始行。"""
    title = FIRST_SECTION_TITLE[ws.title]
    values = [title, *headers_after_a]
    write_section_row(ws, row, values, len(values))
    return row + 1


def write_column_header_row(ws: Worksheet, row: int, values: list, *, start_col: int = 1) -> None:
    for offset, value in enumerate(values):
        cell = ws.cell(row=row, column=start_col + offset, value=value)
        style_key = _header_style(value)
        nxt = values[offset + 1] if offset + 1 < len(values) else None
        if value in (None, "") and _period_kind(nxt) == "hist":
            style_key = "col_header_hist"
        _paint_header_cell(cell, style_key)
    ws.row_dimensions[row].height = HEADER_ROW_HEIGHT
    mark_header(ws, row)


def write_block_header(ws: Worksheet, row: int, start: int, values: list) -> int:
    """左右分栏的模块标题。标题与年份在同一行。返回下一行。"""
    for offset, value in enumerate(values):
        cell = ws.cell(row=row, column=start + offset, value=value)
        apply_structure_style(ws, [cell], "section")
        cell.alignment = Alignment(
            horizontal="left" if offset == 0 else "center",
            vertical="center",
        )
        cell.border = Border()
    ws.row_dimensions[row].height = HEADER_ROW_HEIGHT
    mark_header(ws, row)
    return row + 1


def write_group_row(ws: Worksheet, row: int, label: str, last_col: int, *, start_col: int = 1) -> None:
    cells = [ws.cell(row=row, column=start_col, value=label)]
    for col in range(start_col + 1, last_col + 1):
        cells.append(ws.cell(row=row, column=col, value=None))
    apply_structure_style(ws, cells, "group")
    cells[0].alignment = Alignment(horizontal="left", vertical="center")
    for cell in cells[1:]:
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border()
    cells[0].border = Border()
    if last_col > start_col:
        ws.merge_cells(start_row=row, start_column=start_col, end_row=row, end_column=last_col)
    ws.row_dimensions[row].height = HEADER_ROW_HEIGHT
    mark_header(ws, row)


def write_field_label(ws: Worksheet, row: int, col: int, text: str):
    cell = ws.cell(row=row, column=col, value=text)
    apply_structure_style(ws, [cell], "field_label")
    cell.alignment = Alignment(horizontal="left", vertical="center")
    cell.border = Border()
    return cell


def write_row_label(ws: Worksheet, row: int, col: int, text: str, fill: str = NEAR):
    cell = ws.cell(row=row, column=col, value=text)
    cell.fill = _fill(fill)
    bold = bool(cell.font.bold)
    cell.font = Font(name=TEXT_FONT, size=BODY_SIZE, color=ON_SECTION, bold=bold)
    cell.alignment = Alignment(horizontal="left", vertical="center")
    cell.border = Border()
    return cell


def paint_stub_column(ws: Worksheet, col: int, fill: str = NEAR) -> None:
    """给科目列里尚未上色的行标题铺浅底。表头和结果行保持原样。"""
    headers = header_rows(ws)
    for row in range(2, (ws.max_row or 1) + 1):
        if row in headers:
            continue
        cell = ws.cell(row=row, column=col)
        value = cell.value
        if not isinstance(value, str) or not value or value.startswith("="):
            continue
        if _fill_rgb(cell) in {PALE, *_STRUCTURE_FILLS}:
            continue
        indent = cell.alignment.indent or 0 if cell.alignment is not None else 0
        cell.fill = _fill(fill)
        cell.font = Font(
            name=TEXT_FONT,
            size=BODY_SIZE,
            color=ON_SECTION,
            bold=bool(cell.font.bold),
        )
        cell.alignment = Alignment(horizontal="left", vertical="center", indent=indent)


def write_spacer(ws: Worksheet, row: int, last_col: int | None = None) -> None:
    ws.row_dimensions[row].height = SPACER_ROW_HEIGHT


def mark_bold_row(ws: Worksheet, row: int, start_col: int, end_col: int) -> None:
    for col in range(start_col, end_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = Font(
            name=cell.font.name,
            size=cell.font.size,
            color=cell.font.color,
            bold=True,
            italic=bool(cell.font.italic),
        )


def mark_subtotal(
    ws: Worksheet,
    row: int,
    start_col: int,
    end_col: int,
    *,
    fill: bool = False,
    final: bool = False,
    line: bool = False,
) -> None:
    del final
    border = Border(top=Side(style="thin", color=HAIRLINE)) if line else None
    for col in range(start_col, end_col + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = Font(
            name=cell.font.name,
            size=cell.font.size,
            color=cell.font.color,
            bold=True,
            italic=bool(cell.font.italic),
        )
        if border is not None:
            cell.border = border
        if fill:
            cell.fill = _fill(PALE)


def mark_result_cell(ws: Worksheet, row: int, col: int, *, final: bool = False) -> None:
    del final
    cell = ws.cell(row=row, column=col)
    cell.fill = _fill(PALE)
    cell.font = Font(
        name=cell.font.name or NUMBER_FONT,
        size=cell.font.size or NUMBER_SIZE,
        color=cell.font.color,
        bold=True,
    )
    cell.border = Border()


def set_col_widths(ws: Worksheet, last_col: int, first: float = 28.0, other: float = 13.0) -> None:
    ws.column_dimensions["A"].width = first
    for col in range(2, last_col + 1):
        ws.column_dimensions[get_column_letter(col)].width = other


def choose_number_format(label: str) -> str:
    text = label or ""
    if "涨跌" in text or text == "较现价空间":
        return SIGNED_PCT_FMT
    if text in {"投资评级", "合理价值区间"}:
        return "@"
    if "股本" in text:
        return AMT_FMT
    if re.search(r"PE|PB", text, re.I) and "系数" not in text:
        return MULTIPLE_FMT
    if any(token in text for token in ("率", "增速", "同比", "占比", "空间")):
        return PCT_FMT
    if any(token in text for token in ("EPS", "股价", "目标价")):
        return PX_FMT
    return AMT_FMT


def label_left_of(ws: Worksheet, row: int, col: int) -> str:
    for cursor in range(col - 1, 0, -1):
        value = ws.cell(row=row, column=cursor).value
        if isinstance(value, str) and value and not value.startswith("="):
            return value
    return ""


def apply_number_formats(
    ws: Worksheet,
    ratio_rows: set[int],
    last_col: int,
    note_col: int | None,
) -> None:
    for row in range(2, (ws.max_row or 1) + 1):
        for col in range(2, last_col + 1):
            if note_col is not None and col == note_col:
                continue
            cell = ws.cell(row=row, column=col)
            if cell.value is None:
                continue
            if isinstance(cell.value, str) and not str(cell.value).startswith("="):
                continue
            if cell.value == "-":
                continue
            if cell.number_format == DATE_FMT:
                continue
            label = label_left_of(ws, row, col)
            fmt = choose_number_format(label)
            if row in ratio_rows and fmt == AMT_FMT:
                fmt = PCT_FMT
            cell.number_format = fmt


def formula_role_color(formula: str) -> str:
    body = formula[1:] if isinstance(formula, str) and formula.startswith("=") else str(formula or "")
    if _EXTERNAL_REF.search(body):
        return EXTERNAL_RED
    if "!" in body:
        return CROSS_GREEN
    return FORMULA_BLACK


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _apply_role_font(cell, color: str) -> None:
    cell.font = Font(
        name=NUMBER_FONT,
        size=NUMBER_SIZE,
        color=color,
        bold=bool(cell.font.bold),
        italic=bool(cell.font.italic),
    )
    if cell.alignment is not None and cell.alignment.wrap_text:
        return
    indent = cell.alignment.indent or 0 if cell.alignment is not None else 0
    cell.alignment = Alignment(horizontal="right", vertical="center", indent=indent)


def apply_body_fonts(ws: Worksheet) -> None:
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 1, max_col=ws.max_column or 1):
        for cell in row:
            if _locked_font(cell):
                continue
            value = cell.value
            if not isinstance(value, str) or value.startswith("="):
                continue
            italic = bool(cell.font.italic)
            on_result = _fill_rgb(cell) == PALE
            cell.font = Font(
                name=TEXT_FONT,
                size=BODY_SIZE,
                color=ON_SECTION if on_result else (MUTED if italic else INK),
                bold=bool(cell.font.bold),
                italic=italic,
            )
            prev = cell.alignment
            if prev is not None and prev.wrap_text:
                continue
            indent = prev.indent or 0 if prev is not None else 0
            horizontal = "right" if value == "-" else "left"
            cell.alignment = Alignment(horizontal=horizontal, vertical="center", indent=indent)


def apply_value_roles(ws: Worksheet) -> None:
    inputs = input_cells(ws)
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 1, max_col=ws.max_column or 1):
        for cell in row:
            if _locked_font(cell):
                continue
            value = cell.value
            if (cell.row, cell.column) in inputs and _is_number(value):
                _apply_role_font(cell, INPUT_BLUE)
                continue
            if isinstance(value, str) and value.startswith("="):
                _apply_role_font(cell, formula_role_color(value))
                continue
            if _is_number(value) or isinstance(value, (date, datetime)):
                _apply_role_font(cell, FORMULA_BLACK)


def apply_row_heights(ws: Worksheet) -> None:
    headers = header_rows(ws)
    for row in range(1, (ws.max_row or 1) + 1):
        if ws.row_dimensions[row].height is not None:
            continue
        ws.row_dimensions[row].height = HEADER_ROW_HEIGHT if row in headers else BODY_ROW_HEIGHT


def _with_vertical_center(alignment) -> Alignment:
    if alignment is None:
        return Alignment(vertical="center")
    return Alignment(
        horizontal=alignment.horizontal,
        vertical="center",
        textRotation=alignment.textRotation,
        wrap_text=alignment.wrap_text,
        shrinkToFit=alignment.shrinkToFit,
        indent=alignment.indent,
    )


def center_cell_alignment(ws: Worksheet) -> None:
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 1, max_col=ws.max_column or 1):
        for cell in row:
            if cell.alignment is not None and cell.alignment.vertical == "center":
                continue
            cell.alignment = _with_vertical_center(cell.alignment)


def finish_sheet(ws: Worksheet) -> None:
    apply_body_fonts(ws)
    apply_value_roles(ws)
    apply_row_heights(ws)
    center_cell_alignment(ws)
    ws.sheet_view.showGridLines = False
