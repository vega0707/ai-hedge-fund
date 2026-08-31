#!/usr/bin/env bash
# ai-hedge-fund · 1C1G Ubuntu 部署脚本（复用 FreeLLMAPI + Hermes）
# 用法: sudo ./deploy.sh
set -euo pipefail

INSTALL_DIR="/opt/ai-hedge-fund"
VENV_DIR="$INSTALL_DIR/.venv"
CONFIG_DIR="$INSTALL_DIR/config"
LOG_DIR="$INSTALL_DIR/logs"
SCRIPTS_DIR="$INSTALL_DIR/scripts"

info()  { echo -e "\033[34m[INFO]\033[0m $*"; }
ok()    { echo -e "\033[32m[OK]\033[0m $*"; }
warn()  { echo -e "\033[33m[WARN]\033[0m $*"; }
err()   { echo -e "\033[31m[ERR]\033[0m $*" >&2; exit 1; }

# ---- 前置检查 ----
[[ $EUID -ne 0 ]] && err "请用 root 或 sudo 运行此脚本"
source /etc/os-release 2>/dev/null || true

TOTAL_MEM_MB=$(awk '/MemTotal/ {printf "%.0f", $2/1024}' /proc/meminfo)
info "系统: ${PRETTY_NAME:-unknown} | 内存: ${TOTAL_MEM_MB}MB | CPU: $(nproc)核"

# ============================================================
# Step 1: Swap（仅内存 < 2GB 时添加 2GB）
# ============================================================
if [[ $TOTAL_MEM_MB -lt 2000 ]] && [[ ! -f /swapfile ]]; then
    info "添加 2GB swap..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -w vm.swappiness=10
    grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
    ok "Swap 已启用 (2GB, swappiness=10)"
else
    [[ -f /swapfile ]] && info "Swap 已存在，跳过" || info "内存 ≥ 2GB，跳过 swap"
fi

# ============================================================
# Step 2: Python 3.12
# ============================================================
if command -v python3.12 &>/dev/null; then
    ok "Python $(python3.12 --version 2>&1) 已安装"
else
    info "安装 Python 3.12..."
    apt-get update -qq
    apt-get install -y -qq software-properties-common >/dev/null 2>&1 || true
    add-apt-repository -y ppa:deadsnakes/ppa >/dev/null 2>&1 || true
    apt-get update -qq
    apt-get install -y -qq python3.12 python3.12-venv python3.12-dev
    ok "Python $(python3.12 --version 2>&1) 安装完成"
fi

# ============================================================
# Step 3: 检查 FreeLLMAPI 和 Hermes
# ============================================================
FREEILLMAPI_URL="http://127.0.0.1:3001"

if curl -sf "$FREEILLMAPI_URL/health" &>/dev/null; then
    ok "FreeLLMAPI 已在运行 ($FREEILLMAPI_URL)"
else
    warn "FreeLLMAPI 未运行或无法访问"
    echo "  请确保 FreeLLMAPI 已安装并启动在端口 3001"
    echo "  参考: ~/freellmapi 或 systemd 服务"
    read -rp "继续部署？[y/N] " continue_deploy
    [[ "$continue_deploy" != "y" && "$continue_deploy" != "Y" ]] && exit 1
fi

if command -v hermes &>/dev/null || [[ -f /home/ubuntu/.local/bin/hermes ]]; then
    ok "Hermes 已安装"
    HERMES_BIN=$(command -v hermes 2>/dev/null || echo "/home/ubuntu/.local/bin/hermes")
else
    warn "Hermes 未找到"
    echo "  请确保 Hermes agent 已安装"
    read -rp "继续部署？[y/N] " continue_deploy
    [[ "$continue_deploy" != "y" && "$continue_deploy" != "Y" ]] && exit 1
    HERMES_BIN="/home/ubuntu/.local/bin/hermes"
fi

# ============================================================
# Step 4: 克隆 / 更新仓库
# ============================================================
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "仓库已存在，git pull..."
    cd "$INSTALL_DIR" && git pull --ff-only
else
    info "克隆到 $INSTALL_DIR..."
    git clone https://github.com/vega0707/ai-hedge-fund.git "$INSTALL_DIR"
fi

# ============================================================
# Step 5: 虚拟环境 + 精简依赖（1C1G 优化）
# ============================================================
[[ -d "$VENV_DIR/bin" ]] || python3.12 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q

