"""密钥处理安全测试。

验证密钥引用与日志脱敏机制，覆盖以下安全边界：

* **密钥不出现在日志明文中**：StructuredLogger 自动对敏感字段脱敏。
* **敏感字段脱敏**：password / token / secret / api_key / credentials 替换为 ``"***"``。
* **嵌套结构递归脱敏**：嵌套字典中的敏感字段同样被脱敏。
* **诊断包日志脱敏**：DiagnosticCollector 采集的日志已脱敏。
* **SecretRef 不暴露明文**：SecretRef 只存引用，resolve() 不返回明文。

对齐 architecture 13 / 14（只存引用，不含明文）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from forgemesh.controlplane.diagnostics import (
    SENSITIVE_FIELDS,
    DiagnosticCollector,
    _redact_sensitive,
)
from forgemesh.controlplane.observability import StructuredLogger
from forgemesh.core.secrets import SecretRef

_PLAINTEXT_PASSWORD = "super-secret-password-12345"
_PLAINTEXT_TOKEN = "bearer-token-abcdef"
_PLAINTEXT_API_KEY = "ak-XXXXXXXXXXXX"
_PLAINTEXT_SECRET = "my-secret-value"
_PLAINTEXT_CREDENTIALS = "user:pass"


def _run(coro: Any) -> Any:
    """在同步测试中驱动协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 密钥不出现在日志明文中
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_secret_not_in_logs() -> None:
    """密钥不出现在日志明文中：StructuredLogger 对敏感字段自动脱敏。"""
    logger = StructuredLogger(name="security-test")

    logger.info(
        "user login",
        password=_PLAINTEXT_PASSWORD,
        token=_PLAINTEXT_TOKEN,
        api_key=_PLAINTEXT_API_KEY,
        secret=_PLAINTEXT_SECRET,
        credentials=_PLAINTEXT_CREDENTIALS,
    )

    records = logger.get_records()
    assert len(records) == 1
    record = records[0]

    # 所有敏感字段均被脱敏为 "***"
    assert record["password"] == "***"
    assert record["token"] == "***"
    assert record["api_key"] == "***"
    assert record["secret"] == "***"
    assert record["credentials"] == "***"

    # 明文不出现在任何字段值中
    record_str = json.dumps(record, ensure_ascii=False, default=str)
    assert _PLAINTEXT_PASSWORD not in record_str
    assert _PLAINTEXT_TOKEN not in record_str
    assert _PLAINTEXT_API_KEY not in record_str
    assert _PLAINTEXT_SECRET not in record_str
    assert _PLAINTEXT_CREDENTIALS not in record_str


# --------------------------------------------------------------------------- #
# 敏感字段脱敏
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_sensitive_fields_redacted() -> None:
    """password / token / secret / api_key / credentials 字段均被脱敏为 "***"。"""
    data: dict[str, Any] = {
        "password": _PLAINTEXT_PASSWORD,
        "token": _PLAINTEXT_TOKEN,
        "secret": _PLAINTEXT_SECRET,
        "api_key": _PLAINTEXT_API_KEY,
        "credentials": _PLAINTEXT_CREDENTIALS,
        "normal_field": "should-stay",
        "count": 42,
    }

    redacted = _redact_sensitive(data)

    # 敏感字段脱敏
    assert redacted["password"] == "***"
    assert redacted["token"] == "***"
    assert redacted["secret"] == "***"
    assert redacted["api_key"] == "***"
    assert redacted["credentials"] == "***"
    # 非敏感字段保留
    assert redacted["normal_field"] == "should-stay"
    assert redacted["count"] == 42

    # 确认 SENSITIVE_FIELDS 覆盖全部五种敏感字段名
    assert frozenset({"password", "token", "secret", "api_key", "credentials"}) == SENSITIVE_FIELDS

    # 大小写不敏感匹配（键名小写后比对）
    mixed = {"Password": _PLAINTEXT_PASSWORD, "API_KEY": _PLAINTEXT_API_KEY}
    mixed_redacted = _redact_sensitive(mixed)
    assert mixed_redacted["Password"] == "***"
    assert mixed_redacted["API_KEY"] == "***"


