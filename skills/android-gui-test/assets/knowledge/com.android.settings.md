# 系统设置（ZUI Settings）

- **app**: `com.android.settings`
- **name**: 系统设置
- **验证版本**: 18500217（versionCode，TB522FU / Android 17）
- **最近验证**: 2026-09-09（双栏布局、deep link、导航条目属性 实测）

<!-- ⚠️ 设备无关卡：只写 rid / 文案 / activity。坐标与屏幕尺寸不写。 -->

## 布局-双栏与单栏切换

- 设置分为**左右两部分**：左侧**导航栏**（分类列表），右侧**内容区**（选中分类的详情）
- ⚠️ **平板竖屏下，同一时刻只显示导航 或 内容之一**（单栏模式切换），
  不像手机那样每级一个 activity —— 判断"当前在哪层"**不能只看 activity**，
  要看屏幕上是**分类列表**（WLAN/蓝牙…）还是**具体设置项**
- **在内容区按 BACK 回到导航栏**
- 导航栏**可上下滑动**查找（应用管理等入口在首屏之下，需滚动）

## 导航入口

- 打开设置: `am start -a android.settings.SETTINGS`
  - ⚠️ **该 intent 会恢复上次残留的子页**，不保证落在导航页。
    可靠做法：先 force-stop 再 start；落到内容区时按 BACK 回导航页
- **直达应用管理（推荐，跳过滚动查找）**:
  `am start -a android.settings.APPLICATION_SETTINGS` → 直接落在应用管理-所有应用列表
- 导航分类条目定位：分类名的节点 rid = `android:id/title`，
  **title 本身 clickable=false**，点击目标是**所在的行容器**（可点父级）
- 应用管理页右上角有**「更多选项」**溢出菜单（`desc="更多选项"`，cls=ImageButton）；
  「恢复预装应用」的固定路径见 `_system.md`「预装应用-卸载与恢复」

## 验证要点

- 判定"在导航页还是内容区"：看屏幕文字是**分类列表**（WLAN/蓝牙…）还是**具体设置项**
- 从内容区回导航后，**重新 dump 再定位**（层级切换后节点树完全不同）
- deep link 后 activity 可能读出 unknown，**以 dump 到的页面文字为准**
