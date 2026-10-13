"""CLI 命令：经 :class:`~.client.ApiClient` 调用 API，成功时返回退出码，失败时抛出
:class:`~.errors.CliError`。

命令只管理 ``InferenceService``。单个资源的命令不带 ``--namespace`` 时指不带命名空间的资源，
``list`` 不带 ``--namespace`` 时列出全部命名空间，与 API 的规则一致。
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from .client import REQUEST_TIMEOUT_SECONDS, ApiClient
from .documents import KIND, Document, load_documents
from .errors import ApiError, CliError, ConnectionFailure, ExitCode
from .output import (
    NONE,
    describe_service,
    printable,
    ready_condition,
    resource_ref,
    service_table,
    to_json,
    to_yaml,
    to_yaml_stream,
)

#: ``apply`` 处理一个文档的尝试次数：读取之后资源被并发修改（``409``）、创建或删除时
#: 重新读取再试。
APPLY_ATTEMPTS = 5
#: ``wait`` 两次读取之间的秒数。
POLL_INTERVAL_SECONDS = 1.0

_READY = "Ready"
_FAILED = "Failed"
_TERMINATING = "Terminating"
#: 服务中的 phase：``spec`` 变化之后，被策略拦下或缺少调度器 / 启动器的更新不写 ``status``，
#: 服务以旧的 ``observedGeneration`` 停在这些 phase。
_SERVING_PHASES = frozenset({_READY, "Degraded"})


def cmd_apply(args: argparse.Namespace, client: ApiClient) -> int:
    """``apply -f FILE``：资源不存在就创建，存在就以读到的版本整体替换。

    某个资源失败时报告错误、继续处理其余的资源，返回第一个失败的退出码。
    """
    documents = load_documents(
        args.files, namespace=args.namespace, tenant=args.tenant, stdin=sys.stdin
    )
    result = ExitCode.OK
    for document in documents:
        ref = f"{KIND} {resource_ref(document.namespace, document.name)}"
        try:
            outcome = _apply(client.with_tenant(document.tenant), document)
        except CliError as exc:
            print(f"Error: {ref} ({document.source}): {exc.message}", file=sys.stderr)
            if result is ExitCode.OK:
                result = exc.exit_code
            continue
        print(f"{ref} {outcome}")
    return result


def _apply(client: ApiClient, document: Document) -> str:
    """创建或更新一个资源，返回 ``created`` / ``configured`` / ``unchanged``。"""
    for _ in range(APPLY_ATTEMPTS):
        try:
            current = client.get_service(document.name, document.namespace)
        except ApiError as exc:
            if not exc.not_found:
                raise
            try:
                client.create_service(document.body)
            except ApiError as create_error:
                if create_error.status == 409:  # 刚被并发创建：下一轮读取后更新它
                    continue
                raise
            return "created"
        try:
            updated = client.replace_service(
                document.name, document.namespace, _versioned(document.body, current)
            )
        except ApiError as exc:
            if exc.status == 409 and exc.reason == _TERMINATING:
                raise CliError(
                    f"{exc.message}\nwait until it is deleted ("
                    f"{_command('wait', document.name, document.namespace)} --for delete) "
                    "and apply again",
                    exc.exit_code,
                ) from exc
            if exc.status == 409 or exc.not_found:
                continue
            raise
        same = updated["metadata"].get("resourceVersion") == current["metadata"].get(
            "resourceVersion"
        )
        return "unchanged" if same else "configured"
    raise CliError(
        f"the resource kept changing while it was applied ({APPLY_ATTEMPTS} attempts); try again",
        ExitCode.CONFLICT,
    )


def _versioned(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """以读到的 ``resourceVersion`` 与 ``uid`` 作为 ``PUT`` 的前置条件，替换文档中的值。

    文档中的版本多半已经过时（控制器每次写 ``status`` 都会递增版本），``apply`` 总是更新
    读到的版本；读取之后资源又被修改时 ``PUT`` 返回 ``409``，由调用方重新读取。
    """
    metadata = {
        **body["metadata"],
        "resourceVersion": current["metadata"].get("resourceVersion"),
        "uid": current["metadata"].get("uid"),
    }
    return {**body, "metadata": metadata}


def cmd_get(args: argparse.Namespace, client: ApiClient) -> int:
    """``get KIND NAME``：输出资源（YAML 或 JSON）。"""
    service = _get(client, args.name, args.namespace)
    sys.stdout.write(to_json(service) + "\n" if args.output == "json" else to_yaml(service))
    return ExitCode.OK


def cmd_describe(args: argparse.Namespace, client: ApiClient) -> int:
    """``describe KIND NAME``：便于阅读的详情，含条件与实例地址。"""
    print(describe_service(_get(client, args.name, args.namespace)))
    return ExitCode.OK


def cmd_list(args: argparse.Namespace, client: ApiClient) -> int:
    """``list KIND``：取全部页，输出表格、JSON 数组或以 ``---`` 分隔的 YAML 文档。"""
    services = list(client.list_services(args.namespace, page_size=args.page_size))
    if args.output == "json":
        print(to_json(services))
    elif args.output == "yaml":
        sys.stdout.write(to_yaml_stream(services))
    elif services:
        print(service_table(services))
    else:
        where = "" if args.namespace is None else f" in namespace {args.namespace}"
        print(f"No {KIND} resources found{where}.", file=sys.stderr)
    return ExitCode.OK


def cmd_delete(args: argparse.Namespace, client: ApiClient) -> int:
    """``delete KIND NAME``：受控删除，区分已删除（``204``）与进入 ``Terminating``（``202``）。"""
    ref = f"{KIND} {resource_ref(args.namespace, args.name)}"
    try:
        status = client.delete_service(args.name, args.namespace)
    except ApiError as exc:
        raise _not_found_hint(exc, args.namespace) from exc
    if status == 202:
        print(
            f"{ref} is terminating: the controller stops its instance and releases its "
            "accelerators, then deletes it"
        )
    else:
        print(f"{ref} deleted")
    return ExitCode.OK


def cmd_wait(args: argparse.Namespace, client: ApiClient) -> int:
    """``wait KIND NAME``：等资源就绪（``--for ready``）或被删除（``--for delete``）。

    就绪指同一次读取中 ``status.phase`` 为 ``Ready``、``Ready`` 条件为 ``True``，且
    ``metadata.observedGeneration`` 等于 ``generation``：``spec`` 变化之后服务先以旧的
    ``observedGeneration`` 保持 ``Ready``，控制器开始更新的一轮即记下新的 generation、进入
    ``Updating``，两者同时满足时服务运行的才是当前的 ``spec``。被策略拦下或缺少调度器 / 启动器
    的更新不写 ``status``，服务一直以旧的 ``observedGeneration`` 停在 ``Ready`` / ``Degraded``，
    超时的消息说明更新没有开始。资源正在删除，或在当前的 ``spec`` 下进入 ``Failed`` 时立即失败
    （``spec`` 变化之前控制器不会再部署它）；其余状态继续等待，包括重新部署中的 ``Degraded``。
    API 应答过一次之后，限流、``502`` - ``504`` 与连接失败视为暂时故障，继续等待。

    ``--for delete`` 在资源不存在时成功。开始等待时资源已经不存在，输出与等到它被删除时相同：
    ``delete`` 之后控制器可能在第一次读取之前就删除了资源，结果不能取决于这个先后。
    """
    ref = f"{KIND} {resource_ref(args.namespace, args.name)}"
    goal = "become Ready" if args.condition == "ready" else "be deleted"
    deadline = time.monotonic() + args.timeout
    answered = False
    last = "no response"
    while True:
        delay = POLL_INTERVAL_SECONDS
        try:
            service = client.get_service(args.name, args.namespace, timeout=_timeout(deadline))
        except ApiError as exc:
            answered = True
            if exc.not_found and args.condition == "delete":
                print(f"{ref} deleted")
                return ExitCode.OK
            if not exc.transient:
                raise _not_found_hint(exc, args.namespace) from exc
            last = exc.message
            delay = max(delay, exc.retry_after or 0)
        except ConnectionFailure as exc:
            if not answered:
                raise
            last = exc.message
        else:
            answered = True
            if args.condition == "ready":
                outcome, last = _readiness(service)
                if outcome is True:
                    print(f"{ref} is Ready")
                    return ExitCode.OK
                if outcome is False:
                    raise CliError(f"{ref} will not become Ready: {last}", ExitCode.WAIT_FAILED)
            else:
                last = printable(f"phase {_status(service).get('phase') or NONE}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CliError(
                f"timed out after {args.timeout:g} s waiting for {ref} to {goal}; "
                f"last seen: {last}",
                ExitCode.WAIT_TIMEOUT,
            )
        time.sleep(min(delay, remaining))


def _readiness(service: dict[str, Any]) -> tuple[bool | None, str]:
    """``(结论, 说明)``：``True`` 已就绪，``False`` 不会就绪，``None`` 继续等待。"""
    metadata = service["metadata"]
    phase = _status(service).get("phase")
    generation = metadata.get("generation")
    observed = metadata.get("observedGeneration")
    current = isinstance(generation, int) and observed == generation
    condition = ready_condition(service)
    description = f"phase {phase or NONE}"
    if condition is not None:
        description += f", Ready={condition.get('status')}"
        parts = (condition.get("reason"), condition.get("message"))
        detail = ": ".join(str(part) for part in parts if part)
        if detail:
            description += f" ({detail})"
    hint = ""
    if current:
        if phase == _FAILED:
            hint = "the controller deploys it again only when its spec changes"
    elif phase in _SERVING_PHASES:
        description += (
            f"; the update to generation {generation} has not started (observed {observed})"
        )
        hint = (
            "an update that the policy denies or holds for approval, or that has no scheduler "
            "or launcher, does not start; the reason is in the control plane log"
        )
    else:
        description += (
            f"; the controller has not processed generation {generation} yet (observed {observed})"
        )
    description = printable(description) + (f"\n{hint}" if hint else "")
    if phase == _TERMINATING or (phase == _FAILED and current):
        return False, description
    ready = condition is not None and condition.get("status") == "True"
    if phase == _READY and ready and current:
        return True, description
    return None, description


def _status(service: dict[str, Any]) -> dict[str, Any]:
    status = service.get("status")
    return status if isinstance(status, dict) else {}


def _timeout(deadline: float) -> float:
    """一次读取的超时：不超过剩余的等待时间，但至少 1 秒，最后一次读取也有机会完成。"""
    return max(1.0, min(REQUEST_TIMEOUT_SECONDS, deadline - time.monotonic()))


def _get(client: ApiClient, name: str, namespace: str | None) -> dict[str, Any]:
    try:
        return client.get_service(name, namespace)
    except ApiError as exc:
        raise _not_found_hint(exc, namespace) from exc


def _not_found_hint(exc: ApiError, namespace: str | None) -> CliError:
    """没有给出 ``--namespace`` 而资源不存在时，提示请求指的是不带命名空间的资源。"""
    if not (exc.not_found and namespace is None):
        return exc
    return CliError(
        f"{exc.message}\nno --namespace was given, so this looked for an {KIND} "
        "without a namespace",
        exc.exit_code,
    )


def _command(verb: str, name: str, namespace: str | None) -> str:
    option = "" if namespace is None else f" -n {namespace}"
    return f"forgemesh {verb} {KIND.lower()} {name}{option}"
