# ZUI 日历（联想平板）

- **app**: `com.zui.calendar`
- **name**: ZUI 日历
- **验证版本**: 9.0.0.110-2026.08.25-release（versionCode 900110，TB522FU / Android 17）
- **最近验证**: 2026-09-22（169 图库/拍照权限 4 分支 PASS、179 小节间隔与冲突 3/3 PASS）
- 无课程表前提：`state.py clear --package com.zui.calendar`
- 图库/拍照导入的解析需联网（约 20s，等待窗口给到 60s）

<!-- ⚠️ 本卡设备无关：只写 rid / 文案 / activity / 行为规律。
     坐标、bounds、行距、"第 N 个箭头"这类数值一律不写 —— 换机型即失效。
     需要定位时用 observe 拿 bounds 现场派生。 -->

## 弹窗-首次启动三连

`pm clear` 后首次启动会**按固定顺序连弹三个**（不知道这条会以为"主页打不开 / 更多找不到"）：

1. **App 权限说明框**「日历需要使用以下权限」（必要/可选分组 + 退出/同意）
   - 标题 rid `alertTitle`，同意 = `android:id/button1`
   - ⚠️ 一命中就点会**点空**（入场动画未结束）：等约 1.5s 再点，点完核对框已消失
2. **系统权限弹窗**（默认同意会自动点掉，不用管有几个）
3. **课程表功能引导框**「课程表上新了！」
   - 触发时机是 **`pm clear` 后首次启动、在【日历主页】上弹**，
     **不是**"进入课程表功能时"
   - 文案「课程表上新了！」/「点击快速导入课表，自动同步所有上课时间」/「我知道了」
   - rid `curriculum_guide_title` / `curriculum_guide_message` / `timetable_guide_button`
   - 点掉权限框后出现

> ⚠️ **这三个只在清数据后出现，冷启动不会再有**（本地已记"看过"）。
> 所以验"首次使用/升级后首次"的用例**必须清数据**，冷启动复现不出来。
> 反之，**普通用例不要清数据** —— 否则每条都要重走这三连（见 SKILL.md「状态准备」）。

## 导航入口

| 去向 | 走法 |
|---|---|
| 课程表模块 | 主页 → 右上角「更多」`iv_more`（无 text/desc，按 rid 定位）→ 菜单「课程表」 |
| 周视图 `TimetableActivity` | 上一步的落点 = App 打开默认课表 |
| 全部课程表列表页 `TimetableListActivity` | 周视图 → 顶栏 `action_curriculum_table_settings`（文案「课程表设置」）。**进课程表不会直接到列表页** |
| 图库/拍照/手动创建 | 空状态页 `btnImportFromGallery` / `btnImportFromPhoto` / `btnCreateManually` |
| 新建课程表（**已有课表时**） | 列表页右上角 `action_add_schedule` |
| 设置页 `GeneralSettingsActivity` | 主页 `iv_more` → 菜单「设置」 |
| 备份与恢复 `LenovoSettingsBackupActivity` | 见「设置页-备份与恢复」 |
| 新建事件/提醒 `NewBuildActivity` | 主页右上角 `action_add_all_event`（ImageView，无 text/desc） |

## 页面-备份与恢复

入口：主页 `iv_more` →「设置」→ `GeneralSettingsActivity`。

- 设置页是 `recycler_view` **长列表**，「备份与恢复」在**第 3 屏**（需 2 次上滑），
  位于「其它」分组下（时区之后）。**首屏看不到它** —— 只扫首屏会误判"没有该入口"
- 上滑后**重读 UI 树**再找，不要按固定次数滑

备份与恢复页 `LenovoSettingsBackupActivity` 控件（**都在首屏**）：

| 元素 | rid |
|---|---|
| 自定义恢复 | `selecte_restore_layout`（整行可点） |
| 一键备份 | `backup_button` |
| 一键恢复 | `restore_button` |

### 坑-清数据会清掉备份

备份产物在 `/sdcard/Android/data/com.zui.calendar/cache/LenovoCalendar/BackUp/<时间戳>/`
（含 `Events.vcs` + `114_lecalendar.db`）。

**虽然该路径在外部存储，但 `pm clear` 在 Android 11+ 会连 App 的外部
`Android/data/<pkg>/` 一起清掉** —— 实测 clear 后目录直接消失。

后果：「先清数据、再一键恢复」这类前置**自相矛盾**：清完就没备份可恢复，
点「一键恢复」只提示**「未找到可用的备份记录」**（既不弹 dialog 也不出 toast）。

**写"恢复"类用例的正确姿势**：前置要备份，但**不要** `pm clear`；
若要验证"清数据后恢复"，必须先把备份目录拷出来、clear 后再拷回去。

### 一键恢复的信息反馈

点「一键恢复」后是**两段式反馈**（都会出现）：
1. 模态 dialog「正在努力恢复」（转圈，约 0.6~2.8s）
2. 底部 toast「**恢复成功**」

**用例只断言 toast，不管 dialog。**

抓 toast 的正确手法（⚠️ 别再自创）：
1. `act tap` 触发（按钮坐标从 observe 的 bounds 派生）
2. **等约 2s**（等 dialog 关闭 + toast 渲染；**不要中间插 observe**，截图会挤掉窗口）
3. `read.py` 整屏 OCR → 文本含「恢复成功」即通过

三个坑：
- **toast 不在 UI 树里**：observe 的 dump 永远读不到它，只能靠 read.py 像素 OCR。
  用 UI 树文本判定"toast 不存在"是**方法错误导致的假 FAIL**
- **不要边采样边 OCR**：RapidOCR 单次约 4–9s，会把采样间隔拖到 10s 以上、错过浮层。
  要连拍就**先只截屏、后批量 OCR**
- **不需要录屏/连拍**：点击后等 2s 截一张即可稳定拿到

### 页面-自定义恢复 `SelectRestoreActivity`

- 列表项文本形如 `日历_2026年9月17日_10时37分02秒`
- **右上角「编辑」是纯图标**（无 text/desc）→ rid `edit_menu`，不能按文字定位
- 编辑模式：标题 `listviewx_edit_title`「选取项目」；`listviewx_select_all`「全选」；
  `listviewx_cancel` 取消；`restoreBtnDelete`「删除」
  - **置灰判定看 `enabled`**：未勾选 `enabled=false`，勾选 `slideview_check` 后变 `true`
- 点删除 → 二次弹窗「**删除所选的记录？**」；点某条 → 「**恢复到所选的备份记录？**」
- 选择框 rid = `slideview_check`（每行一个）

## 页面-新建事件 `NewBuildActivity`

- 进入：主页 `action_add_all_event`，或**长按桌面图标 →「创建提醒」**
- 标题占位文本 = 「请输入提醒」（按 text 定位后输入）
- 备注字段按 text=「备注」定位后输入
- 大量文字（600 字）输入正常，无崩溃/ANR

### 表单随左侧分类切换而完全不同（同一 Activity，rid 全变）

新建事件页左侧 `categoryList` 有 6 个常驻分类（提醒/日程/课程/倒数日/生日/纪念日）。
**切换分类不换 Activity**，但右侧表单节点的 rid 整套替换，这是最容易做错的地方：

| 分类 | 标题 rid | 全天开关 | 起止字段 | 其他特征 |
|---|---|---|---|---|
| 提醒（默认） | `reminder_title` | **无** | `eventTimeLayout`（起+止一个弹框） | `reminderTimeLayout`/`reminderRepeatLayout`/`reminderDescription` |
| 日程 | `event_title` | **`is_all_day`** | `start_date`+`end_date`（全天时）/`start_layout`+`end_layout`（非全天） | `rrule`/`busy_text`/`tv_type` |
| 课程 | `mCurriculumTvTitle` | 无 | `mCurriculumLlStartLayout`/`mCurriculumLlEndLayout` | `mCurriculumEtLocation`/`mCurriculumEtRemark`/`mCurriculumTvTimeFrame`(时段)/`mCurriculumTvRepeatRule` |

