"""门户页面控件封装。

约束：

- 只通过页面可见控件（筛选下拉、翻页按钮、逐行复选框、全选按钮、预览/生成按钮）操作；
- 成员事实只从逐行复选框的勾选状态读取，三态的“当前页全选”只用于演示与对照，
  绝不据其推断全部成员；
- 不调用 ``/__control``、``/api/action`` 等运维/内部接口，也不向页面注入选择集合。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from playwright.sync_api import Page, expect

_SUMMARY_RE = re.compile(
    r"查询\s*(?P<query>\S+)\s*；\s*修订\s*(?P<revision>\d+)\s*；\s*匹配\s*(?P<total>\d+)\s*；"
    r"第\s*(?P<page>\d+)\s*页\s*；\s*模式\s*(?P<mode>[a-z]+)"
)


@dataclass(frozen=True)
class PageSnapshot:
    """某一时刻当前页的只读快照。"""

    query: str
    revision: int
    total: int
    page: int  # 1 起
    pages: int
    mode: str
    signature: str
    rows: Tuple[Tuple[str, bool], ...]
    page_size: int

    @property
    def checked_ids(self) -> Tuple[str, ...]:
        return tuple(doc_id for doc_id, checked in self.rows if checked)

    def as_binding(self) -> dict:
        return {"query": self.query, "revision": self.revision, "total": self.total, "mode": self.mode}


class PortalPage:
    """页面控件的薄封装；所有动作都对应真实用户操作。"""

    def __init__(self, page: Page, *, timeout_ms: int = 15000):
        self.page = page
        self.timeout_ms = timeout_ms
        self.page.set_default_timeout(timeout_ms)
        # 满页容量在打开页面时确定；末页可能不足一屏，不能用末页行数反推总页数。
        self._full_page_size: Optional[int] = None

    # ---------- 打开与读取 ----------
    def open(self, base_url: str) -> None:
        self.page.goto(base_url)
        self._wait_ready()
        first = self.read_snapshot()
        self._full_page_size = first.page_size

    def ensure_ready(self, base_url: str) -> bool:
        """页面若已停留在本门户且加载完成则不重载，返回 False；否则打开并返回 True。

        不重载是为了保留调用方页面上既有的选择状态，使陈旧勾选检测真正生效。
        """
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        try:
            current = self.page.url.split("?", 1)[0].rstrip("/")
            target = base_url.rstrip("/")
            if current == target or current.startswith(target + "/"):
                self.page.wait_for_selector(
                    "#records input[data-id]", timeout=self.timeout_ms
                )
                self._wait_ready()
                if self._full_page_size is None:
                    self._full_page_size = self.read_snapshot().page_size
                return False
        except PlaywrightTimeout:
            # 页面在本门户但尚未就绪：重新打开以获得确定的初始状态。
            pass
        self.open(base_url)
        return True

    def _wait_ready(self) -> None:
        expect(self.page.locator("#status")).not_to_have_text("页面加载中")
        expect(self.page.locator("#records input[data-id]").first).to_be_visible()

    def read_snapshot(self) -> PageSnapshot:
        summary = self.page.locator("#summary").inner_text()
        match = _SUMMARY_RE.search(summary)
        if not match:
            raise RuntimeError(f"无法解析页面摘要: {summary!r}")
        records = self.page.locator("#records")
        signature = records.get_attribute("data-signature")
        revision_attr = records.get_attribute("data-revision")
        rows = tuple(
            (doc_id, checked)
            for doc_id, checked in self.page.eval_on_selector_all(
                "#records input[type=checkbox][data-id]",
                "els => els.map(e => [e.dataset.id, e.checked])",
            )
        )
        page_size = len(rows)
        total = int(match.group("total"))
        current = int(match.group("page"))
        # 首页行数即满页容量（门户首页恒为满屏）；末页可能不足一屏，不能用末页行数反推。
        if current == 1 and page_size:
            self._full_page_size = page_size
        full_size = self._full_page_size or page_size
        pages = max(1, (total + full_size - 1) // full_size) if full_size else 1
        snapshot = PageSnapshot(
            query=match.group("query"),
            revision=int(match.group("revision")),
            total=total,
            page=current,
            pages=pages,
            mode=match.group("mode"),
            signature=signature or "",
            rows=rows,
            page_size=page_size,
        )
        if snapshot.signature != snapshot.query:
            raise RuntimeError(
                f"查询签名不一致：摘要={snapshot.query!r}，data-signature={snapshot.signature!r}"
            )
        if str(snapshot.revision) != revision_attr:
            raise RuntimeError(
                f"结果修订不一致：摘要={snapshot.revision}，data-revision={revision_attr}"
            )
        return snapshot

    # ---------- 筛选与翻页 ----------
    def set_filter(self, value: str) -> PageSnapshot:
        self.page.locator("#filter").select_option(value)
        expect(self.page.locator("#records")).to_have_attribute("data-signature", value)
        return self.read_snapshot()

    def filter_options(self) -> List[str]:
        return self.page.eval_on_selector_all("#filter option", "els => els.map(e => e.value)")

    def goto_first(self) -> None:
        snap = self.read_snapshot()
        while snap.page > 1:
            self.page.click("#prev")
            self._wait_page_number(snap.page - 1)
            snap = self.read_snapshot()

    def _wait_page_number(self, expected: int) -> None:
        expect(self.page.locator("#summary")).to_contain_text(f"第 {expected} 页")

    def iter_pages(self, *, reset: bool = True) -> Iterator[PageSnapshot]:
        """逐页生成快照；通过“下一页”按钮正常翻页。

        reset=True 时先回到第一页（用于完整扫描）；reset=False 时从当前页顺序
        向后遍历，不触发任何“上一页”动作，避免翻页控件对筛选/选择状态的联动影响。
        """
        if reset:
            self.goto_first()
        while True:
            snap = self.read_snapshot()
            yield snap
            if snap.page >= snap.pages:
                return
            self.page.click("#next")
            self._wait_page_number(snap.page + 1)

    def goto_page(self, target: int) -> PageSnapshot:
        """target 为 1 起页码。"""
        snap = self.read_snapshot()
        while snap.page < target:
            self.page.click("#next")
            self._wait_page_number(snap.page + 1)
            snap = self.read_snapshot()
        while snap.page > target:
            self.page.click("#prev")
            self._wait_page_number(snap.page - 1)
            snap = self.read_snapshot()
        return snap

    # ---------- 逐行选择（真实控件） ----------
    def set_row(self, doc_id: str, checked: bool) -> None:
        locator = self.page.locator(f"#records input[data-id='{doc_id}']")
        if checked:
            locator.check()
            expect(locator).to_be_checked()
        else:
            locator.uncheck()
            expect(locator).not_to_be_checked()

    def click_all_matching(self) -> None:
        """“选择全部匹配项”按钮：进入 matching 模式。"""
        self.page.click("#all-matching")
        snap = self.read_snapshot()
        if snap.mode != "matching":
            raise RuntimeError("点击“选择全部匹配项”后模式未切换为 matching")

    def click_page_all(self, checked: bool) -> None:
        """“当前页全选”三态复选框；只操作当前页，语义上不是全部匹配。"""
        locator = self.page.locator("#page-all")
        if checked:
            locator.check()
        else:
            locator.uncheck()

    def page_all_state(self) -> Tuple[bool, bool]:
        """返回 (checked, indeterminate)，仅用于演示三态语义。"""
        return self.page.locator("#page-all").is_checked(), self.page.evaluate(
            "document.querySelector('#page-all').indeterminate"
        )

    def assert_clean_workspace(self) -> Tuple[str, ...]:
        """翻全部页，返回仍被勾选的编号；非空说明工作区带着旧选择。"""
        stale: List[str] = []
        for snap in self.iter_pages(reset=False):
            stale.extend(snap.checked_ids)
        return tuple(sorted(stale))

    # ---------- 预览与生成 ----------
    def open_preview(self) -> dict:
        self.page.click("#preview")
        expect(self.page.locator("#members")).to_contain_text("members")
        return json.loads(self.page.locator("#members").inner_text())

    def generate_and_download(self, target: Path) -> Tuple[str, Path]:
        """点击生成并保存资料包，返回 (package_id, 保存路径)。

        门户用 blob: 锚点提供下载，无头 Chromium 对 blob URL 不保证触发 download
        事件，因此点击后从下载控件的 blob 地址在页面上下文取回字节；点击生成这一
        正常控件动作不变，文件内容与用户点击下载完全同源。
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        self.page.click("#generate")
        expect(self.page.locator("#download")).to_have_attribute(
            "data-package-id", re.compile(r"PK-\d+")
        )
        package_id = self.page.locator("#download").get_attribute("data-package-id")
        blob_url = self.page.locator("#download").get_attribute("href")
        if not blob_url:
            raise RuntimeError("生成后下载控件缺少 blob 地址")
        self._save_blob(blob_url, target)
        return package_id, target

    def _save_blob(self, blob_url: str, target: Path) -> None:
        raw = self.page.evaluate(
            """async (url) => {
                const response = await fetch(url);
                if (!response.ok) throw new Error('blob 取回失败 ' + response.status);
                const bytes = new Uint8Array(await response.arrayBuffer());
                let binary = '';
                const chunk = 0x8000;
                for (let i = 0; i < bytes.length; i += chunk) {
                    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
                }
                return btoa(binary);
            }""",
            blob_url,
        )
        import base64

        target.write_bytes(base64.b64decode(raw))

    def list_generated_packages(self) -> List[str]:
        return self.page.eval_on_selector_all(
            "#packages a[data-package-id]", "els => els.map(e => e.dataset.packageId)"
        )

    def download_package_by_id(self, package_id: str, target: Path) -> Path:
        """从“已生成资料包”区域找回同包并下载，不再次点击生成。

        同样通过锚点的 blob 地址在页面上下文取件。
        """
        locator = self.page.locator(f"#packages a[data-package-id='{package_id}']")
        locator.click()
        blob_url = locator.get_attribute("href")
        if not blob_url:
            raise RuntimeError("找回锚点缺少 blob 地址")
        self._save_blob(blob_url, target)
        return target

    def save_storage_state(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.context.storage_state(path=str(path))
        return path

    def restore_packages(self, packages_json: str) -> None:
        """中断找回时把已保存的 packages 记录写回新上下文的 localStorage。"""
        # 在页面脚本执行前注入；打开门户后 renderPackages 即读取同一份记录。
        # JSON 仅含合成编号与清单，内联为 JS 字符串字面量避免参数传递问题。
        literal = json.dumps(packages_json, ensure_ascii=False)
        self.page.add_init_script(script=f"localStorage.setItem('packages', {literal});")

    def screenshot(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True)
        return path
