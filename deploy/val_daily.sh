#!/usr/bin/env bash
# val_daily.sh · 腾讯云每日持仓估值推送
# 每只股票：现价 + 合理价区间(3年PE分位) + 你的持仓浮盈亏 + 操作
# 汇总后经韩国服务器 Hermes 分几条微信推送。
#
# 用法: ./val_daily.sh [--dry-run]
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$BASE_DIR/config"
LOG_DIR="$BASE_DIR/logs"
TICKERS_FILE="$CONFIG_DIR/tickers.yaml"
VENV="$BASE_DIR/.venv"
DATE=$(date +%Y-%m-%d)

NOTIFY_HOST="ubuntu@140.245.66.29"
NOTIFY_WEIXIN="weixin:o9cq804Yt2iNrHSsEikFteKhwPMY@im.wechat"
HERMES_BIN="/home/ubuntu/.local/bin/hermes"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ -f "$VENV/bin/activate" ]]; then
    source "$VENV/bin/activate"
fi
[[ -f "$TICKERS_FILE" ]] || { echo "[ERR] tickers.yaml 不存在" >&2; exit 1; }

echo "[$DATE] 估值分析开始..."

# ---- 生成完整报告（16 只逐只 val_card）----
python3 - "$TICKERS_FILE" "$BASE_DIR" "$LOG_DIR" "$DATE" <<'PYEOF'
import subprocess
import sys

tickers_file, base_dir, log_dir, date = sys.argv[1:5]
import yaml
from pathlib import Path

with open(tickers_file) as f:
    data = yaml.safe_load(f)

rows = []
for t in data.get("tickers", []):
    code = t["code"]
    name = t.get("name", "")
    shares = str(t.get("shares", 0) or 0)
    cost = str(t.get("cost", "") or "")
    r = subprocess.run(
        ["python3", f"{base_dir}/scripts/val_card.py", code, name, shares, cost],
        capture_output=True, text=True)
    out = "\n".join(
        l for l in r.stdout.splitlines()
        if l.strip() and "it/s" not in l
        and l.strip() not in ("login success!", "logout success!"))
    rows.append(out)

report = "\n\n".join(rows)
Path(f"{log_dir}/val-{date}.txt").write_text(report)
# 不 print(report) —— 调用方用 --dry-run + cat 文件预览，避免重复输出
PYEOF

echo "[$DATE] 估值完成"

if $DRY_RUN; then
    echo "===== dry-run 预览（不发微信）====="
    cat "$LOG_DIR/val-${DATE}.txt"
    exit 0
fi

# ---- 按行数拆成 ≤2 条推送 ----
TOTAL_LINES=$(wc -l < "$LOG_DIR/val-${DATE}.txt")
CHUNK=$(( (TOTAL_LINES + 1) / 2 ))

split -l "$CHUNK" -d -a 1 "$LOG_DIR/val-${DATE}.txt" "$LOG_DIR/val-chunk-"
for f in "$LOG_DIR"/val-chunk-?; do
    [[ -e "$f" ]] || continue
    echo "[$DATE] 推送一条 ($(wc -l < "$f") 行)..."
    cat "$f" | ssh "${SSH_OPTS[@]}" "$NOTIFY_HOST" \
        "export PATH=\"\$HOME/.local/bin:\$PATH\"; cat | $HERMES_BIN send --to '$NOTIFY_WEIXIN' --subject '📊 持仓估值 $DATE'" \
        >> "$LOG_DIR/hermes.log" 2>&1 || echo "[WARN] 推送失败" >&2
    sleep 35
    rm -f "$f"
done
echo "[$DATE] 推送完成"
