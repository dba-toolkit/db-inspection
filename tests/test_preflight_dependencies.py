"""入口依赖自检的回归测试。

守的是"静默降级"：图表渲染器找不到 matplotlib 时会安静地退回 Pillow 手绘版，
报告照常生成、只有打开 Word 才看得出差异。自检必须做到：

- 缺 matplotlib → **告警但不阻断**（图变差总比不出报告好）；
- 缺 python-docx / Pillow → **阻断**并说明当前解释器与安装命令；
- 依赖齐全 → 完全静默（否则每次跑都刷一屏噪声，很快就被忽略）。
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest import mock

from inspection_core import preflight


class PreflightDependencyTests(unittest.TestCase):
    def test_missing_chart_backend_warns_but_does_not_block(self) -> None:
        with mock.patch.object(preflight, "_importable", side_effect=lambda m: m != "matplotlib"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = preflight.preflight_report_dependencies()

        self.assertEqual(0, code, "缺 matplotlib 只是降级，不得阻断分析")
        message = err.getvalue()
        self.assertIn("matplotlib", message)
        self.assertIn("降级", message)
        self.assertIn("安装命令", message, "要直接给出可复制的安装命令")

    def test_missing_fatal_dependency_blocks(self) -> None:
        with mock.patch.object(preflight, "_importable", side_effect=lambda m: m != "docx"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = preflight.preflight_report_dependencies(Path("requirements.txt"))

        self.assertEqual(1, code, "缺 python-docx 时报告一定生成不出来，必须阻断")
        message = err.getvalue()
        self.assertIn("python-docx", message)
        self.assertIn("requirements.txt", message)

    def test_all_present_is_silent(self) -> None:
        with mock.patch.object(preflight, "_importable", return_value=True), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = preflight.preflight_report_dependencies()

        self.assertEqual(0, code)
        self.assertEqual("", err.getvalue(), "依赖齐全时不得输出任何内容")


if __name__ == "__main__":
    unittest.main()
