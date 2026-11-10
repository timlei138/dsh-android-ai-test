#!/usr/bin/env node
/**
 * 把本插件需要的 DSH 内部包链接进自己的 node_modules。
 *
 * ## 为什么需要这个文件（2026-11-09 在 Linux 上实测）
 *
 * 本插件 `import z from '@deepseek-ai/schemastery'` 和
 * `import { defineTool } from '@deepseek-ai/dsh-tools'`，但 package.json 里
 * 没有（也不该有）dependencies —— 这两个包**没在 npm 上独立发布**：
 *
 *     pnpm view @deepseek-ai/dsh-tools version  ->  0.0.1-rc.1   (stale)
 *     pnpm view @deepseek-ai/schemastery version ->  3.19.0
 *     而本地 dsh 安装里是                               0.2.1-rc.5 / 3.18.5
 *
 * 版本对不上，不能交给 pnpm 装。只能引用 DSH 安装自带的副本。
 *
 * 但 DSH 也不会自动把它的 node_modules 借给第三方插件：
 * dsh-app-boot 的注释（lib/index.js 约 498 行）写得很清楚 ——
 * "The runtime resolution supplies packages carried by the installation and
 *  selected bundles to Node's ESM and CommonJS resolvers"。
 * 插件目录 `/home/xuyl/Share/plugin/android-tools-plugin` 向上找，够不到
 * `/home/xuyl/.nvm/.../dsh/node_modules`，于是报：
 *
 *     Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@deepseek-ai/schemastery'
 *     imported from .../android-tools-plugin/lib/index.js
 *
 * 也不用 NODE_PATH：ESM 不认它，且 dsh-app-boot 的 BOOTSTRAP_NAMES 明确
 * 禁止在 .env 里设 NODE_PATH。
 *
 * ## 做法
 *
 * 在本包下建 node_modules，把用到的包逐个 symlink 到 dsh 安装里的副本。
 * **DSH 升级后 `npm i -g @deepseek-ai/dsh` 会换掉路径，重跑这个脚本即可**。
 *
 * ## 用法
 *
 *     node link-dsh-deps.mjs            # 用 which dsh 自动找安装目录
 *     node link-dsh-deps.mjs --dsh /path/to/dsh/node_modules
 */

import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))

/** 本插件真正 import 的包。其余传递依赖从各自包旁边解析即可。 */
const NEEDED = ['@deepseek-ai/schemastery', '@deepseek-ai/dsh-tools']

/** 找到 dsh 安装目录下的 node_modules。 */
function findDshNodeModules(explicit) {
  if (explicit) return path.resolve(explicit)
  // 优先问 dsh 包自己：它 require.resolve 出来的位置最准
  try {
    const bin = execFileSync('which', ['dsh'], { encoding: 'utf8' }).trim()
    const real = fs.realpathSync(bin) // <pkg>/bin/dsh.js
    let dir = path.dirname(real)
    while (dir !== path.dirname(dir)) {
      const pkgJson = path.join(dir, 'package.json')
      if (fs.existsSync(pkgJson)) {
        const pkg = JSON.parse(fs.readFileSync(pkgJson, 'utf8'))
        if (pkg.name === '@deepseek-ai/dsh') return path.join(dir, 'node_modules')
      }
      dir = path.dirname(dir)
    }
  } catch {
    /* fall through */
  }
  // 兜底：相对本包，或全局 node_modules
  const candidates = [
    path.join(here, '..', '..', '..', '..', '.nvm', 'versions', 'node', process.version, 'lib', 'node_modules', '@deepseek-ai', 'dsh', 'node_modules'),
  ]
  const found = candidates.find((c) => fs.existsSync(c))
  if (found) return found
  throw new Error('找不到 dsh 安装目录；用 --dsh <path> 指定它的 node_modules')
}

const dshNM = findDshNodeModules(process.argv.includes('--dsh') ? process.argv[process.argv.indexOf('--dsh') + 1] : undefined)
console.log('dsh node_modules =', dshNM)

const ownNM = path.join(here, 'node_modules')
let linked = 0
let missing = []

for (const name of NEEDED) {
  const src = path.join(dshNM, name)
  if (!fs.existsSync(src)) {
    missing.push(name)
    continue
  }
  const dst = path.join(ownNM, name)
  fs.mkdirSync(path.dirname(dst), { recursive: true })
  try {
    fs.rmSync(dst, { recursive: true, force: true })
  } catch {
    /* 忽略 */
  }
  fs.symlinkSync(fs.realpathSync(src), dst, 'dir')
  console.log('  linked', name, '->', fs.realpathSync(dst))
  linked++
}

if (missing.length) {
  console.error('\n❌ dsh 安装里缺这些包：', missing.join(', '))
  console.error('   可能 dsh 版本不匹配。检查：', dshNM)
  process.exit(1)
}

// 自检：真的能解析吗（这才是 --dump-config 验不出来的那一步）
const require = createRequire(path.join(here, 'package.json'))
const probe = []
for (const name of NEEDED) {
  try {
    require.resolve(name)
    probe.push(`${name} ✅`)
  } catch (e) {
    probe.push(`${name} ❌ ${e.code || e.message}`)
  }
}
console.log('\n' + probe.join('\n'))
console.log(`\n完成：${linked} 个包已链接`)
