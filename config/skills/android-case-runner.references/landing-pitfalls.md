# landing 与断言：怎么识别判据有问题

### 这些判据由代码判，不用你操心

`check` 是结构化字典，**代码直接判**；只有下面这些之外的、
或代码判不了的，才会问到你。完整的合法键（代码认这些）：

```
activity      activity_changed_to     package
node_present  node_absent             rid_present
node_text     text_present            nodes_present
dialog_present  dialog_present_on_tap  dialog_on_tap
after_tap     permission
```

- `permission` 判的是**被测 App 的授权结果**（不是弹窗弹没弹过）
- `after_tap` 是嵌套子判据（"点了之后应出现什么"）
- 多个键 = **AND**；`note`/`measured`/`desc` 不参与判定

⚠️ **看到"⚠️ 不认识的判据键 xxx（未实现，未参与判定）"必须上报** ——
它意味着那条断言**这次根本没验**，不能当成通过。
这正是"看着跑了其实没验"的来源。

### 判据写错长什么样（你要能认出来并上报）

你不改判据，但你要能判断"这不是我走错了，是判据本身不可能满足"。
认出来就报告，别硬试把用例耗死。

**症状一：`rid` 是通用按钮**

```
landing = rid=button1        ← 这个 rid 目标是弹窗上的通用按钮
```

`button1` 是 **App 自绘说明页**上的「同意」按钮（包名还是被测 App）。
它可能在遮罩、弹窗、通用按钮上都会出现 —— 拿它当落点，
你在别的地方"命中"了，但真正的目标从没到达。

⇒ **判据：这个 rid 只有目标页面才有吗？** 到处都有的 rid 不可能是好落点。

**症状二：`activity=` 短名和真实全名对不上**

```
判据写 activity=TimetableActivity
实际到的是 .timetable.widget.TimetableGuideActivity   ← 不含 "TimetableActivity"
```

匹配规则是**子串包含**。`TimetableGuideActivity` 里没有 `TimetableActivity`
这个连续子串，所以**永远匹配不上** —— 表现就是你在一个明明对的地方
反复被判"没到"，来回弹到死。

⇒ 看到"我明明到了、代码一直说没到"，优先怀疑这条。

**症状三：该给的 `target` 给得太泛**

「更多」「返回」「加号」这类按钮**没有文字**，`rid=iv_more` 单看认不出是「更多」，
就容易去点列表里第一个带文字的东西。

⇒ 遇到这种，别在屏幕上瞎试：你的目的是「到达 landing 描述的页面」，
换个更明确的入口（选项菜单、其他控件）也算达成，只要最终落点对。
