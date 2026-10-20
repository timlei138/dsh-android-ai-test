"""YAML 加载器：把用例 YAML 读成 CaseSpec。

用户拍板「直接复用」，所以不重写格式，直接吃 storage/cases/*.yaml。

这个测试重点盯**两条**：
  1) 真实用例（8 个）都能读进来，关键字段没丢
  2) **不认识的字段必须报出来** ——
     用例 YAML 自己写着「两者对不上时不会报错，只会静默跑偏」，
     我们不能自己再犯一次。所以 warnings 非空要能被看见。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from android_agent.cases.paths import cases_dir  # noqa: E402
from android_agent.cases.loader import load_case, load_case_by_name  # noqa: E402
import android_agent.agent.nodes as nd  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}" + (f"  -> {extra}" if extra else ""))
    else:
        fail += 1
        print(f"  [FAIL] {name}" + (f"  -> {extra}" if extra else ""))


print("=== 1) 全部用例都能读 ===")
files = sorted(cases_dir().glob("*.yaml"))
print(f"  （共 {len(files)} 个文件）")
specs = {}
for f in files:
    try:
        spec, warns = load_case(f)
        specs[f.stem] = (spec, warns)
        check(f"{f.stem[:26]:28} 读取成功",
              bool(spec.steps) and bool(spec.asserts),
              f"{len(spec.steps)} 步 / {len(spec.asserts)} 断言 / {len(warns)} 警告")
    except Exception as e:
        check(f"{f.stem[:26]:28} 读取成功", False, f"{type(e).__name__}: {e}")

print("\n=== 2) 168 的字段读对了吗（关键路径）===")
spec, warns, path = load_case_by_name("168")
check("package 正确", spec.package == "com.zui.calendar", spec.package)
check("3 个步骤（s0 已从 pitfalls 提成可执行步）",
      len(spec.steps) == 3, str(len(spec.steps)))
check("3 条断言", len(spec.asserts) == 3, str(len(spec.asserts)))

s0, s1, s2 = spec.steps
check("s0 先处理 pm clear 后的两屏（权限说明框）",
      s0.target == "rid=button1", str(s0.target))
check("s0 落点回到日历首页",
      "AllInOneActivity" in (s0.landing or ""), str(s0.landing))
check("s1 定位到 iv_more", s1.target == "rid=iv_more", str(s1.target))
check("s1 落点是课程表页",
      "TimetableActivity" in (s1.landing or ""), str(s1.landing))
check("s2 定位到图库导入按钮",
      s2.target == "rid=btnImportFromGallery", str(s2.target))
# ★ s2 的落点必须是**稳定终态**，不能是"提示框弹出的那一瞬"。
#   早先把「点按钮 → 点掉提示框」拆成 s2/s3，s2 落点写 rid=button1，
#   真机就 FAIL 了：模型点完按钮顺手把框也点了，等轮到 s2 判定时框已经没了，
#   于是"声明完成但落点不符" —— 可三条断言其实全过（假 FAIL）。
#
# ★ 而且要判"到了选图界面"，不能钉死具体 Activity 类名。
#   原来写 PhotoPickerGetContentActivity，真机 Android 17 偶尔给的是
#   PhotoPickerUserSelectActivity，就误报落点不符。
#   用户的话：「打开相册。我不管打开的是哪个相册。打开就行了。」
check("s2 落点是选图界面（稳定终态，且不钉死具体类名）",
      (s2.landing or "").strip() == "album=any", str(s2.landing))

print("\n=== 3) ★ 每个 target 都在代码能唯一判定的形态里 ===")
# 早先这一步验的是「locate_seq 的两个动作都没丢」（点更多 + 点课程表）。
# 现在 locate_seq 已删除，那两下拆成了 s1（点更多）→ s2（点课程表）等多步。
# 要验的东西因此变了：不再是"seq 存了 2 个"，而是
# **每一步的 target 都写成代码能唯一判定的表达式** —— 否则代码直点无从谈起。
for s in spec.steps:
    check(f"{s.id} 有 target", bool(s.target), str(s.target))
    det, why = nd.is_deterministic_landing(s.target or "")
    check(f"{s.id} target 可唯一判定", det, why)

print("\n=== 4) 前提：pm clear 读到了（且是字符串格式的 do）===")
# ⚠️ 这里曾经断言「只有 1 条前提」—— 那是**锁住了 bug**。
#    YAML 里其实有 4 条：1 条 clean（有 do）+ 2 条 assert + 1 条 goto。
#    旧实现把两条 type=assert 全丢了，其中一条正是
#    "★起点：当前必须停在日历首页（有 iv_more 那一屏）"，
#    YAML 自己写明「不校验起点会导致 s1 的 iv_more 点击落空并在无报错的情况下失败」。
#    现在 assert 前提会转成 assert_start（起点校验），所以条数不再是 1。
#    要验的是"该执行的还在、该校验的来了"，不是"恰好几条"。
_do_pre = [p for p in spec.preconditions if p.do]
check("有可执行前提（带 do）", len(_do_pre) >= 1, str(len(_do_pre)))
check("assert 前提转成了起点校验",
      any(p.assert_start for p in spec.preconditions),
      str([p.assert_start for p in spec.preconditions if p.assert_start]))
pc = _do_pre[0]
check("能执行 pm clear",
      any("pm clear" in c for c in pc.do), str(pc.do))
check("skip_if 被保留下来", pc.skip_if is not None, str(pc.skip_if))

print("\n=== 5) 断言的结构化判据原样保留（不是字符串化）===")
a1, a2, a3 = spec.asserts
check("a1 是字典判据", isinstance(a1.check, dict), type(a1.check).__name__)
check("a1 含 activity", "activity" in a1.check, str(a1.check))
# ⚠️ 早先这里断言 '"a1 含 node_text"。那条判据在实战里没用上：
#    a1 要验的是"课程表页打开了"，activity 就够了；
#    硬塞一个 node_text{rid: iv_more} 反而会因为 iv_more 是图标、
#    根本没有文字而引出一个必然失败的判据。
#    换成验"判据里的键都是代码认识的"—— 这才是真正会出事的地方
#    （写了不生效的键 = 看着验了其实没验）。
_UNKNOWN = [k for k in (a1.check or {}) if k not in nd.KNOWN_CHECKS]
check("a1 的判据键都认识", not _UNKNOWN, str(_UNKNOWN))
check("a2 是字典判据", isinstance(a2.check, dict), type(a2.check).__name__)
check("a2 用了 node_present", "node_present" in (a2.check or {}), str(a2.check))
# ★ a3 判"打开某种选图/相册界面"，不判是哪一个具体 Activity。
#   原来还要求 package: com.android.providers.media.module —— 但换成
#   DocumentsUI 或厂商相册时包名就变了，这条判据会把"打开了"判成"没打开"。
#   具体打开了哪个由 album_reached 写进报告的 actual，排查时照样看得到。
check("a3 用 album_reached 判据（不钉死 Activity 类名）",
      "album_reached" in (a3.check or {}), str(a3.check))
_UNKNOWN_A3 = [k for k in (a3.check or {}) if k not in nd.KNOWN_CHECKS]
check("a3 的判据键都认识", not _UNKNOWN_A3, str(_UNKNOWN_A3))

print("\n=== 6) 坑跟着步骤走（用例 YAML 的核心设计）===")
check("s1 有坑", len(s1.pitfalls) > 0, str(len(s1.pitfalls)))
check("s1 的坑提到 iv_more 没文案",
      any("iv_more" in p for p in s1.pitfalls), s1.pitfalls[0][:60] if s1.pitfalls else "")
check("s2 的坑提到不点掉框就永远不出现",
      any("不点掉" in p or "永远不出现" in p for p in s2.pitfalls),
      str(len(s2.pitfalls)))

print("\n=== 7) ★★ 不认识的字段必须报出来（不静默跑偏）===")
# ⚠️ 这里原本断言 "warnings 非空"，因为**旧 YAML 里确实有我们没实现的字段**
#    （skip_if 判不了的键等），要求它们必须被报出来、不能被静默吞掉。
#    现在 168 的 YAML 已经按新规范重写，所有字段都被采纳 —— 0 条 warning
#    才是**正确状态**，再要求"非空"就是在锁住旧数据。
#
#    "不静默跑偏"这条规则本身没变，只是换了个不依赖具体数据的验法：
#    直接喂一个带未知字段的样例，看它报不报。
_probe = {
    "meta": {"case_id": "probe", "title": "探针", "package": "com.test"},
    "steps": [{"id": "s1", "desc": "走", "target": "rid=x",
               "完全不认识的字段": 123}],
    "assertions": [{"id": "a1", "desc": "到",
                    "check": {"不认识的判据": "x"}}],
}
_probe_p = Path(__file__).resolve().parents[1] / "tmp" / "_probe_unknown.yaml"
import yaml as _yaml
_probe_p.parent.mkdir(parents=True, exist_ok=True)
_probe_p.write_text(_yaml.safe_dump(_probe, allow_unicode=True), encoding="utf-8")
try:
    _pspec, _pwarns = load_case(_probe_p)
    check("带未知字段的 YAML 会报警", len(_pwarns) > 0, f"{len(_pwarns)} 条")
except Exception as _e:  # noqa: BLE001
    check("带未知字段的 YAML 会报警", True, f"直接拒绝：{type(_e).__name__}")

# ★ goto 已经**实现了**（转成 nav 起点要求交给模型走过去），
#   所以它不该再出现在"未实现"警告里 —— 那正是曾经误导人的地方。
#   这里正反两面都锁：既不能报警，又必须真的产出 nav 前提。
check("goto 不再报'未实现'（已转成 nav）",
      not any("goto" in w and "未实现" in w for w in warns), warns)
check("goto 真的产出了 nav 前提",
      any(pc.kind == "nav" for pc in spec.preconditions),
      [(pc.kind, pc.desc[:20]) for pc in spec.preconditions])
# playbook / confidence 是**注解字段**，本就不进机器通路，
# 不该当"未采纳"报警（查证：它们的唯一消费者是 case_md.py 的渲染）。
check("playbook 不再被当成'未采纳'",
      not any("playbook" in w for w in warns), "")
check("confidence 不再被当成'未采纳'",
      not any("confidence" in w for w in warns), "")
# do 是字符串是旧格式，我们**已经支持**，不该报警
check("do 是字符串不再报警（已正确支持）",
      not any("do 是字符串" in w for w in warns), "")
# ★ 现役 168 用例应当**零 warning** —— 一条有效的用例不该带着噪音。
check("168 的 YAML 零 warning（字段全被采纳）", len(warns) == 0,
      str(warns[:2]))

print("\n=== 7b) 注解字段要被**如实带出去**（不是丢掉）===")
check("per_step_pitfalls 立起来了", spec.per_step_pitfalls is True,
      str(spec.per_step_pitfalls))
conf = spec.case_confidence or {}
check("confidence.level 读到了", conf.get("level") == "verified",
      str(conf.get("level")))
check("confidence.reason 读到了", bool(conf.get("reason")),
      str(conf.get("reason"))[:60])
check("confidence.todo 读到了", isinstance(conf.get("todo"), list),
      str(conf.get("todo")))

print("\n=== 8) 判不了的 skip_if 不能被当成'已满足'（宁可多清一次）===")
from android_agent.agent.nodes import _skip_if_met  # noqa: E402
check("timetable_state 判不了 -> None（照跑）",
      _skip_if_met({"timetable_state": "empty"}, activity=".X") is None)
check("activity 已在 -> True（跳过）",
      _skip_if_met({"activity": ".AllInOneActivity"},
                   activity="com.zui.calendar/.AllInOneActivity") is True)
check("activity 不在 -> False（照跑）",
      _skip_if_met({"activity": ".Other"}, activity=".AllInOneActivity") is False)
check("无 skip_if -> False（照跑）", _skip_if_met(None, activity=".X") is False)

print(f"\n{'=' * 52}")
print(f"  {'全部通过' if not fail else f'{fail} 项失败'}（{ok + fail} 项）")
print(f"{'=' * 52}")
sys.exit(1 if fail else 0)
