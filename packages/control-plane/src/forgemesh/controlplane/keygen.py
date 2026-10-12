"""生成绑定租户的 API key：``python -m forgemesh.controlplane.keygen --tenant acme --subject ci-bot``。

输出明文 key（只显示这一次）与对应的 key 文件条目；把条目追加到
``FORGEMESH_API_KEYS_FILE`` / ``GATEWAY_API_KEYS_FILE`` 指向的文件的 ``keys`` 数组
（格式见 :mod:`forgemesh.controlplane.auth`）。
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from .auth import (
    KNOWN_SCOPES,
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKeyEntry,
    generate_api_key,
    hash_api_key,
)


def main(argv: list[str] | None = None) -> int:
    """命令行入口；参数非法时返回 2。"""
    parser = argparse.ArgumentParser(
        prog="python -m forgemesh.controlplane.keygen",
        description="Generate an API key bound to a tenant.",
    )
    parser.add_argument("--tenant", required=True, help="tenant bound to the key")
    parser.add_argument("--subject", required=True, help="subject (service account or user)")
    parser.add_argument("--id", help="entry id (default: <tenant>-<subject>)")
    parser.add_argument(
        "--scope",
        action="append",
        choices=sorted(KNOWN_SCOPES),
        help="granted scope, repeatable (default: read and write)",
    )
    args = parser.parse_args(argv)

    key = generate_api_key()
    try:
        entry = ApiKeyEntry(
            id=args.id or f"{args.tenant}-{args.subject}",
            sha256=hash_api_key(key),
            tenantId=args.tenant,
            subject=args.subject,
            scopes=tuple(args.scope) if args.scope else (SCOPE_READ, SCOPE_WRITE),
        )
    except ValidationError as exc:
        print(f"invalid key parameters: {exc}", file=sys.stderr)
        return 2
    print("API key (store it in a secret manager; it is not shown again):")
    print(key)
    print('Key file entry (append it to the "keys" array):')
    print(json.dumps(entry.model_dump(by_alias=True, mode="json"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
