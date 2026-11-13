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
from datetime import datetime
from pathlib import Path

from common import apply_stdin_args, emit, fail, log_auto, rel
from runtime import STORAGE_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
except AttributeError:
    pass


def main() -> None:
    p = argparse.ArgumentParser(description="vision：视觉模型问图（OpenAI 兼容）")
    p.add_argument("--image", required=True, help="图片路径（png/jpg）")
    p.add_argument("--prompt", required=True, help="问题/断言指令")
    # 默认 2000：这类模型的 reasoning 开销很大，max_tokens 给小了会出现
    # completion_tokens 全被 reasoning 吃光、正文是空字符串的情况 ——
    # 看起来"调用成功"，实际什么都没答，比报错更坑。
    p.add_argument("--max-tokens", type=int, default=2000)
    apply_stdin_args()   # 支持 --stdin-json（插件统一走 stdin 传参）
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

    # ★ 空回答必须当成失败，不能报 ok:true。
    #
    #   实测（2026-10-28）：max_tokens 太小时 reasoning 吃光预算，
    #   返回 text="" 且 ok:true。AI 看到 ok 就以为"模型说没有"，
    #   把"模型没答"误当成"画面里没有"—— 这是会直接导致误判的。
    usage = resp.get("usage") or {}
    if not (text or "").strip():
        reasoning = (usage.get("completion_tokens_details") or {}).get(
            "reasoning_tokens")
        fail(
            "视觉模型返回了空回答"
            + (f"（{reasoning} token 全花在 reasoning 上）" if reasoning else "")
            + "。加大 --max-tokens 重试（如 3000），"
              "或把问题问得更短更具体。**不要把空回答当成「画面里没有」**")

    # ★ 必须落一条事件，否则这条视觉断言**绑不到证据**。
    #
    #   实测（2026-11-13）：findings-batch 要求 PASS/FAIL 至少带一个 event_id，
    #   而 vision 原来一个事件都不记 → 拿 vision 做完断言只能引用别的一步
    #   （图上到底问了什么、答了什么，事后查不到），或者干脆判 BLOCKED。
    #
    #   证据就用传进来的那张图（不重拍）：视觉断言的对象本来就是那张图。
    #
    # ⚠️ log_auto 在**没有活动会话**时返回 None（静默不记），所以下面要判空：
    #    无条件把 event_id 塞进 payload 的话，调用方看到的是 `event_id: null`
    #    —— 分不清"记了但没 id"还是"根本没记"，绑证据时就会拿个空值上去。
    _eid = log_auto(
        "vision", "vision ask",
        f"问图：{args.prompt[:40]}",
        {"image": str(img), "prompt": args.prompt,
         "answer": text, "usage": usage,
         "model": model, "max_tokens": args.max_tokens},
        rel(img), None,
        datetime.now().isoformat(timespec="milliseconds"))

    payload = {"text": text, "usage": usage,
               "hint": "视觉模型输出是辅助判据，PASS/FAIL 结论仍需结合 dump/OCR 证据"}
    if _eid:
        payload["event_id"] = _eid
        payload["evidence"] = rel(img)
    else:
        # 没会话就没账 —— 必须说出来，不能让调用方以为已经绑好了证据
        payload["recorded"] = False
        payload["record_hint"] = ("本次调用**没有活动会话**，事件未落库；"
                                  "结论要能复核，请先 session.py start 再问图")
    emit(True, payload)


if __name__ == "__main__":
    main()
