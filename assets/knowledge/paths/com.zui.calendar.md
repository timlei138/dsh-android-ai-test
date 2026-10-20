# com.zui.calendar 路径图（节点图）

<!-- 这是一张**有穷的图**：节点 = 落脚点，边 = 「在这认什么锚点 → 去到哪个节点」。
     节点数有限（本 App 约 12 个），可穷举；不必边探索边记。

     **怎么用**（执行前先定位）：
       1. 认自己在哪：看 activity + **屏幕上有什么锚点**（同 activity 可能多形态）
       2. grep 本文件里对应的 `## 节点名` 节
       3. 在其「出边」表里找方向 → 按锚点（降级链 rid→desc→text→坐标）点过去

     **节点命名**：`页面名-形态`（如「课程表页-空态」/「课程表页-周视图」），
     因为同一个 activity 可能有多种形态，只有名字分开了才检索得到。

     **出边的「→ 去向」写的是节点名**，可直接 grep 跳转过去。 -->

- **app**: com.zui.calendar
- **验证过的设备/版本**:
  - TB522FU / 9.0.0.110-2026.08.25-release（2560×3840）
  - TB323FU / 9.0.0.83-2026.07.22-release（1904×3040）
  - ✅ 两台上 rid / desc / text **完全一致**（仅坐标不同 → 坐标必须现场派生）

**锚点表读法**：`—` = 实测该项为空，不是"没查"。
**定位降级链**：`rid` → `desc` → `text` → 现场坐标（都不中才算失配）。

---

## ⚠️ 同 rid 多态：这类锚点**必须看文字**才能判断

有些锚点的 rid 是**共用**的，**光凭 rid 不知道点的是什么** ——
必须结合 `text`（或"当前在哪一层弹窗"）判断。踩过的都在下表：

| rid | 实测出现过的文案 | 怎么判 |
|---|---|---|
| `android:id/button1` | **删除 / 完成 / 确定** | 系统弹框的「肯定」键，**文案随弹框变** —— 必须看 text |
| `android:id/button2` | 取消 | 系统弹框的「否定」键 |
| `item_title` | 课程表 / 纵览 / 年视图 / 设置 | 四个菜单项共用，**按 text 区分** |
| `icon_thumbnail` | （无文字） | 照片选择器里**所有缩略图共用** → 只能看图按位置选 |
| `iv_arrow` | 编辑 | 时间设置页**每个小节行共用** → 按行序定位 |
| `checkbox_select` | （无文字） | 列表多选态**每行共用** → 按行序定位 |
| `action_schedule` | **编辑 / 取消** | **同 rid 双态**：普通态是「编辑」，多选态变「取消」 |
| `save_view` | 完成 | 多个页面共用（编辑页/时间设置页），但都是「完成」→ 不歧义 |

**判据**：`text` 变了就按 text 定位；`text` 为空的（`icon_thumbnail` /
`checkbox_select`）按**行序/位置**定位，并**点完必须回读状态确认**（如勾选的
`checked`），不能假设点中了。

> ⚠️ **`button1` 是最容易踩的** —— 它出现在所有系统弹框里
> （删除确认、设为当前确认、权限弹窗…），点错就是"确认了不该确认的事"。
> 点之前**先看当时屏幕上是什么弹框**。

---

## 节点索引（先认自己在哪）

> 下表**与文中 `## 节点名` 一一对应**（名字必须完全一致，才 grep 得到）。
> 标 ⚠️ 的是"有去向、但本文还没单独建节点"的页面 —— 走到那里时需要现场探索，
> 或补一个节点。

