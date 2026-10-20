"""read：OCR 读屏。读 UI 树里没有的东西：toast、Canvas 自绘控件文字。

用法：
  python read.py --image storage/evidence/case172/step03/shot.png
  python read.py --image <png> --region 100,200,900,400      # x1,y1,x2,y2
  python read.py --name case172/step03 --region ...          # 不传 --image 则现场截一张
"""
from __future__ import annotations
from runtime import execution

import argparse
from pathlib import Path

from common import (connect, emit, evidence_paths, fail, guard_terminated,
                    log_auto, rel)
import time
from datetime import datetime
from runtime import stage, timed_call


def main() -> None:
    p = argparse.ArgumentParser(description="read：OCR 读取屏幕/截图文字")
    # 只挂 read 真正用得到的参数：read 不产出 nodes，
    # 故 --full/--no-filter/--settle/--perm-action/--perm 都不该出现（会误导）。
    p.add_argument("--serial", default=None, help="多设备时指定 adb serial")
    p.add_argument("--dir", default=None, help="证据根目录（默认 storage/evidence）")
    p.add_argument("--name", default=None, help="证据名，如 case172/step03")
    p.add_argument("--image", default=None, help="已有截图路径；缺省则现场截图")
    p.add_argument("--region", default=None, help="x1,y1,x2,y2（截图坐标系）")
    args = p.parse_args()
    execution("not_executed")

    # 现场截图会被拦截；读"已有图片"（如崩溃前落盘的证据）放行——
    # 那是复核证据，不是对已终止会话继续操作。
    if not args.image:
        guard_terminated("read")
        try:
            d = connect(args.serial)
        except Exception as e:  # noqa: BLE001
            fail(f"设备连接失败: {e}")

    t0 = time.perf_counter()
    started_at = datetime.now().isoformat(timespec="milliseconds")

    try:
        from rapidocr import RapidOCR
    except ImportError:
        fail("缺依赖：pip install rapidocr（运行 setup_env.py 可自动装）")

    if args.image:
        img_path = Path(args.image)
        if not img_path.exists():
            fail(f"截图不存在: {img_path}")
        evidence = rel(img_path)
    else:
        execution("may_have_executed")
        png, _, meta = evidence_paths(args.dir, args.name)
        png.parent.mkdir(parents=True, exist_ok=True)
        image = timed_call("screenshot", d.screenshot)
        timed_call("png_save", image.save, str(png))
        img_path = png
        evidence = rel(png)

    ocr = timed_call("ocr_initialize", RapidOCR)
    execution("may_have_executed")
    out = timed_call("ocr_inference", ocr, str(img_path))
    execution("executed")
    lines = []
    from PIL import Image
    with stage("ocr_image_info"):
        with Image.open(img_path) as source:
            img_w, img_h = source.size
    conversion_started = time.perf_counter()
    # ⚠️ rapidocr 3.x 返回的是 RapidOCROutput 对象（.boxes/.txts/.scores 三个平行数组），
    #    不是旧版 rapidocr_onnxruntime 的 (result_list, elapse) 元组。
    #    这里把两种形态都吃下去 —— 环境可能还没重装，别让工具层因为版本差异直接崩。
    #
    # ⚠️ .boxes 是 numpy 数组，**不能用 `or []`** 兜底：
    #    数组的真值是多元素歧义的，会抛 ValueError（踩过）。
    #    用 `is None` 显式判断。
    if hasattr(out, "boxes"):
        triples = zip(out.boxes if out.boxes is not None else [],
                      out.txts if out.txts is not None else [],
                      out.scores if out.scores is not None else [])
    elif isinstance(out, tuple) and len(out) == 2:
        triples = out[0] or []          # 旧版：(结果列表, 耗时)
    else:
        triples = out or []
    for box, text, score in triples:
        xs = [pt[0] for pt in box]
        ys = [pt[1] for pt in box]
        cx, cy = int(sum(xs) / 4), int(sum(ys) / 4)
        lines.append({
            "text": text,
            "score": round(float(score), 3),
            "center": [cx, cy],
            "norm": [round(cx / img_w, 4), round(cy / img_h, 4)],  # 屏宽高百分比，跨设备可移植
            "bounds": [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))],
        })

    if args.region:
        x1, y1, x2, y2 = map(int, args.region.split(","))
        lines = [l for l in lines
                 if x1 <= l["center"][0] <= x2 and y1 <= l["center"][1] <= y2]

    from runtime import STAGES
    STAGES["ocr_result_conversion"] = (time.perf_counter() - conversion_started) * 1000
    duration_ms = round((time.perf_counter() - t0) * 1000)
    log_auto("read", "read",
             f"OCR {len(lines)} 行" + (f" @ {args.region}" if args.region else ""),
             {"lines": [l["text"] for l in lines],
              "duration_scope": "core OCR including initialization/acquisition/conversion; excludes connection, event write and response",
              "ocr_library_preprocessing_ms": None}, evidence, duration_ms, started_at)
    emit(True, {"image": str(img_path), "lines": lines,
                "hint": "center 可直接用于 act.py tap；toast 断言请在 act 后尽快 read（浮层会消失）"})


if __name__ == "__main__":
    main()
