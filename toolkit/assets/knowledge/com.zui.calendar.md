# 日历 App 知识卡

<!-- 只记「AI observe 拿不到」的五类内容：
       时序   什么时候会发生（不是"怎么点"）
       机制   系统怎么运作（两层数据 / 触发公式）
       工具   不看会做错的行为
       坑     试了才知道
       判据   这算不算符合预期

     路径图（怎么从 A 到 B）不在本卡，看 paths/com.zui.calendar.md。
     系统通用行为（权限框 / Toast / BACK 键 / 卸载恢复）看 _system.md。 -->

- **app**: `com.zui.calendar`

---


## 时序：什么时候会发生

### 首次启动三连（清数据后）

`pm clear` 后首次启动会**按固定顺序连弹三个**（不知道这条会以为"主页打不开 / 更多找不到"）：

1. **App 权限说明框**「日历需要使用以下权限」（必要/可选分组 + 退出/同意）
   - 标题 rid `alertTitle`，同意 = `android:id/button1`
   - ⚠️ 一命中就点会**点空**（入场动画未结束）：等约 1.5s 再点，点完核对框已消失
2. **系统权限弹窗**（默认同意会自动点掉，不用管有几个）
3. **课程表功能引导框**「课程表上新了！」—— 触发时机是 **`pm clear` 后首次启动、在【日历主页】上弹**，**不是**"进入课程表功能时"
   - 文案「课程表上新了！」/「点击快速导入课表，自动同步所有上课时间」/「我知道了」
   - rid `curriculum_guide_title` / `curriculum_guide_message` / `timetable_guide_button`

> ⚠️ **这三个只在清数据后出现，冷启动不会再有**（本地已记"看过"）。
> 反过来：用例没提清数据时不要顺手清 —— 每清一次，后面每条都要重走这三连。
> 用例明确要求（如「首次使用」「清数据验证」）就照做，那正是要测的。


### 空课表的空格子与加号

- 空格子 = `cv_empty_content`（clickable）
- **进入空课表时：全部格子只有 `cv_empty_content`，没有 `iv_add_hint`**
- **点某个格子后：该格出现 `iv_add_hint`（加号浮标）**
- 再点 `iv_add_hint`：进入新建课程页
- 「+」是图，无 text / desc，只能靠 rid 判（OCR 认不了图标）


### 添加/编辑课程弹窗的两段式

⚠️ **形态要点（别判错）：** 实现是 `EditCourseActivity`，但**视觉上是浮层弹窗** ——
圆角卡片压在周视图上、背景压暗、下面的课表还在、关闭后回到原处。
dump 里**没有** `Dialog` 字样，activity 名也不带 Dialog，**但这就是弹窗**。
用例说「弹出编辑弹窗」时按浮层判，不要按 activity 名判成"整页"。

「新建课程」与「编辑课程」是**同一个弹窗**：

- 字段 rid：课程名 `etCourseName`（必填）/ 教室 `etClassroom` / 备注(老师) `etTeacher`；
  课程时间 `llCourseTime` / `tvCourseTime`（默认"第1节"）；上课周数 `llCourseWeeks` /
  `tvCourseWeeks`（默认"第1-20周"）；背景色 `llCourseColor`（行显示 `viewColorIndicator`）
- **底部「删除课程」= `btnDelete`**（仅编辑态有）；右上角「完成」= 保存退出；左上角 desc=关闭
- **「课程背景色」点行弹独立 AlertDialog**（`alertTitle`/`customPanel`/`buttonPanel`），
  `customPanel` 内 **10 个纯色块**（clickable 无 text/desc）
  - ⚠️ **数色块要看图**：UI 树数 clickable 会被嵌套/装饰节点干扰（实测 25≠10），
    看当步 `shot.png` 数，或问视觉模型
- **布局**：`curriculum_scroll_view` 内容区在本机型一屏内 —— **所有字段都在首屏**。
  踩过的坑：误以为"在首屏之外"就无脑上滑，把本来可见的元素滚出屏幕上方、越滚越找不到
  → **读不到元素时先滚回顶部再逐屏向下找（双向）**


---

## 机制：系统怎么运作

### 事件不在 App 数据目录（两层数据）

做「清除日历数据」「以 DB 计数判断事件数量」这类用例前必须知道：

- **事件不在 `com.zui.calendar` 的数据目录里**。`content://com.android.calendar/events`
  由系统组件 **`com.android.providers.calendar`** 承载
- 因此 `pm clear com.zui.calendar` **清不掉任何事件**，它只重置应用自身设置
  （会出现首次运行三连，「抢火车票设置」回到「暂不显示」，「每周起始日」回到「星期一」）
