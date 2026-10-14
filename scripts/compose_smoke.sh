#!/usr/bin/env bash
# 端到端冒烟：构建镜像，以 deploy/docker/docker-compose.yaml 叠加
# deploy/docker/docker-compose.fake-runtime.yaml（一块设备的加速器清单 + 假启动器，不需要 GPU）
# 启动控制平面与网关，经网关走完推理服务的闭环：
#   创建 → Ready（status.endpoints 指向假实例）→ 经网关流式对话（SSE，以 [DONE] 结束）→
#   删除 → 资源消失、实例停止 → 同名重新创建（只有一块设备，可见分配已释放）→ 再次对话 →
#   PUT 修改 spec → Updating → 以新实例回到 Ready（旧实例已停止）→ 再次对话 →
#   假实例退出（POST /fake/exit）→ Degraded（InstanceLost）→ 重新部署，以新实例回到 Ready →
#   再次对话 → 重启控制平面：资源仍在，实例随旧进程结束，服务转为 Degraded（InstanceLost）
#   后重新部署，以新实例回到 Ready → 再次对话 → 删除。
#
# 每一步都有时限，整个闭环只等待状态变化，不依赖网关在某一刻返回的状态码。
#
# 用法（任意目录）：scripts/compose_smoke.sh
# 依赖 docker（含 compose v2 插件）、curl 与 jq；占用 127.0.0.1:8080 与 127.0.0.1:8081。
set -euo pipefail

cd "$(dirname "$0")/.."
key_file=deploy/docker/api-keys.json
gateway=http://127.0.0.1:8081
compose=(docker compose -f deploy/docker/docker-compose.yaml
  -f deploy/docker/docker-compose.fake-runtime.yaml -p forgemesh-smoke)

if [[ -e "$key_file" ]]; then
  echo "ERROR: $key_file already exists; move it away before running the smoke test" >&2
  exit 1
fi
work=$(mktemp -d)

cleanup() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    "${compose[@]}" logs --no-color || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  rm -f "$key_file"
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# check <说明> <期望状态码> <curl 参数...>；不回显参数，避免把 key 打进日志。
check() {
  local description=$1 expected=$2 actual
  shift 2
  actual=$(curl -s --max-time 30 -o /dev/null -w '%{http_code}' "$@")
  if [[ "$actual" != "$expected" ]]; then
    fail "$description: expected HTTP $expected, got $actual"
  fi
  echo "ok: $description (HTTP $actual)"
}

wait_ready() {
  for _ in $(seq 1 30); do
    if curl -sf "$gateway/readyz" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  fail "gateway /readyz did not return 200"
}

# fetch：读取 smoke-svc 写入 $resource，输出 HTTP 状态码。
fetch() {
  curl -s --max-time 30 -o "$resource" -w '%{http_code}' "${auth[@]}" "$service" || true
}

# wait_phase <phase>：等 smoke-svc 的 status.phase 变为 <phase>（最多 60 秒），资源留在 $resource。
# 每 0.25 秒读取一次：协调器每 2 秒查询一次部署运行，Updating 与重新部署中的 Degraded 至少持续
# 这么久，不会错过。
wait_phase() {
  local phase=$1 current=""
  for _ in $(seq 1 240); do
    if [[ "$(fetch)" == 200 ]]; then
      current=$(jq -r '.status.phase // ""' "$resource")
      if [[ "$current" == "$phase" ]]; then
        echo "ok: smoke-svc reached phase $phase"
        return 0
      fi
      if [[ "$current" == Failed ]]; then
        break
      fi
    fi
    sleep 0.25
  done
  jq '.status' "$resource" >&2 || true
  fail "smoke-svc did not reach phase $phase (last phase: ${current:-none})"
}

wait_gone() {
  for _ in $(seq 1 120); do
    if [[ "$(fetch)" == 404 ]]; then
      echo "ok: smoke-svc is gone"
      return 0
    fi
    sleep 0.5
  done
  fail "smoke-svc was not deleted"
}

# expect <说明> <jq 条件> [jq 参数...]：$resource 中的资源满足条件。
expect() {
  if ! jq -e "${@:3}" "$2" "$resource" >/dev/null; then
    jq '.status' "$resource" >&2 || true
    fail "$1"
  fi
  echo "ok: $1"
}

# chat：经网关发出一次流式对话，响应头写入 $headers、响应体写入 $stream，输出 HTTP 状态码。
chat() {
  curl -s -N --max-time 30 -D "$headers" -o "$stream" -w '%{http_code}' "${auth[@]}" \
    -H "Content-Type: application/json" -X POST -d "$chat_body" "$chat_url" || true
}

