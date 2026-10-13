"""CLI 单元测试的公共夹具：假 API 服务器、命令行运行器与虚拟时钟。"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from forgemesh.cli import commands
from forgemesh.cli.config import API_KEY_ENV, API_URL_ENV
from forgemesh.cli.main import main

from .fakes import FakeApi, FakeClock, Result


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeApi]:
    """运行中的假 API；``FORGEMESH_API_URL`` 指向它，没有配置 key。"""
    server = FakeApi()
    monkeypatch.setenv(API_URL_ENV, server.url)
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    # 环境中的 http_proxy 等变量不能把发往本机测试服务器的请求转给代理
    monkeypatch.setenv("no_proxy", "*")
    yield server
    server.close()


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str]) -> Callable[..., Result]:
    """以给定参数运行 ``forgemesh``，返回退出码与输出。"""

    def run(*args: str) -> Result:
        code = main(list(args))
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return run


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """``wait`` 的虚拟时钟：等待不耗时，测试可以检查每次等待的秒数。"""
    fake = FakeClock()
    monkeypatch.setattr(commands, "time", fake)
    return fake
