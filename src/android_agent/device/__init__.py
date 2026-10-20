"""设备层：与 Android 真机/工具层打交道。

**这一层是唯一允许调用 adb 的地方。** 上层（agent/judge）只认这里暴露的
数据形状，不关心底下是 adb shell 还是别的手段。

`Device` 是协议（便于用 MockDevice 做无真机测试），
`RealDevice` 是真实现。别在别处直接 subprocess 调 adb —— 绕过这一层
就失去了会话锁、事件记录和崩溃检测。
"""

from __future__ import annotations

from .bridge import (ActResult, Device, MockDevice, ObserveResult,  # noqa: F401
                     RealDevice, ToolError)

__all__ = [
    "ActResult", "Device", "MockDevice", "ObserveResult",
    "RealDevice", "ToolError",
]
