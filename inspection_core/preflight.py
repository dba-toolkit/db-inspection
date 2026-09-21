"""入口依赖自检：把"静默降级"变成"入口处说清楚"。

项目里最容易出、又最难在代码里发现的交付缺陷是**图表渲染降级**：
``inspection_core/charts/render.py`` 优先用 matplotlib 画图，导入不到时才退回
Pillow 手绘版。退回本身是设计好的兜底，但它**不报错** —— 报告照常生成，
只有打开 Word 才看得出来差异（X 轴末尾两个刻度叠在一起、Y 轴标签贴到左边界）。

实测踩到过：同一条命令在 A 解释器下出 matplotlib 图、在 B 解释器下出降级图，
而报告里除了 ``charts[*].renderer`` 字段外没有任何提示。于是在两个入口
（``analyze.py`` / ``generate_report.py``）启动时自检一次，把
"当前解释器是谁、缺什么、用什么命令装"直接讲出来。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

# 报告生成的硬依赖：缺了必然失败，入口直接返回非零。
FATAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("docx", "python-docx"),
    ("PIL", "Pillow"),
)

# 缺了不报错，但会改变交付物质量（图表降级为 Pillow 手绘版），只告警。
DEGRADED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("matplotlib", "matplotlib"),
)


def _importable(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def preflight_report_dependencies(requirements: Path | None = None) -> int:
    """检查当前解释器是否带齐报告依赖。

    返回 ``0`` 可以继续；``1`` 表示 fatal 级缺失（报告一定生成不出来）。
    ``requirements`` 只用于把安装命令提示写准确。
    """
    hint = f'"{sys.executable}" -m pip install'
    if requirements is not None:
        hint += f' -r "{requirements}"'
    else:
        hint += " " + " ".join(package for _, package in FATAL_PACKAGES + DEGRADED_PACKAGES)

    missing_fatal = [package for module, package in FATAL_PACKAGES if not _importable(module)]
    if missing_fatal:
        print(f"ERROR: 当前解释器缺少报告生成依赖：{'、'.join(missing_fatal)}", file=sys.stderr)
        print(f"       当前解释器：{sys.executable}", file=sys.stderr)
        print(f"       安装命令：{hint}", file=sys.stderr)
        return 1

    missing_charts = [package for module, package in DEGRADED_PACKAGES if not _importable(module)]
    if missing_charts:
        print(
            f"警告: 当前解释器缺少 {'、'.join(missing_charts)}，本次趋势图将降级为 Pillow 手绘版"
            "（刻度可能重叠、Y 轴标签贴边），与 matplotlib 版差异明显。",
            file=sys.stderr,
        )
        print(f"       当前解释器：{sys.executable}", file=sys.stderr)
        print(f"       安装命令：{hint}", file=sys.stderr)
    return 0