- 真正让 provider 归零要用 `pm clear com.android.providers.calendar`
- **但日历 UI 读的不是这一层** —— 出现过「`content query` 查得 0 条，而 UI 仍显示事件」，
  即 UI 走自带同步缓存（联想账号 `com.lenovo.lsf.account` / `com.zui.sync.calendar` 体系）

**启动日历就会触发云同步回灌**：只要启动应用，就会跑一次联想账号同步并把事件插回来。
`sync_events=0` 与 `am force-stop` 都拦不住这次拉取；不启动应用时，
`pm clear com.android.providers.calendar` 的结果能稳定保持。

关联账户：`com.lenovo.lsf.account`；日历账户名 `PC Sync`（`account_type=LOCAL`）。


### provider 只存「日程」类

**新建事件页左侧的分类不是同一套存储**，这个 provider **只暴露「日程」一类**：

| 分类 | provider 可见？ |
|---|---|
| 日程 | ✅ |
| 提醒 / 课程 / 倒数日 / 生日 / 纪念日 | ❌ **不可见**（存应用内部库） |

`content://com.zui.calendar/events` 直接抛 `Could not find provider`；
`content://com.android.calendar/reminders` 无 `title` 列。

**后果**：拿它当「保存是否成功」的判据，会把提醒/课程/生日/倒数日/纪念日的正常保存
全部判成「保存不落库」。这是**方法错误**，不是产品缺陷。

**要看这些分类存没存上，走纵览表**：主页 → `iv_more` →「纵览」，
左侧分类点 `*_layoutx`（`all_layoutx`/`reminder_layoutx`/`event_layoutx`/
`curriculum_layoutx`/`countdown_layoutx`/`birthday_layoutx`/`creditcard_layoutx`/`trip_layoutx`），
右侧列表项 rid 以 `title` 结尾即为该分类下的条目。

> ⚠️ 纵览表默认停在**搜索态**时，左侧分类点了不生效（右侧仍显示上次搜索结果）。
> 点分类前先确认不在搜索态（有搜索词时点 `cancel` 退出）。


### ⭐ 提醒触发时间 = 开始时间 − 偏移

**这条是造「等提醒」类数据的前提，算错就白造。**

```
触发时刻 = 事件开始时间 − 提醒偏移
  提醒偏移取：任务发生时(0) / 5分钟前 / 15分钟前 / 30分钟前
  日程分类的默认值是「15分钟前」

例：事件设 10:19，提醒「15分钟前」→ 触发时刻 = 10:04
```

**⚠️ 触发时刻已经过去 → 保存后立即弹通知**（不是不生效！）

```
当前 10:16，事件 10:19 + 「15分钟前」→ 触发点 10:04 已过
→ 保存的瞬间就弹提醒，alarm 列表里也查不到（因为已经触发掉了）

❌ 别把这个误判成"提醒没注册"——它注册了，只是立刻触发了。
```

**要「等一会儿再响」，反推开始时间：**

```
1. adb shell date 拿设备当前时间（别用本机时间，可能校时跳变）
2. 开始时间 = 当前时间 + 提醒偏移 + 操作余量（至少 5 分钟）
3. 建事件时就把开始时间设成这个值
4. 执行"结束进程"等操作，然后等到点
```

**⭐ 「提醒生效了没有」的判据：看通知栏**

**最简单的方法 —— 直接看通知栏：**

```
android_system(action="notifications", why="判提醒有没有来")
```

一次调用就把通知栏展开、截图、dump 解析完，返回：

- `applied` —— 展开成功没有（`true` 成功 / `false` 没成功 / `null` 判不了，看截图）
- `notifications[]` —— 每条通知的 `title` / `texts` / `center`（要点击就用这个坐标）
- `rows_detected` —— 认不认得出通知行。**`false` 时 `notifications` 为空不代表没有通知**，
  是这个 ROM 换了行 id，请看截图判断
- `shade_note` —— 认不出时的说明

**这也是用户实际做的动作** —— 提醒来了，就是通知栏里多一条。

用完记得收起（`android_system(action="close_shade")`），或直接
`android_system(action="notifications", close=True)` 看一眼顺手收掉。

**要看结构化数据时（不用拉通知栏）：**

```powershell
adb shell "dumpsys notification --noredact" | Select-String "android.title"
# 只看日历的
adb shell "dumpsys notification --noredact" | Select-String "android.title=.*日程"
```

```
例：android.title=String ([日程] 测试83日程E)     ← 提醒真的来了
```

**这是用户实际看到的东西。** `dumpsys alarm` 是系统内部调度表 ——
alarm 存在≠用户收到通知，alarm 不在≠通知没来（触发后就从表里消失了）。

> ❌ **不要用 `dumpsys alarm` 判断"提醒有没有生效"** ——
> 触发过的 alarm 会从表里移除，查不到不代表没生效。
> 实测中 AI 正是这样误判了 5 次，重造了一堆事件。
>
> alarm 列表只在**造数据阶段**有用：确认"闹钟排在未来某个时刻"，
> 判断的是「我设的时间对不对」，不是「用户有没有收到」。

