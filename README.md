# dsh-android-ai-test

在 Android 真机上按用例执行 GUI 测试。

**代码负责「能完全确定的部分」，模型负责「不能完全确定的部分」。**

这条分工是整个项目的核心：能用代码判死的绝不问模型（快、稳、可复现），
判不了的才交给模型（灵活、能应付没见过的界面）。两者之间是**唯一命中就代码点，
否则交模型**这一条硬规则，而不是模型自觉。

---

## 怎么跑

```bash
echo '{"case_name":"168"}' | .venv/Scripts/python.exe run_case_json.py
```

产物落在 `tmp/`：`report_168.json`（完整报告）、`report_168_subagent.json`、
`run_168.log`（逐轮日志）。

报告里有几条关键信息：

| 字段 | 含义 |
|---|---|
| `status` | PASS / FAIL |
| `steps_done` / 断言 | 「步骤 3/3，预期 3/3 已验证」 |
| `runtime_permissions` | 本次点过哪些系统权限（不可逆的现场变更，必须留痕） |
| `runtime_permission_prompts` | 具体授予了哪些权限（读屏文案） |
| `assert[...].actual` | 每条断言的判定依据，标明是代码判的还是模型判的 |

日志末尾会打印**自动化率**：

```
🎯 强证据：代码直点 4 次 / 交给模型 3 次（自动化率 57%）
   · 交给模型原因：s1 目标不在当前屏：rid=iv_more 没找到
```

这个数字是工具好不好用的直接指标。掉到 0% 说明强证据机制没生效。

## 看结果（WebUI）

```powershell
.\start-webui.cmd                 # 起服务 + 自动开浏览器，默认 8770
.\start-webui.cmd -Port 8771      # 换端口
.\start-webui.cmd -NoBrowser      # 只起服务
```

或者直接 `python webui.py`。零依赖，标准库；**重复启动不会报错**，
已开的情况下只是把浏览器再指过去。

页面上能看到：

| 区块 | 内容 |
|---|---|
| 总览 | 各状态数量、每个用例最近一次结果、最近运行 |
| 运行记录 | 历次运行列表（历史不再互相覆盖） |
| 断言结果 | 每条断言 PASS/FAIL + 判定依据（代码判的还是模型判的） |
| 自动化率 | 代码直点 vs 交给模型的比例，附降级原因 |
| 动作时间轴 | 每次点击/输入/按键：目标、点前点后页面、耗时、是否触发变化、**截图** |
| 日志 | 完整运行日志 / 原始账本 jsonl / 模型对话 |

每次运行都会落一条历史到 `assets/storage/runs.db`（本地数据，不入库），
所以「同一用例上次过没过、这次为什么变了」查得到。

> ⚠️ `start-webui.ps1` / `.cmd` 必须保持**纯 ASCII**。
> Windows PowerShell 读无 BOM 的 `.ps1` 按 GBK 解析，中文会被读成乱码
> 并当成命令执行，症状是双击后窗口一闪就没了。
> `tests/test_launcher.py` 盯着这一点。

---

## 架构

主 Agent 拆解用例 → 子 Agent 执行。**设备操作只在子 Agent 里**，
主 Agent 永远不碰设备。

```
主 Agent（拆用例、派任务）
    │  用例结构 + 知识卡路径 + 已有 YAML 路径
    ▼
子 Agent（run_case_json.py）
    │
    ├─ 前提（pm clear / 拉起 App）
    ├─ 逐步骤执行 ──┐
    │               ├─ 强证据直点（代码）
    │               ├─ 判不了 → 模型决策
    │               └─ 视觉模型（颜色/布局）
    └─ 断言判定（代码优先）
```

### 目录