# reply：$stream 中各 SSE 事件拼出的回复；最后一个事件不是 [DONE] 时失败。
reply() {
  local events
  events=$(sed -n 's/^data: //p' "$stream")
  [[ "$(tail -n 1 <<<"$events")" == "[DONE]" ]] || return 1
  grep -v '^\[DONE\]$' <<<"$events" | jq -rj '.choices[0].delta.content // empty'
}

# expect_reply <实例 id>：网关把流式对话转发到这个实例。网关缓存服务的实例列表 5 秒，
# 删除并重新创建之后可能先转发到已停止的旧实例，因此重试。
expect_reply() {
  local want="Hello from the ForgeMesh fake instance $1." got="" code=""
  for _ in $(seq 1 30); do
    code=$(chat)
    if [[ "$code" == 200 ]] && got=$(reply) && [[ "$got" == "$want" ]]; then
      grep -qi '^content-type: text/event-stream' "$headers" \
        || fail "the chat response is not an event stream"
      echo "ok: streaming chat through the gateway reached $1"
      return 0
    fi
    sleep 1
  done
  fail "streaming chat did not reach $1 (last HTTP $code, reply: ${got:-none})"
}

# port_closed <端口>：控制平面容器内没有进程在该端口上监听。
port_closed() {
  "${compose[@]}" exec -T control-plane python - "$1" <<'EOF'
import socket
import sys

try:
    socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=2).close()
except ConnectionRefusedError:
    sys.exit(0)
sys.exit(1)
EOF
}

# exit_instance <端口>：经 POST /fake/exit（只有假实例有这个路由）让该端口上的实例像进程退出
# 一样结束，输出 HTTP 状态码。实例端口不发布到宿主机，因此在控制平面容器内发出请求。
exit_instance() {
  "${compose[@]}" exec -T control-plane python - "$1" <<'EOF'
import http.client
import sys

connection = http.client.HTTPConnection("127.0.0.1", int(sys.argv[1]), timeout=10)
connection.request("POST", "/fake/exit")
print(connection.getresponse().status)
EOF
}

# endpoint_port：$resource 中第一个 endpoint 的端口。
endpoint_port() {
  jq -r '.status.endpoints[0].url | split(":") | last' "$resource"
}

"${compose[@]}" build

# keygen 输出的第 2 行是 key，最后一行是 key 文件条目。
keygen=$(docker run --rm forgemesh/control-plane:0.0.1 \
  python -m forgemesh.controlplane.keygen --tenant smoke --subject compose-smoke)
key=$(sed -n 2p <<<"$keygen")
printf '{"keys": [%s]}\n' "$(tail -n 1 <<<"$keygen")" > "$key_file"
chmod 0644 "$key_file"

"${compose[@]}" up --detach --wait
wait_ready
echo "ok: gateway ready"

auth=(-H "Authorization: Bearer $key")
services="$gateway/api/v1/inference-services"
service="$services/smoke-svc?namespace=default"
chat_url="$gateway/api/v1/namespaces/default/inference/smoke-svc/chat"
resource="$work/resource.json"
update="$work/update.json"
headers="$work/headers.txt"
stream="$work/stream.txt"
body='{"kind": "InferenceService", "metadata": {"name": "smoke-svc", "namespace": "default"},
  "spec": {"model": {"reference": "models/llama"}, "runtime": {"provider": "vllm"},
  "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}}}}'
chat_body='{"model": "models/llama", "messages": [{"role": "user", "content": "Hello"}],
  "stream": true}'

check "request without a key is rejected" 401 "$services"
check "chat without a key is rejected" 401 -H "Content-Type: application/json" \
  -X POST -d "$chat_body" "$chat_url"
check "create through the gateway" 201 "${auth[@]}" -H "Content-Type: application/json" \
  -X POST -d "$body" "$services"
check "read through the gateway" 200 "${auth[@]}" "$service"

wait_phase Ready
expect "Ready=True (Reconciled)" \
  '.status.conditions[] | select(.type == "Ready") | .status == "True" and .reason == "Reconciled"'
expect "one ready openai-chat endpoint on the advertised host" \
  '.status.endpoints | length == 1 and (.[0] | .ready and .protocol == "openai-chat"
    and (.instanceId | startswith("fake-")) and (.url | test("^http://control-plane:[0-9]+$")))'
first_instance=$(jq -r '.status.endpoints[0].instanceId' "$resource")
first_port=$(endpoint_port)
expect_reply "$first_instance"
"${compose[@]}" exec -T control-plane test -f /var/lib/forgemesh/forgemesh.workflows.db \
  || fail "the workflow store is not next to the registry"
echo "ok: the workflow store lives next to the registry"