| 节点 | 认出它 |
|---|---|
| 日历首页 | activity `.AllInOneActivity` + 顶部大月历 |
| 菜单（弹层） | 从首页「更多」弹出，含 课程表/纵览/年视图/设置 |
| 课程表页-空态 | activity `TimetableActivity` + 「还未添加课程表」 |
| 课程表页-周视图 | activity `TimetableActivity` + 顶栏 desc「导入课程表」 |
| 导入方式弹层 | 从周视图「导入课程表」弹出，含 拍照/图库 |
| 图库导入流程 | 见同名节点（含 5 个子落点） |
| 全部课程表列表页 | activity `TimetableListActivity` |
| 课程表基本信息编辑页 | activity `EditTimetableActivity` |
| 课程时间设置页 | activity `TimeSlotSettingsActivity` |
| 课程列表弹层 | activity `CourseListActivity` |
| 添加 / 编辑课程页 | activity `EditCourseActivity` |
| 设置页 / 备份与恢复 | `GeneralSettingsActivity` / `LenovoSettingsBackupActivity` |
| 跨 App 的系统落点 | 相机 / PhotoPicker / 权限框等日历之外的落点 |
| 自动采集的边 | 框架自动记录的边（非人工维护） |
| ⚠️ 新建事件页 | `NewBuildActivity`（首页「+」可到，尚未建节点） |
| ⚠️ 纵览视图 / 年视图 | 首页菜单可到，尚未建节点 |

---

## 日历首页

- **认出它**: activity `.AllInOneActivity`；顶部有大月历/日期
- **识别键**: `rid=iv_more` · `text=今天`

**出边**：

| 锚点（认得出就行） | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 右上角「更多」三个点图标 | `iv_more` | — | — | → 菜单 |
| 右上角「+」新建事件图标 | `action_add_all_event` | — | — | 新建事件页 `NewBuildActivity` |
| 左上角日期区 | `top_button_date_layout` | — | — | 回到今天（同节点） |

> ⚠️ 这两个图标 **desc 与 text 都为空** —— 只有 rid 一条线索。

---

## 菜单（弹层）

- **认出它**: 首页点「更多」后弹出的菜单
- **识别键**: `text=课程表` · `text=纵览`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 菜单项「课程表」 | `item_title` ⚠️多态 | — | 课程表 | → 课程表页-空态 / 课程表页-周视图（看有无课表） |
| 菜单项「纵览」 | `item_title` ⚠️多态 | — | 纵览 | 纵览视图 |
| 菜单项「年视图」 | `item_title` ⚠️多态 | — | 年视图 | 年视图 |
| 菜单项「设置」 | `item_title` ⚠️多态 | — | 设置 | → 设置页 / 备份与恢复 |

> ⚠️ 四个菜单项**共用 rid `item_title`** → 必须按 text 区分。

---

## 课程表页-空态

- **activity**: `.timetable.display.TimetableActivity`
- **认出它**: 屏幕中央有「**还未添加课程表**」（容器 `emptyView`）
- **识别键**: `text=还未添加课程表` · `rid=btnCreateManually`
- ⚠️ **此时没有顶栏工具栏**，不要再找「更多」或顶栏图标

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 页面上「从图库导入课程表」 | `btnImportFromGallery` | — | 从图库导入课程表 | → 图库导入流程 |
| 页面上「拍照导入课程表」 | `btnImportFromPhoto` | — | 拍照导入课程表 | 提示框 → 跨 App 的系统落点（相机） |
| 页面上「手动创建课程表」 | `btnCreateManually` | — | 手动创建课程表 | → 课程表基本信息编辑页 |

> 💡 要进「课程表基本信息编辑页」时，**这条是最短路径（1 步）** ——
> 比从周视图绕「课程表设置 → 点课表行」更省。

---

## 课程表页-周视图

- **activity**: `.timetable.display.TimetableActivity`
- **认出它**: 顶栏有 desc「**导入课程表**」/「课程表设置」；主体是周网格
- **识别键**: `desc=导入课程表` · `desc=课程表设置`
- 顶栏标题 = 课表名，副行 = 第N周（点它可切周次）

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 顶栏「导入课程表」 | `action_curriculum_table_import` | 导入课程表 | — | → 导入方式弹层 |
| 顶栏「课程表设置」 | `action_curriculum_table_settings` | 课程表设置 | — | → 全部课程表列表页 |
| 顶栏「第N周」（切周次） | — | — | 第N周 | 切换课表/周次弹框 |
| 周网格里某一节课格子 | `cv_empty_content` | — | — | **点两次** → 添加 / 编辑课程页 |
| 已有课程卡片 | `curriculum_card_view` | — | （课程名） | → 课程列表弹层 |

