#!/usr/bin/env python3
"""push_email.py · 解析分析结果 → 发送 HTML 邮件"""
import json
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml


# ============================================================
# 颜色 & 信号映射
# ============================================================
COLORS = {
    "bullish": "#16a34a",   # 绿
    "bearish": "#dc2626",   # 红
    "neutral": "#6b7280",   # 灰
    "abstain": "#9ca3af",   # 浅灰
}
SIGNAL_TEXT = {
    "bullish": "看多",
    "bearish": "看空",
    "neutral": "中性",
    "abstain": "弃权",
}


def classify(value: float) -> str:
    if value > 0.3:
        return "bullish"
    if value < -0.3:
        return "bearish"
    return "neutral"


def consensus_text(avg: float) -> str:
    if avg > 0.5:
        return "强烈看多"
    if avg > 0.1:
        return "偏多"
    if avg < -0.5:
        return "强烈看空"
    if avg < -0.1:
        return "偏空"
    return "中性"


# ============================================================
# HTML 模板
# ============================================================
EMAIL_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>持仓日报 · {date}</title>
</head>
<body style="font-family: -apple-system, 'Segoe UI', sans-serif; max-width: 640px; margin: 0 auto; padding: 24px; background: #f9fafb; color: #111827;">

<div style="background: #fff; border-radius: 12px; padding: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);">

<h1 style="margin: 0 0 6px 0; font-size: 22px;">📊 持仓日报 · {date}</h1>
<p style="margin: 0 0 24px 0; color: #6b7280; font-size: 13px;">ai-hedge-fund · {strategy} · {n_tickers} 只标的</p>

{stocks_html}

<div style="margin-top: 32px; padding-top: 20px; border-top: 1px solid #e5e7eb; font-size: 12px; color: #9ca3af;">
  本报告由 AI 生成，仅供参考，不构成投资建议。<br>
  数据源: AkShare + baostock（免费） · 模型: {llm_model}
</div>

</div>
</body>
</html>
"""

STOCK_TEMPLATE = """\
<div style="margin-bottom: 28px; padding-bottom: 24px; border-bottom: 1px solid #f3f4f6;">
  <div style="display: flex; align-items: baseline; gap: 10px; margin-bottom: 8px;">
    <span style="font-size: 17px; font-weight: 600;">{name}</span>
    <span style="font-size: 13px; color: #6b7280;">{code}</span>
    {cost_badge}
  </div>

  <div style="display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; color: #fff; background: {consensus_color}; margin-bottom: 16px;">
    共识: {consensus_label} ({avg_value:+.2f})
  </div>

  {masters_html}
</div>
"""

MASTER_TEMPLATE = """\
<div style="margin-bottom: 10px; padding: 10px 12px; background: #f9fafb; border-radius: 8px; border-left: 3px solid {color};">
  <div style="font-size: 13px; font-weight: 600; margin-bottom: 4px;">
    {master_name}
    <span style="color: {color}; font-weight: 500;">· {signal_label}</span>
  </div>
  <div style="font-size: 13px; color: #374151; line-height: 1.5;">{reasoning}</div>