- **「全天事件」开关 `is_all_day` 只在「日程」分类页存在**。在默认「提醒」页找全天开关会找不到
- 开全天 → 时间栏消失、出现 `start_date`/`end_date`；关全天 → 变回 `start_layout`/`end_layout`（带时分）
- 课程分类的「重复」点进去是 `.curriculum.CurriculumRepeatActivity`（**不是** `.reminder.RepeatSelectedActivity`），
  实测仅三项：不重复(默认)/每天/每周；选「每周」再进 `.curriculum.CurriculumCustomRepeatActivity` 选周一~周日，
  `week_tips` 会显示「事件将于每1周的周二重复发生」这类回显文案，**可作断言依据**
- ⚠️ 左侧菜单的**「课程表」(`.timetable.display.TimetableActivity`) 是另一套数据**，
  在这里新建的「课程」分类事件**不会**出现在课程表里，反之亦然

## 弹框-日期/时间选择器（新建事件页通用）

调起后是**居中真弹框**：`date_time_picker`（时+分）或 `datePicker`（仅日期，全天用），
`left_button`=取消、`right_button`=确定。**activity 不变**，故判「是否真弹框」看 activity 是否变化。

- 三列滚轮左→右 = **日期 / 小时 / 分钟**，列中心 x ≈ **1153 / 1520 / 1887**（3040 宽屏，按比例换算）
- 滚轮是 **Canvas 自绘，dump 读不到当前值** —— 要读值用截图 + `vision`（问「读图中日期选择弹框当前选中的日期」）
- **改值用点按 ±1，且用 `--repeat` 一次点完**：
  - 点**上方**一格 = **-1**，点**下方**一格 = **+1**（实测与直觉相反，别记反）
  - **一次点按就是一格，不需要点一次观察一次、也不需要反馈校验** —— 直接按要走的格数点
  - ⭐ **用 `act tap --x .. --y .. --repeat N`（可加 `--gap 毫秒`，默认120）**：
    N 次点按在**一个进程内**完成。实测 `--repeat 6` 将 10月9日 精确改到 10月15日，
    单次调用 **9.7 秒**（含一次冷启动）；同样 6 格若拆成 6 次调用要 40-60 秒且**极易超时**
  - ❌ 不要用外层 shell 循环包 N 次 `run.py`：冷启动 ×N 撞命令超时 → 动作执行了却没记账
    → 会话被判 `recording incomplete` **直接作废**
  - ❌ 不要用 `swipe`：有惯性会过冲（实测一次 swipe 跳过一天），且每次都要一次工具往返
- 全天开关打开时，日期弹框只有 `datePicker`（单列日期滚轮），**没有农历开关**
- 农历开关 `lunarToggle`（文案「显示农历选择」）**只在部分形态的弹框里出现**，不是每次都有；
  找不到就是没有，别硬找

### ⭐ 滚轮要点准，先量「可点带」；**改月轮时 `--repeat` 要慎用**

上面「点上方一格=-1 / 下方一格=+1」是从**新建事件页**的弹框实测来的。
**设置页/列表页的同款弹框，整体位置会上下平移，可点带也跟着平移**，
照搬 y 会「点了没反应」，很容易误判成「滚轮对合成点击不响应」而去改用 `swipe`（→ 过冲 → 更难收敛）。
**先量后点**：

1. dump 一次，取三件套几何：
   - `datePicker` 的 bounds
   - `parent` 的 bounds（**滚轮真正的可点区**，通常比 `datePicker` 矮一截，去掉底部按钮）
   - `selector_top_divider` 的 bounds（**选中行上沿**，是行高的定位锚点）
2. 用 `parent` 高度 ÷ 可见行数(约5) ≈ 行高；以 divider 为选中行基准，
   算出「选中行 / 上一行 / 下一行」各自的 y。
3. 用 `act tap --x <列x> --y <目标行y>` 点，**点完用 `read --region`(框住弹框 title) 回读当前值**，
   再决定下一步 —— 弹框 title 形如「2026年9月14日 周一」，是**唯一可靠的读数方式**。

**实测锚点 A（月视图 top_button_year 弹出的日期弹框）**：
`datePicker` b=[970,801][2070,1359]，`parent` b=[970,801][2070,1145]，
`selector_top_divider` b=[1036,903][2004,906]；三列 x ≈ **1152 / 1520 / 1887**（年/月/日）。
**有效可点带 = y≈860 ~ 1140**，且 **y 越大 = +1、y 越小 = −1**（与新建事件页弹框记载方向相反）；
y=1020~940 与 y=820~740 是**死带**（点了完全不变）。

**实测锚点 B（抢火车票设置页的时间弹框，整体上移 80px）**：
`datePicker` b=[970,721][2070,1439]，`parent` b=[970,721][2070,1065]，
`selector_top_divider` b=[1036,823][2004,826]；**有效可点带 = y≈780 ~ 1060**：

| 点 y | 效果 |
|---|---|
| 1040 | **+1** |
| 900 / 860 | +1（此处已到钳制边界，不再变化） |
| 800 | 选中行，无变化 |
| 760 | **−1** |

⚠️ **同款弹框在不同页面会平移，必须每次重新量 `parent`/`selector_top_divider`，不要记死 y。**

#### ⚠️ 坑-`--repeat` 连续点「月」轮会把改动吃掉

`--repeat N` 对**日**轮是可靠的（实测 `--repeat 5` 把 20日→25日、`--repeat 6` 把 9日→15日，
一次调用完成，值是准的）。但对**月/年**轮要当心：
第一次点按改变月份后，**滚轮内容会重排，第二次点按落到的可能已是另一格**，
于是「+2」实际变成「+1 再 −1」= **回到原值**，看起来像「改了月却提交不上」。

- ✅ **改月轮：一次调用只点 1 下**，回读 title 确认后再点下一下（跨月就分几次工具调用，别用 `--repeat`）。
- ✅ **改日轮：可以放心用 `--repeat N`** 一次点到位（N = 要走的格数）。
- ⚠️ 判据（本轮实测踩坑后确认）：**只改日** → 提交成功；**只改月（单点）** → 提交成功；
  **月用 `--repeat 2` 再改日** → 「确定」后月改动**丢失**、只剩日改动。
  当时误判成「启程时间弹框提交不可靠」，实为 `--repeat` 用法错误 —— **是工具用法问题，不是设备缺陷**。

- ❌ **不要用 `swipe`**：过冲严重（实测一次 swipe 把 8月→9月 或 11月→2月，直接跳月甚至跳年），
  比点按难收敛得多。
- ⚠️ **探测坐标要「一次调用内跑完」且候选要少**：用 shell 循环包 N 次 `run.py` 逐 y 试探，
  必然撞命令超时 → 会话 `recording incomplete` **作废**（本轮在 385 上踩过一次）。
  候选 y 收敛到 3~5 个，每点一下用一次 `read`（比 `observe` 轻）。

#### 月视图「票」标记的读法

抢火车票设置生效后，月视图日期格**右上角**会出现一个小「票」字。
OCR（`read --region "0,280,1500,1200"`）能稳定读出 `text == "票"`，
但它**与所属日期的横向偏移约 +90px**（标记画在格子右上角，日期数字在格子中偏左），
**对齐时要 `x_标记 − 90` 才是该日期数字的列 x**。
该映射**必须用点击回验**：点日期格后读 `top_button_day`，确认列归属正确，再据此换算日期，
不要只按相对偏移推断（本轮早期就是只靠偏移推断，得出了错误的「提前 10~14 天」结论）。

### ⚠️ 坑-「结束后台运行」只能用 `am kill`，不能用 `am force-stop`

验证「结束进程后提醒仍能到达」这类用例时，**两种结束方式语义完全不同**：

- `am force-stop <pkg>`：结束进程，且把应用置为 `stopped=true`（**禁用应用**），
  **连同已注册的闹钟一并取消** → ❌ 不能用于本类用例（太严厉）。
- `am kill <pkg>`：只结束后台进程，应用保持 `stopped=false`，**闹钟保留**
  → ✅ 等价于用例所说的「双击HOME / 从最近任务划掉，结束后台运行」。

- 用 `force-stop` 后查 `dumpsys alarm` 会看到该闹钟被移除，并在 alarm history 里留下
  `Reason=pi_cancelled rtc=<force-stop 时刻>` —— **这就是「提醒没来」的真正原因**，不是应用缺陷。
  （本轮在 日历_83 上踩过一次，白跑一轮，那个「无通知」的观察已废弃。）