**三种会白做的情形（实测都踩过）：**

```
① 开始时间设成「现在或过去」→ 触发点已过 → 保存瞬间就弹
② 用了默认「15分钟前」但开始时间只比现在晚 10 分钟 → 同样是过去
③ 触发点已过时去查 alarm 查不到，就断定"没注册" → 白重建
```

> 实测代价：一条用例为此连建 5 个事件（A~F），40 分钟才试对。

**⚠️ 但「到点了通知没来」不一定是造数据错 —— 可能是被测行为本身：**

```
如果用例要求「先结束进程，再等提醒」，那么：
  进程被杀 → 闹钟可能随之取消 → 通知不来

这是「提醒在进程结束后能否到达」的**被测结果**，
不是造数据失败。判 FAIL 或 PASS 要看用例怎么写的，别急着重造。
（真实机制见上面「结束后台运行」那一节：force-stop 会 pi_cancelled）
```


---

## 工具行为：不看会做错

### 结束进程只能用 am kill

验证「结束后台运行后提醒仍能到达」这类用例时，两种方式语义完全不同：

- `am force-stop <pkg>`：结束进程且置 `stopped=true`（禁用应用），**连同已注册的闹钟一并取消**
  → ❌ 不能用于这类用例（太严厉）
- `am kill <pkg>`：只结束后台进程，`stopped=false`，**闹钟保留**
  → ✅ 等价于用例说的「双击 HOME / 从最近任务划掉，结束后台运行」

用 `force-stop` 后查 `dumpsys alarm` 会看到闹钟被移除，并在 alarm history 留下
`Reason=pi_cancelled rtc=<force-stop 时刻>` —— **这才是「提醒没来」的真正原因**，不是应用缺陷。
且 `force-stop` 后应用处于 `stopped=true`，**下次手动启动前闹钟/广播都不会送达**；
要恢复得先启动一次（`monkey -p <pkg> -c android.intent.category.LAUNCHER 1`）。

**正确姿势**：先 `act home`，再 `am kill <pkg>`，然后核对三件事再等提醒：

1. `pidof <pkg>` 为空（进程确已结束）
2. `dumpsys package <pkg>` 里 `User 0: ceDataInode=…` 那行 `stopped=false`
3. `dumpsys alarm` 里目标闹钟仍在


### 通知栏会挡住操作 —— 用 android_system 收起

上一条用例结束在**通知栏展开**状态时，下一条的第一步会打在通知栏上而**完全无效**，
表现为 `activity` 仍是 `NotificationShade`、checkpoint 不命中。

- 判据：`dumpsys window | grep -m1 mCurrentFocus` 返回 `Window{… u0 NotificationShade}`
- ✅ **收起的正确做法**：`android_system(action="close_shade")` —— 内部走 back 键，
  收起后再用 `dumpsys window` 复核一遍，仍展开才退到 `cmd statusbar collapse`
- 通知栏展开时按 `android_act(action="back")` 也能收起（2026-11-12 实测）；
  但 `close_shade` 会复核、也能收快捷设置面板，优先用它
- ⚠️ 不要用「再拉一次通知栏」去收起 —— 实测连拉两次仍是展开


---

## 坑：试了才知道

### 一键备份 toast 文案不区分有无内容

即使事件为空时点「一键备份」，也会提示「备份成功」——
这个 toast 在有内容 / 没内容两种情况下文案一样，不能当「确实备了份数据」的判据。


### 必须先读值再操作

**元素不在当前视口时按 rid 读返回 `''`** —— 直接判会连报失败。顺序：

1. 进页面立刻读首屏值（上课时长 / 休息时长 / 上午 / 下午节数）
2. 再上滑读 `tv_evening_slot_count`（懒加载）
3. 要断言的「默认时间显示」必须**在改时长之前**采集快照

第 3 点尤其关键：**节行时间随「上课时长/课间休息」实时重算** ——
把时长拨到 30 分钟后所有节行变成 30 分钟制，此时再读原始时间已不是原值。


### OCR 偶发漏读别进判定循环

目标值 5 但 OCR 在弹框内返回 None → 以为"没动"继续点 → 在循环上绕圈 → 走满上限、原地打转。

**正确做法**：按**档位次数**点按（不读 OCR 反馈），点确定，**读 UI 树文本**
（`tv_lesson_duration`/`tv_break_duration`）作判据。


### 滚轮要点准：先量「可点带」

**同款弹框在不同页面会上下平移，可点带跟着平移。** 照搬旧坐标会「点了没反应」，
很容易误以为是「滚轮不响应合成点击」而去改用 `swipe`（→ 过冲 → 更难收敛）。

