"""parallelcsv 的命令行演示。

用法：
    python -m parallelcsv.demo <csv 路径> <block_bytes> <workers>

逐行打印 (逻辑行号, 起始字节, 结束字节, 字段列表)，结束后打印运行指标
（工作进程数、两阶段源读取量、峰值在途窗口、最大记录字节等）。
"""

from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    # 允许 `python -I parallelcsv/demo.py ...` 直接执行（隔离模式下
    # cwd 与 PYTHONPATH 均不在 sys.path，需要手动补上仓库根目录）。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from parallelcsv.impl import scan
else:
    from .impl import scan


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: python -m parallelcsv.demo <path> <block_bytes> <workers>",
            file=sys.stderr,
        )
        return 2
    path = argv[1]
    block_bytes = int(argv[2])
    workers = int(argv[3])  # 非正整数由 scan 统一抛 ValueError

    stats: dict = {}
    for record in scan(path, block_bytes, workers, stats=stats):
        print(record)

    print("---- runtime stats ----")
    for key in (
        "workers",
        "block_bytes",
        "file_size",
        "blocks",
        "phase1_bytes",
        "phase2_bytes",
        "source_bytes_read",
        "records_emitted",
        "max_inflight_window_bytes",
        "max_record_bytes",
    ):
        print(f"{key}: {stats[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
