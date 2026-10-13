"""控制平面暴露的指标（``GET /metrics``，Prometheus 文本格式 0.0.4）。

全部指标在这里定义，标签只取有限的值，不含租户、名称、命名空间、uid、地址等租户数据：

* ``forgemesh_http_requests_total{method,route,status}``：HTTP 请求数。``route`` 是路由
  模板（如 ``/api/v1/inference-services/{name}``），没有匹配任何路由的请求记为
  ``unmatched``；``method`` 不是常见方法时记为 ``OTHER``。
* ``forgemesh_http_request_duration_seconds{method,route}``：处理 HTTP 请求的耗时。

``/metrics`` 与探针一样不要求认证，只输出聚合数据；它不在 ``/api/v1`` 之下，网关不会
转发它（见 README「可观测性」）。
"""

from __future__ import annotations

from .metrics import MetricsRegistry

#: 记作 ``method`` 标签的 HTTP 方法，其余方法记为 :data:`OTHER_METHOD`。
HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
OTHER_METHOD = "OTHER"
#: 没有匹配任何路由的请求的 ``route`` 标签。
UNMATCHED_ROUTE = "unmatched"


class HTTPMetrics:
    """HTTP 请求的计数与耗时。"""

    def __init__(self, registry: MetricsRegistry) -> None:
        self._requests = registry.counter(
            "forgemesh_http_requests_total",
            "HTTP requests handled by the control plane, by method, route template and status.",
            ("method", "route", "status"),
        )
        self._duration = registry.histogram(
            "forgemesh_http_request_duration_seconds",
            "Time spent handling HTTP requests, by method and route template.",
            ("method", "route"),
        )

    def observe(self, method: str, route: str | None, status: int, seconds: float) -> None:
        """记录一个请求；``route`` 为路由模板，没有匹配路由时为 ``None``。"""
        method = method if method in HTTP_METHODS else OTHER_METHOD
        route = route or UNMATCHED_ROUTE
        self._requests.inc(method, route, str(status))
        self._duration.observe(seconds, method, route)


class ControlPlaneMetrics:
    """控制平面的指标注册中心及各组件的指标。"""

    def __init__(self) -> None:
        self.registry = MetricsRegistry()
        self.http = HTTPMetrics(self.registry)


__all__ = [
    "HTTP_METHODS",
    "OTHER_METHOD",
    "UNMATCHED_ROUTE",
    "ControlPlaneMetrics",
    "HTTPMetrics",
]
