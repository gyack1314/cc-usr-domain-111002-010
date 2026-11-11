"""打包编排：把审查意图转成精确选择集合，并在打包前后核对成员。

核对点（任何一处不一致即撤回，不点击生成、不凑数量）：

1. 选择完成后：逐页逐行复选框读出的实际成员 == 计划集合；
2. 预览：页面预览列出的成员编号、模式、查询签名、修订与计划完全相等；
3. 生成后：下载文件内 manifest 与已确认预览一致，且 package_id 已留痕。

条件模式额外绑定查询签名、结果修订、匹配总数；筛选变化、列表修订变化或预览出现
未计划资料都会撤回并输出多选、漏选及失效条件。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .diff import AbortError, AbortReport, SelectionDiff, compare_sets
from .intent import EXPLICIT, MATCHING, ExplicitIntent, MatchingIntent
from .journal import RunJournal, RunResult
from .portal_page import PortalPage

Hook = Optional[Callable[[PortalPage, RunJournal], None]]


class RecoveryError(RuntimeError):
    """无法按已记录 package_id 找回同一资料包。"""


class EvidencePackager:
    def __init__(
        self,
        base_url: str,
        workdir: str | Path = "state/packager",
        *,
        timeout_ms: int = 15000,
        capture_screenshots: bool = True,
        browser=None,
    ):
        self.base_url = base_url
        self.workdir = Path(workdir)
        self.timeout_ms = timeout_ms
        self.capture_screenshots = capture_screenshots
        self._playwright = None
        self._owns_playwright = False
        # 可复用外部已启动的浏览器（如测试夹具），避免在同一线程嵌套启动 Playwright。
        self.browser = browser

    # ---------- 浏览器生命周期 ----------
    def __enter__(self) -> "EvidencePackager":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self.browser is None:
            self._playwright = sync_playwright().start()
            self._owns_playwright = True
            self.browser = self._playwright.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )

    def close(self) -> None:
        # 复用的外部浏览器不由本实例关闭。
        if self._owns_playwright:
            if self.browser is not None:
                self.browser.close()
            if self._playwright is not None:
                self._playwright.stop()
        self.browser = None if self._owns_playwright else self.browser
        self._playwright = None

    def _new_context(self) -> BrowserContext:
        if self.browser is None:
            raise RuntimeError("浏览器尚未启动，请在 with EvidencePackager(...) 内运行或先调用 start()")
        return self.browser.new_context(
            accept_downloads=True, viewport={"width": 1280, "height": 900}
        )

    # ---------- 主入口 ----------
    def run(
        self,
        intent: "ExplicitIntent | MatchingIntent",
        *,
        after_selection: Hook = None,
        page: Optional[Page] = None,
    ) -> RunResult:
        journal = RunJournal(self.workdir, intent, capture_screenshots=self.capture_screenshots)

        # 中断发生在已生成之后：按已记录 package_id 找回同包，不重复打包。
        if journal.record.get("status") == "packaged" and journal.record.get("package_id"):
            return self.recover(intent, page=page)

        context = None
        own_page = page is None
        if page is None:
            context = self._new_context()
            page = context.new_page()
        portal = PortalPage(page, timeout_ms=self.timeout_ms)
        try:
            # 外部传入的页面若已在门户上则保留其状态（含可能的旧勾选），不做重载。
            reloaded = portal.ensure_ready(self.base_url)
            journal.step("open", {"url": self.base_url, "reloaded": reloaded})

            # 防旧勾选：新上下文本应为空；若工作区带着历史选择，立即撤回等待重新确认。
            stale = portal.assert_clean_workspace()
            if stale:
                self._abort(
                    journal,
                    portal,
                    reason="stale-selection",
                    stage="preflight",
                    detail="工作区存在上一次筛选遗留的勾选，拒绝沿用",
                    planned=self._planned_ids(intent),
                    actual=stale,
                    invalid_conditions=("工作区未清零选择状态",),
                )

            if intent.mode == EXPLICIT:
                planned, binding = self._select_explicit(intent, portal, journal)
            else:
                planned, binding = self._select_matching(intent, portal, journal)

            # 外部事件注入点（测试或人工演练）：模拟筛选变化、新增记录等。
            if after_selection is not None:
                journal.step("before-hook", {"hook": "after_selection"})
                after_selection(portal, journal)
                journal.step("after-hook", {})

            # 预览前活页核对：选择完成后到预览之间，签名/模式变化立即撤回；
            # 修订与匹配总数的变化以点击预览返回的权威数据为准（见 _preview_verify...）。
            actual_pre, _snap_pre = self._read_all_checked(portal, "recheck-after-hook", journal)
            self._check_live_binding(portal, journal, binding, planned, actual_pre)
            self._check_membership(journal, portal, planned, actual_pre, "post-selection", binding)

            result = self._preview_verify_and_generate(portal, journal, intent, planned, binding)
            return result
        except AbortError:
            raise
        except KeyboardInterrupt:
            # 中断发生在预览之前：保持 planned/interrupted，允许重新规划。
            journal.mark_interrupted()
            raise
        finally:
            if own_page and context is not None:
                context.close()

    # ---------- 显式编号模式 ----------
    def _select_explicit(self, intent: ExplicitIntent, portal: PortalPage, journal: RunJournal):
        planned = set(intent.ids)
        portal.set_filter("all")
        journal.step("filter", {"query": "all", "note": "显式模式在“全部”资料中按编号选择"})

        seen: set[str] = set()
        for snap in portal.iter_pages(reset=True):
            page_ids = [doc_id for doc_id, _ in snap.rows]
            for doc_id in page_ids:
                seen.add(doc_id)
                portal.set_row(doc_id, doc_id in planned)
            journal.step(
                "page-select",
                {"page": snap.page, "rows": list(page_ids), "page_size": snap.page_size},
            )
            # 操作后重读：证据 rows 反映真实点击后的勾选状态。
            after = portal.read_snapshot()
            checked = after.checked_ids
            evidence_path = journal.selection_evidence(
                f"p{snap.page}", list(checked), "显式模式：逐行复选框真实点击后的勾选状态"
            )
            journal.write_evidence_json(
                evidence_path,
                {"page": snap.page, "rows": [{"id": i, "checked": c} for i, c in after.rows]},
            )
            if self.capture_screenshots:
                journal.screenshot(portal.page, f"explicit-page-{snap.page}")

        missing = sorted(planned - seen)
        if missing:
            # 显式编号不存在：报告缺项，不默默缩小集合。
            self._abort(
                journal,
                portal,
                reason="missing-explicit-ids",
                stage="selection",
                detail="显式编号在门户全部资料中不存在",
                planned=sorted(planned),
                actual=sorted(seen & planned),
                invalid_conditions=tuple(f"缺项 {doc_id}" for doc_id in missing),
                missing=missing,
            )

        snap = portal.read_snapshot()
        binding = {
            "query": snap.query,
            "revision": snap.revision,
            "total": snap.total,
            "mode": "explicit",
        }
        journal.set_binding(binding)
        journal.step("binding", binding)
        # 统一的绑定/成员核对在 run() 中钩子执行后进行。
        return planned, binding

    # ---------- 条件模式 ----------
    def _select_matching(self, intent: MatchingIntent, portal: PortalPage, journal: RunJournal):
        options = portal.filter_options()
        if intent.query not in options:
            self._abort(
                journal,
                portal,
                reason="unknown-filter",
                stage="selection",
                detail=f"筛选条件 {intent.query!r} 不是页面提供的查询签名",
                planned=self._planned_ids(intent),
                actual=(),
                invalid_conditions=(f"未知查询签名 {intent.query}",),
            )

        snap = portal.set_filter(intent.query)
        bound = {
            "query": snap.signature,
            "revision": snap.revision,
            "total": snap.total,
            "mode": MATCHING,
        }
        journal.set_binding(bound)
        journal.step("binding", {**bound, "note": "条件模式绑定页面显示的查询签名/修订/匹配总数"})

        universe: set[str] = set()
        for page_snap in portal.iter_pages(reset=True):
            universe.update(doc_id for doc_id, _ in page_snap.rows)
        if len(universe) != snap.total:
            self._abort(
                journal,
                portal,
                reason="total-mismatch",
                stage="selection",
                detail="翻页读到的资料数与页面匹配总数不一致",
                planned=sorted(universe),
                actual=sorted(universe),
                invalid_conditions=(f"匹配总数声明 {snap.total}，翻页实得 {len(universe)}",),
            )

        invalid_exclusions = sorted(intent.exclusions - universe)
        if invalid_exclusions:
            # 排除编号不在当前匹配集合中：条件失效，撤回而不是把排除项“吞掉”。
            self._abort(
                journal,
                portal,
                reason="exclusion-not-in-match",
                stage="selection",
                detail="排除编号不属于当前筛选的匹配集合",
                planned=sorted(universe - intent.exclusions),
                actual=sorted(universe),
                invalid_conditions=tuple(f"排除项无匹配 {doc_id}" for doc_id in invalid_exclusions),
                missing=invalid_exclusions,
            )
        planned = universe - intent.exclusions

        # “选择全部匹配项”是独立操作，与当前页全选不同。
        portal.click_all_matching()
        journal.step("click-all-matching", {"query": intent.query, "universe": sorted(universe)})
        if self.capture_screenshots:
            journal.screenshot(portal.page, "matching-all-selected")

        # 在全部匹配模式下取消当前页的一行：该编号进入排除项，而不是退回显式选择。
        for page_snap in portal.iter_pages(reset=True):
            for doc_id, _row_checked in page_snap.rows:
                if doc_id in intent.exclusions:
                    portal.set_row(doc_id, False)
                    journal.step("exclude-row", {"id": doc_id, "page": page_snap.page})
            # 操作后重读：证据 rows 必须反映排除后的真实勾选，而非循环开始时的旧快照。
            after = portal.read_snapshot()
            checked = after.checked_ids
            evidence_path = journal.selection_evidence(
                f"p{page_snap.page}",
                list(checked),
                "全部匹配模式：取消勾选即排除项，逐行复选框为唯一成员事实来源",
            )
            journal.write_evidence_json(
                evidence_path,
                {
                    "page": page_snap.page,
                    "rows": [{"id": i, "checked": c} for i, c in after.rows],
                    "exclusions_on_page": sorted(set(intent.exclusions) & {i for i, _ in after.rows}),
                },
            )
            if self.capture_screenshots:
                journal.screenshot(portal.page, f"matching-page-{page_snap.page}")

        snap = portal.read_snapshot()
        if snap.mode != MATCHING:
            self._abort(
                journal,
                portal,
                reason="mode-lost",
                stage="post-selection",
                detail="选择过程退出了全部匹配模式",
                planned=sorted(planned),
                actual=snap.checked_ids,
                invalid_conditions=(f"当前模式 {snap.mode} != matching",),
            )
        # 统一的绑定/成员核对在 run() 中钩子执行后进行，这里只回读一次实时绑定。
        return planned, bound

    # ---------- 预览与生成 ----------
    def _preview_verify_and_generate(
        self, portal: PortalPage, journal: RunJournal, intent, planned: set, binding: dict
    ) -> RunResult:
        preview = portal.open_preview()
        journal.step("preview", {"preview": preview})
        journal.set_preview(preview)
        if self.capture_screenshots:
            journal.screenshot(portal.page, "preview")

        # 绑定核对：预览中的签名/修订必须与选择时绑定值一致（新增记录会推高修订）。
        invalid = []
        if preview.get("query") != binding["query"]:
            invalid.append(f"查询签名 {preview.get('query')!r} != 绑定 {binding['query']!r}")
        if preview.get("revision") != binding["revision"]:
            invalid.append(f"结果修订 {preview.get('revision')} != 绑定 {binding['revision']}")
        if preview.get("mode") != binding["mode"]:
            invalid.append(f"选择模式 {preview.get('mode')!r} != 绑定 {binding['mode']!r}")
        preview_members = set(preview.get("members", []))
        if binding["mode"] == MATCHING:
            expected_count = binding["total"] - len(getattr(intent, "exclusions", frozenset()))
            if len(preview_members) != expected_count:
                invalid.append(
                    f"预览成员数 {len(preview_members)} 与绑定匹配总数/排除数不符（应为 {expected_count}）"
                )
        diff = compare_sets(planned, preview_members)
        if invalid or not diff.exact:
            self._abort_with_diff(
                journal, portal, "preview-mismatch", "preview",
                "预览与计划集合不完全相等，拒绝点击生成",
                diff, invalid, binding,
            )

        # 生成前最后一次活页核对：筛选/列表在此刻变化同样撤回。
        actual, snap = self._read_all_checked(portal, "pre-generate", journal)
        self._check_binding(portal, journal, binding, planned, actual, "pre-generate")
        self._check_membership(journal, portal, planned, actual, "pre-generate", binding)

        # 只有预览与计划集合完全相等才点击生成。
        package_path = journal.package_dir / "资料包.json"
        package_id, package_path = portal.generate_and_download(package_path)
        journal.step("generate", {"package_id": package_id})

        manifest_doc = json.loads(package_path.read_text(encoding="utf-8"))
        if manifest_doc.get("package_id") != package_id:
            raise RecoveryError(f"资料包文件内 package_id 与下载控件不一致: {manifest_doc}")
        manifest = manifest_doc.get("manifest", {})

        # 生成后文件内 manifest 必须与已确认预览一致，才交给下游。
        if manifest != preview:
            self._abort(
                journal,
                portal,
                reason="manifest-mismatch",
                stage="post-generate",
                detail="资料包 manifest 与已确认预览不一致，资料包不予交付",
                planned=sorted(planned),
                actual=manifest.get("members", ()),
                invalid_conditions=("manifest != confirmed preview",),
            )
        final_diff = compare_sets(planned, manifest.get("members", []))
        if not final_diff.exact:
            self._abort_with_diff(
                journal, portal, "manifest-mismatch", "post-generate",
                "资料包 manifest 成员与计划集合不一致",
                final_diff, ("manifest 成员核对失败",), binding,
            )

        storage_path = portal.save_storage_state(journal.storage_state_path)
        journal.set_generated(package_id, manifest, package_path, storage_path)
        journal.step("verified", {"package_id": package_id, "members": manifest.get("members")})
        return RunResult(
            status="packaged",
            intent_key=journal.key,
            package_id=package_id,
            package_path=str(package_path),
            journal_path=journal.journal_path,
            preview=preview,
            manifest=manifest,
        )

    # ---------- 中断找回 ----------
    def recover(self, intent, *, page: Optional[Page] = None) -> RunResult:
        """按已记录 package_id 找回同包；不再次生成。"""
        journal = RunJournal.load(self.workdir, intent)
        package_id = journal.record.get("package_id")
        if journal.record.get("status") != "packaged" or not package_id:
            raise RecoveryError("台账未记录已生成的 package_id，属于预览之前中断，应重新规划运行")

        package_path = Path(journal.record["package_path"])
        if not package_path.exists():
            raise RecoveryError(f"台账指向的资料包文件缺失: {package_path}")
        manifest_doc = json.loads(package_path.read_text(encoding="utf-8"))
        manifest = manifest_doc.get("manifest", {})
        preview = journal.record.get("preview")
        if manifest != preview:
            raise RecoveryError("本地资料包 manifest 与台账预览不一致，拒绝交付")
        # 意图一致性：显式模式比对编号集合；条件模式比对查询签名（排除语义已固化在 manifest 成员里）。
        if intent.mode == EXPLICIT:
            if set(manifest.get("members", [])) != set(intent.ids):
                raise RecoveryError("本地资料包成员与显式意图编号集合不一致")
        else:
            if manifest.get("query") != intent.query or manifest.get("mode") != MATCHING:
                raise RecoveryError("本地资料包查询签名与条件意图不一致")
            if set(manifest.get("members", [])) & set(intent.exclusions):
                raise RecoveryError("本地资料包仍包含意图声明的排除项")

        context = None
        own_page = page is None
        if own_page:
            context = self._new_context()
            page = context.new_page()
        portal = PortalPage(page, timeout_ms=self.timeout_ms)
        try:
            # 用保存的来源状态恢复 localStorage，再通过页面“已生成资料包”控件找回同包。
            if not journal.storage_state_path.exists():
                raise RecoveryError("缺少 storage-state.json，无法在页面侧验证 package_id")
            portal.restore_packages(_read_storage_packages(journal.storage_state_path))
            portal.open(self.base_url)
            ids = portal.list_generated_packages()
            if package_id not in ids:
                raise RecoveryError(f"页面已生成列表中找不到 {package_id}，无法确认同包")
            re_download = journal.package_dir / f"recovered-{package_id}.json"
            portal.download_package_by_id(package_id, re_download)
            recovered_doc = json.loads(re_download.read_text(encoding="utf-8"))
            if recovered_doc.get("package_id") != package_id or recovered_doc.get("manifest") != manifest:
                raise RecoveryError("页面找回的资料包与台账 manifest 不一致")
            journal.step("recover", {"package_id": package_id, "re_download": str(re_download)})
            journal.flush()
        finally:
            if own_page and context is not None:
                context.close()

        return RunResult(
            status="packaged",
            intent_key=journal.key,
            package_id=package_id,
            package_path=str(package_path),
            journal_path=journal.journal_path,
            preview=preview,
            manifest=manifest,
            recovered=True,
        )

    # ---------- 核对工具 ----------
    def _read_all_checked(self, portal: PortalPage, label: str, journal: RunJournal):
        """回首页后逐页读取全部勾选事实；三态框不参与成员推断。

        完整核对必须覆盖每一页，故先正常翻回第一页再顺序向下；门户的翻页控件
        不改变筛选与逐行选择（已用浏览器检查证明）。
        """
        collected: set[str] = set()
        last_snap = None
        for snap in portal.iter_pages(reset=True):
            collected.update(snap.checked_ids)
            last_snap = snap
            journal.step(
                "membership-read",
                {"label": label, "page": snap.page, "checked": sorted(snap.checked_ids)},
            )
        return collected, last_snap

    def _check_live_binding(self, portal, journal, binding, planned, actual):
        """预览前只核对可在列表上直接观察的签名与模式。"""
        snap = portal.read_snapshot()
        invalid = []
        if snap.signature != binding["query"]:
            invalid.append(f"查询签名 {snap.signature!r} != 绑定 {binding['query']!r}")
        if snap.mode != binding["mode"]:
            invalid.append(f"选择模式 {snap.mode!r} != 绑定 {binding['mode']!r}")
        if invalid:
            diff = compare_sets(planned, actual)
            self._abort(
                journal,
                portal,
                reason="binding-changed",
                stage="post-selection",
                detail="筛选签名或选择模式相对绑定值发生变化",
                planned=sorted(planned),
                actual=sorted(actual),
                invalid_conditions=tuple(invalid),
                extra=diff.extra,
                missing=diff.missing,
            )

    def _check_binding(self, portal, journal, binding, planned, actual, stage, include_diff=False):
        # 现场重读，避免使用翻页遍历遗留在闭包中的旧快照。
        snap = portal.read_snapshot()
        invalid = []
        if snap.signature != binding["query"]:
            invalid.append(f"查询签名 {snap.signature!r} != 绑定 {binding['query']!r}")
        if snap.revision != binding["revision"]:
            invalid.append(f"结果修订 {snap.revision} != 绑定 {binding['revision']}")
        if binding["mode"] == MATCHING and snap.total != binding["total"]:
            invalid.append(f"匹配总数 {snap.total} != 绑定 {binding['total']}")
        if snap.mode != binding["mode"]:
            invalid.append(f"选择模式 {snap.mode!r} != 绑定 {binding['mode']!r}")
        if invalid:
            if include_diff:
                diff = compare_sets(planned, actual)
                self._abort(
                    journal,
                    portal,
                    reason="binding-changed",
                    stage=stage,
                    detail="筛选/列表修订/匹配总数相对绑定值发生变化",
                    planned=sorted(planned),
                    actual=sorted(actual),
                    invalid_conditions=tuple(invalid),
                    extra=diff.extra,
                    missing=diff.missing,
                )
            self._abort(
                journal,
                portal,
                reason="binding-changed",
                stage=stage,
                detail="筛选/列表修订/匹配总数相对绑定值发生变化",
                planned=sorted(planned),
                actual=sorted(actual),
                invalid_conditions=tuple(invalid),
            )

    def _check_membership(self, journal, portal, planned, actual, stage, binding):
        diff = compare_sets(planned, actual)
        if not diff.exact:
            self._abort_with_diff(
                journal, portal, "membership-mismatch", stage,
                "逐行复选框读出的实际成员与计划集合不一致",
                diff, (), binding,
            )

    def _abort_with_diff(self, journal, portal, reason, stage, detail, diff: SelectionDiff,
                         invalid_conditions, binding):
        self._abort(
            journal,
            portal,
            reason=reason,
            stage=stage,
            detail=detail,
            planned=diff.planned,
            actual=diff.actual,
            invalid_conditions=tuple(invalid_conditions),
            extra=diff.extra,
            missing=diff.missing,
        )

    def _abort(self, journal, portal, *, reason, stage, detail, planned=(), actual=(),
               invalid_conditions=(), extra=(), missing=()):
        binding_now = portal.read_snapshot().as_binding()
        diff = compare_sets(planned, actual)
        report = AbortReport(
            reason=reason,
            stage=stage,
            detail=detail,
            planned=tuple(planned),
            extra=tuple(extra) if extra else diff.extra,
            missing=tuple(missing) if missing else diff.missing,
            invalid_conditions=tuple(invalid_conditions),
            binding=binding_now,
        )
        journal.set_aborted(report.to_dict())
        if self.capture_screenshots:
            try:
                journal.screenshot(portal.page, f"abort-{stage}-{reason}")
            except Exception:
                pass
        raise AbortError(report)

    @staticmethod
    def _planned_ids(intent) -> set:
        if intent.mode == EXPLICIT:
            return set(intent.ids)
        # 条件模式在运行前无法得知全集；仅用于缺项报告的占位语义。
        return set(intent.exclusions)


def _read_storage_packages(path: Path) -> str:
    state = json.loads(path.read_text(encoding="utf-8"))
    for origin in state.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == "packages":
                return item["value"]
    raise RecoveryError("storage-state 中没有 packages 记录")
