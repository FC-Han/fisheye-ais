"""run.py 里「进程挂了要说人话」的那部分：日志尾巴截取 + 退出码翻译。

为什么要单独测这个
------------------
这段代码的作用是在**用户最没有耐心的时候**（刚装完、起不来）给他一句能照做的话。
它读的是追加写入的日志文件，如果截取逻辑错了，用户会看到上一次运行的报错，
比不显示更糟 —— 所以这里锁住「只看本次启动」的语义。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import run as run_mod


class TestLogTail(unittest.TestCase):
    def _write(self, text: str) -> Path:
        f = Path(tempfile.mkdtemp()) / "collector.log"
        f.write_text(text, encoding="utf-8")
        return f

    def test_missing_file_is_silent(self):
        self.assertEqual(run_mod._tail(Path("不存在的目录/x.log")), [])
        self.assertEqual(run_mod._tail(None), [])

    def test_only_lines_after_last_start_marker(self):
        f = self._write(
            "===== start 2026-11-24 10:00:00 collector =====\n"
            "10:00:01 ERROR [collector] 上一次的旧错误，不该出现在这里\n"
            "\n"
            "===== start 2026-11-24 11:00:00 collector =====\n"
            "11:00:01 ERROR [collector] 本次的错误\n"
        )
        self.assertEqual(run_mod._tail(f), ["11:00:01 ERROR [collector] 本次的错误"])

    def test_no_marker_returns_whole_file(self):
        f = self._write("第一行\n第二行\n")
        self.assertEqual(run_mod._tail(f), ["第一行", "第二行"])

    def test_limit_keeps_tail_not_head(self):
        body = "\n".join(f"第{i}行" for i in range(50)) + "\n"
        f = self._write("===== start x =====\n" + body)
        tail = run_mod._tail(f, n=5)
        self.assertEqual(tail, ["第45行", "第46行", "第47行", "第48行", "第49行"])

    def test_blank_lines_dropped(self):
        f = self._write("===== start x =====\n\n真正的错误\n\n")
        self.assertEqual(run_mod._tail(f), ["真正的错误"])


class TestHints(unittest.TestCase):
    """退出码翻译表要和实际会出现的 (组件, 退出码) 对得上。"""

    def test_collector_missing_key_hint(self):
        # collector/main.py 在缺少 AISSTREAM_API_KEY 时 return 2
        self.assertIn("AISSTREAM_API_KEY", run_mod.HINTS[("collector", 2)])

    def test_api_port_conflict_hint(self):
        # backend/app/main.py 端口被占用时 SystemExit(3)
        self.assertIn("端口", run_mod.HINTS[("api", 3)])


if __name__ == "__main__":
    unittest.main()
