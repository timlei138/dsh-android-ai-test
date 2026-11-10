> **【历史文档 · 2026-11-09 那台机器的踩坑记录】**
> 里面的 `/home/xuyl/Share/toolkit` 是**已经退役的旧层路径**；现在的装法看 `安装说明.md`
> （一键：`python3 install.py --to <工作区>/dsh-ai-auto-test --wire`）。保留这份是因为
> 插件挂载、profile link、沙箱策略这些坑的**排查过程**别处没有，换机器时仍可能对得上。

# Linux 安装记录（Ubuntu 24.04）

> 📌 **2026-11-09 晚更新**：本文的三处差异已经吸收进主线——
> `toolkit/scripts/setup.sh`（POSIX 安装脚本，含写权限实测）与
> `toolkit/tools/doctor.py`（装完自检）已入库；`plugins/android-tools-plugin/lib/index.js`
> 的跨平台改造见本文 ②；插件补依赖见 ④（`node link-dsh-deps.mjs`）。
> 新用户照 `安装说明.md` 的「1.5 装在哪个目录」+ `bash scripts/setup.sh` 走即可，
> 不必再读本文手敲。本文保留作差异说明与踩坑记录。

> 本文是 `安装说明.md` 的 Linux 补充。原文档写的是 Windows + PowerShell，在 Linux 上有几处必须调整。
> 安装日期：2026-11-09　机器：legion-ubuntu　设备：HA1TEZVX (TB320FC_PRC)

## 环境

| 项 | 实际 |
|---|---|
| 系统 | Ubuntu, kernel 6.8.0-147 |
| Node.js | v26.13.0 |
| Python | 3.12.3 (`/usr/bin/python3`) |
| DSH | 0.2.1-rc.5（要求 ≥ rc.2 ✅） |
| adb | 1.0.41 / 34.0.4-debian |
| DSH profile | `web`（`DSH_HOME=~/.dsh`） |

## 安装位置

```
/home/xuyl/Share/toolkit          ← target（含 .venv / tools / assets / storage）
/home/xuyl/Share/plugins/         ← 两个插件包（android-tools-plugin / android-auto-test-preset）
/home/xuyl/Share/skills/          ← skill 源目录（android-gui-test / android-case-check）
/home/xuyl/Share/scripts/         ← link-dsh-deps.ps1（Windows 补依赖用）
~/.agents/skills/android-gui-test/SKILL.md      (拷贝)
~/.agents/skills/android-case-check/SKILL.md    (拷贝)
```

> 📌 **2026-11-09 目录名已与 git 对齐**：原来是 `plugin/`、`skill/`、`android-auto-test`，
> 现已改名为 `plugins/`、`skills/`、`android-auto-test-preset`，profile 里两个 `link:` 已重挂。
> 本地路径现在与安装说明.md 完全一致，不再有「文档写 plugins、实际叫 plugin」的分歧。
>
> 包内 `skills/lenovo-tbjira-skills` 不属于这套机制，未安装。
>
> 📌 **2026-11-09 结构更新**：git 上（commit `149147f`）把 `skills/android-gui-test`
> 从「toolkit 的完整副本」改成「只有一份 SKILL.md」——依据是 DSH 的 skill 发现只读
> `<skill目录>/SKILL.md` 一个文件（`dsh-skill-filesystem/lib/index.js:590`），
> 副本是纯死重。本地已同步：`~/.agents/skills/android-gui-test` 从软链 toolkit
> 改成放一份 SKILL.md，同时补回了新版 SKILL.md 里的 `android_vision` 用法一节。

## 与原文档不同的三处

### ① venv 路径

Windows 是 `.venv\Scripts\python.exe`，Linux 是 `.venv/bin/python`。

```bash
cd /home/xuyl/Share/toolkit
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt      # 本机已配清华源
```

### ② 插件代码已改造成跨平台（**这是本次唯一的代码改动**）

`plugins/android-tools-plugin/lib/index.js` 原来硬编码 Windows 路径与引号规则：

```js
const py   = `${target}\\.venv\\Scripts\\python.exe`
const argv = [py, `${target}\\tools\\${script}`, '--stdin-json']
command: toCommandLine(argv)      // cmd.exe 的引号规则
```

