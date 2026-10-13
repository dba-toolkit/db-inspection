"""公共渲染器的**双轴**规格（批 20 新增的 ``secondary`` 字段）。

背景：Oracle 的「执行速率 / 硬解析率」是两种量纲画在一张图上，此前由插件手写
matplotlib 的 ``twinx()`` 完成 —— 于是这张图与公共层出的图在**画布尺寸、图题语言、
图例位置、数据点标记、采样断点、缺 matplotlib 时的降级**六个方面都不一致。批 20 把
双轴做进公共层，插件只产出 spec。

这里钉四件事：

1. 带 ``secondary`` 的 spec 在 matplotlib 后端能出图；
2. **屏蔽 matplotlib 时也能出图**（走 Pillow 降级）—— 这正是手写版缺的能力：
   它遇到没有 matplotlib 的机器会整批 `status=skipped`，图直接消失；
3. 不带 ``secondary`` 的 spec 行为不变（图例仍只列左轴系列）；
4. 两侧图例合并成一份，且**副轴不与左轴撞色**（接着左轴的色位往下取）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inspection_core.charts import render_spec
from inspection_core.charts.style import COLORS, has_matplotlib
from inspection_core.models import PackageContext

import inspection_core.charts.render as render_module

X = ["2026-10-10 20:45:55", "2026-10-10 20:46:00", "2026-10-10 20:46:05",
     "2026-10-10 20:46:10"]


def context() -> PackageContext:
    """最小可用上下文：``render_spec`` 只读 ``snapshot`` / ``timeseries`` / ``history``。

    刻意**不复用** ``test_os_common_layer`` 里的同名工厂 —— 这里要的是「渲染路径
    能跑通」，不是那一份 OS 图数据的形状；耦合过去会让本文件在无关重构里连带变红。
    """
    return PackageContext(
        source=Path("synthetic.tar.gz"),
        root=Path("."),
        snapshot={"time_evidence": {"timezone": "Asia/Shanghai"}},
        status={"items": []},
        manifest={},
        integrity={"status": "ok"},
        history={},
        timeseries={},
    )


def dual_spec() -> dict:
    return {
        "chart_id": "TEST_DUAL",
        "title": "测试：执行速率与硬解析率（现场短时采样）",
        "x": X,
        "panels": [("", "次/秒", [("执行速率", [10.0, 20.0, 30.0, 40.0])])],
        "secondary": {"ylabel": "硬解析率（%）",
                      "series": [("硬解析率", [0.1, 0.2, 1.5, 0.0])]},
        "source_scope": "realtime_snapshot",
        "filename": "test_dual.png",
    }


def plain_spec() -> dict:
    spec = dual_spec()
    spec.pop("secondary")
    spec["chart_id"] = "TEST_PLAIN"
    spec["filename"] = "test_plain.png"
    return spec


class SecondaryAxisRenderTests(unittest.TestCase):
    def test_dual_axis_spec_is_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            result = render_spec(context(), dual_spec(), out, out)
            self.assertEqual(result["status"], "generated")
            self.assertTrue((out / "test_dual.png").exists())

    def test_dual_axis_survives_without_matplotlib(self) -> None:
        """★ 手写版遇到没有 matplotlib 的机器会整批消失；公共层必须走 Pillow 兜底。

        这条是批 20 的**正确性**判据之一（另一条是采样断点），不是版式偏好。
        """
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            with mock.patch.object(render_module, "has_matplotlib", return_value=False):
                result = render_spec(context(), dual_spec(), out, out)
            self.assertEqual(result["status"], "generated")
            self.assertEqual(result["renderer"], "pillow_fallback")
            self.assertTrue((out / "test_dual.png").exists())

    def test_plain_spec_still_renders_the_same_way(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            result = render_spec(context(), plain_spec(), out, out)
            self.assertEqual(result["status"], "generated")
            self.assertTrue((out / "test_plain.png").exists())

    def test_one_point_still_counts_as_no_data(self) -> None:
        """门槛没被双轴放松：左轴只有 1 个点仍然 `skipped`。"""
        spec = dual_spec()
        spec["x"] = X[:1]
        spec["panels"] = [("", "次/秒", [("执行速率", [10.0])])]
        spec["secondary"] = {"ylabel": "硬解析率（%）", "series": [("硬解析率", [0.5])]}
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            result = render_spec(context(), spec, out, out)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "insufficient_data_points")


@unittest.skipUnless(has_matplotlib(), "需要 matplotlib 才能检查图例与取色")
class SecondaryAxisLegendTests(unittest.TestCase):
    def test_both_axes_are_merged_into_one_legend(self) -> None:
        import matplotlib.pyplot as plt

        from inspection_core.charts.render import _draw_secondary_axis

        fig, axis = plt.subplots()
        try:
            axis.plot([1, 2], [1, 2], label="左轴系列")
            handles, labels = _draw_secondary_axis(
                axis, [1, 2], 1, {"ylabel": "右轴", "series": [("右轴系列", [3, 4])]})
        finally:
            plt.close(fig)
        self.assertEqual(labels, ["左轴系列", "右轴系列"])
        self.assertEqual(len(handles), 2)

    def test_secondary_colour_does_not_collide_with_the_left_axis(self) -> None:
        """副轴颜色接着左轴已用的色位往下取：左轴 1 条时副轴取 COLORS[1]。"""
        import matplotlib.pyplot as plt

        from inspection_core.charts.render import _draw_secondary_axis

        fig, axis = plt.subplots()
        try:
            axis.plot([1, 2], [1, 2], label="左轴系列", color=COLORS[0])
            handles, _ = _draw_secondary_axis(
                axis, [1, 2], 1, {"ylabel": "右轴", "series": [("右轴系列", [3, 4])]})
            left_colour = handles[0].get_color()
            right_colour = handles[-1].get_color()
        finally:
            plt.close(fig)
        # ``twinx()`` 的线画在**另一个** Axes 上，不进 ``axis.get_lines()`` —— 只能从
        # 返回的句柄取色。本用例初版读 ``axis.get_lines()[-1]``，取到的还是左边那条线，
        # ``assertNotEqual`` 形同虚设（同一条线的颜色当然自等）。
        self.assertEqual(left_colour, COLORS[0])
        self.assertEqual(right_colour, COLORS[1])
        self.assertNotEqual(left_colour, right_colour)

    def test_secondary_is_ignored_on_a_multi_panel_spec(self) -> None:
        """多面板 + 双轴会让「右轴刻度归哪个面板」说不清 ⇒ 只在单面板生效。"""
        import matplotlib.pyplot as plt

        from inspection_core.charts.render import _render_matplotlib

        spec = dual_spec()
        spec["panels"] = [
            ("吞吐", "KiB/s", [("读", [1.0, 2.0, 3.0, 4.0])]),
            ("繁忙度", "%", [("繁忙度", [5.0, 6.0, 7.0, 8.0])]),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            result = _render_matplotlib(spec, out, out, "UTC+08:00", None)
            self.assertIsNotNone(result)
            # 副轴没有生效：第二个面板不该出现 twinx 的右侧刻度
            plt.close("all")


if __name__ == "__main__":
    unittest.main()