- `force-stop` 之后应用处于 `stopped=true`，**在下次手动启动前闹钟/广播都不会送达**；
  要恢复得先启动一次应用（`monkey -p <pkg> -c android.intent.category.LAUNCHER 1`），
  `stopped` 才会变回 `false`。

**正确姿势**：先 `act home` 让应用退到后台，再 `am kill <pkg>`，
然后**核对三件事**再等提醒：

1. `pidof <pkg>` 为空（进程确已结束）
2. `dumpsys package <pkg>` 中 `User 0: ceDataInode=…` 那行里 `stopped=false`
3. `dumpsys alarm` 中目标闹钟仍在（含正确的 `origWhen`）

**通过判据**（实测 日历_83）：进程结束后到点应依次看到
`AlarmManager: sending alarm …` → 进程被**重新拉起**（`pidof` 出现新 pid）→
`LeCalendarAlarmReceiver AlarmReceiver::onReceive … LECALENDAR_EVENT_REMINDER` →
通知栏出现 `NotificationRecord(pkg=com.zui.calendar id=… channel=high_importance_channel)`。
日志里 `isInWhiteList:false` 说明该次成功**不依赖**自启白名单，可一并作为证据。

### ⚠️ 坑-通知栏(NotificationShade)会挡住后续操作，且 `act back` 关不掉

上一条用例结束在**通知栏展开**状态时，下一条用例的第一步（例如点 `action_add_all_event` 新建）
会打在通知栏上而**完全无效**，表现为 `activity` 仍是 `NotificationShade`、`checkpoint.ok=false`，
容易误判成「应用坏了」。

- 判据：`adb shell "dumpsys window | grep -m1 mCurrentFocus"` 返回 `Window{… u0 NotificationShade}`。
- **`act back` 与 `input keyevent KEYCODE_BACK` 都关不掉它**（实测反复无效）。
- ✅ 有效手段（两个都行）：
  - `adb shell input swipe 1520 1400 1520 200 300`（从下往上滑）
  - `adb shell input keyevent KEYCODE_HOME`
- 建议：**每条用例收尾时若曾展开通知栏，先把它关掉再 `session finish`**；
  下一条用例开工前也先确认 `mCurrentFocus` 不是 `NotificationShade`。

### 通知渠道与「提示音」开关（日历_87 用）

- 渠道：`high_importance_channel`（设置页名「**高优先级提醒**」）、
  `low_importance_channel`（「**低优先级提醒**」）、
  `third_curriculum_channel`（「**课程提醒**」，`mSound=android.resource://com.zui.calendar/raw/voicetips`）。
- 日历**没有**独立的「日程提示音」开关，响铃完全由通知渠道承载。
- 开关路径：`am start -a android.settings.APP_NOTIFICATION_SETTINGS --es android.provider.extra.APP_PACKAGE com.zui.calendar`
  → 页面底部「通知类别」分组「其他」(`b=[1292,1416][1392,1489]`，点它展开)
  → 「高优先级提醒」(`b=[1424,1596][1724,1669]`)
  → 该页「提示方式」组：**默认** / **静音**（副标题「不发出提示音，也不振动」）。
- 读回验证：`dumpsys notification --noredact | grep "mId='high_importance_channel'"`，
  看 `mImportance` 与 `mUserLockedFields`：
  - 静音后 `mImportance` 由 **4 → 2**、`mUserLockedFields=24`；
  - 改回「默认」后 `mImportance=3`、`mUserLockedFields=25`。
- **判定「有没有响」不要靠听**，用 logcat 检索
  `RingtonePlayer|NotificationPlayer|MediaPlayer|AudioFocus|playSound`：
  有声时会出现 `RingtonePlayer: onCompletion() abandoning AudioFocus` +
  `NotificationPlayer: About to release MediaPlayer piid:…`（`callingPack=com.android.systemui`）；
  静音时该检索**为空**。这是最干净的有声/无声判据。
- 注意：`cmd media_session volume --stream 5 --set 0` 与 `settings put system volume_notification 0`
  在本机**都不生效**（`dumpsys audio` 里该流音量不变），不要指望用它们静音。

### ⭐ Toast 文本怎么抓：看 logcat 的 `uidumper: detect toast:[…]`

Toast 是悬浮窗、存在时间只有 2~3 秒，**`uiautomator dump` 基本抓不到**
（dump 本身耗时约 1s，等 dump 完 Toast 已经消失，结果里只剩 Activity 自己的控件，容易误判成「没有 Toast」）。

✅ 可靠做法：**点之前先 `adb logcat -c`，点完直接查 logcat**，会出现两行：

```
I ToastUI : showToast display pname=com.zui.calendar
I uidumper: detect toast:[备份成功]      ← 方括号里就是 Toast 的确切文本
```

实用于「备份成功 / 恢复成功 / 保存成功」这类以 Toast 为唯一反馈的用例（日历_89/_90/_92）。
另外 `WindowManager: Window{… u0 Toast} state from HAS_DRAWN to NO_SURFACE` 可用来确认 Toast 何时销毁。

### 页面-设置（GeneralSettingsActivity）滚动到底才看得到「其它」

`.settings.GeneralSettingsActivity` 很长，**「备份与恢复」在页面最底部「其它」分组里**，
需要滚动 2 次才可见，别在首屏找不到就以为没有：

| 项 | rid | 位置 |
|---|---|---|
| 每周起始日 | `title`/`summary` | 上半部 |
| 抢火车票设置 | `title`/`summary` | 上半部 |
| 时区 / 使用自定义时区 / 自定义时区 | `title`/`summary` | 中部 |
| **其它**（分组标题） | `title` | 底部 |
| **备份与恢复** | `title` | 「其它」下 |

- 相关页面：`.settings.LenovoSettingsBackupActivity`（标题「备份与恢复」，
  `backup_button`「一键备份」b=[44,1590][1465,1728]、`restore_button`「一键恢复」b=[1575,1590][2996,1728]、
  「自定义恢复」`title` b=[165,394][415,467]）、
  `.common.backup.SelectRestoreActivity`（标题「自定义恢复」，备份条目 rid=`text1`，
  命名格式 `日历_YYYY年M月D日_H时M分S秒`，`go_to_import_text`「从其它位置导入日程」）。
- 时区选择页：`.settings.LenovoSettingsActivity`（标题「时区」，条目 rid=`text1`，
  首项为「(GMT+8:00) 北京」，选中后**自动返回**并回显到「自定义时区」行的 `summary`）。

### ⚠️ 本机日历是「两层数据」：UI 与 `com.android.calendar/events` 不是同一份

做「清除日历数据」「以 DB 计数判断事件数量」这类用例前必须知道：

- **事件不在 `com.zui.calendar` 的数据目录里**。`content://com.android.calendar/events`
  由系统组件 **`com.android.providers.calendar`**（`/system/priv-app/CalendarProvider`，
  dataDir=`/data/user/0/com.android.providers.calendar`）承载。
- 因此 `pm clear com.zui.calendar` **清不掉任何事件**（实测清完仍 19 条），
  它只重置应用自身的设置（会出现首次运行的权限弹窗、课程表引导，「抢火车票设置」回到「暂不显示」、「每周起始日」回到「星期一」）。
- 真正让 provider 归零要用 `pm clear com.android.providers.calendar`（实测 19 → 0）。
- **但日历 UI 读的不是这一层**。实测出现过
  「`content query` 查得 **0** 条，而日历月视图/纵览仍显示 T83HT/T83KILL/T83RUN 等事件」，
  即 UI 走的是自带同步缓存（联想账号 `com.lenovo.lsf.account` / `com.zui.sync.calendar` 体系）。

**结论**：判定「事件有没有、有几条」**一律以界面观察为准**，
`content query` 只能当辅助；两者冲突时以 UI 为真（与 SKILL 里「UI 观察为主证据、DB 为辅助」一致）。

### ⚠️ 启动日历会触发云同步回灌事件

清除数据后**只要启动日历应用**，就会跑一次联想账号同步并把事件插回来，logcat 可见：

```
E ZuiCalendar: LenovoSyncJobService LenovoSyncJobService$c::onEventResult[28] synctest onEventResult:92
E ZuiCalendar: synctest yy0::insertLeReminder[5] reminder insertLeReminder
E ZuiCalendar: LenovoSyncJobService LenovoSyncJobService$c::onFinish[28] synctest onFinish:92
```

