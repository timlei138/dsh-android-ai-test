# toolFilter 验证记录

**结论：`toolFilter.allow` 确实能把子 Agent 的工具面收窄，17 项实测全过。**
验证脚本：`test_toolfilter.mjs`（可重复跑，`node test_toolfilter.mjs`）。

## 方法

不靠读代码推断，而是复刻 `dsh-tools` 的 `view()` / `restrict()` 算法，
用**真实**的 `ScopedLayers` + `createScope`（来自 `@deepseek-ai/dsh-scope`）
搭出 `host 层 → preset 层（父方工具）→ 子 Agent 自己层` 的链，再施加过滤比对。

## 实测数据

过滤前，子 Agent 继承到 **18 个**工具 —— 其中这些是能绕开 LangGraph 骨架的危险工具：

```
pwsh  write  edit  workflow  ralph  subagent  subagent_fork
```

施加 `toolFilter.allow: [android_case, skill, read, glob, grep]` 后，剩下 **6 个**：

```
android_case  skill  read  glob  grep  __structured_output__
```

## 三个关键问答

**Q1 allow 能不能收窄工具面？** 能。上面 7 个危险工具全部消失。

**Q2 allow 里写 `skill` 合法吗？** 合法 —— 我原先的担心是错的。
`restrictableNames` 包含**所有祖先层**的工具，preset 平面注册的工具（如 `skill`）
都在其中。`restrict()` 只对**真正的未知名字**抛错。

**Q3 子 Agent 自己那层会不会被误杀？** 不会，而且是**故意**的。
`view()` 的 docstring 写得很明白（`dsh-tools/lib/index.js:2840-2850`）：
豁免"自己那层"是必要的，因为委派运行时把结构化输出工具注册在那里，
过滤掉它，子 Agent 就没有答复通道了。

## 踩过的坑（都在 `test_toolfilter.mjs` 里留了注释）

1. **`restrict()` 对未知名字抛错** —— 所以 `allow` 里的名字必须是子 Agent
   真正继承得到的。写错会在挂载时**响亮地失败**，而不是悄悄放行一切。
   这是好事：配错了不会静默降级成"没有限制"。

2. **`chainLayers` 用的父键不是 `createScope` 传入的 key**，
   而是 `bindScopeParent` 自己派生的对象。必须用 `scopeParentOf(childKey)` 取回来，
   否则父层根本不在链上 —— 表现为"子 Agent 一个父方工具都继承不到"，
   很容易误判成 `toolFilter` 不生效。

3. **scope key 必须是对象**（内部用 WeakMap 存父子关系），字符串会直接抛
   `Invalid value used as weak map key`。

## 对 preset 的意义

`C:\Users\leichao4\.dsh\.agent-presets\android-runner\agent.cordis.yml` 里的
`toolFilter.allow` 是**可信的**：子 Agent 拿不到 shell、写不了文件、派不了孙 Agent。
`maxDepth: 0` 再堵住"派一个不过滤的中间人"这条绕路。
