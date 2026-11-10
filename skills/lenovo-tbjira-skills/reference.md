# Lenovo JIRA 参考手册

这份是 `SKILL.md` 的**查阅型**补充。日常照着 SKILL.md 做；JQL 写不出来、状态名对不上、
错误码看不懂的时候再翻这里。

---

## 一、JQL 速查（JIRA Server，非 Cloud）

### 基本形状

JIRA 是多项目的：`<项目KEY>` 用 `projects` 命令查（issue key 的前缀就是项目 KEY），
不写 project 条件时会搜当前账号有权看到的所有项目。

```
project = <项目KEY> AND <条件> [AND <条件>...] [ORDER BY <字段> ASC|DESC]
```

`AND` / `OR` 运算符要**大写**。字段名大小写不敏感，但值大小写敏感。

### 常用条件

| 意图 | JQL 片段 |
|---|---|
| 未解决 | `resolution = Unresolved` |
| 已解决 | `resolution != Unresolved` |
| 未关闭 | `status != Closed` |
| 我名下 | `assignee = currentUser()` |
| 某人名下 | `assignee = someone01` |
| 我报的 | `reporter = currentUser()` |
| 最近 7 天创建 | `created >= -7d` |
| 最近 7 天更新 | `updated >= -7d` |
| 优先级高及以上 | `priority in (Highest, High)` |
| 是 Bug | `issuetype = Bug` |
| 某类型 | `issuetype in (Bug, Task)` |
| 标题含词 | `summary ~ "闪退"` |
| 标题或描述含词 | `(summary ~ "闪退" OR description ~ "闪退")` |
| 没有负责人 | `assignee is EMPTY` |
| 有附件 | `attachments is not EMPTY` |
| 带某个 label | `labels in (crash)` |
| 精确标题 | `summary = "月视图切换时闪退"` |

`~` 是**文本模糊匹配**（对中文分词效果一般，搜不到时换更短的关键词或改用 `text` 命令）。

### 排序

```
ORDER BY created DESC     最新在前
ORDER BY updated DESC     最近动过的在前
ORDER BY priority DESC    优先级降序（按 priority 的排序权重）
ORDER BY key ASC          翻页稳定
```

> **翻页稳定**很重要：`search` 支持 `--start N`，当 JQL 没有 `ORDER BY` 时
> JIRA 的默认排序在不同页之间可能重复或漏条。要翻页就显式 `ORDER BY key ASC`。

### 时间

```
-7d  -24h  -1w（部分实例不支持 w）  2026-07-01
created >= "2026-07-01" AND created < "2026-08-01"
```

### 常见报错对照

| JIRA 返回 | 原因 | 怎么办 |
|---|---|---|
| `Field 'xxx' does not exist or you do not have permission to view it` | 字段名写错 / 无权看该字段 | 换字段名；用 `project` 看可用信息 |
| `Unable to parse the query` | 语法错（最常见是多写了 `AND`、括号不配对、值没用引号） | 逐段缩短 JQL 试 |
| `Operator '~' is not supported` | 某些自定义字段不支持文本匹配 | 换 `=` 或 `in` |
| `Cannot find a value with id 'xxx'` | 状态/优先级值名写错 | `transitions` 或 `project` 里看真实名字 |

---

## 二、字段对照（CLI JSON 键 → JIRA 字段）

`jira.py` 把嵌套字段拍平了，对照如下：

| JSON 键 | JIRA `fields.` 路径 | 说明 |
|---|---|---|
| `summary` | `summary` | 标题 |
| `status` | `status.name` | 状态名 |
| `type` | `issuetype.name` | Bug / Task / Story / Sub-task |
| `priority` | `priority.name` | Highest/High/Medium/Low/Lowest |
| `assignee` | `assignee.displayName` | 负责人显示名（`null` = 未分配） |
| `reporter` | `reporter.displayName` | 报告人 |
| `created` / `updated` | 同名 | ISO8601 带时区，如 `2026-07-28T16:09:34.000+0800` |
| `labels` | `labels[]` | 标签数组 |
| `description` | `description` | 只在 `issue` 命令 / `--fields` 点名时输出 |
| `attachment_count` | `attachment[]` 长度 | 附件个数 |
| `comment_count` | `comment.total` | 评论总数 |

节点里带 `null` 的写法是"JIRA 返回 `null`"，不是"字段不存在"。

---

## 三、状态与转换

**各项目的实际状态名以 `transitions` 命令输出为准**，本文只给通用形态：

```
Open ──开始处理──▶ In Progress ──解决──▶ Resolved ──关闭──▶ Closed
  │                     │                  │
  └──拒绝──▶ Rejected    └──重新打开──◀─────┘
```

规则：

1. **转换 id 是随状态变化的实例级数据**，同一个 issue 换状态后 id 可能全变。
   → **每次改状态前先 `transitions`**
2. `transitions` 输出里 `name` 是**动作名**（如"关闭"），`to_status` 是**目标状态名**（如 `Closed`）。
   `transition --name` 两种都认，动作名优先。
3. 转换被工作流校验拦下时，JIRA 会返回 400 + `errorMessages`，CLI 会原样带出来。
   常见拦截原因：必填字段没填、没有权限执行该转换、该状态不允许从这里跳过去。

---