info "安装精简依赖（跳过 TUI + 非必要 LLM provider，省 ~200MB）..."
pip install -q pandas numpy scipy pydantic pyyaml requests python-dotenv rich
pip install -q langchain-openai langchain-anthropic
pip install -q akshare baostock matplotlib
pip install -q -e . --no-deps   # 项目本体，不重复装依赖
ok "依赖安装完成"

# ============================================================
# Step 6: 配置目录 + 文件
# ============================================================
mkdir -p "$CONFIG_DIR" "$LOG_DIR" "$SCRIPTS_DIR"

# --- tickers.yaml（首次生成） ---
if [[ ! -f "$CONFIG_DIR/tickers.yaml" ]]; then
    cat > "$CONFIG_DIR/tickers.yaml" <<'YAML'
# 持仓列表 · 编辑此文件增删股票，次日自动生效
# code:  A 股 6 位代码，或港股 "0700.HK"
# name:  显示名称（微信消息里用）
# cost:  成本价（可选，填了才显示浮盈亏）
tickers:
  - { code: "300679", name: "电连技术",   cost: 74.249 }
  - { code: "300411", name: "金盾股份",   cost: 10.208 }
  - { code: "600582", name: "天地科技",   cost: 5.468  }
  - { code: "000915", name: "华特达因",   cost: 27.716 }
  - { code: "002271", name: "东方雨虹",   cost: 13.168 }
  - { code: "601669", name: "中国电建",   cost: 5.830  }
  - { code: "601668", name: "中国建筑",   cost: 4.705  }
  - { code: "601128", name: "常熟银行",   cost: 6.328  }
  - { code: "601006", name: "大秦铁路",   cost: 4.859  }
  # ETF 大师会 abstain（无基本面数据），如需跟踪取消注释：
  # - { code: "159851", name: "金融科技ETF华宝", cost: 0.703 }
  # - { code: "512880", name: "证券ETF国泰",     cost: 1.050 }
YAML
    ok "tickers.yaml 已生成"
fi

# ============================================================
# Step 7: LLM 配置（复用 FreeLLMAPI）
# ============================================================
LLM_ENV="$INSTALL_DIR/.env"
cat > "$LLM_ENV" <<ENV
# 复用服务器上的 FreeLLMAPI
HEDGE_FUND_LLM_MODEL=gpt-4o-mini
OPENAI_API_BASE=$FREEILLMAPI_URL/v1
OPENAI_API_KEY=freellmapi-reuse
AIHF_DATA_PROVIDER=akshare
ENV
ok "LLM 配置 → FreeLLMAPI ($FREEILLMAPI_URL/v1)"

# ============================================================
# Step 8: 复制运行脚本
# ============================================================
DEPLOY_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for script in run_daily.sh; do
    if [[ -f "$DEPLOY_SRC/$script" ]]; then
        cp "$DEPLOY_SRC/$script" "$SCRIPTS_DIR/"
        chmod +x "$SCRIPTS_DIR/$script"
    fi
done
ok "运行脚本已复制"

# ============================================================
# Step 9: Cron 任务
# ============================================================
CRON_LINE="30 14 * * 1-5 $SCRIPTS_DIR/run_daily.sh >> $LOG_DIR/cron.log 2>&1"
if crontab -l 2>/dev/null | grep -q "run_daily.sh"; then
    warn "Cron 任务已存在，跳过（修改请用 crontab -e）"
else
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    ok "Cron 已配置：周一到周五 14:30 运行"
    warn "建议改为 15:30（收盘后数据更完整）: crontab -e 改 30 14 → 30 15"
fi

# ============================================================
# Done
# ============================================================
echo ""
echo "============================================"
ok "部署完成！"
echo "============================================"
echo ""
echo "测试运行:"
echo "  $SCRIPTS_DIR/run_daily.sh --dry-run"
echo ""
echo "查看日志:"
echo "  tail -f $LOG_DIR/cron.log"
echo ""
echo "更新持仓: 编辑 $CONFIG_DIR/tickers.yaml（增删股票，次日生效）"
echo "调整时间: crontab -e（建议 15:30 收盘后跑）"
echo ""
echo "通知渠道: Hermes → 微信（通过 hermes send --to weixin）"
