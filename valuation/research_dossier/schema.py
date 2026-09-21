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

COVER_KEYS = ("background", "thesis", "outlook", "risks", "sources")


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
    risks = raw.get("risks") or []
    if isinstance(risks, str):
        risks = [part.strip() for part in re.split(r"[\n；;]", risks) if part.strip()]
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
        "background": str(raw.get("background") or "").strip(),
        "thesis": str(raw.get("thesis") or "").strip(),
        "outlook": str(raw.get("outlook") or "").strip(),
        "risks": [str(item).strip() for item in risks if str(item).strip()],
        "sources": cleaned,
    }


def validate_summary_notes(notes: dict) -> list[str]:
    errors: list[str] = []
    for key in ("background", "thesis", "outlook"):
        text = notes.get(key) or ""
        if not text:
            errors.append(f"封面缺 {key}")
        for token in BANNED:
            if token in text:
                errors.append(f"封面 {key} 含禁止用语 {token}")
    if len(notes.get("risks") or []) < 3:
        errors.append("封面风险须至少 3 条")
    for item in notes.get("risks") or []:
        for token in BANNED:
            if token in item:
                errors.append(f"风险含禁止用语 {token}")
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
    for key, value in (
        ("background", notes.get("background")),
        ("thesis", notes.get("thesis")),
        ("outlook", notes.get("outlook")),
    ):
        if value and not _cover_in_text(str(value), text):
            errors.append(f"封面 {key} 无法在底稿中找到对应句")
    for item in notes.get("risks") or []:
        if not _cover_in_text(str(item), text):
            errors.append("封面风险无法在底稿中找到对应句")
    return errors


def format_sources(notes: dict) -> str:
    return "；".join(notes.get("sources") or [])


def format_risks(notes: dict) -> str:
    return "\n".join(notes.get("risks") or [])


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
