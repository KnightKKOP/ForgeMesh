"""CLI 测试的替身：本机上的假 API 服务器、资源构造函数与虚拟时钟。"""

from __future__ import annotations

import copy
import json
import socket
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

SERVICES = "/api/v1/inference-services"
TENANT = "acme"
#: :class:`Reply` 的 ``status`` 为它时，服务器不响应、直接关闭连接。
DROP = 0


def unused_url() -> str:
    """本机上没有服务监听的地址：连接被拒绝。"""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def document(
    name: str = "llama", namespace: str | None = "default", **metadata: Any
) -> dict[str, Any]:
    """``apply`` 的资源文档；``namespace`` 为 ``None`` 时不带命名空间。"""
    meta: dict[str, Any] = {"name": name, **metadata}
    if namespace is not None:
        meta["namespace"] = namespace
    return {
        "apiVersion": "forgemesh.io/v1alpha1",
        "kind": "InferenceService",
        "metadata": meta,
        "spec": {
            "model": {"reference": "models/llama"},
            "runtime": {"provider": "vllm", "version": "0.6.3"},
            "resources": {"accelerator": {"type": "nvidia-gpu", "count": 1, "minMemoryGiB": 16}},
        },
    }


def resource(
    doc: dict[str, Any],
    *,
    phase: str = "Pending",
    ready: tuple[str, str] | None = None,
    message: str = "",
    generation: int = 1,
    observed: int = 0,
    uid: str = "uid-1",
    version: str = "0",
    tenant: str = TENANT,
) -> dict[str, Any]:
    """服务端保存的资源：``doc`` 加上服务端维护的元数据与 ``status``。

    Args:
        ready: ``Ready`` 条件的 ``(status, reason)``；为 ``None`` 时没有条件。
        observed: ``observedGeneration``（``metadata`` 与 ``status`` 中都写入）。
    """
    metadata = doc["metadata"]
    conditions = []
    if ready is not None:
        conditions.append(
            {
                "type": "Ready",
                "status": ready[0],
                "reason": ready[1],
                "message": message,
                "lastTransitionTime": "2026-01-01T00:00:00Z",
            }
        )
    return {
        "apiVersion": doc.get("apiVersion", "forgemesh.io/v1alpha1"),
        "kind": "InferenceService",
        "metadata": {
            **metadata,
            "uid": uid,
            "tenantId": tenant,
            "resourceVersion": version,
            "generation": generation,
            "observedGeneration": observed,
            "labels": metadata.get("labels") or {},
            "annotations": metadata.get("annotations") or {},
        },
        "spec": copy.deepcopy(doc["spec"]),
        "status": {
            "phase": phase,
            "observedGeneration": observed,
            "conditions": conditions,
            "endpoints": [],
        },
    }


