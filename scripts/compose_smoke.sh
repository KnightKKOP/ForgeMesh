#!/usr/bin/env bash
# 端到端冒烟：构建镜像，按 deploy/docker/docker-compose.yaml 启动控制平面与网关，
# 经网关创建并读取一个推理服务，等控制器回写 status，重启控制平面后确认数据仍在。
#
# 用法（任意目录）：scripts/compose_smoke.sh
# 依赖 docker（含 compose v2 插件）与 curl；占用 127.0.0.1:8080 与 127.0.0.1:8081。
set -euo pipefail

cd "$(dirname "$0")/.."
compose_file=deploy/docker/docker-compose.yaml
key_file=deploy/docker/api-keys.json
gateway=http://127.0.0.1:8081
compose=(docker compose -f "$compose_file" -p forgemesh-smoke)

if [[ -e "$key_file" ]]; then
  echo "ERROR: $key_file already exists; move it away before running the smoke test" >&2
  exit 1
fi

cleanup() {
  local status=$?
  if [[ $status -ne 0 ]]; then
    "${compose[@]}" logs --no-color || true
  fi
  "${compose[@]}" down --volumes --remove-orphans || true
  rm -f "$key_file"
  exit "$status"
}
trap cleanup EXIT

# check <说明> <期望状态码> <curl 参数...>；不回显参数，避免把 key 打进日志。
check() {
  local description=$1 expected=$2 actual
  shift 2
  actual=$(curl -s -o /dev/null -w '%{http_code}' "$@")
  if [[ "$actual" != "$expected" ]]; then
    echo "FAIL: $description: expected HTTP $expected, got $actual" >&2
    exit 1
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
  echo "FAIL: gateway /readyz did not return 200" >&2
  exit 1
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
service="$gateway/api/v1/inference-services"
body='{"kind": "InferenceService", "metadata": {"name": "smoke-svc", "namespace": "default"},
  "spec": {"model": {"reference": "models/llama"}, "runtime": {"provider": "vllm"},
  "resources": {"accelerator": {"type": "nvidia-gpu", "minMemoryGiB": 16}}}}'

check "request without a key is rejected" 401 "$service"
check "create through the gateway" 201 "${auth[@]}" -H "Content-Type: application/json" \
  -X POST -d "$body" "$service"
check "read through the gateway" 200 "${auth[@]}" "$service/smoke-svc?namespace=default"

# 仓库没有调度器实现，控制器把新服务协调为 Ready=False（SchedulerUnavailable）。
reconciled=""
for _ in $(seq 1 20); do
  if curl -sf "${auth[@]}" "$service/smoke-svc?namespace=default" | grep -q SchedulerUnavailable; then
    reconciled=yes
    break
  fi
  sleep 0.5
done
if [[ -z "$reconciled" ]]; then
  echo "FAIL: the controller did not write the status of smoke-svc" >&2
  exit 1
fi
echo "ok: controller wrote the status"

"${compose[@]}" restart control-plane
"${compose[@]}" up --detach --wait
wait_ready
check "resource survives a control plane restart" 200 "${auth[@]}" \
  "$service/smoke-svc?namespace=default"

echo "compose smoke test passed"