- `sync_events=0`（`content update … --bind sync_events:i:0 --where _id=1`）与 `am force-stop` 都**拦不住**这次拉取。
- 不启动应用时，`pm clear com.android.providers.calendar` 的结果能稳定保持（实测 60 秒后仍为 0）。
- 关联账户：`18310108015` / `com.lenovo.lsf.account`；日历账户名 `PC Sync`（`account_type=LOCAL`）。

### ⚠️ 备份文件在应用私有目录，「清除日历的数据」会把备份一起删掉

「备份与恢复」（`.settings.LenovoSettingsBackupActivity`）的产存放在 **应用的 `getFilesDir()`**。
所以「已备份过 + 清除日历的数据」这组预置条件在本机**互相冲突**，清数据后点「一键恢复」得到：

```
E ZuiCalendar: BackupRestoreUtils cq1$a::b[124] yykkmm getFilesDir failed
I uidumper: detect toast:[未找到可用的备份记录]
```

另外，即使事件为空时点「一键备份」，仍会提示「备份成功」，
但日志同时给出 `BackupRestoreUtils ta::h[53] yykkmm isEmpty no events` ——
**「备份成功」不代表备份里有内容**，判读时要把这条日志一起看。

### 设置页入口（补充）

- 「备份与恢复」在 `.settings.GeneralSettingsActivity` **最底部「其它」分组**，需滚动 2 次。
- `LenovoSettingsBackupActivity` 内「自定义恢复」整行 rids 为 **`selecte_restore_`**
  （b=[44,344][2996,517]，点中心 **(1520,430)** 有效；点偏了不会跳转），
  不要只按 `title`(b=[165,394][415,467]) 的位置去点。

### ⚠️ 负一屏（桌面左侧资讯页）相关：开关位置与「预授权弹窗」不可重现

做「负一屏日历权限弹框」这类用例前需知道：

**1. 桌面设置入口是「长按桌面空白处」，注意别长按到负一屏上**

- 桌面空白处长按 → 弹出编辑菜单：`btn_wallpaper`「壁纸和主题风格」、
  `btn_widgets`「添加小部件」、**`btn_settings`「桌面设置」(b=[1959,1562][2171,1773]，点 (2065,1667))**。
- 点「桌面设置」跳转 **`com.zui.launcher/.settings.ZuiLauncherSettings`**
  （也可直接 `am start -n com.zui.launcher/.settings.ZuiLauncherSettings`）。
- ❌ 若长按落在了负一屏页面上，只会唤出负一屏自身内容（屏幕使用时间等），**不会**出现桌面编辑菜单。

**2. 负一屏开关的位置**

`ZuiLauncherSettings` 首行即「负一屏」：
`title`「负一屏」 + `summary`「在桌面负一屏开启天气、资讯等服务。」，
右侧是 **`android:id/switch_widget`**。

⚠️ **别点错行**：该页所有行的开关 **rid 完全相同**（都是 `android:id/switch_widget`），
**只能靠 `bounds` 的 y 区分**。定位方式：先取「负一屏」这个 `title` 节点的 bounds 取 y，
再找 y 最接近它的那个 `switch_widget`——不要按"第几个 switch"去数。

> 📌 **历史记录自相矛盾，已作废**：本卡旧版曾写"负一屏 switch b=[2759,342]"，
> 与 2026-10-14 实测（b=[3585,311]，同机型 TB522FU）**不符**。
> 原因就是上面这条——旧记录把**行号/坐标当稳定数据**记了下来。
> **结论：本机坐标不可信、不可复用，一律现场从 dump 取 bounds。**

**2.1 负一屏开关状态怎么判（重要）**

- 开关是 `android:id/switch_widget`，**用 `checked` 属性判**：`checked="true"` 为开，`"false"` 为关。
- ⚠️ **`settings get secure settings_contextual_home` 在本机返回 `null`**，且与开关状态**不对应** ——
  不要用它判断负一屏开没开（旧记录说它=1 时 HOME 落负一屏，本机读不到该键）。
  判断开关只看 dump 里对应行的 `checked`。
- 点完开关**必须回读一次**：动作返回 `ok` 不代表开关变了，可能因页面未加载完而点空。
  实测点一次即 `checked: false→true`，且 **home 后重进设置页仍为 true**（非瞬时态）。

**2.2 链路（每步都带 --expect 断言落点）**

```
长按桌面空白          --expect rid=btn_settings                → 编辑菜单
点 btn_settings       --expect activity=ZuiLauncherSettings    → 桌面设置页
点该行 switch_widget  （无 expect，靠回读 checked 判定）        → 负一屏开关
```
⚠️ `--expect` 只吃 package 已渲染出来的锚点；开关切换**不能用 expect 断言**，
必须再 `observe` 回读 `checked`（见 2.1）。

**2.3 ⏱️ 为什么「长按桌面 → 桌面设置 → 开负一屏」感觉慢（实测归因）**

单步墙钟 **4.4~6.7s**，其中**真正点下去的动作只占 0.1~1.4s**，其余全是固定开销。
连跑 3 次最小动作（`act home`）实测：**6.67s / 6.48s / 6.36s**——与"这步要干什么"几乎无关。
一次 `act home`（合计 5316ms）的构成：

| 阶段 | 耗时 | 说明 |
|---|---|---|
| `permission` | ~1.2s | **每次动作都全量探测权限弹窗** ⚠️ **这是故意的**：为防"页面卡在权限框点不到"，不要当浪费去掉 |
| `device_connect` | ~1.1s | 每次调用**重新建立设备连接** |
| `png_save` | ~0.65s | 截图落盘 |
| `settle_wait` | 0.6s | 动作后固定等待 |
| `screenshot` | ~0.47s | 3840×2560 全尺寸截图 |
| `device_import` | ~0.41s | 每次**冷启动 Python 重新 import uiautomator2** |
| `foreground`/`rotation` | ~0.3/0.2s | 取前台与旋转 |
| **动作本身** | **0.1~1.4s** | 真正干的事 |

**根因**：工具是**每次调用一个独立进程**（`run.py` 启动 `act.py`），
"导入+连接+权限探测+截图+固定等待"这 ~4s 固定成本**每一步都要重付**。

**⚠️ 没有"批量动作"原语**：`act steps` 是**按路径图从节点 A 导航到节点 B**
（`--from/--to`），**不是**批量执行一串裸动作。别指望用它把 N 个动作塞进一次调用。
要省固定开销只能改工具（常驻服务复用连接+import，可省 ~1.5s/步），
`permission` 那 1.2s **不要动**。

**⚠️ 比工具开销更值钱的教训**：2026-10-14 实测一条"长按→设置→开开关"三步走了 **5 分钟**，
其中工具执行仅约 15s，**其余 ~285s 是 AI 自己在两次调用之间的空转**
（每步重新全量解析输出 + 反复插入无用的 `observe`）。
**先查自己的调用方式，再查工具。**

**还有一个尖峰**：某些 observe/act 会到 **13s+**（实测连续 3 次），
原因是那几次 `permission` 阶段飙到 **5.2~5.3s**（正常 1.2s）——
权限探测在无弹窗时仍有轮询等待，画面变化时会跑满窗口。这是**当前最大的单点抖动源**。

**3. HOME 落在负一屏还是主屏 —— ⚠️ 本机该键读不到，此条不可用**

- 📌 旧记录说：`settings get secure settings_contextual_home` → `1` 时 HOME 落**负一屏**，`0` 落主屏。
- ⚠️ **2026-10-14 实测该键在本机返回 `null`**（`contextual_home` / `negative_screen` 同样为 null），
  且**与负一屏开关的实际状态不对应**（开关已 `checked=true`，该键仍为 null）。
- **结论：不要用这个键判断负一屏状态，也不要指望用它切主屏/负一屏。**
  判断开关状态一律读设置页 `switch_widget` 的 `checked`（见 2.1）。
- 本机实测没再出现"HOME 赖在负一屏退不掉"的现象；若再次遇到，
  **先怀疑前台不是 launcher 主屏，而不是这个设置键**。

**4. ⚠️ 负一屏的「预授权弹窗 / 日历权限弹窗」在本机**无法重现

