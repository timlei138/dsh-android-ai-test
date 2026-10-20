/** 证明自检真的会拦住"漏写 render"。
 *
 * 上一版用 PowerShell 内联 node -e，pathToFileURL 没导入，
 * 结果 SyntaxError 被 catch 当成"拦住了"——假阳性。写成文件重做。
 */
import { pathToFileURL } from "node:url";
import { readFileSync, writeFileSync, unlinkSync } from "node:fs";

const F = "D:/dsh/langgraph-poc/runner-tool/lib/index.js";
const BAK = F + ".selftest.bak";
const orig = readFileSync(F, "utf8");

// 把 render 整块删掉，模拟我最初漏写的样子
const broken = orig.replace(
  /\r?\n {8}render\(_args, value\) \{[\s\S]*?\r?\n {8}\},/,
  "");
if (broken === orig) {
  console.log("  ✖ 替换失败：没匹配到 render 块，测试无效");
  process.exit(2);
}

let pass = 0, fail = 0;

try {
  writeFileSync(BAK, orig);
  writeFileSync(F, broken);

  // 1) 漏写 render -> apply 应当抛错
  try {
    const m = await import(pathToFileURL(F).href + "?v=" + Date.now());
    m.apply({ tools: { register: () => {} } });
    console.log("  [FAIL] 漏写 render 时没拦住 —— 自检失效");
    fail++;
  } catch (e) {
    const msg = String(e?.message ?? e);
    const ok = /output\.render/.test(msg);
    console.log(`  [${ok ? "PASS" : "FAIL"}] 漏写 render 被拦住 -> ${msg.split("\n")[0]}`);
    ok ? pass++ : fail++;
  }
} finally {
  writeFileSync(F, orig);
  try { unlinkSync(BAK); } catch {}
}

// 2) 还原后应当正常注册
try {
  const m = await import(pathToFileURL(F).href + "?v=" + Date.now() + "b");
  let got = null;
  m.apply({ tools: { register: (t) => { got = t; } } });
  // ★ 契约是 ContentBlock[]，不是字符串
  const out = got?.output.render({}, { status: "PASS", engine: "model",
                                       report: "x" });
  const ok = got?.name === "android_case" && Array.isArray(out) &&
    out.some((b) => b.type === "text" && b.text === "x");
  console.log(`  [${ok ? "PASS" : "FAIL"}] 补上 render 后正常注册且能渲染（数组）`);
  ok ? pass++ : fail++;
} catch (e) {
  console.log(`  [FAIL] 还原后仍失败 -> ${e?.message}`);
  fail++;
}

// 3) ★ 自检必须拦住"render 返回字符串"这种违反契约的写法
//
//    这正是 content.some is not a function 的根因：以前 render 返回
//    `value.report`（字符串），而自检断言的就是这个字符串 ——
//    把 bug 钉成了"正确"。现在自检必须拦住它。
try {
  const bad = orig.replace(
    /\r?\n {8}render\(_args, value\) \{[\s\S]*?\r?\n {8}\},/,
    "\n        render(_args, value) {\n          return value.report;\n        },");
  if (bad === orig) {
    console.log("  [FAIL] 无法构造字符串版 render");
    fail++;
  } else {
    writeFileSync(F, bad);
    try {
      const m = await import(pathToFileURL(F).href + "?v=" + Date.now() + "c");
      m.apply({ tools: { register: () => {} } });
      console.log("  [FAIL] render 返回字符串时没拦住 —— 自检失效");
      fail++;
    } catch (e) {
      const msg = String(e?.message ?? e);
      const ok = /ContentBlock/.test(msg);
      console.log(`  [${ok ? "PASS" : "FAIL"}] 字符串版 render 被拦住 -> ${msg.split("\n")[0]}`);
      ok ? pass++ : fail++;
    } finally {
      writeFileSync(F, orig);
    }
  }
} catch (e) {
  console.log(`  [FAIL] 字符串版 render 测试异常 -> ${e?.message}`);
  fail++;
  writeFileSync(F, orig);
}

console.log(`\n  ${fail ? fail + " 项失败" : "全部通过"}（${pass + fail} 项）`);
process.exit(fail ? 1 : 0);