在 Linux 上会失败：`sh -c` 收到 `"/path/.venv/bin/python"` 会把它当成**字面量字符串**而非命令，报 `command not found`。

改法（新增两个函数，`runTool` 里改用它们）：

```js
function buildCommandLine(argv)   // win32 → 原 toCommandLine；POSIX → 单引号并把 ' 转义成 '\''
function venvPython(target)       // win32 → .venv\Scripts\python.exe；POSIX → .venv/bin/python
```

**Windows 分支行为完全不变**，只是多了 POSIX 分支。

### ③ 3 处 target 路径

`安装说明.md` 第 5 步的三处，已全部改为 `/home/xuyl/Share/toolkit`：

- `plugins/android-tools-plugin/cordis.patch.yml`
- `plugins/android-auto-test/preset/cordis.patch.yml`（android-tester 段）
- `plugins/android-auto-test/preset/cordis.patch.yml`（android-executor 段）

### ④ 插件必须链上 DSH 内部包（**文档完全没提，不改就起不来**）

装完插件、重启 DSH 后会报：

```
dsh: warning: 1 entry did not activate
android-tools (@local/dsh-android-tools): failed to import:
  Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@deepseek-ai/schemastery'
  imported from /home/xuyl/Share/plugins/android-tools-plugin/lib/index.js
```

**原因**：`lib/index.js` 头两行 import 了 DSH 自己的包

```js
import z from '@deepseek-ai/schemastery'
import { defineTool } from '@deepseek-ai/dsh-tools'
```

但插件 `package.json` 没有（也不该有）`dependencies`——这两个包**没在 npm 上独立发布**
（`pnpm view @deepseek-ai/dsh-tools version` 返回 `0.0.1-rc.1`，本地实际是 `0.2.1-rc.5`），
只能引用 DSH 安装自带的那份。

而 DSH 也不会把它的 `node_modules` 借给第三方插件。`dsh-app-boot` 源码注释写得明确
（`node_modules/@deepseek-ai/dsh-app-boot/lib/index.js` 约 498 行）：

> The runtime resolution supplies packages **carried by the installation and selected bundles**
> to Node's ESM and CommonJS resolvers.

插件目录向上找不到 `/home/xuyl/.nvm/.../dsh/node_modules`，于是 import 失败。
`NODE_PATH` 也没用（ESM 不认，且 dsh-app-boot 禁止在 `.env` 里设它）。

**修法**：跑本包自带的脚本，把用到的包软链进来：

```bash
cd /home/xuyl/Share/plugins/android-tools-plugin
node link-dsh-deps.mjs        # 自动定位 dsh 安装目录，建 node_modules 软链并自检
```

输出应包含：

```
  linked @deepseek-ai/schemastery -> .../dsh/node_modules/@deepseek-ai/schemastery
  linked @deepseek-ai/dsh-tools   -> .../dsh/node_modules/@deepseek-ai/dsh-tools
@deepseek-ai/schemastery ✅
@deepseek-ai/dsh-tools ✅
```

> ⚠️ 软链指向的是**具体版本目录**。`npm i -g @deepseek-ai/dsh@新版本` 之后路径会变，
> **升级 DSH 后重跑一次 `node link-dsh-deps.mjs` 即可**。
>
> ⚠️ 附带说明：`android-auto-test`（preset 包）只有 yml + 测试脚本，不 import DSH 包，**不需要**这一步。

## 与文档一致的步骤

**装插件**（`dsh plugin add` 会自动登记 `dsh.profile.bundles`，不必手工编辑 package.json）：

```bash
dsh plugin --profile web add "link:/home/xuyl/Share/plugins/android-tools-plugin"
dsh plugin --profile web add "link:/home/xuyl/Share/plugins/android-auto-test"
```

**装 skill**：拷到 `~/.agents/skills/`。DSH 会热加载，**不需要重启**。

## 验证结果

