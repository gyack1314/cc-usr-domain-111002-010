"""证据资料打包模块：把审查意图转成精确选择集合，并在打包前后核对成员。

公共入口：

- :class:`ExplicitIntent` / :class:`MatchingIntent` 与 :func:`build_intent`：两种互斥意图；
- :class:`EvidencePackager`：通过 Playwright 驱动页面控件完成选择、预览、生成与找回；
- :class:`AbortError` / :class:`AbortReport`：筛选或修订变化等场景下的撤回报告；
- :class:`SelectionDiff`：计划集合与实际集合的多选/漏选核对。

模块只通过页面可见控件与 DOM 读取操作门户，不调用运维入口，也不直接改写页面内部选择集合。
"""
from .diff import AbortError, AbortReport, SelectionDiff, compare_sets
from .intent import (
    ExplicitIntent,
    IntentError,
    MatchingIntent,
    build_intent,
    intent_from_dict,
    intent_key,
)
from .journal import RunJournal, RunResult
from .packager import EvidencePackager, RecoveryError
from .portal_page import PageSnapshot, PortalPage

__all__ = [
    "ExplicitIntent",
    "MatchingIntent",
    "IntentError",
    "build_intent",
    "intent_from_dict",
    "intent_key",
    "SelectionDiff",
    "compare_sets",
    "AbortError",
    "AbortReport",
    "EvidencePackager",
    "RecoveryError",
    "RunJournal",
    "RunResult",
    "PortalPage",
    "PageSnapshot",
]
