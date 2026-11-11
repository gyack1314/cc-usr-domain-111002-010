"""审查意图模型。

两种意图互斥，不能混用：

1. 显式模式（explicit）：给出明确资料编号集合，编号不存在时报告缺项，不默默缩小集合；
2. 条件模式（matching）：给出一个筛选条件与排除编号集合，运行时绑定页面显示的
   查询签名、结果修订与匹配总数。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Optional

EXPLICIT = "explicit"
MATCHING = "matching"


class IntentError(ValueError):
    """意图不合法：模式混用、编号非法或条件缺失。"""


def _clean_ids(values: Optional[Iterable[Any]], field_name: str, *, allow_empty: bool) -> FrozenSet[str]:
    if values is None:
        values = frozenset()
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise IntentError(f"{field_name} 必须是编号数组")
    cleaned = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise IntentError(f"{field_name} 中的编号必须是非空字符串")
        cleaned.append(value.strip())
    if not cleaned and not allow_empty:
        raise IntentError(f"{field_name} 不能为空")
    # 重复编号属于书写冗余：规范化去重，而不是改变集合语义。
    return frozenset(cleaned)


@dataclass(frozen=True)
class ExplicitIntent:
    """模式一：明确资料编号集合。"""

    ids: FrozenSet[str]
    mode: str = EXPLICIT

    def __post_init__(self) -> None:
        if self.mode != EXPLICIT:
            raise IntentError("ExplicitIntent 的模式必须是 explicit")
        if not isinstance(self.ids, frozenset) or not self.ids:
            raise IntentError("显式编号集合不能为空")


@dataclass(frozen=True)
class MatchingIntent:
    """模式二：筛选条件加排除编号集合。

    排除编号必须在运行时属于该筛选条件的匹配集合，否则意图失效。允许空排除集合，
    此时语义就是“选择全部匹配项”。
    """

    query: str
    exclusions: FrozenSet[str]
    mode: str = MATCHING

    def __post_init__(self) -> None:
        if self.mode != MATCHING:
            raise IntentError("MatchingIntent 的模式必须是 matching")
        if not isinstance(self.query, str) or not self.query.strip():
            raise IntentError("条件模式必须绑定非空筛选条件")
        if not isinstance(self.exclusions, frozenset):
            raise IntentError("排除编号必须是集合")


def build_intent(
    *,
    ids: Optional[Iterable[str]] = None,
    query: Optional[str] = None,
    exclusions: Optional[Iterable[str]] = None,
) -> "ExplicitIntent | MatchingIntent":
    """按关键字构造意图；显式编号与筛选条件同时出现即拒绝。"""
    has_explicit = ids is not None
    has_matching = query is not None or exclusions is not None
    if has_explicit and has_matching:
        raise IntentError("显式编号与筛选条件两种意图不能混用")
    if has_explicit:
        return ExplicitIntent(_clean_ids(ids, "ids", allow_empty=False))
    if has_matching:
        if not isinstance(query, str) or not query.strip():
            raise IntentError("条件模式必须提供筛选条件 query")
        return MatchingIntent(
            query=query.strip(),
            exclusions=_clean_ids(exclusions, "exclusions", allow_empty=True),
        )
    raise IntentError("必须提供显式编号集合或筛选条件")


def intent_from_dict(payload: dict) -> "ExplicitIntent | MatchingIntent":
    """从 JSON 结构构造意图，严格校验 mode 字段并禁止混用。"""
    if not isinstance(payload, dict):
        raise IntentError("意图必须是 JSON 对象")
    mode = payload.get("mode")
    if mode == EXPLICIT:
        if "query" in payload or "exclusions" in payload:
            raise IntentError("explicit 意图不能携带筛选条件或排除编号")
        return build_intent(ids=payload.get("ids"))
    if mode == MATCHING:
        if "ids" in payload:
            raise IntentError("matching 意图不能携带显式编号集合")
        return build_intent(query=payload.get("query"), exclusions=payload.get("exclusions", []))
    raise IntentError(f"未知意图模式: {mode!r}，仅支持 explicit / matching")


def intent_key(intent: "ExplicitIntent | MatchingIntent") -> str:
    """意图的稳定指纹：同一组计划参数得到同一 key，用于中断后找回。"""
    if isinstance(intent, ExplicitIntent):
        payload = {"mode": EXPLICIT, "ids": sorted(intent.ids)}
    else:
        payload = {"mode": MATCHING, "query": intent.query, "exclusions": sorted(intent.exclusions)}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
