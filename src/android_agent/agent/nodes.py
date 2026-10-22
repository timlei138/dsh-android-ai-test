"""子 Agent：一条用例 = 一个子 Agent = 一张图 + 一个 thread。

上下文隔离的关键（这是本文件存在的理由）：
  主子两层要隔离，子 Agent 的 **state 就不能堆 dump**。
  observe/act 的返回里有 next 清单、nodes、dump 路径 —— 这些是"当轮工作集"，
  用完即弃；只有**结论**（finding / 问题 / 落点）才留在 state 里回报主 Agent。

  所以：
    - 工作集（next/payload）放在 interrupt 的 payload 里，不进 state
    - state 里只累积 findings / issues / visited，都是短记录
    - 回报给主 Agent 的是 SubagentReport（结构化摘要），不是对话历史

子 Agent 的"自主检索文档"：
  图给了它 read_knowledge 节点 —— 它自己决定查哪份知识卡/路径图（见 KNOWLEDGE）。
  这不是把文档塞给它，而是给它**检索能力**，与你的设计一致。
"""

from __future__ import annotations

import json
import operator
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, Field, model_validator

from ..device.bridge import RealDevice, ToolError, is_system_permission_pkg
from . import knowledge as knowledge_mod
from .vision import VisionError, ask_image

# 一条用例内最多检索知识卡的次数。防模型反复换词检索却不推进——
# 真机上每次检索都要读文件，而步骤毫无进展纯属烧时间。
MAX_KNOWLEDGE_LOOKUPS = 8

# 一步最多几轮 decide（保护 2：防死循环）。
MAX_ROUNDS_PER_STEP = 40

# 换目标查找上限（rid 太严，有些控件本来就没 rid）。
MAX_LOCATE_ATTEMPTS = 3

# ★ 一步落点不符，最多试这么多次（含首次），仍不符就 BLOCKED 停整个人 case。
#
# 为什么是 3：用户定的。1 次太急 —— 页面还在加载就会被误判；
# 再多也没意义 —— 同一个动作重复三次都到不了目标页，说明路径本身就不对，
# 继续试只是在烧时间，而且会让"关键路径没找到"这个结论变模糊。
LANDING_MAX_TRIES = 3

# 同一个强证据目标连续点几次没效果，就不再直点、改问模型。
#
# 为什么是 2 而不是 3：点击本身约 200ms，观察约 800ms，一次往返 1 秒多。
# 两次都没反应（2 秒过去了，屏幕连 activity 都没换）已经足够说明
# "不是页面没加载完"，第三种可能只剩"有前置条件没满足" —— 那正是模型该介入的场景。
# 176 实测：这个值设成无穷大时点了 37 次，白花 70 秒且毫无进展。
STRONG_MAX_SAME_TARGET = 2


# 视觉通道连续失败几次后，**禁止再问**，强制模型用现有信息决定。
#
# 为什么需要这个闸门（167 真机，2026-10-20）：
#   视觉后端把 max_tokens 全花在 reasoning 上，返回空文本。
#   模型不知道"没看成"，下一轮换个说法再问，又空 —— 连续 16 轮、
#   白烧 100 多秒，最后靠瞎猜一个坐标才走出来。
#
# 光是"报错"没用（原有的 issues 就是这么做的，模型看不见）。
# 必须**把 vision_question 从 action 里摘掉**，路由才会回到 decide，
# 模型手上没了那个合法动作，才可能去想别的办法。
#
# 为什么是 2：第一次失败可能是网络抖动，给一次重试；
# 同一个问题失败两次就不是偶发了，再问也不会有新答案。
VISION_MAX_FAILURES = 2


# ★ 代码判定判"不成立"时，交 AI 复核的最大轮次（用户要求："AI 判断 3 次上限"）。
#
# 为什么"不成立"需要复核，而"成立"不需要：
#   · 成立  = 代码在留痕里**搜到了**证据，是硬事实，复核只会浪费一轮 LLM；
#   · 不成立= 代码**没搜到**证据。没搜到的原因有一半不是产品的错：
#            文案被拆进多个节点、控件没进可点列表、seen_screens 截断、
#            YAML 里的 rid 改了名。拿"没搜到"直接判 FAIL，等于让一个
#            过期的判据变成对产品的指控（实测 177 a4 / 186 a2、a4）。
#
# 为什么是 3：和 LANDING_MAX_TRIES 同一个理由 —— 用户定的。
# 复核用尽仍判不了，就**维持代码结论**并注明"AI 复核过 N 次"，
# 绝不因为"没复核成"就把 FAIL 洗成 PASS。
ASSERT_AI_RECHECK_MAX = 3

# ★ 一条断言"证据不够 -> 退回补动作"最多几次（用户定的：3）。
#
# 和 ASSERT_AI_RECHECK_MAX 的区别：
#   · 复核：模型再看一遍**同样的 XML**，靠想；
#   · 补动作：执行器**真的去点一下**（展开面板、翻页、滚动），界面变了，看得见的更多。
# 后者才解决 a13 那种"面板根本没展开"的问题 —— 想多少遍都看不到色块。
#
# 用尽仍看不到 -> 判 **FAIL**（用户定的：「那就如果报 FAIL。验证不报 BLOCK」）。
# 但这只针对"补过、界面也动过、还是没有"—— 那说明产品确实没做到。
# 若补的过程中**执行本身失败**（点不到控件、设备出错），仍走 BLOCKED（没验成）。
ASSERT_FIX_MAX = 3

# --------------------------------------------------------------------------
# 主 Agent -> 子 Agent 的任务包
# --------------------------------------------------------------------------

class StepSpec(BaseModel):
    """主 Agent 拆解出来的一步。"""
    id: str = Field(description="步骤号，如 s1")
    desc: str = Field(description="这一步要做什么")
    landing: str | None = Field(default=None, description="落点判据，如 activity=XxxActivity")
    pitfalls: list[str] = Field(default_factory=list, description="该步专属的坑")
    locate: str | None = Field(default=None, description="定位方式，如 rid=iv_more")
    # ★ 强证据目标：这一步要点的东西，怎么**在与分辨率无关的前提下唯一确定**它。
    #
    #   写法（docs/执行链路-边走边验.md）：
    #     rid=iv_more                    可点清单里精确匹配，必须唯一
    #     xpath=//*[@resource-id="..."]  落盘 XML 里算命中数，必须恰好 1 个
    #     text=课程表                     可点清单里按文案匹配，必须唯一
    #     desc=更多                       可点清单里按描述匹配，必须唯一
    #     key=back                        按键动作
    #
    #   命中恰好 1 个 -> 代码直接点，全程不问模型
    #   命中 0 个 / 多于 1 个 -> 交回模型判断（绝不赌）
    target: str | None = Field(
        default=None,
        description="强证据目标表达式（rid=/xpath=/text=/desc=/key=），不存坐标")
    # ⚠️ locate_seq 已删除 —— 它是"按顺序重放的旧机制"。
    #    旧含义：一步里依次 tap 多个目标（点「更多」再点「课程表」）。
    #    现在由 **多步** 表达（s1 点更多、s2 点课程表），每一步都有自己的
    #    target + landing，代码能逐步验证，而不是一口气盲点两下。


class AssertSpec(BaseModel):
    """一条预期结果。

    `check` 支持两种形状：
      · 字符串  —— 我们自己的 --expect 语法（activity=Foo,rid=bar）
      · 字典    —— 用例 YAML 的**结构化判据**
                   {activity: ".Foo", node_text: {rid: x, equals: "课程表"}}
    两种都由 `evaluate_check()` 判；判不了的（要看图）才交回模型。
    """
    id: str = Field(description="预期号，如 a1")
    desc: str = Field(description="预期的文字描述")
    check: str | dict[str, Any] | None = Field(
        default=None, description="机器可判的判据（字符串或结构化字典）")
    # ★ 注解：这条判据为什么这么写 / 证据从哪来。
    #   原实现在 YAML 里写了 note 但这里没声明 —— pydantic 静默丢掉，
    #   写的人以为留下了依据，其实一个字都没存下来。
    #   （和上面"字段必须在 schema 里声明"是同一个坑，第二次踩。）
    note: str = Field(
        default="", description="判据的来由/证据出处，不参与判定")
    # 用例原文里的预期写法，纯文本、不参与判定。
    result: str = Field(
        default="", description="原始用例里的预期原文，供人工对照")
    # ★★ 这条预期属于哪一步（如 "s3"）。
    #
    # 为什么必须有：断言是在**整个流程末尾统一判**的，而每步的界面证据
    # 各不相同。没有这个绑定，"判 a6 时该看哪一屏"就无从得知，
    # 只能拿最后一屏硬判 —— 实测（run 68/69）a6 验的是「新建课程表」的
    # 完成按钮，却拿到「编辑课程」页，模型只能答"看不到完成按钮"，
    # 记成 BLOCKED。看起来像模型不行，其实是给错了那一屏。
    #
    # `None` / "" 表示未绑定：判的时候退回"整轮最后一屏"，并在提示词里
    # 明确告知模型"这条没绑定步骤，别拿这一屏硬判"。
    step: str = Field(
        default="", description="所属步骤 id（如 s3）；空=未绑定")


class Precondition(BaseModel):
    """一条前提条件。

    分两类，处理方式**不同**（这是实测需求）：

    1. kind="data" —— 数据类清理（pm clear、清测试数据）
       数据是执行者自己造的 / 测试账号，清了没问题 -> 可直接执行，无需恢复。

    2. kind="env"  —— 环境/硬件类（关 WiFi、飞行模式、改亮度）
       ⚠️ **执行后必须恢复原状**，否则污染下一条 case。
       所以 env 类必须成对给出 do / undo，且 undo 在收尾时**无论成败都要跑**。
    """
    kind: Literal["data", "env", "nav"] = "data"
    desc: str = ""
    do: list[str] = Field(default_factory=list,
                          description="shell 命令，如 pm clear com.zui.calendar")
    undo: list[str] = Field(default_factory=list,
                            description="env 类必填：恢复命令，收尾时无条件执行")
    # ★ 跳过条件（来自用例 YAML）：条件已满足就别执行这条前提。
    # 例：{activity: .AllInOneActivity} —— 已经在该界面了；
    #     {timetable_state: empty}       —— 数据已经是空的，不用再 pm clear。
    # ⚠️ 判不了的时候**照跑**（宁可多清一次，也不能该清不清）。
    skip_if: dict[str, Any] | None = Field(
        default=None, description="满足则跳过本条前提；判不了则不跳")
    # 起点断言：子 Agent 进来先验，不符则自己想办法走回去（见 route 说明）
    assert_start: str | None = Field(
        default=None, description="起点断言，如 activity=AllInOneActivity")
    # ★ nav（type: goto）专用：目标是哪、以及用例 YAML 记下的路径提示。
    #
    # 为什么是"提示"而不是"命令"：goto 的 path 是自然语言
    #（「点右上角更多 iv_more → 菜单里点课程表」），没法机械转成点击。
    # 所以交给模型读它、自己走过去：不在目标状态 → 走最短路径过去。
    target: str = Field(default="", description="要到达的目标界面（自然语言）")
    hint: str = Field(default="", description="怎么过去的路径提示（给模型看）")

    @model_validator(mode="after")
    def _env_needs_undo(self):
        if self.kind == "env" and not self.undo:
            raise ValueError(
                f"env 类前提必须给 undo（恢复命令），否则会污染下一条用例：{self.desc}")
        return self


class SubagentTask(BaseModel):
    """主 Agent 派给子 Agent 的完整任务包。"""
    case_id: str
    title: str
    package: str
    steps: list[StepSpec]
    asserts: list[AssertSpec]
    preconditions: list[Precondition] = Field(default_factory=list)
    knowledge_files: list[str] = Field(default_factory=list,
                                       description="可供子 Agent 检索的文档，如 knowledge/com.zui.calendar.md")
    start_assert: str | None = Field(
        default=None, description="全局起点断言，如 activity=AllInOneActivity")

    # ── 运行时权限策略 ──────────────────────────────────────────
    # 默认 grant（工具层自动点「允许」，抢 6 秒窗口），**不用声明**。
    # 只有要测「拒绝分支」时才填 —— 对应 SKILL「默认同意，需要拒绝时显式声明」。
    perm_action: str | None = Field(
        default=None, description="grant / deny；留空=默认同意。测拒绝分支才填 deny")
    perm_name: str | None = Field(
        default=None, description="权限短名（camera/media_images/notifications/…），可留空")

    # ── 文件级注解 ──────────────────────────────────────────────
    # per_step_pitfalls：用例 YAML 的「按步读坑」元规则 ——
    #   坑一律写在它真正会出现的那一步，**走到哪步就只看那步的 pitfalls**。
    #   为什么这是对的：回放是按步走的，第 1 步的坑跑到第 2 步还在眼前会
    #   **稀释信息**，真正相关的那条反而看不见（旧文件原话）。
    #   我们本来就把 pitfalls 按步喂给 decide，这个标志只是让"这是有意为之"
    #   变得显式 —— 而不是碰巧。
    per_step_pitfalls: bool = Field(
        default=False, description="坑按步给出（不汇总到全局）")
    # case_confidence：这条用例可不可信（用例 YAML 的 confidence）。
    #   **不参与判定**，只在报告里提示 —— level != verified 时，
    #   主 Agent 该知道"这次 PASS 不代表它稳定"。
    case_confidence: dict[str, Any] = Field(
        default_factory=dict, description="level / reason / todo")
    # engine：这次是谁在做判断（"model" / "autopilot"）。
    #
    # ⚠️ 必须走 task 传进来，不能等 run_case 拿到 report 再赋值 ——
    #   报告文件是 **finish 节点写**的（child_agent.py 的 `out.write_text`），
    #   而 `run_case` 是**从那个文件重新读**出 report 的。
    #   事后赋值只改了内存里的对象，文件里仍然是空的。
    #   （这个坑真实发生过：engine 一直是 ""，查了半天。）
    engine: str = Field(
        default="", description="model / autopilot —— 谁在做判断")


