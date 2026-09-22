"""研究底稿封面与轻校验。"""

from __future__ import annotations

import re
from typing import Any

from valuation.shared.houses import short_house_name

CHAPTERS = (
    "投资要点",
    "投资逻辑",
    "业务展望",
    "盈利预测",
    "估值与评级",
    "主要风险",
)

DISCLAIMER = "本模型由 AI 根据公开资料自动生成，仅供研究演示，不构成投资建议。"

BANNED = (
    "数据缺口",
    "【事件发酵】",
    "事件发酵",
    "final_rationale",
    "pe_adjust",
    "综合各方",
    "股份有限公司",
    "年报主营构成",
    "拆分计划确认后的历史检索",
    "方向一致、幅度更保守/更积极",
)

COVER_KEYS = ("thesis", "outlook", "risks", "sources")
POINT_KEYS = ("thesis", "outlook", "risks")


class SchemaError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


def require_summary_notes(payload: dict) -> dict:
    notes = normalize_summary_notes(payload)
    errors = validate_summary_notes(notes)
    if errors:
        raise SchemaError(errors)
    return notes


def normalize_summary_notes(raw: dict) -> dict:
    sources = raw.get("sources") or []
    if isinstance(sources, str):
        sources = [part.strip() for part in re.split(r"[；;、\n]", sources) if part.strip()]
    cleaned = []
    for item in sources:
        text = str(item).strip()
        if text.startswith("[") or "：《" in text or "《" in text:
            match = re.match(r"\[\d+\]\s*([^.\[]+)", text) or re.match(r"^([^：:《（(]+)", text)
            text = match.group(1).strip() if match else text
        text = short_house_name(text)
        if text and text not in cleaned:
            cleaned.append(text)
    return {
        "thesis": _as_points(raw.get("thesis")),
        "outlook": _as_points(raw.get("outlook")),
        "risks": _as_points(raw.get("risks")),
        "sources": cleaned,
    }


def validate_summary_notes(notes: dict) -> list[str]:
    errors: list[str] = []
    for key in POINT_KEYS:
        items = notes.get(key) or []
        if not items:
            errors.append(f"封面缺 {key}")
        for item in items:
            if not point_text(item):
                errors.append(f"封面 {key} 有空论点")
            for token in BANNED:
                if token in point_title(item) or token in point_text(item):
                    errors.append(f"封面 {key} 含禁止用语 {token}")
    return errors


def validate_dossier(markdown: str, snapshot: dict, notes: dict | None = None) -> list[str]:
    errors: list[str] = []
    text = markdown or ""
    for title in CHAPTERS:
        if title not in text:
            errors.append(f"缺章节 {title}")
    display = snapshot.get("display") or {}
    first = _chapter(text, "投资要点") or text[:1200]
    rating = str(display.get("rating") or "")
    tp = display.get("target_price")
    eps = display.get("eps_y1")
    if rating and rating not in first:
        errors.append("投资要点未出现评级")
    first_flat = first.replace(",", "")
    if tp is not None and not any(token in first_flat for token in _number_tokens(tp, 2)):
        errors.append(f"投资要点未出现目标价 {tp}")
    if eps is not None and not any(token in first_flat for token in _number_tokens(eps, 2)):
        errors.append(f"投资要点未出现 EPS {eps}")
    logic = _chapter(text, "投资逻辑") + _chapter(text, "盈利预测")
    if not logic:
        logic = text
    for item in snapshot.get("material_gaps") or []:
        year = str(item.get("year") or "")
        if year and year not in logic:
            errors.append(f"偏差年 {year} 未在投资逻辑或盈利预测中出现")
    for token in BANNED:
        if token in text:
            errors.append(f"底稿含禁止用语 {token}")
    if re.search(r"\bthin\b", text):
        errors.append("底稿含 thin")
    if DISCLAIMER not in text:
        errors.append("文末缺免责声明")
    elif text.count(DISCLAIMER) != 1:
        errors.append("免责声明应只出现一次")
    elif DISCLAIMER not in text.strip()[-len(DISCLAIMER) - 12 :]:
        errors.append("免责声明须在文末")
    if re.search(
        r"^#+\s*(?:[一二三四五六七八九十0-9]+[、.．]?\s*)?数据来源\b",
        text,
        flags=re.M,
    ):
        errors.append("底稿不要写数据来源章节")
    if re.search(r"(证券|国际|研究|银行)\[\d+\]", text):
        errors.append("正文不要标引用角标")
    logic_ch = _chapter(text, "投资逻辑")
    outlook_ch = _chapter(text, "业务展望")
    if _cjk_len(logic_ch) < 400:
        errors.append("投资逻辑过短")
    if _cjk_len(outlook_ch) < 400:
        errors.append("业务展望过短")
    if logic_ch.count("### ") < 2:
        errors.append("投资逻辑须有主题小标题")
    if outlook_ch.count("### ") < 2:
        errors.append("业务展望须有主题小标题")
    for title in ("投资逻辑", "业务展望", "盈利预测"):
        if re.search(
            r"(?m)^\s*(?:#{3,}\s*(?:\*\*)?20\d{2}[AE]|(?:\*\*)?20\d{2}[AE](?:\*\*)?\s*[：:])",
            _chapter(text, title),
        ):
            errors.append(f"{title}不要按年做小标题")
    pnl_prose = "\n".join(
        line for line in _chapter(text, "盈利预测").splitlines() if not line.strip().startswith("|")
    )
    for year in snapshot.get("forecast_periods") or []:
        if str(year) not in pnl_prose:
            errors.append(f"盈利预测未写到 {year}")
    if notes:
        errors.extend(validate_cover_against(text, notes))
    return errors


