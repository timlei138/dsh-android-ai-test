# 冷启动：起点固定

**规则：除非用例明确要求清数据，否则一律冷启动复位。**

YAML 的步骤是按「从 App 首页出发」写的。如果起点取决于"上次停在哪个界面"，
步骤顺序就没法直接用，只能靠人去迁就设备现在在哪。冷启动把这个从根上消掉。

> ⚠️ **只 `force-stop`（关进程），不清数据。**
> `pm clear` 是**用例明确要求**时才做的事（「首次使用」「测首次授权」）。
> 拿 `pm clear` 当默认复位＝拿大锤砸一切，会清掉本用例要用的前置数据。

这里踩过**三个真机 bug**（都已修，回归测试见 `test_coldstart.py` / `test_wake_guard.py`）：

**bug1：`adbutils.app_stop/app_start` 静默失效。**
实测 `app_stop('com.zui.calendar')` 后 `pidof` 仍返回原 pid，
`app_start()` 返回 `None` 且进程没被拉起 —— **但代码不报错**，
还照样打印"已冷启动复位"。**这是最坏的一类 bug：骗过了日志。**
⇒ 改用 `am force-stop` + `monkey`，并**回读 `mCurrentFocus` 校验**。

**bug2：杀掉 App 后，它拉起的系统浮层还留在前台。**
实测：日历点「从图库导入」调起 PhotoPicker
（`PhotoPickerGetContentActivity`，包名 `com.android.providers.media.module`），
此时杀掉日历 —— **这个框不属于日历进程**，`force-stop` 日历动不了它，它一直在前台挡着。
⇒ 冷启动必须**先清掉这类跨包浮层，再拉起被测 App**。

**bug3：屏幕睡着时，一切点击都静默失效。**
实测：跑 168 得到 `18 轮 / steps 1/4 / BLOCKED`，模型**连续 3 次点 `(0,0)`**。
日志里全是模型的错，但真相是设备休眠了：`mWakefulness=Dozing`、
`mCurrentFocus=NotificationShade`（锁屏遮挡）。屏幕上根本没有可点控件。

> ⚠️ **又是一类"骗过了日志"的故障** —— 症状看起来像"模型不会用"，其实是"设备没醒"。

⇒ 冷启动**第一步**先 `KEYCODE_WAKEUP` + `wm dismiss-keyguard`，
再 force-stop / 拉起 App。**只在需要时发**，不要每次强行点亮用户的屏幕。

#### ★ 资产必须自包含

**设备依赖和数据都在 `assets/` 里，不在任何 `.agents` 技能目录里。**

2026-10 删掉旧技能目录时，结果不是"少了个技能"，而是
**设备依赖（adbutils/opencv/rapidocr）跟着没了** —— 它原本只存在于被删的目录。
于是 `runner_python()` 掉到兜底、`run.py` 报「缺依赖：先建环境」，
整条执行链断掉（`status=BLOCKED steps=0`）。

> ⚠️ 这是**静默崩坏**：没有 ImportError 指向真因，只有一句"缺依赖"。

现状（回归测试 `test_assets_selfcontained.py` 盯着）：

```
D:\dsh\langgraph-poc\.venv    → 唯一的运行环境：编排(langgraph/langchain) + 设备(adbutils/opencv/rapidocr)
D:\dsh\langgraph-poc\requirements.txt   → 能重建
```

**只有一个 venv。** 编排和设备依赖装在同一个环境里 —— 以前分开过两个，
后来 `rapidocr` 支持了 Python 3.13 就合并了。所以：

- `assets/.venv` **不该存在**（存在说明又分家了，测试会失败）
- 别再按"编排 venv / 设备 venv"去理解报错 —— 缺依赖就是这一个环境没装好

#### 报 `content.some is not a function` = 工具的 bug，不是你的问题

只在测试模式炸。看到就把原始报错转给用户，**不要**自己分析、不要重试。

#### ★ 看到 `WinError 267` / `skill_root` 才读这节

> **判据：报错原文里没出现 `267` 或 `skill_root`，就不要往这想。**
>
> 曾经有会话其实失败在**工具 schema 校验**（`steps_done=0`，压根没进 execute），
> 但它读到了这节，就断定"是 skill_root 的已知故障"，
> 进而虚构出一条报错和一整套根因分析 —— **设备一次没碰，报告看起来很专业**。

真遇到时：`skill_root` 指向了**不存在的目录**。
注意**空串表现完全不同**——`Path("")` 就是 `Path(".")`，不报 267，表现为「找不到 `run.py`」。

**别手写资产路径**，一律用统一解析点：
`from android_agent.cases.paths import skill_root, runner_python`。
（`assets_path` 是已经不存在的老模块名，别照它写 import。）
