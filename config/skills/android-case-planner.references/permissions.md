# 权限是预期的一部分

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

**`check` 能写的键**（代码认这些，写别的等于没写）：

```
activity      activity_changed_to     package
node_present  node_absent             rid_present
node_text     text_present            nodes_present
dialog_present  dialog_present_on_tap  dialog_on_tap
after_tap     permission
```

其中：
- `permission` 判的是**被测 App 的授权结果**（`granted_permissions`），
  不是"弹窗弹过没有"
- `after_tap` 是嵌套子判据（"点了之后应出现什么"）
- `node_present` / `dialog_present` 里面写 `{rid: x}`，**只认 `rid`**

⛔ **`dialog_present` 里写 `{permission_dialog: ...}` 会被丢弃**
（虽然会报警，但那条断言仍然没验）。想判权限就给 `permission`。

**关键：权限只看被测 App 的。** 判归属**只看包名** ——
系统权限弹窗包名固定 `com.android.permissioncontroller`，
App 自己的弹窗包名就是被测 App。不要用弹窗文案去猜（文案会骗人）。
把两者混在一起，就会出现"权限明明弹了却判 FAIL"。

> 真实案例（169）：用例预期写"点拍照导入应弹相机权限弹窗"，执行时
> 日历的权限框确实弹了、也允许了、相机也打开了 —— 但因为**没有出现相机
> App 的权限框**，被判成 FAIL。看错对象了。

## 双分支（允许 + 拒绝）：拆成两个用例跑

一个用例往往要验"允许后能用"和"拒绝后有提示"两条，**不能塞进一条**：

1. **先拒绝、后允许** —— 拒绝会改变弹窗形态（多出「不再询问」），
   让"允许"落在确定的第二形态上
2. **第二轮必须先撤销权限** —— 权限已授予时系统**不会再弹窗**，
   不撤销就直接走不通（表现像"没弹框"，其实是根本不弹）
3. **第二轮记得清掉 deny 意图** —— `perm_action` 要重置回 `grant`

> 派发时告诉子 Agent 该用哪个分支（`perm_action: grant` / `deny`），
> 别让它自己猜。默认 `grant`，只有测拒绝分支时才传 `deny`。
