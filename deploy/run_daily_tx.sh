#!/usr/bin/env bash
# run_daily_tx.sh · 腾讯云每日持仓分析 + 韩国服务器 Hermes 微信推送
#
# 为什么分开两台机器：
#   - 分析（本机，腾讯云境内）：akshare/baostock/东财数据源在国内可达且快
#   - 通知（140.245.66.29，韩国 Oracle）：Hermes gateway + 微信 iLink 已配置，
#     本机通过 SSH 调它的 hermes send --to weixin
#
# 用法: ./run_daily_tx.sh [--dry-run]
#   --dry-run: 只分析不通知，每个 ticker 的 JSON 留在 logs/
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_DIR="$BASE_DIR/config"
LOG_DIR="$BASE_DIR/logs"
TICKERS_FILE="$CONFIG_DIR/tickers.yaml"
MANDATE="$BASE_DIR/fund/china.yaml"
VENV="$BASE_DIR/.venv"
DATE=$(date +%Y-%m-%d)

# 通知目标机器（韩国服务器，装有 Hermes + 微信）
NOTIFY_HOST="ubuntu@140.245.66.29"
NOTIFY_WEIXIN="weixin:o9cq804Yt2iNrHSsEikFteKhwPMY@im.wechat"
HERMES_BIN="/home/ubuntu/.local/bin/hermes"
SSH_OPTS=(-o ConnectTimeout=10 -o BatchMode=yes)

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

# ---- 加载虚拟环境 + .env ----
if [[ -f "$VENV/bin/activate" ]]; then
    source "$VENV/bin/activate"
else
    echo "[ERR] 虚拟环境不存在: $VENV" >&2
    exit 1
fi
if [[ -f "$BASE_DIR/.env" ]]; then
    set -a; source "$BASE_DIR/.env"; set +a
fi

[[ -f "$TICKERS_FILE" ]] || { echo "[ERR] tickers.yaml 不存在: $TICKERS_FILE" >&2; exit 1; }

# ---- 构建 ticker 列表（code + 中文名） ----
declare -a CODES NAMES
while IFS='|' read -r code name; do
    [[ -z "$code" ]] && continue
    CODES+=("$code")
    NAMES+=("$name")
done < <(python3 -c "
import yaml
with open('$TICKERS_FILE') as f:
    data = yaml.safe_load(f)
for t in data.get('tickers', []):
    print(f\"{t['code']}|{t.get('name','')}\")
")

if [[ ${#CODES[@]} -eq 0 ]]; then
    echo "[ERR] tickers.yaml 中没有股票" >&2
    exit 1
fi

echo "[$DATE] 共 ${#CODES[@]} 只股票，逐个分析..."

# 通知函数：把文本经 SSH 送到韩国服务器的 Hermes → 微信
notify_weixin() {
    local subject="$1"
    local body="$2"
    if $DRY_RUN; then
        echo "[DRY-RUN] 跳过通知: $subject"
        return 0
    fi
    # body 经远端 stdin 管道进 hermes send（远端 cat 把 stdin 喂给 hermes）
    echo "$body" | ssh "${SSH_OPTS[@]}" "$NOTIFY_HOST" \
        "export PATH=\"\$HOME/.local/bin:\$PATH\"; cat | $HERMES_BIN send --to '$NOTIFY_WEIXIN' --subject '$subject'" \
        2>&1 | tee -a "$LOG_DIR/hermes.log" || {
            echo "[WARN] 微信通知失败 (SSH/Hermes): $subject" >&2
        }
}

# 每个 ticker 单独分析并推送
for i in "${!CODES[@]}"; do
    code="${CODES[$i]}"
    name="${NAMES[$i]:-$code}"
    JSON_OUT="$LOG_DIR/report-${code}-${DATE}.json"
    echo "[$DATE] ($((i+1))/${#CODES[@]}) 分析 $code $name ..."

    python3 -m hedge_fund.run "$MANDATE" --tickers "$code" --out "$JSON_OUT" \
        2>&1 | grep -viE "it/s\]|^\s*$" >> "$LOG_DIR/run-${DATE}.log" || true
    echo "[$DATE] $code 分析完成"

    # 组装可读摘要
    SUMMARY=$(python3 -c "
import json
from pathlib import Path
p = Path('$JSON_OUT')
if not p.exists():
    print('分析结果不存在'); raise SystemExit
data = json.loads(p.read_text())
lines = [f\"代码 $code ｜ $name · \${data.get('marks', {}).get('$code', '?')}\"]
for strat in data.get('strategies', []):
    w = strat.get('weights', {}).get('$code', 0)
    arrow = '🟢 建议' if w > 0 else ('🔴 回避' if w < 0 else '⚪ 中性')
    lines.append(f\"  \${arrow} \${strat.get('name','?')}\")
    for sig in strat.get('signals', []):
        if sig.get('ticker') != '$code': continue
        v = sig.get('value', 0)
        e = '🟢' if v > 0.3 else ('🔴' if v < -0.3 else '⚪')
        r = (sig.get('reasoning') or '').replace(chr(10),' ')[:90]
        lines.append(f\"    \${e} \${sig.get('model_name','?')} \${v:+.2f} \${r}\")
print(chr(10).join(lines))
" 2>/dev/null) || SUMMARY="($code) 摘要生成失败"

    notify_weixin "📊 $code $name · $DATE" "$SUMMARY"

    # 微信 iLink 有 ~30s 冷却，避免连续推送被限流
    if ! $DRY_RUN; then
        sleep 35
    fi
done

echo "[$DATE] 全部完成"
