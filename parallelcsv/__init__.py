"""parallelcsv —— 标准库实现的并行 CSV 定界/解码扫描器。

对外接口：
    scan(path, block_bytes, workers, *, stats=None)
        惰性生成器，按源序产出
        (零起逻辑行号, 起始字节, 结束字节, 字段列表)。
    scan_runtime(path, block_bytes, workers)
        返回 (generator, stats_dict)，stats_dict 实时记录进程数、
        源读取字节数、在途载荷等运行指标。

设计要点见 parallelcsv/impl.py 与 BOUNDARY.md。
"""

from .impl import scan, scan_runtime

__all__ = ["scan", "scan_runtime"]
