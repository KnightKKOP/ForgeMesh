# =============================================================================
# ForgeMesh 顶层 Makefile
# -----------------------------------------------------------------------------
# 兼容 macOS 自带的 GNU Make 3.81：
#   * 不使用 .ONESHELL（3.82+ 才有）；
#   * 多行 shell 逻辑写在单条 recipe 内，用 `\` 续行；
#   * 不使用 $(shell ...) 的高级特性。
#
# 常用目标：
#   make install   创建 .venv，安装全部子包（editable）+ 开发依赖
#   make test      运行全部测试（pytest）
#   make coverage  运行全部测试并检查覆盖率下限（与 CI 相同）
#   make audit     用 pip-audit 检查 .venv 中第三方依赖的已知漏洞（与 CI 相同）
#   make lint      ruff 静态检查与格式检查（与 CI 相同）
#   make fmt       ruff 自动修复 + 格式化
#   make clean     清理构建与缓存产物
# =============================================================================

PY      ?= python3
VENV    ?= .venv
VENV_PY := $(VENV)/bin/python
VENV_PIP:= $(VENV)/bin/pip

# pip-audit 装在单独的虚拟环境中，它自己的依赖不混进被审计的 .venv。
PIP_AUDIT_VERSION ?= 2.10.1
AUDIT_VENV        ?= build/pip-audit
# 尚无修复版本、或经评估不影响 ForgeMesh 的漏洞，写成 --ignore-vuln <ID>，并在这里注释
# 包、原因与移除条件（见 SECURITY.md 3.1）。
PIP_AUDIT_IGNORE  :=

.DEFAULT_GOAL := help

.PHONY: help install deps test test-unit test-contract coverage audit lint fmt clean

help:
	@echo "ForgeMesh 顶层命令："
	@echo "  make install        创建 .venv，安装全部子包（editable）+ 开发依赖"
	@echo "  make test           运行全部测试（pytest）"
	@echo "  make test-unit      仅运行单元测试"
	@echo "  make test-contract  仅运行契约测试"
	@echo "  make coverage       运行全部测试并检查覆盖率下限（与 CI 相同）"
	@echo "  make audit          用 pip-audit 检查 .venv 中第三方依赖的已知漏洞（与 CI 相同）"
	@echo "  make lint           ruff 静态检查与格式检查（与 CI 相同）"
	@echo "  make fmt            ruff 自动修复与格式化"
	@echo "  make clean          清理构建与缓存产物"

# 创建虚拟环境（若已被删除或缺失解释器）
$(VENV_PY):
	@echo ">> 创建虚拟环境 $(VENV)"
	$(PY) -m venv $(VENV)

# 安装：venv -> 升级打包基础 -> 开发依赖 -> 各子包 editable
#
# 注意（--no-deps）：子包之间通过 `forgemesh-*` 名称互相依赖（如 forgemesh-core、
# forgemesh-workflow-api），但它们都是本仓库内的本地包。若按目录字母序逐个做
# 依赖解析式 editable 安装，pip 会在兄弟包尚未安装时误向 PyPI 查询（例如先装
# packages/control-plane 时解析 forgemesh-core），因私有包不在 PyPI 而失败。故统一加
# --no-deps：
#   * 外部第三方依赖（pydantic）已由上面的 requirements-dev.txt 统一提供；
#   * 跨包依赖由本循环内的 editable 安装逐一登记，安装完成后即可互相 import。
install: $(VENV_PY)
	@echo ">> 升级 pip / setuptools / wheel"
	$(VENV_PIP) install -U pip setuptools wheel
	@echo ">> 安装开发依赖 (requirements-dev.txt)"
	$(VENV_PIP) install -r requirements-dev.txt
	@echo ">> 以 editable 模式安装各子包"
	@for p in packages/*/; do \
		if [ -f "$$p/pyproject.toml" ]; then \
			echo "-- pip install --no-deps -e $$p"; \
			$(VENV_PIP) install --no-deps -e "$$p" || exit 1; \
		else \
			echo "-- 跳过 $$p（无 pyproject.toml）"; \
		fi; \
	done
	@echo ">> 安装完成。运行 'make test' 验证。"

deps: install

test:
	$(VENV_PY) -m pytest

test-unit:
	$(VENV_PY) -m pytest tests/unit

test-contract:
	$(VENV_PY) -m pytest tests/contract

coverage:
	$(VENV_PY) -m pytest --cov

# 审计 make install 装进 $(VENV) 的依赖：PIPAPI_PYTHON_LOCATION 让 pip-audit 列出该解释器
# 环境中的包（路径不能解析符号链接，否则指向的是基础解释器）。本仓库的 forgemesh-* 包以
# editable 方式安装、不在 PyPI 上，跳过。
audit:
	@test -x $(VENV_PY) || { echo "ERROR: $(VENV_PY) 不存在，先运行 make install"; exit 1; }
	$(PY) -m venv $(AUDIT_VENV)
	$(AUDIT_VENV)/bin/pip install -q --disable-pip-version-check pip-audit==$(PIP_AUDIT_VERSION)
	PIPAPI_PYTHON_LOCATION=$(abspath $(VENV_PY)) $(AUDIT_VENV)/bin/pip-audit \
		--skip-editable --progress-spinner off $(PIP_AUDIT_IGNORE)

lint:
	$(VENV_PY) -m ruff check .
	$(VENV_PY) -m ruff format --check .

fmt:
	$(VENV_PY) -m ruff check --fix .
	$(VENV_PY) -m ruff format .

clean:
	@echo ">> 清理 Python 构建与缓存产物"
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.pytest_cache' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.ruff_cache' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '.mypy_cache' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name 'build' -prune -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name 'dist' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov .coverage coverage.xml 2>/dev/null || true
	@echo ">> 清理完成"