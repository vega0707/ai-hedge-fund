#!/usr/bin/env bash
# ai-hedge-fund · 1C1G Ubuntu 部署脚本
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
[[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]] && warn "非 Ubuntu/Debian，部分命令可能不兼容"

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
# Step 3: 克隆 / 更新仓库
# ============================================================
if [[ -d "$INSTALL_DIR/.git" ]]; then
    info "仓库已存在，git pull..."
    cd "$INSTALL_DIR" && git pull --ff-only
else
    info "克隆到 $INSTALL_DIR..."
    git clone https://github.com/vega0707/ai-hedge-fund.git "$INSTALL_DIR"
fi

# ============================================================
# Step 4: 虚拟环境 + 精简依赖（1C1G 优化）
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
# Step 5: 配置目录 + 文件
# ============================================================
mkdir -p "$CONFIG_DIR" "$LOG_DIR" "$SCRIPTS_DIR"

# --- tickers.yaml（首次生成） ---
if [[ ! -f "$CONFIG_DIR/tickers.yaml" ]]; then
    cat > "$CONFIG_DIR/tickers.yaml" <<'YAML'
# 持仓列表 · 编辑此文件增删股票，次日自动生效
# code:  A 股 6 位代码，或港股 "0700.HK"
# name:  显示名称（邮件里用）
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

# --- smtp.yaml.example ---
if [[ ! -f "$CONFIG_DIR/smtp.yaml" ]]; then
    cat > "$CONFIG_DIR/smtp.yaml.example" <<'YAML'
# SMTP 配置 · 复制为 smtp.yaml 后填写
# QQ 邮箱: 在 QQ 邮箱设置里开启 SMTP + 生成"授权码"（不是 QQ 密码）
# 163 邮箱: 同样需要授权码
# Gmail:    需要 App Password
smtp:
  host: smtp.qq.com           # QQ: smtp.qq.com | 163: smtp.163.com | Gmail: smtp.gmail.com
  port: 465                   # SSL: 465 | TLS: 587
  use_ssl: true               # 465 → true, 587 → false
  user: your@qq.com           # 登录账号
  password: your-auth-code    # ← 应用授权码，不是登录密码！
  from: your@qq.com           # 发件人显示
  to: recipient@example.com   # 收件人（多收件人逗号分隔）
YAML
    warn "请复制并编辑 $CONFIG_DIR/smtp.yaml"
fi

# ============================================================
# Step 6: FreeLLMAPI 安装 + LLM 配置
# ============================================================
LLM_ENV="$INSTALL_DIR/.env"

# ---- 6a: 安装 Docker（如果没有） ----
if ! command -v docker &>/dev/null; then
    info "安装 Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker
    systemctl start docker
    ok "Docker 安装完成"
else
    ok "Docker 已安装"
fi

# ---- 6b: 运行 FreeLLMAPI 容器 ----
if docker ps --format '{{.Names}}' | grep -q '^freellmapi

# ============================================================
# Step 7: 复制运行脚本到安装目录
# ============================================================
DEPLOY_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for script in run_daily.sh push_email.py; do
    if [[ -f "$DEPLOY_SRC/$script" ]]; then
        cp "$DEPLOY_SRC/$script" "$SCRIPTS_DIR/"
        chmod +x "$SCRIPTS_DIR/$script"
    fi
done
ok "运行脚本已复制"

# ============================================================
# Step 8: Cron 任务
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
echo "剩余配置:"
echo "  1. 编辑 $CONFIG_DIR/smtp.yaml 填写邮箱（必须）"
echo "  2. 手动测试: $SCRIPTS_DIR/run_daily.sh --dry-run"
echo "  3. 查看日志: tail -f $LOG_DIR/cron.log"
echo ""
echo "更新持仓: 编辑 $CONFIG_DIR/tickers.yaml（增删股票，次日生效）"
echo "调整时间: crontab -e（建议 15:30 收盘后跑）"
; then
    ok "FreeLLMAPI 容器已在运行"
else
    info "拉取并启动 FreeLLMAPI（聚合 16+ 免费 LLM）..."
    docker run -d \
        --name freellmapi \
        --restart unless-stopped \
        -p 3001:3001 \
        -e ENCRYPTION_KEY=$(openssl rand -hex 32) \
        ghcr.io/tashfeenahmed/freellmapi:latest
    ok "FreeLLMAPI 已启动 → http://localhost:3001"
    echo "  Web UI: http://<服务器 IP>:3001"
    echo "  首次访问请设置管理员密码并配置免费提供商（Google/Groq/Cerebras 等）"
fi

# ---- 6c: 配置 ai-hedge-fund 使用 FreeLLMAPI ----
cat > "$LLM_ENV" <<'ENV'
# FreeLLMAPI（本地聚合免费 LLM）
HEDGE_FUND_LLM_MODEL=gpt-4o-mini
OPENAI_API_BASE=http://localhost:3001/v1
OPENAI_API_KEY=sk-freellmapi
AIHF_DATA_PROVIDER=akshare
ENV
ok "LLM 已配置 → FreeLLMAPI (localhost:3001)"
echo ""
echo "  重要：首次使用需访问 http://<服务器 IP>:3001 配置免费提供商"
echo "  推荐启用: Google Gemini (免费)、Groq (免费)、Cerebras (免费)"
echo "  配置完成后，ai-hedge-fund 会自动使用这些免费模型"

# ============================================================
# Step 7: 复制运行脚本到安装目录
# ============================================================
DEPLOY_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for script in run_daily.sh push_email.py; do
    if [[ -f "$DEPLOY_SRC/$script" ]]; then
        cp "$DEPLOY_SRC/$script" "$SCRIPTS_DIR/"
        chmod +x "$SCRIPTS_DIR/$script"
    fi
done
ok "运行脚本已复制"

# ============================================================
# Step 8: Cron 任务
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
echo "剩余配置:"
echo "  1. 编辑 $CONFIG_DIR/smtp.yaml 填写邮箱（必须）"
echo "  2. 手动测试: $SCRIPTS_DIR/run_daily.sh --dry-run"
echo "  3. 查看日志: tail -f $LOG_DIR/cron.log"
echo ""
echo "更新持仓: 编辑 $CONFIG_DIR/tickers.yaml（增删股票，次日生效）"
echo "调整时间: crontab -e（建议 15:30 收盘后跑）"
