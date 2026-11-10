---
name: lenovo-tbjira-skills
description: 查询和操作 Lenovo 内网 JIRA（tbjira.lenovo.com）。当用户要找 bug、查任务进度、看某个 issue 的详情/评论/附件/工时、按关键词搜问题、看自己名下待办、创建或评论 issue、改状态、传下载附件、做 bug 状态统计时使用。
---

# Lenovo JIRA（tbjira.lenovo.com）操作手册

> 你是**替用户动 JIRA 的手**，不是替用户拍板的人。
> 读操作随便做；**写操作（创建/评论/改状态/删除/上传）改的是真实系统，会被同事看到** —— 先看清、再动手。

## 一、开工第一件事：`ping`

```powershell
python <skill目录>/jira.py ping
```

**不 ping 就别往下调。** 它一次告诉你三件事：凭据有没有、网络通不通、你是哪个账号。

- `ok: true` → 继续干活。`data.itcode` 就是"我"是谁（`mine` 用它算 `currentUser()`）。
- `ok: false` + `"未配置 JIRA 凭据"` → **停下来告诉用户要配置**，别自己瞎试：
  - 让用户跑 `python <skill目录>/jira.py setup`（交互式，自己输密码）
  - 或者用户在 shell 里设 `JIRA_ITCODE` / `JIRA_PASSWORD` 环境变量
- `ok: false` + `401` → **密码错或过期。重试没有意义**，让用户重新 `setup --force`。
- `ok: false` + 连不上 → 让用户确认 VPN / 内网。

> 排查"为什么读不到配置"用 `whoami`（不联网，只看本地解析结果，含配置文件路径）。

## 二、命令总表

路径统一写成 `jira.py`，实际是 `<skill目录>/jira.py`。

### 读

| 命令 | 用途 | 典型用法 |
|---|---|---|
| `ping` | 连接/账号检查 | **每次开工先跑** |
| `whoami` | 本地配置解析（不联网） | 排查凭据读不到 |
| `projects` | 当前账号有权访问的所有项目 | 不知道项目 KEY 时跑这个 |
| `project [KEY]` | 单项目信息 | `project ABC`（缺省 = 默认项目） |
| `search "<JQL>"` | JQL 搜索 | `search "project = ABC AND status = Open ORDER BY created DESC"` |
| `text "<关键词>" [--project KEY\|all]` | 标题/描述全文搜 | `text "crash"` |
| `bugs [--project KEY]` | Bug 状态分布 | `bugs --limit 500` |
| `mine [--project KEY]` | 我名下未解决 | `mine` |
| `issue <KEY>` | 详情（含描述） | `issue ABC-123` |
| `comments <KEY> [--limit N]` | 评论（默认最新 20 条） | `comments ABC-123` |
| `worklogs <KEY>` | 工时日志 + 合计小时 | `worklogs ABC-123` |
| `transitions <KEY>` | 当前能改成哪些状态 | 改状态前必查 |
| `attach-list <KEY>` | 附件列表（拿 attach id） | 下载前必查 |
| `attach-get <ID> [--save PATH]` | 下载附件 | `attach-get 12345 --save D:\tmp\` |

### 写（谨慎）

| 命令 | 用途 | 注意 |
|---|---|---|
| `create --summary "..."` | 建 issue | 默认 `--type Bug --priority Medium`；类型先 `project <KEY>` 看 `issue_types` |
| `comment <KEY> "<正文>"` | 加评论 | 正文里带换行用双引号包住即可；中文没问题（UTF-8 已处理） |
| `transition <KEY> --id <id>` | 改状态 | **先 `transitions` 拿 id**；`--name "<转换名>"` 也行 |
| `update <KEY> [--summary/--description/--priority/--assignee/--labels]` | 改字段 | `--labels` 是**整体替换**不是追加 |
| `attach-put <KEY> <FILE>` | 上传附件 | 文件必须本地存在 |
| `delete <KEY> --yes` | 删除 | ⚠️ 不可恢复，**必须用户明确说要删**才执行 |

## 三、输出怎么读（最重要的一节）

**stdout 永远是一个 JSON 对象**，成功失败都是：

```json
{ "ok": true,  "cmd": "search", "data": { ... } }
{ "ok": false, "cmd": "ping",   "error": "未配置 JIRA 凭据", "hint": "..." }
```

判断成败**看 `ok` 字段**，不要只看退出码（退出码 0=成功，1=错误，2=认证失败）。

几条硬规矩：

1. **`ok:false` 时读 `hint`** —— 里面通常直接告诉你怎么修，包括"重新 setup"这类动作。
2. **不要因为 `ok:false` 就编造内容**。查不到就说查不到，把 `error` 原文带给用户。
3. **`truncated: true`** 表示结果被截断（`total` > `returned`），回答"有多少个 bug"要说明是**全量还是只看了前 N 条**。
4. **`bugs` 的 `distribution_scope`** 会明说统计范围：`全量` 或 `仅统计已返回的 N 条`。默认 limit 500，超过时记得加 `--limit`。
5. `issue`/`comments` 里的长文本**已被截断到 2000 字符**并带 `...`。要全文就在回答里说明"评论较长已截断"。

### 直接给用户看的时候

默认 JSON 对人不好读。给用户看之前**自己把 JSON 转成人话**（表格/列表）。
需要你亲手看结果时想省点眼睛，可以加 `--human` 走人类排版 —— 但**解析和判断永远用 JSON**。

## 四、常见任务怎么走

### 「现在有多少 bug，各状态多少」

```powershell
python jira.py bugs --limit 500
```

- 回 `total_bugs` + `status_distribution`
- 如果 `distribution_scope` 不是「全量」，要么告知用户，要么加 `--limit 2000` 重跑

### 「查一下 xxx 的问题」

```powershell
python jira.py text "闪退"
```

搜不到再上 `search` 写 JQL（写法见 `reference.md`）。

### 「看 ABC-123 的详情 / 评论 / 附件」

```powershell
python jira.py issue ABC-123          # 描述、状态、负责人、优先级
python jira.py comments ABC-123       # 最新 20 条
python jira.py attach-list ABC-123    # 附件 + attachment id
```

**要下载附件**：`attach-list` 拿 `attachments[].id` → `attach-get <id> --save <目录>`。
不要拿 issue key 当 attachment id。

### 「把 ABC-123 关掉」

```powershell
python jira.py transitions ABC-123    # ① 拿当前可用的转换
python jira.py transition ABC-123 --name "关闭"   # ② 用名字，或 --id
```

- **转换 id 只对当前状态有效**，状态一变就要重新查
- 名字重名时工具会拒并要求用 `--id`
- 命令会**回读当前状态**（`current_status`）确认真的变了 —— 把结果如实转述

### 「给 xxx 加个评论」

```powershell
python jira.py comment ABC-123 "已定位到根因，正在修复"
```

评论正文是**你自己写的内容**，写到真实系统里了。**用户没让你写评论，就别写。**
内容较长或含特殊字符时，建议先把正文存成 UTF-8 文件再用 shell 读进去，避免转义踩坑。

### 「建一个 bug」

```powershell
python jira.py create --summary "日历月视图切换时闪退" --type Bug --priority High --description "复现步骤：..."
```

- `--type` 不在目标项目里存在时 JIRA 会 400，用 `project <KEY>` 看该项目的 `issue_types`
- 建完把 `data.key` 和 `data.url` 给用户（url 是可直接点的 browse 链接）

## 五、坑（都是实测踩出来的，别重蹈）

### 1. 附件下载：**只能**走 `/secure/attachment/{id}/`

REST API 的 `attachment.content` 地址实测返回的是**以 200 状态码包装的 HTML 错误页**（几百字节），
文件打不开还以为是下载成功。CLI 已经固定走正确路径，并在落地前校验
Content-Type + 内容魔数，发现 HTML 直接报错不落盘。

> 所以：**`attach-get` 报"返回的是 HTML 页面"时，是 attachment id 错了**，不是网络问题。回 `attach-list` 重拿 id。

### 2. 中文文件名与中文内容：编码

CLI 已强制 stdout/stderr 为 UTF-8。你自己在 PowerShell 里拼命令时注意：

- 给 `comment` / `create --description` 传长中文，**优先写成 UTF-8 文件再读**，避免 PowerShell 编码转换出错
- 附件名走 `filename*=UTF-8''` 解析，中文文件名能正常还原

### 3. JQL 里的引号

`search` 的 JQL 用双引号包起来，里面的文本搜索也需要双引号。**嵌套引号**最容易出错：

```powershell
# ❌ 会被 shell 吃
python jira.py search "summary ~ "闪退""