```
.venv/bin/python                 ✅
tools/session.py                 ✅
import u2/rapidocr/adbutils      ✅   (uiautomator2 3.7.1, rapidocr 3.10.0, adbutils 2.12.0)
android-gui-test                 ✅   name=android-ai-test-skills
android-case-check               ✅   name=android-case-check
3 处 target                      ✅   全部指向 /home/xuyl/Share/toolkit
profile bundles                  ✅   android-tools + dsh-android-auto-test-preset
adb devices                      ✅   HA1TEZVX device
输入法                           ✅   com.github.uiautomator/.AdbKeyboard
```

**配置装配**（不启动服务即可验证）：

```bash
dsh --profile web --dump-config | grep -A3 "id: android-tools"   # 应有 3 处
dsh --profile web --dump-config | grep "id: preset-android"      # 应有 2 个 preset
```

> ❗ **`--dump-config` 验不出 import 失败**。它只组合配置树，不加载插件模块。
> 我曾据此判断"装好了"，结果重启后插件根本没起来。**必须用下面这条验证。**

**插件能否真正 import**（在任意目录跑，模拟 DSH 的加载路径）：

```bash
cd /tmp && node --input-type=module -e "
await import('/home/xuyl/Share/plugins/android-tools-plugin/lib/index.js')
  .then(m => console.log('IMPORT OK', m.name, m.inject))
"
# 期望：IMPORT OK android-tools [ 'tools', 'shell' ]
```

**整棵插件树有没有条目没激活**（最接近真实启动的检查）：

```bash
dsh --profile web --port 13921 --no-open --host 127.0.0.1 > /tmp/dsh.log 2>&1 &
sleep 40
grep -i "did not activate\|failed to import\|ERR_MODULE_NOT_FOUND" /tmp/dsh.log \
  && echo "❌ 仍有插件未加载" || echo "✅ 全部激活"
grep -o 'token=[A-Za-z0-9_-]*' /tmp/dsh.log   # 拿到 token 后浏览器打开对应端口即可
kill %1
```

启动日志里只要出现 `dsh: warning: N entry did not activate` 就是有插件没起来——
包括 `dsh-app-boot/lib/index.js:4474` 打出的 `activationDiagnostic`。

**端到端冒烟**（真机，已通过）：`session.py start` → `observe.py`（取证落盘）→
`session.py findings-batch` → `session.py finish`，结论 PASS，崩溃事件 0。
证据在 `toolkit/storage/run_history/11e486df-ff78-4b7e-8e7e-ba49d95d5cf1/`。

## 待办

- [x] **同步 git 上游（2026-11-09 15:26）**：按 `share` 分支 `978b903` 对齐本地。
      `toolkit/tools/runtime.py`、`toolkit/tools/env.py`、`toolkit/.gitignore` 已用 git 版覆盖
      （本地旧版备份在 `/tmp/runtime.py.local-bak`、`/tmp/env.py.local-bak`）。
      同步后实测：`storage` 落点仍是 `<target>/storage` 不变，`observe.py` / `read_file.py` 均 `ok:true`。
- [ ] **重启 DSH**：`android_*` 工具是新 bundle，必须重启进程才出现（skill 不受影响，已生效）。
      重启后新建会话，preset 选 **「Android 自动化测试」**。
      若日志出现 `did not activate` → 跑 `node link-dsh-deps.mjs`（见 ④）。
- [ ] 视觉模型（`android_vision`）需要在 WebUI「视觉模型」页配 base_url / api_key / model。
- [ ] `apkutils` 未装（`pip install adbutils[apk]`），只影响 `u2` 的 APK 名解析，不影响测试。
- [ ] 待定：`toolkit/assets/cases/other/` 的 12 个用例（git 有、本地无）是否补回。
- [ ] 待定：插件的跨平台改动（`lib/index.js` 的 `buildCommandLine` / `venvPython`）是否入库。

## 已知问题

**文档里的 PowerShell 写法**：`toolkit/SKILL.md` 与 `toolkit/docs/` 中的命令示例是 PowerShell 风格
（`& "..."` 前缀、`\` 路径分隔符、`-Encoding utf8`）。执行子 Agent 在 Linux 上会照着写错。
好在 `session.py start` 的返回里带 `env` 字段（`platform: linux, shell: bash, path_sep: /`），
`android_env` 也会报，AI 有纠正依据。若反复出问题，再把 `SKILL.md` 改成双平台写法。
