"""策略层：实现《安全吃股息 V3.0》的定价与状态判定逻辑。

核心链路：
    DPS 口径 → 定价 DPS → 三档价格 → 当前状态 → 安全校验

关键规则（对应原书章节）：
  第 17 章  四种 DPS：最新 / 保守 / 正常化 / 压力
  第 18 章  保守 DPS 清洗：剔除特别分红
  第 19 章  正常化 DPS：min(三年最低, 三年均值×80%, 正常化利润法)
  第 20 章  压力 DPS：保守 DPS × 压力系数，决定最大仓位
  第 14 章  三档价格 = 定价 DPS ÷ 目标股息率
  第 16 章  PE / PB 只用于检查，不做买点触发器
  第 23 章  六种假高股息识别
  第 44 章  分红下降的四级处理
"""
from __future__ import annotations

import statistics

# ------------------------------------------------------------------ 参数
NORMALIZED_FACTOR = 0.80  # 三年均值打折系数（第 19 章）
STRESS_FACTOR = 0.80  # 压力 DPS 相对保守 DPS 的折扣（第 20 章）
DPS_DROP_L1 = 0.10  # 一级：分红下降 ≤10%
DPS_DROP_L2 = 0.25  # 二级：10%~25%
MIN_HISTORY_YEARS = 3  # 计算保守 DPS 所需的最少年数

# 估值深度的颜色分档（相对首仓门槛的超额比例）
DEPTH_BUCKETS = [
    (0.60, "重仓区", "heavy"),
    (0.35, "深度低估", "deep"),
    (0.12, "进入买区", "entry"),
    (0.0, "接近买入", "near"),
]


# ------------------------------------------------------------------ 工具
def _pct(x, nd=2):
    return f"{x * 100:.{nd}f}%"


def _safe_div(a, b):
    try:
        if b in (0, None):
            return None
        return a / b
    except Exception:
        return None


# ------------------------------------------------------------------ DPS 计算
def compute_dps(div_years: dict, mode: str, fin: dict | None = None) -> dict:
    """计算四种 DPS，返回 dict。

    div_years: {报告年度: 每股分红}
    mode:      conservative / normalized / min3 / min3_avg80 / none
    fin:       {年度: {profit, ocf, ...}}，用于正常化利润法
    """
    res = {
        "latest_dps": None,
        "conservative_dps": None,
        "normalized_dps": None,
        "stress_dps": None,
        "pricing_dps": None,
        "years_used": [],
        "basis": "",
        "note": "",
    }
    if not div_years:
        res["note"] = "无分红历史数据"
        return res

    years = sorted(div_years.keys())
    vals = {y: div_years[y] for y in years if div_years[y] > 0}
    if not vals:
        res["note"] = "近十年无现金分红记录"
        return res

    y_sorted = sorted(vals.keys())
    latest = y_sorted[-1]
    res["latest_dps"] = round(vals[latest], 4)

    # ---- 保守 DPS = min(最近三年年度 DPS)
    recent3 = y_sorted[-MIN_HISTORY_YEARS:]
    if len(recent3) >= MIN_HISTORY_YEARS:
        cons = min(vals[y] for y in recent3)
        res["conservative_dps"] = round(cons, 4)
        res["years_used"] = recent3
    else:
        res["conservative_dps"] = round(min(vals.values()), 4)
        res["years_used"] = y_sorted
        res["note"] = f"分红历史仅 {len(recent3)} 年，保守 DPS 代表性不足"

    # ---- 正常化 DPS（第 19 章）
    # 弱周期：min(三年最低 DPS, 正常化 DPS)
    # 更保守：min(三年最低, 三年均值×80%, 正常化 DPS)
    # 此处用「五年中位数」近似正常化 DPS（无需外部煤价/运价假设，可稳定复现）
    win5 = y_sorted[-5:]
    median5 = statistics.median([vals[y] for y in win5])
    avg3 = sum(vals[y] for y in recent3) / len(recent3)
    min3 = min(vals[y] for y in recent3)
    res["normalized_dps"] = round(min(median5, avg3 * NORMALIZED_FACTOR, min3), 4)

    # ---- 压力 DPS
    base = res["conservative_dps"] or res["normalized_dps"]
    res["stress_dps"] = round(base * STRESS_FACTOR, 4)

    # ---- 按口径选定定价 DPS
    if mode == "conservative":
        res["pricing_dps"] = res["conservative_dps"]
        res["basis"] = "保守 DPS（近三年最低）"
    elif mode == "normalized":
        res["pricing_dps"] = res["normalized_dps"]
        res["basis"] = "正常化 DPS（中位数/均值80%/三年最低 取低）"
    elif mode == "min3":
        res["pricing_dps"] = round(min3, 4)
        res["basis"] = "三年最低 DPS"
    elif mode == "min3_avg80":
        res["pricing_dps"] = round(min(min3, avg3 * NORMALIZED_FACTOR), 4)
        res["basis"] = "min(三年最低, 三年均值×80%)"
    elif mode == "none":
        res["pricing_dps"] = None
        res["basis"] = "不设固定档（特殊资产）"
    else:
        res["pricing_dps"] = res["conservative_dps"]
        res["basis"] = "保守 DPS"

    return res


