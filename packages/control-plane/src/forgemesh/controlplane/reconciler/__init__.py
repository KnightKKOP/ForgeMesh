"""ForgeMesh 控制平面 — 协调控制器。"""

from __future__ import annotations

from .core import ReconciliationController
from .dedup import DedupKey, compute_dedup_key

__all__ = ["ReconciliationController", "DedupKey", "compute_dedup_key"]
