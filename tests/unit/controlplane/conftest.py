"""控制平面单元测试的公共夹具。"""

from __future__ import annotations

import pytest
from forgemesh.controlplane.api.dependencies import init_authenticator, reset_authenticator
from forgemesh.controlplane.auth import HeaderAuthenticator


@pytest.fixture(autouse=True)
def _dev_authenticator():
    """默认注入 ``dev`` 认证器（信任 X-Tenant-Id），认证相关测试自行替换。"""
    init_authenticator(HeaderAuthenticator())
    yield
    reset_authenticator()
