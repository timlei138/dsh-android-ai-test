# 用例 YAML 规范

> **这份规范以当前工具的代码为准，不从旧格式继承。**
>
> 依据三条，改这里之前先去核对它们：
> 1. 数据模型 `src/android_agent/agent/nodes.py` 的 `StepSpec` / `AssertSpec` / `Precondition`
> 2. 判据白名单 `src/android_agent/agent/nodes.py` 的 `KNOWN_CHECKS`
> 3. 落点表达式 `src/android_agent/agent/nodes.py` 的 `match_landing`
>
> ⚠️ 早先这里写的是 `child_agent.py` —— 那个文件早已不存在。

---

## 一条原则

**YAML 里只写工具真正认的字段。写了不生效 = 不许写。**

原因：不生效的字段不会报错，只会被跳过。于是报告说「3/3 断言已验证」，
实际有 1 条判据从没参与过判定 —— **用例"看着 PASS，其实少验了一大块"**。
静默失效比报错危险得多。

所以落盘前必须过检查器：

```powershell
cd D:\dsh\langgraph-poc
python assets\tools\case_spec_check.py --case 168
```

退出码 `0` 才能落盘。

---

## 顶层结构

```yaml
meta:          # 必填
preconditions: # 可选
steps:         # 必填，至少 1 步
assertions:    # 必填，至少 1 条
```

其它顶层键（`playbook` / `confidence` / …）**不认**，写了会被判「死字段」。

---

## meta

| 字段 | 必填 | 说明 |
|---|---|---|
| `case_id` | ✅ | 如 `"168"` |
| `title` | ✅ | 用例标题 |
| `package` | ✅ | 被测 App 包名 |
| `knowledge_files` | | 要注入的知识卡路径列表 |
| `perm_action` | | `grant` / `deny`，留空=默认同意。**只有测拒绝分支才写 deny** |
| `perm_name` | | 权限短名，如 `camera`，可留空 |

---

## preconditions

`kind` 只有三种 —— **注意这不是旧格式的 `clean`/`assert`/`goto`**：

| kind | 含义 | 要求 |
|---|---|---|
| `data` | 数据类清理（`pm clear`），清了没问题 | `do` 里写 shell 命令 |
| `env` | 环境/硬件（飞行模式、亮度） | **必须给 `undo`**，收尾无条件执行。模型层会强校验 |
| `nav` | 导航到某界面 | 建议给 `target`（去哪）+ `hint`（怎么走） |

### ★★ `pm clear` 只在「首次使用」场景才写

用户的原话很清楚：**「不是每个用例都需要 pm clear。只有涉及到首次使用，这种情况才清理。
其他情况不需要清理。」**

`pm clear` 会**不可逆地清掉真实账号数据**（重新登录、重新同步都回不来），
所以它不是"默认前提"，而是**只有在用例本身就在测首次启动时**才成立：

| 该写 `pm clear` | **不该**写 |
|---|---|
| 首次启动的权限说明框 / CTA 弹窗（03、167） | 进入课程表、看图库导入（168、176、177、183） |
| 首次进入某功能的引导页 | 列表页、详情页、编辑页的字段与交互 |
| 「首次使用 → 引导只出现一次」这类断言 | 「设为当前课程表」「删除课程表」这类操作 |

判据一句话：**把清数据去掉，这条用例还测得出它要测的东西吗？**
测得出 → 不该清；测不出（因为测的就是"第一次"）→ 才清，并在 `desc` 里写明为什么。

```yaml
# ✅ 该清：用例测的就是首次进入
preconditions:
  - kind: data
    desc: 首次打开日历（用例原文前提「可以清空APP数据」）—— 引导框只在清数据后首次启动出现
    do: pm clear com.zui.calendar

# ❌ 不该清：用例测的是编辑页字段，跟"第一次"无关
preconditions:
  - kind: data
    desc: 清数据
    do: pm clear com.zui.calendar      # ← 这里清掉的是用户真实的课程表数据
  - kind: nav
    desc: 起点
    target: 日历首页
    assert_start: activity=.AllInOneActivity
```

字段白名单：`kind` / `desc` / `do` / `undo` / `skip_if` / `assert_start` / `target` / `hint`

`skip_if` 只认 `activity` / `package` / `pkg`。写别的（如 `timetable_state`）**判不了**，
会保守照跑 —— 宁可多清一次，也不该清不清。

例：

```yaml
preconditions:
  - kind: data
    desc: 清数据回到首次使用态（用例原文前提）
    do: pm clear com.zui.calendar
  - kind: nav
    desc: 起点
    target: 日历首页
    assert_start: activity=.AllInOneActivity
```

---

## steps