check "delete through the gateway" 202 "${auth[@]}" -X DELETE "$service"
wait_gone
port_closed "$first_port" \
  || fail "the deleted instance $first_instance still listens on port $first_port"
echo "ok: the deleted instance stopped listening"

# 清单只有一块设备：重新创建的服务能部署，说明删除释放了分配。
check "re-create with the same name" 201 "${auth[@]}" -H "Content-Type: application/json" \
  -X POST -d "$body" "$services"
wait_phase Ready
second_instance=$(jq -r '.status.endpoints[0].instanceId' "$resource")
second_port=$(endpoint_port)
[[ "$second_instance" != "$first_instance" ]] \
  || fail "the re-created service reports the old instance $first_instance"
echo "ok: the re-created service runs a new instance"
expect_reply "$second_instance"

# 更新：GET 的响应改一个 spec 字段后整体提交，metadata.resourceVersion 留在文档中作前置条件。
# 加速器申请不变，新的 generation 复用同一分配；先停后起：Updating 中旧实例停止、新实例部署，
# 之后服务以新实例回到 Ready。
[[ "$(fetch)" == 200 ]] || fail "could not read smoke-svc before the update"
jq -c '.spec.runtime.version = "0.6.3"' "$resource" > "$update"
check "update the spec through the gateway" 200 "${auth[@]}" \
  -H "Content-Type: application/json" -X PUT --data-binary @"$update" "$service"
wait_phase Updating
expect "Ready=False (Updating) and no ready endpoint while generation 2 deploys" \
  '.metadata.generation == 2 and (.status.endpoints | all(.ready | not))
    and (.status.conditions[] | select(.type == "Ready")
      | .status == "False" and .reason == "Updating")'
wait_phase Ready
expect "generation 2 is deployed" \
  '.metadata.generation == 2 and .status.observedGeneration == 2
    and .spec.runtime.version == "0.6.3"'
third_instance=$(jq -r '.status.endpoints[0].instanceId' "$resource")
third_port=$(endpoint_port)
[[ "$third_instance" != "$second_instance" ]] \
  || fail "the updated service reports the old instance $second_instance"
port_closed "$second_port" \
  || fail "the replaced instance $second_instance still listens on port $second_port"
echo "ok: the update replaced the instance"
expect_reply "$third_instance"

# 实例在运行中丢失：控制器每秒核对一次实例（见 docker-compose.fake-runtime.yaml），服务转为
# Degraded（InstanceLost），随后以同一代 spec 重新部署，以新实例回到 Ready。
[[ "$(exit_instance "$third_port")" == 202 ]] \
  || fail "the fake instance $third_instance did not exit"
echo "ok: the fake instance $third_instance exited"
wait_phase Degraded
expect "Ready=False (InstanceLost) and no ready endpoint" \
  '(.status.endpoints | all(.ready | not)) and (.status.conditions[] | select(.type == "Ready")
    | .status == "False" and .reason == "InstanceLost")'
logs=$("${compose[@]}" logs --no-color control-plane)
lost="service degraded, its instance is gone: instance $third_instance at"
lost+=" http://control-plane:$third_port is not running any more"
grep -qF "$lost" <<<"$logs" || fail "the control plane did not report the lost instance"
echo "ok: the instance check reported the lost instance"
wait_phase Ready
fourth_instance=$(jq -r '.status.endpoints[0].instanceId' "$resource")
fourth_port=$(endpoint_port)
[[ "$fourth_instance" != "$third_instance" ]] \
  || fail "the service still reports the lost instance $third_instance"
echo "ok: the service was redeployed with a new instance"
expect_reply "$fourth_instance"

# 假实例随控制平面进程结束：启动时服务转为 Degraded（InstanceLost，日志记录丢失的实例），
# 控制器随后以同一分配重新部署，服务以新实例回到 Ready。
"${compose[@]}" restart control-plane
"${compose[@]}" up --detach --wait
wait_ready
check "resource survives a control plane restart" 200 "${auth[@]}" "$service"
logs=$("${compose[@]}" logs --no-color control-plane)
lost="service degraded, its instance is gone: instance $fourth_instance at"
lost+=" http://control-plane:$fourth_port did not survive the control plane restart"
grep -qF "$lost" <<<"$logs" || fail "the restart did not report the lost instance"
echo "ok: the restart reported the lost instance"
wait_phase Ready
fifth_instance=$(jq -r '.status.endpoints[0].instanceId' "$resource")
[[ "$fifth_instance" != "$fourth_instance" ]] \
  || fail "the service still reports the lost instance $fourth_instance"
echo "ok: the service was redeployed with a new instance after the restart"
expect_reply "$fifth_instance"
check "delete the redeployed service" 202 "${auth[@]}" -X DELETE "$service"
wait_gone

echo "compose smoke test passed"