class SubagentReport(BaseModel):
    """子 Agent 回报给主 Agent 的汇总结论（结构化，短）。"""
    case_id: str
    status: str                       # PASS / FAIL / BLOCKED（只有这三档）
    summary: str
    session_id: int | None = None
    # ★ 本次运行的产物目录（tmp/runs/<uuid>_<时间戳>_<case>/）。
    #   截图、XML、日志、账本、报告都在里面 —— 落库时用它反查"这一次"的现场。
    run_dir: str = ""
    # 崩溃日志片段（只有真崩了才有内容）。崩了却没 log 等于没法定位。
    crash_log: str = ""
    # ★ 模型对话原文（每轮提示词 + 模型回复）。
    #   用户报过「LLM的记录没有」—— 原因是 decide.py 的 _llm_trace
    #   只在设置了 LLM_TRACE_FILE 时才写文件，而没人设过。
    #   现在执行器自己指定位置并把它带进报告，WebUI 才能点开看。
    llm_path: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)      # 过程中遇到的问题
    steps_done: int = 0
    coverage_ok: bool = False
    # 前提处理记录：下一条 case 据此判断现场是否干净
    preconditions_done: list[str] = Field(default_factory=list)
    restored: list[str] = Field(default_factory=list)    # env 类的恢复记录
    # 授权/弹窗处理记录 —— **必须落报告**。
    # 自动点"同意"会改设备权限状态，属于对现场有副作用的动作。
    # 不记录的话，事后没人知道这条用例授权了什么、下次起跑线是否已变。
    permissions_granted: list[dict[str, Any]] = Field(default_factory=list)
    dialogs_dismissed: list[dict[str, Any]] = Field(default_factory=list)
    # Android 运行时权限（GrantPermissionsActivity）的自动授予记录。
    # 由工具层自动点（默认 grant），这里记账 —— 用来回答"点了哪几个权限"。
    runtime_permissions: list[dict[str, Any]] = Field(default_factory=list)
    runtime_permission_prompts: list[dict[str, Any]] = Field(default_factory=list)

    # ★ 进度轨迹（用户的第 2 条优化：子 Agent 的状态要能上报，别让主 Agent 干等）。
    #
    # 主 Agent 派发后只能等结果，中间发生了什么它看不见 ——
    # 卡住的时候完全不知道卡在哪。这里把每一步的"动作 + 结果 + 耗时"
    # 记成一条条事件，随报告回传。
    #
    # 刻意保持**短**（每条一行）：主 Agent 要的是"现在到哪了"，
    # 不是把子 Agent 的完整日志搬过来（那就等于没隔离 context）。
    progress: list[dict[str, Any]] = Field(default_factory=list)
    # 卡在哪 —— 直接给出"最后成功的一步"和"卡住的步骤"，供主 Agent 快速定位
    stalled_at: str = Field(default="", description="卡住的位置（空=没卡）")
    elapsed_s: float = Field(default=0.0, description="总耗时（秒）")
    # 模型相关节点（decide/assert/knowledge）的累计耗时。
    # 和 elapsed_s 的差就是设备/截图/IO 花掉的时间。
    #
    # 为什么要分开：模型一次 5~20 秒，一次点击才 200ms。
    # 只报总耗时，看报告的人会以为"设备慢"，实际大头在等模型。
    model_s: float = Field(default=0.0,
                           description="模型耗时（秒，decide/assert/knowledge）")
    # 节点执行次数与耗时 {节点名: {"n": 次数, "ms": 毫秒}}
    node_calls: dict[str, Any] = Field(default_factory=dict)
    # 这条用例的历史可信度（用例 YAML 的 confidence，**不参与判定**）。
    # 只做提示：level != verified 时，这次 PASS 不代表它稳定。
    case_confidence: dict[str, Any] = Field(default_factory=dict)
    # ★ 纯回放的自动化率 —— 量化"省了多少模型调用"。
    # 用户要的 B 档：能代码走就代码走，走不通才叫 AI。
    # 跑完直接看这个数就知道纯回放顶不顶用（而不是靠感觉）。
    strong_stats: dict[str, Any] = Field(default_factory=dict)

    # ★ 这次是谁在做判断："model"（真模型）或 "autopilot"（规则应答器）。
    #
    # 为什么必须进报告：两者的报告**长得一模一样**（都有轮次/decide/PASS），
    # 但 autopilot 根本没接模型。只在日志里标注不够 ——
    # 主 Agent 读的是报告，日志它可能不看。
    # 实测被这个坑过一次：把 autopilot 的成绩当成了模型的成绩。
    engine: str = ""

    # ★ 每步实际走的路径（回写 YAML 的依据，也给主 Agent 核对）。
    # 形如 [{"step":"s3","actions":[{"key":"back"},{"rid":"btnImportFromGallery"}]}]
    action_trace: list[dict[str, Any]] = Field(default_factory=list)
    # ★★ 用例 YAML 里**写了但代码没用上**的判据字段。
    #
    # 为什么必须进报告：加载器一直在 alerts 里打印
    #   `expect.text_present 未实现（未参与判定）`
    # 但那只是 stdout 的一行字 —— 主 Agent 读的是**报告**，它看不到。
    # 扫描 8 个存量用例发现 14 种字段被丢弃（text_present / dialog_closed /
    # package / title ...），**等于这些判据从来没生效过，而没人知道**。
    #
    # ⚠️ 这不等于都是 bug：像 text_present 这类本来就该交 AI（判不准），
    #    被跳过是对的。但"跳过"这个事实必须让主 Agent 看见，
    #    它才能判断"这条用例的某条预期其实没验"。
    yaml_unused_fields: list[str] = Field(default_factory=list)

    # 报告文件落盘路径（由 run_case 填，回给主 Agent 用）。
    #
    # ⚠️ 必须声明成**字段**：pydantic v2 默认拒绝给未声明的属性赋值
    #   （`"SubagentReport" object has no field "report_path"`）。
    #   而那个赋值点包在 `try/except` 里 → 错误被吞掉 →
    #   **报告文件从来没写出来过，且一声不吭**。
    #   一度还以为是 `to_dict` 方法名错了（那个也确实错了，见下）。
    #   教训：pydantic 模型上加属性，先加字段。
    report_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化成可 JSON 的 dict（pydantic v2 里 model_dump 是同一个意思）。

        ⚠️ 这个方法以前**不存在**，但 `run_case_json.py` 一直在调
        `report.to_dict()` 写报告文件，而调用点包在 `try/except: pass` 里 ——
        于是 `AttributeError` 被**静默吞掉**，那个
        `report_<id>_subagent.json` **从来就没被写出来过**，
        而且没有任何报错。排查时只看到"文件不存在"，
        完全不知道是方法名错了。
        ⇒ 保留这个别名（兼容既有调用），但调用方不要再吞异常了。
        """
        return self.model_dump()


# --------------------------------------------------------------------------
# 子 Agent 内部状态（刻意保持"薄"——只有结论，没有 dump）
# --------------------------------------------------------------------------

class ChildState(TypedDict, total=False):
    task: dict[str, Any]               # SubagentTask 序列化

    # 进度
    step_index: int
    round_in_step: int
    max_rounds_per_step: int
    # check 节点的独立轮次计数：堵"反复判完成却没推进"的死循环。
    # round_in_step 只在 act 里增长，模型光说 done 不点时它不动，
    # 于是那个收口条件永远不成立（167 实测空转到 80 轮上限）。
    check_rounds: NotRequired[int]

    # ★ 断言"代码判不成立 -> 交 AI 复核"的轮次（见 ASSERT_AI_RECHECK_MAX）。
    #
    # assert_ai_rounds  当前这条断言已经被复核了几次
    # assert_ai_last    上一次复核时模型给的意见（用尽轮次时写进 actual，
    #                   让人看得出"AI 看过，但它也判不了/它说的不一样"）
    #
    # ⚠️ 必须在这里声明，不能只靠节点返回值"兜着"：
    #    LangGraph 的状态字段是声明的那些，未声明的键在下一次节点派发时
    #    可能被丢掉 —— 表现就是计数永远是 0、复核无限循环。
    assert_ai_rounds: NotRequired[int]
    assert_ai_last: NotRequired[str]
    # ★ 当前这条断言正在等哪一轮复核回来（复核对的是哪个 assert_id）。
    #   没有它，interrupt 恢复后分不清"这是复核的回复"还是"普通断言的回复"。
    assert_pending_for: NotRequired[str]

    # ★★★ 断言"证据不够 -> 退回补一步"的轮次（用户要求）★★★
    #
    # 用户原话：
    #   「检查 10 个色块。条件不满足了。那就退回执行增加个步骤呗」
    #   「最多 3 次，但要防止一直点相同的」
    #   「那就如果报 FAIL。验证不报 BLOCK」
    #
    # assert_fix_rounds   这条断言已经补过几次（上限 ASSERT_FIX_MAX）
    # assert_fix_for      正在为哪条断言补（补完回来重新判它）
    # assert_fix_tried    这次已经试过的动作签名（防重复点同一个东西）
    # assert_fix_note     执行器对这次补证据的自然语言说明（写进 actual）
    assert_fix_rounds: NotRequired[int]
    assert_fix_for: NotRequired[str]
    assert_fix_tried: NotRequired[list[str]]
    assert_fix_note: NotRequired[str]
    # 交给执行器的"补证据任务卡"（要验什么/为什么不够/相关元素）
    assert_fix_task: NotRequired[dict[str, Any]]
    # ★ 当前正在判哪一步的断言（用户要求的"一个点一个点验证"）。
    #
    # 「判断应该是一个昨晚然后判断下一个。这是正常人类的思维。」
    # 做完一步就把这一步的断言判掉、判完再走下一步 ——
    # 判的时候界面还停在那一屏，证据是拿得到的。
    assert_step: NotRequired[str]
    # ★ 这一步已经走完、该验了（用户定的「合入」：边走边验，不回头验）。
    #
    # 「AI走的时候第一轮就边走边验证了。能拿到所有的记录。然后最后根据
    #   记录写json。而不是回过头就复验证。」
    #
    # 非空 = 本步完成、正在验它名下的预期；验完清空、推进下一步。
    verify_step: NotRequired[str]
    # ★ 正在判哪一条预期（nodes_new 用）。
    #   单独记一份是为了"恢复时不重复问同一条"——
    #   interrupt 挂起后恢复，节点从头重跑，靠它能认出当前这条。
    verify_assert: NotRequired[str]
    # ★ 为了看清而点开了东西时，AI 说的"看完怎么关回去"（按钮文字）。
    #   跨轮次留着，重判时还得知道。空 = 不用关。
    _close_with: NotRequired[str]
    # ★ 补证据点开之后，看完要点的那个"关闭/取消"按钮的文字。
    #
    # 用户的原话：「因为背景色也是个弹窗呀。你验证完要继续推倒上一步啊。
    #              不然继续不下去了。我说了要像个人类测试员一样」
    #
    # 由模型在 need_action.close_with 里给（它知道点开的是个面板、怎么关），
    # 执行器在**重判落定之后**点它一下，把界面退回原来的样子，
    # 后面的断言才不会拿到错屏。空字符串 = 不用关。
    assert_fix_close: NotRequired[str]
    # ★ 每条断言「这次是怎么看见的」——回写 JSON 用（用户要的三样之一）。
    #   a13 记成「点开课程背景色看重板→数色块→关回去」，下次直接照做。
    verify_actions: NotRequired[Annotated[list[dict[str, Any]], operator.add]]

    # ★ 目标查找的有界重试（用户要求）。
    #
    # 「目标 rid 找不到 → 立即停」这条太严：它假设目标一定有 rid，
    # 但实际很多控件没有（iv_more 就没 text/desc，菜单项连 rid 都没有，
    # 只能按文案找）。一刀切会把"UI 变了"和"控件本来就没 rid"
    # 混为一谈，整天 BLOCKED。
    #
    # 用户的原话：「rid 太严了。有些没有。我理解可以找以下。
    #              但次数要有个限制。不能无限次找。同时记录里要提示出来。」
    #
    # 所以：允许换目标找，**但限次**，且每次都要留痕。
    # 超限就停这一步（走 on_fail），不让它把整条用例拖垮。
    locate_attempts: int                 # 本步已换过几次目标
    max_locate_attempts: int             # 上限（默认 3）
    locate_tried: Annotated[list[dict[str, Any]], operator.add]  # 换目标留痕

    # 工作集：观测与动作分开存，避免互相覆盖（曾经因此静默丢动作）
    last_activity: str
    last_shot: str
    # ★★ 按用例步骤存的证据快照（step_evidence[step_id] = {...}）。
    #
    # 为什么必须有它（2026-10-21，run 68 实测）：
    #   last_shot / obs_nodes / obs_clickable 这些**全是单值**，每次 observe 都
    #   整份覆盖。而断言是在**整个流程末尾统一判**的，于是 8 条断言拿到的
    #   都是"最后那一刻"的同一张图 —— 实测 8 条提示词里的图片 base64
    #   **md5 完全一致**（f292dc2c），而它们要验的根本不是同一屏：
    #     · a6 验「新建课程表」的完成按钮
    #     · a11/a12/a13 验「编辑课程」弹窗里的时间/周数/配色
    #     · a14 验「弹框已关闭」
    #   拿最后一屏去判 a6，模型只能答"看不到完成按钮" —— 看起来像模型不行，
    #   其实是喂给它的东西不对。
    #
    # 结构（每步**追加一条**记录，同一 step_id 可能有多条，取最后一条）：
    #   [{"step": "s3", "step_index": 3, "activity", "package",
    #     "shot", "dump", "nodes", "nodes_text", "clickable"}]
    #
    # ⚠️⚠️ 必须是 `Annotated[list, operator.add]`（累加），不能是普通 dict！
    #
    #   第一版写成 `dict[str, Any]`、节点返回 `{sid: {...}}`，看起来像"按
    #   步骤存"，实际 LangGraph 对未声明累加器的字段是**整体替换** ——
    #   于是每一轮都把上一步的记录冲掉，最后 state 里只剩 s5 一条。
    #   实测表现：a14（绑 s5）拿到的是"步骤 s5 的快照"，其余 7 条全部退回
    #   "整轮最后一屏" —— 图、绑定、提示词全对，只有存储方式错了。
    #   上面 seen_rids / seen_texts 用 operator.add 也是同一个原因。
    step_evidence: Annotated[list[dict[str, Any]], operator.add]
    # 视觉通道连续失败次数。达到 VISION_MAX_FAILURES 后禁止再问（见 vision_node）。
    # 没有这个计数，模型会一直以为"还没看到"而无脑重问 —— 167 空转 16 轮。
    vision_failures: int
    vision_error: str
    # 最近一次看图的答案。vision 节点写，decide 节点读并塞进提示词。
    # 没有它，模型"问了图但永远看不到答案"—— 167 连问 21 次的根本原因。
    last_vision: str
    # 节点执行统计 {节点名: {"n": 次数, "ms": 累计毫秒}}。
    # 由 runner._drive 从 stream 事件里数出来，是"谁真的执行了、花了多久"的
    # 唯一真实来源。注意别拿它和 counts 混用 —— 那个只统计会 interrupt 的节点。
    node_calls: dict[str, Any]
    last_event_ids: list[str]
    obs_clickable: list[dict[str, Any]]
    # 当前屏的**全部**文案（不只可点项）。
    # 用途：判据里 `text=xxx` 有时指向的是**不可点的文字**
    # （标题、说明、提示语）——只看 clickable 会判"没有"，
    # 但屏幕上明明写着。match_landing / target_present 的 text 分支用它兜底。
    obs_nodes_text: str
    # 当前屏的**语义节点列表**（工具层 summarize_xml 的产物）。
    # 比 obs_nodes_text 结构更好：带 rid / 文字 / 坐标 / 选中态 / 可滑动。
    # 用途：模型判断"这页到底有什么"时，只给可点项会漏掉不可点的
    # 标题、空态文案、提示语 —— 而那正是很多判据要看的。
    obs_nodes: list[dict[str, Any]]
    # ★ 当前屏 XML 的**落盘路径**（不是内容）。
    # 为什么给路径不给内容：一份日历首页 dump 约 34000 字符 ≈ 1.7 万 token，
    # 每轮都带、几十轮就是百万 token，而 95% 是布局容器，模型根本用不上。
    # 判据要查的时候（xpath 命中数、节点是否存在）从磁盘读，用完就丢。
    obs_xml_path: str
    obs_fresh: bool                      # 画面是否来自 act 返回（免 observe）
    # 整轮执行中**见过**的控件 rid（累加，不随界面变化清空）。
    # 用途：断言的判据可能是 node_present=rid=X，指"这个过程里出现过"，
    # 而断言是在流程走完之后才逐条判的 —— 那时 X 早就翻页翻走了。
    # 不给这份历史，模型只能看当前界面，会把本来成立的判据误判成 FAIL
    # （168 的 a2 就是这样误判的：btnImportFromGallery 出现过，判的时候没了）。
    seen_rids: Annotated[list[str], operator.add]
    # 授权与弹窗留痕（累加）。报告据此回答"这条用例授权了什么"。
    permissions_granted: Annotated[list[dict[str, Any]], operator.add]
    dialogs_dismissed: Annotated[list[dict[str, Any]], operator.add]
    # **Android 运行时权限**的自动授予留痕（工具层点的，不是模型点的）。
    # 与 dialogs_dismissed 是两回事：那个是 App 流程里的弹窗，
    # 这个是系统权限框，会真正改变设备权限状态。
    runtime_permissions: Annotated[list[dict[str, Any]], operator.add]
    runtime_permission_prompts: Annotated[list[dict[str, Any]], operator.add]
    # ★ 被测 App 当前**被授予了哪些运行时权限**（快照，覆盖语义）。
    #
    # 用户要的是「同意了才能打开相机」——判的是**授权结果**，不是"弹过框"。
    # 每次处置完权限弹窗就重查一次，断言用这个判，不猜文案。
    granted_permissions: dict[str, bool]
    # ★ 内容型权限（相机/照片/麦克风/定位）没确认授予时的阻断事实。
    #   和普通权限分开：这类不授予流程必然走不下去，不是"功能受限"那么轻。
    perm_blocked: dict[str, Any]
    # 工具层遇到"权限框在、但没有能安全点的按钮"时置位，让 decide 重决策。
    # 用覆盖语义（不带 reducer）：只需反映**最近一次**的处置状态。
    perm_unmatched: dict[str, Any]

    # 动作后的前台包名（区分系统权限框 / App 自己的界面）
    last_package: str

    action: dict[str, Any] | None        # 待执行/刚执行的动作
    crash: dict[str, Any] | None         # 崩溃信息（若命中）
    pc_done: list[str]                   # 已执行的前提
    pc_skipped: list[str]                # 因 skip_if 已满足而跳过的前提
    # ★ 本轮 prep 里用"记住的固定前置"（setup 记忆）直接点掉的控件。
    #
    # 用户：「这种固定的步骤那就转成setup就行了」——
    # 记住并直接执行的那几下要留痕，否则报告里看不出"这里没问 AI 是谁做的"。
    setup_applied: list[str]
    # ★ 来自 type: goto 的"要到达哪、怎么去"。
    #   不执行命令，交给 decide 的 fix_start 模式让模型走过去。
    nav_wants: list[dict[str, Any]]

    # ★ 来自 prepare 前提的"到了起点之后还要准备什么"。
    #
    # 为什么要单独传：prepare 的动作（点 btnCreateManually → 输入 → 点保存）
    # **依赖起点界面**，而 prep 阶段 App 往往还没走到那儿，做不了。
    # 实测 182：prep 里报了"前提未执行…完整动作：[...]"，可那句话只进了 issues
    # —— 模型压根没看到，到了课程表页还是一直点空白格，11 轮不收敛。
    # 所以必须把它作为**可执行指令**送到 decide。
    prepare_wants: list[dict[str, Any]]

    # ★ prepare 的动作是否已经执行过（避免每次回到 start_check 都重做一遍）
    prep_applied: bool

    # ★ 见过的控件**文案**（不只是 rid）。
    #
    # 为什么需要：断言是流程**末尾**统一判的，那时早就翻页了。
    # seen_rids 只攒了 rid，所以 `node_text{rid, equals}` 这种判据
    # 一判就"不成立" —— 实测 168 的 a1 就是这样被误判成 FAIL 的：
    # toolbar_title={"t": "课程表"} 在第 5 轮明明在屏上，
    # 判的时候人在 PhotoPicker，当前屏里当然没有它。
    #
    # 和 seen_rids 一样用累加 reducer。
    seen_texts: Annotated[list[dict[str, str]], operator.add]

    # ★ 每屏的**整屏文案**（去重后的一整段），一路累加。
    #
    # seen_texts 只收"带 rid 的控件"，可 `text_present` 判的常是
    # **纯文本标签** —— 比如 177 a4 要判「周末是否有课」，
    # 那是页面上一个没有 rid 的标题行，怎么都进不了 seen_texts。
    # 于是断言在末尾判时屏幕早翻走了，`want in screen` 必然不成立。
    #
    # 这跟 seen_rids / seen_texts 是**同一个道理**：
    # 判据要判"过程中出现过"，就必须把"过程中"存下来，
    # 而不是只留最后一屏。
    seen_screens: Annotated[list[str], operator.add]

    # ★ 进度轨迹：每条 {t, phase, step, note}，随报告回传主 Agent。
    # 用累加 reducer —— 一次 invoke 里多个节点都要往里追加。
    progress: Annotated[list[dict[str, Any]], operator.add]

    # ── 强证据直点 ────────────────────────────────────────────
    # 代码先看这一步的 target 在当前屏上能否**唯一确定**：
    #   xpath 命中 1 个 / rid 命中 1 个 → 直接点，不问模型
    #   命中 0 个或多于 1 个          → 交模型
    # 这不是回放 —— 不认"上次点了哪"，只认"现在能不能唯一确定"。
    fallback_reason: str                  # 非空=本步已落回模型（附原因）
    fallback_screen: str                  # 落回模型时那一屏的指纹（换屏即失效）
    # ★ 交给模型时的"处境说明"（测试员语言）：这步要干什么、现在在哪、
    #   上次踩过什么坑、能用哪些基本手段。见 _ask_model 的注释。
    fallback_guidance: str
    retry_landing: int                    # 本步落点不符已重试几次（上限见 LANDING_MAX_TRIES）
    last_step_id: str                     # 上一次 plan 进入的是哪一步（判断是否换步）
    # 统计：代码走了几步 vs 模型走了几步（跑完直接看出自动化率）
    strong_hits: Annotated[list[dict[str, Any]], operator.add]

    start_mismatch: str | None           # 起点不符说明（有值则先纠正起点）
    start_fix_rounds: int                # 纠正起点的尝试轮次（防死循环）
    knowledge_asked: list[str]           # 已检索过的关键词（防同一词反复查）
    # ★ 这一轮**真正查到的知识**（用户要求写回 JSON 的三样之一）：
    #   「或者知识卡查询到的知识」
    # 早先查到就塞进 action["knowledge"] 用一次，下一轮就被覆盖 ——
    # 跑完想不起来当时查到了什么，回写时无从下手。这里单独攒一份。
    knowledge_used: Annotated[list[dict[str, Any]], operator.add]

    # 累积结论（短记录，operator.add）
    findings: Annotated[list[dict[str, Any]], operator.add]
    issues: Annotated[list[str], operator.add]
    visited: Annotated[list[str], operator.add]

    # ★ 每步「实际点/按了什么」的结构化轨迹 —— 供回写 YAML 用。
    #
    # 每条：{"step": "s3", "action": {"rid": "x"} | {"key": "back"} | {}}
    # 只记**能表达成 YAML** 的形态（rid/text/key）；纯坐标点空白处记成 {}，
    # 因为坐标写进用例换设备必失效，所以坐标不作为「怎么点」的经验被采纳。
    action_trace: Annotated[list[dict[str, Any]], operator.add]

    # 控制
    pending: dict[str, Any] | None       # 预期收集阶段的答复
    verdict: dict[str, Any] | None       # 当前预期的判定
    status: str
    summary: str
    session_id: int
    # ★ 这一轮回写出来的用例（在 finish 之前由 learn 节点产出）。
    #   用户：「要回写 JSON。高质量的 JSON 对重新跑有意义」
    learned: NotRequired[dict[str, Any]]
    learned_path: NotRequired[str]

    # ★ 非空 = 整条用例立即终止（不再跑后续步骤）。
    #
    # 为什么需要（会话 #84 实测）：判"卡死"后原本只是"记问题 + 进下一步"，
    # 结果 169 的 s3 在系统相机里卡死后，s4 立刻接着跑、又在同一屏卡死，
    # 白烧 4 分钟。卡死意味着**页面根本没变**，后面每一步只会重演同一循环。
    abort: str


# --------------------------------------------------------------------------
# 知识检索：子 Agent 自主决定查什么
# --------------------------------------------------------------------------

def search_knowledge(skill_root: str, files: list[str], keyword: str,
                     limit: int = 40) -> str:
    """在指定文档里检索关键词，返回命中行（带出处）。

    子 Agent 用它自主查知识卡 —— 不是把整份文档塞进上下文。
    只返回命中行的上下文片段，控制上下文体积。
    """
    hits: list[str] = []
    root = Path(skill_root)
    for rel in files:
        path = root / rel
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if keyword and keyword.lower() in line.lower():
                lo, hi = max(0, i - 1), min(len(lines), i + 2)
                snippet = " / ".join(l.strip() for l in lines[lo:hi] if l.strip())
                hits.append(f"[{rel}:{i+1}] {snippet[:200]}")
                if len(hits) >= limit:
                    return "\n".join(hits)
    return "\n".join(hits) if hits else f"（{keyword} 无命中）"


def read_knowledge_file(skill_root: str, rel: str,
                        max_bytes: int = 4000) -> str:
    """读一份知识卡全文（有截断）。

    与 search_knowledge 的区别：检索是"我问什么你给什么"，
    这里是"用例声明了要读，就整份给它"。
    SKILL 要求"先读卡：路径图告诉你「在哪找」，知识卡告诉你「有什么坑」"——
    这类内容模型不一定会主动索取，所以由代码在第一轮直接注入。
    """
    path = Path(skill_root) / rel
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > max_bytes:
        text = text[:max_bytes] + f"\n…（已截断，原文 {len(text)} 字符）"
    return text


def pick_keywords(step: StepSpec) -> list[str]:
    """从步骤里提候选检索词 —— 给子 Agent 一个起点，它仍可自己换词。"""
    words: list[str] = []
    for src in (step.desc, step.locate or "", step.landing or ""):
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fa5]{2,}", src):
            if tok not in words:
                words.append(tok)
    return words[:6]


class ChildContext:
    """子 Agent 的运行时依赖。

    刻意用显式对象而不是往 state 里塞 _dev/_python 这类下划线键 ——
    state 是要被 checkpointer 序列化的，塞不可序列化的客户端会出问题，
    而且下划线注入让节点函数无法单独测试（PoC 第一版就因此出过 bug）。
    """

    def __init__(self, device: RealDevice, skill_root: str, python: str,
                 report_path: str) -> None:
        self.device = device
        self.skill_root = skill_root
        self.python = python
        self.report_path = report_path


_CTX: ChildContext | None = None


def set_context(ctx: ChildContext) -> None:
    global _CTX
    _CTX = ctx


def _ctx() -> ChildContext:
    if _CTX is None:
        raise RuntimeError("子 Agent 上下文未注入：先 set_context(...)")
    return _CTX


# --------------------------------------------------------------------------
# 节点
# --------------------------------------------------------------------------

def run_restore(state: ChildState, report_issues: list[str]) -> list[str]:
    """执行 env 类前提的恢复命令。

    ⚠️ 无论用例成败都必须跑 —— 这是"不干扰下一个 case"的硬要求。
    恢复失败要显式记进 issues（不能静默），因为现场已被污染。
    """
    dev = _ctx().device
    task = state.get("task") or {}
    done: list[str] = []
    for pc in (task.get("preconditions") or []):
        if pc.get("kind") != "env":
            continue
        for cmd in (pc.get("undo") or []):
            try:
                dev.shell(cmd)
                done.append(cmd)
            except Exception as exc:  # noqa: BLE001
                report_issues.append(
                    f"⚠️ 恢复失败，现场可能已被污染：{cmd} -> {exc}")
    return done


def _on_related_page(pc: dict[str, Any], activity: str,
                     obs: dict[str, Any] | None = None) -> bool:
    """当前界面算不算"跟这条前提相关的页面"？

    ★ 这里**直接复用工具层的 `checkpoint.evaluate`**，不再自己认页面。

    原先这个函数是自己攒了一堆启发式（可点元素个数、弹窗特征词、
    系统包名单……），用来判断"屏上是不是盖着个弹窗"。
    那套东西和 `checkpoint.py` 干的是同一件事，而且做得更差 ——
    我为此连挂了四轮（见 docs/开发记录-踩坑.md 第 1 条）。

    工具层的分工是清楚的：
      · `checkpoint.evaluate` 用**路径图节点的识别键**（rid/desc/text）
        判断"当前屏是不是这个节点" —— 这是确定性的，代码说了算；
      · `checkpoint.detect_transient` 用**白名单**认临时弹窗，
        白名单外一律返回 None —— 交回 AI，绝不猜。

    所以这里只做一件事：把前提声明的落点（`assert_start`）交给
    `checkpoint.evaluate` 去判。没声明落点 = 判不了 = 返回 False，
    调用方据此"保守照跑"（宁可多准备一次，也不能该做准备却跳过）。
    """
    want = str(pc.get("assert_start") or "").strip()
    if not want:
        return False
    obs = obs or {}
    try:
        cp = _checkpoint()
    except Exception:  # noqa: BLE001
        return False
    ck = cp.evaluate(
        cp.parse_expect(want),
        activity=activity,
        sig=obs.get("sig") or [],
        package=obs.get("package"))
    return bool(ck.get("ok"))


def _skip_if_met(skip_if: Any, *, activity: str, package: str = "",
                 obs: dict[str, Any] | None = None,
                 trusted_page: bool = True) -> bool | None:
    """前提的 `skip_if` 成立吗？成立就**跳过**这条前提。

    用例 YAML 的写法：
        skip_if:
          activity: .AllInOneActivity        # 已经在这个界面了
          timetable_state: empty             # 数据已经是空的

    返回 True=跳过 / False=不跳过 / None=判不了（**不跳过**，保守）

    ⚠️ 为什么要有：168 的两条前提都带 skip_if，其中一条是
    `{timetable_state: empty}` —— 意思是"课表已经是空的就别再 pm clear"。
    没有它就是**每次跑都清数据 + 走一遍授权流程**（慢且脆）。
    但反过来，判不了的时候必须**照跑**（宁可多清一次，也不能该清不清）。
    """
    if not skip_if:
        return False
    if isinstance(skip_if, str):
        return None
    if not isinstance(skip_if, dict):
        return None

    # ★ 页面特征类判据（has_node / not_on_page / …）问的是
    #   "屏上有没有某样东西" —— 这个问题**只有站在对的页面上问才成立**。
    #   站错页面时答案没有意义，必须返回"判不了"而不是"不满足"。
    #
    #   实测（182）：「屏上没有『还未添加课程表』= 课表已建好」这条，
    #   在冷启动的权限说明框上求值时必然成立 -> 整条 prepare 被跳过
    #   -> 课表从没建过 -> 步骤要的 cv_empty_content 永不出现 -> 空转 80 轮。
    _PAGE_FEATURE_KEYS = ("has_node", "not_on_page", "not_present",
                          "no_dialog", "dialog_absent")
    if not trusted_page and any(k in _PAGE_FEATURE_KEYS for k in skip_if):
        return None

    verdicts: list[bool] = []
    for k, want in skip_if.items():
        if k == "activity":
            w = str(want).strip()
            verdicts.append(bool(activity) and (w in activity))
        elif k in ("package", "pkg"):
            w = str(want).strip()
            verdicts.append(bool(package) and (w in package))
        elif k in ("timetable_state", "data_state", "state"):
            # 数据状态要查库/查设备，**我们判不了** —— 保守照跑
            return None
        elif k in ("no_dialog", "dialog_absent"):
            obs = obs or {}
            # ⚠️ 用 _screen_text 而不是直接读 nodes_text：
            #    工具层不一定给 nodes_text，但可点项里带着文字 ——
            #    只读 nodes_text 会把"屏上明明写着"判成"不在"。
            txt = _screen_text(obs)
            verdicts.append(want and (str(want) not in txt) or False)
        elif k == "has_node":
            # 「屏上有没有这个 rid」—— 用例 YAML 用它表达"状态已经对了"，
            # 例：{has_node: cv_empty_content} = 周视图已经有空白格了，不用再准备。
            #
            # ★ 这个判得了，而且**必须判**：判不了就"保守照跑"，
            #   结果每次都在已有课表上再建一个（182 实测会越堆越多）。
            obs = obs or {}
            want_rid = str(want).strip().split("/")[-1]
            have = {str(c.get("rid") or "").split("/")[-1]
                    for c in (obs.get("next") or [])}
            have |= {str(c.get("r") or "") for c in (obs.get("next") or [])}
            verdicts.append(bool(want_rid) and want_rid in have)
        elif k in ("not_on_page", "not_present"):
            # 「某个文案不该在屏上」/「状态还没达成」—— 语义和 has_node 反着来。
            # 用例 YAML 的写法是 {not_on_page: '还未添加课程表'}：
            # 意思是"如果屏上已经**没有**『还未添加课程表』这句空态文案，
            # 说明课表已经建好了，这条前提可以跳过"。
            obs = obs or {}
            txt = _screen_text(obs)
            verdicts.append(bool(want) and (str(want) not in txt))
        else:
            return None
    if not verdicts:
        return None
    return all(verdicts)


def _pathmap_expect(package: str, rid: str) -> str:
    """从**路径图**里查"点了 `rid` 之后应该落到哪"，作为 `--expect`。

    ★ 这是"不重复造轮子"的核心：落点判据不该由我猜，
      路径图（`knowledge/paths/<pkg>.md`）里每条边都写着
      「认什么锚点 → 去哪个节点」，节点又带识别键。
      工具层的 `pathmap.finish_expect(node)` 直接把它拼成
      `activity=Xxx,rid=Yyy` 这种 --expect 字符串。

    查不到就返回空串 —— 调用方据此**不带 expect**：第一轮探路本来就没有落点，
    先跑一次看 activity，跑完补进路径图，下一轮就能提速。
    """
    if not package or not rid:
        return ""
    try:
        import importlib

        # ⚠️ 必须先 `_checkpoint()`：它负责把 tools 目录挂进 sys.path，
        #    否则 `pathmap` 根本 import 不到（而且它是 `import checkpoint`
        #    的，单独加载也会缺依赖）。
        _checkpoint()
        pm = importlib.import_module("pathmap")
    except Exception:  # noqa: BLE001
        return ""

    try:
        from pathlib import Path as _P

        pt = None
        for root in _skill_roots():
            cand = _P(root) / "knowledge" / "paths" / f"{package}.md"
            if cand.is_file():
                pt = cand
                break
        if pt is None:
            return ""
        nodes = pm.load(pt)
    except Exception:  # noqa: BLE001
        return ""
    if not nodes:
        return ""

    # 遍历所有节点的出边，找锚点 rid 等于目标的那条，返回它的去向节点判据
    for cur in nodes:
        for e in (cur.get("edges") or []):
            if str(e.get("rid") or "").split("/")[-1] != rid:
                continue
            tgt = pm.find_node(nodes, e.get("to") or "")
            exp = pm.finish_expect(tgt)
            if exp:
                return exp
    return ""


def _parse_prepare_actions(text: str,
                           package: str = "") -> list[dict[str, Any]]:
    """从 prepare 前提的中文描述里解析出可执行的动作序列。

    用例 YAML 的 prepare.do 是**人写给人的操作说明**，但写得足够具体：

        ① 点 `btnCreateManually`（c=[1520,1717]）进 `EditTimetableActivity`；
        ② 点 `et_schedule_name`（c=[1520,375]）聚焦，再用
           `act input --x 1520 --y 375 --text="<名称>"` **一次调用内**写入
        ③ 点 `save_view`（c=[2913,202]）保存 → 自动回到周视图
        ④ **不要动「周末有课」开关**，保持默认关闭

    这种描述**能**转成动作：里面明确写了 rid 和"输入"这个意图。
    转不了的部分（④那种"不要做什么"）自然会被忽略 —— 因为本函数只认
    "点 X" 和 "输入" 两种模式。

    返回形如：
        [{"op": "tap", "rid": "btnCreateManually"},
         {"op": "input", "rid": "et_schedule_name", "text": "<名称>"},
         {"op": "tap", "rid": "save_view"}]

    ⚠️ 设计边界：**只认 rid**，不认坐标也不认文案。
    坐标换设备必失效（YAML 里那些 c=[1520,1717] 只对本机成立）；
    文案输入框更不可靠。rid 是唯一稳定可执行的定位。
    """
    if not text:
        return []
    acts: list[dict[str, Any]] = []

    # 「点 `rid`」/「点 rid」—— 反引号可有可无。
    # ⚠️ 要排除 "点 `act input ...`" 这种伪 rid：act 是命令行不是控件。
    for m in re.finditer(r"点\s*`?([A-Za-z_][\w]*)`?", text):
        rid = m.group(1)
        if rid.lower() in ("act", "click", "tap", "input", "adb", "pm", "am"):
            continue
        if acts and acts[-1].get("rid") == rid and acts[-1].get("op") == "tap":
            continue
        acts.append({"op": "tap", "rid": rid})

    # input：`act input ... --text="<名称>"` -> 挂到最近的、看起来像输入框的 rid 上
    #
    # ⚠️ 必须**插在聚焦那一步之后**，不能 append 到末尾。
    #    踩过：原文是「点 et_schedule_name 聚焦，再 input，③ 点 save_view 保存」，
    #    一 append 就变成「点→点保存→输入」—— 名字还没输就保存了，等于没建名字。
    if re.search(r"act\s+input|--text", text):
        idx = next((i for i in range(len(acts) - 1, -1, -1)
                    if "name" in acts[i]["rid"].lower()
                    or "et_" in acts[i]["rid"].lower()), None)
        tm = re.search(r'--text\s*=\s*"([^"]*)"', text)
        if idx is not None and tm:
            # ⚠️ `<名称>` 是**占位符**，不是要输入的字面值。
            #    真按字面输入会把 "<名称>" 写进输入框（还带尖括号），
            #    建出来的课表名字是错误的，后续断言对不上。
            raw_txt = tm.group(1).strip()
            if not raw_txt or re.fullmatch(r"[<＜][^>＞]*[>＞]", raw_txt):
                raw_txt = "TestTimetable"
            # ⚠️ adb `input text` **不支持非 ASCII**。
            #    实测（182）：传 "测试课表" 直接
            #        java.lang.NullPointerException: Attempt to get length of null array
            #    报在 InputShellCommand.sendText 里 —— 名字没输进去，
            #    后面点"保存"被当成空名拒绝，又退回空态页，整条用例白跑。
            #    所以非 ASCII 一律换成 ASCII 占位名。
            #    （要认真输中文得走 IME 粘贴，那是另一个工程量，先不引入。）
            if any(ord(ch) > 127 for ch in raw_txt):
                raw_txt = "TestTimetable"
            acts.insert(idx + 1,
                        {"op": "input", "rid": acts[idx]["rid"],
                         "text": raw_txt})

    # ★ 给每个动作补上 `expect` —— 从路径图查，查不到就留空。
    #   留空不是漏了：第一次走的新路本来就写不出 expect，
    #   先跑一步看 activity，再把它补进路径图。所以这里不猜。
    if package:
        for a in acts:
            if not a.get("expect"):
                a["expect"] = _pathmap_expect(package, a.get("rid") or "")

    return acts


def _run_prepare_actions(dev, acts: list[dict[str, Any]],
                         pkg_hint: str = "") -> tuple[int, int, list[str]]:
    """真的把 prepare 解析出的动作做掉：observe 找坐标 -> act。

    ⚠️ 为什么每次都要重新 observe：这一步的每个动作都会改变界面
    （点「手动创建课程表」会跳到新 Activity），用上一步的坐标会点空。
    这正是 YAML 里反复强调"不要拆成两次调用"的同一个道理。
    """
    ok_n = fail_n = 0
    issues: list[str] = []
    for a in acts:
        rid = a.get("rid") or ""
        op = a.get("op")
        try:
            out = dev.observe_raw() or {}
            clickable = out.get("next") or []
            # ★ 三种定位方式，优先用最精确的：
            #   1. target（组合 xpath，如 //*[@resource-id='..'][@text='..']）
            #      —— setup 记忆用的就是这个，能区分同 rid 的不同按钮
            #   2. rid（末段匹配）
            #   3. text
            hit = None
            _tgt = str(a.get("target") or "").strip()
            if _tgt:
                # _resolve_strong_target 要求"唯一命中" —— 命中 0 个或多个
                # 都返回 None，正好是我们要的"拿不准就别点"。
                _x, _y, _how = _resolve_strong_target(_tgt, {
                    "obs_clickable": clickable,
                    # observe_raw() 把落盘路径放在 evidence.xml 里，不是顶层
                    "obs_xml_path": str(
                        (out.get("evidence") or {}).get("xml") or ""),
                })
                if _x is not None and _y is not None:
                    hit = {"r": rid or _tgt, "c": [_x, _y]}
                else:
                    fail_n += 1
                    issues.append(
                        f"前提动作跳过了：当前屏（{out.get('activity') or ''}）"
                        f"定位不到 {_tgt}（{_how}）")
                    continue
            if hit is None:
                hit = next((c for c in clickable
                            if str(c.get("rid") or "").split("/")[-1] == rid
                            or str(c.get("r") or "") == rid), None)
            if not hit:
                fail_n += 1
                act_now = out.get("activity") or ""
                issues.append(
                    f"前提动作跳过了：当前屏（{act_now}）找不到 {rid}"
                    f"（{'输入' if op == 'input' else '点击'}）")
                continue
            c = hit.get("center") or hit.get("c") or []
            if len(c) < 2:
                fail_n += 1
                issues.append(f"前提动作 {rid} 没有坐标，跳过")
                continue
            x, y = int(c[0]), int(c[1])
            # ★ 每个动作都带 `expect`，落点交给工具层的检查点判 ——
            #   不再靠 res.ok。`ok=True` 只说明"点击动作执行了"，
            #   不说明"界面到了该去的地方"：实测 182 点完
            #   btnCreateManually，ok=True 但 activity 还停在原页，
            #   于是下一个动作按旧坐标去点，全部落空。
            #   检查点会当场告诉我们没落到哪，并且给出实际是什么。
            exp = a.get("expect") or ""
            print(f"  [prep_apply] ① {rid} -> ({x},{y})  expect={exp or '-'}",
                  file=sys.stderr)
            res = dev.act(x, y, why=f"前提准备：{op} {rid}",
                          expect=exp or None)
            if not getattr(res, "ok", True):
                fail_n += 1
                issues.append(
                    f"前提动作 {rid}（{x},{y}）未生效："
                    f"{getattr(res, 'error', '') or '未知原因'}")
                continue
            # 检查点未命中 = 落点不对。这不是"没点击"，是"点了没到" ——
            # 点击落在空处时 activity 不变、也不报任何错，只能靠检查点发现。
            cp = getattr(res, "checkpoint", None) or {}
            if cp.get("checked") and not cp.get("ok"):
                fail_n += 1
                issues.append(
                    f"前提动作 {rid} 落点未命中：{'；'.join(cp.get('missed') or [])}"
                    f"（实际 activity={getattr(res, 'activity', '')}）")
                continue
            ok_n += 1
            # ⚠️ 等界面稳定再读：点完立刻 observe 会读到旧界面，
            #    下一个动作就会按旧坐标去点（YAML 里说的"落点变化跑偏"）。
            time.sleep(2.0)
            if op == "input" and a.get("text"):
                try:
                    dev.shell(f'input text "{a["text"]}"')
                except Exception as exc:  # noqa: BLE001
                    # ★ 输入失败不能只是"记一笔然后继续"。
                    #   实测 182：input text 报 NPE（中文不支持），
                    #   后面照样点"保存" -> 空名字被拒 -> 退回空态页，
                    #   报告却写着"4 步完成"，问题被彻底掩盖。
                    #   现在：输入失败就中止这条前提，让调用方知道没建成。
                    fail_n += 1
                    issues.append(
                        f"前提动作 {rid} 输入失败：{str(exc)[:160]}"
                        f"（名字没进去，后续保存会白做，已中止）")
                    return ok_n, fail_n, issues
                time.sleep(1.0)
        except Exception as exc:  # noqa: BLE001
            fail_n += 1
            issues.append(f"前提动作 {rid} 失败：{exc}")
    return ok_n, fail_n, issues


def _looks_like_prose(cmd: str) -> bool:
    """这段 `do` 是人话说明，而不是 shell 命令？

    用例 YAML 里 `prepare` 类前提的 `do` 经常写成操作描述，例如：
        do: ① 点 `btnCreateManually`（c=[1520,1717]）进 `EditTimetableActivity`；② …
    它不是命令。扔给 shell 只会得到 "shell 失败(127)"（实测 case 182），
    而且那条报错完全看不出"这是说明文字"，容易被当成设备问题排查半天。

    判据（保守，宁可当命令也别把真命令误判成说明）：
      · 含中文 -> 基本不可能是 adb/pm/am 命令
      · 且不以已知命令开头（pm/am/adb/settings/svc/cmd/input/echo/set）
    """
    s = (cmd or "").strip()
    if not s:
        return False
    has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in s)
    if not has_cjk:
        return False
    head = s.split()[0].lower() if s.split() else ""
    known = ("pm", "am", "adb", "settings", "svc", "cmd", "input",
             "echo", "set", "rm", "mkdir", "cp", "mv")
    return head not in known


def _base_prep_node(state: ChildState) -> dict[str, Any]:
    """前提准备：跑 data 类清理 + 全局起点断言。

    ⚠️ 起点不对**不 BLOCKED**：像人类测试员那样，自己在知识卡里查
    「怎么回到起点」，走回去再开始。只有实在回不去才记问题。

    env 类的 do（关 WiFi 等）也在这里做，但**恢复在 finish 里**，
    因为恢复必须"无论成败都执行"。
    """
    dev = _ctx().device
    task = state.get("task") or {}
    issues: list[str] = []
    done: list[str] = []
    skipped: list[str] = []
    # ★ nav（type: goto）的目标：不执行 shell，攒起来交给 start_check/decide
    nav_wants: list[dict[str, Any]] = []
    # ★ prepare 前提里"现在做不了、要等界面到位再做"的动作（见 ChildState 说明）
    prepare_wants: list[dict[str, Any]] = []

    # 先看一眼现状 —— skip_if 要据此判断（用例 YAML：已经是目标态就别再动）
    cur_act, cur_pkg, obs0 = "", "", {}
    need_probe = any((pc.get("skip_if") for pc in (task.get("preconditions") or [])))
    if need_probe:
        try:
            obs0 = dev.observe_raw() or {}
            cur_act = str(obs0.get("activity") or "")
            cur_pkg = str(obs0.get("package") or "")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"读起点状态失败（skip_if 无法判断，将照常执行）：{exc}")

    for pc in (task.get("preconditions") or []):
        # ★ nav（goto）：**不执行命令**，记下"要到达哪、怎么去"。
        #
        # 已在目标状态 → 直接从步骤1开始；不在 → 走最短路径过去。
        # 这个判断交给模型（decide 的 fix_start 模式），所以这里只收集。
        if pc.get("kind") == "nav":
            nav_wants.append({
                "desc": pc.get("desc") or "",
                "target": pc.get("target") or "",
                "hint": pc.get("hint") or "",
                "assert_start": pc.get("assert_start"),
            })
            continue

        # ★ 这条 prepare 能不能现在就做？看它第一个动作要点的控件在不在屏上。
        #
        # 为什么要有这个判断（实测 182，2026-10）：
        #   prepare 说「点 btnCreateManually 进 EditTimetableActivity」，
        #   但它依赖的界面是**课程表页**，而 prep 跑的时候 App 还在日历首页。
        #   于是 4 个动作全部"找不到控件"，报告写成"成功 0 失败 4" ——
        #   看起来像功能坏了，实际只是**顺序问题**：得先导航过去。
        #   现在改成：控件不在就不点，明确告诉模型"先走到位再准备"。
        _acts = None
        _nav_ready = True
        if pc.get("do"):
            for _c in (pc.get("do") or []):
                if _looks_like_prose(_c):
                    _acts = _parse_prepare_actions(_c)
                    if _acts:
                        _first_rid = str(_acts[0].get("rid") or "")
                        _have = {str(n.get("rid") or "").split("/")[-1]
                                 for n in (obs0.get("next") or [])}
                        _have |= {str(n.get("r") or "")
                                  for n in (obs0.get("next") or [])}
                        _nav_ready = (not _first_rid) or (_first_rid in _have)
                    break

        # ★ skip_if：条件已满足就跳过这条前提，不白跑一遍
        #
        # ⚠️⚠️ 但**只能在"跟这条前提相关的界面"上求值**，否则会把
        #      "因为不在那个页面，所以没看到那句文案" 误判成 "条件已满足"。
        #
        # 实测（2026-10，case 182，代价是整条用例 BLOCKED）：
        #   前提 `prepare 存在一个课程表` 的 skip_if 是
        #       {not_on_page: '还未添加课程表'}     # 屏上没这句 = 课表已建好
        #   可开跑那一刻屏上停在**权限说明框**（冷启动必经），
        #   自然没有"还未添加课程表"这句 —— 于是判成"已满足 → 跳过"，
        #   **建课表这一步永远不执行**，s1 要的 cv_empty_content 永不出现，
        #   模型在日历首页空转了 80 轮。
        #
        # 判据本身没错，错在"在错误的页面上问它"。
        # 所以：skip_if 引用了**页面特征**（has_node / not_on_page / no_dialog）
        # 时，必须确认当前就在相关页面；确认不了就**照跑**（宁可多准备一次）。
        sk = _skip_if_met(pc.get("skip_if"), activity=cur_act,
                          package=cur_pkg, obs=obs0,
                          trusted_page=_on_related_page(pc, cur_act, obs0))
        if sk is True:
            skipped.append(f"[{pc.get('kind')}] {pc.get('desc') or ''}"
                           f"（skip_if 已满足，跳过）")
            continue

        for cmd in (pc.get("do") or []):
            # ⚠️ 用例 YAML 的 prepare 类前提，`do` 常常是**中文说明**而不是命令。
            # 直接扔给 shell 会报 "shell 失败(127)"（实测 case 182：
            # 「① 点 btnCreateManually（c=[1520,1717]）进 EditTimetableActivity…」）。
            #
            # ★ 但"不执行"也不够 —— 实测 182 就是这样 BLOCKED 的：
            #   前提说"存在一个课程表"，没人去建，周视图一直是空态，
            #   s1 要的 cv_empty_content 永不出现，模型点了 80 轮都没用。
            #   所以这里**能把描述解析成动作的就真的去做**，
            #   解析不了的才退化成提示交给模型。
            if _looks_like_prose(cmd):
                acts = _parse_prepare_actions(cmd, package=task.get("package") or "")
                if acts and not _nav_ready:
                    # ★ 界面对不上就别硬点。
                    #   实测（182）：prepare 说"点 btnCreateManually"，但那一刻
                    #   屏上是**日历首页**，控件根本不在 —— 4 个动作全 skipped，
                    #   报告里只看到"成功 0 失败 4"，像是功能坏了。
                    #   真正该做的是：先把界面走到位（nav 的活），再准备数据。
                    #   所以这里不执行，留一条清楚的说明给模型。
                    _where = next((a for a in acts), {})
                    issues.append(
                        f"前提未执行：{pc.get('desc') or 'prepare'[:30]} 需要在"
                        f"「{task.get('start_assert') or '目标页'}」上操作，"
                        f"当前在 {cur_act or '未知页面'} —— 先走到位再准备"
                        f"（要做的动作：{_where.get('op')} {_where.get('rid')} 等）")
                    issues.append(
                        f"  完整动作：{acts}")
                    # ★ 把它变成模型的**可执行指令**（不只是 issue 里的一句话）
                    prepare_wants.append({
                        "desc": pc.get("desc") or "",
                        "assert_start": pc.get("assert_start")
                                        or task.get("start_assert") or "",
                        "actions": acts,
                    })
                elif acts:
                    ok_n, fail_n, act_issues = _run_prepare_actions(
                        dev, acts, pkg_hint=task.get("package") or "")
                    done.append(f"[prepare] {len(acts)} 步"
                                f"（成功 {ok_n}，失败 {fail_n}）：{cmd[:40]}…")
                    issues += act_issues
                else:
                    issues.append(
                        f"前提说明（非命令，未执行）：{cmd[:60]}… —— "
                        f"需要模型自己按这段描述准备状态")
                continue
            try:
                dev.shell(cmd)
                done.append(f"[{pc.get('kind')}] {cmd}")
            except Exception as exc:  # noqa: BLE001
                issues.append(f"前提执行失败：{cmd} -> {exc}")

    # data 类清理会杀掉 App，等一下再继续
    if any(c.startswith("[data]") for c in done):
        time.sleep(2)

    # ★ 冷启动兜底：前提跑完如果不在被测 App 里，就把它拉起来。
    #
    # 为什么必须有这一段：pm clear 会把 App 杀掉并退回**桌面**。
    # 如果用例作者忘了写 "am start"，流程就会在桌面上找被测页面
    # （实测：跑到 .drawer.NormalLauncher，模型去点桌面搜索框，43s 后被
    # 死循环 blocker 终止 —— 守卫是对的，但这个失败本可避免）。
    #
    # ⚠️ 只在**真的跑了前提**时才查 —— 没有前提就不要多一次 observe，
    #    否则会破坏"act 后不 observe"的节奏约定（实测：这条曾让
    #    test_pace 的 observe 次数从 1 变 2，把节奏用例跑挂）。
    #
    # ⚠️ goto 目标是**别的 App**（如 133 要进桌面全局搜索）时不要拉被测 App，
    #    否则会把刚导航好的界面又顶掉。
    _goto_off_app = any(
        "launcher" in (n.get("target") or "").lower()
        or "桌面" in (n.get("target") or "")
        for n in nav_wants)
    pkg = task.get("package") or ""
    # ★ 前提跑完后的首屏 —— 记下来交给后面的节点。
    #
    # 为什么必须记：strong_target 要判"当前前台是不是被测 App"，
    # 靠的是 state 里的 last_package。而这个字段只有 act/verify/finish
    # 会写，**prep 之后到第一次 act 之前它是空的**。
    # 实测 168：首轮 strong_target 拿到
    #     last_activity = com.zui.calendar.AllInOneActivity   （有）
    #     last_package  = ""                                  （空）
    # 于是把日历自己的界面判成"未知包名 → 不是被测 App"，
    # 白问了一次模型。信息明明就在手上：
    #     out["package"] = com.zui.calendar
    # 这里观察到的就是它，别丢掉。
    _prep_obs: dict[str, Any] = {}
    if pkg and done and not _goto_off_app and not any("monkey" in c for c in done):
        try:
            out = dev.observe_raw()
            _prep_obs = out or {}
            cur = str(out.get("package") or "")
            if cur and not cur.startswith(pkg):
                dev.shell(
                    f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
                done.append(f"[auto] 前提跑完不在 {pkg}（当前 {cur}），已拉起")
                time.sleep(3)
                # ★ 拉起后**重新观察一次**，别把旧画面丢掉或留着。
                #
                # 第一版这里写的是 `_prep_obs = {}`（作废、等后面自己观察），
                # 结果 168 复测仍然报"未知包名" —— 因为 pm clear 后前台是桌面，
                # 走的就是这条拉起分支，清空之后 last_package 一路是空。
                # 拉起后真正的前台正是被测 App，这才是该交给下面的那一屏。
                try:
                    _prep_obs = dev.observe_raw() or {}
                except Exception as exc:  # noqa: BLE001
                    _prep_obs = {}
                    issues.append(f"拉起后观察失败（交下游自己观察）：{exc}")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"自动拉起 {pkg} 失败：{exc}")

    # ══════════════════════════════════════════════════════════════════
    # ★ 执行"记住的固定前置"（setup）
    # ══════════════════════════════════════════════════════════════════
    #
    # 用户原话：
    #   「我意思这种固定的步骤那就转成setup就行了。」
    #   「清空后看着这个也会跟着点。而且这个也是固定路径。应该要记录下来。
    #     这样下次通过json文件的时候。就可以直接操作了。而不是还每一步问AI了」
    #   「多次稳定的记录下来。不稳定的可以让AI直接介入」
    #
    # 所以这里读出库里**见够次数**的那几条，直接点，不问 AI。
    # 只见过一次的临时弹窗不会出现在这里（见 store.SETUP_STABLE_TIMES）。
    #
    # ⚠️ 两个"不硬闯"的保证：
    #   1. 找不到控件就**跳过**，不盲点 —— _run_prepare_actions 里做了
    #      （`if not hit: continue`）。临时弹窗下次没了，最多白跑一次查找。
    #   2. 整段包在 try 里。记不住/读不出都不该让用例起不来 ——
    #      读不到就是"还没有记忆"，照常交给后面的 AI，不是错误。
    _setup_tried: list[str] = []
    try:
        from ..reporting import store as _store
        _ck0 = str(task.get("case_id") or "")
        _remembered = _store.get_setup_actions(_ck0) if _ck0 else []
        if _remembered:
            _acts: list[dict[str, Any]] = []
            for r in _remembered:
                one: dict[str, Any] = {"op": "tap"}
                # ★ 优先用记下来的组合判据（xpath 里带 rid 全名 + text）。
                #
                # 为什么会存这个：`android:id/button1` 是 Android 公共按钮 id，
                # 「同意」「知道了」都叫这个名。只按 rid 点，换一屏就可能
                # 点到完全不同的按钮。存下完整判据后，点的是
                # 「那个写着「知道了」的 button1」，不会误点。
                if r.get("sig"):
                    one["target"] = str(r["sig"])
                elif r.get("rid"):
                    one["rid"] = r["rid"]
                elif r.get("text"):
                    one["text"] = r["text"]
                else:
                    continue
                # 只有被测 App 自己的界面才照记忆点。别的包（外部干扰）
                # 结构不受我们控制，交给 AI 现看更稳 ——
                # 用户：「只有本 APP 的才用强证据。跳出去都是让 AI 判断吧。」
                _rp = str(r.get("pkg") or "")
                if pkg and _rp and not (_rp == pkg or _rp.startswith(pkg)):
                    issues.append(
                        f"setup 跳过 {one.get('rid') or one.get('text')}："
                        f"记的是 {_rp} 的界面，不是被测 App 的")
                    continue
                _acts.append(one)
            if _acts:
                _ok, _bad, _s_issues = _run_prepare_actions(dev, _acts,
                                                            pkg_hint=pkg)
                _setup_tried = [f"{a.get('rid') or a.get('text')}"
                                for a in _acts]
                done.append(f"[setup] 记住的固定前置 {len(_acts)} 步"
                            f"（成功 {_ok}，跳过/失败 {_bad}）")
                issues += _s_issues
    except Exception as exc:  # noqa: BLE001
        issues.append(f"setup 记忆读取失败（不影响用例，交给 AI）：{exc}")

    # ★ nav 目标交给起点检查：把它当成"起点要求"，不满足就由模型走过去
    updates: dict[str, Any] = {
        "pc_done": done, "pc_skipped": skipped, "issues": issues,
        "progress": [_tick("prep",
                           f"前提：执行 {len(done)} 条，跳过 {len(skipped)} 条"
                           + (f"｜{len(nav_wants)} 条要走过去" if nav_wants else "")
                           + (f"｜{skipped[0]}" if skipped else ""))],
    }
    # ★ 把前提下跑完看到的那一屏交出去（见 _prep_obs 处的说明）。
    #   只写"谁在前台"这两样：strong_target 判归属要用，
    #   和 act/verify 写的是同一组字段，语义一致。
    #
    # ⚠️ 刚跑过 setup 的话要**重新观察**：那几下已经把界面点变了，
    #    再交旧画面等于让 strong_target 拿前一屏的包名/activity 去判归属。
    if _setup_tried:
        try:
            _prep_obs = dev.observe_raw() or _prep_obs
        except Exception as exc:  # noqa: BLE001
            issues.append(f"setup 之后观察失败（交下游自己观察）：{exc}")
    if _prep_obs:
        updates["last_activity"] = str(_prep_obs.get("activity") or "")
        updates["last_package"] = str(_prep_obs.get("package") or "")
    if _setup_tried:
        updates["setup_applied"] = _setup_tried
    if nav_wants:
        updates["nav_wants"] = nav_wants
    if prepare_wants:
        updates["prepare_wants"] = prepare_wants
    # ★ 留一行"预备清单到底有没有攒下"的痕迹。
    #
    # 没有这行时排查过很久：prepare_wants 为空 vs prep_apply 没被触发，
    # 从外面看**一模一样**（日志里都只是"模型自己在点"）。
    # 有这行就能一眼分清：是前提没解析出来，还是路由没走到。
    #
    # ⚠️ 触发条件曾经写成 `kind == "data"`，于是**只要有用例做了 pm clear
    #    就会误报**「预备清单为空」—— 因为 pm clear 是 data 类、能用 shell
    #    直接执行，根本不需要进 prepare_wants。
    #    实测 168：pm clear 明明执行成功（preconditions_done 里有记录），
    #    报告里还是挂着一条「预备清单为空」，把人引向错误的方向。
    #
    # 只有**真该进 prepare_wants 的**才配这条提示：
    #   · 旧格式 `type: prepare`
    #   · 新格式 `kind: data` 且 do 是**自然语言描述**（要模型按描述造数据），
    #     而不是能直接执行的 shell 命令
    def _needs_prepare_wants(p: dict[str, Any]) -> bool:
        if p.get("type") == "prepare":
            return True
        if p.get("kind") != "data":
            return False
        do = p.get("do")
        cmds = [do] if isinstance(do, str) else list(do or [])
        return any(_looks_like_prose(str(cmd)) for cmd in cmds if str(cmd).strip())

    if not prepare_wants and any(_needs_prepare_wants(p)
                                 for p in (task.get("preconditions") or [])):
        issues.append("预备清单为空：本用例有 prepare 前提，但没有解析出可自动执行的动作"
                      "（可能是 do 写成了纯说明，或控件要求没写明 rid）")
    return updates




def _base_plan_node(state: ChildState) -> dict[str, Any]:
    """进入一步：准备该步的上下文（步骤 + 它的坑 + 检索到的知识）。"""
    task = state["task"]
    idx = state.get("step_index", 0)
    steps = task["steps"]
    if idx >= len(steps):
        # 步骤走完 -> 转去收预期。这里必须把 action/verdict 都清干净，
        # 否则上一轮的答复会被 assert 阶段误当成判定（字段冲突）。
        return {"action": None, "verdict": None}

    step = steps[idx]
    # ⚠️ 不清 obs_fresh：上一步 act 带回的画面对**下一步**同样有效，
    # 没必要为了"进入新步骤"再 observe 一次（SKILL：act → 读 next → act）。
    # 只有画面不新鲜时（首次进入 / act 没带回 next）才真去 observe。
    #
    # ★ 本步是否已经落回模型，要看**步号有没有变**。
    #
    # 这里踩过一个真坑（跨版本名改成 iv_more_RENAMED 实测暴露的）：
    # 原先无条件清空 fallback_reason，但 plan 在**同一步内**也会被反复进入
    # （decide -> act -> check -> plan）。于是"已落回模型"这件事被一遍遍抹掉，
    # 路由又把它送回 strong_target，那里再失败……模型刚点的坐标全白费，
    # 最后在 A/B 两点之间弹到死循环，整条用例 BLOCKED。
    #
    # 正确语义：换步了才重置；同一步内**保留**"已交给模型"的状态。
    same_step = state.get("last_step_id") == step["id"]
    # ★ "已落回模型"这件事，只对**当屏**有效。
    #
    # 早先只要还在同一步就无条件保留 fallback_reason，后果是真机上
    # 一旦某轮代码没认出目标，剩下的 79 轮全走模型 —— 强证据机制被
    # 彻底绕过（168 实测：代码直点 0 次、模型 80 次）。
    #
    # 现在：换步 或 **换屏** 都重新给代码一次机会。
    # 换屏了还认不出，下一轮会再置一次 fallback，成本只是一次内存比对。
    _now_screen = _screen_fingerprint(state)
    _keep_fallback = (same_step
                      and state.get("fallback_screen") == _now_screen)
    return {
        # ★ 本步的轮次只在**换步**时归零。
        #
        # 原先无条件写 0，但 plan 在同一步内会被反复进入
        # （decide → act → check → plan），于是"本步已动过几次手"这件事
        # 每轮都被抹掉。依赖它的判断（落点预判"只在刚进本步时做一次"）
        # 因此**永远成立** —— 实测 loopblock 用例靠这个假通过：
        # 起点就在目标页，每轮都判"落点已满足"，一步不走却报 PASS。
        "round_in_step": (0 if not same_step
                          else state.get("round_in_step", 0)),
        # ★ 每步重置"换目标找"的计数 —— 上一步用掉的额度不该算到这一步头上
        "locate_attempts": 0,
        # ★ 落回模型的状态按步、按屏归零：换步/换屏就重新给代码一次机会。
        "fallback_reason": (state.get("fallback_reason", "")
                            if _keep_fallback else ""),
        "retry_landing": 0 if not same_step else state.get("retry_landing", 0),
        # ★ check 自己的轮次计数，换步归零。见 step_check_node 里 check_rounds 的说明。
        "check_rounds": 0 if not same_step else state.get("check_rounds", 0),
        "last_step_id": step["id"],
        "action": None,
        "issues": (
            # ★ 保留预备清单的来龙去脉，不要被这里清掉 ——
            #   plan 每步都会重置 issues，一旦把"还有 prepare 没做完"抹掉，
            #   外面就再也看不出为什么模型一直在点空白格。
            [f"进入 {step['id']}：前置预备还剩 {len(state.get('prepare_wants') or [])} 项未完成"]
            if (state.get("prepare_wants") and not state.get("prep_applied"))
            else []),
        "last_step_desc": step["desc"],
    }




def _step_evidence_of(state: ChildState, out: dict[str, Any]) -> list[dict[str, Any]]:
    """把这次观测打包成"属于当前用例步骤"的证据快照（**列表，供 operator.add 追加**）。

    同一 step_id 会累积多条，取用时取最后一条 —— 也就是"这一步最后一次观测"，
    因为断言问的是"这一步做完之后屏幕上该有什么"。
    """
    idx = int(state.get("step_index") or 0)
    steps = (state.get("task") or {}).get("steps") or []
    sid = str(steps[idx].get("id") or "") if idx < len(steps) else ""
    # ★ AI 为了验证某条断言而多做的动作，画面归到**该断言绑定的那一步**。
    #
    # 用户：「打开又关闭。AI如果为了验证某个条件多加的步骤应该也记录下来
    #        并截图。dump xml 和普通路径一样。」
    #
    # 走的是和普通步骤完全一样的落盘（dump.xml + shot.png + 控件清单），
    # 唯一区别是 `step` 标签 —— 只有这样重判时才取得到"点开之后"那一屏。
    _fix = state.get("assert_fix_task") or {}
    _bind = str(_fix.get("bind_step") or "")
    if _fix and _bind:
        sid = _bind
        for i, s in enumerate(steps):
            if str(s.get("id") or "") == _bind:
                idx = i
                break
    if not sid:
        return []
    # 截图：observe 会给 evidence.png；act 只给 xml_path，
    # 而落盘时 dump.xml 与 shot.png 同目录 —— 按同目录推。
    shot = (out.get("evidence") or {}).get("png") or ""
    if not shot:
        dump_p = str(out.get("xml_path") or "")
        if dump_p:
            cand = os.path.join(os.path.dirname(dump_p), "shot.png")
            if os.path.exists(cand):
                shot = cand
    return [{
        "step": sid,
        "step_index": idx,
        "activity": out.get("activity") or "",
        "package": out.get("package") or "",
        "shot": shot,
        "dump": _screen_xml_path(out),
        "nodes": _brief_nodes(out.get("nodes")),
        "nodes_text": _screen_text(out),
        "clickable": out.get("next") or [],
    }]


def _evidence_for_assert(state: ChildState, a: dict[str, Any]) -> dict[str, Any]:
    """取这条断言**该看的那一步**的证据快照。

    断言自己声明了 `step`（如 "s3"）就用它；没声明就用整轮最后一屏兜底
    —— 并且**把这个事实显式告诉调用方**，不允许静默地拿最后一屏冒充。

    ⚠️ 当前 176 的 14 条断言全是 step=-（未绑定），所以走得最多的还是
    兜底分支。这跟"每步证据存好了但没有对应关系"是一回事：**存**是
    第 1 件事，**绑**是另一件事，不能混为一谈。
    """
    ev = state.get("step_evidence") or []
    want = str(a.get("step") or "").strip()
    # 同一 step_id 会累积多条（每次 observe/act 一条），取**最后一条**
    # —— 那才是"这一步做完之后"的屏幕。
    mine = [e for e in ev if str(e.get("step") or "") == want] if want and want != "-" else []
    if mine:
        return {"source": f"步骤 {want} 的快照", "data": mine[-1], "bound": True}
    # 兜底：整轮最后一屏（并如实标注）
    if ev:
        return {"source": "整轮最后一屏（该断言未绑定步骤）",
                "data": ev[-1], "bound": False}
    return {"source": "无快照", "data": {}, "bound": False}


# ══════════════════════════════════════════════════════════════════════
# 视觉通道：颜色 / 形状 / XML 判不了的一律交给视觉模型
# ══════════════════════════════════════════════════════════════════════
#
# 用户定的通用规则（原话）：
#   「颜色，形状，还有 xml 判断不了的都要交给视觉模型。这个通用做法。」
#
# 所以要有一个统一的判断：**这条预期光看 XML 能不能定论**。不能的话，
# 判定前先用那一步的截图问一次视觉模型，把答案拼进判定提示词。
#
# 为什么不是"所有断言都上图"：实测（run 68）每条断言 51.7 万字符、
# 图占 99.3%，8 条断言共用同一张最后截图 —— 既烧钱又给错屏。
# 所以只在**确实需要**时问一次，并且问的是**该步骤那一张**。
#
# 为什么是关键词而不是"让模型自己说要图"：
# 那样每条断言都要多一次模型调用。关键词漏掉的，还有下面第 3 条兜底
# ——判定时明确告诉模型"数不全就判 null"，不会硬错。

# 颜色：色值、色块、背景色、主题色……
_VISION_COLOR_WORDS = (
    "颜色", "色块", "色值", "色彩", "配色", "背景色", "主题色",
    "变色", "红", "绿", "蓝", "黄", "紫", "橙", "灰",
)

# 形状 / 图形 / 视觉态：XML 只能给 rid，给不了"长什么样"
_VISION_SHAPE_WORDS = (
    "形状", "图标", "图形", "圆角", "圆形", "方形", "样式", "外观",
    "高亮", "置灰", "选中", "打勾", "对勾", "加号", "减号", "箭头",
    "阴影", "加粗", "斜体", "字号", "排版", "居中", "左对齐", "右对齐",
)

# 数量：XML 里同名节点会重复/嵌套，数不准（知识库实测 25≠10）
_VISION_COUNT_WORDS = ("共", "一共", "总数", "个数", "数量", "几种", "几条", "几项")


def _needs_vision(a: dict[str, Any]) -> tuple[bool, str]:
    """判断这条断言是否**必须看图**才能定论。

    返回 (要不要看图, 原因)。原因会写进判定提示词，让模型知道
    这张图是**为哪句话**问的，不是随手给一张。
    """
    text = str(a.get("desc") or "") + " " + str(a.get("check") or "")
    for w in _VISION_COLOR_WORDS:
        if w in text:
            return True, f"涉及颜色（命中「{w}」），XML 没有颜色信息"
    for w in _VISION_SHAPE_WORDS:
        if w in text:
            return True, f"涉及形状/视觉态（命中「{w}」），XML 只有 rid 描述不了外观"
    for w in _VISION_COUNT_WORDS:
        if w in text:
            return True, f"涉及数量（命中「{w}」），XML 同名节点会重复嵌套、数不准"
    return False, ""


def _vision_for_assert(state: ChildState, a: dict[str, Any],
                       evd: dict[str, Any]) -> dict[str, Any]:
    """需要看图时，用**该步骤那一张**截图问一次视觉模型。

    提示词写死 JSON 输出（用户要求：视觉检查提示词要准确、格式写死），
    并且**明确允许它答不上来** —— 答不上来比编一个答案有用得多，
    因为判定模型会因此判 null，而不是拿到一个假结论当证据。
    """
    need, why = _needs_vision(a)
    if not need:
        return {}
    shot = str((evd or {}).get("shot") or "").strip()
    if not shot or not os.path.exists(shot):
        return {"tried": True, "why": why, "ok": False,
                "error": f"该步骤没有可用截图（{shot or '路径为空'}）"}
    desc = str(a.get("desc") or "").strip()
    prompt = (
        f"你是手机界面视觉核验员。现在要核验一条测试预期是否成立。\n"
        f"预期原文：{desc}\n"
        f"需要你确认的：{why}\n\n"
        f"请**只看这张截图**回答，不要推测截图之外的任何东西。\n"
        f"如果截图里看不到能回答该预期的内容（例如相关弹窗没展开、"
        f"界面不对、被遮挡），can_answer 必须是 false —— "
        f"**不要**为了给答案而猜测或编造。\n\n"
        f"只输出一个 JSON 对象，不要 markdown 围栏，不要任何前后缀：\n"
        f'{{"can_answer": true, "answer": "一句话：预期成立不成立", '
        f'"detail": "你具体看到的内容，含数量/颜色/位置等可核对的事实"}}'
    )
    try:
        raw = ask_image(_ctx().python, _ctx().skill_root, shot, prompt)
    except VisionError as exc:
        return {"tried": True, "why": why, "ok": False, "error": str(exc)}
    # 视觉回复要求是 JSON；解析失败时**原样保留文本**给判定模型看，
    # 而不是丢掉 —— 至少那句话是视觉真看到的，扔了等于白问一次。
    got: Any = {}
    try:
        got = _extract_json_loose(raw)
    except Exception:
        got = {}
    if not isinstance(got, dict) or "can_answer" not in got:
        return {"tried": True, "why": why, "ok": True,
                "can_answer": None, "detail": str(raw)[:1500]}
    return {"tried": True, "why": why, "ok": True,
            "can_answer": bool(got.get("can_answer")),
            "answer": str(got.get("answer") or ""),
            "detail": str(got.get("detail") or "")[:1500]}


def _extract_json_loose(text: str) -> Any:
    """从视觉/模型回复里抠出第一个 JSON 对象（容忍围栏和前后缀）。"""
    s = str(text or "")
    if "```" in s:
        s = "\n".join(l for l in s.splitlines() if not l.strip().startswith("```"))
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("回复里没有 JSON 对象")
    return json.loads(s[i:j + 1])


def _trace_by_step(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐条动作轨迹按步骤归并：[{step, actions:[...]}]。

    为什么要归并：回写 YAML 需要的是"这一步依次点了什么"，
    而不是一长串扁平事件。归并后直接就能和 YAML 的 `do:` 列表对比。
    """
    order: list[str] = []
    bucket: dict[str, list[dict[str, str]]] = {}
    for item in raw or []:
        sid = str(item.get("step") or "")
        if not sid:
            continue
        act = item.get("action") or {}
        if not act:
            continue                      # 纯坐标点空白处：无从表达，丢掉
        if sid not in bucket:
            bucket[sid] = []
            order.append(sid)
        bucket[sid].append(dict(act))
    return [{"step": s, "actions": bucket[s]} for s in order]


