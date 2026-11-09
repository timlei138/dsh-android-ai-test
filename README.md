# Android 真机 GUI 测试机制 —— 分享包

这是跑在 **DSH（DeepSeek Harness）** 上的 Android 真机自动化测试机制。

## 先读这个

**👉 [安装说明.md](安装说明.md)** —— 安装、配置、跑第一条用例，全在里面。

## 里面有什么

```
Share/
├── 安装说明.md          ← 入口文档，先读它
├── 分享介绍.html        ← 这套机制的介绍幻灯片（浏览器直接打开，←/→ 翻页）
├── scripts/             安装辅助脚本
│   └── link-dsh-deps.ps1           给插件补 DSH 依赖链接（第 4 步 ③ 必跑）
├── toolkit/             工具层实体：Python 脚本 + 用例 + 知识卡 + Web UI
├── plugins/             两个 DSH 包
│   ├── android-tools-plugin/       把工具层封装成原生 tool
│   └── android-auto-test-preset/   两个 Agent 角色定义
└── skills/              两个 skill，都要装（每个目录里只有一份 SKILL.md）
    ├── android-gui-test/SKILL.md    执行纪律（DSH 里显示为 android-ai-test-skills）
    └── android-case-check/SKILL.md  用例够不够格跑（主 Agent 用）
```

**`toolkit/` 是唯一一份代码，`skills/` 只放纪律文档。** 两者不重复：

| | 放什么 | 谁读它 |
|---|---|---|
| `toolkit/` | 全部可执行代码、用例、知识卡 | 机器跑（DSH 插件把 `android_*` 工具指向这里） |
| `skills/<名字>/SKILL.md` | 只此一个文件 | AI 读（DSH 的 skill 机制只认 `<目录>/SKILL.md`） |

## 一句话说清它做什么

```
你（用自然语言说用例）
   → 主 Agent 判断能不能跑、派发
   → 执行子 Agent 在真机上逐步执行、每步自动取证
   → 按预期逐条判定，结论 PASS / FAIL / BLOCKED / ERROR
```

**代码管纪律**（格式、顺序、证据、判定一致性），**AI 管语义**（点哪、符不符合）。

## 要求

| | |
|---|---|
| DSH | 0.2.1-rc.2+ |
| Node.js | 20+ |
| Python | 3.10+ |
| 系统 | Windows + PowerShell（目前只验证过这个组合） |
| 设备 | Android **真机**，已开 USB 调试 |

> ⚠️ **跑测试前请人工确认设备连接**（`adb devices` 要显示 `device`）。
> 工具层不会替你修 adb，连不上就如实报错。

## 备注

两个 `SKILL.md` 是这个机制的**作业纪律**，告诉 AI 怎么用 toolkit 里那套 `android_*` 工具：
`skills/android-gui-test/SKILL.md` 是执行子 Agent 的作业纪律，
`skills/android-case-check/SKILL.md` 是主 Agent 判断用例够不够格跑。
改它们**不用重启 DSH**；改 `plugins/` 下的东西要重启（或热重载）。

`toolkit/` 里的实现细节看 `toolkit/docs/`：环境与工具 / 执行参考 / 知识卡与路径图。
