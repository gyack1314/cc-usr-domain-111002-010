"""两种审查意图的端到端运行示例。

通过 ``python -I examples/run_examples.py`` 运行（隔离模式，避免工作目录注入）。
脚本在临时端口启动本地合成门户，依次演示：

1. 显式编号模式（含不在第一页的目标 DOC-09）；
2. 条件模式（review 筛选 + 跨页排除项）；
3. 撤回场景：筛选变化与预览前新增记录，确认不会生成错误资料包；
4. 中断找回：已生成之后按 package_id 找回同包，不重复打包。

所有产物写入 outputs/examples/<意图指纹>/，包含 run.json 台账、选择核对证据、
截图、资料包及其中 manifest。
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from packager import AbortError, EvidencePackager, build_intent, intent_key
from portal.__main__ import make_server

WORKDIR = Path("outputs/examples")


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    server = make_server(port=0)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    print("本地合成门户已启动：", base)

    # ---------- 示例一：显式编号模式 ----------
    banner("示例一：显式编号模式 explicit（DOC-09 在第 3 页，不在第一页）")
    explicit = build_intent(ids=["DOC-01", "DOC-05", "DOC-09"])
    with EvidencePackager(base, WORKDIR) as packager:
        result_explicit = packager.run(explicit)
    print("package_id :", result_explicit.package_id)
    print("manifest   :", json.dumps(result_explicit.manifest, ensure_ascii=False))
    package_doc = json.loads(Path(result_explicit.package_path).read_text(encoding="utf-8"))
    print("文件内 manifest 与确认预览一致：", package_doc["manifest"] == result_explicit.preview)

    # ---------- 示例二：条件模式 ----------
    banner("示例二：条件模式 matching（query=review，排除 DOC-01 与 DOC-09）")
    matching = build_intent(query="review", exclusions=["DOC-01", "DOC-09"])
    with EvidencePackager(base, WORKDIR) as packager:
        result_matching = packager.run(matching)
    print("package_id :", result_matching.package_id)
    print("绑定签名/修订/总数：", json.loads(Path(result_matching.journal_path).read_text())["binding"])
    print("manifest   :", json.dumps(result_matching.manifest, ensure_ascii=False))

    # ---------- 示例三：撤回场景 ----------
    banner("示例三：筛选变化撤回（不生成资料包）")
    with EvidencePackager(base, WORKDIR) as packager:
        try:
            packager.run(
                build_intent(query="review", exclusions=["DOC-01"]),
                after_selection=lambda portal, journal: portal.set_filter("all"),
            )
        except AbortError as error:
            print(json.dumps(error.report.to_dict(), ensure_ascii=False, indent=2))

    banner("示例三（续）：预览前新增记录导致修订变化撤回（不生成资料包）")
    from urllib.request import Request, urlopen

    request = Request(
        base + "/__control",
        data=b'{"phase":"new-record"}',
        headers={"Content-Type": "application/json"},
    )
    urlopen(request).read()
    with EvidencePackager(base, WORKDIR) as packager:
        try:
            packager.run(build_intent(ids=["DOC-01", "DOC-03"]))
        except AbortError as error:
            print(json.dumps(error.report.to_dict(), ensure_ascii=False, indent=2))

    # ---------- 示例四：中断找回 ----------
    banner("示例四：已生成之后按 package_id 找回同包，不重复打包")
    before = len(server.portal_state["actions"])
    with EvidencePackager(base, WORKDIR) as packager:
        recovered = packager.recover(explicit)
    after = len(server.portal_state["actions"])
    print("找回 package_id :", recovered.package_id, "（与示例一相同：",
          recovered.package_id == result_explicit.package_id, "）")
    print("recovered 标记  :", recovered.recovered)
    print("期间新增 package 动作数：", after - before, "（应为 0，不重复打包）")

    server.shutdown()
    server.server_close()

    banner("选择核对记录与产物清单")
    for intent in (explicit, matching):
        key = intent_key(intent)
        run_record = json.loads((WORKDIR / key / "run.json").read_text(encoding="utf-8"))
        print(f"\n[{key}] 模式={run_record['intent']['mode']} 状态={run_record['status']}")
        print("  选择核对（逐行复选框，非三态框推断）：")
        for evidence in run_record["selection_evidence"]:
            print(f"    {evidence['page']}: {evidence['checked']}")
        print("  证据文件：", sorted(p.name for p in (WORKDIR / key / "evidence").iterdir()))
    print("\n全部示例运行结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
