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
# Step 6: LLM 配置（二选一）
# ============================================================
LLM_ENV="$INSTALL_DIR/.env"
echo ""
echo "  === LLM 配置 ==="
echo "  [1] auto(free) - 程小帮网关（需服务器能访问 xiaobang.ctripcorp.com）"
echo "  [2] 自定义 key - DeepSeek / OpenAI / Anthropic（推荐公网服务器）"
echo ""
read -rp "选择 [1/2, 默认 2]: " llm_choice
llm_choice="${llm_choice:-2}"

if [[ "$llm_choice" == "1" ]]; then
    cat > "$LLM_ENV" <<'ENV'
HEDGE_FUND_LLM_MODEL=auto(free)
OPENAI_API_BASE=http://xiaobang.ctripcorp.com/chengxiaobang/coding-plan/v1
OPENAI_API_KEY=<从钥匙串获取填入>
AIHF_DATA_PROVIDER=akshare
ENV
    warn "请编辑 $LLM_ENV 填入 OPENAI_API_KEY"
else
    echo ""
    echo "  推荐 DeepSeek（国内直连，0.1 元/百万 token，10 只股票/天 ≈ ¥0.02）"
    echo ""
    read -rp "Provider [deepseek/openai/anthropic, 默认 deepseek]: " provider
    provider="${provider:-deepseek}"
    read -rp "Model [默认 deepseek-chat]: " model; model="${model:-deepseek-chat}"
    read -rsp "API Key: " api_key; echo ""
    read -rp "Base URL [留空用默认]: " base_url; base_url="${base_url:-}"

    cat > "$LLM_ENV" <<ENV
HEDGE_FUND_LLM_MODEL=$model
AIHF_DATA_PROVIDER=akshare
ENV

    case "$provider" in
        deepseek)
            echo "DEEPSEEK_API_KEY=$api_key" >> "$LLM_ENV"
            [[ -n "$base_url" ]] && echo "DEEPSEEK_API_BASE=$base_url" >> "$LLM_ENV"
            ;;
        openai)
            echo "OPENAI_API_KEY=$api_key" >> "$LLM_ENV"
            [[ -n "$base_url" ]] && echo "OPENAI_API_BASE=$base_url" >> "$LLM_ENV"
            ;;
        anthropic)
            echo "ANTHROPIC_API_KEY=$api_key" >> "$LLM_ENV"
            [[ -n "$base_url" ]] && echo "ANTHROPIC_BASE_URL=$base_url" >> "$LLM_ENV"
            ;;
    esac
    ok "LLM 配置已写入 $LLM_ENV"
fi

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
