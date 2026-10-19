"""守门：采集脚本的每个 CLI 开关都必须有读取点。

守的是 F-05 那一类「假开关」：声明 + 帮助 + 参数解析三处齐全，但**全脚本无人读**。
客户按文档打开它，行为零变化，脚本也不提任何警告 —— 这是"没采到"与"没问题"
长得一样的同族缺陷（旋钮版）。

判据（静态可判）：
  ① 从 `case` 分支里取出 `--flag) ... ;;` 块；
  ② 块内被赋值的变量（`VAR=` / `VAR="${2-}"`）就是该开关的作用点；
  ③ 该变量在**全文件的其它位置**必须出现 `$VAR` 或 `${VAR` 形式的读取。
  不满足 ③ ⇒ 死开关。

豁免（不算死开关，写在 `_NO_VAR_BRANCH` 里）：`--help`、`--` 之类不赋值只改控制流的。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = [
    ROOT / "inspection" / "pg_inspection_standard.sh",
    ROOT / "inspection" / "mysql_inspection_standard.sh",
    ROOT / "inspection" / "oracle_inspection.sh",
]

# `--flag)` / `-h|--help)` / `--verbose)` …；刻意不匹配 `--`、`-*`、`*)`。
_BRANCH = re.compile(
    r"^[ \t]*((?:-{1,2}[a-z0-9][a-z0-9-]*\|)*-{1,2}[a-z0-9][a-z0-9-]*)\)[ \t]*",
    re.M,
)
_ASSIGN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(?!=)")
_READ = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\b")


def branch_blocks(text: str) -> list[tuple[list[str], str]]:
    """返回 [(该分支覆盖的 flag 列表, 分支体)]，分支体止于同一个 `;;`。"""
    out = []
    for m in _BRANCH.finditer(text):
        head = m.group(1)
        if "--" in head:
            flags = [f for f in head.split("|") if f.startswith("--")]
        else:
            flags = [f for f in head.split("|")]
        end = text.find(";;", m.end())
        body = text[m.end(): end if end != -1 else len(text)]
        out.append((flags, body))
    return out


def dead_flags(text: str) -> dict[str, str]:
    """返回 {flag: 被赋值的变量名}，即那些赋了值却没有任何读取点的开关。"""
    reads = set(_READ.findall(text))
    dead: dict[str, str] = {}
    for flags, body in branch_blocks(text):
        # 赋值处写作 `VAR=`，不会被 _READ 计入，所以这里比的是"别处读没读过"
        for var in sorted(set(_ASSIGN.findall(body))):
            if var not in reads:
                for f in flags:
                    dead[f] = var
    return dead


class CliFlagReadPointTests(unittest.TestCase):
    def test_no_dead_cli_flag_in_collectors(self) -> None:
        report = []
        for path in COLLECTORS:
            text = path.read_text(encoding="utf-8")
            for flag, var in sorted(dead_flags(text).items()):
                report.append(f"{path.name}: {flag} -> {var} 无读取点")
        self.assertEqual([], report, "存在假开关（声明+解析都有，但全脚本无人读）：\n" + "\n".join(report))

    def test_audit_catches_planted_dead_flag(self) -> None:
        """尺子自证：故意塞一个只赋值不读的开关，必须被抓到。"""
        fake = (
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --ghost-knob) GHOST_KNOB=1; shift ;;\n"
            "    --real-knob) REAL_KNOB=1; shift ;;\n"
            "  esac\n"
            "done\n"
            "echo \"$REAL_KNOB\"\n"
        )
        got = dead_flags(fake)
        self.assertIn("--ghost-knob", got)
        self.assertEqual("GHOST_KNOB", got["--ghost-knob"])
        self.assertNotIn("--real-knob", got, "有读取点的开关不许误伤")

    def test_audit_accepts_read_inside_default_expansion(self) -> None:
        """`${VAR:-x}` 也算读取（PHP/Shell 里这是最常见的兜底读法）。"""
        fake = (
            "case \"$1\" in\n"
            "  --knob) KNOB=1; shift ;;\n"
            "esac\n"
            "printf '%s' \"${KNOB:-0}\"\n"
        )
        self.assertEqual({}, dead_flags(fake))

    def test_audit_is_not_fooled_by_assign_only_in_branch(self) -> None:
        """只在本分支赋值、别处从不出现 ⇒ 仍然算死（防止"赋值即被当成读取"）。"""
        fake = (
            "case \"$1\" in\n"
            "  --lonely) LONELY=1; shift ;;\n"
            "esac\n"
        )
        self.assertIn("--lonely", dead_flags(fake))


if __name__ == "__main__":
    for p in COLLECTORS:
        t = p.read_text(encoding="utf-8")
        d = dead_flags(t)
        n = len(branch_blocks(t))
        print(f"{p.name}: 分支 {n} 个，死开关 {len(d)} 个 {d if d else ''}")
