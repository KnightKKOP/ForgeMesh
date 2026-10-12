"""启动器共用的网络工具：监听地址与实例地址主机的校验、实例地址、健康检查。

实例监听的地址（``bind_host``）与写进 ``status.endpoints`` 的主机（``advertise_host``）
分开配置：容器中实例监听 ``0.0.0.0``，地址却要写成网关能解析到这个容器的名字。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket

#: DNS 主机名中的一个标签（RFC 1123）。
_HOSTNAME_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")


def check_bind_host(host: str) -> str:
    """校验监听地址并返回规范写法：须是 IP 地址，``0.0.0.0`` / ``::`` 监听全部地址。

    Raises:
        ValueError: 不是 IP 地址（主机名可能解析出多个地址，监听哪一个并不确定）。
    """
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        raise ValueError(f"{host!r} is not an IP address") from None


def check_advertise_host(host: str) -> str:
    """校验写进实例地址的主机：IP 地址（不能是 ``0.0.0.0`` / ``::``）或 DNS 主机名。

    Raises:
        ValueError: 既不是 IP 地址也不是主机名，或是无法连接的通配地址。
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(host) <= 253 and all(_HOSTNAME_LABEL.fullmatch(label) for label in labels):
            return host
        raise ValueError(f"{host!r} is neither an IP address nor a host name") from None
    if address.is_unspecified:
        raise ValueError(f"{host!r} is a wildcard address that clients cannot connect to")
    return str(address)


def resolve_hosts(bind_host: str, advertise_host: str | None) -> tuple[str, str]:
    """校验并返回 ``(bind_host, advertise_host)``。

    ``advertise_host`` 为空时取监听地址；监听全部地址（``0.0.0.0`` / ``::``）时必须给出。

    Raises:
        ValueError: 地址非法，或监听全部地址却没有给出 ``advertise_host``。
    """
    bind = check_bind_host(bind_host)
    if advertise_host is None:
        if ipaddress.ip_address(bind).is_unspecified:
            raise ValueError(f"an advertise host is required when listening on {bind}")
        return bind, bind
    return bind, check_advertise_host(advertise_host)


def instance_url(host: str, port: int) -> str:
    """实例基地址 ``http://host:port``（IPv6 地址加方括号）。"""
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


def local_address(bind_host: str) -> str:
    """本机连接监听在 ``bind_host`` 上的服务所用的地址：通配地址换成回环地址。"""
    address = ipaddress.ip_address(bind_host)
    if address.is_unspecified:
        return "::1" if address.version == 6 else "127.0.0.1"
    return bind_host


def socket_family(host: str) -> socket.AddressFamily:
    """IP 地址 ``host`` 对应的套接字地址族。"""
    return socket.AF_INET6 if ipaddress.ip_address(host).version == 6 else socket.AF_INET


async def probe_health(host: str, port: int, *, timeout: float = 5.0) -> bool:
    """``GET http://host:port/health`` 在 ``timeout`` 秒内返回 ``200`` 时为 ``True``。

    连接失败、超时与其他状态码都返回 ``False``。直接用 asyncio 连接：实例与控制平面在
    同一台机器上，不能像 urllib 那样按 ``HTTP_PROXY`` 等环境变量走代理，也不占用线程。
    """
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    request = f"GET /health HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n"
    writer: asyncio.StreamWriter | None = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(request.encode("ascii"))
            await writer.drain()
            status_line = await reader.readline()
    except (OSError, TimeoutError, ValueError):
        return False
    finally:
        if writer is not None:
            writer.close()
    parts = status_line.split(None, 2)
    return len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1] == b"200"


__all__ = [
    "check_advertise_host",
    "check_bind_host",
    "instance_url",
    "local_address",
    "probe_health",
    "resolve_hosts",
    "socket_family",
]
