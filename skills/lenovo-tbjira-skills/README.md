# lenovo-tbjira-skills

Lenovo 内网 JIRA（tbjira.lenovo.com，**支持当前账号有权访问的所有项目**）的 **Agent Skill** 版封装。

默认项目为 AGENTPILOT（Lenovo Tablet 团队），但所有查询/统计/建单命令都可用
`--project <KEY>` 指定任意项目，`projects` 命令可列出全部可访问项目。

原实现是一套 MCP Server（`jira_mcp_server.py`，18 个 tool，常驻进程）。
本目录把它改造成 **CLI + SKILL.md**：

- `jira.py` —— 单入口 CLI，18 个功能收敛成子命令，stdout 只输出 JSON
- `lib/jira_client.py` —— 认证 / 配置 / HTTP / 附件下载的公共层
- `SKILL.md` —— 给 AI Agent 读的作业指导书
- `reference.md` —— JQL、字段对照、状态流、错误码

## 为什么改成 CLI 而不是 MCP

这 18 个功能全是**一次性 REST 请求**，没有任何常驻状态。为无状态的东西常驻一个进程，
要付的代价是：MCP 协议握手、进程生命周期管理、客户端配置、以及"stdout 被日志污染就挂"。
做成 CLI 后：

- 任何能跑 shell 的 Agent 都能用（不限于支持 MCP 的客户端）
- 每次调用独立，没有进程状态可坏
- 缺凭据 / 认证失败不再需要 `sys.exit`，返回结构化错误即可
- 调试就是跑一条命令看 JSON

## 安装

```powershell
python -m pip install -r requirements.txt
```

## 配置（二选一）

**A. 交互式向导**（密码在终端里输，不落盘到命令行历史）

```powershell
python jira.py setup
```

会先生成候选配置、**验证连通后才写盘**。

**B. 环境变量**（CI / Agent 场景，密码完全不落盘）

```powershell
$env:JIRA_ITCODE = "your_itcode"
$env:JIRA_PASSWORD = "your_password"
```

## 自检

```powershell
python jira.py whoami     # 不联网，看配置解析结果
python jira.py ping       # 联网，确认账号和连接
```

## 快速开始

```powershell
python jira.py projects                      # 当前账号有权访问的所有项目
python jira.py project                       # 默认项目详情
python jira.py bugs                          # 默认项目的 bug 分布
python jira.py bugs --project OTHERKEY       # 指定项目
python jira.py mine
python jira.py search "project = OTHERKEY AND status = Open ORDER BY created DESC"
python jira.py text "闪退" --project all     # 跨全部可访问项目搜
python jira.py issue AGENTPILOT-123
python jira.py comments AGENTPILOT-123
python jira.py attach-list AGENTPILOT-123
python jira.py attach-get 12345 --save D:\tmp\
```

写操作：

```powershell
python jira.py comment AGENTPILOT-123 "已定位到根因"
python jira.py transitions AGENTPILOT-123
python jira.py transition AGENTPILOT-123 --name "关闭"
python jira.py create --summary "月视图切换闪退" --type Bug --priority High
python jira.py update AGENTPILOT-123 --priority High
python jira.py delete AGENTPILOT-999 --yes     # 不可恢复
```

## 输出约定

stdout 永远是一个 JSON 对象：

```json
{"ok": true,  "cmd": "search", "data": {...}}
{"ok": false, "cmd": "ping",   "error": "...", "hint": "..."}
```

退出码：`0` 成功 / `1` 错误 / `2` 认证失败 / `3` 用法错误。
**判断结果看 `ok` 字段**，诊断信息一律走 stderr。

加 `--human` 可切到人类可读排版（仅方便手跑，脚本里别用）。

## 测试

```powershell
python tests\test_offline.py        # 离线单测：配置容错、文件名解析、HTML 误判、CLI 错误路径
```

联机冒烟（需要先配置凭据）：

```powershell
python tests\smoke_live.py          # 只跑读操作，不做任何写操作
```

## 与旧 MCP 版共存

`jira_config.json` 的格式完全兼容（`itcode` / `password` / `jira_base` / `project_key`），
**同一个配置文件两边都能读**。老的 Claude Desktop + MCP 配置不用动。

旧版是一个从下载包解压出来的本地 MCP Server 目录，配置格式与它完全兼容（同一个 `jira_config.json` 两边都能读）。
