# Android 真机 GUI 测试机制 —— 分享包

这是跑在 **DSH（DeepSeek Harness）** 上的 Android 真机自动化测试机制。

## 先读这个

**👉 [安装说明.md](安装说明.md)** —— 安装、配置、跑第一条用例，全在里面。

## 里面有什么

```
Share/
├── 安装说明.md          ← 入口文档，先读它
├── toolkit/             工具层：Python 脚本 + 用例 + 知识卡 + Web UI
├── plugins/             两个 DSH 包
│   ├── android-tools-plugin/       把工具层封装成原生 tool
│   └── android-auto-test-preset/   两个 Agent 角色定义
└── skills/              两个 skill，都要装
    ├── android-gui-test/    执行纪律（DSH 里显示为 android-ai-test-skills）
    └── android-case-check/  用例够不够格跑（主 Agent 用）
```

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

两个 `SKILL.md` 是这个机制的核心：`skills/android-gui-test/SKILL.md` 是执行子 Agent 的作业纪律，
`skills/android-case-check/SKILL.md` 是主 Agent 判断用例够不够格跑。
改它们**不用重启 DSH**；改 `plugins/` 下的东西要重启（或热重载）。

更细的文档在 `toolkit/docs/`：环境与工具 / 执行参考 / 知识卡与路径图。
