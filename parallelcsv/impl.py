"""并行 CSV 扫描的实现（仅依赖 Python 标准库）。

算法概述
========
输入保证是无 BOM 的合法 UTF-8、csv.excel 方言文件，记录终止符为 LF 或
CRLF，末条记录可无终止符。CSV 语法字符 ``"`` / ``\\r`` / ``\\n`` 全是
ASCII 单字节；UTF-8 续字节都 >= 0x80，不可能与它们混淆，因此**定界可以
直接在原始字节上完成**，解码只在最终切出完整记录后进行。

两阶段：

阶段一（定界摘要，workers 个进程并行）
    每个进程读取自己的字节分片 [b, e)，在原始字节上跑一个只看 ``"`` 与
    ``\\n`` 的引号状态机，返回常数大小摘要：
      * 本分片内原始引号数的奇偶 q；
      * 在“进入时引号态为 0/1”两种假设下，本分片内首个、末个**非引号内
        LF** 的绝对字节位置（缺失用 -1）。
    协调者按分片顺序 XOR 奇偶位，即得到每个分片真实的进入引号态
    （O(分片数)），再按“记录起点归属分片 + 反向链接首个右侧终止符”的
    规则合成每个分片负责的记录窗口 [start, end)（同样 O(分片数)）。
    每个 LF 恰好属于一个分片，每条记录恰好落在一个窗口内，窗口互不重叠
    并覆盖整个文件。

阶段二（解码产出，workers 个进程并行，协调者按序回排）
    每个进程只读取自己的窗口（窗口必然从记录边界开始），按非引号内 LF
    切出完整记录，对每条记录做一次 UTF-8 解码并用标准库 ``csv`` 模块解析
    字段（字段内的换行原样保留）。协调者用“在途窗口数 <= workers”的有界
    流水线按源序回排，绝不缓存全部结果。

复杂度
------
* 总源读取量：阶段一每个字节恰好一次 + 阶段二窗口并集恰好为整个文件，
  合计 2 × 文件大小，O(文件大小)，不存在逐片从头重扫。
* 协调者状态：O(分片数) 的小整数摘要 + O(workers) 的在途窗口。
* 载荷内存：单进程 O(block_bytes + 最大记录字节数)，全局
  O(workers × (block_bytes + 最大记录字节数))。
"""

from __future__ import annotations

import csv
import io
import multiprocessing as mp
import os
from typing import Dict, Iterator, List, Optional, Tuple

__all__ = ["scan", "scan_runtime"]

Record = Tuple[int, int, int, List[str]]

_QUOTE = ord('"')
_LF = ord("\n")


def _positive_int(name: str, value: object) -> int:
    # bool 是 int 的子类，按“正整数”语义显式拒绝。
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# 阶段一：分片定界摘要（顶层函数，可被进程池 pickle）
# ---------------------------------------------------------------------------

def _scan_block_summary(args: Tuple[str, int, int]) -> Tuple[int, int, int, int, int]:
    """读取 [block_start, block_end) 并返回常数大小摘要。

    返回 (q, first0, last0, first1, last1)：
      q      —— 分片内 ``"`` 字节数的奇偶（与进入引号态无关）；
      firstX —— 假设进入时引号态为 X，分片内首个非引号 LF 的绝对位置；
      lastX  —— 假设进入时引号态为 X，分片内末个非引号 LF 的绝对位置；
      缺失时为 -1。
    """
    path, block_start, block_end = args
    q = 0
    open0 = False          # 进入态 0 的当前引号开关
    open1 = True           # 进入态 1 的当前引号开关
    first0 = last0 = -1
    first1 = last1 = -1

    with open(path, "rb") as f:
        f.seek(block_start)
        remaining = block_end - block_start
        pos = block_start
        # 分块读取，单进程缓冲内存仍以 block_bytes 为界。
        while remaining > 0:
            chunk = f.read(min(remaining, 1 << 20))
            if not chunk:  # 约定读取期间文件不变，短读即视为异常截断
                raise IOError("file changed or truncated while scanning")
            remaining -= len(chunk)
            for ch in chunk:
                if ch == _QUOTE:
                    q ^= 1
                    open0 = not open0
                    open1 = not open1
                elif ch == _LF:
                    if not open0:
                        if first0 < 0:
                            first0 = pos
                        last0 = pos
                    if not open1:
                        if first1 < 0:
                            first1 = pos
                        last1 = pos
                pos += 1

    return q, first0, last0, first1, last1


