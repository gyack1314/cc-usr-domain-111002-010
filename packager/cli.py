"""命令行入口。

意图通过 JSON 文件给出，两种模式字段互斥：

显式模式::

    {"mode": "explicit", "ids": ["DOC-01", "DOC-09"]}

条件模式::

    {"mode": "matching", "query": "review", "exclusions": ["DOC-07"]}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .diff import AbortError
from .intent import intent_from_dict
from .packager import EvidencePackager, RecoveryError


def _load_intent(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return intent_from_dict(payload)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="审查证据资料精确打包")
    parser.add_argument("--base-url", default="http://127.0.0.1:8090", help="门户地址")
    parser.add_argument("--workdir", default="state/packager", help="运行台账与资料包目录")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="按意图选择、核对并生成资料包")
    run_p.add_argument("intent", type=Path, help="意图 JSON 文件")
    run_p.add_argument("--no-screenshots", action="store_true", help="不保存页面截图")

    rec_p = sub.add_parser("recover", help="按已记录 package_id 找回同包，不重复打包")
    rec_p.add_argument("intent", type=Path, help="意图 JSON 文件")

    args = parser.parse_args(argv)
    intent = _load_intent(args.intent)

    with EvidencePackager(
        args.base_url, args.workdir, capture_screenshots=not getattr(args, "no_screenshots", False)
    ) as packager:
        try:
            if args.command == "run":
                result = packager.run(intent)
            else:
                result = packager.recover(intent)
        except AbortError as error:
            print(json.dumps({"status": "aborted", "abort": error.report.to_dict()},
                             ensure_ascii=False, indent=2))
            return 2
        except RecoveryError as error:
            print(json.dumps({"status": "recovery-failed", "error": str(error)},
                             ensure_ascii=False, indent=2))
            return 3
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0


if __name__ == "__main__":
    sys.exit(main())
