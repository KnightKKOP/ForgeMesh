"""分页游标的签发与校验（:mod:`forgemesh.controlplane.pagetokens`）。

资源列表与审计记录列表的游标行为（篡改、换条件、重启）分别见 ``test_registry.py`` 与
``test_audit_api.py``。
"""

from __future__ import annotations

import sqlite3

import pytest
from forgemesh.controlplane.pagetokens import (
    InvalidPageTokenError,
    PageTokenSigner,
    load_page_token_key,
    page_scope,
)

SCOPE = page_scope({"list": "things", "tenant": "tenant-a"})
POSITION = {"name": str}


@pytest.mark.unit
def test_the_key_is_created_once_and_kept_in_the_database(tmp_path) -> None:
    path = tmp_path / "meta.db"
    conn = sqlite3.connect(path)
    try:
        key = load_page_token_key(conn)
        again = load_page_token_key(conn)
    finally:
        conn.close()
    reopened = sqlite3.connect(path)
    try:
        after_restart = load_page_token_key(reopened)
        rows = reopened.execute("SELECT name FROM registry_meta").fetchall()
    finally:
        reopened.close()

    assert len(key) == 32
    assert key == again == after_restart
    assert rows == [("page_token_key",)]


@pytest.mark.unit
def test_tokens_are_only_valid_for_their_scope_and_key() -> None:
    signer = PageTokenSigner(b"k" * 32)
    token = signer.encode({"name": "ü"}, SCOPE)

    assert signer.decode(token, SCOPE, POSITION) == ("ü",)
    with pytest.raises(InvalidPageTokenError):
        signer.decode(token, page_scope({"list": "things", "tenant": "tenant-b"}), POSITION)
    with pytest.raises(InvalidPageTokenError):
        PageTokenSigner(b"o" * 32).decode(token, SCOPE, POSITION)


@pytest.mark.unit
@pytest.mark.parametrize("position", [{}, {"name": 7}, {"name": None}])
def test_signed_tokens_without_the_expected_position_are_rejected(position: dict) -> None:
    signer = PageTokenSigner(b"k" * 32)
    token = signer.encode(position, SCOPE)

    with pytest.raises(InvalidPageTokenError):
        signer.decode(token, SCOPE, POSITION)


@pytest.mark.unit
def test_the_scope_does_not_depend_on_the_order_of_the_conditions() -> None:
    assert page_scope({"tenant": "t", "list": "x"}) == page_scope({"list": "x", "tenant": "t"})
