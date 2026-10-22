"""图内视觉通道：让子 Agent 不依赖主模型的多模态能力。

为什么需要它：
  子 Agent 跑图时，"看一眼屏幕判断"这一步不能要求主模型能读图。
  本机已配置 vision.json（OpenAI 兼容），走 vision.py 即可。

⚠️ 实测坑（2026-10-16）：
  max_tokens 给小了（如 500）时，推理模型把额度全花在 reasoning_tokens 上，
  最终 text 返回**空字符串**（ok 仍为 true）—— 看着成功其实什么都没答。
  ⇒ 默认给 2000，并在 text 为空时明确报错，不静默当成功。

⚠️⚠️ 2000 还是不够（2026-10-20，167 真机实测）：
  168 的报错里 usage 写得很清楚 ——
      completion_tokens=2000, reasoning_tokens=2000
  也就是**一个 token 都没留给答案**，全被推理吃了。

  1x1 的测试图只要 265 个 reasoning token，所以"测试连接"是通的；
  但真实截图要看一个 4 项菜单、还要读中文，推理量翻好几倍。
  **小图能通不代表真图能通** —— 这个误判让我多花了一轮才找到原因。

  更严重的是它引出的连锁反应（167）：
      视觉答不上来 → 模型以为"还没看到" → 再问一次 → 又空 → ...
  连续空转 16 轮、白烧 100 多秒，最后靠模型自己猜对一个坐标才走出来。

  ⇒ 默认提到 6000。推理模型在截图这种任务上，2000 是明显偏小的。
     另外：空返回时**必须让上层的动作彻底失败**，见 nodes.vision_node 的说明。

⚠️⚠️⚠️ 6000 也不够（2026-XX-XX，178 真机实测）：
  报错原文：completion_tokens=6000, reasoning_tokens=6000
  —— **又是一个 token 都没剩**。这次不是看菜单，而是判
  「每节课上课时长默认 50 分钟」这类断言：要在一屏里找控件、读值、
  再和预期比一遍，推理链条比"菜单有几项"长得多。

  后果比之前更隐蔽：`_answer` 拿不到正文 -> AssertVerdict 解析失败
  -> 那条断言记成"判定缺失"（BLOCKED），而用例**总结论仍然写 PASS**
  （因为代码判的三条过了）。等于**三条断言根本没验，报告却说全过**。
  这正是用户反映的"看不出怎么判定的"那类问题。

  ⇒ 12000。经验规律：**视觉 + 判断言**的推理量约是"看图找控件"的 2 倍，
     4000 只能看，6000 能看能说一句，12000 才够"看 + 对照预期 + 组织判词"。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# 视觉问答默认额度：够推理 + 出答案。
#
# 2000 → 6000（167 实测 2000 会被 reasoning 吃光，一个 token 都不剩）。
# 这个数不是拍脑袋：168 那次 usage 报的就是 completion=2000 且
# reasoning=2000，说明推理本身能长到上限；给 6000 留出足够余量。
#
# 6000 → 12000（178 实测 6000 同样被吃光，complete=6000/reasoning=6000）。
# 断言比"看图找控件"更吃推理：要读值、对照预期、组织判词。
# ⚠️ 别再往下调。这个值调小不会"省"，只会让断言静默变成"判定缺失"。
DEFAULT_MAX_TOKENS = 12000


class VisionError(RuntimeError):
    pass


def ask_image(python: str, skill_root: str, image: str, prompt: str,
              max_tokens: int = DEFAULT_MAX_TOKENS,
              timeout: int = 180) -> str:
    """问视觉模型。返回文本答案。

    经 run.py 调用（进账本），与其它工具保持同一条纪律。
    """
    cmd = [python, str(Path(skill_root) / "tools" / "run.py"), "vision",
           "--image", image, "--prompt", prompt,
           "--max-tokens", str(max_tokens)]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=skill_root)
    raw = (proc.stdout or "").strip()
    try:
        data: dict[str, Any] = json.loads(raw[raw.find("{"):]) if "{" in raw else {}
    except json.JSONDecodeError as exc:
        raise VisionError(f"vision 输出无法解析：{raw[:200]}") from exc

    if not data.get("ok"):
        raise VisionError(f"vision 失败：{data.get('error') or proc.stderr[:200]}")

    text = (data.get("text") or "").strip()
    if not text:
        # ⚠️ 关键：ok=true 但 text 空 = 额度被 reasoning 吃光，绝不能当成功
        usage = data.get("usage") or {}
        raise VisionError(
            "vision 返回空文本（ok=true 但 text 为空）。"
            f"usage={usage}；通常是 max_tokens 太小被 reasoning 吃光，"
            f"当前 max_tokens={max_tokens}，建议调大。")
    return text


def screenshot_of(obs_nodes_evidence: dict[str, Any]) -> str | None:
    """从 observe/act 的返回里取截图路径。"""
    ev = obs_nodes_evidence.get("evidence") or {}
    return ev.get("png")


if __name__ == "__main__":
    # 自检：对最新截图问一句
    #
    # ⚠️ 这里以前直接用了 `skill_root()` / `runner_python()`，但它们从没被
    #    import 过 —— 这个自检入口一跑就 NameError，等于坏的。
    from glob import glob

    from ..cases.paths import runner_python, skill_root

    root = skill_root()
    py = runner_python()
    shots = sorted(glob(str(Path(root) / "storage" / "evidence" / "**" / "shot.png"),
                        recursive=True))
    if not shots:
        print("没有截图可测")
        sys.exit(1)
    print("用图:", shots[-1])
    print("答案:", ask_image(py, root, shots[-1], "这张截图最上方一行的文字是什么？"))

