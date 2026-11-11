"""集合核对与撤回报告。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence, Tuple


def _sorted_frozen(values: Iterable[str]) -> Tuple[str, ...]:
    return tuple(sorted(set(values)))


@dataclass(frozen=True)
class SelectionDiff:
    """计划集合与页面实际成员集合的差异。"""

    planned: Tuple[str, ...]
    actual: Tuple[str, ...]
    extra: Tuple[str, ...] = field(default_factory=tuple)
    missing: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def exact(self) -> bool:
        return not self.extra and not self.missing


def compare_sets(planned: Iterable[str], actual: Iterable[str]) -> SelectionDiff:
    """计算多选（actual 多出的）与漏选（planned 缺少的）。"""
    planned_set = set(planned)
    actual_set = set(actual)
    return SelectionDiff(
        planned=_sorted_frozen(planned_set),
        actual=_sorted_frozen(actual_set),
        extra=_sorted_frozen(actual_set - planned_set),
        missing=_sorted_frozen(planned_set - actual_set),
    )


@dataclass(frozen=True)
class AbortReport:
    """撤回当前执行时的结构化报告。

    - extra/missing：相对计划集合的多选、漏选；
    - invalid_conditions：失效的绑定条件（查询签名、结果修订、匹配总数变化等文字记录）；
    - binding：撤回时页面实际显示的签名/修订/总数，便于与计划绑定值对照。
    """

    reason: str
    stage: str
    detail: str
    planned: Tuple[str, ...] = ()
    extra: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()
    invalid_conditions: Tuple[str, ...] = ()
    binding: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "reason": self.reason,
            "stage": self.stage,
            "detail": self.detail,
            "planned": list(self.planned),
            "extra": list(self.extra),
            "missing": list(self.missing),
            "invalid_conditions": list(self.invalid_conditions),
            "binding": self.binding,
        }


class AbortError(Exception):
    """计划与页面状态不一致，当前执行被撤回，需要重新确认。"""

    def __init__(self, report: AbortReport):
        super().__init__(f"[{report.reason}] {report.detail}")
        self.report = report
