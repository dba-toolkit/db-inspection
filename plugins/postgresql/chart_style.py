#!/usr/bin/env python3
"""Chart style configuration for PG inspection charts."""
from __future__ import annotations


def apply_style(plt: object) -> None:
    """Apply consistent chart styling."""
    try:
        plt.style.use("seaborn-v0_8-whitegrid")
    except Exception:
        try:
            plt.style.use("seaborn-whitegrid")
        except Exception:
            plt.style.use("ggplot")

    plt.rcParams.update({
        "font.size": 10,
        "axes.titlesize": 12,
        "axes.labelsize": 9,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "legend.fontsize": 8,
        "figure.dpi": 150,
        "savefig.dpi": 150,
        "savefig.bbox": "tight",
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#cccccc",
        "grid.alpha": 0.3,
        "grid.color": "#dddddd",
        "axes.unicode_minus": False,
    })

    # Try to suppress CJK font warnings gracefully
    try:
        import matplotlib.font_manager as fm
        for f in fm.fontManager.ttflist:
            if "SimHei" in f.name or "WenQuanYi" in f.name or "Noto Sans CJK" in f.name:
                plt.rcParams["font.family"] = f.name
                break
    except Exception:
        pass


# Color palette matching Chinese stock convention (red = increase/warning)
COLOR_RED = "#DC143C"
COLOR_GREEN = "#228B22"
COLOR_BLUE = "#1E90FF"
COLOR_ORANGE = "#FF8C00"
COLOR_GRAY = "#808080"
