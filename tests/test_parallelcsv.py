"""parallelcsv.scan 的业务验证与边界测试（仅标准库，python -I 可运行）。

覆盖：
  1. 题目给定的两个规格样例与空文件；
  2. CRLF、转义双引号、跨行字段、UTF-8 多字节切分、无终止符末条；
  3. 分片从多字节字符 / 转义引号 / 跨行字段内部切入，不得重漏；
  4. block_bytes / workers 的正整数校验（含 bool、0、负数、非 int）；
  5. 随机模糊测试：与单进程 csv 参考实现逐条核对，并校验字节区间
     左闭右开、首尾相接、覆盖全文件；
  6. 惰性产出（提前 break 不要求读完）与运行指标（读取量 = 2×文件大小）。
"""

from __future__ import annotations

import csv
import io
import os
import random
import string
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parallelcsv import scan  # noqa: E402


def write_tmp(data: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="pcsv_", suffix=".csv")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def reference_records(blob: bytes):
    """单进程参考实现：直接用 csv 模块解析，并用字节级状态机给出区间。"""
    # newline='' 是 csv 模块对“文件类输入”的标准要求，避免其误判裸换行。
    expected_fields = list(csv.reader(
        io.StringIO(blob.decode("utf-8"), newline=""),
        dialect="excel",
    ))
    spans = []
    in_quotes = False
    start = 0
    for i, ch in enumerate(blob):
        if ch == ord('"'):
            in_quotes = not in_quotes
        elif ch == ord("\n") and not in_quotes:
            spans.append((start, i + 1))
            start = i + 1
    if start < len(blob):
        spans.append((start, len(blob)))
    assert len(spans) == len(expected_fields)
    return [
        (i, spans[i][0], spans[i][1], expected_fields[i])
        for i in range(len(spans))
    ]


class SpecSamples(unittest.TestCase):
    def test_sample_quoted_newline(self):
        path = write_tmp('a,"b\nc"\n'.encode("utf-8"))
        try:
            got = list(scan(path, 4, 2))
        finally:
            os.unlink(path)
        self.assertEqual(got, [(0, 0, 8, ["a", "b\nc"])])

    def test_sample_crlf_and_unicode(self):
        path = write_tmp('中,"a""b"\r\nx,'.encode("utf-8"))
        try:
            got = list(scan(path, 5, 3))
        finally:
            os.unlink(path)
        self.assertEqual(
            got,
            [(0, 0, 12, ["中", 'a"b']), (1, 12, 14, ["x", ""])],
        )

    def test_empty_file(self):
        path = write_tmp(b"")
        try:
            self.assertEqual(list(scan(path, 1024, 2)), [])
        finally:
            os.unlink(path)


class BoundaryCases(unittest.TestCase):
    def _run_all_block_sizes(self, blob: bytes, workers: int = 3):
        path = write_tmp(blob)
        try:
            expected = reference_records(blob)
            for size in range(1, len(blob) + 2):
                got = list(scan(path, size, workers))
                self.assertEqual(got, expected,
                                 f"block_bytes={size} mismatch")
        finally:
            os.unlink(path)

    def test_crlf_escaped_quote_multiline(self):
        blob = (
            'h1,h2\r\n'
            '"a,b",c\r\n'
            '"line1\nline2","say ""hi"""\r\n'
            'plain,"x\r\ny"\n'
            'tail,无尾'
        ).encode("utf-8")
        self._run_all_block_sizes(blob)

    def test_giant_quoted_record_spanning_blocks(self):
        # 单条跨行引号记录跨越大量分片，两侧再放普通记录。
        inner = ("\n".join(f"row{i}" for i in range(50)))
        blob = (
            "head\r\n"
            + f'"{inner}"\n'
            + "middle,x\n"
            + '"unterminated\nmulti\nline'
        ).encode("utf-8")
        self._run_all_block_sizes(blob, workers=4)

    def test_only_newlines_and_empty_fields(self):
        blob = b"\n\n\n"
        self._run_all_block_sizes(blob)
        blob2 = 'a,""\n,"\n'.encode("utf-8")
        self._run_all_block_sizes(blob2)

    def test_block_cuts_multibyte_char(self):
        # “中” 占 3 字节；逐字节分片必然切到多字节字符内部。
        blob = ("中文,字段\n" * 20 + "末,行").encode("utf-8")
        self._run_all_block_sizes(blob, workers=2)

    def test_spans_are_partition(self):
        blob = (
            'a,"b\nc"\n中,"a""b"\r\nx,\n'.encode("utf-8")
        )
        path = write_tmp(blob)
        try:
            for size in (1, 2, 3, 5, 7, 64):
                got = list(scan(path, size, 3))
                # 行号连续
                self.assertEqual([r[0] for r in got], list(range(len(got))))
                # 区间首尾相接、覆盖整个文件
                self.assertEqual(got[0][1], 0)
                for prev, cur in zip(got, got[1:]):
                    self.assertEqual(prev[2], cur[1])
                self.assertEqual(got[-1][2], len(blob))
        finally:
            os.unlink(path)