> ⚠️ **点空格要两次**：第一次点击被"吸收"（画面无变化），第二次才进页面。
> **不要再等加号浮标 `iv_add_hint`** —— 该中间态已随改版移除，现在点第一次
> 连 UI 树都不变（详见知识卡「坑-周视图空格子点击」）。

---

## 导入方式弹层

- **认出它**: 从周视图「导入课程表」弹出的选项
- **识别键**: `text=拍照导入课程表`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 「拍照导入课程表」 | — | — | 拍照导入课程表 | 提示框 → 跨 App 的系统落点（相机） |
| 「图库导入课程表」 | — | — | 图库导入课程表 | → 图库导入流程 |

---

## 图库导入流程

从任一个「图库导入」锚点进入，是一条**串联的 5 步**（每步一个子落点）：

- **识别键**: `rid=btnDone` · `rid=btn_next` · `rid=btn_finish` · `text=请确保图片清晰、完整` · `rid=icon_thumbnail`

| 子落点 | 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|---|
| 课程表页 | App 提示框「知道了」 | `android:id/button1` | — | 知道了 | 系统照片选择器 `PhotoPickerGetContentActivity` |
| 照片选择器 | 缩略图 | `icon_thumbnail` ⚠️多态（**所有缩略图共用**） | — | 拍摄于…的照片 | 裁剪页 `CropImageActivity` |
| 裁剪页 | 右上角「完成」 | `btnDone` | — | 完成 | 解析（联网约 20s）→ 识别结果页 `TempTimetableActivity` |
| 识别结果页 | 「下一步」 | `btn_next` | — | 下一步 | 基本信息确认页 `TempConfirmTimetableActivity` |
| 基本信息确认页 | 「完成」 | `btn_finish` | — | 完成 | → 全部课程表列表页（周视图） |

> ⚠️「知道了」的 rid 是 `android:id/button1`（系统通用），与权限弹窗等**共用** →
> 必须结合当前在哪一层弹窗判断。

**基本信息确认页（`TempConfirmTimetableActivity`）的字段** ——
注意它**不是**「课程表基本信息编辑页」，两者是不同页面：

| 字段 | rid |
|---|---|
| 名称 | `et_schedule_name` |
| 学期开始时间 | `layout_semester_start_date` |
| 当前周数 | `layout_current_week` |
| 学期总周数 | `layout_total_weeks` |
| 周末是否有课 开关 | `switch_weekend_classes` |
| 显示非本周课程 开关 | `switch_show_non_current_week` |
| 课程时间设置 | `layout_time_settings` |
| **课程提醒时间** | `layout_default_reminder`（行内值 `tv_default_reminder`，默认「5分钟前」） |

> 💡 **只有本页有「课程提醒时间」入口** —— `EditTimetableActivity` 没有。

---

## 全部课程表列表页

- **activity**: `.timetable.management.TimetableListActivity`
- **认出它**: 标题「全部课程表」；列表项带「当前」角标
- **识别键**: `text=全部课程表` · `rid=action_add_schedule`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 每行的「设置」 | `tv_schedule_info` | — | 设置 | → 课程表基本信息编辑页 |
| 顶栏「添加课程表」 | `action_add_schedule` | 添加课程表 | — | → 课程表基本信息编辑页（新建态） |
| 顶栏「编辑」 | `action_schedule` ⚠️多态（多选态下变「取消」） | — | 编辑 | 多选态（出现 `checkbox_select` + `btn_set_default` / `btn_delete`） |
| 「转到上一层级」 | — | — | 转到上一层级 | 回上一步 |

> ⚠️ 「编辑」按钮**是同 rid 的双态**：多选态下它变成「取消」。
>
> ⚠️ 多选态删除确认框文案**随勾选数量变化**：
> 1 个 = 「确定删除此课程表吗？」；多个 = 「确定要删除选中的 N 个课程表吗？」

---

## 课程表基本信息编辑页

