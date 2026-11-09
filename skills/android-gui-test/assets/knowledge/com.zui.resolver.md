# 系统「打开方式」选择器（跨应用跳转中转站）

- **app**: `com.zui.resolver`
- **name**: 系统「打开方式」选择器
- **验证版本**: 8.0.0.0158（versionCode 8000158，TB522FU / Android 17）
- **最近验证**: 2026-09-16（仅采集到包名/版本/Activity；界面项未逐条真机复核，仍是 stub）

<!-- ⚠️ 卡的文件名 = 检索键 = 前台包名。实测本机该页面的前台包名是
     `com.zui.resolver`（Activity `com.zui.resolver.ResolverActivity`），
     而 AOSP 同名包 `com.android.intentresolver` 在本机**根本没安装** ——
     用那个名字建卡会**永远命中不到**。换机型时按实际前台包名再建一张即可，内容可复用。 -->

## 界面结构

- 应用列表: 每行一个可打开当前内容的 App（图标 + 名称）
- 部分版本有「仅此一次 / 始终」选项

## 高效操作

- 点按目标 App 所在行即可选择
- 列表里找不到目标 App 时，先滑动列表再找
- 误开后可用 Back 返回原应用

## 验证要点

- 选择后前台包名应变为所选目标应用
- **判"是否在这个页面"要看包名**：`com.zui.resolver`（不是 `com.android.intentresolver`）