字段：`id` / `desc` / `target` / `landing` / `pitfalls` / `know` / `on_fail`

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 不可重复 |
| `desc` | ✅ | 做什么，要具体（「点右上角更多打开菜单」，不是「点更多」） |
| `target` | | **点哪个控件**：`rid=` / `xpath=` / `text=` / `key=`。唯一命中就代码直点 |
| `landing` | | **落点判据**，字符串表达式。必须是**稳定终态**，不能是流程中途一闪而过 |
| `pitfalls` | | **该步专属的坑**，`list[str]`，每条坑一个字符串 |
| `know` | | 这一步为什么这么点（`where` / `why_this` / `locate`） |
| `on_fail` | | 失败了怎么办 |

> ⚠️ **不写 `do:` / `expect:` / `locate_seq`**。
>
> · `do:` / `expect:` 是旧格式，靠 `case_yaml.py` 事后翻译（该文件已不存在）。
> · `locate_seq` 是**按序重放**的旧机制，已整体删除 —— 它把「第 1 个点这个、
>   第 2 个点那个」写成剧本，本质还是回放，换个界面就废。
>
> 现在只有一条路：每步看当前屏，`target` 唯一命中就代码点，否则交模型。


### pitfalls：为什么值得认真写

坑是**按步喂给模型**的（`src/android_agent/agent/decide.py` 的 `pitfalls` 渲染进
decide 的 payload）。所以**走到哪一步，就只看那一步的坑** —— 前面步骤的坑
不会污染当前判断。

写坑的原则：

- **一条坑一个字符串**。不要写成 `{text: ..., from: ...}` 那种 dict ——
  `StepSpec.pitfalls` 是 `list[str]`，字典模型读不到，检查器会判 `DEAD`。
- **只写这一步真会遇到的**。不要写全局注意事项（那些是知识卡的事）。
- **写"看到什么现象该怎么办"**，不要只写"注意 xxx"。反例 vs 正例：

  ```yaml
  # ❌ 空泛，模型不知道该做什么
  - "注意 iv_more 没有文字"

  # ✅ 说明现象 + 对策
  - "iv_more 没有 text/desc —— 用文案找「更多」会找不到，必须按 rid 定位。"

  # ✅ 说明坑的形态 + 正确做法
  - "若菜单没弹出（点空了），不要重复点同一坐标硬试 —— 回读 activity 确认当前在哪。"
  ```

- **坑的出处不必写进用例**。旧格式的 `from: com.zui.calendar.md#某节` 是给
  `kb_check.py` 校验引用锚用的，那属于**知识卡的维护职责**，不该塞进步骤坑。

### 知识卡内容可以内联进 pitfalls

`knowledge_files` 是**按关键词检索**的
（`src/android_agent/agent/nodes.py` 的 `search_knowledge`）：
给它一个关键词，它只在指定文档里返回**命中行及其上下文**（带 `文件:行号` 出处），
不把整份文档塞进上下文 —— 这样既查得到，又不会把上下文撑爆。
**凑不上关键词就是空**，所以知识卡里该写的词得写清楚。

所以"少查一次卡"的正确做法是：**把这一步真正要用的那几句，直接写成 pitfalls 字符串。**

```yaml
- id: s2
  desc: 点「从图库导入课程表」
  locate: rid=btnImportFromGallery
  landing: rid=button1
  pitfalls:
    # ↓ 这两句来自知识卡「链路-导入弹窗顺序」，内联后模型必然读到，
    #   不必指望它用关键词恰好检索到
    - "两段式、有先后：①App 内提示框「请确保图片清晰、完整」→ 点「知道了」；
       ②系统权限弹窗（仅未授予时才弹，已授予直接跳过）。第①段不点掉，PhotoPicker 不会出现。"
```

**代价**：知识卡改了，YAML 里的副本会过期。
**可接受**，因为 `pitfalls` 本来就该写"这次实测踩出来的经验"，不是知识卡原文的镜像。
**不要为此新增 `knowledge_inline` 之类的字段** —— 那会重蹈 `no_dialog` 的覆辙
（加了字段但下游不读，等于没加）。

### 落点表达式语法

支持这些键，**逗号=AND，竖线=OR**：

```
activity=.Foo          页面类名，子串匹配（.Foo 命中 a.b.Foo）
rid=btnOk              控件资源 id 存在
text=课程表            屏幕文字
desc=编辑              content-desc
package=com.x          前台包名
```

例：

```yaml
target: rid=iv_more
landing: activity=.timetable.display.TimetableActivity
```

> ⚠️ 早先这里还举了 `locate_seq` 的例子（一步连点两下，按序重放）。
> 那个机制已删除 —— 想连点两下就**拆成两步**，各自写自己的 `target` 和 `landing`。

