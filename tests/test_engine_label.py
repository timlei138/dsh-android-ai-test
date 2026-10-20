"""引擎标注 —— 防止"把 autopilot 的成绩当成模型的成绩"。

背景（实测踩过）：
    `answer` 开关曾经**不存在**，于是所有跑出来的数字都是规则应答器
    （autopilot）的，但报告看起来和真模型一模一样（都有轮次/decide/PASS）。
    当时查"纯回放省了多少模型调用"，跑出 decide=0，
    以为模型真没被叫 —— 其实是根本没接模型。

现在的防线有三层，这个测试盯住它们：
    1. 默认值是 model（忘了传 → 真模型，而不是假成绩）
    2. 报告里有 engine 字段（主 Agent 读报告，日志它可能不看）
    3. 渲染时 autopilot 要显眼警告（不能和 model 长得一样）
"""
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

JS = HERE / "runner-tool" / "lib" / "index.js"
PY = HERE / "run_case_json.py"

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


js = JS.read_text(encoding="utf-8")
py = PY.read_text(encoding="utf-8")

print("=== 1) ★ 默认必须是 model（忘了传 → 真模型，不是假成绩）===")
DEFAULT_MODEL = 'args.answer ?? "model"'
DEFAULT_AUTO = 'args.answer ?? "autopilot"'
_n_model = js.count(DEFAULT_MODEL)
check("JS 两个分支默认都是 model", _n_model == 2, f"{_n_model} 处")
check("JS 里不再有 autopilot 作默认", DEFAULT_AUTO not in js)
check("Python 默认也是 model",
      'spec_in.get("answer") or "model"' in py)

print("\n=== 2) ★ 报告里必须有 engine（主 Agent 读的是报告）===")
_ca = (HERE / "src" / "android_agent" / "agent" / "nodes.py").read_text(encoding="utf-8")
check("SubagentReport 有 engine 字段", "engine: str = \"\"" in _ca)
check("SubagentTask 也有 engine（要经 task 传进去）",
      "engine: str = Field" in _ca)
check("run_case 把 engine 塞进 task", 'task["engine"] = engine' in py or
      'task["engine"] = engine' in (HERE / "src" / "android_agent" / "agent" / "runner.py").read_text(encoding="utf-8"))
check("finish 节点从 task 读 engine 写进报告",
      'engine=str(task.get("engine") or "")' in _ca)
check("run_case_json 传 engine 参数", "engine=_engine" in py)
check("output.schema 声明了 engine",
      re.search(r"engine:\s*\{[^}]*required:\s*true", js, re.S) is not None)
check("execute 返回体带 engine",
      'engine: String(report.engine ?? "")' in js)

print("\n=== 2b) ★ 两个被 except:pass 吞掉的 bug（回归）===")
# 这段代码从来没成功执行过，而 except:pass 让它一声不吭。
check("SubagentReport 有 to_dict（否则报告写不出）",
      "def to_dict(" in _ca)
check("SubagentReport 有 report_path 字段（pydantic 拒绝未声明字段）",
      re.search(r"class SubagentReport.*?report_path: str", _ca, re.S) is not None)
check("写报告失败不再静默吞掉", "[report] ⚠️ 报告文件没写成" in py)
check("给 report 赋值 engine 不再包 try/except",
      "report.engine = _engine" in py)

print("\n=== 3) ★ 渲染时 autopilot 要显眼（不能和 model 长得一样）===")
check("有 autopilot 专用警告分支",
      'report.engine === "autopilot"' in js)
check("警告里明说「没有调用模型」",
      "没有调用模型" in js)
check("警告里明说「不能当作用例的真实结论」",
      "不能当作用例的真实结论" in js)
check("model 分支也标注（正向确认）",
      'report.engine === "model"' in js)

print("\n=== 4) 非枚举值要报错，不能静默退回 ===")
# 静默退回正是"拿到假成绩却不知道"的成因 —— 拼错必须炸。
check("Python 校验枚举值", '_engine not in ("model", "autopilot")' in py)
_marker = "answer 只能是"
if _marker in py:
    _ctx = py[py.find(_marker):py.find(_marker) + 300]
    check("报错文案说明为什么不容忍拼写错误", "静默" in _ctx)
else:
    check("报错文案说明为什么不容忍拼写错误", False, "找不到校验文案")

print("\n=== 5) 参数描述写清了「什么场景选哪个」===")
# 只是列两个选项、不说场景，模型只能瞎猜。
desc_start = js.find("answer: {")
desc = js[desc_start:desc_start + 1400]
check("说明跑用例用默认", "跑用例" in desc and "不用传" in desc)
check("说明自测才用 autopilot",
      "autopilot" in desc and "自测" in desc)
check("enum 顺序 model 在前（默认值优先）",
      'enum: ["model", "autopilot"]' in js)

print("\n=== 6) 端到端：两种引擎的报告要能分辨 ===")
# 不真连设备 —— 直接验 run_case_json 对非法值和缺省的处理。
for payload, label, want in [
    ('{"case_name":"__no_such_case__","answer":"bogus"}', "非法值", "ERROR"),
    ('{"case_name":"__no_such_case__","answer":"autopilot"}', "autopilot", None),
    ('{"case_name":"__no_such_case__"}', "不传（默认）", None),
]:
    r = subprocess.run([str(HERE / ".venv" / "Scripts" / "python.exe"),
                        str(PY)], input=payload, capture_output=True,
                       text=True, encoding="utf-8", timeout=180,
                       cwd=str(HERE))
    try:
        rep = json.loads(r.stdout.strip().splitlines()[-1])
        got = rep.get("status")
    except Exception:
        got = "(无输出)"
    if want:
        check(f"{label} -> 应报 {want}", got == want, str(got))
    else:
        # 这两个会因为找不到用例而报错，但**不该**是"answer 不合法"
        check(f"{label} -> 不是被 answer 校验拦下",
              got != "(无输出)" and "answer 只能是" not in str(rep.get("summary", "")),
              str(rep.get("summary", got))[:80])

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
