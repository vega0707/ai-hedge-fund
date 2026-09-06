#!/usr/bin/env python3
"""summarize.py · 把单只股票的 report JSON 变成结构化微信消息。

用法: python3 summarize.py <report.json> <code> [<name>] [<shares>] [<cost>]

输出适合微信阅读的分行文本：
  头部（名称/代码/现价/持仓盈亏）
  三组策略信号（按策略分组，每行: emoji 大师名 数值 一句话理由）
  汇总（方向一致度）
"""
import json
import re
import sys
from pathlib import Path

MODEL_CN = {
    "graham": "格雷厄姆", "buffett": "巴菲特", "munger": "芒格",
    "burry": "伯里", "templeton": "邓普顿", "neff": "内夫",
    "greenblatt": "格林布拉特", "ackman": "阿克曼", "marks": "马克斯",
    "dalio": "达利欧", "lynch": "林奇", "pead": "PEAD",
}
STRAT_CN = {
    "deep-value": "深度价值", "quality-masters": "质量成长",
    "earnings-drift": "盈余动量",
}


def clean(text: str, limit: int = 66) -> str:
    """压缩空白、去掉换行；超长截断到 limit 并加省略号。"""
    if not text:
        return ""
    t = re.sub(r"\s+", " ", str(text)).strip()
    if len(t) > limit:
        return t[: limit - 1] + "…"
    return t


def emoji(v: float) -> str:
    if v > 0.3:
        return "🟢"
    if v < -0.3:
        return "🔴"
    return "⚪"


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: summarize.py <report.json> <code> [name] [shares] [cost]")
        sys.exit(2)
    path, code = Path(sys.argv[1]), sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else ""
    shares = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 0
    cost = float(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else 0

    if not path.exists():
        print(f"❌ {code} 无分析结果")
        sys.exit(0)

    data = json.loads(path.read_text())
    mark = data.get("marks", {}).get(code)
    final_w = data.get("final_weights", {}).get(code, 0)

    lines = []
    title = f"📊 {name} {code}" if name and name != code else f"📊 {code}"
    price_txt = f" 现价 {mark}" if mark is not None else ""
    lines.append(f"{title}{price_txt}")

    # 持仓浮盈亏
    if shares and cost and mark:
        pnl = (mark - cost) * shares
        pnl_pct = (mark / cost - 1) * 100 if cost else 0
        arrow = "📈" if pnl >= 0 else "📉"
        lines.append(
            f"{arrow} 持仓{int(shares)}股 成本{cost:.2f} "
            f"浮动 {pnl:+.0f} ({pnl_pct:+.1f}%)"
        )

    # 综合判定
    if final_w > 0:
        verdict = f"综合：🟢 看多（建议仓位 {final_w:.0%}）"
    elif final_w < 0:
        verdict = f"综合：🔴 看空（建议仓位 {final_w:.0%}）"
    else:
        verdict = "综合：⚪ 中性/观望"
    lines.append(verdict)
    lines.append("")

    # 各策略信号
    n_bull = n_bear = n_neutral = 0
    for strat in data.get("strategies", []):
        signals = [s for s in strat.get("signals", []) if s.get("ticker") == code]
        if not signals:
            continue
        label = STRAT_CN.get(strat.get("name", ""), strat.get("name", ""))
        # 该策略组里本股的加权意见
        w = strat.get("weights", {}).get(code, 0)
        if w > 0:
            lines.append(f"▎{label} 组 · 🟢")
        elif w < 0:
            lines.append(f"▎{label} 组 · 🔴")
        else:
            lines.append(f"▎{label} 组 · ⚪")
        for s in signals:
            model = MODEL_CN.get(s.get("model_name", ""), s.get("model_name", ""))
            v = s.get("value", 0)
            if v > 0.3:
                n_bull += 1
            elif v < -0.3:
                n_bear += 1
            else:
                n_neutral += 1
            reason = s.get("reasoning") or ""
            abstained = bool(s.get("metadata", {}).get("abstained"))
            if abstained or reason.startswith("abstained:"):
                reason_txt = "（弃权：数据不足）"
            elif s.get("model_name") == "greenblatt" and v == 0:
                reason_txt = "（中性：无显著机会）"
            else:
                reason_txt = clean(reason)
            lines.append(f"{emoji(v)} {model} {v:+.2f} {reason_txt}")
        lines.append("")

    # 汇总
    total = n_bull + n_bear + n_neutral
    if total:
        lines.append(f"—— {n_bull}看多 · {n_neutral}中性 · {n_bear}看空 ——")
    lines.append("数据源：akshare/东财 ｜ 仅供参考")

    print("\n".join(lines).rstrip())


if __name__ == "__main__":
    main()
