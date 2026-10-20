---
name: android-case-planner
description: 主 Agent 用于把 Android GUI 测试需求拆成可执行的用例结构（步骤/落点判据/断言/前提），再派发给执行子 Agent。适用于"跑某个用例""验证某功能""复现某 bug"这类需求。你只负责拆解和派发，不操作设备。
---

# Android 用例：拆解与派发（主 Agent 专用）

## 你的职责只有两件事

1. **拆解**：把需求变成结构化的 `steps` / `asserts` / `preconditions`
2. **派发**：交给执行子 Agent

**你不执行用例。** 主 Agent 调用 `android_case` 会被**代码直接拒绝**
（按 `delegationDepth` 判断，主 Agent = 0 → 拒，子 Agent = 1 → 放行）。

> 这不是提示词约束，是**运行时闸门** —— 模型的自觉性绕不过去。
> 拒绝时会告诉你该怎么改。别在报错后原地重试，改用 subagent 派发。
>
> 为什么这样设计：执行节奏、坐标合法性、落点复核、死循环阻断
> 都在子 Agent 那侧的执行器里。主 Agent 直接调等于把这些纪律全绕过。


## 第一步：先看有没有沉淀好的用例

**优先复用，不要重新拆。** 已经验证过的用例比现场拆的可靠得多
（里面有跨版本踩出来的坑、验证过的落点判据）。

用 `glob` 查：

```
glob("**/storage/cases/*.yaml")
glob("**/storage/cases/*联想日历*")
```

- **找到了** → 直接派发，`case_name` 传名字片段即可（见下）
- **没找到** → 走第二步现场拆解

YAML 里 `confidence: verified` 表示已经跑通过多次，可信度高。

## 第二步（没现成用例时）：现场拆解

### 拆解产物长什么样

```json
{
  "case_id": "168",
  "title": "联想日历_168_图库导入入口",
  "package": "com.zui.calendar",
  "preconditions": [
    {"kind": "data", "desc": "清理 APP 数据",
     "do": "pm clear com.zui.calendar", "undo": null}
  ],
  "steps": [
    {
      "id": "s1",
      "do": "点右上角「更多」",
      "locate": {"rid": "iv_more"},
      "landing": {"activity": ".timetable.display.TimetableActivity"}
    }
  ],
  "asserts": [
    {"id": "a1", "desc": "进入课程表页面",
     "check": {"activity": ".timetable.display.TimetableActivity"}}
  ]
}
```

### `landing` 是质量的关键

**每一步都必须给 `landing`。** 没有它，执行器无法判断这一步到底成没成，
只能问模型 —— 那就退化回"靠模型自觉"了。

`landing` 支持的判据：

| 判据 | 例子 | 说明 |
|---|---|---|
| `activity` | `.timetable.display.TimetableActivity` | 页面类名。**是子串包含匹配**，短名必须真的包含在完整名里 |
| `rid` | `btnImportFromGallery` | 控件资源 id |
| `text` | `图库导入` | 屏幕文字 |
| `package` | `com.android.providers.media.module` | 弹窗/浮层的来源包（跨 App 跳转时用） |

多个条件用**逗号**（AND）或**竖线**（OR）。

### 拆解纪律（这几条是踩出来的）

1. **`locate` 优先写 `rid`，图标按钮必须给 `locate`**
   纯图标按钮没有文字，模型看不出它是干什么的。

2. **别把弹窗的按钮当落点，但权限要写进断言**
   点"允许"之后界面没变，`landing` 会判失败。**点弹窗**是执行器的事。
   但**权限该不该弹、允许/拒绝后什么表现**，是用例的**预期**，必须写进 `asserts`。
   见下面「权限是预期的一部分」。

3. **断言要可核对**
   `check` 写成 `activity` / `rid` 这类结构化判据，不要写成"页面正常显示"。
   结构化判据代码能核，自然语言只能再问一遍模型。

