#!/usr/bin/env bash
# guigui_invest 环境初始化脚本
# 用法: bash init.sh [--force-install]

set -euo pipefail
cd "$(dirname "$0")"

FORCE_INSTALL=0
[[ "${1:-}" == "--force-install" ]] && FORCE_INSTALL=1

echo "=== guigui_invest 初始化 ==="

# 1. 查找 Python >= 3.10
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        if "$cmd" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PYTHON="$cmd"
            break
        fi
    fi
done
if [[ -z "$PYTHON" ]]; then
    echo "❌ 未找到 Python >= 3.10，请先安装"
    exit 1
fi
echo "✓ Python: $($PYTHON --version)"

# 2. 创建 venv
if [[ ! -d .venv ]]; then
    echo "创建虚拟环境 .venv ..."
    "$PYTHON" -m venv .venv
fi

# 3. 安装依赖
if [[ ! -f .venv/.deps_installed ]] || [[ $FORCE_INSTALL -eq 1 ]]; then
    echo "安装依赖 ..."
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
    touch .venv/.deps_installed
    echo "✓ 依赖已安装"
else
    echo "✓ 依赖已就绪（如需重装: bash init.sh --force-install）"
fi

# 4. 环境变量提示（AKShare 免费无 token，.env 可选）
if [[ ! -f .env ]]; then
    cp .env.sample .env
    echo "✓ 已创建 .env（AKShare 免费数据源，无需 Token）"
fi

# 5. 运行测试验证
echo "运行测试 ..."
if .venv/bin/python -m pytest tests/ -q 2>&1 | tail -5; then
    echo "✓ 测试通过"
else
    echo "⚠️  部分测试未通过，请检查上方输出"
fi

echo ""
echo "=== 初始化完成 ==="
echo "用法（在 OpenCode + GLM 环境中）:"
echo "  /business-analysis 600887   # 定性商业分析"
echo "  /valuation 600887           # 独立估值分析"
echo "  /turtle-analysis 600887     # 完整龟龟策略"
