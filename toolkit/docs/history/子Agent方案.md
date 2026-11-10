# 历史：DSH 子 Agent 方案（2026-10-15 的可行性结论）

> 归档保留。当时的结论已经落地（现在的 android-executor preset 就是按这个做的）。新问题不要从这里找答案。

# DSH 子 Agent 执行器：可行性结论

> 调查日期：2026-10-15
> 目的：确认能否在 DSH 层给「用例执行子 Agent」配置专属人格 + 工具收窄。
> 结论：**可行**。`spawn` provider 宣称全部 5 项启动期能力，`persona` 与 `toolFilter` 都支持。

---

## 一、被否决的方案：「全局 persona」

**不可行 —— 会直接导致 DSH 启动失败。**

`@deepseek-ai/dsh-persona` README 原文：

> Mount it inside a preset composition — **mounting it globally collides with the
> prompt registry's own persona registration and fails loud.**
>
> **No global mount** — the prompt registry owns the unscoped persona slot, so this
> row is usable only from a scoped composition.

原因：进程内只有**一个**全局人格槽位（`deployment:persona-prefix`），由
`dsh-system-prompt` 无条件注册。挂 `dsh-persona` 到全局 = 撞注册 = fail loud。

若要改全局人格，正确位置是 `dsh-system-prompt` 那行的自己的 config
（README：「The deployment-wide persona itself is configured on the
`dsh-system-prompt` row, not here」）—— 但那会让**所有** Agent（含主 Agent）
都变成「Android 用例执行器」，不可接受。

---

## 二、可行方案：新增一个平行的 tool-subagent 实例

### 依据 1：spawn provider 宣称全部 5 项能力

`@deepseek-ai/dsh-subagent-spawn-in-process` README：

> **The backend advertises all five start-time capabilities, including
> `agentOptions`, because it controls the child's creation window and can enforce
> each one.**

⇒ `persona` / `toolFilter` / `depthLimit` / `agentOptions` / `prepareContinuable`
全部可用。

### 依据 2：persona 是 per-child，正好是想要的粒度

`dsh-tool-subagent` 的 `persona` 字段 JSDoc（`lib/types/index.d.ts` L46-50）：

> Per-child persona that **shadows `deployment:persona-prefix`**. Requires the
> provider's `persona` capability; **omission preserves the deployment persona**.

⇒ 只有走这个工具的 child 带人格，其他 Agent 不受影响。

### 依据 3：toolFilter 的确切 schema

zod schema（`lib/index.js` L265-268）：

```js
toolFilter: z.object({
    allow: z.array(z.string()).default(void 0),
    deny:  z.array(z.string()).default(void 0)
}).default(void 0)
```

JSDoc（`index.d.ts` L51-61）：

> Tool filter applied to every child. **Filtered tools disappear from its prompt
> and reject execution.** Requires the provider's `toolFilter` capability;
> **unknown names fail startup.**
>
> `allow`：Global tool names the child keeps; everything else is removed.
> `deny` ：Global tool names removed from the child.

⚠️ 注意 `lib/index.js` L370 的校验：

```js
if (config.toolFilter !== void 0 && config.toolFilter.allow === void 0
    && config.toolFilter.deny === void 0)
    throw new Error("tool-subagent: `toolFilter` ...")
```

⇒ 配了 `toolFilter` 就必须至少给 `allow` 或 `deny` 之一，否则 mount 失败。

### 依据 4：配置绑在「工具」上，不绑在「单次调用」上

`dsh-tool-subagent` 已知限制：

> **Non-routing child policy is fixed per instance** — another persona, tool
> filter, or depth cap **requires another distinctly named tool**.

⇒ 想要两副面孔（通用 subagent + 用例执行器），**必须两个工具**。

---

## 三、建议的配置片段

写进 `C:\Users\<你>\.dsh\profiles\web\cordis.patch.yml`
（该文件当前是空的 `[]`，属"从零新增"而非"改别人的"）。

```yaml
# 用例执行器：带人格 + 工具收窄的子 Agent
- id: tool-subagent-case-runner
  name: '@deepseek-ai/dsh-tool-subagent'
  config:
    provider: spawn
    toolName: case_runner
    backgroundMode: continuable
    persona: |-
      你是 Android 用例执行器，一次只跑一个 case。

      硬性纪律：
      1. 只走 run.py，禁止裸 adb
      2. 每个预期结果记一条 finding，禁止合并
      3. PASS finding 必须带 --event-id
      4. 多个 run.py 调用塞进单个 .bat，一次 cmd /c 执行
      5. 用例没要求就不清理

      返回格式：状态 + 每条预期 + event id
    toolFilter:
      allow:
        - pwsh
        - read
        - write
        - edit
        - glob
        - grep
        - skill
        - todo_write
```

⚠️ **`toolFilter.allow` 里的名字必须是"全局工具名"，写错会导致 mount 失败**
（"unknown names fail startup"）。上面的名单**未经核验**，落地前必须
逐一对照实际注册的工具名。

---

## 四、待办 / 风险

1. **工具名核验**：`allow` 列表里的每个名字都要确认是已注册的全局工具名。
   写错 → mount 失败 → **整个 DSH 起不来**。
2. **`cordis.patch.yml` 是全局 profile 配置**，改错影响整个 DSH 启动，
   比改 `run.py` 危险得多。建议改之前先备份。
3. **persona 解决不了「6 条预期验完没有」** —— persona 是**声明**，不是
   **执行时机**，和 SKILL 的失效方式相同。仍需 `run.py` 侧的
   `session progress` + `finish` 覆盖闸做硬校验。
4. **`includeRuntimeContext` 与 persona 无关**：那是 `dsh-persona` 包的字段，
   不是 `tool-subagent` 的 `persona` 字段，别混淆。

---

## 五、两者分工

| 层 | 干什么 | 谁能改 |
|---|---|---|
| DSH persona + toolFilter | 子 Agent **天生**带纪律、工具**物理**收窄 | 全局 profile 配置（危险） |
| run.py progress + finish 闸门 | 6 条不齐**不许收尾** | 业务层（安全） |

**两件都要**：persona 管「它想不想守规矩」，闸门管「守没守有没有人查」。