# --------------------------------------------------------------------------- #
# 嵌套结构递归脱敏
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_nested_sensitive_redacted() -> None:
    """嵌套字典中的敏感字段递归脱敏。"""
    data: dict[str, Any] = {
        "user": {
            "name": "alice",
            "password": _PLAINTEXT_PASSWORD,
            "profile": {
                "api_key": _PLAINTEXT_API_KEY,
                "preferences": {"theme": "dark"},
            },
        },
        "config": {
            "database": {
                "credentials": _PLAINTEXT_CREDENTIALS,
                "host": "localhost",
            },
        },
        "token": _PLAINTEXT_TOKEN,
    }

    redacted = _redact_sensitive(data)

    # 顶层敏感字段脱敏
    assert redacted["token"] == "***"

    # 一层嵌套脱敏
    assert redacted["user"]["password"] == "***"
    assert redacted["user"]["name"] == "alice"

    # 二层嵌套脱敏
    assert redacted["user"]["profile"]["api_key"] == "***"
    assert redacted["user"]["profile"]["preferences"]["theme"] == "dark"

    # 不同分支嵌套脱敏
    assert redacted["config"]["database"]["credentials"] == "***"
    assert redacted["config"]["database"]["host"] == "localhost"

    # 明文不出现在整个脱敏结果中
    redacted_str = json.dumps(redacted, ensure_ascii=False, default=str)
    assert _PLAINTEXT_PASSWORD not in redacted_str
    assert _PLAINTEXT_API_KEY not in redacted_str
    assert _PLAINTEXT_CREDENTIALS not in redacted_str
    assert _PLAINTEXT_TOKEN not in redacted_str

    # 原字典不被修改（不可变语义）
    assert data["user"]["password"] == _PLAINTEXT_PASSWORD
    assert data["user"]["profile"]["api_key"] == _PLAINTEXT_API_KEY


# --------------------------------------------------------------------------- #
# 诊断包日志脱敏
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_diagnostics_logs_redacted() -> None:
    """诊断包中日志已脱敏：敏感字段不出现在采集结果的日志中。"""
    collector = DiagnosticCollector()
    tenant_id = "tenant-a"

    now = int(time.time())
    collector.register_log(
        tenant_id,
        {
            "timestamp": now,
            "level": "info",
            "message": "resource created",
            "password": _PLAINTEXT_PASSWORD,
            "token": _PLAINTEXT_TOKEN,
            "nested": {
                "api_key": _PLAINTEXT_API_KEY,
                "safe": "ok",
            },
        },
    )

    bundle = _run(collector.collect(tenant_id))

    assert len(bundle.logs) == 1
    log_entry = bundle.logs[0]

    # 敏感字段已脱敏
    assert log_entry["password"] == "***"
    assert log_entry["token"] == "***"
    assert log_entry["nested"]["api_key"] == "***"
    assert log_entry["nested"]["safe"] == "ok"

    # 明文不出现在诊断包日志中
    log_str = json.dumps(log_entry, ensure_ascii=False, default=str)
    assert _PLAINTEXT_PASSWORD not in log_str
    assert _PLAINTEXT_TOKEN not in log_str
    assert _PLAINTEXT_API_KEY not in log_str


# --------------------------------------------------------------------------- #
# SecretRef 不暴露明文值
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_secret_ref_not_exposed() -> None:
    """SecretRef 不暴露明文值：只存引用（provider / name / key），不含明文。"""
    ref = SecretRef(provider="vault", name="db/creds", key="password")

    # SecretRef 只持有引用信息，不持有明文
    assert ref.provider == "vault"
    assert ref.name == "db/creds"
    assert ref.key == "password"

    # SecretRef 的字段中不含任何明文密钥值
    ref_dump = ref.model_dump()
    ref_str = json.dumps(ref_dump, ensure_ascii=False, default=str)
    assert _PLAINTEXT_PASSWORD not in ref_str
    assert _PLAINTEXT_TOKEN not in ref_str

    # resolve() 默认不实现——核心契约不持有明文，须由具体 Provider 实现
    with pytest.raises(NotImplementedError):
        _run(ref.resolve())

    # SecretRef 序列化后仍不含明文（可安全写入日志/事件/审计）
    ref_json = ref.model_dump_json()
    assert "vault" in ref_json
    assert "db/creds" in ref_json
    # 无任何明文字段
    assert "super-secret" not in ref_json
