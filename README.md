# Android 真机 GUI 测试机制 —— 分享包

这是跑在 **DSH（DeepSeek Harness）** 上的 Android 真机自动化测试机制。

## 先读这个

**👉 [安装说明.md](安装说明.md)** —— 安装、配置、跑第一条用例，全在里面。

## 里面有什么

```
<包根>/
├── 安装说明.md          ← 入口文档，先读它
├── README.md            ← 本文
├── Linux安装记录.md     ← 历史踩坑记录（随包走，不影响安装）
├── install.py           ← 唯一安装入口（./install.py --help）
├── scripts/             安装辅助脚本
│   └── link-dsh-deps.ps1           Windows 版补依赖链接（第 4 步 ③ 必跑）
├── toolkit/             工具层实体：Python 脚本 + 用例 + 知识卡 + Web UI
├── plugins/             两个 DSH 包
│   ├── android-tools-plugin/       把工具层封装成原生 tool
│   │   ├── link-dsh-deps.mjs       ← 跨平台版补依赖链接（Linux/mac 用这个）
│   │   └── lib/index.js
│   └── android-auto-test-preset/   两个 Agent 角色定义
└── skills/              三个 skill（前两个必装，第三个可选）
    ├── android-gui-test/SKILL.md    执行纪律（DSH 里显示为 android-ai-test-skills）
    ├── android-case-check/SKILL.md  用例够不够格跑（主 Agent 用）
    └── lenovo-tbjira-skills/        内网 JIRA 查单/改单，跟测试机制无关
```

**`link-dsh-deps.ps1` 和 `link-dsh-deps.mjs` 是两个不同的东西**，别当成一个的两种写法：
`.ps1` 在顶层 `scripts/`、Windows 专用、能自动定位 profile 目录；`.mjs` 在**插件包自己
目录里**、跨平台、在哪个插件目录下跑就给那个插件建链接。只有
`android-tools-plugin` 需要补链接（它 import `@deepseek-ai/dsh-tools` +
`schemastery`，而这两个包没发布到 npm，插件 `package.json` 里也**不声明**任何
dependency，`pnpm install` 对它们无事可做）；preset 不 import 任何外部包，不用跑。

**`toolkit/` 是唯一一份代码，`skills/` 只放纪律文档。** 两者不重复：

| | 放什么 | 谁读它 |
|---|---|---|
| `toolkit/` | 全部可执行代码、用例、知识卡 | 机器跑（DSH 插件把 `android_*` 工具指向这里） |
| `skills/<名字>/SKILL.md` | 只此一个文件 | AI 读（DSH 的 skill 机制只认 `<目录>/SKILL.md`） |

> 唯一例外是 `lenovo-tbjira-skills/`：它带 `jira.py` + `lib/` + `tests/` 一整套代码，
> 因为那个 skill 本身要执行脚本。凭据 `jira_config.json` 在 `.gitignore` 里，
> **永不入包**；每台机器自己 `python3 jira.py setup` 生成一次。

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
| 系统 | Windows + PowerShell 是主线；Linux / macOS 已实跑验证（`toolkit/scripts/setup.sh`） |
| 设备 | Android **真机**，已开 USB 调试 |

> ⚠️ **跑测试前请人工确认设备连接**（`adb devices` 要显示 `device`）。
> 工具层不会替你修 adb，连不上就如实报错。

## 备注

两个 `SKILL.md` 是这个机制的**作业纪律**，告诉 AI 怎么用 toolkit 里那套 `android_*` 工具：
`skills/android-gui-test/SKILL.md` 是执行子 Agent 的作业纪律，
`skills/android-case-check/SKILL.md` 是主 Agent 判断用例够不够格跑。
改它们**不用重启 DSH**；改 `plugins/` 下的东西要重启（或热重载）。

`toolkit/` 里的实现细节看 `toolkit/docs/`：环境与工具 / 执行参考 / 知识卡与路径图。
