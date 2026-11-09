# Android 自动化测试 preset（2026-10-27）

> **为什么重建**：旧方案（`D:\dsh\android-runner-preset`）给子 Agent 只挂
> `android_case` 一个工具，然后用 18851 行 Python 补回被剥夺的能力。
> 实测两周后结论：**那 18851 行大部分在防 AI 出错，而 AI 本来不会犯那些错。**
>
> 新方案：**放开工具的权限，把纪律写进 SKILL。**
>
> | | 旧 | 新 |
> |---|---|---|
> | 子 Agent 的工具面 | `android_case` 一个 | **bash + 读文件 + skill** |
> | 纪律靠什么 | 代码里的 7 个 ``保护 N`` | **SKILL.md** |
> | 每步谁决定 | 代码路由 | **AI 自己** |
> | 代码量 | 18851 行 | 6114 行（工具层）|

---

## 两个 preset

### `android-tester`（主 Agent，order 50）

职责只有四件，**不碰设备**：

1. case 相关问题回答（查库，不 glob 文件）
2. 用户聊天
3. 状态恢复（子 Agent PAUSED 时把问题转给用户）
4. **质量检查**（派发前用 check skill 判断用例够不够格）

工具面：`subagent` + 只读文件 + skill。**没有 bash。**

### `android-executor`（执行子 Agent，order 51）

**这是和旧方案最大的区别**：它不是一个"只能调一个工具"的壳，
而是一个**有完整动手能力的执行者**：

| 工具 | 用来干什么 |
|---|---|
| **bash** | 跑 `python tools/act.py` / `observe.py` / `session.py` |
| read / glob / grep | 读 SKILL.md、读 dump.xml 找控件坐标 |
| read_image | 看图（视觉判定） |
| skill | 按 SKILL.md 的工作流执行 |
| ~~write / edit~~ | **关掉** —— 不许改工具层代码 |

**为什么给 bash**：实测两次跑下来，AI 被卡住的地方全是
"工具没给这个原语"，而它的绕道方案都需要 shell：

```
卡点1  点不到 clickable=false 的 checkbox
       -> 它 grep dump.xml 拿 bounds 手算坐标 (176,391)
       如果没 bash，这一步做不到，只能报"工具不支持"

卡点3  判断"屏幕有没有加号"（视觉通道 401）
       -> 它用 Pillow 逐格统计像素，20 格非背景像素=0
       如果没 bash，它只能猜
```

**为什么仍然关 write/edit**：用户明确要求过
「怎么还有自己改代码的情况。这个不允许。」
shell 里跑 python **不等于**能改源码 —— 后者会让它在跑不通时去改工具层，
掩盖真问题。

---

## 和旧方案的能力对照

| | 旧 `android-worker` | 新 `android-executor` |
|---|---|---|
| bash | ❌ | ✅ |
| grep dump.xml | ❌ | ✅ |
| read_image | ✅（但模型不支持图片输入） | ✅ |
| python | ❌ | ✅ |
| 改源码 | ❌ | ❌ |
| 派发 | ❌ | ❌ |

---

## 部署

```powershell
# 1. 同步到 DSH 真实加载位置
Copy-Item D:\dsh\android-auto-test\preset\cordis.patch.yml `
          D:\dsh\android-auto-test-preset\cordis.patch.yml

# 2. 在 profile 的 package.json 里挂上这个 bundle
#    D:\Users\leichao4\.dsh\profiles\web\package.json
#    "dsh": { "profile": { "bundles": [ ..., "@local/dsh-android-auto-test-preset" ] } }

# 3. 重启 DSH（preset 是启动时加载的）
```

---

## 待办（按优先级）

| # | 事项 | 来源 |
|---|---|---|
| 1 | **视觉通道**：`vision.py` 返回 401，缺 API key | 176 实测 |
| 2 | **`tap --rid` 原语**：现在点 `clickable=false` 的控件要手算坐标 | 168/176 两次都提 |
| 3 | **SKILL 加"原地打转"约束**：477 在 EditCourseActivity 里点了 11 次循环 | 本轮实测 |
| 4 | 减 `timing`/`sig` 体积：占工具返回 53%，AI 用不上 | token 分析 |

---

## 相关文件

- 工具层与 SKILL：`D:\Project\deepseek\android-ai-test-skills`
- 旧方案（保留参考）：`D:\dsh\langgraph-poc`、`D:\dsh\android-runner-preset`
- 用例原文：`D:\dsh\langgraph-poc\assets\cases\联想日历\_cases.json`
- 知识卡与路径图：`D:\dsh\langgraph-poc\assets\knowledge\`
