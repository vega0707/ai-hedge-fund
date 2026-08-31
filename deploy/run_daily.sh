#!/usr/bin/env bash
# run_daily.sh · 每日持仓分析 + 邮件推送
# 用法: ./run_daily.sh [--dry-run]
# dry-run: 只跑分析不发邮件，结果输出到 stdout
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$BASE_DIR/config"
LOG_DIR="$BASE_DIR/logs"
TICKERS_FILE="$CONFIG_DIR/tickers.yaml"
SMTP_FILE="$CONFIG_DIR/smtp.yaml"
MANDATE="$BASE_DIR/fund/china-daily.yaml"
VENV="$BASE_DIR/.venv"
DATE=$(date +%Y-%m-%d)
JSON_OUT="$LOG_DIR/report-${DATE}.json"

# ---- 加载虚拟环境 ----
if [[ -f "$VENV/bin/activate" ]]; then
    source "$VENV/bin/activate"
else
    echo "[ERR] 虚拟环境不存在: $VENV" >&2
    exit 1
fi

# ---- 加载 .env ----
if [[ -f "$BASE_DIR/.env" ]]; then
    set -a; source "$BASE_DIR/.env"; set +a
fi

# ---- 检查前置 ----
[[ -f "$TICKERS_FILE" ]] || { echo "[ERR] tickers.yaml 不存在: $TICKERS_FILE" >&2; exit 1; }

# dry-run 模式
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ---- 构建 ticker 列表 ----
TICKERS=$(python3 -c "
import yaml
with open('$TICKERS_FILE') as f:
    data = yaml.safe_load(f)
print(','.join(t['code'] for t in data.get('tickers', [])))
")

if [[ -z "$TICKERS" ]]; then
    echo "[ERR] tickers.yaml 中没有股票" >&2
    exit 1
fi

echo "[$DATE] 分析 $TICKERS ..."

# ---- 运行分析 ----
mkdir -p "$LOG_DIR"

if $DRY_RUN; then
    python3 -m hedge_fund.run "$MANDATE" --tickers "$TICKERS"
else
    python3 -m hedge_fund.run "$MANDATE" --tickers "$TICKERS" --out "$JSON_OUT"
    echo "[$DATE] 分析完成 → $JSON_OUT"

    # ---- 发送邮件 ----
    if [[ -f "$SMTP_FILE" ]] && [[ -f "$JSON_OUT" ]]; then
        python3 "$BASE_DIR/scripts/push_email.py" "$JSON_OUT" "$DATE"
        echo "[$DATE] 邮件已发送"
    else
        [[ ! -f "$SMTP_FILE" ]] && echo "[WARN] smtp.yaml 不存在，跳过邮件" >&2
        [[ ! -f "$JSON_OUT" ]] && echo "[WARN] 分析结果不存在，跳过邮件" >&2
    fi
fi

# ---- 清理旧日志（保留 30 天） ----
find "$LOG_DIR" -name "report-*.json" -mtime +30 -delete 2>/dev/null || true
