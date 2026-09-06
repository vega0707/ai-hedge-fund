#!/usr/bin/env python3
"""decision_card.py · 单只股票 → 一张「决策卡片」

不做大师明细轰炸。只回答三件事：
  1. 现在多少钱 / 今天怎么走（mark）
  2. 大师怎么看（共识方向 + 置信），以及「值多少钱」的锚：
     从大师理由里提取 市盈率/市净率/每股净资产/ROE 等可核实的估值事实
  3. 结合你的持仓（shares/cost）给出操作建议

用法: python3 decision_card.py <report.json> <code> <name> [shares] [cost]
"""
import json
import re
import sys
from pathlib import Path

# 关键估值锚：从大师理由提取，宽松模式（中文常写「市盈率在6-11倍」「负债/权益比率高达8.8」）
# 每类多条正则按顺序尝试，取第一个命中
_ANCHOR_GROUPS = {
    "PE": [
        r"市盈率[在约为达至到]?[\s]*(?:仅|才)?([0-9]+(?:\.[0-9]+)?)\s*[—\-~～至]\s*[0-9]+倍",
        r"市盈率[在约为达至到]?[\s]*(?:仅|才)?([0-9]+(?:\.[0-9]+)?)",
        r"市盈率.*?([0-9]+(?:\.[0-9]+)?)\s*倍",
    ],
    "PB": [
        r"市净率[在约为达至到]?[\s]*(?:仅)?([0-9]+(?:\.[0-9]+)?)",
        r"市净率.*?([0-9]+(?:\.[0-9]+)?)\s*倍",
    ],
    "BVPS": [r"每股净资产[约从为到]?[\s]*([0-9]+(?:\.[0-9]+)?)"],
    "EPS": [
        r"每股收益[约从为到]?[\s]*([0-9]+(?:\.[0-9]+)?)",
        r"每股基本盈利[约为]?[\s]*([0-9]+(?:\.[0-9]+)?)",
        r"每股盈利[约为]?[\s]*([0-9]+(?:\.[0-9]+)?)",
    ],
    "ROE": [
        r"ROE[约在]?[\s]*([0-9]+(?:\.[0-9]+)?)\s*%",
        r"净资产收益率[约为]?[\s]*([0-9]+(?:\.[0-9]+)?)\s*%",
    ],
    "负债权益": [
        r"负债[/与及]?权益[比率为约高达升至]?[\s]*([0-9]+(?:\.[0-9]+)?)",
    ],
    "毛利率": [r"毛利率[约为]?[\s]*([0-9]+(?:\.[0-9]+)?)\s*%"],
    "净利率": [r"净利率[约为]?[\s]*([0-9]+(?:\.[0-9]+)?)\s*%"],
}


def collect(report: dict, code: str) -> dict:
    """汇总一只股票所有大师信号 → 共识 + 估值锚 + 代表理由。"""
    vals = []
    bull_reasons, bear_reasons, neutral_reasons = [], [], []
    anchors = {}  # anchor -> list of numbers mentioned

    for strat in report.get("strategies", []):
        for sig in strat.get("signals", []):
            if sig.get("ticker") != code:
                continue
            model = sig.get("model_name", "")
            v = sig.get("value", 0)
            if sig.get("metadata", {}).get("abstained") or \
                    str(sig.get("reasoning", "")).startswith("abstained"):
                continue
            vals.append(v)
            reason = sig.get("reasoning") or ""
            # 估值锚提取（宽松）
            text = re.sub(r"\s+", " ", reason)
            for label, pats in _ANCHOR_GROUPS.items():
                for pat in pats:
                    m = re.search(pat, text)
                    if m:
                        try:
                            num = float(m.group(1))
                        except (ValueError, IndexError):
                            continue
                        # PE/PB/ROE 合理范围过滤（排除 0 和离谱值）
                        if 0 < num < 5000:
                            anchors.setdefault(label, []).append(num)
                        break  # 同一 label 取第一个命中
            # 代表理由按方向收集
            bucket = bull_reasons if v > 0.3 else (bear_reasons if v < -0.3 else neutral_reasons)
            if len(bucket) < 1:  # 每个方向只留 1 条最长最完整的
                bucket.append(reason)
            elif len(reason) > len(bucket[0]):
                bucket[0] = reason

    n = len(vals)
    consensus = sum(vals) / n if n else 0.0
    n_bull = sum(1 for v in vals if v > 0.3)
    n_bear = sum(1 for v in vals if v < -0.3)
    n_neutral = n - n_bull - n_bear

    # 估值锚：每类取出现频次最高的值（多数大师引用的数更可信）
    anchor_best = {}
    for label, nums in anchors.items():
        if not nums:
            continue
        from collections import Counter
        c = Counter(round(x, 2) for x in nums if x < 10000)
        anchor_best[label] = c.most_common(1)[0][0]

    return {
        "n": n, "consensus": consensus,
        "n_bull": n_bull, "n_bear": n_bear, "n_neutral": n_neutral,
        "anchors": anchor_best,
        "bull_reason": bull_reasons[0] if bull_reasons else None,
        "bear_reason": bear_reasons[0] if bear_reasons else None,
        "neutral_reason": neutral_reasons[0] if neutral_reasons else None,
    }


