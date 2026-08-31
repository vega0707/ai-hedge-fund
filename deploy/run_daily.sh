#!/usr/bin/env bash
# run_daily.sh · 每日持仓分析 + Hermes 微信推送
# 用法: ./run_daily.sh [--dry-run]
# dry-run: 只跑分析不发通知，结果输出到 stdout
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$BASE_DIR/config"
LOG_DIR="$BASE_DIR/logs"
TICKERS_FILE="$CONFIG_DIR/tickers.yaml"
MANDATE="$BASE_DIR/fund/china.yaml"
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

    # ---- 通过 Hermes 发送微信通知 ----
    if [[ -f "$JSON_OUT" ]]; then
        # 查找 hermes 命令
        HERMES_BIN=$(command -v hermes 2>/dev/null || echo "/home/ubuntu/.local/bin/hermes")
        
        if [[ -x "$HERMES_BIN" ]]; then
            # 生成简洁的文本摘要
            SUMMARY=$(python3 <<'PYEOF'
import json
import sys
from pathlib import Path

json_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("$JSON_OUT")
if not json_path.exists():
    print("分析结果不存在")
    sys.exit(0)

with open(json_path) as f:
    data = json.load(f)

lines = []
for strat in data.get("strategies", []):
    lines.append(f"  策略：{strat.get('name', 'unknown')}")
    for sig in strat.get("signals", []):
        ticker = sig.get("ticker", "?")
        model = sig.get("model_name", "?")
        value = sig.get("value", 0)
        reasoning = sig.get("reasoning", "")[:80]
        emoji = "" if value > 0.3 else "🔴" if value < -0.3 else "⚪"
        lines.append(f"    {emoji} {ticker} · {model}: {value:+.2f} · {reasoning}")

print("\n".join(lines))
PYEOF
            "$JSON_OUT")

            # 发送微信消息
            echo "[$DATE] 通过 Hermes 发送微信通知..."
            echo "$SUMMARY" | "$HERMES_BIN" send --to weixin \
                --subject "📊 持仓日报 · $DATE" \
                2>&1 | tee -a "$LOG_DIR/hermes.log"
            
            echo "[$DATE] 通知已发送"
        else
            echo "[WARN] hermes 命令不可用，跳过通知" >&2
        fi
    else
        echo "[WARN] 分析结果不存在，跳过通知" >&2
    fi
fi

# ---- 清理旧日志（保留 30 天） ----
find "$LOG_DIR" -name "report-*.json" -mtime +30 -delete 2>/dev/null || true