def error_body(
    code: str,
    message: str,
    correlation_id: str = "cid-server",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """控制平面与网关的错误信封。"""
    error: dict[str, Any] = {"code": code, "message": message, "correlationId": correlation_id}
    if details is not None:
        error["details"] = details
    return {"error": error}


@dataclass(frozen=True)
class Call:
    """服务器收到的一个请求；请求头名称为小写。"""

    method: str
    target: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: Any


@dataclass(frozen=True)
class Reply:
    """一个响应：``body`` 编码为 JSON，为 ``None`` 时发送纯文本 ``raw``。"""

    status: int
    body: Any = None
    raw: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    """一次 ``forgemesh`` 调用的退出码与输出。"""

    code: int
    out: str
    err: str


class FakeApi:
    """``/api/v1/inference-services`` 的假实现，规则与控制平面相同：

    * ``POST`` 创建（``201``），同名资源已存在时 ``409``；
    * 单个资源的 ``GET`` / ``PUT`` / ``DELETE``，不存在时 ``404``；``PUT`` 依次检查
      ``Terminating``、``uid`` 与 ``resourceVersion``（``409`` 带 ``details.reason``），内容
      不变时不递增版本，``spec`` 变化时递增 ``generation``；
    * ``DELETE`` 按 :attr:`delete_status` 直接删除（``204``）或进入 ``Terminating``（``202``）；
    * 列表按 ``(namespace, name)`` 排序、按 ``limit`` 分页，``nextPageToken`` 是下一页的起点。

    :meth:`reply` 预设的响应按顺序优先于这些规则。
    """

    def __init__(self) -> None:
        self.services: dict[tuple[str | None, str], dict[str, Any]] = {}
        self.calls: list[Call] = []
        self.delete_status = 204
        self._replies: list[tuple[str, str, Reply]] = []
        self._created = 0
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(self))
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        # shutdown() 要等 serve_forever 的下一次轮询，默认的 0.5 秒会拖慢每个测试
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def reply(
        self,
        method: str,
        path: str,
        status: int,
        body: Any = None,
        *,
        raw: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        """预设 ``method path``（不含查询串）的下一个响应。"""
        with self._lock:
            self._replies.append((method, path, Reply(status, body, raw, headers or {})))

    def add(self, doc: dict[str, Any], **fields: Any) -> dict[str, Any]:
        """放入一个资源（参数见 :func:`resource`），返回它。"""
        service = resource(doc, **fields)
        metadata = service["metadata"]
        with self._lock:
            self.services[(metadata.get("namespace"), metadata["name"])] = service
        return service

    def log(self) -> list[str]:
        """收到的请求，形如 ``"GET /api/v1/inference-services?limit=100"``。"""
        with self._lock:
            return [f"{call.method} {call.target}" for call in self.calls]

    def handle(self, call: Call) -> Reply:
        with self._lock:
            self.calls.append(call)
            for index, (method, path, reply) in enumerate(self._replies):
                if (method, path) == (call.method, call.path):
                    del self._replies[index]
                    return reply
            return self._default(call)

    def _default(self, call: Call) -> Reply:
        if call.path == SERVICES and call.method == "GET":
            return self._list(call)
        if call.path == SERVICES and call.method == "POST":
            return self._create(call)
        prefix = SERVICES + "/"
        if call.path.startswith(prefix) and call.method in ("GET", "PUT", "DELETE"):
            name = unquote(call.path.removeprefix(prefix))
            key = (call.query.get("namespace"), name)
            current = self.services.get(key)
            if current is None:
                return self._error(call, 404, "NOT_FOUND", f"InferenceService '{name}' not found")
            if call.method == "GET":
                return Reply(200, current)
            if call.method == "PUT":
                return self._replace(call, key, current)
            return self._delete(key, current)
        return self._error(call, 404, "ROUTE_NOT_FOUND", "route not found")

    def _create(self, call: Call) -> Reply:
        metadata = call.body["metadata"]
        key = (metadata.get("namespace"), metadata["name"])
        if key in self.services:
            message = f"InferenceService '{metadata['name']}' already exists"
            return self._error(call, 409, "CONFLICT", message)
        self._created += 1
        service = resource(
            call.body, uid=f"uid-{self._created}", tenant=call.headers.get("x-tenant-id", TENANT)
        )
        self.services[key] = service
        return Reply(201, service)

    def _replace(self, call: Call, key: tuple[str | None, str], current: dict[str, Any]) -> Reply:
        metadata = call.body["metadata"]
        old = current["metadata"]
        reason = None
        if current["status"]["phase"] == "Terminating":
            reason = "Terminating"
        elif metadata.get("uid") not in (None, old["uid"]):
            reason = "UidMismatch"
        elif metadata.get("resourceVersion") not in (None, old["resourceVersion"]):
            reason = "ResourceVersionMismatch"
        if reason is not None:
            message = f"InferenceService '{key[1]}' was not updated: {reason}"
            return self._error(call, 409, "CONFLICT", message, {"reason": reason})
        spec = call.body.get("spec")
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        if (spec, labels, annotations) == (current["spec"], old["labels"], old["annotations"]):
            return Reply(200, current)
        updated = copy.deepcopy(current)
        updated["metadata"].update(
            labels=labels,
            annotations=annotations,
            resourceVersion=str(int(old["resourceVersion"]) + 1),
        )
        if spec != current["spec"]:
            updated["spec"] = copy.deepcopy(spec)
            updated["metadata"]["generation"] = old["generation"] + 1
        self.services[key] = updated
        return Reply(200, updated)

    def _delete(self, key: tuple[str | None, str], current: dict[str, Any]) -> Reply:
        if self.delete_status == 202:
            current["status"]["phase"] = "Terminating"
            return Reply(202, {"name": key[1], "phase": "Terminating"})
        del self.services[key]
        return Reply(204)

    def _list(self, call: Call) -> Reply:
        namespace = call.query.get("namespace")
        limit = int(call.query.get("limit", "100"))
        start = int(call.query.get("pageToken") or "0")
        items = sorted(
            (s for (ns, _), s in self.services.items() if namespace is None or ns == namespace),
            key=lambda s: (s["metadata"].get("namespace") or "", s["metadata"]["name"]),
        )
        page = items[start : start + limit]
        body: dict[str, Any] = {"items": page, "count": len(page)}
        if start + limit < len(items):
            body["nextPageToken"] = str(start + limit)
        return Reply(200, body)

    def _error(
        self,
        call: Call,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Reply:
        correlation_id = call.headers.get("x-correlation-id", "cid-server")
        return Reply(status, error_body(code, message, correlation_id, details))


def _handler_for(api: FakeApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            parts = urlsplit(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            call = Call(
                method=self.command,
                target=self.path,
                path=parts.path,
                query=dict(parse_qsl(parts.query, keep_blank_values=True)),
                headers={name.lower(): value for name, value in self.headers.items()},
                body=json.loads(raw) if raw else None,
            )
            reply = api.handle(call)
            if reply.status == DROP:
                self.close_connection = True
                return
            payload = reply.raw if reply.body is None else json.dumps(reply.body).encode()
            content_type = "text/plain; charset=utf-8" if reply.body is None else "application/json"
            self.send_response(reply.status)
            for name, value in {"Content-Type": content_type, **reply.headers}.items():
                self.send_header(name, value)
            if reply.status != 204:
                self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if reply.status != 204:
                self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    return Handler


class FakeClock:
    """代替 ``commands`` 模块中的 :mod:`time`：``sleep`` 不等待，只推进 ``monotonic``。"""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