# ------------------------------------------------------------------ 三档价格
def three_tranches(pricing_dps, entry, add, heavy):
    """由定价 DPS 反推三档价格。"""
    if not pricing_dps or pricing_dps <= 0 or entry <= 0:
        return {"entry_price": None, "add_price": None, "heavy_price": None}
    return {
        "entry_price": round(pricing_dps / entry, 2),
        "add_price": round(pricing_dps / add, 2) if add > 0 else None,
        "heavy_price": round(pricing_dps / heavy, 2) if heavy > 0 else None,
    }


# ------------------------------------------------------------------ 状态判定
def judge_state(price, dps_info, entry, add, heavy):
    """判断当前处于第几档。

    返回 (state, state_label, depth_pct, tranche)
      state: watch / near / entry / add / heavy
    """
    pdps = dps_info.get("pricing_dps")
    if not pdps or pdps <= 0 or price <= 0:
        return ("watch", "观察", None, "-")

    cur_yield = pdps / price
    tp = three_tranches(pdps, entry, add, heavy)

    if heavy > 0 and cur_yield >= heavy:
        return ("heavy", "重仓档", round(cur_yield / entry - 1, 4), "第三档 40%")
    if add > 0 and cur_yield >= add:
        return ("add", "加仓档", round(cur_yield / entry - 1, 4), "第二档 30%")
    if cur_yield >= entry:
        return ("entry", "首仓档", round(cur_yield / entry - 1, 4), "第一档 30%")
    if cur_yield >= entry * 0.85:
        return ("near", "接近买点", round(cur_yield / entry - 1, 4), "暂不建仓")
    return ("watch", "观察", round(cur_yield / entry - 1, 4), "暂不建仓")