- **activity**: `.timetable.management.EditTimetableActivity`
- **认出它**: 有「课程表名称（必填）」「学期开始时间」「课程时间设置」
- **识别键**: `text=课程表名称（必填）` · `rid=layout_time_settings`
- 也叫「新建课程表」（从「添加课程表」/「手动创建」进入时标题不同，页面相同）

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 名称输入框 | `et_schedule_name` | — | （课表名） | — |
| 「课程时间设置」行 | `layout_time_settings` | — | — | → 课程时间设置页 |
| 右上角「完成」 | `save_view` | — | 完成 | 保存 → 课程表页-周视图 |
| 「删除课程表」 | `btnDeleteSchedule` | — | 删除课程表 | 删除确认框（框内「确定」= `android:id/button1` ⚠️多态，见开篇） |

> ⚠️ **本页没有「课程提醒时间」入口**（那个在基本信息确认页，见「图库导入流程」）。
>
> 💡 **要进本页时优先新建一个干净课表**，不要复用已有课表 ——
> 复用的课表带着未完成的历史状态（名称被清空过、开关被改过），
> 会让后续步骤被必填校验或残留数据干扰。

---

## 课程时间设置页

- **activity**: `.timetable.management.TimeSlotSettingsActivity`
- **认出它**: 顶部「每节课上课时长」「课间休息时长」
- **识别键**: `text=每节课上课时长` · `rid=tv_lesson_duration`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 「每节课上课时长」行 | `layout_lesson_duration` | — | — | 时长弹层（Canvas 双滚轮） |
| 「课间休息时长」行 | `layout_break_duration` | — | — | 时长弹层 |
| 每个小节的编辑箭头 | `iv_arrow` ⚠️多态（**多行共用**） | 编辑 | — | 节行编辑弹层 |
| 上午节数 − / + | `btn_remove_morning_slot` / `btn_add_morning_slot` | 删除 / 新建 | — | — |
| 右上角「完成」 | `save_view` | — | 完成 | **直接创建课程表**（⚠️ 不是返回） |

**只读字段（断言用）**：
`tv_lesson_duration` / `tv_break_duration` / `tv_morning_slot_count` /
`tv_afternoon_slot_count` / `tv_evening_slot_count` / `tv_time_range`（各小节时间段）

> **两条创建路径的默认值不同**：
> 手动创建 = 50分钟 / 10分钟 / 上午4节 / 下午4节
> 图库导入 = 30分钟 / 10分钟 / 上午4节 / 下午5节

---

## 课程列表弹层

- **activity**: `.timetable.course.CourseListActivity`
- **认出它**: 从周视图点课程卡片弹出；标题「课程列表」
- **识别键**: `text=课程列表` · `rid=clCourseDetail`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 课程条目 | `clCourseDetail` | — | （课程名 + 节次/周数） | → 添加 / 编辑课程页 |

---

## 添加 / 编辑课程页

- **activity**: `.timetable.course.EditCourseActivity`
- **认出它**: 有「课程名（必填）」；编辑态底部有「删除课程」
- **识别键**: `text=课程名` · `rid=llCourseWeeks`

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 课程名 / 教室 / 备注 | `etCourseName` / `etClassroom` / `etTeacher` | — | — | — |
| 课程时间行 | `llCourseTime` | — | — | 节次弹层（「x到x节」） |
| 上课周数行 | `llCourseWeeks` | — | — | 周数多选弹层 |
| 课程背景色行 | `llCourseColor` | — | — | 颜色弹层（10 色块，Canvas） |
| 「删除课程」 | `btnDelete`（仅编辑态） | — | 删除课程 | 删除确认框（框内「删除」= `android:id/button1` ⚠️多态，见开篇） |

**周数弹层**：顶部三单选实际文案是「**全选 / 单周 / 双周**」（不是规格写的"全部/单选/双选"）；
下面 1~N 周多选；一个都不选点完成 → toast「请选择上课周数」；
选中集合既非全选也非单双周 → 顶部三项都不高亮。

---

## 设置页 / 备份与恢复

