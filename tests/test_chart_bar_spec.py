"""公共层「类别轴条形图」测试（批 12b）。

``render_bar_spec`` 是批 12b 新加的入口，与时间轴折线 ``render_spec`` 并列：
折线的 x 是时间戳、要求 ≥2 点；条形的 x 是**类别**（表空间名 / SQL_ID），
一个类别就该能画。这里钉住四件事：

1. 判据：无类别、或全类别都无值 ⇒ 无数据；
2. 缺值的类别被**丢弃**（不是当 0 画一根假条）；
3. 渲染：matplotlib 优先，缺失时降级 Pillow；两路都返回**具名渲染器**，绝不静默成功；
4. 阈值着色：越过阈值的条变红（表空间 ≥90%），其余用主色。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inspection_core.charts import bar_spec_has_data, has_matplotlib, render_bar_spec
from inspection_core.charts.render import _bar_color, _bar_pairs

try:  # pragma: no cover - 环境相关
    import PIL  # noqa: F401

    _HAS_PIL = True
except ImportError:  # pragma: no cover
    _HAS_PIL = False


def spec(**overrides):
    base = {
        "chart_id": "demo_bar",
        "title": "演示排行",
        "categories": ["A", "B", "C"],
        "values": [3.0, 1.5, 2.0],
        "filename": "demo_bar.png",
        "source_scope": "tables",
    }
    base.update(overrides)
    return base


class BarSpecDataTests(unittest.TestCase):
    def test_requires_at_least_one_category(self) -> None:
        self.assertFalse(bar_spec_has_data(spec(categories=[], values=[])))
        self.assertTrue(bar_spec_has_data(spec()))

    def test_all_missing_values_is_no_data(self) -> None:
        self.assertFalse(bar_spec_has_data(spec(values=[None, None, None])))

    def test_pairs_drop_missing_values(self) -> None:
        # 缺值不是 0：丢掉整个类别，别画一根不存在的假条。
        pairs = _bar_pairs(spec(categories=["A", "B", "C"], values=[1.0, None, 3.0]))
        self.assertEqual(pairs, [("A", 1.0), ("C", 3.0)])

    def test_skip_record_states_the_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            record = render_bar_spec(spec(categories=[], values=[]), target, target)
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(record["reason"], "insufficient_data_points")


class BarSpecColorTests(unittest.TestCase):
    def test_threshold_turns_bars_red(self) -> None:
        self.assertEqual(_bar_color(95.0, spec(alert_threshold=90.0)), "#DC2626")
        self.assertEqual(_bar_color(85.0, spec(alert_threshold=90.0)), "#2563EB")

    def test_without_threshold_every_bar_is_the_primary_hue(self) -> None:
        self.assertEqual(_bar_color(95.0, spec()), "#2563EB")


@unittest.skipUnless(has_matplotlib(), "需要 matplotlib 才能出图")
class BarSpecMatplotlibTests(unittest.TestCase):
    def test_renders_png_and_names_the_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            record = render_bar_spec(spec(), target, target)
            self.assertEqual(record["status"], "generated")
            self.assertEqual(record["renderer"], "matplotlib")
            self.assertEqual(record["axis_mode"], "category")
            self.assertEqual(record["source_points"], 3)
            self.assertTrue((target / "demo_bar.png").is_file())


@unittest.skipUnless(_HAS_PIL, "需要 Pillow 才能走降级渲染")
class BarSpecPillowFallbackTests(unittest.TestCase):
    def test_falls_back_to_pillow_when_matplotlib_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with mock.patch("inspection_core.charts.render.has_matplotlib",
                            return_value=False):
                record = render_bar_spec(spec(), target, target)
            self.assertEqual(record["status"], "generated")
            self.assertEqual(record["renderer"], "pillow_fallback")
            self.assertTrue((target / "demo_bar.png").is_file())

    def test_no_renderer_available_is_reported_not_silent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            with mock.patch("inspection_core.charts.render.has_matplotlib",
                            return_value=False):
                with mock.patch("inspection_core.charts.render._render_bar_pillow",
                                return_value=None):
                    record = render_bar_spec(spec(), target, target)
            self.assertEqual(record["status"], "skipped")
            self.assertEqual(record["reason"], "no_renderer_available")


if __name__ == "__main__":
    unittest.main()