# ------------------------------------------------------------------ 安全校验
def safety_check(div_years, fin, is_bank, level):
    """基本面与分红安全校验，返回 (等级, 提示列表, 否决标志)。

    等级：ok / caution / risk
    否决标志 True 表示该股进入风控名单，不作为买入候选（第 48 章 EXIT/PAUSE）。

    注意：原书第 44 章对分红下降分四级处理，只有「结构恶化 / 分红失效」才退出系统。
    因此本函数只在「多重恶化信号叠加」时否决，单一指标异常仅降级为 caution。
    """
    notes = []
    level_out = "ok"
    veto_flags = 0

    years = sorted(div_years.keys())
    vals = div_years

    # --- 分红趋势（第 44 章四级处理）
    if len(years) >= 2:
        y1, y2 = years[-1], years[-2]
        if vals[y2] > 0:
            drop = (vals[y2] - vals[y1]) / vals[y2]
            if drop > DPS_DROP_L2:
                notes.append(f"最新分红同比 -{_pct(drop,1)}，需核实是否结构恶化")
                level_out = "risk"
                veto_flags += 1
            elif drop > DPS_DROP_L1:
                notes.append(f"最新分红同比 -{_pct(drop,1)}，趋势弱化")
                if level_out == "ok":
                    level_out = "caution"

    # --- 分红连续性（只看近 10 年，避免拿 90 年代数据说事）
    recent_years = [y for y in years if y >= years[-1] - 10]
    if len(recent_years) >= 3:
        gaps = [recent_years[i + 1] - recent_years[i] for i in range(len(recent_years) - 1)]
        if max(gaps) > 1:
            notes.append(f"近 10 年有 {max(gaps)} 年未分红，连续性存疑")
            if level_out == "ok":
                level_out = "caution"

    # --- 分红支付率与现金流（第 5 章）
    if fin and years:
        ly = years[-1]
        f = fin.get(ly) or fin.get(years[-2] if len(years) >= 2 else ly) or {}
        eps = f.get("eps")
        if eps and eps > 0:
            payout = vals[ly] / eps
            if payout > 1.05:
                notes.append(f"分红支付率 {_pct(payout,0)} > 100%，透支风险")
                veto_flags += 1
                level_out = "risk"
            elif payout > 0.90:
                notes.append(f"分红支付率 {_pct(payout,0)} 偏高")
                if level_out == "ok":
                    level_out = "caution"

        profit = f.get("profit")
        ocf = f.get("ocf")
        if not is_bank and profit and ocf is not None:
            cover = _safe_div(ocf, profit)
            if cover is not None and cover < 0.5:
                notes.append(f"经营现金流/净利润仅 {cover:.2f}，现金流偏弱")
                veto_flags += 1
                if level_out == "ok":
                    level_out = "caution"
            elif cover is not None and cover < 0.8:
                notes.append(f"经营现金流/净利润 {cover:.2f} 偏低")
                if level_out == "ok":
                    level_out = "caution"

        # --- 负债率（银行不适用该口径）
        d = f.get("debt")
        if d is not None and not is_bank and d > 78:
            notes.append(f"资产负债率 {d:.1f}% 偏高")
            if level_out == "ok":
                level_out = "caution"

    # --- 观察级 / 特殊资产提示
    if level == "watch":
        notes.append("观察级资产：单股上限受限，需更高风险补偿")
    if level == "special":
        notes.append("特殊资产：不设固定三档，仅作机会仓")

    # 否决条件：分红腰斩 + 支付率超 100%（或现金流严重不覆盖）同时出现
    veto = veto_flags >= 2
    return level_out, notes, veto


# ------------------------------------------------------------------ 假高股息识别
def fake_high_yield_flags(static_yield, pricing_yield, div_years, fin, dps_info,
                          need_cycle=False):
    """第 23 章：识别六种假高股息。返回提示列表。

    注意：「周期顶部型」只在确实需要周期判断的资产上检查，
    稳定资产（银行/水电/高速/通信）的静态与保守 DPS 天然接近，不适用该规则。
    """
    flags = []

    # 1. 周期顶部型：仅在周期资产上检查
    if need_cycle and static_yield and pricing_yield and static_yield > pricing_yield * 1.35:
        flags.append("周期顶部型：静态股息率远高于正常化股息率")

    # 2. 高支付率型
    if fin and div_years:
        years = sorted(div_years.keys())
        ly = years[-1]
        f = fin.get(ly) or {}
        eps = f.get("eps")
        if eps and eps > 0 and div_years[ly] / eps > 0.9:
            flags.append("高支付率型：分红率接近或超过利润")

    # 3. 股价暴跌型：最新分红同比下降但静态股息率奇高
    years = sorted(div_years.keys())
    if len(years) >= 2 and div_years[years[-2]] > 0:
        if div_years[years[-1]] < div_years[years[-2]] * 0.9:
            if static_yield and static_yield > 0.06:
                flags.append("股价暴跌型：分红下滑但股息率虚高")

    # 4. 借债分红型（第 5 章）：经营现金流显著低于净利润且分红率偏高
    if fin and div_years:
        years = sorted(div_years.keys())
        f = fin.get(years[-1]) or {}
        profit = f.get("profit")
        ocf = f.get("ocf")
        eps = f.get("eps")
        if profit and profit > 0 and ocf is not None and ocf < profit * 0.5:
            payout = (div_years[years[-1]] / eps) if (eps and eps > 0) else 0
            if payout > 0.5:
                flags.append("借债分红型：经营现金流明显低于净利润")

    return flags


