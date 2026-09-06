#!/usr/bin/env python3
"""val_card.py · 单只股票 → 量化估值决策卡

回答三件事（只给结论，不给大师明细）：
  1. 现在多少钱          —— 现价（实时）
  2. 值多少钱            —— 用该股过去 3 年 PE/PB 历史分位推合理价区间
  3. 你的持仓怎么操作    —— 现价 vs 合理区间 + 你的浮盈亏 → 动作

估值逻辑（透明、可复核）：
  - 拉 baostock 近 3 年日频 peTTM / pbMRQ
  - PE 有效时：合理价 = 现价 × (历史PE分位 / 当前PE)
     低值取 P25、高值取 P75；EPS 亏损（PE<=0）时退回 PB 法
  - 现价 < P25 区间 → 低估；> P75 区间 → 高估；在区间内 → 合理
  - 用「合理价中位（P50）」与现价比，得到上行/下行空间 %

用法: python3 val_card.py <code> <name> [shares] [cost]
"""
import sys
import numpy as np
import baostock as bs

_YEARS = 3          # 历史估值窗口


def _bs_symbol(code: str) -> str:
    return ("sh." if code.startswith(("5", "6", "9")) else "sz.") + code


def hist_pe_pb(code: str):
    """近 3 年日频 peTTM/pbMRQ 序列（升序排序后的数组 + 最新值）。"""
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            _bs_symbol(code), "date,peTTM,pbMRQ",
            start_date="2023-09-01", end_date="2026-09-06",
            frequency="d", adjustflag="3")
        pes, pbs, dates = [], [], []
        while rs.error_code == "0" and rs.next():
            d = rs.get_row_data()
            if len(d) >= 3 and d[1] not in ("", "null") and float(d[1]) > 0:
                pes.append(float(d[1]))
                pbs.append(float(d[2]) if d[2] not in ("", "null") else np.nan)
                dates.append(d[0])
    finally:
        bs.logout()
    if not pes:
        return None
    arr = np.array(pes)
    return {"pe": np.sort(arr[~np.isnan(arr)]),
            "pb": np.sort(np.array(pbs)[~np.isnan(np.array(pbs))]),
            "cur_pe": float(arr[-1]), "last_date": dates[-1]}


def pctile(sorted_arr, q) -> float:
    return float(np.percentile(sorted_arr, q))


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: val_card.py <code> <name> [shares] [cost]")
        sys.exit(2)
    code, name = sys.argv[1], sys.argv[2]
    shares = float(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else 0
    cost = float(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 0

    import urllib.request
    # 实时价：腾讯行情（免费、稳定）— sh600519 / sz000001 / sh512880
    qq_symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
    url = "https://qt.gtimg.cn/q=" + qq_symbol
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
        # v_sh600519="1~贵州茅台~600519~现价~昨收~今开~..."
        parts = raw.split('~')
        mark = float(parts[3]) if len(parts) > 3 else None
    except Exception:
        mark = None
        print("⚠ 取现价失败")

    hist = hist_pe_pb(code)
    if hist is None:
        print(f"📊 {name} {code} 现价 {mark}\n无 3 年估值数据，无法估值")
        return

    cur_pe = hist["cur_pe"]
    if cur_pe <= 0:
        print(f"📊 {name} {code} 现价 {mark}\n当前 PE<=0（亏损），历史估值法不适用")
        return
    if mark is None:
        print(f"📊 {name} {code}\n无法取现价，跳过")
        return

    p25, p50, p75 = (pctile(hist["pe"], q) for q in (25, 50, 75))
    lo, mid, hi = (mark * p25 / cur_pe, mark * p50 / cur_pe,
                   mark * p75 / cur_pe)
    # gap = 现价相对合理中值的偏离：正 = 现价高于合理中值（偏贵）
    gap = (mark / mid - 1) * 100 if mark and mid else 0.0

    # 现价相对合理区间的定位
    if mark < lo:
        zone = f"低估（低于合理区间下沿 {lo:.2f}）"
    elif mark > hi:
        zone = f"高估（高于合理区间上沿 {hi:.2f}）"
    else:
        zone = f"合理区间内（{lo:.2f} ~ {hi:.2f}）"

    lines = [
        f"📊 {name} {code}",
        f"现价 {mark}",
        "",
        f"合理价区间：{lo:.2f} ~ {hi:.2f}（中值 {mid:.2f}，3年PE P25~P75 法）",
        f"现价 vs 合理中值：{gap:+.1f}%　→　{zone}",
        f"当前PE {cur_pe:.1f}　历史3年PE：P25={p25:.1f} 中位={p50:.1f} P75={p75:.1f}",
    ]

    # 操作建议：现价 vs 合理中值 + 持仓盈亏
    if not shares or shares <= 0:
        if gap < -15:
            lines.append(f"操作：未持仓 · 现价比合理中值低 {abs(gap):.0f}%，可关注低吸")
        elif gap > 15:
            lines.append(f"操作：未持仓 · 现价偏贵（高 {gap:.0f}%），不追")
        else:
            lines.append("操作：未持仓 · 估值接近合理，观望或小仓")
    else:
        pnl_pct = (mark / cost - 1) * 100
        pnl = (mark - cost) * shares
        lines.append(f"持仓 {int(shares)}股 @ {cost:.2f} → 浮动 {pnl:+,.0f}（{pnl_pct:+.1f}%）")
        # 决策：便宜+持仓 + 贵+持仓
        if gap < -15 and pnl_pct < 0:
            lines.append(f"操作：浮亏{pnl_pct:.0f}% 且现价低于合理中值{abs(gap):.0f}% → "
                         f"估值已低，可持有等修复，勿在此割肉")
        elif gap < -15:
            lines.append(f"操作：浮盈{pnl_pct:.0f}% 且现价低于合理中值 → 继续持有，"
                         f"仍有上行空间")
        elif gap > 15 and pnl_pct > 10:
            lines.append(f"操作：浮盈{pnl_pct:.0f}% 但现价已高于合理中值{gap:.0f}% → "
                         f"建议减仓止盈，落袋为主")
        elif gap > 15:
            lines.append(f"操作：现价高估（{gap:.0f}%）→ 反弹减仓，不宜加仓")
        elif pnl_pct > 10:
            lines.append(f"操作：估值合理但已浮盈{pnl_pct:.0f}% → 持有，上移止盈位")
        else:
            lines.append("操作：估值合理区间 → 持有观察，不加不减")

    lines.append("")
    lines.append("注：合理价=现价×3年历史PE分位/当前PE，估值中枢会随业绩与市场整体漂移")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
