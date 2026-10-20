# 子 Agent 工具限制：事实与实测结论

## 用户实测结论（最重要）

在 standard preset 下，用 `subagent` 起子 Agent 问"你有哪些工具"，回答是 **28 个全量工具**：

```
ask_user_question, create_goal, edit, exit_plan_mode, get_goal, glob, grep,
interrupt_agent, job_kill, job_list, job_output, list_agents, present, pwsh,
ralph, read, read_image, send_message, skill, subagent, subagent_fork,
todo_write, update_goal, web_fetch, web_search, wechat_notify, workflow, write
```

⇒ **子 Agent 默认继承父方全部工具**，包括 `pwsh`/`write`/`edit`/`workflow`/`ralph`。
这印证了"必须限制"的判断。

⚠️ 但这次**没有验证 toolFilter**：会话里不存在 `probe_child`，说明我建的
`probe` preset **根本没被挂载**，那次跑的是 `standard`。
所以"toolFilter 能不能限制住"仍然只有代码证据，没有实测证据。

## 代码证据（已确认）

### 1. toolFilter 确实会被应用

`dsh-subagent/lib/index.js:554`：

```js
if (composition.toolFilter !== void 0) childCtx.tools.restrict(composition.toolFilter);
```

`tool-subagent` 把它透传进 child 创建 spec（`lib/index.js:518`）：
```js
...config.toolFilter !== void 0 ? { toolFilter: config.toolFilter } : {},
```

### 2. restrict 的作用范围（关键）

`dsh-tools/lib/index.js:2786` 的注释：

> A restriction filters **what a scope inherits** — the global layer and every
> ancestor layer on its chain — and **never what its OWN layer registers**.
> That exemption is what a per-child capability filter has to keep intact.

⇒ 子 Agent 继承到的工具会被过滤；子 Agent 自己那层注册的不受影响。

### 3. 写错工具名会**抛错**（不会静默）

```js
const known = this.view(scope).restrictableNames;
const unknown = [...allow ?? [], ...deny ?? []].filter((n) => !known.has(n));
if (unknown.length > 0) throw new Error(`tools.restrict() names unknown global tool ...`);
```

⇒ 好处：白名单写错名字会立刻炸，不会静默失效。
⇒ 代价：**必须先拿到准确的工具名**，否则子 Agent 创建失败。

### 4. 空 filter 会抛错

```js
if (allow === void 0 && deny === void 0)
  throw new Error("tools.restrict({}) is a no-op: ...");
```

⇒ `toolFilter` 要么给 `allow`、要么给 `deny`、要么整个键不写。

### 5. 不能过滤 `run_code`

```js
if ([...allow ?? [], ...deny ?? []].includes("run_code")) throw ...
```

### 6. ★ 审批策略被钉死为 `never`

`dsh-subagent/lib/index.js:561`：

> ...the approval policy is **pinned to `'never'` regardless of the parent's own policy**.

⇒ 这解释了用户观察到的"两边描述不一致"：
**子 Agent 说"审批已禁用"是对的**，不是它误推。
父方是 `ask`、子方是 `never`，同一个进程里两者就是不同 —— 这是设计如此。

## `skill` 工具能否在白名单里存活？

**未确证。** 但有一条重要线索：

`restrictableNames` 只收集 **inherited** 的名字（`lib/index.js:2864-2866`），
而 `skill` 在 standard preset 里是 `tool-skill` 注册的 —— 属于 inherited 层。
所以它大概率**可以被 allow 保留**。

但反过来说，如果 allow 只写我们自己的工具而漏掉 `skill`，
`skill` 会被过滤掉 → 子 Agent 无法加载技能。

**这仍需要一次真实验证**（起一个真挂载了新 preset 的会话来问）。

## 待办

- [ ] 真实验证：新 preset 挂载后，子 Agent 的工具面是否只剩白名单
- [ ] 真实验证：白名单含 `skill` 时技能是否仍可加载
- [ ] 确认准确的工具名清单（写错会抛错）
