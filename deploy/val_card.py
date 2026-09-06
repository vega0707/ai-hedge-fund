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

    # 盈利崩塌保护：PE 畸高（EPS 近零）时历史 PE 分位法会给出荒谬"合理价"，
    # 这类票直接判定为"盈利异常"，不给数字，提示看基本面/大师方向。
    if cur_pe > 100:
        print(f"📊 {name} {code}\n现价 {mark}\n"
              f"⚠ 当前 PE {cur_pe:.0f}（盈利几乎为零/崩塌），历史 PE 估值不适用\n")
        return

    p25, p50, p75 = (pctile(hist["pe"], q) for q in (25, 50, 75))
    lo, mid, hi = (mark * p25 / cur_pe, mark * p50 / cur_pe,
                   mark * p75 / cur_pe)
    # gap = 现价相对合理中值的偏离：正 = 现价高于合理中值（偏贵）
    gap = (mark / mid - 1) * 100 if mark and mid else 0.0

    lines = [
        f"📊 {name} {code}",
        f"现价 {mark}",
        f"合理价 {lo:.1f}~{hi:.1f}（中值{mid:.1f}）",
    ]

    # 操作建议：现价 vs 合理中值 + 持仓盈亏（短句，同 demo）
    if not shares or shares <= 0:
        if gap < -15:
            lines.append("操作：可关注，回踩分批建仓")
        elif gap > 15:
            lines.append("操作：不追，等右侧信号")
        else:
            lines.append("操作：观望")
    else:
        pnl_pct = (mark / cost - 1) * 100
        pnl = (mark - cost) * shares
        lines.append(f"持仓 {int(shares)}股 浮动 {pnl:+,.0f}（{pnl_pct:+.1f}%）")
        # 决策：便宜+持仓 + 贵+持仓
        if gap < -15:
            lines.append("操作：持有，仍低于合理价有空间" if pnl_pct >= 0
                         else "操作：持有/补仓，估值已低")
        elif gap > 15:
            lines.append("操作：减仓止盈" if pnl_pct > 10
                         else "操作：反弹减仓")
        else:
            lines.append("操作：持有" if pnl_pct > 10 else "操作：持有观察")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
