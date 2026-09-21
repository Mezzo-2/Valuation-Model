"""券商简称与数据来源条目。底稿、封面、估值表都走这一套。"""

from __future__ import annotations

import re

_LEGAL_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "研究所有限公司",
    "有限公司",
    "集团股份公司",
)

_CITY_PREFIX = (
    "上海",
    "北京",
    "深圳",
    "广州",
    "浙江",
    "江苏",
    "南京",
    "杭州",
    "中国",
)

_JUNK_SUBSTR = (
    "年报主营",
    "拆分计划",
    "历史检索",
    "固定口径",
    "fixture",
)

_JUNK_TITLE = (
    "晨会",
    "早会",
    "研究所",
)

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATE_COMPACT_RE = re.compile(r"\d{8}")
_INLINE_CITE_RE = re.compile(r"(证券|国际|研究|银行)\[\d+\]")


def short_house_name(name: str | None) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    text = re.sub(r"[（(][^）)]*[）)]", "", raw).strip()
    for suffix in _LEGAL_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    text = text.replace("研究所", "").strip(" ·.-")
    for prefix in _CITY_PREFIX:
        rest = text[len(prefix) :] if text.startswith(prefix) else ""
        if rest and len(rest) >= 4 and any(token in rest for token in ("证券", "国际", "研究")):
            text = rest
            break
    text = re.sub(r"证券证券", "证券", text).strip(" ·.-")
    return text or raw


def is_broker_source(name: str | None) -> bool:
    text = str(name or "").strip()
    if not text:
        return False
    if any(token in text for token in _JUNK_SUBSTR):
        return False
    if re.fullmatch(r"S\d+", text):
        return False
    return True


def is_synthetic_title(
    title: str | None,
    house: str | None = None,
    company: str | None = None,
    date: str | None = None,
) -> bool:
    text = str(title or "").strip()
    if not text:
        return True
    leftover = _strip_meta(text, house, company, date)
    if not leftover:
        return True
    if leftover in {"研究所", "研究", "上海", "北京", "深圳"}:
        return True
    if len(leftover) <= 2:
        return True
    return False


def report_title(
    title: str | None,
    house: str | None = None,
    company: str | None = None,
    date: str | None = None,
) -> str:
    text = str(title or "").strip()
    if is_synthetic_title(text, house, company, date):
        return ""
    if any(token in text for token in _JUNK_TITLE) and str(company or "") not in text:
        return ""
    text = re.sub(r"股份有限公司|有限责任公司|有限公司", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ·.-|/")
    return text


def format_source_line(
    house: str | None,
    *,
    title: str | None = None,
    date: str | None = None,
    company: str | None = None,
) -> str:
    short = short_house_name(house)
    heading = report_title(title, house, company, date)
    if heading and date:
        return f"{short}：《{heading}》（{date}）"
    if heading:
        return f"{short}：《{heading}》"
    if date:
        return f"{short}（{date}）"
    return short


def format_citation(
    index: int,
    house: str | None,
    *,
    title: str | None = None,
    date: str | None = None,
    company: str | None = None,
) -> str:
    return format_source_line(house, title=title, date=date, company=company)


def bibliography(sources: list[dict], *, company: str | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    ordered = sorted(
        sources,
        key=lambda item: (
            str(item.get("date") or ""),
            short_house_name(item.get("name")),
        ),
        reverse=True,
    )
    for item in ordered:
        raw_name = item.get("name")
        if not is_broker_source(raw_name):
            continue
        short = short_house_name(raw_name)
        if not short or short in seen:
            continue
        seen.add(short)
        date = str(item.get("date") or "")
        heading = report_title(item.get("title"), raw_name, company, date)
        line = format_source_line(raw_name, title=item.get("title"), date=date, company=company)
        rows.append(
            {
                "ref": f"[{len(rows) + 1}]",
                "name": short,
                "date": date,
                "title": heading,
                "line": line,
                "citation": line,
            }
        )
    return rows


def source_block(rows: list[dict] | None) -> str:
    lines = []
    for item in rows or []:
        line = str(item.get("line") or item.get("citation") or "").strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def strip_inline_cites(text: str) -> str:
    return _INLINE_CITE_RE.sub(r"\1", text or "")


def _strip_meta(title: str, house: str | None, company: str | None, date: str | None) -> str:
    text = str(title or "")
    full = str(house or "").strip()
    short = short_house_name(full)
    for token in (full, short, str(company or "").strip(), str(date or "").strip()):
        if token:
            text = text.replace(token, " ")
    text = _DATE_RE.sub(" ", text)
    text = _DATE_COMPACT_RE.sub(" ", text)
    text = re.sub(r"股份有限公司|有限责任公司|有限公司|研究所", " ", text)
    text = re.sub(r"[\s\-—_/,.]+", " ", text).strip()
    return text
