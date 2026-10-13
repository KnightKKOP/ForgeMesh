"""控制平面版本号。

不依赖控制平面的其他模块：资源注册表等底层模块引用它时不会形成循环导入。
"""

from __future__ import annotations

#: 控制平面版本（用于探针与事件 producer.version）。
CONTROL_PLANE_VERSION = "0.0.1"

__all__ = ["CONTROL_PLANE_VERSION"]