```
langgraph-poc/
├── run_case_json.py          ← 入口：stdin 收 {case_name}，stdout 出报告 JSON
│
├── src/android_agent/
│   ├── cases/                用例层：读 YAML、算路径
│   ├── device/               设备层：唯一允许碰 adb 的地方
│   │   └── direct.py           进程内设备实现（含权限就地处置）
│   ├── agent/                Agent 层：LangGraph 状态机
│   │   ├── nodes.py            子 Agent 节点（判定逻辑都在这里）
│   │   ├── graph.py            用例级图
│   │   ├── runner.py           interrupt 循环驱动、死循环检测
│   │   └── decide.py           决策通道（渲染 prompt、校验回复）
│   └── report/               报告层
│       ├── writeback.py        YAML 回写（只写代码直点的动作）
│       └── generate.py         首次跑完后生成 YAML
│
├── assets/
│   ├── tools/                  工具层：observe / act / perm / vision ...
│   ├── knowledge/              知识卡（每个包一份，供子 Agent 查询）
│   └── storage/cases/          用例 YAML
│
├── scripts/                  一次性工具脚本（审计、调试）
├── tests/                    测试
├── tmp/                      临时产物（不进版本控制）
└── docs/执行机制.md            ★ 架构与规则的完整说明
```

---

## 用例状态：PASS / FAIL / BLOCKED

三个状态的边界是死的：

| 状态 | 含义 | 判据 |
|---|---|---|
| **PASS** | 用例与实际**一致** | 所有步骤走完 + 所有断言成立 |
| **FAIL** | 与实际**有差别**，但所有路径都**执行完了** | 步骤全走完、断言全验到，其中有断言不成立 |
| **BLOCKED** | 用例**没执行完** | 崩溃、关键路径没找到、或断言没验全 |

两条铁律：

1. **报 BLOCKED 就不再执行后面的步骤** —— 没执行完，后面的结论没有依据。
   路由层强制：状态为 BLOCKED 时直接去 `finish`，不会再多点一次设备。
2. **报 FAIL 或 PASS，一定表示所有路径都跑完了** ——
   所以"3 步只走了 2 步"永远不可能是 FAIL。

| 现场情况 | 状态 |
|---|---|
| 点 A 应跳 B，实际停在 C（试满 3 次） | BLOCKED |
| 按钮文字期望 AAA，实际 BBBB | FAIL |
| 3 步走完，5 条断言只验到 3 条 | BLOCKED |
| App 崩溃 | BLOCKED |
| 全部走完且全部成立 | PASS |

**落点不符最多试 3 次**（`LANDING_MAX_TRIES`）。1 次太急（页面还在加载会误判），
再多没意义（同一动作三次都到不了，说明路径本身不成立）。
试满后 BLOCKED，并记录实际停在哪一页，便于人判断是不是产品缺陷。

---

## 判定：三档

| 证据强度 | 手段 | 用在哪 |
|---|---|---|
| 完全确定 | 代码（activity / rid / xpath 唯一命中） | 直点、落点判定、断言 |
| 半确定 | OCR（RapidOCR） | XML 里看不到的文字（图片上的字） |
| 不能确定 | 视觉模型 | 颜色、背景、布局这类 XML 无描述的东西 |

**「判不了」绝不等于「已完成」。** 没有可靠判据时不能默认跳过这一步。
唯一命中是硬门槛：0 个命中或多个命中都交模型，绝不「取第一个」。

---

## 强证据直点

YAML 里每一步写 `target`，表示**这一步要点哪个控件**：

```yaml
- id: s1
  target: rid=iv_more
  landing: activity=.timetable.display.TimetableActivity
  desc: 点首页右上角「更多」按钮，进入课程表页面
```

执行时按当前屏去找这个 target：

- **唯一命中** → 代码直接点（不等模型，不受 6 秒自动消失影响）
- **0 个命中** → 目标不在这一屏，交模型
- **多个命中** → 歧义，交模型

这不是「回放」。回放是把坐标或动作序列重放一遍，换个分辨率就废；
这里每次都是**当场按当前屏重新判定**，判据是结构化的（rid / xpath / activity）。

### 回退给模型是「按屏」的，不是永久的

某一屏认不出，只说明这一屏认不出。屏幕一变（换 activity、换包名、
可点控件集合变了），必须重新给代码一次机会。

