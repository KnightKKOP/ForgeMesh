"""``apply -f`` 的文档读取单元测试：YAML / JSON、多文档、标准输入、命名空间与租户规则，
以及在发出任何请求之前发现的错误。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml
from forgemesh.cli.documents import Document, load_documents
from forgemesh.cli.errors import UsageError

from .fakes import document


def load(
    paths: list[str], *, namespace: str | None = None, tenant: str | None = None, stdin: str = ""
) -> list[Document]:
    return load_documents(paths, namespace=namespace, tenant=tenant, stdin=io.StringIO(stdin))


def load_error(stdin: str, **options: str) -> str:
    with pytest.raises(UsageError) as raised:
        load(["-"], stdin=stdin, **options)
    return raised.value.message


def test_a_yaml_file_with_several_documents(tmp_path: Path) -> None:
    path = tmp_path / "svcs.yaml"
    path.write_text(
        "# two services\n---\n"
        + yaml.safe_dump(document("a"))
        + "---\n---\n"
        + yaml.safe_dump(document("b")),
        encoding="utf-8",
    )

    assert load([str(path)]) == [
        Document(f"{path} (document 1)", document("a"), "a", "default", None),
        Document(f"{path} (document 2)", document("b"), "b", "default", None),
    ]


def test_json_objects_arrays_and_yaml_lists(tmp_path: Path) -> None:
    single = tmp_path / "svc.json"
    single.write_text(json.dumps(document()), encoding="utf-8")
    array = tmp_path / "svcs.json"
    array.write_text(json.dumps([document("a"), document("b")]), encoding="utf-8")
    sequence = tmp_path / "svcs.yaml"
    sequence.write_text(yaml.safe_dump([document("c")]), encoding="utf-8")

    docs = load([str(single), str(array), str(sequence)])

    assert [(doc.source, doc.name) for doc in docs] == [
        (str(single), "llama"),
        (f"{array} (document 1)", "a"),
        (f"{array} (document 2)", "b"),
        (str(sequence), "c"),
    ]


def test_yaml_flow_style_is_not_mistaken_for_invalid_json() -> None:
    [doc] = load(["-"], stdin="{kind: InferenceService, metadata: {name: a}, spec: {}}\n")

    assert doc.body == {"kind": "InferenceService", "metadata": {"name": "a"}, "spec": {}}


def test_a_byte_order_mark_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "svc.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(document()).encode())

    assert [doc.name for doc in load([str(path)])] == ["llama"]


def test_standard_input_can_be_read_once() -> None:
    [doc] = load(["-"], stdin=yaml.safe_dump(document()))

    assert doc.source == "<stdin>"
    with pytest.raises(UsageError) as raised:
        load(["-", "-"])
    assert raised.value.message == "standard input ('-') can be read only once"


def test_the_namespace_option_fills_in_a_missing_namespace() -> None:
    [doc] = load(["-"], namespace="team-a", stdin=json.dumps(document(namespace=None)))
    [without] = load(["-"], stdin=json.dumps(document(namespace=None)))

    assert (doc.namespace, doc.body["metadata"]) == (
        "team-a",
        {"name": "llama", "namespace": "team-a"},
    )
    assert (without.namespace, without.body["metadata"]) == (None, {"name": "llama"})


def test_the_namespace_option_must_match_the_document() -> None:
    assert load_error(json.dumps(document()), namespace="other") == (
        "<stdin>: metadata.namespace 'default' does not match --namespace 'other'"
    )


def test_the_tenant_comes_from_the_document_or_the_option() -> None:
    [own] = load(["-"], stdin=json.dumps(document(tenantId="team-a")))
    [legacy] = load(["-"], stdin=json.dumps(document(tenant_id="team-a")))
    [option] = load(["-"], tenant="team-a", stdin=json.dumps(document()))
    [same] = load(["-"], tenant="team-a", stdin=json.dumps(document(tenantId="team-a")))

    assert [doc.tenant for doc in (own, legacy, option, same)] == ["team-a"] * 4


def test_conflicting_tenants_are_rejected() -> None:
    assert load_error(json.dumps(document(tenantId="team-a")), tenant="team-b") == (
        "<stdin>: metadata.tenantId 'team-a' does not match --tenant 'team-b'"
    )
    assert load_error(json.dumps(document(tenantId="team-a", tenant_id="team-b"))) == (
        "<stdin>: metadata.tenantId must be a single string"
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "<stdin>: no documents found"),
        ("---\n---\n", "<stdin>: no documents found"),
        ("- 1\n", "<stdin>: a resource document must be a mapping"),
        ("[1, 2]", "<stdin> (document 1): a resource document must be a mapping"),
        (
            "kind: Workflow\nmetadata: {name: a}\n",
            "<stdin>: kind 'Workflow' is not supported; the CLI applies InferenceService",
        ),
        ("spec: {}\n", "<stdin>: metadata must be a mapping"),
        ("metadata: [a]\n", "<stdin>: metadata must be a mapping"),
        ("metadata: {name: ''}\n", "<stdin>: metadata.name must be a non-empty string"),
        ("metadata: {name: 5}\n", "<stdin>: metadata.name must be a non-empty string"),
        ("metadata: {name: a, namespace: 5}\n", "<stdin>: metadata.namespace must be a string"),
        (
            "metadata: {name: a, namespace: ''}\n",
            "<stdin>: metadata.namespace must not be empty; omit it for resources without a "
            "namespace",
        ),
        (
            "metadata: {name: a, tenantId: team a}\n",
            "<stdin>: metadata.tenantId must be a non-empty string of visible ASCII characters",
        ),
        (
            "metadata: {name: a, tenantId: 5}\n",
            "<stdin>: metadata.tenantId must be a single string",
        ),
        (
            "metadata: {name: a}\nspec: {since: 2026-01-01}\n",
            "<stdin>: spec.since is a date value, which JSON cannot represent; quote it to make "
            "it a string",
        ),
        (
            "metadata: {name: a}\nspec: {items: [1, 2026-01-01]}\n",
            "<stdin>: spec.items[1] is a date value, which JSON cannot represent; quote it to "
            "make it a string",
        ),
        (
            "metadata: {name: a}\nspec: {ratio: .nan}\n",
            "<stdin>: spec.ratio is nan, which JSON cannot represent",
        ),
        (
            "metadata: {name: a}\nspec: {1: one}\n",
            "<stdin>: spec has a key that is not a string: 1",
        ),
        ("&loop [*loop]\n", "<stdin>: a resource document must be a mapping"),
        ("&loop {metadata: *loop}\n", "<stdin>: the document is nested too deeply"),
        ("[" * 100_000 + "]" * 100_000, "<stdin> is nested too deeply"),
    ],
)
def test_invalid_documents_are_rejected(text: str, message: str) -> None:
    assert load_error(text) == message


def test_invalid_yaml_is_reported_with_its_position() -> None:
    assert load_error("metadata: {name: a\n").startswith(
        "<stdin> is not valid YAML or JSON: while parsing a flow mapping"
    )


def test_an_unreadable_file_is_reported(tmp_path: Path) -> None:
    binary = tmp_path / "svc.yaml"
    binary.write_bytes(b"\xff\xfe")

    with pytest.raises(UsageError) as missing:
        load([str(tmp_path / "missing.yaml")])
    with pytest.raises(UsageError) as not_utf8:
        load([str(binary)])

    assert missing.value.message == (
        f"cannot read {tmp_path / 'missing.yaml'}: No such file or directory"
    )
    assert not_utf8.value.message == f"{binary} is not UTF-8 text"
