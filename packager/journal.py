"""运行台账：选择核对记录、截图、预览与 manifest 的落盘，以及中断找回依据。

工作目录布局（默认 ./state/packager/<intent_key>/）::

    run.json          运行记录（原子写入）
    storage-state.json 打包成功后保存的浏览器来源状态（localStorage 内含 package_id）
    package/资料包.json 下载回的资料包
    evidence/         每个操作步骤的页面截图与选择核对快照
    index.json        所有运行的索引（位于工作目录根部）
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .intent import intent_key


@dataclass
class RunResult:
    """一次运行的最终结论。"""

    status: str  # packaged | aborted | interrupted
    intent_key: str
    package_id: Optional[str]
    package_path: Optional[str]
    journal_path: Path
    preview: Optional[dict] = None
    manifest: Optional[dict] = None
    recovered: bool = False
    abort: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "intent_key": self.intent_key,
            "package_id": self.package_id,
            "package_path": str(self.package_path) if self.package_path else None,
            "journal_path": str(self.journal_path),
            "preview": self.preview,
            "manifest": self.manifest,
            "recovered": self.recovered,
            "abort": self.abort,
        }


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class RunJournal:
    """一次打包运行的台账；记录只追加，关键字段更新时整体原子替换。"""

    def __init__(self, workdir: Path, intent, *, capture_screenshots: bool = True):
        self.intent = intent
        self.key = intent_key(intent)
        self.capture_screenshots = capture_screenshots
        self.dir = Path(workdir) / self.key
        self.evidence_dir = self.dir / "evidence"
        self.package_dir = self.dir / "package"
        self.journal_path = self.dir / "run.json"
        self.storage_state_path = self.dir / "storage-state.json"
        self.index_path = Path(workdir) / "index.json"
        self.record: dict = {
            "intent_key": self.key,
            "intent": self._intent_payload(),
            "status": "planned",
            "steps": [],
            "selection_evidence": [],
            "binding": None,
            "preview": None,
            "package_id": None,
            "manifest": None,
            "abort": None,
            "interrupted": False,
        }
        self.dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.package_dir.mkdir(parents=True, exist_ok=True)
        # 新规划从空白记录开始；若存在旧记录则归档，避免与历史运行混淆。
        if self.journal_path.exists():
            old = json.loads(self.journal_path.read_text(encoding="utf-8"))
            _atomic_write_json(self.dir / f"run.superseded-{old.get('status', 'x')}.json", old)
        self.flush()

    def _intent_payload(self) -> dict:
        from .intent import EXPLICIT

        if self.intent.mode == EXPLICIT:
            return {"mode": self.intent.mode, "ids": sorted(self.intent.ids)}
        return {
            "mode": self.intent.mode,
            "query": self.intent.query,
            "exclusions": sorted(self.intent.exclusions),
        }

    def flush(self) -> None:
        _atomic_write_json(self.journal_path, self.record)

    def step(self, name: str, detail: dict) -> None:
        entry = {"step": name, **detail}
        self.record["steps"].append(entry)
        self.flush()

    def selection_evidence(self, page_label: str, checked: list, note: str) -> Path:
        """记录一次选择核对：实际勾选编号来自逐行复选框，绝不使用三态框推断。"""
        entry = {"page": page_label, "checked": sorted(checked), "count": len(checked), "note": note}
        self.record["selection_evidence"].append(entry)
        self.flush()
        return self.evidence_dir / f"selection-{len(self.record['selection_evidence']):02d}-{page_label}.json"

    def write_evidence_json(self, path: Path, payload: Any) -> None:
        _atomic_write_json(path, payload)

    def screenshot(self, page, name: str) -> Optional[Path]:
        if not self.capture_screenshots:
            return None
        target = self.evidence_dir / f"{len(self.record['steps']):02d}-{name}.png"
        page.screenshot(path=str(target), full_page=True)
        return target

    def set_binding(self, binding: dict) -> None:
        self.record["binding"] = binding
        self.flush()

    def set_preview(self, preview: dict) -> None:
        self.record["preview"] = preview
        self.flush()

    def set_generated(self, package_id: str, manifest: dict, package_path: Path, storage_state_path: Path) -> None:
        self.record["package_id"] = package_id
        self.record["manifest"] = manifest
        self.record["package_path"] = str(package_path)
        self.record["storage_state_path"] = str(storage_state_path)
        self.record["status"] = "packaged"
        self.record["interrupted"] = False
        self.flush()
        self._update_index()

    def set_aborted(self, abort: dict) -> None:
        self.record["status"] = "aborted"
        self.record["abort"] = abort
        self.flush()
        self._update_index()

    def mark_interrupted(self) -> None:
        if self.record["status"] == "planned":
            self.record["interrupted"] = True
            self.flush()
            self._update_index()

    def _update_index(self) -> None:
        index = []
        if self.index_path.exists():
            try:
                index = json.loads(self.index_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                index = []
        index = [item for item in index if item.get("intent_key") != self.key]
        index.append(
            {
                "intent_key": self.key,
                "status": self.record["status"],
                "package_id": self.record.get("package_id"),
                "journal": str(self.journal_path),
            }
        )
        _atomic_write_json(self.index_path, index)

    @classmethod
    def load(cls, workdir: Path, intent) -> "RunJournal":
        """找回场景：直接装载既有台账，不重新初始化。"""
        key = intent_key(intent)
        path = Path(workdir) / key / "run.json"
        if not path.exists():
            raise FileNotFoundError(f"找不到运行台账: {path}")
        journal = cls.__new__(cls)
        journal.intent = intent
        journal.key = key
        journal.capture_screenshots = True
        journal.dir = Path(workdir) / key
        journal.evidence_dir = journal.dir / "evidence"
        journal.package_dir = journal.dir / "package"
        journal.journal_path = path
        journal.storage_state_path = journal.dir / "storage-state.json"
        journal.index_path = Path(workdir) / "index.json"
        journal.record = json.loads(path.read_text(encoding="utf-8"))
        return journal
