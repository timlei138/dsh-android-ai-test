"""vision：可选的视觉模型通道（OpenAI 兼容多模态 API）。

配置在测试台「视觉模型」页维护（storage/vision.json）。
v2 默认由对话 AI 自身判图；本工具用于批量回放等无对话场景的工具级视觉断言。

用法：
  python vision.py ask --image storage/evidence/c1/step02/shot.png \
      --prompt "按钮是置灰的吗？回答 置灰/可用 + 一句依据"
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request
from pathlib import Path

from common import emit, fail
from runtime import STORAGE_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
except AttributeError:
    pass


def main() -> None:
    p = argparse.ArgumentParser(description="vision：视觉模型问图（OpenAI 兼容）")
    p.add_argument("--image", required=True, help="图片路径（png/jpg）")
    p.add_argument("--prompt", required=True, help="问题/断言指令")
    # ⚠️ 默认值原来是 500，小到离谱。
    #
    # 推理模型会把额度先花在 reasoning_tokens 上，剩多少才给答案。
    # 500 的后果是：截图一张都读不透，返回空文本（ok 仍是 true），
    # 上层以为"看过了但没内容"，于是反复重问 —— 167 就这么空转 16 轮。
    #
    # 168 的真实 usage 是 completion=2000 & reasoning=2000（仍全被吃光），
    # 所以这里给 6000。调用方显式传 --max-tokens 时以调用方为准。
    p.add_argument("--max-tokens", type=int, default=6000)
    args = p.parse_args()

    conf_fp = STORAGE_DIR / "vision.json"
    try:
        conf = json.loads(conf_fp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("未配置视觉模型：先在测试台「视觉模型」页填写并保存")
    base = (conf.get("base_url") or "").rstrip("/")
    model = conf.get("model") or ""
    key = conf.get("api_key") or ""
    if not (base and model and key):
        fail("配置不完整（base_url / model / api_key），先在测试台「视觉模型」页补全")

    img = Path(args.image)
    if not img.exists():
        fail(f"图片不存在: {img}")
    mime = "image/jpeg" if img.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    b64 = base64.b64encode(img.read_bytes()).decode()

    body = json.dumps({
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": args.prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": args.max_tokens,
    }).encode()

    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=120))
    except Exception as e:  # noqa: BLE001
        fail(f"视觉模型调用失败: {e}")

    try:
        text = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        fail(f"响应格式异常: {json.dumps(resp, ensure_ascii=False)[:300]}")
    emit(True, {"text": text, "usage": resp.get("usage"),
                "hint": "视觉模型输出是辅助判据，PASS/FAIL 结论仍需结合 dump/OCR 证据"})


if __name__ == "__main__":
    main()