def _build_windows(
    file_size: int,
    blocks: List[Tuple[int, int, int]],
    summaries: List[Tuple[int, int, int, int, int]],
) -> List[Tuple[int, int, int]]:
    """根据阶段一摘要合成每个分片的记录窗口。

    全局记录起点只有两类：位置 0，以及任一**非引号内 LF** 的下一字节。
    每条记录按其起点所在字节分片归属；窗口 = 该分片归属的首条记录起点
    到其末条记录终止符之后（无终止符则到文件末尾）。

    返回 [(block_index, window_start, window_end), ...]，空窗口已剔除。
    窗口互不重叠、并集恰为 [0, file_size)，且都在记录边界起止。
    """
    n = len(summaries)

    # 真实进入引号态：前缀 XOR（全部分片引号奇偶的前缀和模 2）。
    entry_quote = [0] * n
    state = 0
    for k, (q, _f0, _l0, _f1, _l1) in enumerate(summaries):
        entry_quote[k] = state
        state ^= q

    def first_lf(k: int) -> int:
        _q, f0, _l0, f1, _l1 = summaries[k]
        return f0 if entry_quote[k] == 0 else f1

    def last_lf(k: int) -> int:
        _q, _f0, l0, _f1, l1 = summaries[k]
        return l0 if entry_quote[k] == 0 else l1

    # 每个分片窗口的起点：其归属的首条记录的起点。
    starts: List[int] = [-1] * n
    for k, (_idx, b, _e) in enumerate(blocks):
        if k == 0:
            starts[k] = 0
            continue
        if last_lf(k - 1) == blocks[k - 1][2] - 1:
            # 上一分片最后一个字节就是非引号 LF：本分片首字节即记录起点。
            starts[k] = b
        else:
            f = first_lf(k)
            if f >= 0:
                starts[k] = f + 1
            # 否则本分片不含任何记录起点，窗口保持为空。

    # 反向链接：first_lf_right = 右侧分片中位置最小的非引号 LF。
    ends: List[int] = [-1] * n
    first_lf_right = -1
    for k in range(n - 1, -1, -1):
        _idx, _b, e = blocks[k]
        if starts[k] >= 0:
            l = last_lf(k)
            if l == e - 1:
                # 末字节即 LF：最后一条归属记录在分片边界处结束。
                ends[k] = e
            else:
                # l < e-1（或 l < 0）：最后一条归属记录延伸到右侧，
                # 终止符是右侧首个非引号 LF；右侧没有则为无尾记录。
                ends[k] = first_lf_right + 1 if first_lf_right >= 0 else file_size
        f = first_lf(k)
        if f >= 0:
            first_lf_right = f

    windows: List[Tuple[int, int, int]] = []
    for k in range(n):
        start, end = starts[k], ends[k]
        if start >= 0 and start < end <= file_size:
            windows.append((k, start, end))
    return windows


# ---------------------------------------------------------------------------
# 阶段二：窗口内完整记录的切分、解码与字段解析
# ---------------------------------------------------------------------------

def _parse_window(args: Tuple[str, int, int]) -> List[Record]:
    """读取窗口 [win_start, win_end)（保证从记录边界开始），解析全部记录。

    返回的字节区间左闭右开、包含终止符；末条无终止符时结束于窗口末端。
    """
    path, win_start, win_end = args
    with open(path, "rb") as f:
        f.seek(win_start)
        blob = f.read(win_end - win_start)
    if len(blob) != win_end - win_start:
        raise IOError("file changed or truncated while scanning")

    out: List[Record] = []
    in_quotes = False
    rec_start = 0
    length = len(blob)

    for i, ch in enumerate(blob):
        if ch == _QUOTE:
            in_quotes = not in_quotes
        elif ch == _LF and not in_quotes:
            rec_end = i + 1  # 包含 LF；CRLF 时 CR 自然包含在前一字节
            text = blob[rec_start:rec_end].decode("utf-8")
            fields = next(csv.reader(io.StringIO(text), dialect="excel"))
            out.append((win_start + rec_start, win_start + rec_end, fields))
            rec_start = rec_end

    if rec_start < length:
        # 末条记录无终止符。
        text = blob[rec_start:].decode("utf-8")
        fields = next(csv.reader(io.StringIO(text), dialect="excel"))
        out.append((win_start + rec_start, win_end, fields))

    return out


# ---------------------------------------------------------------------------
# 协调者
# ---------------------------------------------------------------------------