- 该弹窗**只在「首次授权」时出现一次**，之后开开关都是**静默生效**、不弹任何对话框。
- **`pm clear com.zui.launcher` 清不掉这个授权态**：复位桌面数据（负一屏回到关闭、桌面恢复默认布局）后
  再开开关，**依然不弹预授权弹窗** —— 说明授权记录保存在 launcher 应用数据之外
  （随账号/服务端持久化），本机无法回到「首次开启负一屏」的初始状态。
- 因此凡涉及「负一屏首次授权的预授权弹窗 → 日历权限弹窗」的用例，在本机**只能如实记为无法验证**。

**5. 本机负一屏不含「今日日程」卡片**

当前负一屏卡片组合为：**服务与反馈、天气 2x2、文件夹、应用商店、安全中心、设置、天禧智能体、超级文件**
（已上滑翻查整屏确认）。用例备注里「未获得日历权限后『今日日程』卡片呈现缺省态」在本机**没有对应卡片**。
另：`com.zui.launcher` 的 `READ_CALENDAR` 为 **granted=false**，但负一屏卡片仍正常显示。

## 倒数日小部件（widget 2x1）与倒数日列表/详情页

> 实测于 2026-10-14（会话 #432，用例 121）。**本卡约定不写坐标**，只写 rid/文案/activity，
> 定位时用 `observe` 现场取 bounds。

### 三个页面要分清（用例常把它们混为一谈）

| 页面 | activity | 怎么进 | 关键 rid |
|---|---|---|---|
| **倒数日列表页** | `.widget.countdown.CountdownListActivity` | 点桌面 widget 占位区 / 长按 widget→「编辑小部件」 | `toolbar_title`/`countDownTip`/`date_left`/`todaynull_tv`/`bottom_tv`/`countdown_action_add` |
| **新建倒数日页** | `.reminder.EditCountDownActivity` | **只能**从列表页点右上 `countdown_action_add` | `countDownTvTitle`/`countDownLlDate`/`countdownLlReminder`/`countDownTvDescription`/`save_view` |
| **倒数日详情页** | `.reminder.CountDownInfoActivity` | **只能**从日历首页 agenda 条目 `agenda_item_text_container` 点该倒数日 | `countTvTitle`/`countTvDate`/`countTvReminderLabel`/`countTvOtherDate`/`bottom_bar` |

⚠️ **最易踩的坑**：**点 widget 永远不会进「新建倒数日页」，也永远不会进「详情页」**，
一律只到 `CountdownListActivity`（无论有无数据）。用例若写"点小部件进新建/详情页"，
实测**不成立**——新建页要点列表页的 `+`，详情页要从**日历 App 首页 agenda** 进。

### 桌面 widget 的 rid 与占位态

- 桌面 widget 本体在 UI 树里以 `content-desc="倒数日 2x1"` 暴露（无 rid）；
  其内部节点是日历进程的：容器 `countdownLlNo`（`clickable=true`）、
  占位文案 `countdownTvNo`（`clickable=false`）。
- **无数据占位文案是「暂无倒数日」**（不是"无倒数日"/"无事件"）。
- 列表页无数据时是 `todaynull_tv` = **「无事件」**（与此不同，别混）。
- ⚠️ **点 widget 请点 `countdownLlNo` 的中心**；`countdownTvNo` 自身 `clickable=false`。

### ⌨️ 长按 widget 本体 → 编辑菜单（步骤3 的备选路径）

长按**桌面 widget 本体**（不是长按桌面空白）弹出两项：`编辑小部件` / `移除`。
点「编辑小部件」进入 `CountdownListActivity` —— **这就是用例说的"编辑菜单页"**。
（注意：长按桌面空白出的是 launcher 菜单「壁纸和主题风格/添加小部件/桌面设置」，不是这个。）

### ✅ 已验证行为（2026-10-14）

- 无数据时点 widget → `CountdownListActivity`，无数据空态 `todaynull_tv='无事件'` ✅
- 点 `countdown_action_add` → `EditCountDownActivity`（`toolbar_title='新建倒数日'`）✅
- 新建页填 `countDownTvTitle` → 点 `save_view`「完成」→ 回列表页并出现该条目 ✅
  （列表页从「无事件」变为 `title`=条目名 + `time1`='全天'；日历首页 agenda 也同步出现）
- 详情页点左底部按钮 → 二次确认框 `alertTitle='确定删除此事件吗'`，`button1`='删除'/`button2`='取消' ✅
- 点「删除」→ 回日历首页且 agenda 条目消失；回桌面 widget 恢复「暂无倒数日」✅

### ❌ 已实测的缺陷（2026-10-14，用例 121，FAIL）

1. **有数据时 widget 不展示最近一条**：建好倒数日后回桌面，widget **仍显示「暂无倒数日」**。
   已排除刷新延迟（切页+等待后重取仍为占位）与取错页（桌面仅一页，bounds 一致）。
   ⚠️ 同现象在 **#425** 已出现过一次，**两次独立会话复现**，非偶发。
2. **列表页点选倒数日卡片不返回桌面**：条目容器 `click_layout` 虽 `clickable=true`，
   点其中心/左/右三种位置**均无任何反应**（activity 不变、无选中态、无 checked 变化）。
   用例预期"点选中的倒数日卡片即返回桌面，切换完成"→ 不成立。
3. **列表页无选中态**：点击后 `checked=true` 数为 0、无 `selected` 属性，无法判断"选中的是哪个"。

> 另注：`com.zui.calendar` 的倒数日数据不通过 provider 暴露，
> `content query` / `sqlite3` 直接查不到，**验证数据是否存在要靠 UI 回显对比**
> （列表页「无事件」↔ 条目名、日历首页 agenda 是否出现），不要因"查不到库"误判未保存。

## 页面-列表页（全部课程表）


- 标题「全部课程表」；列表项 `tv_schedule_name`（名称）/ `tv_default_badge`（「当前」角标）/
  `tv_schedule_info`（「设置」）
- 顶栏两图标：`action_schedule`（文案「编辑」）、`action_add_schedule`（文案「添加课程表」）

### 设为当前课程表

- 确认框「**确定设为当前课程表吗？**」+ 取消(`android:id/button2`) / 确定(`android:id/button1`)
- 确定 → 「当前」角标移到选中行；取消 → 仅关框不改状态
- 删除弹框文案**随勾选数量变化**：「确定要删除选中的 **N** 个课程表吗？」

### 坑-删除全部课程表后的两种"空"

| 时机 | 落点 | 三按钮 |
|---|---|---|
| 删除全部**当场** | 列表空态「没有课程表」+「点击 + 创建新课程表」 | ❌ 无 |
| **重新进入**课程表 | 引导式空状态页「还未添加课程表」 | ✅ 三个都在 |

删光后列表页**不会自动跳转**，要**离开再回来**才回到引导式空状态页。
断言"空状态三按钮"必须补"重新进入"这一步，否则误判 FAIL。

### 多选态与置灰规则

点 `action_schedule` → 多选态：出现 `checkbox_select` + `bottom_bar` +
`btn_set_default` / `btn_delete`。**置灰规则看 `enabled` 字段**：

| 勾选情况 | btn_set_default | btn_delete |
|---|---|---|
| 仅勾选**当前**课程表 | `false`（置灰） | `true` |
| 勾选**多个**（含当前） | `false`（置灰） | `true` |
| 仅勾选**非当前**课程表 | `true`（可用） | `true` |

即：**勾选集合含"当前课程表" → 设为当前置灰**。

⚠️ 删除弹框文案「确定删除**此**课程表吗？」区别于课程卡片的
「确定删除**此课程**吗？」（后者是删单条课程）。

- 新建的课表**不自动抢占"当前"**

## 坑-课程表空状态

- 三个入口按钮**直接铺在屏幕上**，**此时没有工具栏** —— 不要再去找「更多」菜单
- 容器 `emptyView`；文案「还未添加课程表」

## 链路-导入弹窗顺序（拍照/图库通用，先读这条）

点「拍照导入」或「从图库导入」后是**两段式、有先后**：

1. **App 内提示弹窗**「请确保图片清晰、完整」→ 点「知道了」
   - 日历自己的框，**与系统权限弹窗无关，无论权限是否已授予都会弹**
   - **必须关掉，否则后面什么都不发生**：不点掉，相机 25s 都不拉起、PhotoPicker 也不出现
2. **系统权限弹窗**（相机/照片）→ **仅当对应权限未授予时才弹**；已授予则跳过