**先量后点：**

1. dump 一次，取三件套：
   - `datePicker` 的 bounds —— 整个弹框
   - `parent` 的 bounds —— **滚轮真正的可点区**，比 datePicker 矮一截（去掉了底部按钮）
   - `selector_top_divider` 的 bounds —— **选中行上沿**，行高的定位锚点
2. `parent` 高度 ÷ 可见行数(约5) ≈ 行高；以 divider 为基准，算出「选中行 / 上一行 / 下一行」的 y
3. 点完用 `read --region`（框住弹框 title）**回读当前值**再决定下一步

规则（跨弹框通用）：

- 点 `parent` 里偏上的位置 → **−1**
- 点选中行 → 无变化
- 点偏下的位置 → **+1**
- 太靠近弹框上下边缘是**死带**，点了完全不变
- ⚠️ **方向可能因弹框而异**：月视图的日期弹框与新建事件页的弹框方向相反，**第一次用先探一下**


### 弹框滚轮是双向循环

- **每节课上课时长**（`{30,35,…,120}`，每档 5）：**循环** —— 120 再拨 → 30分钟
- **课间休息**（`{5,10,15,20,25,30}`，每档 5）：**循环** —— 5 再上拨 → 30分钟、30 再上拨 → 25分钟

方向：**上=减小、下=增大**（两个轮一致）。

⚠️ **拨档 `--gap` 不能太小**：`--gap 350` 时点 14 次只生效 5 格（点击被丢弃）；
`--gap 800` 时点 9 次全部生效。**别把"点了几次"当拨了几档，以 OCR 回读为准。**


---

## 判据：这算不算符合预期

### 必填校验与规格不符（置灰）

规格说"名称为空时完成按钮**置灰**"，实测**不置灰**：

- 新建课程表页 `save_view`、图库导入确认页 `btn_finish`：`enabled=true`、`clickable=true`
- 点击后**被校验拦住**（页面停在原页、无 toast）

**置灰断言**：日历的置灰是「整体变淡」，UI 树 `enabled/clickable` 可能不变；
以截图判读（问视觉模型「这个按钮是不是灰色不可点」）。


### 自定义结束时间点了没反应（已知缺陷）

`.event.options.EventLastDateActivity`：

- 结构：`toolbar_title`=重复结束｜『永不结束』复选框 + `lastSwOver`(Switch)
  ｜`lastTvOverText`=『自定义结束时间』｜`lastTvOvertime`=日期文字
- **『永不结束』默认是勾选的**；此时自定义结束时间的日期为**灰色**不可用
- 点 `lastSwOver` 或复选框可取消勾选 → 该行文字与日期**变黑**（界面认为可交互了）
- ⚠️ **但那一行点了没有任何反应**：容器 `lastLlDate` 在 dump 里 `clickable=true`，
  多点位置都试过，Activity 不变、无弹框、节点无变化；logcat 显示触摸**已投递到应用**且无异常
  ⇒ 是点击被吃掉，不是工具没点到
- 附带：`lastTvOvertime` 默认值为 **2024年1月1日**，早于当前日期，初始值本身不合理


### 删除全部课程表后的两种「空」

| 时机 | 落点 | 三按钮 |
|---|---|---|
| 删除全部**当场** | 列表空态「没有课程表」+「点击 + 创建新课程表」 | ❌ 无 |
| **重新进入**课程表 | 引导式空状态页「还未添加课程表」 | ✅ 三个都在 |

删光后列表页**不会自动跳转**，要**离开再回来**才回到引导式空状态页。


### 多选态与置灰规则

点 `action_schedule` → 多选态：出现 `checkbox_select` + `bottom_bar` + `btn_set_default` / `btn_delete`。
**置灰看 `enabled` 字段**：

| 勾选情况 | btn_set_default | btn_delete |
|---|---|---|
| 仅勾选**当前**课程表 | `false`（置灰） | `true` |
| 勾选**多个**（含当前） | `false`（置灰） | `true` |
| 仅勾选**非当前**课程表 | `true`（可用） | `true` |

即：**勾选集合含"当前课程表" → 设为当前置灰**。

⚠️ 删除弹框文案「确定删除**此**课程表吗？」区别于课程卡片的「确定删除**此课程**吗？」。

- 新建的课表**不自动抢占"当前"**


### 月视图「票」标记的读法

抢火车票设置生效后，月视图日期格**右上角**会出现一个小「票」字。整屏 OCR 能读出 `text == "票"`。

⚠️ **「票」画在格子右上角，日期数字在格子中偏左** —— 两者横向不对齐，直接用「票」的 x 认日期会错位。
**必须用点击回验**：点日期格后读 `top_button_day`，确认列归属正确，再据此换算日期。


---