def _plan_blocks(file_size: int, block_bytes: int) -> List[Tuple[int, int, int]]:
    blocks = []
    b = 0
    idx = 0
    while b < file_size:
        e = min(b + block_bytes, file_size)
        blocks.append((idx, b, e))
        b = e
        idx += 1
    return blocks


def scan_runtime(
    path: str,
    block_bytes: int,
    workers: int,
    stats: Optional[Dict[str, object]] = None,
) -> Tuple[Iterator[Record], Dict[str, object]]:
    """同 :func:`scan`，额外返回实时统计字典。"""
    block_bytes = _positive_int("block_bytes", block_bytes)
    workers = _positive_int("workers", workers)

    live: Dict[str, object] = stats if stats is not None else {}
    live.clear()
    live.update({
        "workers": workers,
        "block_bytes": block_bytes,
        "file_size": os.path.getsize(path),
        "blocks": 0,
        "source_bytes_read": 0,          # 两阶段累计源读取字节
        "phase1_bytes": 0,
        "phase2_bytes": 0,
        "records_emitted": 0,
        "max_inflight_window_bytes": 0,  # 在途窗口字节峰值
        "max_record_bytes": 0,           # 已观测到的最大记录字节数
    })
    stats = live

    def gen() -> Iterator[Record]:
        file_size = stats["file_size"]
        if file_size == 0:
            return

        blocks = _plan_blocks(file_size, block_bytes)
        stats["blocks"] = len(blocks)
        phase1_jobs = [(path, b, e) for _idx, b, e in blocks]
        stats["phase1_bytes"] = file_size
        stats["source_bytes_read"] = file_size

        ctx = mp.get_context()  # 默认上下文（Linux 为 fork），顶层函数可 pickle
        with ctx.Pool(processes=workers) as pool:
            # 阶段一：摘要极小，map 的结果缓存仅 O(分片数) 个小整数元组。
            summaries = pool.map(_scan_block_summary, phase1_jobs)
            windows = _build_windows(file_size, blocks, summaries)

            # 阶段二：有界流水线。在途窗口数始终 <= workers，
            # 每个窗口的载荷为 O(block_bytes + 最大记录字节数)。
            inflight: Dict[int, "mp.pool.AsyncResult"] = {}
            cursor = 0
            line_no = 0
            phase2_bytes = 0

            while cursor < len(windows) or inflight:
                while cursor < len(windows) and len(inflight) < workers:
                    _k, ws, we = windows[cursor]
                    inflight[cursor] = pool.apply_async(
                        _parse_window, ((path, ws, we),)
                    )
                    stats["max_inflight_window_bytes"] = max(
                        stats["max_inflight_window_bytes"], we - ws
                    )
                    phase2_bytes += we - ws
                    cursor += 1
                if not inflight:
                    break
                key = min(inflight)
                result = inflight.pop(key).get()  # 异常在此向上抛出
                for start, end, fields in result:
                    stats["max_record_bytes"] = max(
                        stats["max_record_bytes"], end - start
                    )
                    stats["records_emitted"] += 1
                    yield (line_no, start, end, fields)
                    line_no += 1

            stats["phase2_bytes"] = phase2_bytes
            stats["source_bytes_read"] = file_size + phase2_bytes

    return gen(), stats


def scan(
    path: str,
    block_bytes: int,
    workers: int,
    *,
    stats: Optional[Dict[str, object]] = None,
) -> Iterator[Record]:
    """并行扫描 CSV 文件，按源序惰性产出四元组记录。

    :param path:        输入文件路径（读取期间保持不变、无 BOM、合法
                        UTF-8 的 csv.excel 文件）。
    :param block_bytes: 分片字节数，正整数。
    :param workers:     工作进程数，正整数。
    :param stats:      可选字典，调用结束后填入运行指标。
    :yield: ``(逻辑行号, 起始字节, 结束字节, 字段列表)``，逻辑行号从 0
                        起（表头也是第 0 条记录），字节区间左闭右开且
                        包含记录终止符，字段内部换行原样保留。
    :raises ValueError: ``block_bytes`` 或 ``workers`` 不是正整数。
    """
    # 在生成器之外即刻校验，保证 scan(...) 调用本身就抛出 ValueError。
    block_bytes = _positive_int("block_bytes", block_bytes)
    workers = _positive_int("workers", workers)
    # 传入同一字典对象，生成器迭代过程中指标实时落盘，调用方迭代结束即可读。
    it, _live = scan_runtime(path, block_bytes, workers, stats=stats)
    return it
