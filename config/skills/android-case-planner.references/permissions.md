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