def validate_cover_against(markdown: str, notes: dict) -> list[str]:
    errors: list[str] = []
    text = markdown or ""
    for key in POINT_KEYS:
        for item in notes.get(key) or []:
            body = point_text(item)
            if body and not _cover_in_text(body, text):
                errors.append(f"封面 {key} 无法在底稿中找到对应句")
    return errors


def format_sources(notes: dict) -> str:
    return "；".join(notes.get("sources") or [])


def format_points(items: list | None) -> str:
    lines = []
    for i, item in enumerate(items or [], 1):
        title = point_title(item)
        text = point_text(item)
        lines.append(f"{i}. {title} {text}".strip() if title else f"{i}. {text}")
    return "\n".join(lines)


def format_risks(notes: dict) -> str:
    return format_points(notes.get("risks") or [])


def format_thesis(notes: dict) -> str:
    return format_points(notes.get("thesis") or [])


def format_outlook(notes: dict) -> str:
    return format_points(notes.get("outlook") or [])


def point_title(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("title") or "").strip()
    return ""


def point_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("detail") or item.get("body") or "").strip()
    return str(item or "").strip()


def _as_points(value: Any) -> list[dict[str, str]]:
    if isinstance(value, list):
        raw = list(value)
    elif str(value or "").strip():
        raw = [value]
    else:
        raw = []
    items: list[dict[str, str]] = []
    for part in raw:
        point = _as_point(part)
        if point and (point["title"] or point["text"]):
            items.append(point)
    return items


def _as_point(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        title = str(value.get("title") or "").strip()
        text = str(value.get("text") or value.get("detail") or value.get("body") or "").strip()
        title = _clean_title(title)
        if text:
            return {"title": title, "text": text}
        if title:
            return _as_point(title)
        return None
    raw = str(value or "").strip()
    raw = re.sub(r"^\d+[\.、]\s*", "", raw).strip()
    raw = raw.strip("* ")
    if not raw:
        return None
    match = re.match(r"^([^。：:]{2,16})[。：:]\s+(.+)$", raw, flags=re.S)
    if match and not re.match(r"^\d", match.group(1)):
        return {"title": _clean_title(match.group(1)), "text": match.group(2).strip()}
    return {"title": "", "text": raw}


def _clean_title(title: str) -> str:
    return re.sub(r"^[*【\[]+|[】\].。:：*]+$", "", str(title or "").strip())


def _chapter(markdown: str, title: str) -> str:
    pattern = (
        rf"(?:^|\n)##(?!#)\s*(?:[一二三四五六七八九十0-9]+[、.．、]?\s*)?{re.escape(title)}\s*\n"
        rf"(.*?)(?=\n##(?!#)|\Z)"
    )
    match = re.search(pattern, markdown, flags=re.S)
    return match.group(1) if match else ""


def _cover_in_text(value: str, markdown: str) -> bool:
    compact_md = _compact(markdown)
    compact_val = _compact(value)
    if not compact_val:
        return False
    if compact_val in compact_md:
        return True
    for sentence in re.split(r"[。！？\n；;]", value):
        piece = _compact(sentence)
        if len(piece) >= 8 and piece in compact_md:
            return True
    if len(compact_val) >= 12 and compact_val[:12] in compact_md:
        return True
    hits = 0
    width = 10
    if len(compact_val) >= width:
        for i in range(0, len(compact_val) - width + 1, 3):
            if compact_val[i : i + width] in compact_md:
                hits += 1
        if hits >= 3:
            return True
    nums = re.findall(r"\d+(?:\.\d+)?", value)
    distinctive = [n for n in nums if len(n) >= 3]
    if distinctive and all(n in markdown.replace(",", "") for n in distinctive[:4]):
        return True
    return False


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _cjk_len(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _number_tokens(value: Any, digits: int) -> set[str]:
    number = float(value)
    tokens = {f"{number:.{digits}f}", f"{number:.1f}", f"{number:.2f}"}
    tokens.add(f"{number:.{digits}f}".rstrip("0").rstrip("."))
    return {token for token in tokens if token}
