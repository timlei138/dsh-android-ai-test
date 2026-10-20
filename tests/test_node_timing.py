# -*- coding: utf-8 -*-
"""节点执行次数与耗时统计。

## 为什么要有这个

167 排查时，日志写着：

    轮次：27  依次 {'decide': 27, 'assert': 0, 'vision': 0, 'knowledge': 0}

我据此判断"视觉节点压根没跑过"，于是花很长时间去翻路由和图定义 ——
可它明明跑了 6 次，每次都失败。

原因：那个 counts 是数 `__interrupt__` 的 kind 的，
**vision / knowledge / observe / act 都不产生 interrupt，所以永远是 0。**
它不是"0 次"，是"数不到"。

教训：一个数看着像数据，可能只是个空集。计数必须数**真的被执行的东西**。

## 用户另外提的

「模型得时间也要统计在内」—— 模型一次 5~20 秒，点击一次 200ms。
只报总耗时，看报告的人会以为设备慢，实际大头在等模型。
所以按节点分开记，再单列一个 model_s。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = FAIL = 0


def ck(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")


import os  # noqa: E402

os.environ.setdefault("ANDROID_SKILL_ROOT", str(ROOT / "assets"))

import android_agent.agent.nodes as nd  # noqa: E402
import android_agent.agent.runner as rn  # noqa: E402

print("=" * 70)
print("1) 统计口径：不能只数 interrupt")
print("=" * 70)
rsrc = (ROOT / "src" / "android_agent" / "agent" / "runner.py").read_text(
    encoding="utf-8")
ck("★ 用 stream 驱动图（invoke 拿不到中间节点）",
   'app.stream(' in rsrc and 'stream_mode="updates"' in rsrc)
# 只数**真正的调用**，注释里提到 app.invoke 不算
# （第一版这里用 `"app.invoke(" not in rsrc` 匹配，被自己写的说明注释绊倒了）
_live_invoke = [ln for ln in rsrc.splitlines()
                if "app.invoke(" in ln and not ln.strip().startswith("#")]
ck("★ 已经没有生效的 app.invoke（三处都换掉了）",
   not _live_invoke, "还剩: " + str(_live_invoke[:2]))
ck("★ 日志里明确标注了 counts 的口径",
   "只统计会 interrupt" in rsrc or "不含 observe" in rsrc,
   "不标注的话，读日志的人会把 0 当真")

print()
print("=" * 70)
print("2) 模型耗时要单独算")
print("=" * 70)
ck("model_s 在报告里", "model_s" in nd.SubagentReport.model_fields)
ck("node_calls 在报告里", "node_calls" in nd.SubagentReport.model_fields)

_MODEL_NODES = nd._MODEL_NODES
ck("decide 算模型节点", "decide" in _MODEL_NODES)
ck("assert 算模型节点", "assert" in _MODEL_NODES)
ck("observe 不算模型节点（它等设备）", "observe" not in _MODEL_NODES)
ck("act 不算模型节点", "act" not in _MODEL_NODES)
ck("vision 不算模型节点（视觉是另一路）", "vision" not in _MODEL_NODES)

st = {"node_calls": {"decide": {"n": 3, "ms": 5000},
                     "observe": {"n": 5, "ms": 2000},
                     "assert": {"n": 2, "ms": 1500}}}
ck("★ 只累加模型节点（5.0+1.5=6.5）", nd._model_seconds(st) == 6.5,
   str(nd._model_seconds(st)))
ck("没有 node_calls 时返回 0，不去猜", nd._model_seconds({}) == 0.0)
ck("字段残缺也不崩", nd._model_seconds(
    {"node_calls": {"decide": {"n": 1}}}) == 0.0)

print()
print("=" * 70)
print("3) runner 侧统计工具")
print("=" * 70)
calls = rn._new_node_calls()
rn._bump(calls, "observe", 100)
rn._bump(calls, "observe", 300)
rn._bump(calls, "decide", 5000)
ck("次数累计", calls["observe"]["n"] == 2)
ck("耗时累计", calls["observe"]["ms"] == 400)
ck("不同节点分开记", calls["decide"]["n"] == 1)
line = rn._fmt_node_calls(calls)
ck("日志串里有次数和秒数", "observe×2" in line and "decide×1" in line, line)
ck("次数多的排前面", line.index("observe") < line.index("decide"), line)
ck("空表不报错", rn._fmt_node_calls({}) == "(无)")

print()
print("=" * 70)
print("4) 数据库有 model_s 这一列，并且老库能补上")
print("=" * 70)
ssrc = (ROOT / "src" / "android_agent" / "reporting" / "store.py").read_text(
    encoding="utf-8")
ck("★ 建表里有 model_s", "model_s     REAL" in ssrc or "model_s REAL" in ssrc)
# 直接切出 _ADDED_COLUMNS 这个元组本体再找，别用"到第一个右括号"那种切法
# （元组里本身就有右括号，第一版就是这么切错的）
_blk = ssrc.split("_ADDED_COLUMNS = (", 1)[1].split("\n)", 1)[0]
ck("★ 老库迁移列表里有 model_s", '"model_s"' in _blk,
   "老库不补列的话 insert 会报 no column named model_s")
ck("list_runs 带出来", "elapsed_s, model_s" in ssrc)

print()
print("=" * 70)
print("5) 前端把两段拆开显示")
print("=" * 70)
app = (ROOT / "webui" / "app.js").read_text(encoding="utf-8")
ck("★ 有 fmtSplit", "const fmtSplit" in app)
ck("★ 详情页用了 fmtSplit", "fmtSplit(run)" in app)
ck("没有 model_s 的旧记录不显示（不用 0 假装）",
   "model == null" in app and "return ''" in app)

print()
print("=" * 70)
print(f"  {FAIL} 失败 / {PASS + FAIL} 项")
print("=" * 70)
sys.exit(1 if FAIL else 0)