# 共识阈值：|consensus| >= 0.3 视为有方向，否则中性（verdict 与 action 共用）
_DIR_THRESHOLD = 0.3


def verdict(consensus: float) -> str:
    if consensus >= _DIR_THRESHOLD:
        return "看多 · 大师多数认为低估/有空间"
    if consensus <= -_DIR_THRESHOLD:
        return "看空 · 大师多数认为高估/风险大"
    return "中性 · 多空分歧，无明显方向"


def action(consensus: float, shares: float, cost: float, mark: float,
           name: str) -> str:
    """按共识方向 × 持仓盈亏 → 操作建议。"""
    if not shares or shares <= 0:
        base = "（未持仓）"
        if consensus > _DIR_THRESHOLD:
            return f"{base} 可关注，回踩分批建仓"
        if consensus < -_DIR_THRESHOLD:
            return f"{base} 不追，等右侧信号"
        return f"{base} 观望"
    pnl_pct = (mark / cost - 1) * 100 if cost else 0
    pos = "盈利" if pnl_pct >= 0 else "亏损"
    pos_txt = f"{pnl_pct:+.1f}%"

    # 四象限
    if consensus > _DIR_THRESHOLD:        # 大师偏多
        if pnl_pct > 0:
            return f"持有（已{pos} {pos_txt}，共识看多），拿住吃趋势，跌破关键位再走"
        return f"持有/补仓（{pos} {pos_txt}，但共识看多），可摊低成本，严格止损"
    if consensus < -_DIR_THRESHOLD:       # 大师偏空
        if pnl_pct > 0:
            return f"减仓止盈（已{pos} {pos_txt}，共识看空），落袋为安，留底仓观察"
        return f"止损/减仓（{pos} {pos_txt} 且共识看空），反弹减仓，勿硬扛"
    # 中性
    if pnl_pct > 0:
        return f"持有观察（已{pos} {pos_txt}，共识中性无方向），设好止盈位"
    return f"观望/小仓（{pos} {pos_txt}，共识中性无方向），不加仓"


def anchor_line(anchors: dict, mark: float) -> str:
    """把估值锚拼成一行可核实的事实。"""
    parts = []
    for label, display in (("PE", "PE"), ("PB", "PB"), ("BVPS", "BVPS"),
                           ("EPS", "EPS"), ("ROE", "ROE"),
                           ("负债权益", "负债/权益"), ("毛利率", "毛利率"),
                           ("净利率", "净利率")):
        if label in anchors:
            parts.append(f"{display}≈{anchors[label]:g}")
    if parts:
        return " · ".join(parts)
    return "（大师未引用明确估值数字）"


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: decision_card.py <report.json> <code> [name] [shares] [cost]")
        sys.exit(2)
    path, code = Path(sys.argv[1]), sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else code
    shares = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 0
    cost = float(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else 0

    if not path.exists():
        print(f"❌ {name} {code} 无分析结果")
        return

    data = json.loads(path.read_text())
    mark = data.get("marks", {}).get(code)
    info = collect(data, code)
    if info["n"] == 0:
        print(f"📊 {name} {code}\n大师全部弃权（无财务数据，可能为 ETF）\n")
        return

    v = info["consensus"]
    lines = [
        f"📊 {name} {code}",
        f"现价 {mark}",
        "",
        f"大师共识（{info['n']}位）：{verdict(v)}",
        f"   🟢{info['n_bull']}看多  ⚪{info['n_neutral']}中性  🔴{info['n_bear']}看空  (信号 {v:+.2f})",
        f"估值锚：{anchor_line(info['anchors'], mark)}",
    ]
    # 一句话代表理由（只取与共识方向一致的最强）
    if v > 0.15 and info["bull_reason"]:
        lines.append(f"看多逻辑：{info['bull_reason'][:110]}…")
    elif v < -0.15 and info["bear_reason"]:
        lines.append(f"看空逻辑：{info['bear_reason'][:110]}…")
    elif info["neutral_reason"]:
        lines.append(f"主要顾虑：{info['neutral_reason'][:110]}…")

    if shares and cost and mark:
        pnl = (mark - cost) * shares
        pnl_pct = (mark / cost - 1) * 100
        lines.append(f"你的持仓：{int(shares)}股 @ 成本{cost:.2f} → 浮动 {pnl:+,.0f} ({pnl_pct:+.1f}%)")
    lines.append(f"建议：{action(v, shares, cost, mark if mark else 0, name)}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