早先的 bug：代码判不出 → 交给模型 → 记下「本步已交模型」→ 之后每轮都读到这个标记
→ 再也不回代码。结果 80 轮全是模型、代码直点 0 次，整套机制一次没跑起来。
修法是把标记绑定到**当屏指纹**，换屏即失效。

### 轮次计数只在换步时归零

放在「每次进入步骤」都归零的话，依赖它的判断会永远成立 ——
用例写成「从 A 到 A」时，一步没走却报 PASS。

### 弹窗豁免有上限

模型每轮都能申报「这是弹窗，不算连点」，无限豁免等于没有死循环检测。
实测踩到：在「取消」和「前往设置」之间来回点 7 次，跑到 80 轮上限。
修法是每屏最多豁免 4 次，用完就按普通动作计入连点/循环检测。

---

## 系统权限框

**这一类弹窗不是被测对象的业务，是 Android 平台的标准流程。**

系统权限框约 **6 秒没人点就自动消失**，而「该同意还是该拒绝」要读用例语义（慢）。
决策慢、窗口短 —— 所以必须在**动作返回的那一刻**就地处置：
点击/按键 → 立刻检测权限框 → 有就按意图点 → 框没了才继续观察。

### 归谁：只看包名

```
com.android.permissioncontroller   ← 系统权限框（跨设备通用）
其它包                              ← 被测 App 自己的界面/弹窗
```

不用文案猜：相机 App 弹的「相机正在尝试使用相机」里也有「相机」二字，
按文案判会把被测 App 自己的话当成系统申请。

### 两类权限分开判

| | 普通运行时权限 | 内容型权限 |
|---|---|---|
| 例子 | 通知、日历读写、设备信息 | 相机、照片视频、麦克风、定位 |
| 不给会怎样 | 某个功能不可用，**流程照走** | **流程根本走不下去** |
| 判定 | 记账即可，失败不阻断 | 必须确认**真的授予了**（查 dumpsys） |

「点过允许按钮」≠「授权成功」。dumpsys 查不到就如实记「未能确认」，
绝不默认成「已授予」。

### 只记账，不进 YAML

系统权限框是平台标准流程，每个 App 都一样 —— 写进用例 YAML 没有任何信息量，
只会让文件变脏、把平台通用行为冒充成这个 App 的经验。

**但点过什么必须留下** —— 授权是不可逆的现场变更，审计要查。所以分两个出口：

```
点过的权限   → report.runtime_permissions / runtime_permission_prompts
权限框动作   → 永不进入 YAML（writeback / generate 两道都过滤）
```

---

## 用例 YAML

YAML 是**一级参考，不是执行脚本**。它记录「这一步想做什么、做到什么算完」，
不记录怎么点。

```yaml
meta:
  case_id: "168"
  title: 联想日历_168_图库导入入口
  package: com.zui.calendar
  knowledge_files:
    - knowledge/com.zui.calendar.md

per_step_pitfalls: true      # 坑按步给，不汇总（防稀释）

preconditions:
  - kind: data
    do: pm clear com.zui.calendar
    skip_if: {activity: .AllInOneActivity}
  - kind: nav
    target: activity=.AllInOneActivity
    assert_start: activity=.AllInOneActivity

steps:
  - id: s0
    desc: 依次点掉 pm clear 后弹出的两个框，落回日历首页
    target: rid=button1
    landing: activity=.AllInOneActivity
    pitfalls:
      - "顺序不能反：先 button1「同意」，再 timetable_guide_button「我知道了」"
      - "权限说明框是日历自己的（包名仍是 com.zui.calendar），不是系统权限框"
  - id: s1
    desc: 点右上角「更多」（iv_more），在菜单里选「课程表」，进入课程表页
    target: rid=iv_more
    landing: activity=.timetable.display.TimetableActivity
    know:
      where: 首页 → 课程表
      why_this: 课程表入口不在首页直接露出来，藏在「更多」菜单里
    pitfalls:
      - "iv_more 没有 text/desc —— 用文案找「更多」会找不到，必须按 rid 定位"

asserts:
  - id: a1
    desc: 可打开课程表页面
    check:
      activity: .timetable.display.TimetableActivity
```

