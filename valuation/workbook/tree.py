"""把叶子名单还原成先父后子的出表顺序。父项只做加总，不是预测对象。"""

from __future__ import annotations

from dataclasses import dataclass, field

from valuation.segment_split.split_plan import RESIDUAL_NAMES, is_residual_name


@dataclass
class SplitGroup:
    parent: str
    drilled: bool
    leaf: dict | None = None
    children: list[dict] = field(default_factory=list)

    @property
    def top_name(self) -> str:
        if self.drilled:
            return self.parent
        return str((self.leaf or {}).get("name") or self.parent)


def build_split_groups(split: dict) -> tuple[list[SplitGroup], dict | None]:
    segs = [item for item in (split.get("final_segments") or []) if item.get("name")]
    official = [str(name).strip() for name in (split.get("official_parents") or []) if str(name).strip()]
    company_res = _company_residual(segs, official)
    residual_name = str(company_res.get("name") or "") if company_res else ""
    groups: list[SplitGroup] = []
    placed: set[str] = set()
    if residual_name:
        placed.add(residual_name)

    if official:
        for parent in official:
            children = [
                item
                for item in segs
                if str(item.get("parent") or "").strip() == parent and str(item.get("name") or "") != parent
            ]
            self_leaf = next((item for item in segs if str(item.get("name") or "") == parent), None)
            if children:
                groups.append(SplitGroup(parent=parent, drilled=True, children=children))
                placed.update(str(item.get("name") or "") for item in children)
                if self_leaf:
                    placed.add(parent)
            elif self_leaf:
                groups.append(SplitGroup(parent=parent, drilled=False, leaf=self_leaf))
                placed.add(parent)
    else:
        for item in segs:
            name = str(item.get("name") or "")
            if name == residual_name:
                continue
            groups.append(SplitGroup(parent=name, drilled=False, leaf=item))
            placed.add(name)

    for item in segs:
        name = str(item.get("name") or "")
        if name in placed:
            continue
        groups.append(SplitGroup(parent=str(item.get("parent") or name), drilled=False, leaf=item))
        placed.add(name)
    return groups, company_res


def top_level_names(groups: list[SplitGroup], company_res: dict | None) -> list[str]:
    names = [group.top_name for group in groups]
    if company_res:
        names.append(str(company_res["name"]))
    return names


def top_level_revenue_labels(groups: list[SplitGroup], company_res: dict | None) -> list[str]:
    return [f"分部收入_{name}" for name in top_level_names(groups, company_res)]


def segment_group_title(name: str, method: str) -> str:
    return f"{name}（{method}）"


def revenue_group_titles(split: dict) -> set[str]:
    titles: set[str] = set()
    groups, company_res = build_split_groups(split)
    for group in groups:
        if group.drilled:
            titles.add(group.parent)
            for child in group.children:
                titles.add(segment_group_title(child["name"], child["method"]))
        elif group.leaf:
            titles.add(segment_group_title(group.leaf["name"], group.leaf["method"]))
    if company_res:
        titles.add(segment_group_title(company_res["name"], company_res["method"]))
    return titles


def _company_residual(segs: list[dict], official: list[str]) -> dict | None:
    for item in segs:
        name = str(item.get("name") or "").strip()
        parent = str(item.get("parent") or "").strip()
        if name in RESIDUAL_NAMES and not parent:
            return item
        if not parent and is_residual_name(name, official_parents=official) and name not in official:
            return item
    return None