# ✅ 用 text 子命令（内部已经处理好转义）
python jira.py text "闪退"
```

### 4. 不要自动重试写操作

`comment` / `transition` / `update` / `create` / `delete` / `attach-put` **失败后不要重试**——
可能已经写进去了（超时≠没生效）。先 `issue` 或 `comments` 读一遍确认，再决定。

读操作（`search`/`issue`/`bugs`/`mine` 等）失败重试一次没问题。

### 5. 401 不是"再试一次"能解决的

密码过期是**唯一**的 401 原因，重试只会继续失败。直接让用户 `setup --force` 重配。

### 6. `mine` 用的是 `currentUser()`

它算的是**配置里那个 ITCode**，不是"用户口头说的我"。如果用户问的是别人名下的待办，
用 `search "project = <KEY> AND assignee = <itcode> AND resolution = Unresolved"`。

## 六、别做的事

| ❌ 不要 | 为什么 |
|---|---|
| 用户没让你写就 `comment` / `create` / `transition` | 改的是真实系统，同事会看到 |
| 自己 `delete --yes` | 不可恢复；**只有用户明确说"删掉 XXX-xxx"才做**，做完把 key 报出来 |
| 拿 `attach-list` 的 key 去 `attach-get` | `attach-get` 要的是 **attachment id**，不是 issue key |
| `ok:false` 时凭印象编结果 | 查不到就说查不到 |
| 反复 ping 换客户端排查 | `ping` 一次就够，网络问题找用户 |
| 改 `jira_config.json` 里的密码 | 让用户自己 `setup` |

## 七、环境与依赖

```powershell
python -m pip install -r <skill目录>/requirements.txt
```

只需要 `requests`（`urllib3` 随它一起装）。

配置读取优先级：**环境变量 > `jira_config.json`**。
配置文件默认在 skill 目录下（`<skill目录>/jira_config.json`），
也可以用 `JIRA_CONFIG` 指向别处。

详细参考（JQL 写法、字段对照、状态流、错误码）见同目录 `reference.md`。
