# android_case 参数

## 两种互斥的调用方式

**不要同时传**，会和 YAML 打架。

| 参数 | 说明 |
|---|---|
| `case_name` | 用现成 YAML（推荐）。填文件名片段，如 `"168"` |
| `case_id` / `title` / `package` | 内联模式的基本信息 |
| `steps` / `asserts` / `preconditions` | 内联模式的用例内容 |

## 可选开关

| 参数 | 默认 | 什么时候改 |
|---|---|---|
| `serial` | 自动 | 多台设备在线且要指定某台时 |
| `writeback` | `true` | 想先看会改 YAML 什么、暂不落盘 → `false` |
| `perm_action` | `grant` | 只测"拒绝分支"时 → `deny` |
| `answer` | `model` | **别改**（见下） |
| `knowledge_files` | 空 | 额外知识卡路径（YAML 里也可写） |

## `answer`：别动它

| 值 | 含义 |
|---|---|
| `model`（默认） | 真模型判断。**这是用例过不过的唯一真实答案** |
| `autopilot` | 规则应答器，完全不调模型。只验"YAML 写对没有" |

⚠️ **绝不拿 autopilot 的 PASS 当用例结论。**
两者报告长得一模一样，但 autopilot 根本没接模型。

## 看到「未采纳」警告

YAML 里有些字段执行器还没实现。这些会**明确打印成"未采纳"**，
不会静默丢掉。看到它不代表跑失败，但意味着**那条信息这次没生效** ——
报告时要提一句，否则主 Agent 会以为验过了。