测权限行为前若上一轮已允许过，需 `revoke` 重置前提，否则第 2 段根本不出现。

## 坑-图库导入解析

- 解析需联网（约 20s），**等待窗口给到 60s**；低于 20s 会在解析中途误判超时
- **选图别盲点第一张**：照片 tab 按媒体库时间倒序、所有缩略图**共用
  `icon_thumbnail` 一个 rid** → 第一张取决于媒体库最近有什么（其他用例拍照/截图会插队）。
  要先看缩略图内容再选
- **解析失败弹窗（模态，非 toast）**：标题「图片内容不是课程表」+「知道了」
  - 这是**通用解析失败框**：选错图会弹、**真课程表云端解析偶发失败也弹**
    → 不能据此断定"图选错了"
  - 预检通过的候选被弹此框 = 偶发失败 → 重试同一张；总次数上限 3 次，
    用尽 BLOCKED，**禁止归因为"超时/需联网"**

## 坑-必填校验（与规格不符）

规格说"名称为空时完成按钮**置灰**"，实测**不置灰**：

- 新建课程表页 `save_view`、图库导入确认页 `btn_finish`：`enabled=true`、`clickable=true`
- 点击后**被校验拦住**（页面停在原页、无 toast）
- 即"必填约束存在、表现形式与需求不符" → 按用例记 FAIL 并注明实际行为
- **置灰断言**：日历的置灰是「整体变淡」，UI 树 `enabled/clickable` 可能不变；
  以截图判读，未配置视觉凭据时降级 WARN（不是 FAIL）

## 坑-拍照导入返回

从相机返回：返回键可能被取景器吃掉，`am force-stop com.zui.camera` 更快更稳；
且退回的是**日历主界面**（不是课程表页），需重新走「更多 → 课程表」。

## 页面-新建课程表（`EditTimetableActivity`）

- 名称默认空且**必填**（图库导入确认页才有预填名）
- 保存 = toolbar 右侧 `save_view`（文本「完成」，clickable），**不是** `btn_finish`；
  `action_save` 是其不可点父容器
- **不要按 back 收键盘** —— 见 `_system.md`「BACK 键-两段式」（有输入法时第 1 次只收键盘，
  第 2 次才回退，容易多退一级或丢未保存的编辑）
- 两个开关**默认均关闭**（`switch_weekend_classes`/`switch_show_non_current_week`）；
  图库导入确认页的「显示非本周课程」可能默认开启，两者默认态不同
- ⚠️ **本页就是「课程表基本信息编辑页」**：用例前提写"手动创建后进入基本信息编辑页"时停在本页即可
- ⚠️ **点「完成」保存后的落点是周视图**（不是列表页），且**新建的课表名不会作为
  可点列表项出现**

## 页面-图库导入确认页（`TempConfirmTimetableActivity`）

- 断言用控件：名称 `et_schedule_name`（**预填**「学生课程表」/ 学期名）、完成 `btn_finish`、
  学期开始 `layout_semester_start_date`/`tv_semester_start_date`、
  当前周数 `layout_current_week`/`tv_current_week`、总周数 `layout_total_weeks`/`tv_total_weeks`、
  周末有课 `switch_weekend_classes`、显示非本周 `switch_show_non_current_week`
- **学期总周数弹框 = 滚轮选择器**：当前值居中高亮，数字为 Canvas 绘制 **dump 读不到**
  → 用 `read.py` OCR 定位数字坐标后点选
- 学期开始点开为日期选择框：`rid=datePicker`，标题形如「2026年9月1日 周二」，
  按钮是 `left_button`=取消 / `right_button`=确定（⚠️ 不是 `android.widget.DatePicker`，
  也不是 Compose —— 实测 TB323FU/9.0.0.83 为该结构；不同版本控件树可能不同，
  **用 `rid=datePicker` + `left_button`/`right_button` 定位，别认类名**），「取消」关闭
- ⚠️ **总周数弹框的按钮 rid 与日期弹框不同**：总周数是系统对话框
  `button1`=确定 / `button2`=取消；日期框是 `left_button`/`right_button`。别混用

## 页面-确认页导航关系（拓扑）

- 「确认课程表基本信息」页点左上角返回 → 「确认识别结果」页
  （`TempTimetableActivity`），**不是**课程表页也不是主页；要重进就再点「下一步」
- 识别结果页顶栏右侧**没有图标**
- 确认页点「完成」→ 创建课程表 → 课程表列表（含「全部课程表 / <课表名> / 当前 / 设置」）；
  多课表时**每个课表名下各带一个「设置」**
- 「当前」标签跟在**原有**课表后面 —— 新导入/新建的课表**不会自动抢占当前**

## 页面-课程时间设置页

入口：编辑页的「课程时间设置」行 `layout_time_settings`（**不是**
`layout_time_slot_settings`）。Activity
`com.zui.calendar/.timetable.management.TimeSlotSettingsActivity`。

默认值都有独立 rid，**可直接断言，不用 OCR**：

| 字段 | rid | 图库导入路径 | 手动创建路径 |
|---|---|---|---|
| 每节课上课时长 | `tv_lesson_duration` | 30分钟 | 50分钟 |
| 课间休息时长 | `tv_break_duration` | 10分钟 | 10分钟 |
| 上午课程节数 | `tv_morning_slot_count` | 4节 | 4节 |
| 下午课程节数 | `tv_afternoon_slot_count` | 5节 | 4节 |
| 晚上课程节数 | `tv_evening_slot_count` | 0节 | 4节 |

**默认值因创建路径不同** → 按实际路径断言，跨路径断言会误判。

- 时长两行的**可点容器**是 `layout_lesson_duration` / `layout_break_duration`；
  `tv_lesson_duration` / `tv_break_duration` 只是值节点（`clickable=false`），点值节点无效
- 节次明细「第1节」与「08:00-08:30」是**两个独立文本节点** → 断言要分别匹配，
  别要求同一节点同时含「第」和「-」（否则永远匹配不到、把正常产品误报成 FAIL）
- 两个时长弹框的内容区是 **Canvas 自绘**（`customPanel` 下无 dump 子节点），当前值读不到：
  只断言弹框打开（标题 + 取消/确定），数值用 `read.py` OCR 补读并记 INFO ——
  弹框半透明，OCR 会混入背景文字，**不可作断言**
- 「上午课程 / 下午课程 / 晚上课程」是**分组标题**（`clickable=false`），点了页面不动是正常
- 返回确认页用 BACK 键；**别点本页「完成」**（会直接创建课程表）

### 坑-必须先读值再操作

**元素不在当前视口时按 rid 读返回 `''`** —— 直接判会连报 FAIL。顺序：

1. 进页面立刻读首屏值（上课时长 / 休息时长 / 上午 / 下午节数）
2. 再上滑读 `tv_evening_slot_count`（懒加载）
3. 要断言的「默认时间显示」必须**在改时长之前**采集快照

第 3 点尤其关键：**节行时间随「上课时长/课间休息」实时重算** —— 把时长拨到 30 分钟后
所有节行变成 30 分钟制，此时再断言原始时间必然 FAIL。
**更强的写法**：能"滚到可见再读"就不要依赖排好的读取顺序。

### 节行定位（共用 rid，没有 per-slot rid）

所有小节共用 `item_container` / `tv_slot_number`（第N节）/ `tv_time_range`（时间）/
`iv_arrow`（可点，desc=编辑）。定位第 N 节 = 取 `iv_arrow` **按 bounds 的 y 排序后取下标**。
跨屏读全部节行要逐屏下滚 + 并集累加（UI 树只返回当前视口，一屏装不下 12 节）。

### 节数 ＋/－ 按钮是 ImageView

「+」「－」是图形（OCR 会读成粘连文本）。固定 rid：
减号（desc=删除）`btn_remove_<morning|afternoon|evening>_slot`；
加号（desc=新建）`btn_add_<morning|afternoon|evening>_slot`。

### 课程提醒时间入口

`layout_default_reminder`（可点行）/ `tv_default_reminder`（行内值）。
入口在**编辑页** `EditTimetableActivity` 首屏，**不在课程时间设置页**。默认 `5分钟前`。

- 点开是 RadioGroup（非 Canvas，文本可读）：`不提醒 / 任务发生时 / 5分钟前 /
  15分钟前 / 30分钟前`，底部「取消」
