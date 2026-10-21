# android_case 完整参数表

### 完整参数表

| 参数 | 说明 |
|---|---|
| `case_name` | 用现成 YAML（推荐）。**与下面四个互斥** |
| `case_id` / `title` / `package` | 内联模式的基本信息 |
| `steps` / `asserts` / `preconditions` | 内联模式的用例内容 |
| `knowledge_files` | 额外知识卡（YAML 里也可写） |
| `serial` | 指定设备；不填则用唯一在线设备 |
| `perm_action` | `grant`（默认）/ `deny`，只测"拒绝分支"才填 deny |
| `writeback` | 是否回写 YAML，默认 `true` |
| `answer` | `model`（默认）/ `autopilot`，见下 |

不需要"纯代码模式"这类开关 —— **每一步都先由代码在当前屏找唯一命中**，
找到就直点、找不到才叫模型。报告里的 `auto_rate` 就是自动化率。

#### `answer` —— 谁来做判断（默认 `model`）

```
android_case(case_name="168")                        # 默认就是真模型
android_case(case_name="168", answer="autopilot")    # 只自测 YAML 时
```

| 值 | 含义 |
|---|---|
| `model`（**默认**） | 真模型：`decide`/`assert` 交给 LLM。这是"用例过不过"的唯一真实答案 |
| `autopilot` | 规则应答器：按 `target`/`landing` 走，**完全不调用 LLM**。只验「YAML 写对没有」，不产生用例结论 |

**默认是 `model`，跑用例不用传。** 理由：忘了传 → 真模型（正确结果）；
而不是忘了传 → 假成绩（危险）。`autopilot` 改成"要显式声明才用"。

> ⚠️ **绝对不要拿 autopilot 的结果当用例结论。**
> 两者的报告长得一模一样（都有轮次/`decide`/PASS），但 autopilot 根本没接模型。

> ⚠️ YAML 里有些字段执行器还没实现。这些会**明确打印成 "未采纳"** 警告，
> 不会静默丢掉。看到警告不等于失败，但意味着那条信息这次没生效。