def _strong_stats(state: ChildState) -> dict[str, Any]:
    """代码点击率：这次运行里**每次点击**是谁决定的。

    用户定的口径（原话）：
      「代码点击率应该是所有的步骤里有代码自动点击的有多少。
        问 AI 的有多少。」

    所以分子分母都是**点击次数**，不是步数、也不是决策轮次。
    三类来源全部计入：

      · 代码直点（strong_target 命中唯一）—— 省了一次模型调用
      · setup 记忆（清空数据后那几下固定弹窗）—— 也是代码点的
      · 工具层自动授予权限（点「允许」）—— 也是代码点的

    ## 为什么以前只算第一类（错的口径）

    早先只统计 `strong_hits` —— 那是**步骤内决策**的记录。
    setup 和权限框根本不写进 strong_hits（它们不经过 decide），
    于是"同意"明明是代码点的，报表里却看不见，代码点击率虚低。

    168 实测：改前报 0%（看着像机制没生效），
    实际「同意」早就由 setup 点掉了 —— 只是没被算进去。

    ## by_model 怎么来的

    `strong_hits` 里 by=model 的那些是"代码定不了、交给模型"的决策，
    一次决策对应一次点击，所以可以直接当点击次数用。
    """
    hits = state.get("strong_hits") or []
    by_code = [h for h in hits if h.get("by") == "code"]
    by_model = [h for h in hits if h.get("by") == "model"]

    # setup：清冷启动弹窗那几下（代码按记忆点的，没问模型）
    _setup_n = len(state.get("setup_applied") or [])
    # 工具层自动授予的权限（点「允许」/「全部允许」）
    _perm_n = len(state.get("runtime_permissions") or [])

    code_n = len(by_code) + _setup_n + _perm_n
    model_n = len(by_model)
    total = code_n + model_n

    fallbacks: list[str] = []
    for h in by_model:
        r = str(h.get("reason") or "")
        if r and r not in fallbacks:
            fallbacks.append(r)
    return {
        # 兼容老字段名：by_code/by_model 就是点击次数
        "by_code": code_n,
        "by_model": model_n,
        # 拆开也留着，方便看"代码点的这几次分别是谁"
        "by_code_strong": len(by_code),
        "by_code_setup": _setup_n,
        "by_code_permission": _perm_n,
        "auto_rate": round(code_n / total, 3) if total else 0.0,
        "fallback_reasons": fallbacks,
    }


def _stalled_at(state: ChildState, steps_done: int, total: int) -> str:
    """卡在哪 —— 直接回答"为什么没跑完"（用户第 1、2 条优化）。

    用户原话：「日志记录。来分析每个 case 卡在哪里了」
              「子 agent 的状态能否上报给 Agent 呢。不然 Agent 干等？」

    所以这里不只给结论，给出**卡点的具体位置和原因**：
      · 走完了        -> ""（没卡）
      · 有 stuck 事件 -> 那个位置的原文
      · 否则          -> 停在哪个步骤 + 当时在哪个界面
    """
    prog = state.get("progress") or []
    for ev in reversed(prog):
        if ev.get("phase") == "stuck":
            return str(ev.get("note") or "")
    if steps_done >= total and total > 0:
        return ""
    steps = (state.get("task") or {}).get("steps") or []
    if not steps:
        return "无步骤定义"
    i = min(max(state.get("step_index", 0), 0), len(steps) - 1)
    sid = steps[i].get("id") or "?"
    act = state.get("last_activity") or "未知界面"
    # 最后一条动作记录能说清"点的什么、去了哪"
    last_act = ""
    for ev in reversed(prog):
        if ev.get("phase") == "act":
            last_act = f"｜最后动作：{ev.get('note')}"
            break
    return f"停在 {sid}（进度 {steps_done}/{total}），当前界面 {act}{last_act}"


def _elapsed_s(state: ChildState) -> float:
    """总耗时（秒）—— 从第一条进度事件算起。"""
    prog = state.get("progress") or []
    if len(prog) < 2:
        return 0.0
    try:
        return round(float(prog[-1]["t"]) - float(prog[0]["t"]), 1)
    except Exception:  # noqa: BLE001
        return 0.0


# 这些节点的时间基本全花在**等模型**上（网络往返 + 推理）。
# observe/act/vision 不算：它们等的是设备。
_MODEL_NODES = ("decide", "assert", "knowledge")


def _model_seconds(state: ChildState) -> float:
    """模型相关节点的累计耗时（秒）。

    数据来自 runner._drive 从 stream 里数出来的 node_calls。
    没有它时返回 0，不去猜 —— 猜出来的数字比没有更糟。
    """
    calls = state.get("node_calls") or {}
    ms = 0.0
    for name in _MODEL_NODES:
        rec = calls.get(name) or {}
        try:
            ms += float(rec.get("ms") or 0)
        except (TypeError, ValueError):
            continue
    return round(ms / 1000.0, 1)


def _step_id(state: ChildState) -> str:
    """当前步骤号（越界时返回最后一步）—— 只用于进度上报。"""
    steps = (state.get("task") or {}).get("steps") or []
    if not steps:
        return ""
    i = min(max(state.get("step_index", 0), 0), len(steps) - 1)
    return str(steps[i].get("id") or "")


def _tick(phase: str, note: str, *, step: str = "") -> dict[str, Any]:
    """记一条进度事件（用户的第 2 条优化：别让主 Agent 干等）。

    刻意**短**：主 Agent 要的是"现在到哪了"，不是子 Agent 的完整日志
    （搬过去就等于没隔离 context）。所以每条只留：何时、哪一段、哪一步、一句话。
    """
    return {"t": round(time.time(), 1), "phase": phase,
            "step": step, "note": str(note)[:200]}


def _screen_xml_path(out: dict[str, Any]) -> str:
    """从观察结果里取**落盘 XML 的路径**（不是内容）。

    XML 一律落盘不进口袋：一份日历首页 dump 约 34000 字符，
    每轮都塞进 prompt 的话，几十轮就是百万 token，而 95% 是布局容器。
    判据要查的时候从这个路径读，读完就丢。
    """
    ev = out.get("evidence") or {}
    return str(ev.get("xml") or "")


def _screen_text(out: dict[str, Any]) -> str:
    """把当屏**所有**文案拼成一段，供 `text=xxx` 判据兜底。

    为什么需要：`text=` 判据有时指向不可点的文字（标题/提示语）。
    只看 `next`（可点项）会判成"没有"，但屏幕上明明写着 ——
    这种"其实在，却说不在"的误判，比"确实不在"更难查。

    ⚠️ **必须去重**。早先每个节点的 `t`/`text`/`d`/`desc`/`label` 五个字段
    全部拼进去，而它们绝大多数时候是**同一个值的重复**（只是格式兼容）。
    结果同一条文案在一屏里出现 2~5 次，整段还被塞进每一轮的 prompt ——
    子 Agent 跑几十轮，这就是上下文迅速膨胀的主因之一。
    判据只做"包含"检查，重复值没有任何信息价值。
    """
    parts: list[str] = []
    seen: set[str] = set()

    def _add(v: Any) -> None:
        if not v:
            return
        s = str(v)
        if s in seen:
            return
        seen.add(s)
        parts.append(s)

    for n in (out.get("next") or []):
        for k in ("t", "text", "d", "desc", "label"):
            _add(n.get(k))
    # ★★ 语义节点里的文字也要收 —— 这是"当屏所有文案"的主体。
    #
    #   `next` 只有**可点项**。而断言最常看的恰恰是不可点的文字：
    #   弹框标题、分组小标题、提示语。漏掉它们，`text=` 判据就会
    #   把"明明写在屏幕上"判成"没出现"。
    #
    #   真机实测（用例 03，run#30/#31）：清数据后首次启动的权限说明框，
    #   标题「日历需要使用以下权限」在 alertTitle 上，「必要权限」「可选权限」
    #   是分组标题 —— 全是不可点 TextView。于是：
    #       a2（按钮 button1/button2，走 seen_rids）PASS
    #       a1/a3/a4（文案，走 seen_screens）FAIL —— 报"没见过该控件"
    #   可执行器明明看见了整个弹框，还把它的内容当理由点掉了。
    #   这种**假失败**比漏判更糟：会让人去改一个根本没坏的东西。
    #
    #   工具层一直有 `nodes`（summarize_xml 压过的语义节点，含不可点文字），
    #   这里直接用上。`_brief_nodes` 的注释也写着"保留有文字的节点"，
    #   等于两边本来就约定好了，只是这里没接上。
    for n in (out.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        for k in ("t", "text", "d", "desc", "label"):
            _add(n.get(k))
    # 工具层若直接给了整屏文案，直接用
    for k in ("nodes_text", "text", "all_text", "screen_text"):
        v = out.get(k)
        if isinstance(v, str) and v.strip():
            _add(v)
    return "\n".join(parts)


def _seen_texts(items: list[dict[str, Any]] | None,
                nodes: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """把当前屏带 rid 的控件的**文案**攒下来（供 seen_texts 累加）。

    和 `_seen_rids` 是一对：那个记"见过哪些 rid"，这个记"那时它写着什么"。

    ⚠️ 为什么非要有：断言在流程末尾才判，那时人早翻页了。
    168 的 a1 要求 node_text{rid: toolbar_title, equals: 课程表} ——
    toolbar_title 在第 5 轮明明写着"课程表"，判的时候停在 PhotoPicker，
    当前屏里没有它，于是被判成"不成立"、a1 从 PASS 改判 FAIL。
    **只有 rid 没有文案，这种判据就永远判不对。**
    """
    out: list[dict[str, str]] = []
    for n in (items or []):
        rid = str(n.get("r") or "")
        if not rid:
            continue
        txt = str(n.get("t") or n.get("text") or n.get("d") or "")
        out.append({"rid": rid, "text": txt})
    # ★ 值节点（clickable=false）不在可点列表里，但它们的文案恰恰是
    #   node_text 判据最常要读的东西（178 的「50分钟」「10分钟」）。
    #   不补这一路，那条判据就永远读不到值。
    for n in (nodes or []):
        if not isinstance(n, dict):
            continue
        rid = str(n.get("r") or n.get("rid") or "")
        if not rid:
            continue
        txt = str(n.get("t") or n.get("text") or "")
        if not txt:
            continue          # 没文字的值节点记了也没用
        out.append({"rid": rid, "text": txt})
    return out


def _seen_rids(items: list[dict[str, Any]] | None,
               nodes: list[dict[str, Any]] | None = None) -> list[str]:
    """把当前屏出现过的 rid 提取出来（供 seen_rids 累加）。

    断言的 node_present=rid=X 是"过程中出现过"的语义，
    而断言在流程末尾才判，那时 X 可能早已翻页消失。
    所以要一路把见过的 rid 攒下来（168 的 a2 就因此被误判成 FAIL）。

    ★★ `nodes` 这一路是必须的（178 实测）。
    「可点项」里只有**可点的**控件，而很多被断言的控件恰恰不可点：
    178 的 a2/a3/a4 要判 tv_lesson_duration / tv_break_duration /
    tv_morning_slot_count —— 它们是值节点（clickable=false），
    只收可点项就永远攒不到它们，于是三条全报 `rid ✘`。
    那不是产品没这个值，是**我们没记**，纯假失败。

    工具层的 `nodes` 是 summarize_xml 压过的语义节点（含不可点文字），
    里面带 rid —— 和 `_screen_text` 用的是同一批数据。
    """
    out: list[str] = []
    for it in items or []:
        rid = it.get("r") or it.get("rid")
        if rid and str(rid) not in out:
            out.append(str(rid))
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        rid = n.get("r") or n.get("rid")
        if rid and str(rid) not in out:
            out.append(str(rid))
    return out


def _step_knowledge(state: ChildState, task: dict[str, Any],
                    step: dict[str, Any]) -> str:
    """决定这一步带哪些知识卡内容。

    ★ 用户要求（原话）：「知识卡是APP相关的内容。需要经常查」

    所以现在**每一步都带**，不再只在第一步注入全文。但带的形态变了：

      第一步  -> 声明知识卡的**全文**（建立整体认知：这 App 有什么坑）
      后续步  -> 用本步的描述/定位/落点自动检索，**只带命中片段**
                 （既"经常查"，又不把整张卡反复塞爆上下文）

    另外模型仍可主动 need_knowledge 换词再查 —— 自动检索只是保底，
    它的关键词来自步骤文本，不一定命中模型当下真正关心的现象词。

    为什么必须自动：SKILL 把"先读卡"列为纪律，但实现上完全靠模型自觉发起
    检索 —— 实测 168 整条用例 knowledge 命中 0 次，纪律形同虚设。
    """
    got = (state.get("action") or {}).get("knowledge") or ""
    if got:
        return got
    files = task.get("knowledge_files") or []
    if not files:
        return ""

    idx = state.get("step_index", 0)
    if idx == 0:
        # 第一步给**目录**，不是全文。
        #
        # 为什么不给全文：日历知识卡 34552 字符，而 read_knowledge_file 上限
        # 4000 —— 等于只给了前 12%，且模型不知道后面还有 88%。
        # 实测 168：模型引用的全是开头那几条，`knowledge` 轮次 0 次，
        # 它压根没意识到"还能查更多"。
        #
        # 目录约 2400 字符（省 93%），但把"有哪些章节"完整摆出来，
        # 模型想深入时用 need_knowledge 按章节名或关键词取。
        return ("（以下是知识卡目录。需要某一节的详细内容，用 need_knowledge "
                "传章节名或关键词。）\n\n"
                + knowledge_mod.outline(_ctx().skill_root, files))

    # 后续步骤：按本步文本检索，只带命中的片段
    src_text = " ".join(str(step.get(k) or "")
                        for k in ("desc", "locate", "landing"))
    kws: list[str] = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fa5]{2,}",
                          src_text):
        if tok not in kws:
            kws.append(tok)
    snippets: list[str] = []
    seen: set[str] = set()
    for kw in kws[:6]:
        hit = search_knowledge(_ctx().skill_root, files, kw, limit=8)
        if not hit or hit.startswith("（"):
            continue
        if hit in seen:
            continue
        seen.add(hit)
        snippets.append(hit)
        if len(snippets) >= 3:       # 最多带 3 组，控制上下文体积
            break
    if not snippets:
        return ""
    return ("（以下是从知识卡按本步内容自动检索到的相关片段；"
            "需要别的内容可以 need_knowledge 换词再查）\n\n"
            + "\n\n".join(snippets))