4. **前提该清就清，但要能恢复**
   `kind: "data"` 表示会改设备数据（如 `pm clear`），必须写 `undo`。
   用户会看到"我设备上的日历数据被清了"这类副作用。

5. **不确定的写在 `pitfalls`，别猜**
   每步可以带 `pitfalls`（这一步容易踩什么）。宁可多写一句提示，
   也不要留一个错误的坐标。

### ★ 权限是预期的一部分

**最容易漏、也最容易判错的一类。** 用例说"点拍照导入能进相机"，
隐含的前提是**被测 App 要有相机权限**。

拆解时读完用例，把**词 → 权限**对上：

| 用例里的词 | 权限 |
|---|---|
| 图库 / 相册 / 照片 / 图片 | `READ_MEDIA_IMAGES`（部分照片为 `READ_MEDIA_VISUAL_USER_SELECTED`） |
| 拍照 / 相机 / 扫码 | `CAMERA` |
| 文件 / 导入 | 存储 |
| 录音 / 麦克风 | `RECORD_AUDIO` |
| 位置 / 定位 | `ACCESS_FINE_LOCATION` |

然后**写进 `asserts`**，两种分支都要：

```json
"asserts": [
  {"id": "a1", "desc": "日历申请相机权限，允许后能打开相机",
   "check": {"permission": {"CAMERA": true}, "package": "com.zui.camera"}},
  {"id": "a2", "desc": "日历申请照片权限，允许后可进入选择界面",
   "check": {"permission": {"READ_MEDIA_IMAGES": true}}}
]
```

**`permission` 判的是被测 App 的**授权状态**（`dumpsys` 里的 granted），
不是"屏幕上弹过什么框"。
写成 `{"CAMERA": true}` 表示"日历已被授予相机权限"，`{"CAMERA": false}` 表示"未授予"。

> 为什么判结果不判弹窗：用户要的是「同意了才能打开相机」——
> 弹窗只是过程，授权才是结果。而且系统权限框是系统的（包名固定
> `com.android.permissioncontroller`），App 弹窗是 App 自己的，
> 拿两者当"权限给没给"的判据都会判错。

**注意判据的边界**（代码只认这几个，写了别的等于没写）：

| 能写 | 代码怎么判 |
|---|---|
| `activity` | 子串包含匹配 |
| `rid` | 控件在不在 |
| `package` | **相等**匹配前台包名 |
| `text` | 屏幕文字包含 |
| `permission` | 权限框弹过没有（只认被测 App 的） |

⛔ **不要写** `dialog_present: {permission_dialog: ...}` 这类——
没有 `rid` 会被丢弃（虽然现在会报警，但那条断言仍然没验）。

**关键：权限只看被测 App 的。** 判归属**只看包名** ——
系统权限弹窗包名固定 `com.android.permissioncontroller`，
App 自己的弹窗包名就是被测 App。不要用弹窗文案去猜（文案会骗人）。
把两者混在一起，就会出现"权限明明弹了却判 FAIL"。

> 真实案例（169）：用例预期写"点拍照导入应弹相机权限弹窗"，执行时
> 日历的权限框确实弹了、也允许了、相机也打开了 —— 但因为**没有出现相机
> App 的权限框**，被判成 FAIL。看错对象了。


## 第三步：派发

用 `subagent` 工具。**把拆解结果完整放进 prompt**，因为子 Agent 不继承你的上下文。

```
subagent(
  prompt: """
    执行这个 Android 用例，用 android_case 工具。

    case_name: 168          ← 有现成 YAML 时用这个，最省事
    或者
    用例结构: {上面那份 JSON}   ← 现场拆解时用这个

    要求：
    - 直接跑，不用加别的开关（代码会自动先试强证据直点）
    - 把报告的 status / engine / steps_done / issues 原样转述回来
    - steps_done=0 表示一步都没执行，这时不要说"功能正常/不正常"
  """
)
```

### 派发时必须交代的三件事