**写什么**

- `target` — 点哪个控件（`rid=` / `xpath=` / `text=` / `key=`）
- `landing` — 做到什么算完成，必须是**稳定的终态**
- `desc` — 这一步的意图，要具体（「点右上角更多打开菜单」，不是「点更多」）
- `pitfalls` — 这一步会踩什么坑，**跟着步走，不汇总**
- `know` — 这一步为什么这么点（`where` / `why_this` / `locate`）
- `preconditions` — `kind: data`（清数据）/ `kind: nav`（走到起点）
- `confidence` — 这条链路验证到什么程度、还差什么

**不写什么**

- ❌ 坐标 —— 换分辨率必失效
- ❌ `locate_seq` 坐标序列 —— 同上，且隐藏了真实意图
- ❌ 权限框动作 —— 标准流程，各 App 一样

不认识的字段会**报 warning**（不静默忽略），避免写了没生效。

### 落点判据必须是稳定终态

早先把「点按钮 → 点掉提示框」写成两步，第一步落点写 `rid=button1`（提示框的按钮）。
真机上就 FAIL 了：模型点完按钮顺手把框也点了，轮到第一步判定时框已经没了，
于是「声明完成但落点不符」—— 可三条断言其实全过。

**判据不能落在流程中途的一闪而过上。**

### 只有代码直点的动作会回写进 YAML

模型点的动作是**猜测**，不是经验。把猜测写进用例，下次就照着错误的路子走。

所以动作记录带 `by` 字段（`code` / `model`），回写只认 `by == "code"`。
实测踩到：回写把 s1 的 `iv_more` 改成了 `rid=button1`（那是 s0 的「同意」按钮）。

### 第一次跑完会生成 YAML

未知用例第一次跑，跑完自动落一份 YAML 作为下次的一级参考。

---

## 分层规则

**`device/` 是唯一能调 adb 的层。** 别在别处 `subprocess.run(["adb", ...])` ——
绕过它就失去了设备锁、事件账本和崩溃检测。

> 唯一例外：`run_case_json.py` 里有一次 `adb devices`，只为挑出唯一在线设备当
> serial，不点、不看、不改设备状态。真正的设备动作全在 `run_case()` 内部。

**`cases/` 不碰设备。** 给它一个用例名，它只回答「有哪些步骤、判据是什么」。

**`agent/` 负责编排。** 节点输入 `ChildState`，输出要更新的字段。

**判定集中在 `nodes.py`。** 能在代码里判死的都在这里判，这是分工的落点。

---

## 子 Agent：三层职责不靠自觉

主 Agent **拆解 + 派发**，执行子 Agent **跑**，执行器**兜底**。
这条分工不是靠提示词维持的，每一层都有代码闸门：

| 闸门 | 位置 | 拦住什么 |
|---|---|---|
| 深度闸门 | `runner-tool/lib/index.js` | 主 Agent（`delegationDepth=0`）调 `android_case` 直接被拒 |
| 工具面收窄 | `agent.cordis.yml` 的 `toolFilter.allow` | 子 Agent 只有 `android_case` + 只读工具；`maxDepth: 1` 不许再派孙 Agent |
| 设备锁 | `device/lock.py` | 两条用例同时跑同一台设备 → 后来者排队，不会互相踩 |

**为什么深度闸门不能靠"不给主 Agent 挂工具"**：子 Agent 的工具面是**继承**父方再收窄的
（`childOptions = { ...parent.options }`），主 Agent 不挂，子 Agent 也继承不到，
`toolFilter` 里写 `android_case` 会抛 "unknown global tool"，**根本派不出去**。
所以工具必须在，只能靠运行时判断"谁在调"。`delegationDepth` 是持久化且单调的，模型改不了。