def _brief_nodes(nodes: Any, limit: int = 60) -> list[dict[str, Any]]:
    """把工具层的语义节点列表压成"够判断、又不臃肿"的形状。

    ★ 用户的要求：「最新的页面 XML 信息」。
      但**不能真的把 XML 原文塞进去** —— 一屏 XML 实测 6~7 千字符，
      每轮都带就是纯浪费。工具层的 `summarize_xml` 已经把它压成语义节点
      （同一屏 6826 → 509 字符，省 93%），这里再按需精简一次。

    保留哪些：
      · **有文字的节点**（标题、空态文案、提示语 —— 判据最常看的就是它们）
      · **有 rid 的节点**（`rid=` 判据要用）
      · **可滑动 / 已选中 / 不可用** 这类状态位（判断"这个开关开着吗"要用）
    丢掉：纯布局容器（既无文字又无 rid、也没有状态位）—— 它们对判断毫无贡献。

    ⚠️ 上限是**按信息价值排序后**截断的，不是简单切前 N 个：
       一屏几十个布局容器排在前面的话，真正有文字的节点会被挤掉，
       模型又变回"看不见屏幕上的字"。
    """
    if not isinstance(nodes, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        rid = str(n.get("r") or n.get("rid") or "")
        txt = str(n.get("t") or n.get("text") or n.get("d")
                  or n.get("desc") or "")
        flags = bool(n.get("s") or n.get("scrollable")
                     or n.get("off") or n.get("disabled")
                     or n.get("checked") or n.get("selected"))
        # 三无节点（无 rid、无文字、无状态）对判断没有贡献
        if not rid and not txt and not flags:
            continue
        # 同一 (rid, 文字) 只留一次：summarize_xml 会为嵌套结构重复给出
        key = f"{rid}\x00{txt}"
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {}
        if rid:
            item["r"] = rid
        if txt:
            item["t"] = txt[:60]
        c = n.get("c") or n.get("center")
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            item["c"] = [c[0], c[1]]
        if n.get("s") or n.get("scrollable"):
            item["s"] = 1
        if n.get("off") or n.get("disabled"):
            item["off"] = 1
        if n.get("checked") or n.get("selected"):
            item["sel"] = 1
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _recent_actions(state: ChildState, n: int = 3) -> list[dict[str, Any]]:
    """取最近 n 次实际动作，给模型看"我刚点过什么"。

    ★ 用户的原话：「历史最近的3步」。
      只带最近 3 条是刻意的 —— 子 Agent 会跑几十轮，把整段轨迹塞进每轮
      prompt 纯属浪费：模型需要这些只是为了避免重复点同一个地方，
      以及理解自己是怎么走到当前屏的。更早的历史对"下一步"没有价值。

    ⚠️ 数据来自 `action_trace`（act_node 每轮都会追加），不是另起炉灶。
       `decision_channel` 里那段 `payload["history"]` 的渲染逻辑一直是**死代码**
       ——从来没有人填过它，所以模型永远看不到"本步最近点过的位置"，
       于是反复点同一处，直到代码的循环检测事后把它拦下来。

    返回形如：
        [{"at": "s1", "rid": "cv_empty_content", "xy": [1520,1206],
          "why": "点空格子", "to": ".EditCourseActivity"}, ...]
    最新的在最后（和渲染顺序一致，模型读起来是时间顺序）。
    """
    trace = state.get("action_trace") or []
    out: list[dict[str, Any]] = []
    for t in trace[-max(1, n):]:
        if not isinstance(t, dict):
            continue
        act = t.get("action")
        if not isinstance(act, dict):
            continue
        item: dict[str, Any] = {}
        step = t.get("step")
        if step:
            item["at"] = step
        # 坐标动作
        if act.get("x") is not None and act.get("y") is not None:
            item["xy"] = [act["x"], act["y"]]
        # 系统键（back/home）
        if act.get("key"):
            item["key"] = act["key"]
        for k_out, k_in in (("rid", "rid"), ("text", "text"),
                            ("why", "why"), ("to", "to")):
            v = act.get(k_in)
            if v:
                # why 是模型自己写的，可能很长 —— 截断，它的作用只是唤起记忆
                item[k_out] = str(v)[:60] if k_out == "why" else str(v)
        if item:
            out.append(item)
    return out


def _base_decide_node(state: ChildState) -> dict[str, Any]:
    """AI 判断点：这一步做完了吗？下一步点哪？（interrupt，无副作用）

    两种模式：
      - 起点纠正模式（start_mismatch 有值）：目标不是"完成步骤"，
        而是"走回用例期望的起点"。提示词和判据都不同。
      - 正常模式：推进当前步骤。
    """
    from langgraph.types import interrupt

    task = state["task"]
    idx = state.get("step_index", 0)
    steps = task["steps"]
    mismatch = state.get("start_mismatch")

    if mismatch:
        # 起点纠正：不测任何步骤，只想办法回到起点
        #
        # ★ 把 goto 的 path 提示一起带上 —— 用例 YAML 里那段话是前人的实测路径
        #   （「点右上角更多 iv_more → 菜单里点课程表」），比让模型瞎找强得多。
        _nav = state.get("nav_wants") or []
        nav_hint = "\n\n".join(
            f"· 目标：{n.get('target') or n.get('desc') or ''}\n"
            f"  前人记的路径：{n.get('hint') or '（无）'}"
            for n in _nav if (n.get("hint") or n.get("target")))

        # ★ prepare 前提的动作清单 —— 走到起点后还要按这些动作把数据准备好。
        #
        # 实测 182：只把清单塞进 issues，模型看不到，
        # 到了课程表页照样先点空白格（那时没有空白格，因为课表还没建），
        # 11 轮不收敛。这里必须写成模型能直接照做的指令。
        _prep = state.get("prepare_wants") or []
        prep_hint = "\n\n".join(
            f"· 目标：{p.get('desc') or ''}\n"
            f"  到「{p.get('assert_start') or '目标页'}」之后，先按顺序做：\n"
            + "\n".join(
                f"    {i}. {('点 ' + a['rid']) if a.get('op') == 'tap' else ('在 ' + a['rid'] + ' 里输入 ' + repr(a.get('text')))}"
                for i, a in enumerate(p.get("actions") or [], 1))
            for p in _prep)
        payload = {
            "kind": "decide",
            "mode": "fix_start",
            "start_mismatch": mismatch,
            "want_activity": task.get("start_assert"),
            "nav_targets": _nav,
            "prepare_targets": _prep,
            "activity": state.get("last_activity"),
            "clickable": state.get("obs_clickable", []),
            "knowledge_files": task.get("knowledge_files", []),
            "knowledge": (state.get("action") or {}).get("knowledge") or "",
            "round": state.get("start_fix_rounds", 0),
            "question": ("当前不在用例起点。像人类测试员一样**自己想办法走回去**"
                         "（可先 need_knowledge 查路径图，或直接点合适的入口/返回键）。"
                         + (f"\n\n用例前提给出了前人走过的路径，优先照它走：\n{nav_hint}"
                            if nav_hint else "")
                         + (f"\n\n⚠️ 到达起点后，前置数据还没准备。"
                            f"先把下面这些动作依次做完（用 tap 点对应 rid；"
                            f"输入用 input_text），**做完再谈步骤**：\n{prep_hint}"
                            if prep_hint else "")
                         + "\n\n回到起点后置 step_done=true；"
                         "实在回不去置 give_up=true 并说明原因。"),
        }
        answer = interrupt(payload, response_schema=StepAction)
        if isinstance(answer, StepAction):
            answer = answer.model_dump()
        return {"action": answer,
                "start_fix_rounds": state.get("start_fix_rounds", 0) + 1}

    step = steps[idx] if idx < len(steps) else steps[-1]

    # ★ prepare 清单在**正常模式**下也要带上：
    #   起点一旦对了就走 step 模式，可前置数据可能还没准备
    #   （prep 阶段做不了，因为那时界面不对）。
    #   不带的话模型会拿着一份"没有空白格"的周视图去点 s1，怎么都点不到。
    _prep_norm = state.get("prepare_wants") or []
    prep_hint_norm = "\n\n".join(
        f"· 目标：{p.get('desc') or ''}\n"
        f"  先按顺序做完：\n"
        + "\n".join(
            f"    {i}. "
            + (f"点 {a['rid']}" if a.get("op") == "tap"
               else f"在 {a['rid']} 里输入 {a.get('text')!r}")
            for i, a in enumerate(p.get("actions") or [], 1))
        for p in _prep_norm)

    payload = {
        "kind": "decide",
        "mode": "step",
        "step_id": step["id"],
        "step_desc": step["desc"],
        "step_landing": step.get("landing"),
        "step_target": step.get("target") or step.get("locate"),
        # ★ 坑：**只给这一步的**。
        # 用例 YAML 的元规则（原话）：「跑到第 2 步时第 1 步的坑
        # 还在眼前，信息被稀释，真正相关的那条反而看不见」。
        # 所以宁可这一步没有坑、也不把别步的坑铺过来。
        "pitfalls": step.get("pitfalls", []),
        "pitfalls_scope": ("only_this_step" if task.get("per_step_pitfalls")
                           else "all"),
        "activity": state.get("last_activity"),
        "clickable": state.get("obs_clickable", []),
        # ★★ 当前屏的完整语义节点（含不可点的文字）。
        # `clickable` 只有可点项，而很多判据要看的恰恰是不可点的东西 ——
        # 标题、空态文案、「还未添加课程表」这类提示语。
        # 只给可点项，模型就"看不见"屏幕上明明写着的字。
        "screen_nodes": state.get("obs_nodes") or [],
        "knowledge_files": task.get("knowledge_files", []),
        # ★ 第一步自动带上该用例的知识卡内容。
        # SKILL 说"先读卡：路径图告诉你「在哪找」，知识卡告诉你「有什么坑」"，
        # 但早先完全靠模型自觉 need_knowledge 去要 —— 实测 168 跑了 12 轮
        # 一次都没触发（knowledge: 0）。凡是"用例已声明"的东西都该由代码给出，
        # 不能指望模型主动索取。后续步骤仍可 need_knowledge 按需再查。
        "knowledge": _step_knowledge(state, task, step),
        # ★★★ 上一轮看图的结果，必须在这一轮告诉模型 ★★★
        #
        # 这是 167 空转的真正病根（比 max_tokens 更根本）：
        #   vision 节点把答案写进 action["vision"]，然后路由去 observe
        #   → 重新 decide。可 decide 的 payload 里**从来就没有 vision**，
        #   模型只看得到自己上一轮问的问题，看不到答案。
        #
        #   于是它以为"还没看到"，换个说法再问一次 —— 21 次、95 秒。
        #   而我加的那个"失败 2 次就停"的闸门根本没触发，
        #   因为视觉是**成功**的，只是没人把结果递给它。
        #
        #   这就是"加了个刹车但没接上油管"：修的是失败路径，坏的是成功路径。
        "vision": state.get("last_vision") or "",
        "round": state.get("round_in_step", 0),
        # ★ 代码没能直点时，**明确告诉模型"代码卡在哪了"**。
        #
        # 不告诉的话模型会从零重新理解这一步 —— 实测它跑去点
        # top_button_date_layout / left_button 这些跟本步无关的控件，
        # 在两点之间弹 6 轮才被死循环检出。
        # 说清"要找的是什么、为什么定不下来"，它才知道该解决什么问题。
        "code_gave_up": state.get("fallback_reason") or None,
        # ★ 前置数据还没准备完的话，把动作清单交给模型（见 prep_hint_norm）
        "prepare_targets": _prep_norm,
        # ★ 用测试员语言交代处境：这步要干什么、现在在哪个界面、
        #   上次踩过什么坑、该用什么手段。见 _ask_model 的注释。
        "situation": state.get("fallback_guidance") or None,
        # ★ 权限框没能自动处理时，把现场交给模型（工具层只点"安全"的按钮）。
        # 典型：第二次申请照片/相机权限，弹窗多了「拒绝并不再询问」和
        # 「允许访问所选照片」——这两个工具层故意不点，只能由模型按用例决定。
        "perm_unmatched": state.get("perm_unmatched") or None,
        # ★ 当前前台包名：模型据此判断"我是不是已经跑到别的 App 里了"。
        # 169 实测：跑进 com.zui.camera 后模型还在找日历的 btnImportFromGallery，
        # 它知道自己"在相机页"，但没意识到**这已经不属于被测 App**。
        "package": state.get("obs_package") or None,
        # ★ 被测 App 的包名 + 是否已经跑到别的 App 里。
        # 这是"换前提"的信号：当 front package ≠ 被测包，模型该做的是**先回到
        # 被测 App**，而不是在当前这个陌生 App 里找目标控件。
        "app_package": task.get("package") or None,
        "off_app": bool(
            state.get("obs_package")
            and task.get("package")
            and state.get("obs_package") != task.get("package")
            and not is_system_permission_pkg(str(state.get("obs_package")))
        ),
        # ★ 可用的系统级动作（代码保证支持，见 StepAction.key）。
        # 不列出来模型不会想到用 —— 可点列表里只有 tap 目标。
        "system_actions": ["back", "home"],
        # ★★ 最近 3 步做过什么（用户要求：「历史最近的3步」）。
        #
        # 为什么是 3 而不是全部：子 Agent 跑几十轮，把整段轨迹带进每轮 prompt
        # 是纯浪费 —— 模型真正需要的只是"我刚才点过哪、结果如何"，
        # 用来避免重复点同一个地方、以及理解自己是怎么走到当前屏的。
        # 更早的历史对"下一步点哪"没有帮助，只占 token。
        #
        # 这也补上了一个真实缺陷：`decision_channel` 早就渲染 `history`
        # 字段（写明「本步最近点过的位置，不要重复点」），但**从来没人填过它**，
        # 属于死代码 —— 模型一直看不到自己刚点过什么，所以会反复点同一处，
        # 直到代码的循环检测（loop_warning）事后把它拦下来。
        "recent_actions": _recent_actions(state, 3),

        # ★★ 这条用例**最终要验证什么**。
        #
        # 早先完全不告诉子 Agent asserts —— 它只知道"这一步要走到哪"，
        # 不知道"走完要看什么"。于是执行时没有任何方向感：
        # 该留意「周末有课」开关状态的时候没留意，该记下颜色名的时候没记，
        # 到最后断言阶段只能靠翻痕迹猜，甚至反过来重跑一遍。
        #
        # 只给 id + 一句话描述 + 判据原文，**不给整份 YAML** ——
        # 目的是让模型知道"往哪个方向看"，不是让它替代码判断言
        # （断言由代码闸门 + 独立判定节点负责）。
        "asserts_brief": [
            {"id": a.get("id"), "desc": a.get("desc"),
             "check": a.get("check") or a.get("expect")}
            for a in (task.get("asserts") or [])
        ],
        # 当前进行到第几条断言（让模型知道整体进度，避免"以为快完了"）
        "asserts_total": len(task.get("asserts") or []),
        "steps_total": len(task.get("steps") or []),
        "question": (
            "这一步完成了吗？"
            "① 完成了 -> step_done=true；"
            "② 需要点屏幕 -> 给 next_x/next_y（必须取自 clickable[].c）与 why；"
            # ★ 输入文字：必填项的正解。缺了它模型只能一直点输入框。
            #
            # 176 实测（两轮都是这个病）：模型每次都把原因说得完全正确 ——
            #   「名称是必填项，先点进输入框让键盘弹出，才能输入文字」
            # 然后它的动作是 `点 (1520,375)`。因为**动作清单里没有"输入"这一项**。
            # 点输入框界面不会变 -> 被当成无效动作 -> 连点 3 次被熔断 -> BLOCKED。
            #
            # 教训：模型想到了要做什么，还得有对应的**手段**交给它。
            #      只把话写在 schema 的 description 里不够 —— 模型读的是这段 question。
            "③ 需要**往输入框里填文字** -> 给 input_rid（输入框的 rid，取自 "
            "clickable[].r）和 input_text（要填的内容），**不要给 next_x/next_y**。"
            "这一步只负责填，填完下一轮再决定要不要点保存；"
            "④ **当前页面走不通/已经偏离到别的 App** -> "
            "不要死点语义不明的图标，直接给 key=\"back\" 退回上一层，"
            "必要时 key=\"home\" 回桌面再重新进入 App。"
            # ★ 弹窗的定位（用户定的分工）：
            #   弹窗**不是判据**，它是干扰。用例关心"点了按钮后跳到哪"，
            #   不关心中途蹦出个什么框。
            #   所以处理方式是：看到挡路的框 -> 点掉它（申报 is_dialog=true）
            #   -> 再去做本步本来要做的事。点掉弹窗不会算作"重复点击"。
            "若屏幕上有挡路的弹窗（如权限说明框、更新提示、知道了），"
            "先点掉它并申报 is_dialog=true，然后**继续完成本步本来的目标** —— "
            "弹窗只是干扰，不要因为出现了弹窗就判定本步完成或失败。"
            # ★ 前置数据优先：没有准备好就直接点本步，点了也是白点
            #   （182 实测：周视图没有空白格，就是因为课表还没建）。
            + (f"\n\n⚠️ 注意：这条用例的前置数据还没准备好。"
               f"**先按下面的动作把前提做掉**，再谈本步骤：\n{prep_hint_norm}"
               if prep_hint_norm else "")),
    }
    answer = interrupt(payload, response_schema=StepAction)
    if isinstance(answer, StepAction):
        answer = answer.model_dump()

    # ★★★ 代码兜底：模型只说"要输入"却给了坐标 -> 由代码改成输入动作 ★★★
    #
    # ## 为什么需要这条（不是模型不够聪明）
    #
    # 176 两轮实测，模型**每次判断都对**，原话：
    #   「名称是必填项，先点进 et_schedule_name 输入框让键盘弹出，才能输入文字」
    # 然后它给的动作是 `点 (1520,375)`。
    #
    # 它想干的事（输入）和能表达的动作（点坐标）对不上。
    # 我在 question 里加了 ③ 输入分支之后它可能就会用了，但**不能只靠它自觉**——
    # 用户在这一点上说得非常清楚：规则要用代码约束，不靠模型自觉。
    #
    # 所以这里做一层翻译：**意图已经明确要输入，就给输入**。
    #
    # ## 判据（必须保守，不能瞎猜）
    #
    # 三条同时成立才转换：
    #   ① 模型没给 input_rid（给了说明它自己会用，不用管）
    #   ② 模型给了坐标，且那个坐标上的控件**是个输入框**（class 含 EditText）
    #   ③ 模型的 why 里明确出现了"输入/填写/键入"这类意图词
    #
    # ②是关键：只有点到的是真输入框才转换。点普通按钮时坐标不是输入框，
    #      这条自然不生效，不会把"点保存"错改成"往保存按钮里写字"。
    #
    # ## 填什么内容
    #
    # 模型在 why 里通常写了要填的值（引导语也教了）。这里从 why 的反引号
    # /「」里取，取不到就用占位符文字反推。
    # 取不到内容就**不转换**（宁可让模型下一轮自己说清楚，也不瞎填）。
    answer = _coerce_input_intent(answer, state)
    return {"action": answer}


class StepAction(BaseModel):
    step_done: bool = Field(description="本步是否已完成")
    next_x: int | None = None
    next_y: int | None = None
    # ★ 系统级动作：屏幕上看不到「返回」，但它是离开当前页最可靠的手段。
    #
    # 为什么必须有（169 卡死 4 分钟的直接原因）：早先模型表达动作的唯一方式是
    # next_x/next_y，于是它**只能点屏幕上的像素**。169 跑进系统相机后，
    # 可点列表里只有 flash_toggle_btn / camera_switch_button / shutter_button /
    # intent_back_button —— 模型想"退出相机"却无处表达，只能反复点
    # intent_back_button（那是「查看」不是返回），13 轮不中，白烧 4 分钟。
    #
    # 有了这个字段，它可以像人一样「按一下返回键」。
    key: str | None = Field(
        default=None,
        description=("系统级按键动作，不点屏幕：back（返回，离开当前页最可靠）、"
                     "home（回桌面）。当屏幕上找不到合适的控件、或需要退出"
                     "当前 App/页面时**优先用它**，不要去点语义不明的图标。"),
    )
    # ★ 输入文字：遇到输入框（必填项）必须能填内容，否则永远过不去。
    #
    # 为什么必须有（176 卡住的直接原因）：日历「新建课程表」的名称是必填项，
    # 执行器当时只有"点"没有"输入"，于是点了「完成」被校验拦住、页面不动，
    # 落点判据永远不满足，看起来就是"一直在点完成"。
    # 加了这个字段，模型可以像人一样"先填名称再点完成"。
    input_rid: str | None = Field(
        default=None,
        description=("要输入文字的控件 rid（如 et_schedule_name）。"
                     "和 input_text 一起用；设了它就不要再给 next_x/next_y。"),
    )
    input_text: str | None = Field(
        default=None,
        description=("要填进去的文字（如「测试课程表」）。"
                     "遇到必填输入框就**先把内容填上再继续**，"
                     "不要反复点提交按钮硬试 —— 点了也不会通过校验。"),
    )
    # ★ 语义点击目标（新契约）。模型只说点哪个控件，坐标由 act 解析。
    #
    # 必须在这里声明：没声明的键会被 pydantic **静默丢掉** ——
    # StepAction 是 interrupt 的 response_schema，模型给的字段
    # 不在 schema 里就等于没给（`unknown`/`error` 就是被这样丢过一次）。
    tap_target: str | None = Field(
        default=None,
        description=("要点的控件名（rid 或文字，取自当前可点项）。"
                     "同名多个时用「名字#序号」。不要给坐标。"),
    )
    why: str = ""
    expect: str | None = Field(default=None, description="落点判据")
    need_knowledge: str | None = Field(default=None, description="想检索的关键词，没有就留空")
    vision_question: str | None = Field(default=None, description="需要看图时的问题，没有就留空")
    give_up: bool = Field(
        default=False,
        description=("本步走不通且已修正过一次仍不行 -> 置 true 转入下一步并记问题。"
                     "对应 SKILL「停在别的页面 -> 尝试修正一次，仍不中才停」"))
    give_up_reason: str = ""
    stuck: bool = Field(
        default=False,
        description=("仅代码内部使用：这条 give_up 是不是「整条用例都别跑了」的"
                     "真卡死（页面根本没变 / 死循环）。"
                     "模型不用填 —— 填了也没人信，见 check 的处理。"))
    note: str = ""

    # ── 副作用声明 ──────────────────────────────────────────────
    # 你点掉的按钮是不是"同意/允许"性质，必须自己说清楚。
    # 这属于不可逆的现场变更（授权一旦给出，下次起跑线就不同了），
    # 会原样落进报告供人审计。
    #
    # ⚠️ 判据是"**这个按钮是不是同意/允许**"，不是"它算不算系统权限框"。
    # 实测教训：日历有个自绘的「访问文件相关信息（日历需要使用以下权限）」
    # 说明页，按钮文案是「同意」——模型认为"这不是系统权限框"就没申报。
    # 从技术分类上它没错，但对审计者来说"点了同意"这个**事实**才是关键，
    # 纠结分类会让最该记的东西漏掉。所以宁可宽一点：看到同意/允许就记。
    grants_permission: bool = Field(
        default=False,
        description=("你这一步点的按钮，是否是**同意/允许/授权**性质的？"
                     "（文案含 同意/允许/始终允许/仅本次/授权，"
                     "或系统权限框里的肯定按钮）是=True。"
                     "普通业务按钮、知道了/确定这种纯提示框=False。"))
    permission_name: str = Field(
        default="", description="若 grants_permission=True：这是什么权限（如 存储/相机/位置）")
    permission_decision: str = Field(
        default="", description="若 grants_permission=True：allow / deny / 仅本次")
    is_dialog: bool = Field(
        default=False,
        description=(
            "这一步点的是不是**挡路的弹窗**？\n"
            "⚠️ 这个字段**不区分**系统权限框和 App 弹窗 —— 它只表示"
            "「这一步是清掉挡路的东西，不是本步的业务操作」。"
            "是不是系统权限弹窗由代码按包名判定，你不用判断。"
        ))
    dialog_text: str = Field(
        default="", description="若 is_dialog=True：弹窗上的主要文字（如「知道了」「访问文件相关信息」）")


def _trace_of_tap(action: dict[str, Any],
                  clickable: list[dict[str, Any]] | None) -> dict[str, str]:
    """把"这一次点了什么"压成可回写的形态：{rid}|{text}|{}（空=无从表达）。

    为什么优先 rid、其次 text：
      · rid 最稳（YAML 里首选）
      · 没有 rid 时用 text —— 换了 rid 还能靠文案找到
      · 两者都没有（纯坐标点空白处）返回 {}：**坐标绝不能写进 YAML**，
        换设备/换分辨率必失效，写进去等于给后来人埋雷。
    """
    rid = str(action.get("_rid") or "").strip()
    if rid:
        return {"rid": rid}
    item = action.get("_item") or {}
    if isinstance(item, dict):
        if str(item.get("r") or "").strip():
            return {"rid": str(item["r"]).strip()}
        for k in ("t", "text", "d", "desc"):
            if str(item.get(k) or "").strip():
                return {"text": str(item[k]).strip()}
    # 按坐标在当前可点列表里反查（模型自己点的走这条路）
    try:
        x = int(action.get("next_x"))
        y = int(action.get("next_y"))
    except (TypeError, ValueError):
        return {}
    for n in (clickable or []):
        c = n.get("c") or []
        if len(c) >= 2 and int(c[0]) == x and int(c[1]) == y:
            if str(n.get("r") or "").strip():
                return {"rid": str(n["r"]).strip()}
            for k in ("t", "text", "d", "desc"):
                if str(n.get(k) or "").strip():
                    return {"text": str(n[k]).strip()}
    return {}


def _base_act_node(state: ChildState) -> dict[str, Any]:
    """执行点击。【无 interrupt】—— 保证恢复不重放。

    ⚠️ 关键：act 的返回里已经带了动作后的画面（activity/next/checkpoint），
    所以**动作后不需要再 observe**。这是 SKILL 的核心节奏：
        act → 读 next → act → …
    observe 只在两种情况下用：① 会话第一步 ② next 里找不到目标。
    早先每步都 observe，白多一倍往返，还把 next 冲掉了。
    """
    dev = _ctx().device
    action = state.get("action") or {}

    # ★★ 语义目标 -> 坐标（新契约 kind=tap 走这里）★★
    #
    # 模型的契约是「点哪个控件」，不是「点哪个像素」。它给 tap_target
    #（可点项的名字/rid），坐标由**观察层的解析结果**决定：
    #   · 唯一命中 -> 拿到 (x,y)
    #   · 不唯一    -> 不猜第一个，报错让模型用 `名字#序号` 指明
    #   · 找不到    -> 报错让模型换目标
    #
    # 这和 strong_target 用的是**同一个解析器**（_resolve_strong_target），
    # 所以"代码直点"和"模型指定"两条路的行为完全一致 ——
    # 不会出现"代码说唯一、模型点了别的"这种两套逻辑打架。
    if action.get("tap_target") and action.get("next_x") is None:
        _tt = str(action.get("tap_target") or "").strip()
        _tx, _ty, _thow = _resolve_strong_target(_tt, state)
        if _tx is None:
            return {"issues": [f"点不了「{_tt}」：{_thow}"],
                    "action": {**action, "acted": False, "error": _thow}}
        action = {**action, "next_x": _tx, "next_y": _ty,
                  "why": (str(action.get("why") or "") or "")}

    # ★ 输入文字动作：填必填输入框。
    #
    # 顺序上放在最高优先级 —— 模型说"先填名称"时，这一轮就只做填，不要又去点。
    # 填完之后**不清 action.step_done**，下一轮照常由模型决定点不点提交。
    _irid = str(action.get("input_rid") or "").strip()
    _itext = action.get("input_text")
    if _irid and _itext is not None and not action.get("step_done"):
        # ★★ 已经有这个值了就别再填 ★★
        #
        # 176 实测（run 65）：模型对 et_schedule_name 连填 19 次，
        # 其中 18 次 chg=0 —— 第一次就填进去了，它看不见，以为没填上。
        # 这不是模型蠢：**输入动作在界面上不留可见反馈**（值填进去了，
        # 但对 XML 的可点项清单几乎无影响），它只能靠"上次 why"猜。
        #
        # 所以由代码判死：值已经一样，这个动作就是无效的，直接拒掉。
        # 拒掉要**明确告诉它"已经填好了，去点保存"** ——
        # 只说"无效"，它会换个写法再填一遍。
        if not action.get("_retry_after_guard"):
            _cur = _current_input_value(state, f"rid={_irid}")
            if _cur is not None and str(_cur).strip() == str(_itext).strip():
                # 用 _ask_model 而不是自定义字段：它会把话拼进下一轮的
                # 提示词里交给模型。自己加个没人读的键等于没说话。
                return _ask_model(
                    state,
                    f"输入框 {_irid} 里**已经有** {_cur!r} 了，内容和你这次要填的"
                    f"一模一样，**不需要再输入**。"
                    f"请直接去做这一步剩下的事（通常是点「完成」/「保存」提交，"
                    f"或者判定已经完成）。")
        try:
            # step/why 传下去是为了**账本里能查到这一步**：
            # 只记 rid 和时间不记步骤号，页面上一堆动作就分不清属于哪一步。
            res = dev.input_text(_irid, str(_itext),
                                 step=_step_id(state),
                                 why=str(action.get("why") or ""))
        except (ToolError, AttributeError, TypeError) as exc:
            return {"issues": [f"输入 {_irid} 失败：{exc}"],
                    "action": {**action, "acted": False, "error": str(exc)}}
        if res is None:
            return {"issues": [f"输入 {_irid} 失败：当前屏找不到这个控件"],
                    "action": {**action, "acted": False,
                               "error": f"找不到 {_irid}"}}
        return {
            "action": {**action, "acted": True,
                       "event_ids": res.event_ids,
                       "checkpoint": res.checkpoint},
            "last_activity": res.activity,
            "last_event_ids": res.event_ids,
            "last_package": getattr(res, "package", "") or "",
            "obs_clickable": res.next or state.get("obs_clickable", []),
            # 不写 action_trace：输入不是"点"，回写 YAML 时不该被当成点击动作
        }

    # 系统键动作（back/home）：没有坐标，走 dev.key 而不是 dev.act。
    #
    # 两个来源都认：
    #   `_key` —— 代码回放层给的（YAML 里写 `back: true`）
    #   `key`  —— **模型自己给的**（StepAction.key）。这是"YAML 不对时
    #            模型能自己绕过去"的关键能力：169 的模型想退出相机，
    #            但早先它只能在屏幕上点像素，没有表达"按返回"的途径。
    _k = action.get("_key") or action.get("key")
    if _k and not action.get("step_done"):
        try:
            task = state.get("task") or {}
            res = dev.key(str(_k),
                          why=str(action.get("why", "")),
                          expect=action.get("expect"),
                          perm_action=task.get("perm_action"),
                          perm=task.get("perm_name"),
                          step=_step_id(state))
        except ToolError as exc:
            return {"issues": [f"key 失败：{exc}"],
                    "action": {**action, "acted": False, "error": str(exc)}}
        return {
            "action": {**action, "acted": True,
                       "event_ids": res.event_ids,
                       "checkpoint": res.checkpoint},
            "last_activity": res.activity,
            "last_event_ids": res.event_ids,
            "obs_clickable": res.next,
            "obs_package": res.package,
            # 轨迹：按键动作 —— 回写 YAML 时需要它
            "action_trace": [{"step": _step_id(state),
                              "action": {"key": str(_k).strip().lower()}}],
        }

    if action.get("step_done") or action.get("next_x") is None:
        return {"action": {**action, "acted": False}}

    try:
        # 权限意图**必须在动作之前**传给工具层，否则本次动作触发的弹窗接不住。
        # 留空 = 默认 grant（工具层抢 6 秒窗口自动点「允许」）。
        task = state.get("task") or {}
        # ★ 这一次 act 是不是"为了验证某个点"而做的？
        #
        # 用户要的东西：「为了验证个点我执行了多少步。每一步都记录。」
        #
        # clicks.jsonl 的 step 字段要能区分两种身份：
        #   · 被测流程的步骤（如 s4）—— 原用例的一部分
        #   · 验证动作（如 a13）—— 为了看清 a13 临时做的
        # 记成 a13 之后，事后按验证点一筛就知道"为了验它我动了几次"。
        _fix_task = state.get("assert_fix_task") or {}
        _trace_step = (str(_fix_task.get("assert_id") or "")
                       if _fix_task else _step_id(state))
        res = dev.act(int(action["next_x"]), int(action["next_y"]),
                      why=str(action.get("why", "")),
                      expect=action.get("expect"),
                      perm_action=task.get("perm_action"),
                      perm=task.get("perm_name"),
                      step=_trace_step)
    except ToolError as exc:
        # 动作可能已执行 -> 不能盲目重试；直接如实记录并停下
        return {"issues": [f"act 失败：{exc}"],
                "action": {**action, "acted": False, "error": str(exc)}}

    # 用 act 的返回刷新画面（不额外 observe）
    updates: dict[str, Any] = {
        "action": {**action, "acted": True,
                   "event_ids": res.event_ids,
                   "checkpoint": res.checkpoint},
        "last_activity": res.activity,
        "last_event_ids": res.event_ids,
        # 动作后的前台包名 —— 用来区分"系统权限框"和"App 自己的界面/弹窗"
        "last_package": getattr(res, "package", "") or "",
        "obs_clickable": res.next or state.get("obs_clickable", []),
        # act 返回的 next 也带上文案；没有就沿用上一次的（别把已知文案清空）
        "obs_nodes_text": _screen_text({"next": res.next})
                           or state.get("obs_nodes_text", ""),
        # act 后设备会重新观察并落盘，新的 XML 路径在这里
        "obs_xml_path": getattr(res, "xml_path", "") or state.get("obs_xml_path", ""),
        "obs_fresh": bool(res.next),          # 画面是否来自 act 返回
        # ★★★ 证据池要同时收「动作前」和「动作后」两屏 ★★★
        #
        #   原来只收 res.next（动作**之后**那一屏）。于是"点之前屏幕上有什么"
        #   从来没被留档 —— 而这个系统恰恰是靠"过程中出现过"来判断言的。
        #
        #   真机实测（用例 03，run#30）踩得很清楚：清数据后首次启动弹出的
        #   「日历需要使用以下权限」说明框，执行器明明看见了（看得见按钮，
        #   才点得中 button1），可它是在动作**之前**那一屏上；点完框就没了，
        #   res.next 里当然没有它。末尾判 a1~a4 时全是"没见过该控件/没出现该文案"
        #   —— 结论判成 FAIL，但产品行为其实完全正确（弹框文案、5 个权限申请
        #   都对得上）。**假失败**比漏判更糟：它会让人去修一个不存在的问题。
        #
        #   动作前的画面就在 state 里（决策就是基于它做的），直接用：
        #     · obs_clickable    —— 可点项，补 seen_rids / seen_texts
        #     · obs_nodes_text   —— 整屏文字（含不可点标签），补 seen_screens
        #       「必要权限」「可选权限」这类分组标题是不可点的 TextView，
        #       只会出现在整屏文字里，收 obs_clickable 是收不到的。
        #
        #   这几个字段都是 operator.add 累加，返回两个列表就是拼接，不会覆盖。
        #
        #   ★ 必须把 nodes 也喂进去：很多被断言的控件是不可点的值节点
        #     （178 的 tv_lesson_duration 等），只收可点项就永远攒不到，
        #     三条断言全报 `rid ✘` —— 那是假失败（见 _seen_rids 说明）。
        #
        #   ⚠️ ActResult 上没有 `nodes` 字段（只有 next / xml_path），
        #     所以 `res.nodes` 会 AttributeError（实测崩过一次）。
        #     用 getattr 取，取不到就是空 —— 后面的 rounds 里
        #     `obs_nodes` 会从 act_node 自己的 out（含 nodes）补上。
        "seen_rids": (_seen_rids(state.get("obs_clickable"),
                                 state.get("obs_nodes"))
                      + _seen_rids(res.next, getattr(res, "nodes", None))),
        "seen_texts": (_seen_texts(state.get("obs_clickable"),
                                   state.get("obs_nodes"))
                       + _seen_texts(res.next, getattr(res, "nodes", None))),
        "seen_screens": [s for s in (state.get("obs_nodes_text"),
                                     _screen_text({"next": res.next})) if s],
        # ★ 动作之后的那一屏也按步留档（覆盖本步上一条）。
        #
        # 断言的语义是"这一步**做完之后**屏幕上该有什么"，所以动作之后的
        # 画面才是它该看的。只存 observe 那一份的话，一步里最后一个动作
        # 造成的界面变化就进不了快照 —— 那正是断言最该看的那一屏。
        #
        # 注意 ActResult 没有 nodes / shot（只有 next / xml_path）：
        # 截图路径由 xml_path 同目录推出（落盘时 dump.xml 与 shot.png 同放一处）。
        "step_evidence": _step_evidence_of(state, {
            "activity": res.activity,
            "package": getattr(res, "package", "") or "",
            "xml_path": getattr(res, "xml_path", "") or "",
            "next": res.next,
            "nodes": getattr(res, "nodes", None),
        }),
        "round_in_step": state.get("round_in_step", 0) + 1,
        "visited": [f"{state.get('last_activity')} -> {res.activity}"],
        # 轨迹：记下这一步实际点了哪个控件 —— 回写 YAML 用。
        # 优先用强证据层给的 _rid；模型自己点的，按坐标反查当前可点列表拿 rid/text。
        #
        # ★ by 必须记：只有代码直点的动作才配回写进 YAML。
        #   模型摸索出来的点击是**猜测**，把它固化成"经验"会让下次更错
        #   （168 实测：模型在 s1 里点了 s0 遗留的「同意」按钮，
        #    回写就把 s1 的 iv_more 改成了 rid=button1）。
        "action_trace": [{
            # ★ 补证据的动作：归到断言绑定的那一步，并标 kind=verify。
            #
            # 用户：「B 这也是一步隐形的流程步骤。」
            # 所以它既进 action_trace（回写时能还原出"为看它点了什么"），
            # 又不混进被测流程的正常步骤里 —— 靠 kind 区分。
            "step": str((state.get("assert_fix_task") or {}).get("bind_step")
                        or _step_id(state)),
            "kind": "verify" if state.get("assert_fix_task") else "",
            "by": "code" if action.get("_strong") else "model",
            "action": _trace_of_tap(
                action, state.get("obs_clickable") or [])}],
        "issues": ([] if not res.error else [f"act 报错：{res.error}"]),
        "progress": [_tick(
            "act",
            f"点 ({action['next_x']},{action['next_y']}) {action.get('_rid') or ''}"
            f" -> {res.activity}"
            + (f"｜{len(res.next)} 个可点" if res.next else "｜无返回画面"),
            step=_step_id(state))],
    }

    # ── 副作用留痕：授权 / 弹窗 ──────────────────────────────────
    # 模型自己声明这一步的性质，代码照实记账（不替它猜）。
    # 为什么必须记：自动点"同意"改了设备权限状态，是**不可逆**的现场变更。
    # 不记的话，事后没人说得清"这条用例授权了什么"，下次跑的起跑线也不同。
    rec_common = {
        "step": state.get("step_index", 0),
        "x": int(action["next_x"]), "y": int(action["next_y"]),
        "rid": action.get("_rid") or "",
        "why": str(action.get("why", ""))[:300],
        "event_ids": [str(e) for e in (res.event_ids or [])],
    }
    if action.get("grants_permission"):
        updates["permissions_granted"] = [{
            **rec_common,
            "permission": action.get("permission_name") or "（模型未指明）",
            "decision": action.get("permission_decision") or "allow",
        }]
    # ── 运行时权限留痕 ──────────────────────────────────────────
    # 这是**工具层自动做的**（act.py 内置 perm.handle，默认 grant），
    # 不是模型决定的 —— 系统权限框约 6 秒不点就自动消失，
    # 而"该同意还是拒绝"要读用例（慢），所以必须由工具层抢时间。
    #
    # 责任划分（别再搞混）：
    #   · App 自绘的权限说明页/提示框 = **App 流程的一部分**，由模型按流程点掉
    #   · Android 运行时权限框（GrantPermissionsActivity）= 工具层自动点 + 这里记账
    #
    # 为什么要记账：自动点"允许"是不可逆的现场变更。
    # 实测一次冷启动动作就授予了 5 个权限（通知/读日历/读联系人/设备信息/写删日历），
    # 事后必须能回答"这条用例到底授予了哪几个"，否则下次起跑线不明。
    perm = getattr(res, "permission", None) or {}
    if perm.get("detected"):
        granted = []
        # ★ 权限归属：这是**谁的**权限框。
        #
        # 为什么必须有（真机踩过，169）：用例预期"点拍照导入应弹相机权限弹窗"，
        # 实际弹的是**日历自己的**「日历正在尝试打开摄像头」。两者都叫"相机权限"，
        # 但归属完全不同 —— 日历的是被测 App 的（要验），相机 App 的不是。
        # 不记归属，事后只看 prompts 那几个字根本分不出是谁弹的，
        # 就会出现"权限明明弹了、也允许了、相机也打开了，却判 FAIL"。
        owner = str(perm.get("owner_package") or "")
        for c in perm.get("clicks") or []:
            granted.append({
                "step": state.get("step_index", 0),
                "button": c.get("text") or "",
                "rid": c.get("rid") or "",
                # ★ 不记坐标（用户：「坐标不要了。没意义。」）
                #
                # 这里的 x/y 只是**这次在这台机器上**的位置：换设备、换分辨率、
                # 换系统版本全都不成立。留着容易被人误当成"可以照着点"，
                # 反而比不记更危险。真正跨设备成立的是「授了哪个权限」
                # （下面的 permission / owner_package），那个留着。
                "action": perm.get("action") or "grant",
                "action_source": perm.get("action_source") or "default",
                "event_ids": [str(e) for e in (res.event_ids or [])],
                # 申请方包名。和被测 App 包名一比就知道是不是被测 App 的权限。
                #
                # ⚠️ 用 task["package"] 而不是 state["app_package"]：
                #    后者是 start_check 才写进 state 的，如果权限框在这之前出现
                #    （168 实测：冷启动第一个动作就弹），state 里还是空的，
                #    于是**每一条都判成 is_sut=False** —— 包括明明是日历自己的权限。
                "owner_package": owner,
                "is_sut": bool(owner) and owner == str(
                    (state.get("task") or {}).get("package")
                    or state.get("app_package") or ""),
            })
        updates["runtime_permissions"] = granted
        # ★ 权限授予状态变了 -> 立刻重新查一次。
        #
        # 为什么在这里查：刚点完「允许」，此刻查最准。
        # 断言在流程末尾才判，那时设备早已翻页，再查会拿到"后续又被改了"的状态。
        # 而且这正是用户要的语义 ——「同意了才能打开相机」，
        # 判的是**授权结果**，不是"屏幕上弹过什么框"。
        _sut = str(state.get("app_package") or "")
        if _sut:
            snap = _granted_permissions(dev, _sut)
            if snap:
                updates["granted_permissions"] = snap
        # ★ 按权限类别核实"到底授予成功了没有"。
        #
        # 用户指出的关键差异：
        #   「普通运行权限不给可能某个功能会有影响。
        #     相机、图片这种，同意了才能进行下一步。」
        #
        # 所以不能一律"点过允许就算完"：
        #   · content（相机/照片/麦克风/定位）：没真授予 -> 流程必然卡死，
        #     必须当场记成一个**明确的阻断事实**，让后面的落点判定和断言看得见。
        #   · normal（通知/日历/设备信息）：没授予也能继续走，
        #     记账即可，不升级成问题（否则满是噪音告警，真问题会被淹没）。
        _snap = updates.get("granted_permissions") or {}
        _prompts = [p for p in (perm.get("prompts") or []) if p]
        for _p in _prompts:
            _kind = _perm_kind(_p)
            _ok = _perm_is_granted(_snap, _p)
            if _kind != "content":
                continue
            if _ok is True:
                continue
            # ok is None -> 判不了，**不能**当成已授予
            updates.setdefault("issues", []).append(
                f"🚫 内容型权限未确认授予：「{_p[:40]}」"
                + ("（已授予）" if _ok else
                   "（dumpsys 显示未授予）" if _ok is False else
                   "（dumpsys 未能确认）")
                + " —— 相机/照片这类不授予，下一步必然进不去")
            updates["perm_blocked"] = {
                "step": state.get("step_index", 0),
                "permission": _p, "kind": _kind, "granted": _ok,
            }
        # 弹窗上写的权限名（"日历正在尝试显示通知" → 通知），一并留档
        if perm.get("prompts"):
            updates.setdefault("runtime_permission_prompts", []).extend(
                [{"step": state.get("step_index", 0), "prompt": p}
                 for p in perm["prompts"]])
        if perm.get("unmatched"):
            # ★ 权限框还在，但工具层找不到**能安全点**的按钮 → 必须停下重决策。
            #
            # 真实场景（用户指出的）：第二次申请照片/相机权限时，弹窗多出
            # 「拒绝并不再询问」和「允许访问所选照片」——这两个工具层都**故意不点**
            # （点前者会设 don't-ask-again，让后续所有"授予"分支永远不再弹窗；
            #  点后者只是部分授权，语义不对）。
            # 于是两个按钮都在、却没一个能自动点。
            #
            # 早先这里只 issues.append 记一笔就继续往下点 —— 而权限框**还挡在屏幕上**，
            # 后续点击全落在框上（真机实测连点 scrollView 12 次不动，白等 80 秒）。
            # 所以这里返回一个"重决策"信号，让 decide 重新面对这个框，
            # 并把可见按钮和 unmatched 标记一起交给模型，由它按用例判断该走哪个分支。
            updates.setdefault("issues", []).append(
                f"⚠️ 权限弹窗无匹配按钮，交回决策："
                f"可见 {perm.get('buttons')}，activity={perm.get('activity')}")
            updates["perm_unmatched"] = {
                "step": state.get("step_index", 0),
                "buttons": perm.get("buttons") or [],
                "prompts": perm.get("prompts") or [],
                "activity": perm.get("activity"),
            }

    if action.get("is_dialog"):
        text = str(action.get("dialog_text") or "")
        rec = {**rec_common, "text": text}
        # ★ 只给**系统权限弹窗**打标记，App 自己的弹窗不做特殊处理。
        #
        # 用户的判断（决定了这里的写法）：
        #   「App 的权限页不会自己主动消失，就不用特殊处理了。
        #     我们只处理系统权限弹窗。」
        #
        # 道理：App 的权限页是**业务流程的一部分** —— 它停在那儿等用户决策，
        # 模型照常按用例走就行，代码不需要替它做什么。
        # 而系统权限框是**外部插入**的，会突然盖在业务界面上、
        # 且包名表明它不是被测 App，必须识别出来单独处理，否则会被误当成
        # 业务界面（168 轮07 就是这么判错的）。
        #
        # 判定用包名，由代码给 —— 这是"能完全确定的交给代码"。
        if is_system_permission_pkg(state.get("last_package")):
            rec["is_system_permission"] = True
            rec["package"] = state.get("last_package") or ""
            updates.setdefault("issues", []).append(
                f"🔐 点掉了**系统权限弹窗**（包名 {rec['package']}）"
                f"「{text[:40]}」")
        # ★ 兜底：模型把"授权"归成了普通弹窗时，代码补一条提醒。
        # 实测踩到：日历自绘的「访问文件相关信息（日历需要使用以下权限）」页，
        # 按钮是「同意」，模型按"不是系统框"没申报 grants_permission ——
        # 分类上没错，但报告里就看不到"授权了什么"，审计会漏。
        # 这里不看分类，只看**文字里有没有权限字样**，宁多记不少记。
        if not action.get("grants_permission"):
            why_text = str(action.get("why", "")) + text
            if any(k in why_text for k in ("权限", "同意", "允许", "授权")):
                rec["permission_suspect"] = True
                updates.setdefault("issues", []).append(
                    f"⚠️ 疑似授权未申报：弹窗「{text[:40]}」"
                    f"（why=「{str(action.get('why',''))[:40]}」），"
                    "模型未置 grants_permission=true，请人工复核")
        updates["dialogs_dismissed"] = [rec]
    return updates






def _vision_failed(state: ChildState, action: dict[str, Any],
                   why: str) -> dict[str, Any]:
    """视觉失败的统一出口：计数 + 把"失败"这件事说给模型听。

    ⚠️ 保留 vision_question 是有意的：让它再试一次（网络抖动很常见）。
       但**计数**在涨，达到 VISION_MAX_FAILURES 就会被上面摘掉。
    """
    n = int(state.get("vision_failures") or 0) + 1
    return {
        "action": {**action, "vision": None, "vision_error": why},
        "vision_failures": n,
        "vision_error": why,
        "issues": [f"vision 失败（第 {n}/{VISION_MAX_FAILURES} 次）：{why}"],
        "progress": [_tick("vision", f"看图失败 {n} 次：{why[:60]}")],
    }


def is_deterministic_landing(expr: str) -> tuple[bool, str]:
    """这条落点判据，**代码能不能唯一确定**？返回 (能?, 为什么)。

    ★ 用户定的分工原则（原话）：
      「能完全确定的可以让代码判断。不能完全确定的让AI来判断。
        完全确定的比如点击这个按钮跳转到一个Activity这种。就直接走代码了。
        其他有异议的还是靠AI来处理。」

    为什么必须区分：
      · `activity=.XxxActivity` —— "跳到这个页面了"，**唯一确定**，无歧义。
        代码判得又快又准，交 AI 是浪费。
      · `rid=btnXxx`      —— "这个控件在不在"，**唯一确定**。
      · `text=课程表`      —— 代码只能做**子串包含**。屏幕上有这三个字
        不等于到了"课程表页"（可能只是别的页面有个同名条目）。
        **判得出来，但判不准** —— 这种结论比"判不了"更危险，
        因为它带着代码的权威感。所以交 AI。

    这也决定了临时弹窗的处理边界：
      弹窗**不是判据**。activity 判据满足了就走，中途出现过什么弹窗不影响结论；
      判据不满足了才让 AI 看屏幕 —— 那时它才会发现"哦，被弹窗挡住了"。
    """
    expr = (expr or "").strip()
    if not expr:
        return False, "无判据"

    # OR / AND 里只要有一个非确定项，整体就交给 AI ——
    # 因为代码无法判断"非确定项到底成没成立"，合出来的结论就不可信。
    seps = "|" if "|" in expr else ("," if "," in expr else "")
    parts = [p for p in expr.split(seps) if p.strip()] if seps else [expr]

    weak: list[str] = []
    strong: list[str] = []
    for cond in parts:
        c = cond.strip()
        if not c or "=" not in c:
            weak.append(c or "?")
            continue
        key = c.split("=", 1)[0].strip().lower()
        if key in ("activity", "rid", "r", "package", "pkg"):
            strong.append(key)
        else:
            weak.append(key)

    if weak:
        return False, (f"含代码无法唯一确定的判据（{','.join(weak)}）—— "
                       f"交给 AI 判断更可靠")
    return True, f"仅含确定判据（{','.join(strong)}）"


_CHECKPOINT_MOD: Any = None


def _skill_roots() -> list[str]:
    """候选的 skill 根目录，按可信度递减。

    ⚠️ 不能只依赖 `_ctx()`：`match_landing` / `_pathmap_expect` 是
    **纯函数**，单测和 dev_probe 里都没有运行上下文。早先只读
    `_ctx().skill_root`，结果这两处在测试里永远返回空 ——
    而且**不报错**，看起来就像"路径图里没有这条边"，极难发现。

    顺序：运行上下文 → 环境变量 → 本仓库自带 assets。
    """
    import os

    out: list[str] = []
    try:
        out.append(str(_ctx().skill_root))
    except Exception:  # noqa: BLE001
        pass
    env_root = os.environ.get("ANDROID_SKILL_ROOT")
    if env_root:
        out.append(env_root)
    # 仓库自带的 assets（唯一正主）。路径统一由 cases.paths 算 ——
    # 早先这里写 `Path(__file__).parent / "assets"`，文件挪进 agent/ 包以后
    # 会指向 src/android_agent/agent/assets（不存在），
    # 而且是**静默**加进候选列表，只在真的走到那条分支时才炸。
    from ..cases.paths import REPO_ROOT
    out.append(str(REPO_ROOT / "assets"))
    return out


def _checkpoint():
    """惰性加载工具层的 `checkpoint` 模块。

    ⚠️ 不能写成模块级 `import checkpoint` —— `checkpoint.py` 在
    `assets/tools/` 下，那个目录**不在 sys.path 里**（见本文件上面
    关于 perm.py 的注释：那样会让所有测试 import 失败）。

    但落点判定**必须**用工具层那一份，不能自己再写一遍：
      · 它的 `evaluate` 按**段落边界**匹配 activity，
        而手写的 `activity.endswith(want)` 会让
        `EditTimetableActivity` 误判成命中 `TimetableActivity`
        —— 在错误的页面上静默通过，比不校验更危险。
      · 它的 `detect_transient` 只认白名单，白名单外交回 AI 判断，
        正是用户定的「能完全确定的让代码判断，不能确定让 AI 判断」。

    所以：按需把 tools 目录临时塞进 sys.path 加载一次，加载完就撤。
    """
    global _CHECKPOINT_MOD
    if _CHECKPOINT_MOD is not None:
        return _CHECKPOINT_MOD
    import importlib

    candidates = [str(Path(r) / "tools") for r in _skill_roots()]
    tools_dir = next((c for c in candidates if Path(c, "checkpoint.py").is_file()), "")
    if not tools_dir:
        raise ImportError("找不到工具层的 checkpoint.py")
    # ⚠️ 目录要**留在 sys.path 里**：`pathmap` 自己 `import checkpoint`，
    #    撤掉目录会让它 import 失败（连坐）。
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    _CHECKPOINT_MOD = importlib.import_module("checkpoint")
    return _CHECKPOINT_MOD


def match_landing(expr: str, *, activity: str = "",
                  clickable: list[dict[str, Any]] | None = None,
                  nodes_text: str = "",
                  state_pkg: str = "",
                  sig: list[str] | None = None) -> tuple[bool, str]:
    """判落点是否命中。返回 (命中?, 说明)。

    ★ 直接委托给工具层的 `checkpoint.evaluate` —— 不再自己实现一套。

    以前这里是手写的解析（按逗号/竖线切、逐条比对），注释还写着
    "按工具层同样的语义实现"。那就是重复造轮子，而且造错了：
    旧实现用 `activity.endswith(want)`，会把 `EditTimetableActivity`
    误判成命中 `TimetableActivity` —— 检查点在**错误的页面**上静默通过。

    现在补上 `sig`（工具层 observe/act 返回的页面签名）后，
    rid/desc/text 三类判据也交给工具层，语义只有一份。
    """
    expr = (expr or "").strip()
    if not expr:
        return False, "无落点判据"

    try:
        cp = _checkpoint()
    except Exception:  # noqa: BLE001
        # 工具层不可用（单测环境/目录缺失）-> 退回"判不了"，
        # 绝不假装命中，也不假装未命中。
        return False, f"落点判不了（工具层 checkpoint 加载失败）：{expr}"

    # 把 clickable 也折进签名：工具层的 sig 是 xml 里所有 rid/text/desc，
    # 而调用方手里只有精简后的 next —— 两者合并才不会漏判。
    merged: list[str] = list(sig or [])
    seen = set(merged)
    for n in (clickable or []):
        for kind, key in (("rid", "r"), ("rid", "rid"),
                          ("text", "t"), ("text", "text"),
                          ("desc", "d"), ("desc", "desc")):
            v = str(n.get(key) or "").split("/")[-1]
            tag = f"{kind}={v}"
            if v and tag not in seen:
                seen.add(tag)
                merged.append(tag)
    # ⚠️ `nodes_text` 是**整屏裸文本**（一行一条），不是 `kind=value` 签名。
    #    直接塞进 sig 的话 `text=完成` 这种判据永远匹配不上 ——
    #    工具层的 sig 里文字是带 `text=` 前缀的。
    #    同时保留裸行：老调用方（如 test_locate_retry）依赖"文案能在屏上找到"。
    for line in (nodes_text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        for tag in (s, f"text={s}"):
            if tag not in seen:
                seen.add(tag)
                merged.append(tag)

    ck = cp.evaluate(cp.parse_expect(expr), activity=activity, sig=merged,
                     package=state_pkg)
    if not ck.get("checked"):
        return False, f"无落点判据：{expr}"
    if ck.get("ok"):
        return True, "命中：" + "、".join(ck.get("matched") or []) or "命中"
    return False, "未命中：" + "；".join(ck.get("missed") or [expr])


def resolve_locate(locate: str, *, clickable: list[dict[str, Any]] | None,
                   nodes_text: str = "") -> tuple[dict[str, Any] | None, str]:
    """把一条定位表达式解析成"点哪个控件"。返回 (命中项, 怎么找到的)。

    这是**纯回放能扛住版本差异**的关键（用户原话：
    「因为有些版本 rid 有变化。所以才会由这个考虑」）。

    找法按可靠性递减：
        rid   —— YAML 首选，精确
        text  —— rid 变了但文案没变（最常见的情况）
        desc  —— 无障碍描述（图标按钮常有，如"更多选项"）
        label —— 同上，不同工具层字段名

    ⚠️ 注意"rid 找不到但文案能找到"这件事**必须被允许**：
    iv_more 这类图标按钮本来就没文案，而菜单项反过来只有文案没有 rid ——
    只用 rid 会在版本升级时整条用例报废。
    但**找到了要说清是怎么找到的**，否则事后分不清"按 YAML 走的"还是"猜的"。
    """
    expr = (locate or "").strip()
    if not expr or expr.startswith("x="):
        return None, ""
    # 拆 key=value（--expect 语法，逗号=AND，这里只取第一个定位键）
    key, _, want = expr.partition("=")
    key = key.strip().lower()
    want = want.strip().strip('"\'')
    if not want:
        return None, ""

    items = clickable or []

    def _find(k: str) -> dict[str, Any] | None:
        for n in items:
            v = str(n.get(k) or "")
            if v and v == want:
                return n
        return None

    def _find_sub(k: str) -> dict[str, Any] | None:
        """子串匹配 —— 文案常被工具层截断或加后缀。"""
        for n in items:
            v = str(n.get(k) or "")
            if v and want in v:
                return n
        return None

    # ① 按 YAML 写的判据找（首选）
    if key in ("rid", "r"):
        hit = _find("r") or _find("rid")
        if hit:
            return hit, "rid"
    elif key in ("text", "t"):
        hit = _find("t") or _find("text") or _find_sub("t")
        if hit:
            return hit, "text"
    elif key in ("desc", "d"):
        hit = _find("d") or _find("desc") or _find_sub("d")
        if hit:
            return hit, "desc"

    # ② rid 找不到 -> 用"想点的那个东西的文案"兜底。
    #    YAML 的 locate 只有 rid 时，desc/desc 里往往藏着人话（如 s1 的 desc
    #    写着「点右上角『更多』」）—— 但那是句子不是控件文案，不能直接拿来匹配。
    #    所以这里只做一件事：如果 rid 是 **图标类**（常见命名）
    #    -> 该位置的可点项可能带 desc，逐个比对 known 别名。
    if key in ("rid", "r"):
        ALIASES = {
            "iv_more": ("更多", "更多选项", "more"),
            "iv_back": ("返回", "转到上一层级", "back"),
            "iv_add": ("添加", "新建", "add"),
            "iv_search": ("搜索", "search"),
            "iv_close": ("关闭", "close"),
        }
        for alias in ALIASES.get(want, ()):
            hit = _find_sub("d") or _find_sub("t")
            if hit:
                return hit, f"别名({alias})"
            for n in items:
                if alias in str(n.get("d") or "") or alias in str(n.get("t") or ""):
                    return n, f"别名({alias})"
        # 整屏文案里出现过这个 rid 对应的控件名？当做线索记下，但不据此点击
        if want and want in nodes_text:
            return None, f"仅在整屏文案里见到 {want}"

    return None, ""


def target_present(locate: str, *, clickable: list[dict[str, Any]] | None = None,
                   nodes_text: str = "") -> bool | None:
    """本步声明的定位目标(`locate`)**当前屏**在不在？

    返回：
        True  = 目标在，模型可以直接点
        False = 目标不在 —— 需要"换目标找"（有界重试），或按 on_fail 停
        None  = 没声明 locate，无从判断（不参与决策）

    ⚠️ 为什么要这个：用例 YAML 反复强调一条事故 ——
        「目标 rid 当前屏不存在 → 点击落在空处，activity 不变，**无任何报错**」
        （会话454：停在时间设置页时点 iv_more，点空，静默跑偏）
        「rid 是判据，坐标是注释。」

    但**不能**像用例 YAML 那样"不在就立即停"——很多控件本来就没 rid。
    所以这里只负责**如实报告在不在**，要不要停由调用方按次数决定。
    """
    locate = (locate or "").strip()
    if not locate:
        return None
    # 复用落点判据的同一套匹配语义，避免两处规则不一致
    hit, _why = match_landing(locate, activity="",
                              clickable=clickable, nodes_text=nodes_text)
    return hit








class AssertVerdict(BaseModel):
    """断言判定结果。

    ⚠️ `passed` 必须是 `bool | None`，不能是 `bool`。
    模型有时会回 `"passed": null`（真机上遇到过：它想说"我没看清"），
    严格 bool 会直接抛
        ValidationError: Input should be a valid boolean [input_value=None]
    整个 assert 节点崩掉、整条用例作废 —— 而它其实只是"没看清"，
    应该走 unknown 通道（上层会转成 BLOCKED 或交给 vision），不该崩进程。
    """
    passed: bool | None = Field(
        default=None,
        description="true=通过，false=不通过，null=看不清/无法判断（不要猜）")
    actual: str = Field(description="实际现象，必须具体")
    event_id: str = Field(default="", description="证据事件 ID")
    # ★★ 这两个字段必须声明，否则会被 pydantic **静默丢掉**。
    #
    # 178 实测踩到，这是"报错信息是空的"的真正原因：
    #   runner._answer 在模型回复解析失败时返回
    #       {"passed": None, "actual": "...", "unknown": True, "error": "..."}
    #   但 interrupt 带了 response_schema=AssertVerdict，
    #   LangGraph 会按这个 schema **校验并转换**，没声明的键直接丢弃。
    #   于是 assert_verdict_node 里 `v.get("unknown")` 永远是 None、
    #   `v.get("error")` 永远是 None —— 结果就是：
    #     · 判不出来这件事**根本传递不上来**（unknown 丢了）
    #     · issues 里印出空括号「判定缺失：模型回复无法解析（）」
    #   排查时看到空括号完全无从下手，就是这个原因。
    #
    # 教训：凡是 runner 要往回带的状态，**必须在 schema 里声明**。
    # 声明了才留得住，不声明就等于没传。
    unknown: bool = Field(
        default=False, description="模型回复解析失败/无法判定（不是 PASS 也不是 FAIL）")
    error: str = Field(default="", description="解析失败的具体原因，用于排查")
    # 复核轮标记（见 assert_node / runner）：代码说不成立、这轮是让 AI 复核
    ai_recheck: bool = Field(
        default=False, description="本轮是否属于「代码判定失败 -> 交 AI 复核」")
    code_why: str = Field(default="", description="代码判定不成立的原话")
    # 计算成本相关的注解，模型可以不填
    evidence: str = Field(default="", description="判断依据")
    confidence: str = Field(default="", description="high/medium/low")
    note: str = Field(default="", description="补充说明")
    event_id_corrected: str = Field(default="", description="evidence id 纠正记录")
    # ★★★ 证据不足时，请执行器补一步（用户要求）★★★
    #
    # 场景（a13 实测）：预期「能调背景色，共 10 种」，但色块面板没展开，
    # 只看到一行「课程背景色」。判定只能给 null —— 而 null 只会变成
    # BLOCKED，报告里就是"这条没验"。用户的原话：
    #   「检查 10 个色块。条件不满足了。那就退回执行增加个步骤呗」
    #
    # 所以判定可以**回话**：告诉执行器我还要看什么、为什么现在看不到、
    # 屏幕上哪些元素可能相关。执行器补一步动作，回来重判。
    #
    # 形状（结构化而不是一句话，让执行器能直接用）：
    #   {"verify_target": "要核验什么",
    #    "why_insufficient": "为什么现在判不了",
    #    "related_elements": [{"rid": "llCourseColor", "text": "课程背景色"}],
    #    "close_with": "取消"}
    #
    # `close_with` 是"点开看完怎么关回去"（用户的要求）：
    #   「因为背景色也是个弹窗呀。你验证完要继续推倒上一步啊。
    #     不然继续不下去了。我说了要像个人类测试员一样」
    #   人类点开色板看完会点「取消」退回编辑弹窗再继续。不关的话，
    #   下一条断言拿到的是色板 —— 验的根本不是一回事。
    #
    # 空 dict / 没有该键 = 不需要补证据（正常路径）。
    need_action: dict[str, Any] = Field(
        default_factory=dict,
        description="证据不足时请求补动作：要验什么/为什么不够/相关元素")


def _steps_done(state: ChildState) -> int:
    """真正走完的步数 —— **放弃的步不算**。

    ⚠️ 这里曾经只判 kind=="step"，于是"放弃"的步骤也被计入完成，
    闸门 (steps_done < len(steps)) 失效：
    模型一步没走到、明确放弃，最后却能报 PASS。
    本用例测试死循环时撞出来的（give_up -> 步骤被算完成 -> PASS）。
    """
    return len([f for f in state.get("findings", [])
                if f.get("kind") == "step" and f.get("status") != "give_up"])






# ══════════════════════════════════════════════════════════════════════
# 结构化判据
# ══════════════════════════════════════════════════════════════════════
#
# 用例 YAML 里的 check 是**结构化的**，不是自然语言字符串：
#     check:
#       activity: ".timetable.display.TimetableActivity"
#       node_text: {rid: toolbar_title, equals: "课程表"}
#       node_present: {rid: btnImportFromGallery}
#       dialog_present: {rid: button1, text: "知道了"}
#       package: "com.android.providers.media.module"
#
# 这几种判据**在代码里判死** —— 不能丢给模型自由发挥
# （那就等于没用 YAML）。
#
# 实现原则：
#   · 能代码判的，代码判（activity / node_present / package / node_text）
#   · 判不了的（需要看图的，如"按钮可点"），返回 None 交回模型
#   · 判据里出现**不认识的键**，必须显式报错 ——
#     用例 YAML 自己写着「两者对不上时不会报错，只会静默跑偏」

KNOWN_CHECKS = (
    "activity", "activity_changed_to", "package",
    "node_present", "node_absent", "rid_present",
    "node_text", "text_present",
    "dialog_present", "dialog_present_on_tap", "dialog_on_tap",
    "nodes_present",
    # ★ 嵌套：after_tap 表示"点了之后应出现的现象"。
    # 168 的 a2 就是这个形状：
    #     node_present: {rid: btnImportFromGallery}
    #     after_tap: {dialog_present: {rid: button1, text: "知道了"}}
    # 它是**同一批证据**（点击带回的画面）上的子判据，所以就地展开判，
    # 不假装"点击前后"分得开 —— 我们本来也只留了点击后的画面。
    "after_tap",
    # ★ permission: {"camera": true} / {"READ_MEDIA_IMAGES": "granted"} ——
    # "被测 App 有没有申请/被授予某权限"。
    #
    # 为什么必须有（真机踩过，169）：用例预期是
    #   「点拍照导入应弹相机权限弹窗，允许后能打开相机」
    # 但 YAML 里只能写成 dialog_present:{permission_dialog:"CAMERA"} ——
    # **代码不认识这个形状，静默丢弃**，于是那条断言从来没验过。
    # 实际执行时日历的权限框确实弹了、也允许了、相机也打开了，
    # 但报告拿不出任何"权限弹了"的证据，只能判 FAIL。
    #
    # 权限名和包名都是**唯一确定**的，符合"确定的给代码判"的原则。
    "permission",
)
# 明确**不判**的键（注解性质，不是判据）
ANNOTATION_CHECKS = (
    "note", "measured", "supporting", "computed", "placeholder",
    "reason", "evidence", "case_expect", "desc",
)


def parse_granted_permissions(dumpsys_text: str) -> dict[str, bool]:
    """从 `dumpsys package <pkg>` 里解析出**运行时权限**的授予状态。

    只看运行时权限段，形如：
        runtime permissions:
          android.permission.CAMERA: granted=true, flags=[USER_SET]

    ⚠️ 不要扫全文：`requested permissions:` 段里也有权限名，但那里没有
       granted 标记，混进来会把"声明了"当成"已授予"。
       （169 就踩过：一度以为日历没声明 CAMERA，其实声明了且已授予。）
    """
    out: dict[str, bool] = {}
    in_rt = False
    for raw in (dumpsys_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith(("runtime permissions:", "install permissions:",
                           "requested permissions:")):
            in_rt = low.startswith(("runtime permissions:", "install permissions:"))
            continue
        if not in_rt:
            continue
        m = re.match(r"^([\w.]+):\s*granted=(true|false)", line)
        if m:
            out[m.group(1)] = (m.group(2) == "true")
    return out


def _granted_permissions(dev: Any, package: str) -> dict[str, bool]:
    """查被测 App 当前被授予了哪些运行时权限。查不到返回空 dict。

    为什么判授权状态而不判弹窗 —— 用户原话：
        「同意了才能打开相机。只要打开就行了。
          哪怕打开后相机APP权限。那不是我们这个case关心的。」
    弹窗只是过程，授权才是结果。
    """
    try:
        text = dev.shell(f"dumpsys package {package}")
    except Exception:  # noqa: BLE001
        return {}
    return parse_granted_permissions(text)


def _perm_kind(permission: str) -> str:
    """把权限分成两类 —— 两类的失败后果完全不同，处置必须分开。

    用户明确指出的差异：
        「普通运行权限不给可能某个功能会有影响。
          相机、图片这种，同意了才能进行下一步。」

    ★ content（内容型）：相机 / 照片视频 / 麦克风 / 定位
        不授予 = **流程根本走不下去**。点「从图库导入」没照片权限，
        图片选择器永远不出现。
        -> 必须验"真的授予了"，不能只看"点过允许按钮"。

    ★ normal（普通型）：通知 / 日历读写 / 设备信息等
        不授予只是某些能力受限，主流程照常能走。
        -> 默认同意即可，失败不阻断，如实记账。

    ⚠️ 存储按 Android 版本归属不同：13+ 照片走 READ_MEDIA_IMAGES（内容型），
       老版本走 READ_EXTERNAL_STORAGE。这里一并按内容型处理 ——
       宁可多验一个，不要漏验（漏验的代价是流程静默卡死）。
    """
    p = (permission or "").upper()
    content_keys = ("CAMERA", "MICROPHONE", "RECORD_AUDIO",
                    "READ_MEDIA", "MEDIA_IMAGES", "MEDIA_VIDEO",
                    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE",
                    "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
                    "READ_CONTACTS", "WRITE_CONTACTS",
                    "ACCESS_MEDIA_LOCATION", "BODY_SENSORS")
    return "content" if any(k in p for k in content_keys) else "normal"


def _perm_is_granted(granted: dict[str, bool], permission: str) -> bool | None:
    """这次申请到底授予成功没有？True/False；判不了返回 None。

    ⚠️ None 是**合法结果**，不是失败 —— dumpsys 取不到、权限名对不上都算。
       这时绝不能默认成"已授予"，那就是"判不了 = 已完成"。
    """
    if not granted or not permission:
        return None
    key = permission.strip()
    if key in granted:
        return granted[key]
    for name, ok in granted.items():
        if key.upper() in name.upper() or name.upper() in key.upper():
            return ok
    return None


# 系统权限弹窗的包名判断 —— 复用 tools_bridge 已有的实现。
#
# ★ 判归属**只看包名**，不看文案。用户原话：
#   「你要区分APP弹窗和系统权限弹窗。系统权限弹窗的包名是固定的。
#     APP弹窗的包名就是当前APP自己。」
#
# 曾经用"权限名 → 文案特征词"去猜归属，是错的：相机 App 弹的
# 「相机正在尝试使用相机」里也有"相机"二字，会被当成被测 App 的申请。
# 包名唯一确定，文案不是。
#
# ⚠️ 不要在这里 `from perm import ...` —— perm.py 在 assets/tools 下，
#    不在 sys.path 里，那样会让**所有**测试 import 失败（真踩过）。


def evaluate_check(check: Any, *, state: ChildState) -> tuple[bool | None, str]:
    """判一条结构化 check。返回 (结论, 说明)。

    结论：True=成立 / False=不成立 / None=代码判不了（交回模型）

    ★★ 已停用（用户决定：「chek现在除了很多问题先去掉」）★★

    判据这套东西在真机上反复出错：
      · a4 判 iv_add_hint "不在"，实际就在（判据和真实界面脱节）
      · a13 要数色块，判据根本写不出来
      · 同 Activity 多 Fragment 时，activity 判据完全失效
    而且它判错时会**盖过 AI 的结论**（见下面的"判定纠正"），
    于是错得更理直气壮。

    现在断言一律交 AI 现场判断。这个函数留着但**永远返回"判不了"**，
    调用方那套"代码纠正 AI"的逻辑自然失效，不用拆。

    ⚠️ 别在这里重新启用——除非有一套在真机上验证过、不会误判的判据。
    """




def build_learned_case(state: ChildState) -> dict[str, Any]:
    """把这一轮的实际情况**回写成一份更好的用例 JSON**（用户的要求）。

    用户原话：
      「要回写 JSON。高质量的 JSON 对重新跑有意义」
      「实际步骤。坑的步骤就不要了。但总结成为了验证这个点或者走到验证这一步
        碰到的坑。或者知识卡查询到的知识。」
      「最后生成 JSON 的时候可以把无用的步骤剔除。如果有坑归纳到这一步的坑里。
        这样才能稳定吧」

    ## 产出什么
    一份和输入同构、但**带着答案**的用例：
      · steps   —— 只留**真正走通**的步骤；没走通/没必要的剔掉
      · asserts —— 每条挂上
          verify_action  这次是怎么看见它的（"点开课程背景色再返回"）
          pitfalls       走/验这个点时踩的坑（下次直接避开）
          knowledge      查到的知识（下次不用再查）

    ## 为什么不用模型来写
    这些都是**已经发生过的事实**，代码全都有（action_trace / findings /
    issues / knowledge_used）。让模型再复述一遍只会引入编造 ——
    用户反复强调过「不要自动聪明」。所以这里只做**归纳**，不做生成。

    ## 什么算"无用的步骤"
    判据只有一条：**这一步没有任何证据表明它推进过**。
    （状态 give_up、或从头到尾没有一次成功的动作。）
    注意它和"这一屏验证失败"是两回事：验证失败是产品的结论，
    步骤本身是有效的，必须留着 —— 剔掉会让下次重跑漏掉这一段。
    """
    task = state.get("task") or {}
    findings = state.get("findings") or []
    issues = [str(i) for i in (state.get("issues") or [])]
    trace = state.get("action_trace") or []

    # --- 每条断言的实际结果 ---
    res_of: dict[str, dict[str, Any]] = {}
    for f in findings:
        if f.get("kind") == "assert":
            res_of[str(f.get("id"))] = f

    # --- 哪些步骤真的走通过 ---
    # 放弃的步骤一定算没走通；其余看有没有成功的动作记录。
    gave_up = {str(f.get("id")) for f in findings
               if f.get("kind") == "step" and f.get("status") == "give_up"}
    acted: dict[str, list[dict[str, Any]]] = {}
    verify_of: dict[str, list[dict[str, Any]]] = {}
    for t in trace:
        sid = str(t.get("step") or "")
        if not sid:
            continue
        # ★ 补证据的动作（kind=verify）单独放一支：
        #   用户：「B 这也是一步隐形的流程步骤。」——它要进 steps，
        #   但必须和被测流程的动作区分开，否则下次重跑会把它当被测步骤执行。
        if str(t.get("kind") or "") == "verify":
            verify_of.setdefault(sid, []).append(t.get("action") or {})
        else:
            acted.setdefault(sid, []).append(t.get("action") or {})

    steps_out: list[dict[str, Any]] = []

    # ══════════════════════════════════════════════════════════════════
    # setup 不在这里算
    # ══════════════════════════════════════════════════════════════════
    #
    # 用户原话：「我意思这种固定的步骤那就转成setup就行了。」
    #
    # 冷启动那几下固定的清障（168：用户协议「同意」→ 课程表引导
    # 「我知道了」）必须从 steps 里挪出去，否则 s1.actions 会变成
    # [button1, 允许, 我知道了, iv_more, item_title] 一大坨
    # —— 清弹窗、进菜单、选课程表三件事混在一条里，下次照着点必错。
    #
    # 但这里算不出来：`action_trace` 里的动作是 `_trace_of_tap` 压过的，
    # **只有 rid/text、没有坐标**；而 `dialogs_dismissed` 里的弹窗
    # rid 又常常是空的（168 实测 `"rid": ""`）。两边对不上。
    #
    # 只有点击账本同时有 rid 和坐标，所以这件事交给
    # `_split_setup_from_steps` 在 learn 节点里做（那里能读文件）。

    for s in (task.get("steps") or []):
        sid = str(s.get("id") or "")
        # 只有"验证动作"没有正常动作的步骤不算推进过（如 s0 被放弃）
        if sid in gave_up or not acted.get(sid):
            continue                     # 没推进过的步骤：剔掉
        item: dict[str, Any] = {"id": sid, "desc": s.get("desc") or ""}
        if s.get("locate"):
            item["locate"] = s["locate"]
        if s.get("landing"):
            item["landing"] = s["landing"]
        # 实际动作用结构化记录替换（比原来的描述更贴近真相）
        item["actions"] = acted[sid]
        # ★ 隐形流程：为了验证而额外点的东西（如点开色板）。下次直接照做。
        if verify_of.get(sid):
            item["verify_actions"] = verify_of[sid]
        # 这一步专属的坑：从 issues 里挑提到这一步的
        mine = [i for i in issues
                if sid in i or f"步骤 {sid}" in i or f"{sid} " in i]
        if mine:
            item["pitfalls"] = mine[:6]
        steps_out.append(item)

    # --- 每条断言的"怎么看见的" ---
    #
    # 用户：「为了验证个点我执行了多少步。每一步都记录。」
    #
    # 所以断言上挂的是**一串步骤**，不是一句概述：
    #   a13
    #     ├─ 第1步 tap rid=llCourseColor（为什么：色板没展开）
    #     │        截图 shots/step30/shot.png
    #     │        XML  shots/step30/dump.xml
    #     └─ 第2步 重判 → 看到 10 个色块
    # 每步都带截图和 XML 路径，事后可以逐个核对。
    va_of: dict[str, list[dict[str, Any]]] = {}
    for v in (state.get("verify_actions") or []):
        va_of.setdefault(str(v.get("assert_id") or ""), []).append(v)

    # 点击账本里那些"为验证而做"的动作（clicks.jsonl 的 step 记的是断言 id）
    click_of: dict[str, list[dict[str, Any]]] = {}
    for t in trace:
        sid = str(t.get("step") or "")
        if str(t.get("kind") or "") == "verify" and sid:
            click_of.setdefault(sid, []).append(t)

    # --- 断言：挂上验证步骤 / 坑 / 知识 ---
    asserts_out: list[dict[str, Any]] = []
    for a in (task.get("asserts") or []):
        aid = str(a.get("id") or "")
        r = res_of.get(aid) or {}
        item: dict[str, Any] = {
            "id": aid,
            "desc": a.get("desc") or "",
            "step": a.get("step") or "",
            "result": r.get("status") or "",
        }
        if a.get("check"):
            item["check"] = a["check"]

        # ★ 为了验证它，实际执行了几步、每步做了什么、证据在哪
        _va = va_of.get(aid) or []
        if _va:
            vsteps: list[dict[str, Any]] = []
            for n, v in enumerate(_va, 1):
                s: dict[str, Any] = {
                    "n": n,
                    "why": v.get("why_insufficient") or "",
                    "open": v.get("open") or "",
                }
                if v.get("close"):
                    s["close"] = v["close"]
                if v.get("shot"):
                    s["shot"] = v["shot"]
                if v.get("xml"):
                    s["xml"] = v["xml"]
                vsteps.append(s)
            item["verify_steps"] = vsteps
            item["verify_count"] = len(vsteps)
        elif a.get("verify_action"):
            item["verify_steps"] = [a["verify_action"]]

        if a.get("pitfalls"):
            item["pitfalls"] = list(a["pitfalls"])
        if r.get("actual"):
            item["actual"] = str(r.get("actual"))[:400]
        asserts_out.append(item)

    return {
        "case_id": task.get("case_id"),
        "title": task.get("title"),
        "package": task.get("package"),
        "status": state.get("status") or "",
        # ★ 这份回写的 JSON 能不能直接拿去重跑。
        # 用户定的（B 方案）：BLOCKED 也生成，只是标成不可用 ——
        # 生成是为了让人看"跑到哪儿断了"，标 usable=false 是为了
        # 别让下一次把它当可靠用例直接复用。
        "usable": (state.get("status") or "") == "PASS",
        # setup 由 _split_setup_from_steps 在 learn 节点里补上
        # （要读点击账本才知道哪些点击是"点掉弹窗"）。
        "steps": steps_out,
        "asserts": asserts_out,
        "knowledge": [k for k in (state.get("knowledge_used") or [])][:8],
    }


def _base_learn_node(state: ChildState) -> dict[str, Any]:
    """在 finish **之前**把这一轮的经验回写成 JSON。

    放在 finish 之前（而不是之后）是因为 finish 之后 state 就不再往前传了，
    拿不到 action_trace / knowledge_used 这些东西。

    产出写进 run 目录的 case_learned.json，同时写回用例库
    （用户要的「高质量的 JSON 对重新跑有意义」—— 下次直接读它）。
    """
    try:
        learned = build_learned_case(state)
    except Exception as exc:  # noqa: BLE001
        # 回写失败**不能**影响结论 —— 用例跑完的结果比这份副产物重要得多
        return {"issues": [f"回写用例失败：{exc}"]}
    out: dict[str, Any] = {}
    try:
        ctx = _ctx()
        # 放在 report.json 旁边 —— 那里就是本次 run 的目录
        run_dir = Path(ctx.report_path).parent
        # ★ 从点击账本把"每一步的截图/XML 路径"补进来。
        #
        # 用户：「为了验证个点我执行了多少步。每一步都记录。」
        # 点击账本（clicks.jsonl）里每条都有 shot_before/shot_after、
        # xml_before/xml_after —— 这是最可靠的原始记录，直接读它，
        # 不指望 assert 那一刻还记得住路径。
        _fill_evidence_from_ledger(learned, run_dir / "clicks.jsonl")
        # ★ 把冷启动清障那几下从 steps 挪进 setup（原地改）。
        #   用户：「我意思这种固定的步骤那就转成setup就行了。」
        _split_setup_from_steps(learned, run_dir / "clicks.jsonl",
                                state.get("dialogs_dismissed") or [])
        # ★ 回灌 target：把这次**能稳定定位**的控件写成下次的 target。
        #
        # 用户：「这个回灌应该是每个 case 跑完后总结的时候自己就做了」
        # 没有这一步，跑出来的知识下次用不上，代码点击率永远是 0。
        #
        # ⚠️ **必须在 _split_setup_from_steps 之后**。
        #    顺序反了会踩坑：拆 setup 之前 actions 里还混着清弹窗的动作，
        #    回灌就会把「允许」（系统权限框的按钮）当成 s1 的入口。
        #    168 实测踩到：s1.target 成了 rid=permission_allow_button_two，
        #    下次照着点就会去点权限框，而不是点「更多」。
        _backfill_targets(learned, run_dir / "clicks.jsonl")
        # ★ 把这次见到的 setup 动作累加进库。
        #
        # 用户的问题：「APP 如果有临时的弹窗，正好被我们捕获了。
        #              下次没有了会怎么样」
        # 用户定的：「多次稳定的记录下来。不稳定的可以让 AI 直接介入」
        #
        # 所以这里**只是记数**，不决定用不用 —— 见够了（≥2 次）才会
        # 出现在 get_setup_actions(stable_only=True) 里，被 prep 拿去直接执行。
        # 只撞见一次的临时弹窗永远停在 1，下次照样问 AI。
        _ck = str((state.get("task") or {}).get("case_id") or "")
        if _ck and learned.get("setup"):
            try:
                from ..reporting import store as _store
                _n_setup = _store.save_setup_actions(_ck, learned["setup"])
                if _n_setup:
                    print(f"  [learn] 累加 setup 记忆 {_n_setup} 条"
                          f"（见够 {_store.SETUP_STABLE_TIMES} 次才直接执行）",
                          file=sys.stderr)
                # ★ JSON 里的 setup 以**库里的稳定项**为准，不是"这次见到的"。
                #
                # 用户定的：「多次稳定的记录下来。不稳定的可以让 AI 直接介入」
                # 所以只出现过一次的临时弹窗**不该出现在这份 JSON 里** ——
                # 它是下一次要拿去执行的东西，写进一次性的动作等于埋雷。
                #
                # 踩到过：第 3 次跑时 button1 是 prep 阶段点的，不在步骤账本里，
                # 于是按"这次见到的"算出来的 setup 只剩 timetable_guide_button ——
                # 跟库里积累的规律对不上。JSON 是给人看/给下次用的成果物，
                # 应该反映积累下来的结论，而不是某一次执行路径的副产品。
                _stable = _store.get_setup_actions(_ck)
                learned["setup"] = [
                    ({**({"rid": r["rid"]} if r.get("rid")
                         else {"text": r.get("text") or ""}),
                      # 组合判据：下次照着它点，不会误点同名的别的按钮
                      **({"sig": r["sig"]} if r.get("sig") else {}),
                      "times": r.get("seen_times"),
                      "pkg": r.get("pkg") or ""})
                    for r in _stable
                ]
            except Exception as exc:  # noqa: BLE001
                # 记不住不该坏了整次回写，更不该影响用例结论
                out.setdefault("issues", []).append(f"setup 记忆未落库：{exc}")
        path = run_dir / "case_learned.json"
        path.write_text(json.dumps(learned, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        out["learned_path"] = str(path)
        _n = sum(int(a.get("verify_count") or 0)
                 for a in (learned.get("asserts") or []))
        print(f"  [learn] 回写用例 -> {path.name}"
              f"（步骤 {len(learned.get('steps') or [])}，"
              f"断言 {len(learned.get('asserts') or [])}，"
              f"验证动作 {_n} 次）", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        out["issues"] = [f"回写文件失败：{exc}"]
    out["learned"] = learned
    return out


def _fill_evidence_from_ledger(learned: dict[str, Any], ledger: Path) -> None:
    """把 clicks.jsonl 里为验证而做的动作，补进各断言的 verify_steps。

    账本里 `step` 字段写成断言 id（如 a13）的就是验证动作 ——
    这是 act_node 刻意打的标记（见那里的注释）。
    每条带 shot_before/shot_after 和 xml_before/xml_after，
    取"动作之后"那一份：那才是看见了新东西的那一屏。
    """
    if not ledger.exists():
        return
    by_assert: dict[str, list[dict[str, Any]]] = {}
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = str(o.get("step") or "")
            if not sid.startswith("a"):
                continue
            tgt = o.get("target") or {}
            by_assert.setdefault(sid, []).append({
                "action": o.get("action") or "",
                "target": tgt.get("rid") or tgt.get("text") or "",
                "why": str(o.get("why") or "")[:200],
                "changed": bool(o.get("changed")),
                "shot": o.get("shot_after") or o.get("shot_before") or "",
                "xml": o.get("xml_after") or o.get("xml_before") or "",
            })
    except OSError:
        return
    for a in (learned.get("asserts") or []):
        got = by_assert.get(str(a.get("id") or ""))
        if not got:
            continue
        # 账本是"实际做了什么"，比断言阶段记的意图更可信：以它为准，
        # 但保留意图里的 why（账本的 why 已经带上了模型说的理由）。
        a["verify_steps"] = got
        a["verify_count"] = len(got)


def _sig_from_xml(xml_path: str, xy: tuple[int, int] | None) -> str:
    """从落盘 XML 里，按点击坐标找到那个节点，生成**组合判据**字符串。

    返回 `xpath=//*[@resource-id='android:id/button1'][@text='知道了']`
    这样的表达式；查不到就返回空串。

    ## 为什么用组合 xpath 而不是 rid=（用户提的）

    用户原话：「能否用 xpath 那种方式。多个组合。rid 和 text 那种是不是更稳点」

    对，稳得多。踩到的具体问题：

      `android:id/button1` 是 Android 的**公共按钮 id**，
      用户协议的「同意」、弹窗的「知道了」、别的 App 的对话框，
      全都叫这个名字（实测同一个用例里就有 2 处）。

      只写 `rid=button1` 命中的是「第几个叫 button1 的」，
      换个界面就可能点到完全不同的东西。

      组合写法则命中「那个写着「知道了」的 button1」——
      **这才是我们真正想表达的意思**，换设备、换分辨率都不受影响。

    ## 为什么要用全名

    账本里存的是末段（`button1`），全名（`android:id/button1`）得回 XML 查。
    差异很关键：全名自带命名空间，System 的 button1 和 App 自己的 button1
    天然分开，误点风险小一大截。

    ## 为什么按坐标找节点

    账本的 `target.c` 就是这一下的点击中心，XML 里 bounds 包含它的就是那个节点。
    比按 rid 反查更准 —— 同一 rid 可能有多个（比如 item_title 有 4 个）。
    """
    if not xml_path or xy is None:
        return ""
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(xml_path).getroot()
    except Exception:  # noqa: BLE001
        return ""
    x, y = int(xy[0]), int(xy[1])
    for el in root.iter():
        b = str(el.get("bounds") or "")
        if "[" not in b or "]" not in b:
            continue
        try:
            a, z = b.split("][")
            x1, y1 = [int(v) for v in a.strip("[").split(",")]
            x2, y2 = [int(v) for v in z.strip("]").split(",")]
        except (ValueError, AttributeError):
            continue
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        # 命中第一个包含该点的节点 —— 从外到内遍历，最外层先命中，
        # 但我们要的是**可点的那个**，所以优先返回 clickable 的。
        full = str(el.get("resource-id") or "").strip()
        txt = str(el.get("text") or "").strip()
        if str(el.get("clickable", "")).lower() != "true":
            continue
        # 有 rid 就带上（全名，不是末段）
        parts = []
        if full:
            parts.append(f"@resource-id='{full}'")
        if txt:
            parts.append(f"@text='{txt}'")
        if not parts:
            continue
        return "xpath=//*[" + "][".join(parts) + "]"
    return ""


def _split_setup_from_steps(learned: dict[str, Any], ledger: Path,
                            dialogs: list[dict[str, Any]]) -> None:
    """把冷启动那几下固定清障从 steps 里挪进 setup（原地改 learned）。

    用户原话：「我意思这种固定的步骤那就转成setup就行了。」
              「系统权限就不管了。你放上反而有印象。」

    ## 为什么在这里做，不在 build_learned_case 里

    `action_trace` 存的动作是 `_trace_of_tap` 压出来的，**只有 rid/text、
    没有坐标**（坐标故意不留 —— 换设备必失效）。而 `dialogs_dismissed`
    里点掉的那些弹窗 rid 又常常是空的（168 实测 `"rid": ""`）。
    两边都缺东西，只有**点击账本**同时有"点了哪个 rid"和"点在哪个坐标"。

    ## 判据

    账本某一条点击，若它的坐标命中 `dialogs_dismissed` 里
    **step==0 且非系统权限**的记录 —— 那就是冷启动清障，归 setup：

      · step==0 之外的不动：s2 点完「从图库导入」弹的「知道了」是流程
        的一部分，必须留在 s2。
      · 系统权限框不进 setup：它是工具层自动点掉的（见 nodes 里那条
        责任划分）。写进用例等于告诉后来人"这里要手动点允许"，是错的
        印象 —— 用户明确说了「系统权限就不管了，你放上反而有印象」。

    ## 剔除按坐标，不按 rid

    踩过：setup 里是 button1「同意」，而 s2 里那个「知道了」rid 也是
    button1。按 rid 剔会把 s2 流程必需的动作一起删掉，下次重跑 s2
    就少了关键一步。所以必须按坐标判 —— 这也正是非读账本不可的原因。
    """
    if not learned or not ledger.exists():
        return

    # 第一屏上、App 自己的那些弹窗的坐标
    want: set[tuple[int, int]] = set()
    # 系统权限框的坐标 —— 工具层自动点的，**哪里都不要出现**。
    # 用户：「系统权限就不管了。你放上反而有印象。」
    sys_pos: set[tuple[int, int]] = set()
    for d in dialogs or []:
        try:
            _p = (int(d.get("x")), int(d.get("y")))
        except (TypeError, ValueError):
            continue
        if d.get("is_system_permission"):
            sys_pos.add(_p)
            continue
        if int(d.get("step") or 0) != 0:
            continue
        try:
            want.add((int(d.get("x")), int(d.get("y"))))
        except (TypeError, ValueError):
            continue
    if not want:
        return

    # 扫账本：setup 序列 + 每个 step 自己还剩哪些动作（按坐标剔除）
    setup: list[dict[str, Any]] = []
    setup_pos: set[tuple[int, int]] = set()
    per_step: dict[str, list[dict[str, Any]]] = {}
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            tgt = o.get("target") or {}
            # 坐标在 target["c"]（形如 [1795, 1583]），不是 action ——
            # action 只是个字符串 "click"。
            _c = tgt.get("c")
            pos: tuple[int, int] | None = None
            if isinstance(_c, (list, tuple)) and len(_c) >= 2:
                try:
                    pos = (int(_c[0]), int(_c[1]))
                except (TypeError, ValueError):
                    pos = None
            rid = str(tgt.get("rid") or "").strip()
            one: dict[str, Any] = {"rid": rid} if rid else {
                "text": str(tgt.get("text") or "")}

            # 系统权限框：工具层自动点的，用例里不体现 —— 用户
            # 「系统权限就不管了。你放上反而有印象。」
            if pos is not None and pos in sys_pos:
                continue

            if pos is not None and pos in want:
                setup_pos.add(pos)
                if one not in setup:
                    # pkg 记"这一下发生在谁的界面上"（点击前的 activity）。
                    # 落库时用它区分"被测 App 自己的流程"和"外来干扰"。
                    #
                    # ⚠️ 账本里的 before 是**完整 activity 名**
                    #    （com.zui.calendar.AllInOneActivity），不含 "/"，
                    #    所以不能 split("/") 取包名 —— 会连类名一起留下。
                    #    这里原样存 activity，读取侧用 startswith 比前缀。
                    one["pkg"] = str(o.get("before") or "")
                    # ★ 优先用组合 xpath（rid 全名 + text），比裸 rid 稳得多。
                    #   查不到就退回 rid 末段 —— 有总比没有强，但会标出来。
                    _sig = _sig_from_xml(str(o.get("xml_before") or ""), pos)
                    if _sig:
                        one["sig"] = _sig
                    setup.append(one)
                continue          # 归了 setup，不再算进任何步骤

            sid = str(o.get("step") or "")
            if not sid or sid.startswith("a"):
                continue          # 验证动作用的 "aNN"，不算流程动作
            if one not in per_step.setdefault(sid, []):
                per_step[sid].append(one)
    except OSError:
        return

    if not setup:
        return

    # 用账本的结论覆盖 steps 的 actions：
    # 账本记的是"真点了什么、点在哪"，比 action_trace 压出来的 rid 序列
    # 更能分辨"同 rid 不同位置"的动作。
    for st in (learned.get("steps") or []):
        sid = str(st.get("id") or "")
        if sid in per_step:
            st["actions"] = per_step[sid]
    learned["setup"] = setup


def _backfill_targets(learned: dict[str, Any], ledger: Path) -> None:
    """把"这一步**能稳定定位**的那个控件"回灌成 target（原地改 learned）。

    用户原话：
      「我理解我们努力这么久。不就是想提供控件的稳定性。
        下次同样的 case 跑的时候能少问 AI」
      「这个回灌应该是每个 case 跑完后总结的时候自己就做了」

    ## 为什么必须有这一步

    跑完只在 case_learned.json 里记下"这次点了哪个 rid"，**下次跑读不到** ——
    输入用例里没有 target，代码没依据可算，于是每一步照样问 AI。
    实测就是代码点击率 0%：知识躺在上一轮的产物里，闭环没接上。

    ## 什么样的控件才配当 target

    不是所有点过的控件都能回灌，判据是**它下次还能不能被唯一定位**：

      ✅ 写了：本屏 XML 里 rid 只出现 **1 次**，且 **clickable=true**
      ❌ 不写：命中多个（168 的 item_title 就有 4 个同名：课程表/纵览/
              年视图/设置）、或 clickable=false（文字挂在不可点的 TextView 上，
              真正能点的是外层容器，而外层没有任何标识）

    168 实测印证：iv_more / btnImportFromGallery / timetable_guide_button
    都是"唯一 + 可点"，能直点；item_title 4 个同名且全不可点，
    **换成 text=课程表 也找不到**（文字在不可点节点上），只能交给 AI。

    ## 为什么还要排除坐标

    没有 rid/text 的控件只剩坐标，而坐标**换设备/换分辨率必失效**。
    写进用例等于埋雷 —— 所以这里宁可留空让 AI 判断，也不写坐标。

    ## 选哪一个

    一步可能有多个动作（先展开菜单、再选条目），只有**第一个**
    可定位的适合当 target：它是这一步的入口。后面的动作属于
    "展开之后才知道要选什么"，交给 AI 或 verify_actions 更合适。

    ⚠️ 已经有 target 的不覆盖 —— 用例作者写的比我们推断的可信。
    """
    if not learned or not ledger.exists():
        return
    # 账本里按 (step, rid) 找到点它之前的 XML
    xml_of: dict[tuple[str, str], str] = {}
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = str(o.get("step") or "")
            if not sid or sid.startswith("a"):
                continue
            rid = str((o.get("target") or {}).get("rid") or "").strip()
            xml = str(o.get("xml_before") or "")
            if rid and xml:
                xml_of.setdefault((sid, rid), xml)
    except OSError:
        return
    if not xml_of:
        return

    def _unique_clickable(xml_path: str, rid: str) -> bool:
        """这个 rid 在本屏是不是"唯一 + 可点"。读不懂就当不能。"""
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(xml_path).getroot()
        except Exception:  # noqa: BLE001
            return False
        total = clickable = 0
        for el in root.iter():
            full = str(el.get("resource-id") or "")
            if full.split("/")[-1] != rid:
                continue
            total += 1
            if str(el.get("clickable", "")).lower() == "true":
                clickable += 1
        return total == 1 and clickable == 1

    def _combo_sig(xml_path: str, rid: str) -> str:
        """给这个 rid 生成**组合 xpath**（rid 全名 + text），拿不到就退回 rid=。

        用户：「能否用 xpath 那种方式。多个组合。rid 和 text 那种是不是更稳点」

        稳在哪：`android:id/button1` 是 Android 公共按钮 id，
        「同意」「知道了」都叫这个。只按末段 rid 点，换一屏就可能点到
        完全不同的按钮；组合判据要求 rid 和 text **同时**满足，
        点的就是"那个写着「知道了」的 button1"。

        全名（android:id/button1）必须回 XML 查 —— 账本里只有末段。
        """
        try:
            import xml.etree.ElementTree as ET
            root = ET.parse(xml_path).getroot()
        except Exception:  # noqa: BLE001
            return f"rid={rid}"
        for el in root.iter():
            full = str(el.get("resource-id") or "")
            if full.split("/")[-1] != rid:
                continue
            if str(el.get("clickable", "")).lower() != "true":
                continue
            txt = str(el.get("text") or "").strip()
            if full and txt:
                # 引号要转义：text 里可能有单引号
                t = txt.replace("'", "&apos;")
                return f"xpath=//*[@resource-id='{full}'][@text='{t}']"
            if full:
                return f"xpath=//*[@resource-id='{full}']"
            break
        return f"rid={rid}"

    for st in (learned.get("steps") or []):
        if st.get("target"):
            continue                      # 作者写了就不动
        sid = str(st.get("id") or "")
        for act in (st.get("actions") or []):
            rid = str(act.get("rid") or "").strip()
            if not rid:
                continue                  # 没有 rid 的（只有 text）回灌不了结构
            xml = xml_of.get((sid, rid))
            if not xml or not _unique_clickable(xml, rid):
                continue                  # 定位不住：留给 AI，不写死
            st["target"] = _combo_sig(xml, rid)
            break                         # 只用第一个可定位的当入口


def _base_finish_child_node(state: ChildState) -> dict[str, Any]:
    """子 Agent 收尾：写 finish 并生成回报给主 Agent 的汇总。

    ⚠️ 结论判定必须把**步骤落点未达成**算进去。
    曾经的 bug：3 步全部落点不符（steps_done=0），却仍报 PASS ——
    步骤没走到目标界面，后面的"预期通过"根本不是在测该测的东西。
    """
    ctx = _ctx()
    dev = ctx.device
    task = state["task"]
    asserts = task["asserts"]
    steps = task["steps"]
    got = [f for f in state.get("findings", []) if f.get("kind") == "assert"]
    issues = list(state.get("issues") or [])
    steps_done = _steps_done(state)

    landing_miss = [i for i in issues if "落点不符" in i]
    gave_up = [f for f in state.get("findings", [])
               if f.get("kind") == "step" and f.get("status") == "give_up"]
    blocked = state.get("status") == "BLOCKED"

    if blocked:
        status = "BLOCKED"
    elif gave_up:
        # 明确放弃 = 走不下去，不是"通过"。必须先于 steps_done 判，
        # 否则放弃的步骤会在计数里消失、被误当成"走完"。
        status = "BLOCKED"
        issues.append(f"{len(gave_up)} 步被放弃（走不到目标界面）")
    elif steps_done < len(steps):
        # 步骤没走完 -> 前置不满足，结论不可信（不是 PASS，也不是 FAIL）
        status = "BLOCKED"
        issues.append(f"步骤 {len(steps)} 步只完成 {steps_done} 步")
    elif landing_miss:
        # ★ 落点不符 = 关键路径没找到 = 用例没执行完 -> BLOCKED，不是 FAIL。
        #
        # 用户定的语义（原话）：
        #   BLOCKED 表示这个 Case 没执行完 —— 崩了、或者关键路径没找到。
        #   FAIL    表示所有路径都执行完了，只是结果和预期有差别。
        #
        # 落点不符恰恰是"没走到该去的地方"：后面那些步骤和断言
        # 都是在错误的前置上做的，结论没有意义，所以归 BLOCKED。
        # （plan 里试满 LANDING_MAX_TRIES 次就会直接 BLOCKED 并停下，
        #   这里兜的是别的路径漏进来的、或被模型重新判过的情况。）
        status = "BLOCKED"
        issues.append(f"{len(landing_miss)} 步落点不符，关键路径没找到，"
                      f"用例未执行完")
    elif len(got) < len(asserts):
        status = "BLOCKED"
        issues.append(f"预期 {len(asserts)} 条，只验到 {len(got)} 条")
    elif any(f["status"] == "BLOCKED" for f in got):
        # ★★ 有断言记成 BLOCKED（"判定缺失"）时，整条用例**不是 PASS**。
        #
        # 178 实测踩到的：a2/a3/a4 三条因为视觉模型返回空文本 -> 判定缺失，
        # 状态记 BLOCKED，可**上面这两条判断都拦不住**：
        #   · len(got) == len(asserts)  —— 三条都"写进去了"
        #   · any(status == "FAIL")     —— 它们是 BLOCKED，不是 FAIL
        # 于是走到 else 分支，总结论写成 **PASS**，
        # summary 还写着「预期 6/6 已验证」——
        # 实际是 3 条代码判过、3 条压根没判。
        #
        # 这比直接报 FAIL 更危险：**它让没验的东西看起来验过了**。
        # 用户问"这个 case 到底过没过"，得到的是假的"过"。
        #
        # 归 BLOCKED 而不是 FAIL，理由和单条断言一致：
        # 我们没验，所以既不能替产品说"坏"，也不能说"好"。
        _n_blocked = len([f for f in got if f["status"] == "BLOCKED"])
        status = "BLOCKED"
        issues.append(
            f"{_n_blocked} 条预期未做出判定（判定缺失，不是功能问题）——"
            f"这些预期**没有验过**，不能算通过")
    elif any(f["status"] == "FAIL" for f in got):
        status = "FAIL"
    else:
        status = "PASS"

    _verified = len([f for f in got if f["status"] in ("PASS", "FAIL")])
    _blocked_n = len([f for f in got if f["status"] == "BLOCKED"])
    summary = state.get("summary") or (
        f"{task['title']}：步骤 {steps_done}/{len(steps)}，"
        f"预期 {_verified}/{len(asserts)} 已判定"
        + (f"（{_blocked_n} 条未判定）" if _blocked_n else ""))

    # ⚠️ env 类前提的恢复：**无论成败都要跑**，否则污染下一条用例。
    # 这一步放在 finish 里而不是正常流程里，就是为了保证异常路径也能执行到。
    try:
        restored = run_restore(state, issues)
    except Exception as exc:  # noqa: BLE001
        restored = []
        issues.append(f"⚠️ 恢复流程本身出错：{exc}")

    try:
        dev.finish(status, summary)
    except ToolError as exc:
        issues.append(f"finish 失败：{exc}")

    report = SubagentReport(
        case_id=task["case_id"], status=status, summary=summary,
        session_id=state.get("session_id"),
        findings=got, issues=issues,
        steps_done=steps_done,
        coverage_ok=(len(got) >= len(asserts) and steps_done >= len(steps)
                     and not landing_miss),
        preconditions_done=list(state.get("pc_done") or []),
        restored=restored,
        permissions_granted=list(state.get("permissions_granted") or []),
        dialogs_dismissed=list(state.get("dialogs_dismissed") or []),
        runtime_permissions=list(state.get("runtime_permissions") or []),
        runtime_permission_prompts=list(
            state.get("runtime_permission_prompts") or []),
        # ★ 进度轨迹 + 卡点（用户第 2 条：子 Agent 状态要能上报，别让主 Agent 干等）
        progress=list(state.get("progress") or []),
        stalled_at=_stalled_at(state, steps_done, len(steps)),
        elapsed_s=_elapsed_s(state),
        # ★ 模型耗时单独算一份（用户要求"模型得时间也要统计在内"）。
        #   只报总耗时看不出"慢在模型还是慢在设备"：decide 一次 5~20s，
        #   而一次点击只有 200ms。分开记才知道该优化哪头。
        model_s=_model_seconds(state),
        node_calls=dict(state.get("node_calls") or {}),
        case_confidence=dict(task.get("case_confidence") or {}),
        strong_stats=_strong_stats(state),
        # ★ 每步实际走的路径 —— 按步归并，回写 YAML 与人工核对都用它
        action_trace=_trace_by_step(state.get("action_trace") or []),
        # ★ 引擎来自 task（见 SubagentTask.engine 的注释：
        #   报告是这里写文件、run_case 再从文件读，事后赋值没用）
        engine=str(task.get("engine") or ""),
    ).model_dump()

    # 回写报告文件，主 Agent 直接读（不靠回传整段 context）
    out = Path(ctx.report_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return {"status": status, "summary": summary, "_report": report}


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------























def _foreground_owner(state: ChildState) -> tuple[str, str]:
    """当前前台是哪个包、哪个 activity。拿不到就返回空串（不猜）。"""
    return (str(state.get("last_package") or ""),
            str(state.get("last_activity") or ""))


def _is_sut(state: ChildState, pkg: str) -> bool:
    """这个包是不是**被测 App 自己**。

    强证据的适用范围就卡在这一个问题上（用户：
    「只有本 APP 的才用强证据。跳出去都是让 AI 判断吧」）。

    ## 为什么用包名判，而不是 activity 名

    包名是 App 的身份，稳定；activity 名是内部实现，会变。
    而且"是不是我们的 App"本来就该看身份 —— 同一个 activity 名
    在别的 App 里出现也不能算数。

    ## 判不了怎么办

    没有包名（还没观察到、dump 失败）→ 返回 False，即按"外部"处理。
    宁可多花一次模型调用，也不要在归属不明时硬判 ——
    硬判错了会去点一个不该点的地方，代价比多一次调用大得多。
    """
    app_pkg = str((state.get("task") or {}).get("package") or "").strip()
    if not app_pkg or not pkg:
        return False
    # 精确比包名，不做子串匹配：
    # com.zui.calendar 和 com.zui.calendar.widget 是两个 App，
    # 子串匹配会把后者误认成前者。
    return pkg.strip() == app_pkg




def _resolve_strong_target(target: str, state: ChildState) -> tuple[int | None, int | None, str]:
    """把 target 表达式解析成坐标。返回 (x, y, 怎么找到的/为什么找不到)。

    只认**结构**，不认坐标。可靠性递增：
        rid=xxx       —— 可点清单里精确匹配；必须唯一
        xpath=...     —— 落盘 XML 里算命中数；必须恰好 1 个
        text=/desc=   —— 可点清单里匹配；必须唯一

    ⚠️ "必须唯一" 是关键：命中多个时返回 None，让模型来定，
       绝不用"第一个"糊弄过去 —— 那就是"两个相同的找错"。
    """
    from ..device.direct import Screen

    expr = target.strip()
    key, _, want = expr.partition("=")
    key = key.strip().lower()
    want = want.strip().strip('"\'')

    clickable = state.get("obs_clickable") or []
    xml_path = state.get("obs_xml_path") or ""

    # -- xpath：从落盘 XML 算命中数 ------------------------------------
    if key == "xpath":
        if not xml_path:
            return None, None, "落盘 XML 路径缺失，无法执行 xpath 判据"
        try:
            scr = Screen(xml_path=xml_path, activity="", package="",
                         width=0, height=0, clickable=clickable)
            n = scr.xpath_count(want)
        except Exception as e:  # noqa: BLE001
            return None, None, f"xpath 执行失败：{e}"
        if n == 0:
            return None, None, f"目标不在当前屏：xpath 命中 0 个"
        if n > 1:
            return None, None, f"目标不唯一：xpath 命中 {n} 个，交给模型判断"
        c = scr.xpath_center(want)
        if not c:
            return None, None, "xpath 命中的节点没有有效坐标"
        return _check_in_screen(scr, c[0], c[1], "xpath 命中唯一")

    # -- rid / text / desc：从可点清单找，必须唯一 ----------------------
    if key in ("rid", "r"):
        field, label = "r", "rid"
    elif key in ("text", "t"):
        field, label = "t", "text"
    elif key in ("desc", "d"):
        field, label = "t", "desc"
    else:
        return None, None, f"target 表达式不认识：{target}"

    hits = [n for n in clickable if str(n.get(field) or "") == want]
    if not hits and label == "rid":
        # rid 常写成全名 com.xxx:id/iv_more，可点清单里只留末段
        want_tail = want.split("/")[-1]
        hits = [n for n in clickable if str(n.get("r") or "") == want_tail]
    if not hits and label == "rid" and "#" in want:
        # ★ 支持 rid=xxx#2 取同名的第 2 个（从 1 开始）。
        #
        # 为什么需要：空课程表有 25 个格子，rid 全都叫 cv_empty_content
        # （它们自己的 rid 是空的，按子节点借用来的名字），
        # 「点第一个小节」用 rid= 表达不出来。没有序号时命中 25 个 ->
        # 判定"不唯一" -> 交模型 -> 模型继续瞎点 -> 176 的 s4 空转。
        #
        # ⚠️ 只对 rid= 生效，且**必须带序号**才算数：
        #    不带序号的 rid= 仍然要求唯一，绝不默认取第一个。
        base, _, nth = want.partition("#")
        base = base.split("/")[-1]
        if nth.isdigit() and int(nth) >= 1:
            same = [n for n in clickable if str(n.get("r") or "") == base]
            k = int(nth) - 1
            if 0 <= k < len(same):
                hits = [same[k]]
                label = f"rid={base}#{nth}"
    if not hits:
        return None, None, f"目标不在当前屏：{label}={want} 没找到"
    if len(hits) > 1:
        return None, None, f"目标不唯一：{label}={want} 命中 {len(hits)} 个，交给模型判断"

    c = hits[0].get("c") or []
    if len(c) < 2:
        return None, None, f"{label}={want} 没有有效坐标"
    return _check_in_screen(state, int(c[0]), int(c[1]), f"{label} 命中唯一")


def _check_in_screen(state: ChildState | Any, x: int, y: int,
                     how: str) -> tuple[int | None, int | None, str]:
    """坐标必须落在屏幕内，否则拒绝直点。

    ## 这一条不是 176 的解法，别搞错

    176 点 37 次「完成」**不是**坐标越界 —— 实测 `save_view` 中心
    就是 (2913,202)，屏幕 3040x1904（横屏），坐标完全正确、点击也确实送达
    （每次 click 约 200ms 正常返回）。真正原因是**必填项没填**。

    那为什么还要留这一条？

    因为 `u2.window_size()` 返回的是 **(高, 宽)**：实测设备 1904x3040，
    它给 (3040, 1904)。这是个真实存在的顺序陷阱 —— 一旦有人当 (宽, 高) 用，
    "屏幕宽度"就成了 3040，屏幕外几十像素的坐标会被判成合法，
    点了没反应还查不出原因。设备横竖屏切换时尤其容易踩。

    所以这里是**防守性检查**，守的不是 176，而是将来不要重犯同一个错。
    "找得到"（唯一性）和"点得中"（坐标合法）是两条独立的保证。

    屏幕尺寸从落盘 XML 的 bounds 推（根节点没有 display_width/height，
    实测过），推不出来就放行 —— 不因量不到屏幕让所有步骤瘫掉。
    """
    sw, sh = _screen_size_from_state(state)
    if sw <= 0 or sh <= 0:
        # 拿不到尺寸就不挡：宁可放行，也不因"量不到屏幕"让所有步骤瘫掉
        return x, y, how
    if 0 <= x < sw and 0 <= y < sh:
        return x, y, how
    return None, None, (
        f"目标坐标 ({x},{y}) 在屏幕外（屏幕 {sw}x{sh}），拒绝直点 —— "
        f"控件坐标不可信，需要模型换一种定位方式")


def _screen_size_from_state(state: ChildState | Any) -> tuple[int, int]:
    """从落盘 XML 推算屏幕尺寸。取不到返回 (0, 0)。

    ⚠️ 不用 `u2.window_size()` —— 它返回的是 **(高, 宽)**，实测设备 1904x3040
    它给 (3040, 1904)。这个顺序陷阱真实存在（横竖屏切换时必踩），
    所以宁可自己从 XML 推，也不碰它一次。

    ⚠️ XML 根节点**没有** display_width/display_height（实测这台设备的
    dump 里根本没有这两个属性），所以退一步：**用所有节点的 bounds 推**。

    可行性依据（实测）：这份 XML 有 89 个 bounds，最大右下角正好是
    3040x1904 —— 也就是与控件坐标同一个坐标系下的屏幕尺寸。
    根布局一定是满屏的，所以最大值可靠。

    取不到就返回 (0,0)，调用方会放行（宁可放行，也不因量不到屏幕让全部步骤瘫掉）。

    参数既可以是 state dict，也可以直接是 Screen 对象
    （xpath 分支手上只有 Screen）。
    """
    xml_path = getattr(state, "xml_path", None) \
        if not isinstance(state, dict) else state.get("obs_xml_path")
    if not xml_path:
        return 0, 0
    try:
        import re as _re
        from pathlib import Path as _P

        text = _P(xml_path).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return 0, 0
    mx = my = 0
    for m in _re.finditer(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', text):
        mx = max(mx, int(m.group(3)))
        my = max(my, int(m.group(4)))
    if mx > 0 and my > 0:
        return mx, my
    return 0, 0


def _current_input_value(state: ChildState, target: str) -> str | None:
    """这个输入框**现在里面是什么**？不是输入框、或读不到就返回 None。

    只读真的能读到的东西：可点清单里该 rid 的 text。
    宁可不答（返回 None）也不要瞎猜 —— 猜错会把模型带偏。
    """
    want = target.partition("=")[2].strip().strip('"\'')
    if not want:
        return None
    tail = want.split("/")[-1]
    # 去掉 #n 序号（cv_empty_content#1 那种）
    base = tail.split("#")[0]
    for it in (state.get("obs_clickable") or []):
        # 可点项有两种键名：解析层给 r/t，中间层规范化后是 rid/text。
        # 只认一种就会漏读（176 里输入框的值就读不到，模型只能反复填）。
        rid = str(it.get("rid") or it.get("r") or "")
        if rid.split("#")[0] != base:
            continue
        txt = str(it.get("text") or it.get("t")
                  or it.get("desc") or it.get("d") or "").strip()
        if txt:
            return txt
    return None


def _strong_tries(state: ChildState, sid: str, target: str) -> int:
    """这一步的这个目标，**连续几次点了都没有进展**？

    强证据直点的退出判据就是它。返回值 >= STRONG_MAX_SAME_TARGET 时
    必须把这一步交给模型（见 strong_target_node 里的说明）。

    ## 数据来源：点击账本，不是 state 里的计数

    这是踩过坑之后的决定。state 里那个 `retry_landing` 计数只在
    "本步判定完成但落点不符"时累加 —— 而 176 每轮都是重新决策、
    压根谈不上完成，于是它**一次都没涨**，37 次点击它全程是 0。
    计数器的正确性取决于"每条路径都记得加"，这靠不住。

    账本不一样：`act()` 里点一次写一条，没有旁路可绕。

    ## 怎么算"一次都没进展"

    看账本的 `changed` 字段。它的语义是"界面有没有实质变化"：

        有的进展：activity 变了 / 可点控件清单变了 / 控件文字变了
        没进展：  以上全都没变

    从最后一条往前数，**连续没进展**的条数就是答案。中途有任何一条
    有进展，或点的是别的控件，就停 —— 说明执行路径已经不在原地打转了。

    ⚠️ 只数"这个目标"。点输入框、点别的按钮都不算进这个目标的账。
    """
    want = target.partition("=")[2].strip().strip('"\'')
    want_tail = want.split("/")[-1]

    try:
        dev = getattr(_ctx(), "device", None)
    except Exception:  # noqa: BLE001  没注入上下文（单测/离线）就当不知道
        return 0
    lg = getattr(dev, "ledger", None) if dev is not None else None
    if lg is None:
        return 0

    n = 0
    for row in reversed(lg.rows()):
        tgt = row.get("target") or {}
        rid = str(tgt.get("rid") or "")
        if rid != want_tail and rid != want:
            break          # 不是同一个目标 -> 连续性断了，停止计数
        if row.get("changed"):
            break          # 这一下有效果 -> 之前的不算"原地打转"
        n += 1
    return n


def _coerce_input_intent(answer: dict[str, Any],
                         state: ChildState) -> dict[str, Any]:
    """模型说了"要输入"却只给坐标 -> 翻译成真正的输入动作。

    见 decide 里调用处的说明。这里是**保守翻译**，条件不满足就原样返回。

    ## 为什么要翻译而不是让模型重来

    模型重来一轮要 8 秒左右，而它想表达的事情已经写在 `why` 里了 ——
    只是动作字段对不上。既然意图明确、目标也明确（就是那个输入框），
    代码代劳比让它再说一遍划算得多。这也是"用代码约束，不靠模型自觉"。

    ## 什么情况下**不**翻译（宁可不做，不可做错）

      · 模型已经给了 input_rid        -> 它自己会用，不插手
      · 没有坐标                       -> 不是点击动作，不插手
      · 坐标上不是输入框               -> 点的是普通按钮，绝不改写
      · why 里没有输入意图             -> 意图不明，交回模型
      · why 里取不到要填的内容         -> 不知道填什么，交回模型
    """
    if not isinstance(answer, dict):
        return answer
    if str(answer.get("input_rid") or "").strip():
        return answer            # 模型自己会用，不动
    x, y = answer.get("next_x"), answer.get("next_y")
    if x is None or y is None:
        return answer            # 不是点击
    if answer.get("step_done") or answer.get("key"):
        return answer

    why = str(answer.get("why") or "")

    # 意图词：模型说"要输入/填写/键入/填入"才算
    INTENT = ("输入", "填写", "填入", "键入", "录入", "填上", "写入")
    if not any(w in why for w in INTENT):
        return answer

    # 坐标上必须真的是输入框
    rid, hint = _input_at(state, int(x), int(y))
    if not rid:
        return answer

    # 要填什么：先从 why 里找，找不到就看输入框里的占位文字
    text = _extract_fill_text(why, hint)
    if not text:
        return answer

    out = dict(answer)
    out["input_rid"] = rid
    out["input_text"] = text
    # ⚠️ 必须清掉坐标，否则 act 会先走输入分支、这一轮不点 ——
    #    但下一轮坐标还在状态里容易被误当成"还没执行"。
    out["next_x"] = None
    out["next_y"] = None
    out["why"] = (f"{why}｜（代码转换：这其实是往 {rid} 里输入「{text}」，"
                  f"已改成 input_rid/input_text）")
    out["_coerced_input"] = True
    return out


def _input_at(state: ChildState, x: int, y: int) -> tuple[str, str]:
    """(x,y) 落在哪个输入框里？返回 (rid, 占位文字)，不是输入框返回 ("", "")。

    用落盘 XML 的 bounds 判包含关系 —— 和控件坐标是同一个坐标系。
    """
    xml_path = state.get("obs_xml_path") or ""
    if not xml_path:
        return "", ""
    try:
        from pathlib import Path as _P

        text = _P(xml_path).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return "", ""

    import re as _re

    for m in _re.finditer(r"<node\b[^>]*>", text):
        tag = m.group(0)
        c = _re.search(r'class="([^"]*)"', tag)
        cls = c.group(1) if c else ""
        if not any(k in cls for k in ("EditText", "AutoCompleteTextView",
                                      "TextInputEditText")):
            continue
        b = _re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', tag)
        if not b:
            continue
        x1, y1, x2, y2 = (int(b.group(i)) for i in (1, 2, 3, 4))
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            continue
        r = _re.search(r'resource-id="([^"]*)"', tag)
        rid = (r.group(1) if r else "").split("/")[-1]
        if not rid:
            continue
        t = _re.search(r'\btext="([^"]*)"', tag)
        return rid, (t.group(1) if t else "")
    return "", ""


def _extract_fill_text(why: str, hint: str) -> str:
    """从模型的 why 里取出"要填什么"。取不到返回空串。

    模型通常会把值放在反引号、引号或「」里，比如：
        「…向其中输入文字「测试课程表」」
        「…input_text 填 '测试课程表'」
    优先取这些显式标出来的；没有标点什么就返回空 —— 不猜。
    """
    import re as _re

    for pat in (r"「([^」]{1,40})」", r"`([^`]{1,40})`",
                r"\"([^\"]{1,40})\"", r"‘([^’]{1,40})’",
                r"'([^']{1,40})'"):
        for mm in _re.finditer(pat, why):
            v = mm.group(1).strip()
            # 过滤掉明显不是"要填的值"的片段
            if not v or v in ("必填", "课程表名称（必填）"):
                continue
            if any(k in v for k in ("输入框", "rid=", "点击", "保存", "完成")):
                continue
            if len(v) >= 1:
                return v
    return ""


def _list_empty_inputs(state: ChildState) -> list[str]:
    """列出当前屏上**空的输入框**，附带它们自己的占位文字。

    只做"告知"，不做"替模型决定填什么"——
    填什么是业务判断（该填课程名还是教室？），那是模型的事。
    代码的责任是让模型**知道这里有输入框、而且它现在可以输入**。

    返回形如：
        ["rid=et_schedule_name class=EditText 当前文字「课程表名称（必填）」"]

    判据：
      · class 含 EditText / AutoCompleteTextView / TextInputEditText
      · text 有内容（占位符也算内容，因为那正是"还没填"的证据）
    """
    from pathlib import Path as _P

    xml_path = state.get("obs_xml_path") or ""
    if not xml_path:
        return []
    try:
        text = _P(xml_path).read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []

    import re as _re

    out: list[str] = []
    for m in _re.finditer(r"<node\b[^>]*>", text):
        tag = m.group(0)
        c = _re.search(r'class="([^"]*)"', tag)
        cls = c.group(1) if c else ""
        if not any(k in cls for k in ("EditText", "AutoCompleteTextView",
                                      "TextInputEditText")):
            continue
        r = _re.search(r'resource-id="([^"]*)"', tag)
        rid = (r.group(1) if r else "").split("/")[-1]
        if not rid:
            continue
        t = _re.search(r'\btext="([^"]*)"', tag)
        txt = t.group(1) if t else ""
        out.append(f"rid={rid} 现在显示「{txt or '(空)'}」")
        if len(out) >= 6:
            break
    return out


def _ask_model(state: ChildState, reason: str) -> dict[str, Any]:
    """代码定不了 -> 交回模型。

    ★ 不丢一段报错就完事。像带新人一样交代处境：
      · 这一步要干什么（desc，写得越具体这里越有用）
      · 现在屏幕上是什么
      · 上次踩过什么坑（pitfalls）
      · 能用的手段有哪些
    """
    sid = _step_id(state)
    task = state.get("task") or {}
    idx = state.get("step_index", 0)
    steps = task.get("steps") or []
    step = steps[idx] if 0 <= idx < len(steps) else {}
    act_now = state.get("last_activity") or "(未知)"
    pkg_now = state.get("obs_package") or ""
    app_pkg = str(task.get("package") or "")

    lines = [f"【这一步要做的】{step.get('desc') or sid}"]
    if step.get("landing"):
        lines.append(f"【做完应该是】{step['landing']}")
    lines.append(f"【现在屏幕上】activity={act_now}"
                 + (f"，包名={pkg_now}" if pkg_now else ""))
    if pkg_now and app_pkg and pkg_now != app_pkg \
            and not is_system_permission_pkg(pkg_now):
        lines.append(f"⚠️ 你现在**已经不在被测 App 里了**"
                     f"（被测是 {app_pkg}，当前是 {pkg_now}）——"
                     f"先用 key=\"back\" 退回被测 App，别在这个陌生界面里找控件。")
    lines.append(f"【代码为什么没直接点】{reason}")

    pits = step.get("pitfalls") or []
    if pits:
        lines.append("【上次踩过的坑】")
        lines.extend(f"  · {p}" for p in pits[:6])

    lines.append("【你可以用的手段】点屏幕上的控件（给 next_x/next_y）、"
                 "**在输入框里填文字**（给 input_rid + input_text，"
                 "不要同时给坐标）、"
                 "按返回键（key=\"back\"）、回桌面（key=\"home\"）。")

    # ★ 如果屏幕上就摆着一个空的必填输入框，直接点出来。
    #
    # 为什么值得单独说：176 里模型三轮都在点输入框 —— 它知道要填名称，
    # 但没意识到"点输入框"和"往输入框里写字"是两种不同的动作。
    # 屏幕上明明写着「课程表名称（必填）」，那就把话挑明。
    _req = _list_empty_inputs(state)
    if _req:
        lines.append("【屏幕上这些输入框是空的（可能是必填）】")
        lines.extend(f"  · {r}" for r in _req[:4])
        lines.append("  要填就用 input_rid（上面那个 rid）+ input_text。"
                     "只点输入框不会改变界面，也不会有任何效果。")

    return {
        "action": None,
        "fallback_reason": reason,
        "fallback_guidance": "\n".join(lines),
        # ★ 记下"这次是在哪一屏放弃的"。
        #
        # 早先只记 reason，于是 fallback_reason 一旦置上就**永久生效**：
        # 后续每一轮 plan 都保留它 → route_after_plan 直接去 decide →
        # strong_target 再也不被调用。真机 168 实测 80 轮**全是模型**，
        # 强证据直点 0 次，整个机制形同虚设（日志里"代码直点 0 次"）。
        #
        # 正确语义：代码在**这一屏**上认不出目标，不代表换了一屏还认不出。
        # 屏幕一变就该重新给代码一次机会 —— 这才是"能直判的别问模型"。
        "fallback_screen": _screen_fingerprint(state),
        "strong_hits": [{"step": sid, "by": "model", "reason": reason}],
        "progress": [_tick("fallback", f"{sid} 代码定不了，交给模型：{reason}",
                           step=sid)],
    }


def _screen_fingerprint(state: ChildState) -> str:
    """当前这一屏的指纹 —— 用来判断"是不是换屏了"。

    只用代价最低、最稳的三样：activity + 包名 + 可点控件的 rid 序列。
    不做哈希（可读性更重要）：`AllInOneActivity|com.zui.calendar|iv_more,btnX`
    """
    act = str(state.get("last_activity") or state.get("obs_activity") or "")
    pkg = str(state.get("last_package") or "")
    rids = ",".join(sorted(str(n.get("r") or "") for n in
                           (state.get("obs_clickable") or [])))
    return f"{act}|{pkg}|{rids}"
# ========================================================================
# 新图的 8 个节点（边走边验）
# ========================================================================



# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _pending_for(state: ChildState, sid: str) -> list[dict[str, Any]]:
    """这一步名下**还没验**的预期。

    保护 11：证据按步骤取。这里就是"按步骤"的定义 ——
    一步走完只验它名下的，不碰别的步骤的断言。
    """
    if not sid:
        return []
    done = {str(f.get("id")) for f in (state.get("findings") or [])
            if f.get("kind") == "assert"}
    return [a for a in ((state.get("task") or {}).get("asserts") or [])
            if str(a.get("step") or "") == sid
            and str(a.get("id") or "") not in done]


def _asserts_done(state: ChildState) -> int:
    return len([f for f in (state.get("findings") or [])
                if f.get("kind") == "assert"])


def _step_desc(task: dict[str, Any], sid: str) -> str:
    for s in (task.get("steps") or []):
        if str(s.get("id") or "") == sid:
            return str(s.get("desc") or "")
    return ""


def _acts_seen(state: ChildState) -> list[str]:
    """一路走过哪些 activity。

    ⚠️ visited 里存的是 "A -> B" 形式的转移记录（保护 12），
       这里拆成单个 activity 名，因为断言问的是"到达过 X 没有"。
    """
    out: set[str] = set()
    for v in (state.get("visited") or []):
        s = str(v)
        if "->" in s:
            left, right = s.split("->", 1)
            out.add(left.strip())
            out.add(right.strip())
        elif s.strip():
            out.add(s.strip())
    if state.get("last_activity"):
        out.add(str(state["last_activity"]))
    return sorted(x for x in out if x)


def _advance_from_verify(state: ChildState, vs: str) -> dict[str, Any]:
    """本步验完 -> 推进到下一步，并把本步的各种计数清零。"""
    nxt = int(state.get("step_index") or 0) + 1
    print(f"  [verify] {vs} 的预期全部验完 -> 进入下一步"
          f"（step_index {nxt}）", file=sys.stderr)
    return {"step_index": nxt, "verify_step": "", "verify_assert": "",
            "round_in_step": 0, "check_rounds": 0, "retry_landing": 0,
            "locate_attempts": 0, "fallback_reason": "",
            "assert_fix_rounds": 0}


def _reset_per_assert() -> dict[str, Any]:
    """一条断言判完了就清它的补点计数。

    清在这里而不是"整步结束"，因为补点次数是**针对某一条预期**的：
    上一条点了 2 次，不该让下一条只剩 1 次机会。
    """
    return {"assert_fix_rounds": 0}


# 判定 schema 直接复用 nodes.py 的 AssertVerdict，**不自己定义一份**。
#
# 踩过的坑（保护 9）：这个 schema 是 LangGraph 校验模型回复用的，
# **没声明的字段会被静默丢掉**。178 实测：runner 在解析失败时返回
# {"passed": None, "unknown": True, "error": "..."}，但 schema 里没有
# 这两个键 -> 直接丢 -> "判不出来"这件事根本传不上来，
# issues 里印出空括号「判定缺失：模型回复无法解析（）」，排查时完全无从下手。
# 所以这份 schema 只能有一份，改动必须落在 nodes.py 那个类上（已在上面导入）。


# --------------------------------------------------------------------------
# 节点 6/8：verify —— 验这一步名下的预期
#
# 合并了旧版的 assert + verdict。
#
# ⚠️ 为什么内部还是**两段式**（保护 25/26）：
#    LangGraph 的 interrupt 恢复时，整个节点**从头重跑**。
#    形态：
#      第一次进 verify  -> interrupt(问题) -> 挂起
#      恢复后重跑 verify -> interrupt 直接返回答案 -> 落 finding
#    这是机制，对外就是一个节点。
# --------------------------------------------------------------------------

def verify_node(state: ChildState) -> dict[str, Any]:
    """验本步名下的预期；全部验完后推进 step_index。

    ## 为什么必须紧跟 check

    用户：「AI走的时候第一轮就边走边验证了。能拿到所有的记录。
           然后最后根据记录写json。而不是回过头就复验证。」

    旧链路走完所有步骤再回头验，那时人早不在那一屏：
      a2     要验"进编辑页"，判的时候在引导页  -> FAIL（判错）
      a4/5/6 要验"空课表"，判的时候在显示页    -> FAIL（判错）
      a14    要验"弹窗关了"，判的时候在编辑页   -> FAIL（判错）
    这些不是产品坏了，是验错了地方。
    """
    from langgraph.types import interrupt

    task = state.get("task") or {}
    asserts = task.get("asserts") or []
    vs = str(state.get("verify_step") or "")

    if not vs:
        return {"pending": None, "verify_assert": ""}

    mine = _pending_for(state, vs)
    if not mine:
        # 进了 verify 却没活干 -> 也要推进，否则路由原地空转。
        return _advance_from_verify(state, vs)

    a = mine[0]
    # 证据必须是**这一步**那一屏（保护 11）
    ev = _evidence_for_assert(state, a)
    evd = ev.get("data") or {}
    # 颜色/形状/数量这类 XML 判不了的，先问视觉
    vis = _vision_for_assert(state, a, evd)

    # ⚠️ interrupt 恢复时这里会重跑，所以上面必须全是**纯读**：
    #    不写盘、不改 state，否则恢复时重复执行。
    payload = {
        "kind": "assert",
        "index": _asserts_done(state) + 1,
        "total": len(asserts),
        "assert_id": a.get("id"),
        "desc": a.get("desc") or "",
        "case_title": task.get("title"),
        "step_id": vs,
        "step_desc": _step_desc(task, vs),
        "evidence_source": ev.get("source") or "",
        "evidence_bound": ev.get("bound"),
        "screen_activity": evd.get("activity") or "",
        "screen_nodes_text": evd.get("nodes_text") or "",
        "screen_nodes": evd.get("nodes") or [],
        "screen_clickable": evd.get("clickable") or [],
        # 图片只走 vision 通道（保护 15：run 68 每条提示词图片占 99.3%）
        "image": "",
        "vision_result": vis,
        # 一路见过什么（保护 12：判"过程中出现过"要用）
        "activity_seen": _acts_seen(state),
        "seen_rids": sorted({str(r) for r in (state.get("seen_rids") or [])}),
        "visited": (state.get("visited") or [])[-8:],
        "evidence_event_ids": state.get("last_event_ids") or [],
        "knowledge": (state.get("action") or {}).get("knowledge") or "",
        "knowledge_files": task.get("knowledge_files") or [],
        "question": _verify_question(ev, vis),
    }

    answer = interrupt(payload, response_schema=AssertVerdict)
    if isinstance(answer, AssertVerdict):
        answer = answer.model_dump()

    # ── 信息不足，AI 要求再点一下才能判 ──
    #
    # 用户定的：「因为是这样。当前信息不足。我需要进一步点击或者其他动作
    #             才能判定。」
    #
    # 这不是"补证据"那种独立机制，就是**验证时自己动手看一眼** ——
    # 人类测试员点开色板数颜色，看完点「取消」退回，天经地义。
    #
    # 所以放在 verify 内部处理，不加节点：
    #   点一下 -> 重新观察 -> 回来再判（同一条断言）
    # 轮次上限用 assert_fix_rounds（ASSERT_AI_RECHECK_MAX），防无限点。
    na = answer.get("need_action") if isinstance(answer, dict) else None
    if na and _need_action_valid(na):
        return _do_need_action(state, a, vs, answer, na)

    return _land_verdict(state, a, vs, answer, vis)


def _need_action_valid(na: Any) -> bool:
    """是不是一个真的"要动手"请求（而不是空对象）。"""
    if not isinstance(na, dict):
        return False
    # 有明确要点的东西才算 —— 只给个说明没有落点，执行器没法动
    els = na.get("related_elements") or []
    for e in els:
        if isinstance(e, dict) and (e.get("rid") or e.get("text")):
            return True
    return False


def _do_need_action(state: ChildState, a: dict[str, Any], vs: str,
                    answer: dict[str, Any], na: dict[str, Any]) -> dict[str, Any]:
    """按 AI 的要求点一下，然后让它重新判同一条。

    ## 为什么要点"看完怎么关回去"的那个键

    用户：「因为背景色也是个弹窗呀。你验证完要继续推倒上一步啊。
           不然继续不下去了。我说了要像个人类测试员一样」

    点开色板看完，得点「取消」退回编辑弹窗，否则下一条断言
    （a14 要验弹窗关了）拿到的是色板，验的根本不是一回事。

    ## ⚠️ 绝不按 back

    run 79 的教训（保护 16）：补证据补完按了 back，退到桌面，
    后面 11 条断言全对着 launcher 判，整轮报废。
    所以只按 need_action 里明确给的**按钮文字**去点，
    点不到就什么都不做（宁可留着不关，也不乱按）。
    """
    dev = _ctx().device
    rounds = int(state.get("assert_fix_rounds") or 0)
    limit = ASSERT_AI_RECHECK_MAX

    # 先把"看完要关回去"的按钮记下来 —— 等重判完再点它
    close_with = str(na.get("close_with") or "").strip()

    if rounds >= limit:
        # 点过这么多次还判不了，就此打住，如实记 BLOCKED
        return _blocked_verdict(
            state, a, vs,
            f"已经为这条预期补点过 {rounds} 次仍然判不了："
            f"{na.get('why_insufficient') or ''}",
            f"（多次尝试后仍无法判定：{na.get('verify_target') or ''}）")

    # 挑一个要点的元素：优先 rid，没有 rid 用文字
    target = None
    for e in (na.get("related_elements") or []):
        if not isinstance(e, dict):
            continue
        rid = str(e.get("rid") or "").strip()
        txt = str(e.get("text") or "").strip()
        if rid or txt:
            target = {"rid": rid, "text": txt}
            break
    if not target:
        return _blocked_verdict(state, a, vs,
                               "AI 说要动手，但没给出具体要点的东西",
                               str(answer.get("actual") or ""))

    why = f"为验 {a['id']} 点开「{target['text'] or target['rid']}」看实际内容"
    shot_before = state.get("last_shot") or ""
    xml_before = state.get("obs_xml_path") or ""
    # ⚠️ 这里**不能用 click_rid / click_xpath**：
    #    它们内部调 act() 时没传 step，clicks.jsonl 里这条记录就丢了 step ——
    #    回写 JSON 时"这次是为验哪条断言点的"就断了。
    #    所以自己取坐标、再调 act(step=..., why=...)，账本才完整。
    try:
        scr = dev.observe()
        node = scr.find_rid(target["rid"]) if target["rid"] else None
        if node is None and target["text"]:
            # 没 rid（或找不到）就按文字精确找：命中必须唯一，不赌（保护 18）
            t = target["text"]
            cands = [n for n in scr.clickable
                     if n.get("t") == t or n.get("d") == t]
            if len(cands) == 1:
                node = cands[0]
            elif len(cands) > 1:
                return _blocked_verdict(
                    state, a, vs,
                    f"「{t}」在当前屏有 {len(cands)} 个同名控件，不敢赌点哪个",
                    str(answer.get("actual") or ""))
        if node is None:
            return _blocked_verdict(
                state, a, vs,
                f"按 AI 要求点「{target['text'] or target['rid']}」，"
                f"但当前屏上找不到它",
                str(answer.get("actual") or ""))
        cx, cy = int(node["c"][0]), int(node["c"][1])
        res = dev.act(cx, cy, why=why, step=f"a{vs}")
    except Exception as exc:  # noqa: BLE001
        return _blocked_verdict(
            state, a, vs,
            f"按 AI 要求点「{target['text'] or target['rid']}」失败：{exc}",
            str(answer.get("actual") or ""))

    # 点完重新观察，新的证据就来自这一屏
    obs = {}
    try:
        obs = dev.observe_raw() or {}
    except Exception as exc:  # noqa: BLE001
        print(f"  [verify] {a['id']} 点开后观察失败：{exc}", file=sys.stderr)

    new_shot = (obs.get("evidence") or {}).get("png") or ""
    # ⚠️ 键名必须是 assert_id：build_learned_case 按 assert_id 归集，
    #    写成 "assert" 的话回写的 JSON 里"为验这条点了什么"就丢了 ——
    #    而那正是用户要的「为了验证个点我执行了多少步」。
    act_note = {
        "assert_id": a["id"],
        "step": vs,
        "why_insufficient": str(na.get("why_insufficient") or ""),
        "open": target["text"] or target["rid"],
        "close": close_with,
        "action": f"点开 {target['text'] or target['rid']}",
        "why": why,
        "changed": (obs.get("activity") or "") != (state.get("last_activity") or ""),
        "shot": new_shot or shot_before,
        "xml": _screen_xml_path(obs) if obs else xml_before,
    }
    print(f"  [verify] {a['id']} 信息不足 -> 点开 "
          f"「{target['text'] or target['rid']}」重判"
          f"（第 {rounds + 1}/{limit} 次）", file=sys.stderr)

    return {
        "assert_fix_rounds": rounds + 1,
        "last_activity": obs.get("activity") or state.get("last_activity") or "",
        "last_shot": new_shot or shot_before,
        "last_event_ids": [str(e) for e in (obs.get("event_ids") or [])],
        "last_package": obs.get("package") or state.get("last_package") or "",
        "obs_clickable": obs.get("next") or [],
        "obs_nodes_text": _screen_text(obs) if obs else "",
        "obs_nodes": _brief_nodes(obs.get("nodes")) if obs else [],
        "obs_xml_path": _screen_xml_path(obs) if obs else "",
        # ★ 证据换成"点开之后"这一屏，并且**仍然记在这一步名下**
        "step_evidence": _step_evidence_of(state, obs) if obs else [],
        "seen_rids": _seen_rids(obs.get("next"), obs.get("nodes")) if obs else [],
        "seen_texts": _seen_texts(obs.get("next"), obs.get("nodes")) if obs else [],
        "verify_actions": [act_note],
        # ★ 这个要跨轮次留着：重判时还得知道"看完怎么关回去"。
        #   必须声明在 ChildState 里，否则下一轮就被丢掉了。
        "_close_with": close_with,
        # 保持 verify_step 不变 -> 路由会回到 verify 重判同一条
        "verify_step": vs,
    }


def _verify_question(ev: dict[str, Any], vis: dict[str, Any]) -> str:
    """给判定模型的问题。

    只说"要判什么、证据从哪来、判不了怎么办"；
    「可以XX」这类不可观察写法的判定规则在 decide.py 的 _SYSTEM_VERDICT 里，
    那是全局约定，不在这里重复。
    """
    parts = [
        f"证据来自{ev.get('source') or '本次执行'}，只有 XML 文字（**没有图片**）。",
    ]
    if not ev.get("bound"):
        parts.append(
            "⚠️ 这条预期没绑定到某一步，只能拿整轮最后一屏当证据 —— "
            "如果这一屏明显不是该看的地方，请判 null 并说明。")
    if vis.get("ok"):
        parts.append(f"视觉模型已经看过图，回答是：{vis.get('answer') or ''}")
    elif vis.get("tried") and not vis.get("ok"):
        parts.append(
            f"⚠️ 这条预期需要看图（{vis.get('why') or ''}），"
            f"但视觉模型没给答案（{vis.get('detail') or '未知原因'}）。"
            "纯 XML 判不了就判 null，**不要猜**。")
    parts.append(
        "这条预期达成了吗？actual 写具体现象；"
        "event_id 必须取自 evidence_event_ids。"
        "XML 里确实找不到依据、且必须看图才能判 -> passed=null，"
        "并在 actual 里写清要看的到底是什么。")
    return "\n".join(parts)


def _blocked_verdict(state: ChildState, a: dict[str, Any], vs: str,
                     why: str, actual: str) -> dict[str, Any]:
    """记成"没验成"——既不是 PASS 也不是 FAIL。

    保护 7：这是最容易被写错的地方。"模型没答上来"和"产品坏了"
    是两件事，混成一件就会把自己的执行问题算到产品头上。
    """
    dev = _ctx().device
    note = f"（{why}）"
    try:
        dev.finding_no_evidence("BLOCKED", a.get("desc") or "",
                                (actual + note).strip() or note)
    except Exception:  # noqa: BLE001
        pass
    out: dict[str, Any] = {
        "findings": [{"kind": "assert", "id": a["id"],
                      "desc": a.get("desc") or "", "step": vs,
                      "status": "BLOCKED", "actual": actual or why,
                      "event_id": "", "issues": [why]}],
        "pending": None,
        "verify_assert": "",
    }
    return {**out, **_after_asserts_exhausted(state, out, vs)}


def _after_asserts_exhausted(state: ChildState, out: dict[str, Any],
                             vs: str) -> dict[str, Any]:
    """这条验完后：先把"点开的东西关回去"，再决定继续验还是进下一步。

    ## 为什么必须关回去

    用户：「因为背景色也是个弹窗呀。你验证完要继续推倒上一步啊。
           不然继续不下去了。我说了要像个人类测试员一样」

    点开色板数完颜色，界面停在色板上。不关的话，下一条断言
    （比如 a14「弹框关闭」）拿到的是色板 —— 验的根本不是一回事。

    ## ⚠️ 绝不按 back（保护 16）

    run 79 的教训：补完证据按了 back，退到桌面，后面 11 条断言
    全对着 launcher 判，整轮报废。所以只点 need_action 里明确给了的
    **按钮文字**，点不到就什么都不做。

    ## ⚠️ 必须用**加过本条之后**的 findings 判断

    本条 finding 刚产生、state 里还没有，用 state 判会误以为"还有剩的"，
    于是永远不推进 —— 旧版踩过。
    """
    _all = list(state.get("findings") or []) + out.get("findings", [])
    _left = [x for x in ((state.get("task") or {}).get("asserts") or [])
             if str(x.get("step") or "") == vs
             and not any(f.get("kind") == "assert"
                         and str(f.get("id")) == str(x.get("id"))
                         for f in _all)]
    extra = _close_after_look(state)
    if not _left:
        return {**_advance_from_verify(state, vs), **extra,
                "assert_fix_rounds": 0}
    print(f"  [verify] {vs} 还有 {len(_left)} 条："
          f"{','.join(str(x.get('id')) for x in _left)}", file=sys.stderr)
    return {**extra, **_reset_per_assert()}


def _close_after_look(state: ChildState) -> dict[str, Any]:
    """把为了看清而点开的东西关回去。

    只认 AI 在 need_action.close_with 里**明确给过的按钮文字**，
    且必须唯一命中。点不到就什么都不做 —— 宁可界面留着，
    也不乱点（保护 16：乱按 back 毁过一整轮）。
    """
    want = str(state.get("_close_with") or "").strip()
    if not want:
        return {}
    dev = _ctx().device
    try:
        scr = dev.observe()
        cands = [n for n in scr.clickable
                 if n.get("t") == want or n.get("d") == want]
        if len(cands) != 1:
            print(f"  [verify] 「{want}」命中 {len(cands)} 个，不关（不乱点）",
                  file=sys.stderr)
            return {"_close_with": ""}
        c = cands[0]["c"]
        dev.act(int(c[0]), int(c[1]), why=f"看完关回去：{want}", step="close")
        print(f"  [verify] 已点「{want}」关回去", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"  [verify] 关回去失败：{exc}", file=sys.stderr)
    return {"_close_with": ""}


def _land_verdict(state: ChildState, a: dict[str, Any], vs: str,
                  v: dict[str, Any], vis: dict[str, Any]) -> dict[str, Any]:
    """把结论落成一条 finding。

    ⚠️ 三态（保护 7/8/9）：
        passed=True  -> PASS
        passed=False -> FAIL
        passed=None  -> BLOCKED（"没验成"，**不是**"功能坏了"）

    `bool(None)` 是 False —— 那正是"把没验成当成验失败"，
    是这份代码里最容易犯也最伤人的错，所以必须显式判 None。
    """
    dev = _ctx().device
    issues: list[str] = []
    actual = str(v.get("actual") or "")
    p = v.get("passed")

    # 保护 9：模型回复解析失败 -> "判不了"，不是"功能坏了"。
    # 178 实测：这里当初没判断，于是解析失败的回复被当成 passed=False 记成 FAIL。
    if v.get("unknown") or str(v.get("error") or "").strip():
        err = str(v.get("error") or "").strip() or "未给出原因"
        return _blocked_verdict(state, a, vs,
                                f"模型回复无法解析（{err}）", actual)

    if p is None:
        unreviewed = True
        passed = False
        if not actual.strip():
            actual = "（模型没给出结论）"
    else:
        unreviewed = False
        passed = bool(p)

    # 证据 id 校验（保护 13）
    # 176 实测：last_event_ids 为空时 a7/a8/a9 全被判 PASS，其实没证据。
    got = str(v.get("event_id") or "").strip()
    valid = set(state.get("last_event_ids") or [])
    if passed and valid and got and got not in valid:
        issues.append(f"{a['id']} 引用的 event_id {got} 不在本次证据里，忽略")
        got = ""

    status = "BLOCKED" if unreviewed else ("PASS" if passed else "FAIL")
    if unreviewed and vis.get("tried") and not vis.get("ok"):
        actual = (actual + f"｜视觉通道失败：{vis.get('detail') or ''}").strip("｜")

    try:
        if got and got.isdigit():
            dev.finding(status, got, a.get("desc") or "", actual)
        else:
            dev.finding_no_evidence(status, a.get("desc") or "", actual)
    except Exception as exc:  # noqa: BLE001
        issues.append(f"{a['id']} finding 写入失败：{exc}")

    out: dict[str, Any] = {
        "findings": [{"kind": "assert", "id": a["id"],
                      "desc": a.get("desc") or "", "step": vs,
                      "status": status, "actual": actual,
                      "event_id": got, "issues": issues}],
        "issues": issues,
        "pending": None,
        "verify_assert": "",
    }
    return {**out, **_after_asserts_exhausted(state, out, vs)}

# --------------------------------------------------------------------------
# 节点 5/8：check —— 这一步走完了吗
#
# 这是「边走边验」的枢纽：判断完成时**不推进** step_index，
# 而是设 verify_step，把控制权交给 verify（保护 31）。
# --------------------------------------------------------------------------

def check_node(state: ChildState) -> dict[str, Any]:
    """判本步是否达成目的，决定：去验、重试、还是进下一步。

    双重保险：AI 说 done 之后，**代码再用 landing 复核一次**。
    AI 可能乐观（"看起来到了"），代码可不信。
    """
    from ..device.bridge import is_system_permission_pkg

    action = state.get("action") or {}
    task = state["task"]
    idx = state.get("step_index", 0)
    step = task["steps"][idx]
    sid = str(step.get("id") or "")
    done = bool(action.get("step_done"))
    give_up = bool(action.get("give_up"))
    rounds = state.get("round_in_step", 0)
    limit = state.get("max_rounds_per_step", MAX_ROUNDS_PER_STEP)
    issues: list[str] = []
    landing = (step.get("landing") or "").strip()
    act_now = state.get("last_activity") or ""
    front_pkg = state.get("last_package") or ""

    # 系统权限框不是"完成了本步"（保护 21）
    if done and is_system_permission_pkg(front_pkg):
        issues.append(
            f"{sid} 声明完成，但屏幕上仍是**系统权限框**（包名 {front_pkg}）"
            f"—— 权限框不是本步目标，等它被处理完再判")
        done = False

    # 落点复核：AI 说到了，代码再验一遍（保护 28：相等不子串）
    if done and landing:
        hit, why = match_landing(landing, activity=act_now,
                                 clickable=state.get("obs_clickable"),
                                 state_pkg=front_pkg)
        if not hit:
            issues.append(
                f"{sid} 声明完成但落点不符：期望 {landing}，"
                f"实际 activity={act_now}（{why}）")

    # ── 放弃 ──
    if give_up and not done:
        reason = action.get("give_up_reason") or "未给出原因"
        # ⚠️ 这里曾经靠关键字猜：`any(k in reason for k in ("卡死","still_stuck","放弃"))`。
        #    结果交替死循环给的原因写的是「代码判定死循环」，一个词都没命中 ——
        #    于是被当成"只是这一步放弃"，跳一步继续跑，后面 53 轮反复
        #    报同一个已被判死的决定（test_cycle 抓到）。
        #    改成显式字段：是不是真卡死，由**判定的那一段代码**说了算，
        #    不靠事后解析文案。模型给的 give_up 一律不带 stuck，
        #    等价于旧行为（只放弃本步），所以迁移是安全的。
        stuck = bool(action.get("stuck"))
        if stuck:
            # 真卡死才终止整条用例（保护 30 的反面：卡死=页面不变，
            # 后面每步只会重演同一循环，169 因此白烧 4 分钟）
            return {
                "status": "BLOCKED",
                "summary": f"{sid} 判定卡死（{reason}），整条用例终止"
                           f"（当前 activity={act_now}）",
                "issues": issues + [f"{sid} 卡死终止：{reason}"],
                "abort": f"{sid} 卡死：{reason}",
                "findings": [{"kind": "step", "step": sid,
                              "desc": step["desc"], "landing": act_now,
                              "status": "give_up"}],
            }
        # 只是这一步放弃 -> 记下来，继续下一步（保护 30：不传染）
        return {
            **_after_step(state, sid, step, act_now, "give_up"),
            "issues": [f"{sid} 放弃：{reason}"],
            "progress": [_tick("fallback", f"{sid} 放弃，进入下一步", step=sid)],
        }

    # ── 声明完成但落点不对 -> 重试 ──
    if done and issues:
        tried = state.get("retry_landing", 0)
        if tried + 1 < LANDING_MAX_TRIES:
            return {
                "retry_landing": tried + 1,
                "issues": issues,
                "progress": [_tick(
                    "retry",
                    f"{sid} 落点未到，第 {tried + 2}/{LANDING_MAX_TRIES} 次尝试"
                    f"（当前 {act_now}）", step=sid)],
            }
        return {
            "status": "BLOCKED",
            "retry_landing": tried + 1,
            "issues": issues + [
                f"🚫 {sid} 落点不符：试了 {LANDING_MAX_TRIES} 次都没到目标页"
                f"（期望 {landing}，实际停在 {act_now}）。关键路径没找到"],
            "findings": [{"kind": "step", "step": sid,
                          "desc": step["desc"], "landing": act_now,
                          "status": "blocked"}],
            "action": None,
            "summary": f"{sid} 落点不符（{LANDING_MAX_TRIES} 次），用例未执行完",
            "action_trace": [],
        }

    # ── 完成 ──
    if done:
        # 优先级：本步有断言 -> 就地验（保护 32）
        _mine = _pending_for(state, sid)
        if _mine:
            return {
                **_after_step(state, sid, step, act_now, "ok"),
                "issues": issues,
                # ★ 不推进 step_index，交给 verify
                "verify_step": sid,
            }
        # 本步没有断言 -> 直接推进（否则没人来清 verify_step，流程卡死）
        return {
            **_after_step(state, sid, step, act_now, "ok"),
            "issues": issues,
            "step_index": idx + 1,
            "verify_step": "",
            "round_in_step": 0,
            "check_rounds": 0,
            "retry_landing": 0,
            "locate_attempts": 0,
        }

    # ── 没完成：先查死循环闸门 ──
    crounds = state.get("check_rounds", 0)
    if rounds >= limit or crounds >= limit:
        why2 = "本步一直没动手" if crounds >= limit else f"重试 {rounds} 次"
        # 保护 30：这一步没收敛，记下来、进下一步，**不终止整条用例**
        return {
            **_after_step(state, sid, step, act_now, "not_converged"),
            "status": "BLOCKED",
            "summary": f"{sid} {why2}仍未收敛，停止",
            "issues": issues + [
                f"{sid} 未收敛（act {rounds} 轮 / check {crounds} 轮，"
                f"上限 {limit}）"],
            "step_index": idx + 1,
        }

    # ── 目标查找有界重试（保护：rid 太严） ──
    locate = (step.get("locate") or "").strip()
    attempts = state.get("locate_attempts", 0)
    max_attempts = state.get("max_locate_attempts", MAX_LOCATE_ATTEMPTS)
    extra: dict[str, Any] = {}
    if locate:
        here = target_present(locate, clickable=state.get("obs_clickable"),
                              nodes_text=state.get("obs_nodes_text", ""))
        if here is False:
            attempts += 1
            extra["locate_attempts"] = attempts
            why = (action.get("why") or "").strip()
            extra["locate_tried"] = [{
                "step": sid, "attempt": attempts, "of": max_attempts,
                "locate": locate, "activity": act_now, "why": why,
            }]
            issues.append(
                f"{sid} 第 {attempts}/{max_attempts} 次换目标找："
                f"目标 {locate} 不在当前屏（activity={act_now}）"
                + (f"，理由：{why}" if why else ""))
            if attempts > max_attempts:
                return {
                    **_after_step(state, sid, step, act_now, "locate_exhausted"),
                    "status": "BLOCKED",
                    "summary": f"{sid} 换目标找了 {max_attempts} 次仍找不到 "
                               f"{locate}，停止",
                    "issues": issues + [
                        f"{sid} 查找次数用尽（上限 {max_attempts}）"],
                    "step_index": idx + 1,
                    **extra,
                }
    return {
        "issues": issues,
        "check_rounds": crounds + 1,
        "progress": [_tick("retry",
                           f"{sid} 第 {rounds + 1} 轮：{act_now}"
                           + (f"｜{issues[-1]}" if issues else ""), step=sid)],
        **extra,
    }


def _after_step(state: ChildState, sid: str, step: dict[str, Any],
                act_now: str, status: str) -> dict[str, Any]:
    """记录本步的 finding 和进度 —— 各出口共用，避免遗漏。"""
    return {
        "findings": [{"kind": "step", "step": sid,
                      "desc": step.get("desc") or "",
                      "landing": act_now, "status": status}],
        "progress": [_tick("step", f"{sid} {status}（{act_now}）", step=sid)],
    }


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------

def route_after_plan(state: ChildState) -> Literal["strong_target", "decide", "learn", "finish"]:
    """plan 之后：还有步骤就先让代码试一次，没了就收尾。

    ★ 为什么先去 strong_target 而不是直接 decide
      "能直判的别问模型"是这个项目最基本的目标，而 168 两次实测
      "代码直点 0 / 交模型 0"—— target 字段从声明到执行没人读。
      先过一道代码判定，命中就不花 token。

    ⚠️ 必须先看 status —— plan 是**唯一**能直接判 BLOCKED 的节点
    （崩溃 / observe 失败），但它只是把 status 写进 state，路由不认这个
    字段的话会继续走到 decide 去问 AI、去 act。
    实测：被测 App 启动即崩，plan 已抓到终止信号，图却又发了一轮
    interrupt 问模型「这一步该怎么办」—— 设备都死了，问了也没法执行。
    """
    if state.get("abort") or state.get("status") == "BLOCKED":
        return "finish"
    steps = (state.get("task") or {}).get("steps") or []
    if int(state.get("step_index") or 0) >= len(steps):
        return "learn"
    return "strong_target"


def route_after_decide(state: ChildState) -> Literal["act", "check", "learn", "finish"]:
    """decide 之后：分派到执行 / 判定 / 收尾。

    ⚠️⚠️ 这个函数是**踩过三次同一个坑**的地方，改的时候要格外小心：

        176 轮 05~08：模型请求看图 -> 路由只认坐标 -> 判成"无动作" -> 白烧 20 秒
        169：模型要按返回键 -> key 没坐标 -> 动作静默丢失 -> 退不出相机
        176：模型正确调用 type_text(rid=...) -> 路由只判 next_x -> 输入从未发生

    教训：**action 里任何一个"能单独构成动作"的字段，都必须在这里有分支。**
    现在的全集：need_knowledge / vision_question / step_done / give_up /
                key / input_rid / tap_target / next_x

    视觉与知识的处理方式变了（不再拆成独立节点，见 decide_node 的说明），
    但它们**仍然是一种"这一轮没动作"**，所以这里要回到 check 让它重来，
    而不是让它掉到 else 被当成"有坐标"。
    """
    if state.get("abort"):
        return "finish"
    a = state.get("action") or {}
    # ── 明确"这一步完了 / 要放弃" -> 交给 check 判定 ──
    if a.get("step_done") or a.get("give_up"):
        return "check"
    # ── 这一轮是"要查知识 / 要看图"，没有实际动作 ──
    # decide_node 已经处理过它（查完/看完会把结果写回 action），
    # 所以这里回 check 让它再决策一轮 —— 撞轮次闸门也不会空转。
    if a.get("need_knowledge") or a.get("vision_question"):
        return "check"
    # ── 真正有动作的四种形态 ──
    if a.get("key") or a.get("_key"):
        return "act"
    if str(a.get("input_rid") or "").strip():
        return "act"
    if str(a.get("tap_target") or "").strip():
        return "act"
    if a.get("next_x") is not None:
        return "act"
    # 什么都没给 -> 也去 check（它会撞轮次闸门，而不是死循环）
    return "check"


def route_after_check(state: ChildState) -> Literal["plan", "verify", "learn", "finish"]:
    """check 之后：要走下一步就 plan，要验就 verify，走完了就 learn。

    ⚠️ 保护 31：verify_step 只在"本步有断言"时才被设上，
    所以这里看到它非空就知道该验，不会空转。

    ⚠️ 全部步骤走完必须去 **learn**（回写 JSON），不是直接 finish。
       漏这一步的后果：case_learned.json 根本不生成，跑完什么也没留下。
       （run 84 就漏了 —— 日志里节点执行没有 learn，只剩 finish。）
    """
    if state.get("abort"):
        return "finish"
    if state.get("verify_step"):
        return "verify"
    steps = (state.get("task") or {}).get("steps") or []
    if int(state.get("step_index") or 0) >= len(steps):
        return "learn"
    return "plan"


def route_after_verify(state: ChildState) -> Literal["verify", "plan", "learn", "finish"]:
    """verify 之后：本步还有预期没验完就继续验，否则走下一步。

    ⚠️ 必须能回到 verify 自己：verify 一次只处理一条断言，
       一步有 7 条断言（s4 就是）时，回 plan 会去重新 decide，
       等于每验一条就打断一次流程 —— 那不是"这一步验完再走"。

    ⚠️ 全部步骤走完必须去 **learn**（回写 JSON），不是直接 finish。
    """
    if state.get("abort"):
        return "finish"
    # 本步还有没验的 -> 接着验（一条一条来，像人一样）
    if _pending_for(state, str(state.get("verify_step") or "")):
        return "verify"
    steps = (state.get("task") or {}).get("steps") or []
    if int(state.get("step_index") or 0) >= len(steps):
        return "learn"
    return "plan"

# --------------------------------------------------------------------------
# 节点 4/8：act —— 执行动作
#
# ★ 直接复用旧实现。
#
# 为什么复用而不重写：act 里没有任何流程问题，全是**设备交互的坑**
# （输入框已有相同内容就别再填、坐标合法性、key 的返回、动作后画面归哪个步骤）。
# 这些坑都是真机一条条试出来的，重写一遍等于把 400 行踩过的雷再踩一遍。
#
# 这次改的是"什么时候验"，不是"怎么点"。
# --------------------------------------------------------------------------



# --------------------------------------------------------------------------
# 节点 3/9：strong_target —— 这一步要点的东西，代码能不能唯一确定？
#
# ★ 为什么它必须是一个**独立节点**，不能塞进 decide ★
#
# 重写成 8 个节点时我把它删了，以为 decide 内部会走这条路径 ——
# 但 _base_decide_node 第一行就是 interrupt 问模型，根本不看 target。
# 结果 target 字段**从声明到执行没有任何人读**：
# 两次跑 168 都是"代码直点 0 次 / 交给模型 0 次"（0/0 不是"都交给模型"，
# 是整个统计压根没被填充）。用例里写的 rid=iv_more 一次都没生效。
#
# 为什么不塞进 decide：
#   decide 在图和 runner 里有一条硬约束 —— 它是**唯一会 interrupt 的地方**
#   （runner 靠 answer_fn 喂它）。往里塞一段"可能不 interrupt 就直接返回"
#   的逻辑，会让"什么时候会停"变得不可预测，排查时也看不出这一步
#   到底是代码点的还是模型点的。
#
#   而它的开销只是多一次图调度（内存操作，微秒级），
#   相比一次模型调用（168 实测 3 次断言判定约占 20s）可以忽略。
#
# 判"能不能唯一确定"完全靠 state 里已有的 obs_clickable / obs_xml_path，
# **不碰设备、不发请求**，所以放在问模型之前是纯赚的。
# --------------------------------------------------------------------------

def strong_target_node(state: ChildState) -> dict[str, Any]:
    """强证据直点：这一步的 target 在**当前屏**能不能唯一确定？

    这个节点**不是回放**。它不认"上次点了哪"，只认"现在能不能唯一确定"：

        命中恰好 1 个  → 取它的坐标，直接点，全程 LLM 零介入
        命中 0 个      → 目标不在当前屏 → 交模型
        命中 >1 个     → 不唯一，可能点错 → 交模型

    为什么"唯一"是硬门槛：实测设置页有 4 个节点 rid/desc/text 全空、
    只有 bounds 不同。只按单一属性找根本分不出是哪个，点错了整条用例就废。
    所以宁可退回模型，也不赌。

    这个节点**只决定点哪**，不执行 —— 真正的点击仍走 act 节点，
    坐标校验、崩溃检测这些守卫一条都不少。
    """
    task = state.get("task") or {}
    steps = task.get("steps") or []
    idx = state.get("step_index", 0)
    if idx >= len(steps):
        return {"action": {"step_done": True, "why": "步骤已走完"}}

    step = steps[idx]
    sid = str(step.get("id") or "")

    # ══════════════════════════════════════════════════════════════════
    # ① 落点是不是已经满足了？（**必须排在 ⓪ 前面**）
    # ══════════════════════════════════════════════════════════════════
    # 上一步点完可能顺带就到了这一步的界面，此时不该再找控件点。
    #
    # ⚠️ 只在**本步还没动过手**时才预判（round_in_step == 0）。
    #    否则会出现致命的假通过：用例"从 A 到 A"（起点就在 A），
    #    每轮都判"落点已满足"→ 立刻完成 → 一步没走却报 PASS。
    #
    # ⚠️⚠️ 顺序不能反（168 实测踩到）：
    #
    #   原来是"先判前台是不是被测 App，再判落点"，于是出现一堆假告警：
    #     s3 当前前台不是被测 App（PhotoPickerGetContentActivity）…交给 AI
    #
    #   而 s3 是**验证步骤**（「验证可进入图片选择入口」），
    #   它落在系统图库里正是**预期结果**——已经到位了，根本不需要点任何东西。
    #   先判前台的话，永远走不到"落点已满足"，白白记一条"交给 AI"。
    #
    #   先判落点的道理很简单：**已经到目的地了，就不该再问"该往哪走"**，
    #   更不该因为"这块地不是我家的"而拒绝承认自己已经到了。
    untouched = state.get("round_in_step", 0) == 0
    if untouched and step.get("landing"):
        det, _det_why = is_deterministic_landing(step["landing"])
        if det:
            hit, why = match_landing(
                step["landing"],
                activity=str(state.get("last_activity") or ""),
                clickable=state.get("obs_clickable") or [],
                nodes_text=state.get("obs_nodes_text") or "",
                state_pkg=str(state.get("last_package") or ""))
            if hit:
                return {
                    "action": {"step_done": True,
                               "why": f"落点已满足，代码直接判定完成（{why}）"},
                    "strong_hits": [{"step": sid, "by": "code",
                                     "how": f"落点已满足({why})"}],
                    "progress": [_tick("strong",
                                       f"{sid} 落点已满足（{why}），跳过",
                                       step=sid)],
                }

    # ══════════════════════════════════════════════════════════════════
    # ⓪ 强证据只对**被测 App 自己的界面**生效
    # ══════════════════════════════════════════════════════════════════
    # 用户原话：「只有本 APP 的才用强证据。跳出去都是让 AI 判断吧。」
    #
    # 外部界面（系统图库、权限框、分享面板）不是我们的代码，
    # rid / activity 随时可能变，我们改不了却要拿写死的规则去判 ——
    # 168 就是活证据：a3 钉死 PhotoPickerGetContentActivity，
    # Android 17 换个分支给了 PhotoPickerUserSelectActivity 就误报。
    #
    # 分界是**归属**，不是**内容**：
    #   前台是被测 App → 结构稳定，强证据可靠，用代码判
    #   前台是别人家的 → 结构不稳定，交给模型看一眼，别硬判
    from_pkg, from_act = _foreground_owner(state)
    if not _is_sut(state, from_pkg):
        return _ask_model(
            state,
            f"{sid} 当前前台不是被测 App"
            f"（{from_pkg or '未知包名'}/{from_act or '未知界面'}）。"
            f"强证据只在被测 App 自己的界面里用 —— 外部界面的结构不受我们控制，"
            f"写死的 rid/activity 判不准。这一步由你看着屏幕判断该做什么。")

    # ══════════════════════════════════════════════════════════════════
    # ② 要点的东西在当前屏上有唯一位置吗？
    # ══════════════════════════════════════════════════════════════════
    target = str(step.get("target") or step.get("locate") or "").strip()
    if not target:
        return _ask_model(state, f"{sid} 没有声明 target，代码不知道点什么")

    x, y, how = _resolve_strong_target(target, state)

    if x is None:
        return _ask_model(state, f"{sid} {how}")

    # ★★★ 强证据直点的**退出条件** ★★★
    #
    # 用户原话：「如果强证据不能到预计那就让大模型介入。而不是一直无用的点。」
    #
    #   强证据 = **省一次模型调用**，不是"永远不叫模型"。
    #   一旦代码发现自己到不了预期（点完还是原样），就必须让模型接手。
    #
    # 第一版写歪过：加了"命中唯一就点"却没有任何退出条件，
    # 结果 176 死点「完成」37 次、白跑 70 秒。代码不知道
    # **"点得中"和"点了有用"是两件事**。
    #
    # 退出判据：连续 N 次**没有进展**（activity 变 / 可点清单变都算进展）。
    # 什么都不变 = 这个动作在当前界面状态下无效，继续点等于拿设备陪跑。
    _tries = _strong_tries(state, sid, target)
    if _tries >= STRONG_MAX_SAME_TARGET:
        _now = state.get("last_activity") or "(未知)"
        # ★ 输入框要单独说清楚（176 实测）：给模型的话若是「点了 N 次
        #   毫无变化，缺前置条件」，它看到"缺前置条件"就**又去输入一次**，
        #   形成死循环 —— 名字其实第一次就填进去了，它却以为没填上。
        _val = _current_input_value(state, target)
        if _val is not None:
            return _ask_model(
                state,
                f"{sid} 的输入框 {target} **已经有值了**：{_val!r}。"
                f"代码已经把同一个值又写了 {_tries + 1} 次，界面毫无变化 ——"
                f"说明内容是对的、不需要再输入。"
                f"**下一步不要再输入**，去做这一步真正剩下的事"
                f"（例如点「完成」/「保存」，或本来就该判定完成了）。")
        return _ask_model(
            state,
            f"{sid} 代码已经把这个目标点了 {_tries + 1} 次，"
            f"**每次点完界面都毫无变化**（一直停在 {_now}）。"
            f"说明「点它」这个动作在当前状态下根本不起作用 —— "
            f"不是坐标错了，也不是没点中，而是缺了前置条件。"
            f"代码不知道缺什么，这一步交给你判断。")

    return {
        "action": {
            "step_done": False, "next_x": x, "next_y": y,
            "why": f"强证据直点：{sid} → {target}（{how}）",
            "_strong": True,
        },
        "strong_hits": [{"step": sid, "by": "code", "target": target,
                         "how": how}],
        "progress": [_tick("strong",
                           f"{sid} 强证据命中唯一，直接点 ({x},{y}) [{how}]",
                           step=sid)],
    }


def route_after_strong(state: ChildState) -> Literal["act", "decide", "finish"]:
    """strong_target 之后：代码定了坐标就 act，没定就交模型。

    ⚠️ `_strong` 是这里的**唯一依据** —— 看 action 里有没有带坐标，
    而不是看 strong_hits。strong_hits 在"落点已满足"时也会写，
    那种情况下 action 里是 step_done，该走 check 不该走 act。
    """
    if state.get("abort") or state.get("status") == "BLOCKED":
        return "finish"
    a = state.get("action") or {}
    if a.get("_strong") and a.get("next_x") is not None:
        return "act"
    return "decide"


# --------------------------------------------------------------------------
# 节点 4/9：decide —— AI 决定下一步
#
# knowledge / vision 收在这里（模型说要查知识、要看图，都在本轮内做完）。
#
# 这次的关键改动：**把本步的预期也交给 AI**。
#
# 用户：「AI走的时候第一轮就边走边验证了。能拿到所有的记录。」
#       「AI第一次应该会根据要验证的东西点击看看吧」
#
# 也就是说——AI 在走这一步时就知道这一步要验什么，会顺手点开色板看颜色、
# 数一下色块。这不是"补证据"，这是人类测试员的正常动作。

#       「AI第一次应该会根据要验证的东西点击看看吧」
#
# 也就是说——AI 在走这一步时就知道这一步要验什么，会顺手点开色板看颜色、
# 数一下色块。这不是"补证据"，这是人类测试员的正常动作。
# --------------------------------------------------------------------------

def decide_node(state: ChildState) -> dict[str, Any]:
    """让 AI 决定这一步的下一步动作。

    ## 这一版真正变了什么

    旧图把模型的三种"额外请求"拆成了三个节点：
        need_knowledge -> knowledge 节点 -> 再回 decide
        vision_question -> vision 节点   -> 再回 observe
    于是图上有 15 个节点，而其中三个只是 decide 的分支产物。

    现在它们被收进 decide 内部：模型说要查知识，**就在这一轮里查完**；
    说要看图，**就在这一轮里看完**，然后立刻再问一次模型。
    对外还是一个节点，但少了两轮图调度。

    ## 视觉那条为什么必须带失败上限

    保护 5/6：167 实测模型连问 21 次图、白烧 100 多秒，
    因为它**不知道"这次没看成"** —— 它手上一直有个合法动作，
    于是下一轮又问同一个问题。

    所以：失败计数进 state，连续失败 VISION_MAX_FAILURES 次后
    **把 vision_question 从 action 里摘掉**，并明确告诉它别再问了。
    只报错不摘动作没有用 —— 这是 167 连挂两次的教训。

    ## 其它部分

    7 类输出契约、强证据直点、起点纠正、知识卡目录这些调好的东西
    仍然走旧实现（_base_decide_node），因为这次改的是"什么时候验"，
    不是"怎么决策"。
    """

    # ── 第一步：让旧 decide 拿到模型的动作 ──
    out = _base_decide_node(state)

    action = out.get("action") or {}
    if not isinstance(action, dict):
        return out

    # ── 处理 need_knowledge：在这一轮里查完 ──
    if action.get("need_knowledge"):
        return _handle_knowledge(state, out, action)

    # ── 处理 vision_question：在这一轮里看完 ──
    if action.get("vision_question"):
        return _handle_vision(state, out, action)

    return out


def _handle_knowledge(state: ChildState, out: dict[str, Any],
                      action: dict[str, Any]) -> dict[str, Any]:
    """模型要查知识卡 -> 就地查，结果写回 action，让它下一轮用得上。

    保护 4：查询次数有上限。168 实测模型会无限换词检索却不推进。
    """
    asked = list(state.get("knowledge_asked") or [])
    kw = str(action.get("need_knowledge") or "").strip()
    if len(asked) >= MAX_KNOWLEDGE_LOOKUPS:
        clean = {k: v for k, v in action.items() if k != "need_knowledge"}
        return {**out, "action": clean,
                "issues": [f"知识检索已达上限 {MAX_KNOWLEDGE_LOOKUPS} 次，"
                           f"要求模型用现有信息决定（最后一次查的是「{kw}」）"]}
    if kw and kw in asked:
        return {**out, "action": {**action, "need_knowledge": ""},
                "issues": [f"「{kw}」已经查过了，换一种说法或直接决策"]}
    files = (state.get("task") or {}).get("knowledge_files") or []
    text = search_knowledge(_ctx().skill_root, files, kw, limit=12) if kw else ""
    return {
        **out,
        "action": {**action, "need_knowledge": "", "knowledge": text},
        "knowledge_asked": asked + [kw],
        "knowledge_used": [{"keyword": kw, "hit": text[:400]}] if text else [],
    }


def _handle_vision(state: ChildState, out: dict[str, Any],
                   action: dict[str, Any]) -> dict[str, Any]:
    """模型要看图 -> 就地看完，答案写回 action。

    保护 5/6：连续失败到上限就**摘掉 vision_question**，
    否则它会一直问（167 连问 21 次）。
    """
    from .vision import VisionError, ask_image

    q = str(action.get("vision_question") or "").strip()
    fails = int(state.get("vision_failures") or 0)
    # 超上限 -> 摘动作，明确告诉模型别再问
    if fails >= VISION_MAX_FAILURES:
        clean = {k: v for k, v in action.items()
                 if k not in ("vision_question", "vision", "vision_error")}
        return {**out, "action": clean,
                "issues": [f"视觉通道连续失败 {fails} 次，已强制停止看图，"
                           f"要求用现有信息判断（最后原因："
                           f"{state.get('vision_error') or '未知'}）"]}
    shot = state.get("last_shot") or ""
    if not shot:
        return {**out, "action": {**action, "vision_question": ""},
                "vision_failures": fails + 1,
                "vision_error": "拿不到截图（last_shot 为空）",
                "issues": ["这次没看成：拿不到截图"]}
    try:
        answer = ask_image(_ctx().python, _ctx().skill_root, shot, q)
    except VisionError as exc:
        return {**out, "action": {**action, "vision_question": ""},
                "vision_failures": fails + 1, "vision_error": str(exc),
                "issues": [f"这次没看成：{exc}"]}
    return {**out,
            "action": {**action, "vision_question": "", "vision": answer},
            "last_vision": answer,
            "vision_failures": 0, "vision_error": ""}


# --------------------------------------------------------------------------
# 节点 2/8：plan —— 进入一步（含观察）
#
# 合并了旧版的 plan + observe。
# --------------------------------------------------------------------------

def plan_node(state: ChildState) -> dict[str, Any]:
    """进入当前步骤 + 看一眼屏幕（旧图的 plan + observe）。

    ## 为什么合并

    旧图是 plan -> observe 两个节点，但 observe 只做一件事：看一眼屏幕。
    拆成两个节点意味着每次都要多一轮图调度，没有收益。

    ## ⚠️ 这个节点**必须真的观察**，不能只调旧 plan

    我第一版就是只转调旧 plan_node（它只准备上下文、不取画面），
    结果 `last_shot` 永远是空的 —— 模型要看图时拿不到截图，
    `ask_vision` 永远失败，于是 80 轮全在原地空转。
    真机日志：`plan×81 / decide×80 / act×0`。

    ## 保护 19：崩溃检测就在这里

    工具层检测到被测包崩溃时会 terminate_session，
    之后 observe/act 全被拒。图若不认这个信号，会疯狂重试到轮次上限，
    报出误导性的"未收敛"。所以这里直接转 BLOCKED。
    """
    from ..device.bridge import ToolError

    # ① 旧的 plan 逻辑：切步、清计数、准备上下文
    base = _base_plan_node(state)
    # 步骤走完了就没有"当前屏"要看了
    if not base.get("last_step_desc"):
        return base

    # ② 观察当前屏
    dev = _ctx().device
    try:
        obs = dev.observe_raw()
    except ToolError as exc:
        msg = str(exc)
        if "terminated" in msg.lower() or "incomplete recording" in msg.lower():
            return {**base, "status": "BLOCKED",
                    "summary": f"会话被硬终止：{msg}",
                    "issues": [f"observe 被拒（硬终止）：{msg}"]}
        return {**base, "status": "BLOCKED",
                "summary": f"observe 失败（拿不到屏幕，没执行完）：{exc}",
                "issues": [f"observe 失败：{exc}"]}

    shot = (obs.get("evidence") or {}).get("png") or ""
    common = {
        "last_activity": obs.get("activity") or "",
        "last_shot": shot,
        "last_event_ids": [str(e) for e in (obs.get("event_ids") or [])],
        "obs_clickable": obs.get("next") or [],
    }
    # 崩溃 -> 硬终止（保护 19）
    if obs.get("terminate") or obs.get("blocked"):
        reason = (obs.get("terminate_reason")
                  or (obs.get("crash") or {}).get("type") or "被测包崩溃")
        return {**base, **common, "status": "BLOCKED",
                "summary": f"检测到崩溃，硬终止：{reason}",
                "issues": [f"crash terminate：{reason}"],
                "crash": obs.get("crash")}

    return {
        **base, **common,
        "obs_nodes_text": _screen_text(obs),
        "obs_nodes": _brief_nodes(obs.get("nodes")),
        "obs_xml_path": _screen_xml_path(obs),
        "step_evidence": _step_evidence_of(state, obs),
        "seen_rids": _seen_rids(obs.get("next"), obs.get("nodes")),
        "seen_texts": _seen_texts(obs.get("next"), obs.get("nodes")),
        "seen_screens": [_screen_text(obs)],
    }


# --------------------------------------------------------------------------
# 节点 1/8：prep —— 准备
#
# 合并了旧版的 prep + start_check + prep_apply。
# 复用旧实现：里面全是前提解析的边界情况（散文式 do、skip_if、
# 起点不在被测 App 时自动拉起），重写风险高、收益低。
# --------------------------------------------------------------------------



# --------------------------------------------------------------------------
# 节点 7/8：learn —— 回写 JSON
# --------------------------------------------------------------------------

def learn_node(state: ChildState) -> dict[str, Any]:
    """根据**这一轮的记录**生成 case JSON。

    用户：「第一轮就边走边验证了。能拿到所有的记录。
           然后最后根据记录写json。而不是回过头就复验证。」

    记录有两个来源：
      · findings       每条预期怎么判的（含 actual）
      · clicks.jsonl   每一击的原始记录（含为什么点、点完变没变）
    两者拼起来就是"这个用例到底是怎么走的"，回写 JSON 就是从里面提炼。

    ## ⚠️ 必须补 status

    `build_learned_case` 读的是 `state["status"]`，可 status 是 **finish**
    节点才算出来的，而 learn 跑在 finish **之前** —— 直接读会拿到空字符串，
    JSON 里 status 就是空的（run 85 就是这样）。

    所以这里按和 finish 一样的口径先算一遍。
    口径：有一条 FAIL 就是 FAIL；没验完/被阻断是 BLOCKED；否则 PASS。
    """

    # 先补 status，再回写
    st = str(state.get("status") or "")
    if not st:
        fs = [f for f in (state.get("findings") or [])
              if f.get("kind") == "assert"]
        total = len((state.get("task") or {}).get("asserts") or [])
        if any(f.get("status") == "FAIL" for f in fs):
            st = "FAIL"
        elif len(fs) < total or any(f.get("status") == "BLOCKED" for f in fs):
            st = "BLOCKED"
        else:
            st = "PASS"
    # ⚠️ usable 必须在**写盘之前**就放进 state 里带上，
    #    因为 _base_learn_node 是先写文件再返回的 —— 事后再改 dict，
    #    文件里已经是旧内容了。
    out = _base_learn_node({**state, "status": st})
    return out


# --------------------------------------------------------------------------
# 节点 8/8：finish —— 出结论
# --------------------------------------------------------------------------









# --------------------------------------------------------------------------
# 节点对外名（复用的旧实现）
#
# 这三个节点这次**没有新逻辑**，直接复用旧实现：
#   act    —— 400 行设备交互的坑（输入框已有内容别再填、坐标合法性、key 回退）
#   prep   —— 前提解析的边界情况（pm clear / goto / prepare 的 skip_if）
#   finish —— 出结论、写报告
# 合并进同一个文件后，旧实现改名为 _base_*，这里把对外名接回去。
# --------------------------------------------------------------------------

act_node = _base_act_node
prep_node = _base_prep_node
finish_child_node = _base_finish_child_node


# --------------------------------------------------------------------------
# 建图
# --------------------------------------------------------------------------

def build_child_graph(ctx, checkpointer=None):
    """子 Agent 的图 —— 8 个节点。

    ## 流程

        prep        准备（前提 + 起点）
          |
        plan        进入一步 + 观察
          |
        decide      AI 决定动作（<-- 含知识/视觉/强证据直点）
          |
        act         执行
          |
        check       这一步走完了吗
          |
          +-- 没走完 --> plan（重试，撞轮次闸门）
          +-- 走完了，但有预期 --> verify（就地验！）
          +-- 走完了，没预期   --> plan（下一步）
                  |
                verify  一条一条验本步的预期
                  |
                  +-- 还有 --> verify
                  +-- 没了 --> plan（下一步） / finish（没了）

    ## 和旧图的区别

    旧图 15 个节点，其中 strong_target / observe / knowledge / vision
    四个全是 decide 的**分支产物**（模型说"我要查知识"/"我要看图"，
    就被拆成了独立节点）。现在收进 decide 内部，对外还是一个节点。

    真正重要的是这一步：**check → verify → plan**。
    旧图是 check → plan → ... → 全部走完 → assert → verdict（回头验），
    新图是 check 判定完成时**不推进**，先验完这一步的预期再推进。

    ⚠️ 为什么 verify 要能在原地自环（保护：一步可能有 7 条预期）：
       s4 有 a7~a13 七条。如果每条都回 plan/decide 一次，
       等于每验一条就打断流程重新决策 —— 那不是"这一步验完再走"。
    """
    from langgraph.graph import END, START, StateGraph

    set_context(ctx)

    b = StateGraph(ChildState)
    b.add_node("prep", prep_node)
    b.add_node("plan", plan_node)
    b.add_node("strong_target", strong_target_node)
    b.add_node("decide", decide_node)
    b.add_node("act", act_node)
    b.add_node("check", check_node)
    b.add_node("verify", verify_node)
    b.add_node("learn", learn_node)
    b.add_node("finish", finish_child_node)

    b.add_edge(START, "prep")
    b.add_edge("prep", "plan")

    # plan 之后先让**代码**试一次（不花 token），定不下来才问模型。
    b.add_conditional_edges("plan", route_after_plan,
                            {"strong_target": "strong_target",
                             "decide": "decide",
                             "learn": "learn",
                             "finish": "finish"})
    b.add_conditional_edges("strong_target", route_after_strong,
                            {"act": "act",
                             "decide": "decide",
                             "finish": "finish"})
    b.add_conditional_edges("decide", route_after_decide,
                            {"act": "act",
                             "check": "check",
                             "plan": "plan",
                             "finish": "finish"})
    # act 之后一律去 check：动作执行完就该判"这一步完了没有"。
    # 旧图这里还有 strong_target / observe / start_check 三条分支，
    # 那是因为它把"纠正起点"也放进了主循环；新图起点在 prep 一次做完
    # （prep 复用旧实现，含 start_check 的纠正逻辑）。
    b.add_edge("act", "check")
    b.add_conditional_edges("check", route_after_check,
                            {"plan": "plan",
                             "verify": "verify",
                             "learn": "learn",
                             "finish": "finish"})
    b.add_conditional_edges("verify", route_after_verify,
                            {"verify": "verify",
                             "plan": "plan",
                             "learn": "learn",
                             "finish": "finish"})
    b.add_edge("learn", "finish")
    b.add_edge("finish", END)

    return b.compile(checkpointer=checkpointer)