1. **用哪个用例**（`case_name` 还是内联结构）
2. **用例内容别改** —— 判据（`landing` / `asserts`）是验收标准，
   子 Agent 只负责跑，跑不通就照实报，不许迁就结果改判据
3. **报告怎么转述**（见下）

不需要给子 Agent 加任何"加速开关"：执行器**每一步都先在当前屏找唯一命中**，
命中就代码直点、找不到才叫模型。报告里的 `auto_rate` 就是自动化率。

## 报告怎么读、怎么转述

子 Agent 回来后，先看三个字段：

| 字段 | 怎么用 |
|---|---|
| `steps_done` | **= 0 表示一步都没跑**。这时不许产出"功能正常/不正常"的结论 |
| `engine` | `autopilot` = 没调模型，只验了 YAML 结构，**不是用例结论** |
| `status` | 见下表 |

### ★ 三个状态的含义（转述时不能混）

| 状态 | 含义 | 能不能说"功能有问题" |
|---|---|---|
| **PASS** | 用例与实际**一致**：所有步骤走完 + 所有断言成立 | 可以说"正常" |
| **FAIL** | 与实际**有差别**，但**所有路径都执行完了**（如按钮文字期望 AAA 实际 BBBB） | **可以**，这就是产品缺陷 |
| **BLOCKED** | 用例**没执行完**：崩溃，或关键路径没找到（如点 A 应跳 B，实际停在 C） | **不可以** —— 是没跑完，不是功能坏 |

- **BLOCKED 之后执行器会直接停下**，后面的步骤不会跑
- **FAIL 和 PASS 一定表示所有路径都跑完了** —— 所以"只走了 2/3 步"不可能是 FAIL

⚠️ 最常见的错误是把 BLOCKED 转述成"这个功能有问题"。
BLOCKED 只说明"没走到/没跑完"，要么是路径变了，要么是设备环境问题 ——
**需要人来判断**，不是直接当缺陷报出去。

**必须如实转述，不许粉饰：**

- ✅ "3 步通过，第 4 步 FAIL：按钮文字期望 AAA，实际 BBBB"
- ✅ "BLOCKED：点「从图库导入」应进入图片选择器，实际停在日历首页（试了 3 次）"
- ❌ "用例基本通过，有个小问题"（含糊会让人误判）
- ❌ 把 BLOCKED 说成"功能不正常"（没有依据 —— 根本没走完）

如果 `status` 是 `BLOCKED`/`ERROR`，明确说"这次没跑成，原因是什么"，
**不要**说成"功能有问题"——那是两件完全不同的事。

### 先自查：我到底有没有真的跑起来？

`steps_done == 0` 且 `BLOCKED`/`ERROR` → **一步都没执行**。
这时**不许**产出任何用例结论，只能说"没跑起来，原因是什么"。
只有 `steps_done > 0` 的 PASS/FAIL 才是结论。

### `playbook` 和 `confidence` 不报警告

它们是注解，本来就不进执行通路：

- `playbook`：旧文件里的「执行须知」。**「按步读坑」这条元规则已生效** ——
  坑一律按步给出，走到哪步就只看那步的 `pitfalls`。为什么：回放按步走，
  第 1 步的坑跑到第 2 步还在眼前会**稀释信息**，真正相关的那条反而看不见。
- `confidence`：用例**历史可信度**，**不参与判定**。`level != verified` 时
  （如 `single-run`/`flaky`），这次 PASS **不代表功能稳定**。
  报告的 `case_confidence` 字段带 `level`/`reason`/`todo`。

## 常见误判（别踩）

- **`steps_done=0` + `BLOCKED`** → 没跑起来，不是功能有问题
- **`engine=autopilot` 的 PASS** → 只说明 YAML 结构没问题，功能没验证
- **设备锁屏/掉线** → 表现为"点了没反应"，看 `issues` 里有没有设备相关字样
- **`content.some is not a function`** → 工具层 bug，不是你的问题，
  把原始报错转给用户