- **activity**: `GeneralSettingsActivity` / `LenovoSettingsBackupActivity`
- **识别键**: `rid=selecte_restore_layout` · `text=备份与恢复`
- 设置页是**长列表**，「备份与恢复」在**第 3 屏**（首屏看不到，需上滑）

**出边**：

| 锚点 | rid | desc | text | → 去向 |
|---|---|---|---|---|
| 「自定义恢复」 | `selecte_restore_layout` | — | — | `SelectRestoreActivity` |

---

## 跨 App 的系统落点

| 节点 | 认出它 | 说明 |
|---|---|---|
| 系统权限弹窗 | `com.android.permissioncontroller` | 见 `knowledge/_system.md` |
| 照片选择器 | `com.android.providers.media.module` | 见「图库导入流程」 |
| 相机 | `com.zui.camera/.CaptureActivity` | 返回键可能被取景器吃掉，`am force-stop` 更稳 |
| 桌面 | `com.zui.launcher` | App 崩溃/退出后回落 |

---

## 自动采集的边

<!-- 执行时自动累积；重新执行即更新。人工可把高频边整理进上方各节点 -->
- `菜单（弹层）` --[text=课程表]--> `课程表页-空态`
- `课程表页-空态` --[rid=btnCreateManually]--> `课程表基本信息编辑页`
- `.drawer.NormalLauncher` --[countdownLlNo 倒数日部件占位区]--> `.widget.countdown.CountdownListActivity`
- `.widget.countdown.CountdownListActivity` --[countdown_action_add]--> `.reminder.EditCountDownActivity`
- `.drawer.NormalLauncher` --[desc=编辑小部件]--> `.widget.countdown.CountdownListActivity`
- `.reminder.EditCountDownActivity` --[save_view 完成]--> `.widget.countdown.CountdownListActivity`
- `日历首页` --[agenda_item_text_container C121倒数日]--> `.reminder.CountDownInfoActivity`
- `.reminder.CountDownInfoActivity` --[button1 删除]--> `日历首页`
- `课程表基本信息编辑页` --[layout_time_settings]--> `课程时间设置页`
- `菜单（弹层）` --[menu 课程表]--> `课程表页-周视图`
- `课程表页-周视图` --[转到上一层级]--> `日历首页`
- `课程表页-周视图` --[action_curriculum_table_settings]--> `全部课程表列表页`
- `全部课程表列表页` --[课表名(空课表)]--> `课程表基本信息编辑页`
- `课程表基本信息编辑页` --[转到上一层级]--> `全部课程表列表页`
- `课程表页-周视图` --[空白小节(周一第1节)]--> `添加 / 编辑课程页`
- `添加 / 编辑课程页` --[关闭]--> `课程表页-周视图`
- `全部课程表列表页` --[课表名(学生课程表)]--> `课程表基本信息编辑页`
- `全部课程表列表页` --[转到上一层级]--> `课程表页-周视图`
- `课程表页-周视图` --[curriculum_card_view(语文)]--> `课程列表弹层`
- `课程列表弹层` --[ivEdit(编辑)]--> `添加 / 编辑课程页`
- `.timetable.course.EditCourseActivity` --[button1(删除)]--> `课程表页-周视图`
- `课程表页-周视图` --[cv_empty_content(552,648)]--> `添加 / 编辑课程页`
- `课程时间设置页` --[转到上一层级]--> `课程表基本信息编辑页`
- `.AllInOneActivity` --[button1(同意)]--> `.timetable.widget.TimetableGuideActivity`
- `.timetable.widget.TimetableGuideActivity` --[timetable_guide_button(我知道了)]--> `日历首页`
- `菜单（弹层）` --[菜单-课程表]--> `课程表页-空态`
- `图库导入流程` --[button1(知道了)]--> `.CaptureActivity`
- `图库导入流程` --[取消]--> `课程表页-空态`
- `图库导入流程` --[btnCancel(上一步)]--> `课程表页-空态`
- `课程表页-空态` --[btnCreateManually(手动创建课程表)]--> `课程表基本信息编辑页`