# ------------------------------------------------------------------ 综合评估
def evaluate(code, name, cat, sub, mode, entry, add, heavy, level, cap,
             need_cycle, quote, div_years, fin, in_book=True):
    """对单只股票做完整评估，返回一条记录。"""
    price = quote.get("price", 0) if quote else 0
    is_bank = cat == "银行"

    dps = compute_dps(div_years, mode, fin)
    pdps = dps["pricing_dps"]

    static_yield = None
    pricing_yield = None
    if price > 0:
        if dps["latest_dps"]:
            static_yield = dps["latest_dps"] / price
        if pdps:
            pricing_yield = pdps / price

    tp = three_tranches(pdps, entry, add, heavy)
    state, state_label, depth, tranche = judge_state(price, dps, entry, add, heavy)
    risk_level, notes, veto = safety_check(div_years, fin, is_bank, level)
    flags = fake_high_yield_flags(
        static_yield, pricing_yield, div_years, fin, dps, need_cycle=need_cycle
    )

    # 假高股息降级（第 23 章）：命中识别信号的，不允许直接进入重仓/加仓档
    if flags and state in ("heavy", "add"):
        state, state_label = "entry", "首仓档（需核实）"
        notes = list(notes) + ["命中假高股息信号，档位已降级，买入前须人工复核"]
    elif flags and state == "entry":
        state_label = "首仓档（需核实）"

    # 扩展池标的：价格达到买点也不直接给结论，需人工核对基本面
    if not in_book and state in ("heavy", "add", "entry"):
        state_label = state_label.replace("档", "档（扩展池）")

    # 风控否决：不进入可买清单
    if veto:
        state, state_label = "watch", "风控暂停"
        notes = list(notes) + ["风控否决：不作为买入候选"]

    # 与首仓价的偏离
    gap = None
    if tp["entry_price"] and price > 0:
        gap = (price - tp["entry_price"]) / tp["entry_price"]

    return {
        "code": code,
        "name": name,
        "cat": cat,
        "sub": sub,
        "level": level,
        "cap": cap,
        "need_cycle": need_cycle,
        "in_book": in_book,
        "price": price,
        "change_pct": quote.get("change_pct", 0) if quote else 0,
        "pe": quote.get("pe", 0) if quote else 0,
        "pb": quote.get("pb", 0) if quote else 0,
        "high52": quote.get("high52", 0) if quote else 0,
        "low52": quote.get("low52", 0) if quote else 0,
        "latest_dps": dps["latest_dps"],
        "conservative_dps": dps["conservative_dps"],
        "normalized_dps": dps["normalized_dps"],
        "stress_dps": dps["stress_dps"],
        "pricing_dps": pdps,
        "dps_basis": dps["basis"],
        "dps_note": dps["note"],
        "entry_y": entry,
        "add_y": add,
        "heavy_y": heavy,
        "entry_price": tp["entry_price"],
        "add_price": tp["add_price"],
        "heavy_price": tp["heavy_price"],
        "static_yield": static_yield,
        "pricing_yield": pricing_yield,
        "state": state,
        "state_label": state_label,
        "depth": depth,
        "tranche": tranche,
        "gap": gap,
        "risk_level": risk_level,
        "notes": notes,
        "veto": veto,
        "flags": flags,
    }