⚠️ **activity 短名必须真的包含在完整名里**（子串匹配）。
`activity=TimetableActivity` **不命中** `.timetable.widget.TimetableGuideActivity` ——
历史事故：这里来回弹了 58 轮才被死循环检出。

---

## assertions

字段：`id` / `desc` / `check`

`check` 可以是字符串（落点表达式），也可以是结构化字典。
字典形式的**判据键白名单**（逐字抄自 `KNOWN_CHECKS`）：

| 判据 | 形状 | 说明 |
|---|---|---|
| `activity` / `activity_changed_to` | `".Foo"` | 本次**到达过**（不是"当前在哪"） |
| `package` | `"com.x"` | 当前前台包名，相等匹配 |
| `node_present` | `{rid: x}` | 过程中出现过即算 |
| `node_absent` | `{rid: x}` | 从没出现过 |
| `rid_present` | `{rid: x}` 或 `"x"` | 同上，允许裸字符串 |
| `nodes_present` | `[{rid: a}, {rid: b}]` | 多个都在 |
| `node_text` | `{rid: x, equals: "文案"}` | 该控件的文案 |
| `text_present` | `"文案"` | 屏幕文字 |
| `dialog_present` | `{rid: x}` | 弹框出现。**必须带 rid** |
| `dialog_present_on_tap` / `dialog_on_tap` | 同上 | 点击后的弹框 |
| `after_tap` | `{子判据}` | 嵌套：点了之后应出现的现象 |
| `permission` | `{CAMERA: true}` | **被测 App** 的授权状态（不是"弹过框"） |

> `permission` 判的是 `dumpsys` 里的 granted 结果。写 `{CAMERA: true}` = 日历已被授予相机权限。
> 不要用"屏幕上弹过权限框"当判据 —— 弹窗是过程，授权才是结果。

### 注解字段

这些允许出现但不参与判定：`note` / `measured` / `supporting` / `computed` /
`placeholder` / `reason` / `evidence` / `case_expect` / `desc`

### ★★ 代码判不了的，就别写 `check`

这条是踩出来的（176）。用例原文写了三条预期：
「顶部标题提示『添加课程』」「不加课程也能点完成保存空课程表」「背景色 10 种」，
YAML 却一条都没验。

补的时候差点又犯同一个错 —— 给它们各写一个"看起来对"的 check：

```yaml
# ❌ 错：这三条会假 PASS
- id: a7
  desc: 顶部标题提示「添加课程」
  check: {activity: .timetable.course.EditCourseActivity}   # 只说明"进了这页"
- id: a8
  desc: 不添加课程也能点完成
  check: {node_present: {rid: save_view}}                   # 只说明"按钮在"
- id: a9
  desc: 背景色 10 种
  check: {node_present: {rid: llCourseColor}}               # 只说明"这行在"
```

`activity=` 成立只代表进了页面，`node_present` 成立只代表控件存在 ——
**都不是用例要的那件事**。代码判成立还会直通（不问模型），于是这三条
会稳定地假 PASS，比不验更危险。

正确写法：**不写 `check`**。`check` 为空 → `evaluate_check` 返回 `None`
→ 交模型判，那才是"标题写了什么 / 能不能点 / 有几个颜色"该走的路。

```yaml
# ✅ 对：交模型判，在 desc 里把判点写清楚
- id: a9
  desc: 【预期5】课程背景色可选项共 10 种
  # 判点：展开色板数出几个色块。数不清写"看不清"，不许猜成 10。
```

判据一句话：**你写的 `check` 成立时，用例要验的那件事就一定成立吗？**
答不上来，就别写。

---

## 落盘前自检

1. 每条 `landing` 和 `assertions.check`，**`KNOWN_CHECKS` 里都有吗**？
2. `dialog_present` 带 `rid` 了吗？（不带的会被直接丢弃）
3. `activity=` 的短名，真的包含在目标完整类名里吗？
4. **每一条 `check` 成立，是否真的等于该预期的语义成立？**（见上节）
5. `pm clear` 真的只在测"首次使用"时才写吗？（见 preconditions 节）
6. 跑一遍检查器，**0 ERROR / 0 死字段**了吗？

---

## 检查器怎么判

| 档位 | 含义 | 能否落盘 |
|---|---|---|
| `ERROR` | 硬错误：必填缺失、形状不对、判据键不存在 | ❌ |
| `DEAD` | 写了但工具绝不认的字段 | ❌ |
| `warn` | 合法但值得知道（某步没写 `landing`、`skip_if` 判不了） | ✅ |

`DEAD` 为什么也算失败：它正是「静默失效」的来源。
`warn` 为什么不算：缺 `landing` 是**合法状态**（只观察的步交给模型走）。
把两者混成一档，要么死字段偷渡，要么误杀合法步骤。
