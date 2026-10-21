# landing 与断言：踩过的坑

### 断言用 YAML 的结构化判据（判得比模型准）

跑 YAML 用例时，`check` 是**结构化字典**，会被**代码直接判定**，
判不了才交回模型。已实现：

| 判据 | 形状 | 说明 |
|---|---|---|
| `activity` | `{activity: ".Foo"}` | 本次**到达过**（不是"当前在哪"） |
| `activity_changed_to` | 同上 | 同义 |
| `node_present` | `{node_present: {rid: x}}` | 过程中出现过即算 |
| `node_absent` | `{node_absent: {rid: x}}` | 从没出现过 |
| `node_text` | `{node_text: {rid: x, equals: "课程表"}}` | 该控件的文案 |
| `dialog_present` | `{dialog_present: {rid: button1}}` | 弹框出现 |
| `package` | `{package: "com.x"}` | 当前前台包名 |
| `after_tap` | `{after_tap: {dialog_present: {...}}}` | 嵌套子判据 |

多个键 = **AND**。注解字段（`note`/`measured`/`desc`）不参与判定。

> **不认识的判据键会被明确标出来**（"⚠️ 不认识的判据键 xxx（未实现，未参与判定）"），
> 不会静默忽略 —— 静默忽略会让用例"看着跑了其实没验"。

### 踩过的坑：别把弹窗的按钮当落点

写 landing 时**只写那个页面独有的、稳定的标识**。反面教材：

```
s2 落点 = rid=button1        ← 错的
```

`button1` 是 **App 自绘说明页**上的「同意」按钮（包名还是被测 App）。
把权限框弹出来当成了"到达图库入口"，于是照片权限从没被授予，用例误判。

**正确写法**：`activity=PhotoPickerGetContentActivity` —— 那是唯一真正的落点。

判据：**这个 rid 只有目标页面才有吗？** 如果它在弹窗、遮罩、通用按钮上也会出现，
就不能用它当落点。

### 更隐蔽的坑：`activity=` 短名必须真的包含在完整名里

```
落点写 activity=TimetableActivity
实际到的是 .timetable.widget.TimetableGuideActivity   ← 不含 "TimetableActivity"
```

匹配规则是**子串包含**。`TimetableGuideActivity` 里没有 `TimetableActivity`
这个连续子串，所以**永远匹配不上** —— 流程会在原地来回弹到死循环（实测 58 轮）。

写 `activity=` 落点前，先确认目标 activity 的**真实全名**。
多个可能的落点用竖线：`activity=TimetableActivity|activity=TimetableGuideActivity`。

### 图标按钮的 target 要选对

「更多」「返回」「加号」这类按钮**没有文字**，模型光看 `rid=iv_more` 认不出它是「更多」，
会去点列表里第一个带文字的东西（实测点了日期栏，然后卡死）。

所以 `target` 要给得让代码能唯一命中：

```yaml
target: rid=iv_more
landing: activity=.timetable.display.TimetableActivity
```

`target` 支持 `rid=` / `xpath=` / `text=` / `desc=` / `key=`。
**推荐 `xpath=`** —— 它能表达"右上角那个"这种结构关系，比单个 rid 更不容易撞车。

> ⚠️ **绝不存坐标**。坐标换个分辨率就废，而且看起来"很精确"，最容易被误信。
> 要定位就用 xpath 这种**结构路径**，同一个控件不会因为分辨率变化而找错。

**菜单项通常没有 rid**（`rid=None`），这时必须按文字定位：`target: text=课程表`。