- ⚠️ RadioButton 的 `checked`/`selected` **本 ROM 不暴露在 dump 里**（视觉有绿点，
  dump 节点是 False/缺）→ 别用 `enabled/checked` 判定；改读**行内显示值**
  （选中即时更新行内，弹框里没有"确定"）或看截图
- 点其它项即时换行内值；点「取消」/点外面关弹框都**保留新值**

### 弹框滚轮-上课时长循环、课间休息也是循环

两个轮的边界行为**实测一致，都是循环**：

- **每节课上课时长**（`{30,35,…,120}`，每档 5）：**循环** —— 实测 `120 再拨 → 30分钟`
- **课间休息**（`{5,10,15,20,25,30}`，每档 5）：**循环** —— 实测 `5 再上拨 → 30分钟`（下界回绕）、`30 再上拨 → 25分钟`（上界回绕）

⚠️ **本节曾把课间休息记为「钳制」（`30 再拨 5 档 → 30分钟`），会话473 实测推翻了该结论**：
课间休息与上课时长**同构，都是双向循环**。此前的"钳制"很可能是**拨档次数不足**
（gap 太小导致点击被丢弃，见下）造成的误判。

方向：**上=减小、下=增大**（两轮一致）。

**滚轮几何（会话473 实测，可跨弹框复用）**：Canvas 自绘，`dump` 读不到值，只能 `read.py` OCR。
选中行 `y=1025`，**格距 163px** ⇒ 上格 `y=862`、下格 `y=1192`。**按格点按（非 swipe）**。

⚠️ **拨档 `--gap` 不能太小**：`--gap 350` 时点 14 次**只生效 5 格**（点击被丢弃）；
`--gap 800` 时点 9 次**全部生效**。**别把"点了几次"当拨了几档，一律以 OCR 回读为准。**

所以「可选择 30-120 / 5-30」的验证姿势：**拨到上端核对到位 → 再越界拨一格 →
回读确认回绕到另一端**。两轮都是循环，可直接断言回绕点。
（历史注记：旧结论"不要断言必然回绕"是基于把课间休息误判为钳制而写的，**该禁忌现已解除**；
但仍须**先拨到端点、再越界一格**，不要在中间值上断言。）

### 坑-OCR 偶发漏读时别让 OCR 进判定循环

实测：目标值 5 但 OCR 在弹框内返回 None → 脚本以为"没动"继续点 → 在循环上绕圈 →
走满上限 → 原地打转 14 分钟。

**正确做法**：按**档位次数**点按（不读 OCR 反馈），点确定，**读 UI 树文本**
（`tv_lesson_duration`/`tv_break_duration`）作判据。

### 坑-`重复结束`页的『自定义结束时间』点了没反应（实测缺陷）

`.event.options.EventLastDateActivity`（重复结束页）：

- 结构：`toolbar_title`=重复结束｜『永不结束』复选框 `[121,300,321,473]` + `lastSwOver`(Switch)
  ｜`lastTvOverText`=『自定义结束时间』｜`lastTvOvertime`=日期文字
- **『永不结束』默认是勾选的**；此时自定义结束时间的日期为**灰色**不可用
- 点 `lastSwOver` 或复选框可取消勾选 → 该行文字与日期**变黑**（界面认为可交互了）
- ⚠️ **但那一行点了没有任何反应**（实测缺陷）：容器 `lastLlDate` 在 dump 里
  `clickable=true`、`bounds=[0,473][3040,646]`、center=(1520,559)，
  但在 (2685,559)/(296,559)/(1520,559)/(500,559) **4 处都点过**，
  Activity 不变、无弹框、节点无变化；logcat 显示 `ViewRootImpl enqueue motion
  ACTION_DOWN/ACTION_UP` 触摸**已投递到应用**且**无异常** ⇒ 是点击被吃掉，不是工具没点到。
  **别以为是坐标错**，换个点法也一样，直接如实记 FAIL。
- 附带：`lastTvOvertime` 默认值实测为 **2024年1月1日**，早于当前日期，初始值本身不合理

## 页面-节行编辑弹窗（双滚轮）

- 设置页 desc=编辑 的 arrow，**按 y 排序后前 2 个是「每节课上课时长/课间休息时长」行，
  第 3 个起才是节行** —— 按序号定位前先排序
- 节行弹窗 = 同款 Canvas 双滚轮（左=开始 时+分，右=结束 时+分），无 UI 节点，
  读值用 `read.py` OCR（先看选中行、再按列 x 匹配）
- **改值用点按 ±1，且用 `--repeat` 一次点完**：点选中行**下方一格=+1、上方一格=-1**，
  比滑动稳（滑动有惯性会过冲）。
  ⭐ 用 `act tap --x .. --y .. --repeat N` 一次点到位，**要走几格就传几**，不要拆成 N 次调用
  （每次 `run.py` 冷启动 3-6 秒，易撞超时；超时=动作执行了却没记账 → 会话作废）
- 修改任一小节点「确定」后会弹**「是否根据课程时长和休息时长自动调整其他课程？」**
  确认框 → 点「确定」（**真实产品流程，不是干扰弹窗**）
- **同步规则（实测）**：第1节结束改 :53 后，上午后续节 = 前节结束 + 课间休息
  （…→09:03-09:53→10:03-10:53→11:03-11:53）；**下午/晚上不受影响**；
  「每节课上课时长」仍 50分钟
- **冲突表现（上午末节结束 > 下午开始）**：冲突两节的时间文字**变红**
  （UI 树无色值，看截图判断）；点右上角「完成」→ toast
  **「课程时间有冲突，无法设置」**（窗口约 2-3s）
  - 抓这个 toast：点击后**立即** `read.py` 整屏 OCR 匹配「冲突」；
    中间多一次 observe 截图就会占满窗口
  - ⚠️ 本页右上角「完成」是 `save_view`，**bounds 中心点击可能无反应**，
    必要时带偏移；换机型先小探针校准

## 页面-权限弹窗（本 App 文案）

**通用机制（默认同意、先拒后允、rid 匹配）见 `_system.md`「权限弹窗-响应机制」
—— 那是跨 App 系统行为，本卡不复制。**

本 App 实测（Android 17）：
- **首次** = 仅在使用时允许 / 仅本次使用时允许 / **拒绝**
- **拒绝后再请求** = 同上，末项变 **拒绝并不再询问**
- MEDIA 首次 = 选择照片 / 全部允许 / **拒绝**
- 拒绝后 App 自弹提示框（**不是系统弹窗**），可作「提示需要授予权限」的断言依据：
  文案「需要权限」+「拍照和导入课程表需要相机权限，请前往设置中开启」/「访问相册和导入
  课程表需要存储权限，请前往设置中开启」，按钮 取消 / 前往设置

## 坑-周视图空格子点击

- 空格子 = `cv_empty_content`（clickable，空课表里 25 个）
- **真实链路（2026-10 实测复核，两次 dump + 视觉确认）：点两次，中间会有加号**
  1. 点空格子 → activity **不变**，UI 树里**新增 `iv_add_hint`**（就是那个「+」号）
  2. 点 `iv_add_hint` → 进 `EditCourseActivity`（添加课程页）
- ⚠️ 以下是**已被实测证伪的旧记载**，不要再照做：
  - ❌「加号中间态已移除 / `iv_add_hint` 不存在」—— **错**，第 1 次点击后它就出现
  - ❌「点空格 → 直接进添加课程页」—— **错**，第 1 次点击只出加号
  - ❌「第二次点**空格子**才进」—— **错**，第 2 次要点的**是加号 `iv_add_hint`**，
    不是再点一次格子
- 实测数据（25 格空课表，点第 13 格）：
  | 阶段 | `iv_add_hint` | 空格子数 | activity |
  |---|---|---|---|
  | 点之前 | 不存在 | 25 | TimetableActivity |
  | 第 1 次点格子 | `[1590,1242][1722,1374]` | 25 | TimetableActivity |
  | 第 2 次点加号 | 消失 | 0 | EditCourseActivity |
- 「+」是图无文本/desc，**判定用 rid `iv_add_hint`**；XML 里查不到时再上视觉确认

## 周视图结构（断言用）

- toolbar 标题 = 课表名，副行 = 当前周数（第N周）
- 结构 = viewPager + 表头周几（`tv_monday…tv_friday` + `_date`）+
  recyclerView 网格（`tv_section` 节号 + `tv_time`）

