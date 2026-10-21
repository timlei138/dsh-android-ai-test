# 已知问题：Harness 的 `output.render` 是隐式必填

**状态**：已定位，**故意不修**（原因见下）
**影响**：任何只给 `output.schema`、不给 `output.render` 的 DSH 插件
**发现**：跑 Android 用例 168 时，子 Agent 报 `render` 相关错误，设备一步没动

---

## 现象

工具注册**成功**，`execute` 也**真的跑完了**并返回了结构化结果，
但框架在**渲染层**炸掉，调用方只看到一个跟用例毫无关系的 TypeError。

最坑的一点：**失败发生在执行之后**。所以看起来像"用例有问题"，
实际上是「跑完了、结果丢了」。

## 根因

在 `@deepseek-ai/dsh-tools/lib/index.js`：

```js
L840   const userRender = options.output.render;          // 用户没给 → undefined
L855   render(args, value) {
L856     return userRender(args, value);                  // ← 必炸
L857   },
L858   ...userPresentationMeta !== void 0 ? {            // ← 下一行就判了空
L859     presentationMeta(args, value) { ... }
L860   } : {}
```

而注册校验写着：

```js
L2776  throw new TypeError(`tool "${name}" must declare output { schema, render, presentationMeta? }`)
```

**`render` 是必填，`presentationMeta` 才是可选。**
所以 `defineTool` 无条件包装 `render` 本身符合契约 ——
**错在包装函数没兜住"用户没给"**：它造出一个"注册能过、一调就炸"的工具，
反过来绕过了自己的校验。

💡 **`L858` 是最直接的证据**：同一个对象字面量里，作者给 `presentationMeta`
判了空，却没给 `render` 判。这不是设计取舍，是疏忽。

同一份代码在 `lib/types/schema.js` 有第二份拷贝（L281/L303），**同样的 bug**。

## 实测复现

```
--- 1) 只给 schema，不给 render（复现原 bug）---
        抛错原文: userRender is not a function
  [PASS] 原 bug 已复现
--- 2) 我们的 android_case 插件 ---
  [PASS] render 不抛错 -> "用例结论摘要"
```

见 `test_render_live.mjs`（用**真实** dsh-tools 跑，不是模拟）。

## 我们的绕法

插件侧自己给 `render`，并且在**加载时自检**：

```js
// runner-tool/lib/index.js，apply() 里
// ★ 断言的是契约（ContentBlock[]），不是"返回字符串"——
//   早期写成 probe !== "__selfcheck__"，把错的形态钉死成了"正确"，
//   结果普通会话正常、测试模式（PTC）一跑就 content.some is not a function。
const probe = tool.output.render({}, { status: "PASS", report: "__selfcheck__" });
if (!Array.isArray(probe) || !probe.some(b => b?.type === "text")) throw ...
```

**为什么是"试打"而不是"检查存在"**：包装函数**永远存在**，
只检查 `typeof === 'function'` 永远为真，拦不住任何东西。
必须真调一次才暴露。

实测效果：

```
[PASS] 漏写 render 被拦住 -> android_case: output.render 自检失败 -> userRender is not a function
```

见 `test_render_selfcheck.mjs`。

## 为什么不修 harness

1. **`dsh-tools` 只发布 `lib/`，没有 `src`，没有 sourcemap**
   改的是**编译产物**，不是源码 —— 下次升级无声蒸发，且无从追溯。
2. **这个 DSH 是 fork 的，后面要 merge 上游**
   改 `lib/` 容易在 merge 时冲突或被覆盖。
3. **上游可能已经修了** —— merge 完顺手核一下即可。
4. **插件侧已经兜住** —— 现有唯一的自建插件不会再踩。

### 什么时候该重新考虑

- 写了**第二个**自建插件时（新插件未必照抄自检）
- 要把这个改动**回馈上游**时（那才是正确出口：改源码、提 issue/PR）
- merge 完上游后，确认此 bug 仍在

## 正确的修法（留给将来的自己）

在 `defineTool` 里二选一，**关键是要在挂载时就失败**：

```js
// 方案 A：挂了就抛（推荐 —— 契约本来就要求 render）
if (typeof options.output.render !== "function") {
  throw new TypeError(`defineTool(${options.name}): output.render is required`);
}

// 方案 B：给默认实现（宽松，但至少不炸）
render: options.output.render ?? ((_args, value) => JSON.stringify(value, null, 2)),
```

**不要**保持现状。"注册成功、首次调用才炸"是最坏的一种失败方式。
