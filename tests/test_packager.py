"""证据打包模块的单元与浏览器端到端检查。

场景覆盖：

- 两种意图的模式混用拒绝、缺项报告；
- 显式模式跨页选择（不在第一页的目标可选中）；
- 条件模式绑定签名/修订/总数，排除项跨页保持、不会被重新勾回；
- 筛选变化、预览前新增记录（修订变化）、预览出现未计划资料时撤回且不生成；
- 中断找回：预览之前可重新规划，已生成之后按 package_id 找回同包、不重复打包；
- 三态“当前页全选”只反映当前页，不用于推断全部成员。
"""
import json
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright, expect

from packager import (
    AbortError,
    EvidencePackager,
    ExplicitIntent,
    IntentError,
    MatchingIntent,
    build_intent,
    compare_sets,
    intent_from_dict,
    intent_key,
)
from packager.journal import RunJournal
from portal.__main__ import running


def post_control(base, payload):
    request = Request(
        base + "/__control",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        return json.load(response)


class IntentModelTests(unittest.TestCase):
    def test_two_modes_are_mutually_exclusive(self):
        with self.assertRaises(IntentError):
            build_intent(ids=["DOC-01"], query="review")
        with self.assertRaises(IntentError):
            intent_from_dict({"mode": "explicit", "ids": ["DOC-01"], "exclusions": []})
        with self.assertRaises(IntentError):
            intent_from_dict({"mode": "matching", "query": "review", "ids": ["DOC-01"]})
        with self.assertRaises(IntentError):
            intent_from_dict({"mode": "matching", "exclusions": ["DOC-01"]})

    def test_builder_and_key_stability(self):
        explicit = build_intent(ids=["DOC-02", "DOC-02", "DOC-09"])
        self.assertIsInstance(explicit, ExplicitIntent)
        self.assertEqual(intent_key(explicit), intent_key(build_intent(ids=["DOC-09", "DOC-02"])))
        matching = build_intent(query="review", exclusions=["DOC-07"])
        self.assertIsInstance(matching, MatchingIntent)
        self.assertEqual(matching.query, "review")
        empty_exclusion = build_intent(query="review")
        self.assertEqual(frozenset(), empty_exclusion.exclusions)

    def test_diff_reports_extra_and_missing(self):
        diff = compare_sets(["A", "B"], ["B", "C"])
        self.assertEqual(("C",), diff.extra)
        self.assertEqual(("A",), diff.missing)
        self.assertFalse(diff.exact)


class BrowserPackagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(headless=True, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.server = running()
        self.base, self.state = self.server.__enter__()
        self.workdir = Path("state/packager-test") / self.id().split(".")[-1]

    def tearDown(self):
        self.server.__exit__(None, None, None)

    def _packager(self):
        return EvidencePackager(self.base, self.workdir, capture_screenshots=True,
                                browser=self.browser)

    def _open_page(self):
        context = self.browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 900})
        page = context.new_page()
        return context, page

    # ---------- 显式模式 ----------
    def test_01_explicit_cross_page_selection(self):
        # DOC-09 位于第 3 页（每页 4 条），验证不在第一页的目标也能通过翻页选中。
        intent = build_intent(ids=["DOC-01", "DOC-05", "DOC-09"])
        with self._packager() as packager:
            result = packager.run(intent)
        self.assertEqual("packaged", result.status)
        self.assertEqual(sorted(["DOC-01", "DOC-05", "DOC-09"]), result.manifest["members"])
        self.assertEqual("explicit", result.manifest["mode"])
        journal = json.loads(Path(result.journal_path).read_text(encoding="utf-8"))
        pages_visited = {s["page"] for s in journal["steps"] if s["step"] == "page-select"}
        self.assertEqual({1, 2, 3}, pages_visited)
        # 选择核对记录来自逐行复选框，且覆盖目标所在的三页。
        evidence_pages = {e["page"] for e in journal["selection_evidence"]}
        self.assertEqual({"p1", "p2", "p3"}, evidence_pages)

    def test_02_explicit_missing_ids_abort_without_shrinking(self):
        intent = build_intent(ids=["DOC-01", "DOC-99", "DOC-42"])
        with self._packager() as packager:
            with self.assertRaises(AbortError) as caught:
                packager.run(intent)
        report = caught.exception.report
        self.assertEqual("missing-explicit-ids", report.reason)
        self.assertEqual(["DOC-42", "DOC-99"], list(report.missing))
        self.assertTrue(any("DOC-99" in item for item in report.invalid_conditions))
        self.assertTrue(any("DOC-42" in item for item in report.invalid_conditions))
        # 缺项时绝不生成资料包。
        self.assertFalse(any(a["name"] == "package" for a in self.state["actions"]))

    # ---------- 条件模式 ----------
    def test_03_matching_mode_with_cross_page_exclusions(self):
        # review 共 6 条（01,03,05,07,09,11），排除第一页的 01 与第三页的 09。
        intent = build_intent(query="review", exclusions=["DOC-01", "DOC-09"])
        context, page = self._open_page()
        try:
            with self._packager() as packager:
                result = packager.run(intent, page=page)
            self.assertEqual("packaged", result.status)
            self.assertEqual(
                sorted(["DOC-03", "DOC-05", "DOC-07", "DOC-11"]), result.manifest["members"]
            )
            self.assertEqual("review", result.manifest["query"])
            self.assertEqual(1, result.manifest["revision"])
            journal = json.loads(Path(result.journal_path).read_text(encoding="utf-8"))
            self.assertEqual(6, journal["binding"]["total"])
            # 排除项在选择核对中始终未勾选，证明不会被“全部匹配”重新勾回。
            for evidence in journal["selection_evidence"]:
                self.assertNotIn("DOC-01", evidence["checked"])
                self.assertNotIn("DOC-09", evidence["checked"])
            # 页面在生成所用的同一上下文中最终仍处于 matching 模式（模式状态只活在页面生命周期）。
            summary = page.locator("#summary").inner_text()
            self.assertIn("模式 matching", summary)
            self.assertIn("查询 review", summary)
        finally:
            context.close()

    def test_04_matching_exclusion_outside_match_aborts(self):
        # DOC-02 是 archived，不属于 review 匹配集合。
        intent = build_intent(query="review", exclusions=["DOC-02"])
        with self._packager() as packager:
            with self.assertRaises(AbortError) as caught:
                packager.run(intent)
        self.assertEqual("exclusion-not-in-match", caught.exception.report.reason)
        self.assertFalse(any(a["name"] == "package" for a in self.state["actions"]))

    # ---------- 撤回场景 ----------
    def test_05_filter_change_after_selection_aborts(self):
        intent = build_intent(query="review", exclusions=["DOC-01"])
        context, page = self._open_page()
        try:
            with self._packager() as packager:
                def switch_filter(portal, journal):
                    # 模拟“切换筛选后沿用旧勾选”：通过页面正常控件改筛选。
                    portal.set_filter("all")

                with self.assertRaises(AbortError) as caught:
                    packager.run(intent, page=page, after_selection=switch_filter)
        finally:
            context.close()
        report = caught.exception.report
        self.assertEqual("binding-changed", report.reason)
        self.assertTrue(any("查询签名" in item for item in report.invalid_conditions))
        self.assertFalse(any(a["name"] == "package" for a in self.state["actions"]))

    def test_06_new_record_revision_change_aborts_at_preview(self):
        # 门户契约：首次点击预览时新增 DOC-NEW 并推高修订，预览必须因此撤回。
        intent = build_intent(ids=["DOC-01", "DOC-03"])
        post_control(self.base, {"phase": "new-record"})
        context, page = self._open_page()
        try:
            with self._packager() as packager:
                with self.assertRaises(AbortError) as caught:
                    packager.run(intent, page=page)
        finally:
            context.close()
        report = caught.exception.report
        self.assertEqual("preview-mismatch", report.reason)
        # 显式模式下新增记录默认未勾选，不进入成员，但会推高结果修订导致绑定失效。
        self.assertEqual([], list(report.extra))
        self.assertEqual(sorted(["DOC-01", "DOC-03"]), list(report.planned))
        self.assertTrue(any("结果修订" in item for item in report.invalid_conditions))
        self.assertFalse(any(a["name"] == "package" for a in self.state["actions"]))

    def test_07_unplanned_preview_member_aborts(self):
        # 条件模式选择 review-排除01 后注入新增记录，预览多出 DOC-NEW。
        intent = build_intent(query="review", exclusions=["DOC-01"])
        post_control(self.base, {"phase": "new-record"})
        context, page = self._open_page()
        try:
            with self._packager() as packager:
                with self.assertRaises(AbortError) as caught:
                    packager.run(intent, page=page)
        finally:
            context.close()
        report = caught.exception.report
        self.assertEqual("preview-mismatch", report.reason)
        self.assertIn("DOC-NEW", report.extra)
        self.assertFalse(any(a["name"] == "package" for a in self.state["actions"]))

    # ---------- 旧勾选防护 ----------
    def test_08_stale_workspace_selection_aborts(self):
        intent = build_intent(ids=["DOC-03"])
        context, page = self._open_page()
        try:
            page.goto(self.base)
            expect(page.locator("#status")).not_to_have_text("页面加载中")
            # 在同一来源上先制造一条旧勾选，再把该页面交给打包器。
            page.check("[data-id=DOC-01]")
            with EvidencePackager(self.base, self.workdir, browser=self.browser) as packager:
                with self.assertRaises(AbortError) as caught:
                    packager.run(intent, page=page)
        finally:
            context.close()
        self.assertEqual("stale-selection", caught.exception.report.reason)
        self.assertIn("DOC-01", caught.exception.report.extra)
        self.assertFalse(any(a["name"] == "package" for a in self.state["actions"]))

    # ---------- 三态语义 ----------
    def test_09_page_all_only_covers_current_page(self):
        from packager.portal_page import PortalPage

        context, page = self._open_page()
        try:
            portal = PortalPage(page)
            portal.open(self.base)
            portal.click_page_all(True)
            checked, indeterminate = portal.page_all_state()
            self.assertTrue(checked)
            self.assertFalse(indeterminate)
            portal.goto_page(2)
            # 第二页没有任何勾选：三态框只描述当前页。
            self.assertEqual((), portal.read_snapshot().checked_ids)
            checked2, _ = portal.page_all_state()
            self.assertFalse(checked2)
        finally:
            context.close()

    # ---------- 中断与找回 ----------
    def test_10_interrupt_before_preview_can_replan(self):
        work = self.workdir
        intent = build_intent(ids=["DOC-01", "DOC-11"])
        context, page = self._open_page()
        try:
            with EvidencePackager(self.base, work, browser=self.browser) as packager:
                def interrupt(portal, journal):
                    journal.mark_interrupted()
                    raise KeyboardInterrupt

                with self.assertRaises(KeyboardInterrupt):
                    packager.run(intent, page=page, after_selection=interrupt)
        finally:
            context.close()
        key = intent_key(intent)
        record = json.loads((work / key / "run.json").read_text(encoding="utf-8"))
        self.assertEqual("planned", record["status"])
        self.assertTrue(record["interrupted"])
        self.assertIsNone(record["package_id"])
        # 重新规划运行：同一意图目录重新运行并成功生成。
        with EvidencePackager(self.base, work, browser=self.browser) as packager:
            result = packager.run(intent)
        self.assertEqual("packaged", result.status)
        self.assertEqual(sorted(["DOC-01", "DOC-11"]), result.manifest["members"])

    def test_11_recover_after_generation_uses_same_package_id(self):
        work = self.workdir
        intent = build_intent(query="review", exclusions=["DOC-03"])
        with EvidencePackager(self.base, work, browser=self.browser) as packager:
            first = packager.run(intent)
        package_actions_before = [a for a in self.state["actions"] if a["name"] == "package"]
        self.assertEqual(1, len(package_actions_before))

        with EvidencePackager(self.base, work, browser=self.browser) as packager:
            recovered = packager.recover(intent)
        self.assertTrue(recovered.recovered)
        self.assertEqual(first.package_id, recovered.package_id)
        self.assertEqual(first.manifest, recovered.manifest)
        # 没有产生第二次 package 动作：不重复打包。
        package_actions_after = [a for a in self.state["actions"] if a["name"] == "package"]
        self.assertEqual(1, len(package_actions_after))
        self.assertTrue(
            (work / intent_key(intent) / "package" / f"recovered-{first.package_id}.json").exists()
        )

    def test_12_manifest_inside_file_matches_confirmed_preview(self):
        intent = build_intent(ids=["DOC-02", "DOC-07"])
        with self._packager() as packager:
            result = packager.run(intent)
        package_doc = json.loads(Path(result.package_path).read_text(encoding="utf-8"))
        self.assertEqual(result.package_id, package_doc["package_id"])
        self.assertEqual(result.preview, package_doc["manifest"])
        self.assertEqual(sorted(["DOC-02", "DOC-07"]), package_doc["manifest"]["members"])


if __name__ == "__main__":
    unittest.main()
