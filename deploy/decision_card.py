#!/usr/bin/env python3
"""decision_card.py · 单只股票 → 一张「决策卡片」

只回答你关心的三件事：
  1. 现在多少钱（mark，真实行情）
  2. 大师整体怎么看（共识方向：看多/中性/看空 + 票数分布）
  3. 按你的持仓（shares/cost）该怎么操作

不做大师明细轰炸。大师口述的具体价格数字经核实可能与行情矛盾
（LLM 幻觉/过时上下文），因此本卡片只信方向、不信数字。

用法: python3 decision_card.py <report.json> <code> <name> [shares] [cost]
"""
import json
import sys
from pathlib import Path

_DIR_THRESHOLD = 0.30       # |consensus| >= 0.3 视为有方向
_DISPLAY_THRESHOLD = 0.15   # 低于此显示"无明显信号"


def collect(report: dict, code: str) -> dict:
    """汇总一只股票所有大师信号 → 共识 + 票数 + 每方向代表理由。"""
    vals = []
    reasons = {"bull": [], "bear": [], "neutral": []}

    for strat in report.get("strategies", []):
        for sig in strat.get("signals", []):
            if sig.get("ticker") != code:
                continue
            if sig.get("metadata", {}).get("abstained") or \
                    str(sig.get("reasoning", "")).startswith("abstained"):
                continue
            v = sig.get("value", 0)
            vals.append(v)
            reason = (sig.get("reasoning") or "").strip()
            if not reason:
                continue
            bucket = ("bull" if v > _DIR_THRESHOLD
                      else "bear" if v < -_DIR_THRESHOLD else "neutral")
            # 每方向只留最长最完整的一条当代表
            if not reasons[bucket] or len(reason) > len(reasons[bucket][0]):
                reasons[bucket] = [reason]

    n = len(vals)
    consensus = sum(vals) / n if n else 0.0
    n_bull = sum(1 for v in vals if v > _DIR_THRESHOLD)
    n_bear = sum(1 for v in vals if v < -_DIR_THRESHOLD)
    return {
        "n": n, "consensus": consensus,
        "n_bull": n_bull, "n_bear": n_bear,
        "n_neutral": n - n_bull - n_bear,
        "bull": reasons["bull"][0] if reasons["bull"] else None,
        "bear": reasons["bear"][0] if reasons["bear"] else None,
        "neutral": reasons["neutral"][0] if reasons["neutral"] else None,
    }


def consensus_label(consensus: float) -> str:
    c = consensus
    if c >= 0.6:
        return "强烈看多"
    if c >= _DIR_THRESHOLD:
        return "看多"
    if c <= -0.6:
        return "强烈看空"
    if c <= -_DIR_THRESHOLD:
        return "看空"
    if c >= _DISPLAY_THRESHOLD:
        return "略偏多（未达行动线）"
    if c <= -_DISPLAY_THRESHOLD:
        return "略偏空（未达行动线）"
    return "无明显方向"


def action(consensus: float, shares: float, cost: float, mark: float,
           name: str) -> str:
    """按共识方向 × 持仓盈亏 → 操作建议（四象限 + 未持仓）。"""
    if not shares or shares <= 0:
        if consensus >= _DIR_THRESHOLD:
            return "未持仓：大师看多，可关注回踩分批建仓（轻仓试探）"
        if consensus <= -_DIR_THRESHOLD:
            return "未持仓：大师看空，不追，等右侧信号再介入"
        return "未持仓：大师无明确方向，观望"

    pnl_pct = (mark / cost - 1) * 100 if cost else 0
    pos = "浮盈" if pnl_pct >= 0 else "浮亏"

    if consensus >= _DIR_THRESHOLD:
        if pnl_pct >= 0:
            return (f"{pos} {pnl_pct:+.1f}% + 大师看多 → 持有"
                    f"，让利润奔跑，跌破关键位再减")
        return (f"{pos} {pnl_pct:+.1f}% 但大师看多 → 可逢低补仓"
                f"摊低成本，严格设止损")
    if consensus <= -_DIR_THRESHOLD:
        if pnl_pct > 10:
            return (f"{pos} {pnl_pct:+.1f}% + 大师看空 → 建议减仓止盈"
                    f"，落袋为安，留小仓观察")
        return (f"{pos} {pnl_pct:+.1f}% + 大师看空 → 反弹减仓/止损"
                f"，勿死扛，换强逻辑标的")
    # 中性
    if pnl_pct >= 0:
        return (f"{pos} {pnl_pct:+.1f}% + 大师无方向 → 持有观察，"
                f"设好止盈位（如 -5% 移动止盈）")
    return (f"{pos} {pnl_pct:+.1f}% + 大师无方向 → 不加仓不割肉，"
            f"跌破成本 -8%~-10% 再考虑止损")