## 页面-添加/编辑课程 `EditCourseActivity`

「新建课程」与「编辑课程」是**同一个页面**：

- 字段 rid：课程名 `etCourseName`（必填）/ 教室 `etClassroom` / 备注(老师) `etTeacher`；
  课程时间 `llCourseTime` / `tvCourseTime`（默认"第1节"）；上课周数 `llCourseWeeks` /
  `tvCourseWeeks`（默认"第1-20周"）；背景色 `llCourseColor`（行显示 `viewColorIndicator`）
- **底部「删除课程」= `btnDelete`**（仅编辑态有）；右上角「完成」= 保存退出；左上角 desc=关闭
- **「课程背景色」点行弹独立 AlertDialog**（`alertTitle`/`customPanel`/`buttonPanel`）。
  `customPanel` 内 **10 个纯色块**（clickable 无 text/desc）
  - ⚠️ **数色块要看图**：UI 树数 clickable 会被嵌套/装饰节点干扰（实测 25≠10），
    看当步 `shot.png` 数准确
- **布局**：`curriculum_scroll_view` 内容区在本机型一屏内 —— **所有字段都在首屏**。
  踩过的坑：误以为"在首屏之外"就无脑上滑，把本来可见的元素滚出屏幕上方、越滚越找不到
  → **读不到元素时先滚回顶部再逐屏向下找（双向）**

## 页面-课程查看/编辑/删除

**页面拓扑（别走错）**

- 列表页**点课名 = 编辑课程表**（`EditTimetableActivity`），**不是查看**
- 列表页 **BACK 一次 = 周视图**；再 BACK = 日历主页。**这是从列表页到周视图最便宜的边** ——
  别回主页重走「更多 → 课程表」（多 3 步），更别重跑「图库导入 → 保存」（约 100s）

**课程卡片 → 编辑页是两段，不是一段**：点周视图课程卡片 `curriculum_card_view` →
**先进「课程列表」弹框** `CourseListActivity`（`clCourseDetail` / `tvFirstLine` 课程名 /
`tvSecondLine`「第1节 | 第1-20周」）→ 再点 **`ivEdit`**（desc=编辑）才进编辑页。
直接按"点卡片=进编辑"写用例会挂在找不到 `etCourseName`。

**删除确认框**：标题「确定删除此课程吗？」，取消 `android:id/button2`，删除 `android:id/button1`。

- 断言用**课程数减少**，不要用"课程名消失"（同名课程有多门）
- ⚠️ **基准必须在周视图上取**：编辑页里读不到课程列表，会拿到空；
  而 `空集 ⊆ 任意集合` 恒真 → 「取消后课程仍在」会变成**无论是否真删除都 PASS 的假阳性**。
  基准务必在打开编辑页**之前**、回到周视图后取，且基准为空时按 WARN 处理而非 PASS
- ⚠️ **网格异步渲染**：进周视图立刻 dump 可能拿到空 recyclerView →
  按 rid 轮询等待，别一次性读取就下结论

## 验证要点

- 「进入某界面」用回传的 `activity` 字段判定
- 名称类输入默认覆盖已有内容
- 时间选择器点「确定」后看背景列表该节时间是否更新；新建课程保存后看列表是否出现
- 右上角「完成」= 保存并返回；对话框里「确定」= 确认当前选择

### 纪律-不要主动锁旋转

**用例不涉及横竖屏时，禁止主动改旋转设置**（`accelerometer_rotation` / `user_rotation`）。

理由：主动锁了竖屏，App 冷启动又会把 `accelerometer_rotation` 改回 1 →
会话起止 rotation 不一致，报告里全是「方向漂移」噪音、**PASS 变 WARN**。
实测：7 个用例去掉主动锁旋转后，全部从 WARN 变回干净 PASS。

- 只有**真正测横竖屏**的用例才动方向，且动作前后 observe 记录 rotation、测完恢复
- **坐标一律从当次元素 bounds 派生**，不要依赖"屏幕一定是竖的"

### 纪律-只断言 toast，不追弹窗

用例关心的是 **toast**（用户可见的结果反馈），**不关心途经的 dialog/弹窗**。
不要为"某个 dialog 是否出现"设计采样、连拍、录屏 —— 那是过度工程，
且极易把方法问题误判成产品问题。

### 纪律-判据以「用例指定的界面」为准，数据库只作辅助

用例写了「**在月视图或周视图界面观察事件**」，就必须真的切到月/周视图去看到，
**不能拿 `content query content://com.android.calendar/events` 的结果替代**。
数据库查询只能当**旁证/辅助定位**（例如判断事件是否真的落库），

- 事件类用例的**主证据 = 界面观察**：月视图事件列表里 `date_tv`（时间，全天事件显示为「全天」）
  + `title`（标题）成对出现，就是"显示对应事件标题、显示对应起始结束时间段"
- 落库与否**不等于**用例通过与否；两者结论不一致时（界面没有但库里有，或反之）
  要在 actual 里**如实写明这个矛盾**，这本身就是有价值的发现

#### ⚠️ 坑-`content://com.android.calendar/events` 只存「日程」类，拿它查别的分类必然查不到

**新建事件页左侧的 8 个分类不是同一套存储**，`content://com.android.calendar/events`
**只暴露其中「日程」一类**：

| 分类 | content provider 可见？ |
|---|---|
| 日程 | ✅ 写入 `content://com.android.calendar/events` |
| 提醒 / 课程 / 倒数日 / 生日 / 纪念日 | ❌ **不可见**（存应用内部库，无 provider 暴露） |

实测佐证：同一批建好后查 `content://com.android.calendar/events` 只有日程那条；
`content://com.zui.calendar/events` 直接抛 `Could not find provider`；
`content://com.android.calendar/reminders` 无 `title` 列。

> **后果**：拿它当"保存是否成功"的判据，会把
> **提醒/课程/生日/倒数日/纪念日 的正常保存全部误判为「保存不落库」**。
> 这是**方法错误**，不是产品缺陷 —— 曾据此错判「新建课程」「新建-生日」两条用例。

**正确判据**：按用例写的界面去查。查这些分类的落库/可见性走
**纵览表**（主页 → `iv_more` → 「纵览」）：左侧分类点 `*_layoutx`
（`all_layoutx`/`reminder_layoutx`/`event_layoutx`/`curriculum_layoutx`/
`countdown_layoutx`/`birthday_layoutx`/`creditcard_layoutx`/`trip_layoutx`），
右侧列表项 rid 以 `title` 结尾即为该分类下的条目 —— **列得出来就是保存成功**。

> ⚠️ 纵览表默认停在**搜索态**时左侧分类点了不生效（右侧仍显示上次搜索结果）。
> 点分类前先确认不在搜索态（有搜索词时点 `cancel` 退出）。

### 纪律-按「预期结果」逐条记 finding

一条用例的预期结果有几条，就记几条 finding（`expect` 写清是哪一条），
每条都要在 actual 里单独给出 PASS/FAIL/未验证 的结论和界面证据。
**不要把多条预期压成一条 finding 只给一个总 PASS** —— 那样丢信息，也看不出到底哪一条没过。

### 效率-同一批数据一次造完，再逐条操作

用例写「创建事件（提醒、日程、课程、倒数日、生日纪念日），设置提醒」时，
**一次性把这几个事件都建好，然后只挑一个做后续操作**（如通知滑动、延迟提醒），
不需要"建一个 → 测完 → 再建下一个"。日历类用例大量存在这种结构。

- 反面：把 N 个事件串行地"建一个测一个"，N 倍耗时且没必要
- 正面：批量建好 → 逐个只做用例真正要观察的那一个动作

### 效率-同一次工具往返里做完重复手势

拨滚轮、连点加减号这类「同一动作重复 N 次」，**不要拆成 N 次工具调用**
（每次 `run.py` 都要冷启动 Python，3-6 秒/次，极易触发命令超时；
超时会让动作"执行了但没记账"，会话被判定 recording incomplete 而作废）。

- 优先用**点按 ±1**（见「弹框-日期/时间选择器」），点按本身就要多次，务必在**一条命令内**循环
- 一条命令内循环仍要留足超时余量；宁可分 2-3 条命令，也不要一条塞几十次

