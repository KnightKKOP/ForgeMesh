"""启动器网络工具测试：监听地址与实例地址主机的校验、实例地址、``GET /health`` 探测。"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable

import pytest
from forgemesh.controlplane.deployment.network import (
    check_advertise_host,
    check_bind_host,
    instance_url,
    local_address,
    probe_health,
    resolve_hosts,
)

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", "127.0.0.1"), ("0.0.0.0", "0.0.0.0"), ("0:0::0", "::"), ("::1", "::1")],
)
def test_bind_hosts_are_ip_addresses(host: str, expected: str) -> None:
    assert check_bind_host(host) == expected


@pytest.mark.unit
@pytest.mark.parametrize("host", ["localhost", "control-plane", "", "127.0.0.1:80"])
def test_bind_hosts_that_are_not_ip_addresses_are_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="is not an IP address"):
        check_bind_host(host)


@pytest.mark.unit
@pytest.mark.parametrize(
    "host", ["control-plane", "gpu-1.example.com", "localhost", "10.0.0.7", "::1", "a" * 63]
)
def test_advertise_hosts_are_host_names_or_ip_addresses(host: str) -> None:
    assert check_advertise_host(host) == host


@pytest.mark.unit
@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("0.0.0.0", "wildcard"),
        ("::", "wildcard"),
        ("", "neither"),
        ("under_score", "neither"),
        ("-leading", "neither"),
        ("a..b", "neither"),
        ("a" * 64, "neither"),
        ("host:80", "neither"),
        ("http://host", "neither"),
    ],
)
def test_unusable_advertise_hosts_are_rejected(host: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_advertise_host(host)


@pytest.mark.unit
def test_the_advertise_host_defaults_to_the_bind_host_unless_it_is_a_wildcard() -> None:
    assert resolve_hosts("127.0.0.1", None) == ("127.0.0.1", "127.0.0.1")
    assert resolve_hosts("0.0.0.0", "control-plane") == ("0.0.0.0", "control-plane")
    with pytest.raises(ValueError, match="an advertise host is required when listening on ::"):
        resolve_hosts("::", None)


@pytest.mark.unit
def test_instance_urls_and_local_addresses() -> None:
    assert instance_url("control-plane", 8000) == "http://control-plane:8000"
    assert instance_url("::1", 8000) == "http://[::1]:8000"
    assert local_address("0.0.0.0") == "127.0.0.1"
    assert local_address("::") == "::1"
    assert local_address("10.0.0.7") == "10.0.0.7"


async def _serve(handler: Handler, probe: Callable[[int], Awaitable[bool]]) -> bool:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        return await probe(server.sockets[0].getsockname()[1])
    finally:
        server.close()
        await server.wait_closed()


def _replying(status_line: bytes) -> Handler:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        assert request.startswith(b"GET /health HTTP/1.1\r\n")
        writer.write(status_line + b"\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    return handler


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status_line", "healthy"),
    [
        (b"HTTP/1.1 200 OK", True),
        (b"HTTP/1.0 200 OK", True),
        (b"HTTP/1.1 503 Service Unavailable", False),
        (b"garbage", False),
    ],
)
def test_probe_health_wants_a_200_from_get_health(status_line: bytes, healthy: bool) -> None:
    async def probe(port: int) -> bool:
        return await probe_health("127.0.0.1", port)

    assert asyncio.run(_serve(_replying(status_line), probe)) is healthy


@pytest.mark.unit
def test_probe_health_reports_unreachable_and_silent_instances_as_unhealthy() -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.close()

    async def probe(port: int) -> bool:
        return await probe_health("127.0.0.1", port, timeout=0.05)

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    assert asyncio.run(_serve(silent, probe)) is False
    assert asyncio.run(probe_health("127.0.0.1", closed_port)) is False