## 四、错误码 → 处置

CLI 失败时 `ok:false`，`error` 带状态码，可能的字段：

| HTTP | `error` 里的字样 | 含义 | 你该做什么 |
|---|---|---|---|
| 401 | `认证失败（401）` | ITCode/密码错或密码过期 | **不重试**，让用户 `setup --force` |
| 403 | `无权限（403）` | 有账号但没这个权限 | 报给用户，别绕过 |
| 404 | `资源不存在（404）` | key 写错 / 项目不对 | 核对 key；用 `mine`/`search` 找正确 key |
| 400 | `请求参数被 JIRA 拒绝（400）` | 字段值非法（类型不存在、必填缺失） | 看 `hint` 里的 `errorMessages`，改参数 |
| 5xx | `JIRA 服务端错误（5xx）` | 服务端问题 | 读操作可重试一次；写操作**先读回确认**再决定 |
| — | `返回的是 HTML 页面而非附件内容` | attachment id 错 / 被重定向登录页 | 回 `attach-list` 重拿 id |
| — | `连不上 JIRA` | 网络 / VPN | 找用户，别反复试 |
| — | `未配置 JIRA 凭据` | 没配置 | 找用户 `setup` 或设环境变量 |

退出码：`0` 成功 / `1` 一般错误 / `2` 认证失败（`auth_error: true`）/ `3` 用法错误。

---

## 五、配置与凭据

### 文件格式（`jira_config.json`）

```json
{
  "itcode": "<你的工号>",
  "password": "明文密码",
  "jira_base": "https://tbjira.lenovo.com/issue/rest/api/2",
  "project_key": "默认项目 KEY（如 AGENTPILOT，仅作缺省值，不是唯一项目）"
}
```

- 兼容原 MCP 版的配置格式，**同一个文件两边都能读**
- 可选 `issue_base`（默认由 `jira_base` 去掉 `/rest/api/2` 推出来），用于非 REST 的 `/secure/attachment/` 路径

### 读取优先级

```
环境变量 JIRA_ITCODE / JIRA_PASSWORD / JIRA_BASE / JIRA_PROJECT_KEY / JIRA_CONFIG
        ↓（没有就下一级）
配置文件（JIRA_CONFIG 指定 → skill 目录 → 当前工作目录）
        ↓
默认值（tbjira.lenovo.com + 默认项目 AGENTPILOT）
```

环境变量优先，所以 CI 里可以不落盘密码。

### 配置文件查找顺序（找不到就说"未配置"）

1. `$env:JIRA_CONFIG` 指定的路径
2. `<skill 目录>\jira_config.json` —— **推荐**，跟 skill 一起走，换工作目录不丢
3. `<当前工作目录>\jira_config.json` —— 兼容原版（原版是相对路径读写）

### 容错

**空文件 / 非法 JSON / 顶层不是对象 / 字段缺失，一律当"未配置"，不崩溃。**
（原 MCP 版碰到一个 0 字节的 `jira_config.json` 会直接 `JSONDecodeError`）

### 安全

- 配置文件里是**明文密码**，权限已收紧到 `0600`（仅在 POSIX 生效，Windows 靠 ACL）
- 想避免落盘：只用环境变量
- 传输是 `verify=False`（内网自签名证书），已屏蔽警告

---

## 六、与原 MCP 版的功能对照

原 `jira_mcp_server.py` 的 18 个工具，映射关系：

| 原 MCP 工具 | 本 CLI |
|---|---|
| `search_issues` | `search` |
| `get_issue` | `issue` |
| `create_issue` | `create` |
| `update_issue` | `update` |
| `delete_issue` | `delete` |
| `add_comment` | `comment` |
| `get_comments` | `comments` |
| `get_transitions` | `transitions` |
| `transition_issue` | `transition` |
| `get_issue_attachments` | `attach-list` |
| `upload_attachment` | `attach-put` |
| `download_attachment` | `attach-get` |
| `get_worklogs` | `worklogs` |
| `list_bugs_by_status` | `bugs` |
| `get_my_open_issues` | `mine` |
| `search_by_text` | `text` |
| `get_project_info` | `project` |
| `check_connection` | `ping` |

### 相对原版的修复

| 原版问题 | 本版处理 |
|---|---|
| 0 字节 / 非法 JSON 配置 → 进程崩溃 | 容错，当"未配置"处理 |
| 缺凭据时 `print` + `sys.exit(1)`，MCP 场景污染 stdout | 返回结构化错误，进程不死 |
| 先存配置再测连接，密码错了留下坏配置 | **先验通再落盘** |
| 下载附件只检测 `<!DOCTYPE` / `<html` 前缀 | 再加 Content-Type 判断 + 二进制魔数判断，且**不落盘** |
| 评论正文硬截断 200 字符 | 放宽到 2000 并标注截断 |
| `get_my_open_issues` 用 `status != Closed`（漏掉已解决未关闭的） | 改 `resolution = Unresolved` |
| `list_bugs_by_status` 只统计返回的 1000 条还不说明 | 输出 `distribution_scope` 明说统计范围 |
| 工时日志没有汇总 | 输出 `total_hours` |
| 二进制文件用 `resp.content` 全量入内存 | 分块拼接（仍是全量，但不再重复拷贝） |
