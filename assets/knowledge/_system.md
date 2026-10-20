# 通用系统界面（权限弹窗 / 系统对话框）—— 兜底卡

<!-- 前台包名不在任何 App 卡时读本卡（AI 主动检索，非框架注入）。
     跨 App 的**系统行为**都写这里，App 卡不复制。
     ⚠️ 本卡设备无关：只写 rid / 文案 / 行为，不写坐标。 -->

- **app**: `_system`（兜底卡，不可删除）
- 适用包名: `com.android.permissioncontroller`、`com.google.android.permissioncontroller`、
  `com.android.systemui`、`android`

## 权限弹窗-多权限申请：同一个框，点一次换下一个

一次申请**多个**权限时，Android **不会**关掉再弹新框 ——
就着同一个弹窗，点一次「允许」后**框内换成下一个权限**继续问，直到全部问完才关闭。

**所以：看见权限框就点允许，一直点到框消失。** 不用关心有几个、分别是什么。
（默认同意已经这么做了；要测「拒绝」才用 `--perm-action deny` 声明。）

> 实现注意：判"是否卡住"**不能**用按钮文本或 activity —— 多权限时它们全都一样。
> 要看**框内申请文案**是否变化：变了=推进到下一个，没变=真点不动。

## 权限-响应机制

**默认同意，需要拒绝时显式声明**：

```powershell
# 默认：遇到权限弹窗自动点「允许」，多权限会连续点到框消失
python act.py tap --x ... --via "rid=btn_import"

# 要测「拒绝」路径时才声明
python act.py --perm-action deny --perm camera tap --x ... --via "rid=btn_import"
python session.py perm-intent --clear          # 分支测完清除
```

- 匹配按 **resource-id 优先**（与文案/语言/OEM 差异无关）
- grant 优先「全部允许 / 始终允许」，其次「仅在使用中允许」
- deny **只点「拒绝」**，绝不点「拒绝并不再询问」（会设 don't-ask-again，
  导致后续"授予"分支弹窗永不再出现）
- 「选择照片」（部分媒体）、「前往设置」**不自动点**，交回 AI 判断
- 返回里看 `permission.detected / handled / clicks / prompts / unmatched`

> **系统权限框约 6 秒未点击会自动消失**（Android 通用机制）。
> 所以**不要**用"点一下 → observe 看看 → 再决定点哪"的节奏：一次 observe 就吃掉整个窗口。
> 意图必须在动作前声明好（默认同意已覆盖大多数场景）。

## 权限弹窗-按钮文案随"是否拒绝过"变化

| 权限 | 首次请求 | 拒绝过一次后再请求 |
|---|---|---|
| CAMERA | 仅在使用时允许 / 仅本次使用时允许 / **拒绝** | 同上，末项变 **拒绝并不再询问** |
| MEDIA（照片/视频） | 选择照片 / 全部允许 / **拒绝** | 同上，末项变 **拒绝并不再询问** |
| 普通危险权限（位置/通讯录/麦克风等） | 仅在使用中允许 / 仅本次允许 / 拒绝 | 同类地变为「拒绝并不再询问」 |

**「拒绝」→「拒绝并不再询问」是系统通用行为。**

## 权限弹窗-按钮 resource-id

前缀 `com.android.permissioncontroller:id/`（AOSP）或
`com.google.android.permissioncontroller:id/`（GMS 设备）：

| 按钮 | rid 末段 |
|---|---|
| 允许 | `permission_allow_button` |
| 仅在使用中允许 | `permission_allow_foreground_only_button` |
| 仅本次允许 | `permission_allow_one_time_button` |
| 全部允许（媒体） | `permission_allow_all_button` |
| 选择照片（媒体） | `permission_allow_selected_button` |
| 拒绝 | `permission_deny_button` |
| 拒绝并不再询问 | `permission_deny_and_dont_ask_again_button` |

## 权限-前置状态重置

- **弹窗出现 = 该权限尚未授予**；已授予则不弹，直接进目标功能
- `state.py revoke --package <pkg> --perm <短名|全名>` 重置为"未授予"。
  注意 revoke 后 flags 只带 `USER_SET`（不是 `USER_FIXED`），**弹窗仍会再弹**，
  但按钮已是「拒绝并不再询问」形态
- `state.py grant` 用于**预置已授权**——只在用例明确测"已授权路径"时用
- `state.py clear` 最干净（连数据一起重置）

### 双分支用例：先拒绝、后允许

```
第一轮：声明 deny → 触发 → 验证拒绝提示
第二轮：revoke 重置（必须！否则不再弹窗）→ 清除意图回到默认同意 → 触发 → 验证功能可用
```

## BACK 键-两段式：有输入法时先收键盘

**系统级行为，所有 App 通用**：

| 当前状态 | BACK 第 1 次 | BACK 第 2 次 |
|---|---|---|
| **输入法已弹出** | 收起键盘 | 页面回退 |
| 无输入法 | 页面回退 | — |

**所以"用 BACK 收键盘"要按两次，否则会误退页面。**

⚠️ 最常见的翻车：以为键盘已经收了，按一次 BACK 想回退，
结果那一次只是收键盘（页面没动）→ 再按一次才回退 → **整体多退了一级**；
或者反过来：键盘其实已收，按一次就退出了页面，**丢了未保存的编辑**。

**正确做法 —— 先确认键盘状态，再决定按几次**：

```powershell
adb shell "dumpsys input_method | grep mInputShown"   # true = 键盘正在显示
```
```powershell
# 只想收键盘、不想退页面 → 确认 mInputShown=true 时按一次
# 想回退页面 → 先确保键盘已收（必要时按一次收键盘），再按一次回退
```

> 更稳的做法：**别用 BACK 收键盘** —— 点页面空白处、或点「完成/关闭」等页面自身锚点。

## 预装应用-卸载与恢复

### 恢复预装应用：固定路径

- **固定路径**：`设置 → 应用管理 → 恢复预装应用 → 找到目标 APP → 恢复`
  这是**唯一固定入口**，不要另找路径、也不要用命令替代。
- 判定恢复成功：APP 重新出现在「所有应用」列表，且该页按钮由「恢复」变为「打开」
- `pm install-existing <pkg>` 对**已真正卸载**的预装包**无效**
  （实测报 `NameNotFoundException`）。它只能重新启用"被禁用"的包。

### 判断 APP 是否支持卸载

- 判定依据：**桌面 / 所有应用列表长按该 APP 图标**，看菜单里**有没有「卸载」**
  - 有 → 支持卸载（实测：便签、日历）
  - 没有 → 不支持（实测：系统「设置」只有 小部件/应用信息）
- **不要**用 `pm uninstall` 是否返回 Success 来判定用户侧可卸载性 ——
  预装包常返回 Success，但那是另一套语义。

### 判定"包是否安装"必须精确匹配（真实事故）

- `pm list packages <pkg>` 是**子串匹配**：查 `com.zui.calendar` 会命中
  `com.zui.calendar.overlay.*` 等主题包 → **永远认为已安装**。
- 必须按行精确匹配：`("package:" + PKG) in [l.strip() for l in output.splitlines()]`
- 事故后果：误判"已安装"会让卸载检测恒为 False、兜底恢复永不触发，
  最终把设备留在"APP 已卸载"状态。带销毁性的卸载用例，兜底必须走
  「恢复预装应用」UI + 精确匹配校验。

## 验证要点

- 点击后弹窗应消失，界面回到原应用
- 判定"进入某界面"用回传的 `activity` 字段，不要用界面文字猜测