> ⚠️ 别用提示词代替这些闸门。上面每一条都对应一次实测事故：
> 主 Agent 顺手自己跑了（子 Agent 从没被创建）、
> 子 Agent 以为自己是主 Agent 想再往下派（报 "subagent run failed"，连诊断都没有）、
> 两个子 Agent 同时点同一台设备（产出一份看起来正常的错误报告，比直接报错危险得多）。

### 仓库外的两个文件，同样决定子 Agent 行为

```
~/.dsh/.agent-presets/android-runner/agent.cordis.yml   工具面 + persona
~/.agents/skills/android-case-runner/SKILL.md           子 Agent 读的技能卡
~/.agents/skills/android-case-planner/SKILL.md          主 Agent 读的技能卡
```

它们不在 git 里，但**决定子 Agent 被教成什么样** ——
子 Agent 读技能卡、跑代码，两边说法不一致，它就会照着错的写。
所以 `config/` 下存了它们的**镜像快照**（可 diff、可回退）：

```powershell
python sync_config.py            # 改完原文件，同步进快照
python sync_config.py --check    # 只比对
```

`test_skill_card_consistency.py`（24 项）盯着两件事：
**技能卡不许教代码里不存在的东西**、**快照不许和真实文件不一致**。

---

## 路径规范

统一从 `android_agent.cases.paths` 取，别自己拼 `Path(__file__).parent / "assets"`：

```python
from android_agent.cases import paths
paths.skill_root()    # 资产根目录
paths.cases_dir()     # 用例目录
paths.REPO_ROOT       # 仓库根
```

自己拼的路径在文件挪位置后会**静默指错**（不报错，只是找不到东西）。

---

## 知识卡

`assets/knowledge/` 下每个包一份文档，子 Agent 按需查询：入口在哪、
有哪些坑、控件怎么找。

```
com.zui.calendar.md       主体知识
paths/com.zui.calendar.md 路径图（从哪能到哪）
_system.md                跨 App 的系统行为
_template.md              新建知识卡的模板
```

---

## 跑测试

```bash
# Python
for f in tests/test_*.py; do .venv/Scripts/python.exe "$f"; done

# JS
cd tests && for f in test_*.mjs; do node "$f"; done
```

当前 **51 个 Python 测试 + 7 个 JS 测试全部通过**。

其中四个专门盯着"不靠自觉"这件事：

| 测试 | 盯什么 |
|---|---|
| `test_subagent_chain.py` | 子 Agent 执行链完整：插件 → 执行器 → 设备层，导入不断、stdout 干净 |
| `test_subagent_contract.py` | 三层闸门都在：主 Agent 被拒、工具面收窄、报告字段齐 |
| `test_skill_card_consistency.py` | 技能卡和 preset 不许提代码里不存在的参数/字段 |
| `test_perm_autogrant.py` | 权限自动处置 + 只记账不进 YAML |

---

## 停止条件

**全是进度驱动，没有超时。** 超时会误杀慢但正常的流程。

| 条件 | 动作 |
|---|---|
| 轮次上限（80） | 停，报「没跑完」 |
| 同一点连点 3 次 | 停，报循环 |
| A→B→A 交替循环（≤3 个点、每个出现 ≥3 次） | 停，报循环 |
| App 崩溃 | 工具层返回退出码 3 → 硬终止，**禁止重试** |
| `executed=False` | **唯一**允许重试的条件 |

---

## 环境

- Python 3.13（`.venv/`）
- adb（platform-tools）
- 依赖见 `requirements.txt`；`lxml` 用于 XPath，`rapidocr` 用于 OCR

设备要求：已开启 USB 调试并授权。

---

## 文档

- **`docs/执行机制.md`** —— 架构与规则的完整说明，改判定逻辑前必读
- `docs/开发记录-踩坑.md` —— 反复踩过的坑（尤其「不要重复造工具层的轮子」）
- `assets/storage/cases/SPEC.md` —— 用例 YAML 的字段规范