def pick_logic(info: dict) -> tuple[str | None, str | None]:
    """取与共识方向一致的一条代表逻辑；无方向时提示分歧/风险。"""
    c = info["consensus"]
    if c >= _DIR_THRESHOLD and info["bull"]:
        return "看多逻辑：", info["bull"]
    if c <= -_DIR_THRESHOLD and info["bear"]:
        return "看空逻辑：", info["bear"]
    if info["neutral"]:
        return "多空分歧：", info["neutral"]
    if info["bear"]:
        return "注意风险：", info["bear"]
    if info["bull"]:
        return "积极因素：", info["bull"]
    return None, None


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: decision_card.py <report.json> <code> [name] [shares] [cost]")
        sys.exit(2)
    path, code = Path(sys.argv[1]), sys.argv[2]
    name = sys.argv[3] if len(sys.argv) > 3 else code
    shares = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 0
    cost = float(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else 0

    if not path.exists():
        print(f"❌ {name} {code}：无分析结果")
        return

    data = json.loads(path.read_text())
    mark = data.get("marks", {}).get(code)
    info = collect(data, code)

    if info["n"] == 0:
        # ETF/指数：无基本面，大师不评级（量化组也可能中性）
        is_etf = code.startswith(("1", "5")) and len(code) == 6
        if is_etf:
            print(f"📊 {name} {code}\n现价 {mark}\n"
                  f"ETF 无个股基本面，大师不评级；只看价格趋势"
                  f"，不适用个股估值/操作建议\n")
        else:
            print(f"📊 {name} {code}\n现价 {mark}\n大师均弃权"
                  f"（无财务数据）\n")
        return

    label = consensus_label(info["consensus"])
    emoji = ("🟢" if info["consensus"] >= _DIR_THRESHOLD
             else "🔴" if info["consensus"] <= -_DIR_THRESHOLD else "⚪")

    lines = [
        f"{emoji} {name} {code}",
        f"现价 {mark}",
        "",
        f"大师共识：{label}（{info['n_bull']}看多 · "
        f"{info['n_neutral']}中性 · {info['n_bear']}看空）",
    ]

    head, body = pick_logic(info)
    if head and body:
        body_one = " ".join(body.split())
        # 跳过纯占位内容（数据不足等）
        if body_one and "数据不足" not in body_one[:12] and body_one != "中性":
            lines.append(f"{head}{body_one[:96]}{'…' if len(body_one) > 96 else ''}")

    if shares and cost and mark:
        pnl = (mark - cost) * shares
        pnl_pct = (mark / cost - 1) * 100 if cost else 0
        lines.append(f"持仓 {int(shares)}股 @ {cost:.2f} → 浮动 {pnl:+,.0f}"
                     f"（{pnl_pct:+.1f}%）")

    lines.append(f"操作：{action(info['consensus'], shares, cost, mark if mark else 0, name)}")
    lines.append("")
    lines.append("注：方向基于最新财报；大师口述的具体价格数字未经核实，勿直接采信")
    print("\n".join(lines).rstrip())


if __name__ == "__main__":
    main()