class ArgumentValidation(unittest.TestCase):
    def setUp(self):
        self.path = write_tmp(b"a,b\n1,2\n")

    def tearDown(self):
        os.unlink(self.path)

    def test_non_positive_raises(self):
        for bad in (0, -1, -1024):
            with self.assertRaises(ValueError):
                scan(self.path, bad, 2)
            with self.assertRaises(ValueError):
                scan(self.path, 4, bad)

    def test_non_int_raises(self):
        for bad in (1.5, "4", None, [4], object()):
            with self.assertRaises(ValueError):
                scan(self.path, bad, 2)
            with self.assertRaises(ValueError):
                scan(self.path, 4, bad)

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            scan(self.path, True, 2)
        with self.assertRaises(ValueError):
            scan(self.path, 4, False)

    def test_error_raised_eagerly(self):
        # 即使从不迭代，非法参数也要立刻抛错。
        with self.assertRaises(ValueError):
            scan(self.path, 0, 2)


class Fuzz(unittest.TestCase):
    @staticmethod
    def _random_csv(rng: random.Random) -> bytes:
        # 生成合法 csv.excel 内容：裸 CR/LF/引号/逗号只允许出现在引号字段内。
        rows = []
        for _ in range(rng.randint(1, 12)):
            fields = []
            for _ in range(rng.randint(1, 4)):
                kind = rng.randrange(5)
                if kind == 0:
                    fields.append("")
                elif kind == 1:
                    # 引号字段，内含换行/CRLF/转义引号/逗号
                    raw = "".join(
                        rng.choice(string.ascii_letters + '",\n\r中x')
                        for _ in range(rng.randint(0, 14))
                    )
                    fields.append('"' + raw.replace('"', '""') + '"')
                elif kind == 2:
                    # 多行引号字段
                    inner = "\n".join(
                        "".join(rng.choice("abc中123")
                                for _ in range(rng.randint(0, 6)))
                        for _ in range(rng.randint(2, 4))
                    )
                    fields.append(f'"{inner}"')
                elif kind == 3:
                    fields.append("".join(rng.choice("abc中文 123")
                                          for _ in range(rng.randint(0, 10))))
                else:
                    fields.append('""')
            rows.append(",".join(fields))
        text = "\n".join(rows)
        if rng.random() < 0.7:
            text += rng.choice(["\n", "\r\n"])
        return text.encode("utf-8")

    def test_fuzz(self):
        rng = random.Random(20261113)
        for seed in range(120):
            rng.seed(1000 + seed)
            blob = self._random_csv(rng)
            path = write_tmp(blob)
            try:
                expected = reference_records(blob)
                for size in (1, 2, 3, rng.randint(1, max(1, len(blob))),
                             max(1, len(blob) + 1)):
                    for workers in (1, 2, 4):
                        got = list(scan(path, size, workers))
                        self.assertEqual(
                            got, expected,
                            msg=f"seed={seed} size={size} workers={workers}\n"
                                f"blob={blob!r}",
                        )
            finally:
                os.unlink(path)


class LazinessAndStats(unittest.TestCase):
    def test_lazy_early_break(self):
        blob = ("a,b\n" * 1000).encode("utf-8")
        path = write_tmp(blob)
        try:
            it = scan(path, 16, 2)
            first = next(it)
            self.assertEqual(first[0], 0)
            it.close()  # 提前终止，不得要求消费完全部记录
        finally:
            os.unlink(path)

    def test_stats_and_read_volume(self):
        blob = ('h1,h2\n' + '"x\ny",z\n' * 500 + "tail,1").encode("utf-8")
        path = write_tmp(blob)
        stats: dict = {}
        try:
            records = list(scan(path, 37, 3, stats=stats))
        finally:
            os.unlink(path)
        self.assertEqual(stats["workers"], 3)
        self.assertEqual(stats["phase1_bytes"], len(blob))
        # 窗口并集恰为整个文件：阶段二读取量 == 文件大小，总量 == 2×。
        self.assertEqual(stats["phase2_bytes"], len(blob))
        self.assertEqual(stats["source_bytes_read"], 2 * len(blob))
        self.assertEqual(stats["records_emitted"], len(records))
        # 在途窗口受 workers 有界；窗口长度不超过 block + 最大记录。
        self.assertLessEqual(stats["max_inflight_window_bytes"],
                             stats["block_bytes"] + stats["max_record_bytes"])
        self.assertGreaterEqual(stats["max_record_bytes"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
