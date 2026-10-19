"""采集侧「这项到底采没采」的唯一判据（跨库共用）。

为什么单独成模块
----------------
「没采到」与「没问题」在报告里的长相**必须不同**（批 14 立的规矩）。但有三条路会让
它们长得一样：

① ``if rows:`` —— 「键写错」与「查了 0 行」都落 ``[]``；
② ``or <默认值>`` —— 缺数据时**主动编造**一个 0（比 ① 更坏）；
③ **两套 id 命名空间** —— 展示层按「报告章节」组织（``pg.performance`` 一章含
   I/O / 检查点 / WAL 多张表），采集台账按「采集项」登记（``pg.stat_io`` /
   ``pg.checkpointer`` / ``pg.stat_wal``）。拿章节 id 去查台账，多数**查不到**，
   于是落回「按渲染行数猜」；更坏的是有 5 个章节 id 与台账的 **category** 同名
   （``pg.performance`` 既不是 item_id 却又是 category）⇒ 一旦有人"顺手用名字查"，
   会静默取到**另一批**项的状态。

本模块只提供**判据与归并**，不认识任何具体库的 item_id —— 那是各插件自己的映射表
（如 ``PG_SECTION_SOURCES``）。它也不知道报告长什么样。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

#: 「查了、确认没有」。与「根本没查」的报告结论**相反**，所以必须是一个独立取值。
EMPTY_STATE = "empty"

#: 「这项**采集过**吗」的判据集合。``empty``（查了、确认 0 行）**算采集过**。
DATA_STATES = frozenset({"ok", EMPTY_STATE, "partial"})

#: 完整度**算到位**的状态 = `DATA_STATES` 去掉 `partial`。
#:
#: `partial`（部分有证据）在**规则层**够用了（可以下结论），但在**完整度**上确实是
#: 个缺口 —— 所以两个判据刻意不同源。这里用**差集**表达，`DATA_STATES` 增删时自动跟上。
#:
#: ★ 打分一律**正着数**（`status in SCORE_OK_STATES`），不许写
#:   `status not in error_states | warning_states`：判否式在新状态词忘了登记时
#:   **不抛异常、不报错、分数反而变高** —— 这是本项目里「没采到」变「没问题」的经典入口。
#:   正着数的最坏后果是"新状态被算成缺口"（分数偏低、缺口表多一行），**方向是安全的**。
#:   实测过的具体漏洞：缺状态（``""``）不在任何失败集合里 ⇒ 判否式把它算成「到位」。
SCORE_OK_STATES = frozenset(set(DATA_STATES) - {"partial"})

#: 「没采到」。缺状态（``""``）与未登记（``unmapped``）也按这一档处理 ——
#: 宁可写「证据不足」，不可写「检查通过」。
#: ``schema_mismatch``（表在、列不在）是**采集器缺陷**，同样不是"没有数据"。
NO_DATA_STATES = frozenset({
    "unavailable", "permission_denied", "timeout", "error", "missing",
    "not_enabled", "skipped", "unsupported", "schema_mismatch",
    "unknown", "unmapped",
})

#: 「当前环境用不上」，既不是采到也不是没采到 —— 规则层按 `not_applicable` 处理，
#: 不能落进 `DATA_STATES`（否则会被判成"有证据 ⇒ 可以下通过结论"）。
NOT_APPLICABLE_STATES = frozenset({"not_applicable"})

#: **本环境不适用** —— 采集器按设计没采，或环境根本没有这个东西。
#:
#: 与「没采到」的区别：它不是缺陷，是**环境事实**（目标机没装 barman ≠ 采集失败；
#: 顶层指标已改由逐库采集 ≠ 这一项漏采）。两者在报告里的**去向也不同**：
#: 前者是"这项本来就问不到"，后者是"该问没问到"。
#:
#: ★ 完整度分母必须剔除它们 —— 否则会得出一个**看着精确、含义是错的**数字：
#:   真机实测 113 项里 16 项属于此类，完整度被压到 83.2%，而**真正"该采未采"的只有 3 项**。
#:   这台机器装的东西越少，"完整度"越低 —— 而真实结论恰恰是"这个环境本来就不需要这些"。
#: ★ 剔除的是**统计口径**，不是透明度：这些项仍逐条保留在采集侧缺口清单里，
#:   附录「数据缺口」表照印（读者要能看到"为什么没有这一项"）。
OUT_OF_SCOPE_STATES = frozenset({"not_applicable", "skipped", "unsupported"})

#: 归并时"越靠前越严重"（仅用于挑出该报哪一个）。**不是**章节归并的主判据 ——
#: 见 `merge_section_state`：章节归并问的是"证据够不够"，不是"最坏有多坏"。
_SEVERITY_ORDER = ("error", "schema_mismatch", "timeout", "permission_denied")


def state_is_collected(state: str) -> bool:
    """「采集侧状态」→ **这项采集过吗**。

    ``empty``（查了、确认 0 行）算采集过；``skipped`` / ``unsupported`` /
    ``unavailable`` / ``error`` 不算；缺状态（``""``）一律按**没采到**处理。
    """
    return str(state) in DATA_STATES


def is_no_data(state: str) -> bool:
    """**没采到**（含缺状态）。与 `state_is_collected` 互补，但 `not_applicable` 两边都不算。"""
    return str(state) == "" or str(state) in NO_DATA_STATES


def is_out_of_scope(state: str) -> bool:
    """**本环境不适用**（按设计跳过 / 环境不支持）。

    与 `is_no_data` 有交集（`skipped` / `unsupported` 两边都算），但**语义不同**：
    `is_no_data` 回答"能不能下分析结论"（不能 ⇒ 写"证据不足"），
    `is_out_of_scope` 回答"这一项该不该被计入完整度分母"（不该 ⇒ 剔出统计口径）。

    ★ 缺状态（``""``）**不算**不适用 —— 不知道就是不知道，宁可留在分母里当缺口。
    """
    return str(state) in OUT_OF_SCOPE_STATES


def scores_as_ok(state: str) -> bool:
    """完整度上**算到位**吗（`ok` / `empty`）。正着数 —— 见 `SCORE_OK_STATES`。"""
    return str(state) in SCORE_OK_STATES


def worst_state(states: Sequence[str]) -> str:
    """挑出"该优先报"的那一个：严重失败 > 没采到 > 采到。空输入返回 ``""``。"""
    known = [str(state) for state in states if str(state)]
    if not known:
        return ""
    for severe in _SEVERITY_ORDER:
        if severe in known:
            return severe
    for state in known:
        if state in NO_DATA_STATES or state in NOT_APPLICABLE_STATES:
            return state
    return known[0]


def merge_section_state(states: Sequence[str]) -> str:
    """多个采集项状态 → **一个「这一章的采集状态」**。

    ====================  ===================  ==================================
    输入                  返回                 含义
    ====================  ===================  ==================================
    空                    ``unmapped``        台账里一个都查不到 —— **不默认 ok**
    含 error/timeout 等   该最严重者          采集失败，重采有意义
    全在 {ok, partial}    ``ok``              证据齐
    全 empty              ``empty``           都查了、确实没有
    全 skipped            ``skipped``         按设计跳过
    全 unsupported 等     该值                环境不支持 / 未启用
    全 not_applicable     ``not_applicable``  当前架构用不上
    混合                  ``partial``         **部分有证据** —— 必须写出来
    ====================  ===================  ==================================

    ★ 混合一律 ``partial`` —— 既不能因为"有一项 ok"就说这一章齐了（把没采到的子项
      藏起来），也不能因为"有一项 skipped"就说整章没采到（把已有的证据丢掉）。
      这是「未采集 ≠ 0」在**章节粒度**上的最小诚实表达。
    ★ 返回 ``unmapped`` 而不是 ``ok``：映射表没写、台账里没登记，**不知道**就是不知道。
      调用点应把它当"证据不足"处理（`is_no_data("unmapped")` 为真）。
    """
    known = [str(state) for state in states if str(state)]
    if not known:
        return "unmapped"
    distinct = set(known)
    if len(distinct) == 1:
        return known[0]
    for severe in _SEVERITY_ORDER:
        if severe in distinct:
            return severe
    collected = {state for state in distinct if state in DATA_STATES}
    if not collected:
        # 一项证据都没有：全是同一类"没采到"时按该类回答，否则报最坏的那一个。
        return worst_state(known)
    if collected == distinct:
        return "ok"
    return "partial"


def expand_source_names(index: Mapping[str, Any], names: Sequence[str]) -> list[str]:
    """按别名展开成台账里**实际存在**的键，保序去重；支持 ``前缀.*.后缀`` 通配。

    逐库采集的项在台账里是 ``db.mydb.table_health`` / ``db.postgres.table_health``
    这样**一条一个库**，展示层的章节却是"全部库" ⇒ 必须展开，否则只能看到其中一个库
    （只取第一个命中的库，报告就会把另一个库的缺口静默藏起来）。

    ★ **不补空状态**：``names`` 里写了但台账里没有的项，**不出现在结果里**。
      调用方据此得到 ``[]``，`merge_section_state` 判成 ``unmapped`` ——
      "查不到"不许被静默吞掉（这正是它和"查了 0 行"必须分开的地方）。
    """
    found: list[str] = []
    seen: set[str] = set()
    for name in names:
        name = str(name)
        if not name:
            continue
        if "*" in name:
            prefix, _, suffix = name.partition("*")
            for key in sorted(index):
                if key.startswith(prefix) and key.endswith(suffix) and key not in seen:
                    seen.add(key)
                    found.append(key)
            continue
        if name in index and name not in seen:
            seen.add(name)
            found.append(name)
    return found