</div>
"""


# ============================================================
# 构建邮件内容
# ============================================================
def build_email(report: dict, tickers_map: dict, date: str) -> str:
    strategy = report.get("name", "unknown")
    llm_model = report.get("metadata", {}).get("llm_model", "unknown")

    # 按 ticker 聚合信号
    by_ticker: dict[str, list[dict]] = {}
    for strat in report.get("strategies", []):
        for sig in strat.get("signals", []):
            ticker = sig.get("ticker", "")
            by_ticker.setdefault(ticker, []).append(sig)

    # 按共识值排序（最看空在前，方便关注风险）
    ticker_consensus = []
    for ticker, signals in by_ticker.items():
        vals = [s.get("value", 0.0) for s in signals]
        avg = sum(vals) / len(vals) if vals else 0.0
        ticker_consensus.append((ticker, avg, signals))
    ticker_consensus.sort(key=lambda x: x[1])

    stocks_html_parts = []
    for ticker, avg, signals in ticker_consensus:
        info = tickers_map.get(ticker, {})
        name = info.get("name", ticker)
        cost = info.get("cost")

        cls = classify(avg)
        consensus_label = consensus_text(avg)
        consensus_color = COLORS.get(cls, "#6b7280")

        cost_badge = ""
        if cost:
            cost_badge = f'<span style="font-size: 12px; color: #9ca3af;">成本 {cost}</span>'

        # 取置信度最高的 3 位大师
        signals_sorted = sorted(
            signals,
            key=lambda s: s.get("metadata", {}).get("confidence", 0),
            reverse=True,
        )[:3]

        masters_html = ""
        for sig in signals_sorted:
            master_name = sig.get("model_name", "?")
            value = sig.get("value", 0.0)
            reasoning = sig.get("reasoning") or ""
            meta = sig.get("metadata", {})
            signal_key = meta.get("signal", classify(value))
            confidence = meta.get("confidence", 0)

            sig_color = COLORS.get(signal_key, "#6b7280")
            sig_label = SIGNAL_TEXT.get(signal_key, signal_key)

            masters_html += MASTER_TEMPLATE.format(
                color=sig_color,
                master_name=master_name.capitalize(),
                signal_label=f"{sig_label} {confidence}",
                reasoning=reasoning[:180] + ("…" if len(reasoning) > 180 else ""),
            )

        stocks_html_parts.append(
            STOCK_TEMPLATE.format(
                name=name,
                code=ticker,
                cost_badge=cost_badge,
                consensus_color=consensus_color,
                consensus_label=consensus_label,
                avg_value=avg,
                masters_html=masters_html,
            )
        )

    stocks_html = "\n".join(stocks_html_parts)
    if not stocks_html:
        stocks_html = "<p style='color: #9ca3af;'>暂无数据</p>"

    return EMAIL_TEMPLATE.format(
        date=date,
        strategy=strategy,
        n_tickers=len(by_ticker),
        stocks_html=stocks_html,
        llm_model=llm_model,
    )


# ============================================================
# 发送邮件
# ============================================================
def send_email(html: str, smtp_cfg: dict, subject: str) -> None:
    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port", 465))
    use_ssl = smtp_cfg.get("use_ssl", True)
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    from_addr = smtp_cfg.get("from", user)
    to_addrs = [a.strip() for a in smtp_cfg["to"].split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(html, "html", "utf-8"))

    if use_ssl:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.starttls()

    try:
        smtp.login(user, password)
        smtp.sendmail(from_addr, to_addrs, msg.as_string())
        print(f"[OK] 邮件已发送至 {', '.join(to_addrs)}")
    finally:
        smtp.quit()


# ============================================================
# Main
# ============================================================
def main():
    if len(sys.argv) < 3:
        print(f"用法: {sys.argv[0]} <report.json> <date> [tickers.yaml]")
        sys.exit(1)

    json_path = Path(sys.argv[1])
    date = sys.argv[2]
    tickers_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None

    # 加载分析结果
    with open(json_path) as f:
        report = json.load(f)

    # 加载 ticker 名称映射
    tickers_map = {}
    if tickers_path and tickers_path.exists():
        with open(tickers_path) as f:
            data = yaml.safe_load(f)
        for t in data.get("tickers", []):
            tickers_map[t["code"]] = t

    # 加载 SMTP 配置
    smtp_path = Path(__file__).parent.parent / "config" / "smtp.yaml"
    if not smtp_path.exists():
        print(f"[ERR] SMTP 配置不存在: {smtp_path}", file=sys.stderr)
        sys.exit(1)

    with open(smtp_path) as f:
        smtp_cfg = yaml.safe_load(f).get("smtp", {})

    # 构建并发送
    html = build_email(report, tickers_map, date)
    subject = f" 持仓日报 · {date} · {report.get('name', '')}"
    send_email(html, smtp_cfg, subject)


if __name__ == "__main__":
    main()
