"""图内视觉通道：让子 Agent 不依赖主模型的多模态能力。

为什么需要它：
  子 Agent 跑图时，"看一眼屏幕判断"这一步不能要求主模型能读图。
  本机已配置 vision.json（OpenAI 兼容），走 vision.py 即可。

⚠️ 实测坑（2026-10-16）：
  max_tokens 给小了（如 500）时，推理模型把额度全花在 reasoning_tokens 上，
  最终 text 返回**空字符串**（ok 仍为 true）—— 看着成功其实什么都没答。
  ⇒ 默认给 2000，并在 text 为空时明确报错，不静默当成功。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# 视觉问答默认额度：够推理 + 出答案。低于 1000 极易空返回。
DEFAULT_MAX_TOKENS = 2000


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

