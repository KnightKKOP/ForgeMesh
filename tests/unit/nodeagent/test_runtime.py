"""``forgemesh.nodeagent.runtime`` 单元测试。

覆盖本地运行时管理：

* start_instance 启动并返回 RuntimeInstance（endpoint 即 instance_id）；
* 重复启动同端口抛 ConflictError；
* stop_instance 停止实例；不存在抛 NotFoundError；
* list_instances 列出运行中实例；
* get_instance 查询实例（存在/不存在）。

测试以 ``asyncio.run`` 驱动异步方法，不依赖 pytest-asyncio。
"""

from __future__ import annotations

import asyncio

import pytest
from forgemesh.core.errors import ConflictError, NotFoundError
from forgemesh.nodeagent.runtime import RuntimeConfig, RuntimeManager

pytestmark = pytest.mark.unit


def _config(**overrides: object) -> RuntimeConfig:
    """构造一份合法的运行时配置。"""
    base: dict = {
        "runtime": "vllm",
        "model_path": "/models/llama",
        "port": 8000,
        "gpu_ids": [0, 1],
        "env": {"VLLM_PORT": "8000"},
    }
    base.update(overrides)
    return RuntimeConfig(**base)


def test_start_instance() -> None:
    """启动实例应返回 RuntimeInstance，endpoint 为 instance_id。"""
    mgr = RuntimeManager()

    inst = asyncio.run(mgr.start_instance(_config()))

    assert inst.runtime == "vllm"
    assert inst.endpoint == "http://localhost:8000"
    assert inst.health == "Unknown"


def test_start_duplicate_raises_conflict() -> None:
    """同端口重复启动应抛 ConflictError。"""
    mgr = RuntimeManager()
    asyncio.run(mgr.start_instance(_config(port=8000)))

    with pytest.raises(ConflictError):
        asyncio.run(mgr.start_instance(_config(port=8000)))


def test_stop_instance() -> None:
    """停止实例后 get_instance 应返回 None。"""
    mgr = RuntimeManager()
    inst = asyncio.run(mgr.start_instance(_config()))

    asyncio.run(mgr.stop_instance(inst.endpoint))  # type: ignore[arg-type]

    assert asyncio.run(mgr.get_instance(inst.endpoint)) is None  # type: ignore[arg-type]


def test_stop_unknown_raises_not_found() -> None:
    """停止不存在的实例应抛 NotFoundError。"""
    mgr = RuntimeManager()

    with pytest.raises(NotFoundError):
        asyncio.run(mgr.stop_instance("http://localhost:9999"))


def test_list_instances() -> None:
    """list_instances 应返回所有运行中实例。"""
    mgr = RuntimeManager()
    asyncio.run(mgr.start_instance(_config(port=8000)))
    asyncio.run(mgr.start_instance(_config(port=8001, runtime="tgi")))

    instances = asyncio.run(mgr.list_instances())

    assert len(instances) == 2
    runtimes = {i.runtime for i in instances}
    assert runtimes == {"vllm", "tgi"}


def test_get_instance() -> None:
    """get_instance 存在返回实例，不存在返回 None。"""
    mgr = RuntimeManager()
    inst = asyncio.run(mgr.start_instance(_config()))

    got = asyncio.run(mgr.get_instance(inst.endpoint))  # type: ignore[arg-type]
    assert got is not None and got.runtime == "vllm"

    none = asyncio.run(mgr.get_instance("http://localhost:9999"))
    assert none is None
