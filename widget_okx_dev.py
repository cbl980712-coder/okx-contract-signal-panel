#!/usr/bin/env python3
"""
OKX Widget Server - 提供OKX永续合约分析
端口: 8765
"""

import json
import threading
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError
import urllib.request
import time

OKX_BASE = "https://www.okx.com"

# ---- OKX symbol 转换 ----
def to_okx_sym(symbol):
    """BTCUSDT → BTC-USDT-SWAP"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    return symbol

def from_okx_sym(okx_sym):
    """BTC-USDT-SWAP → BTCUSDT"""
    parts = okx_sym.split("-")
    if len(parts) == 3 and parts[2] == "SWAP":
        return parts[0] + parts[1]
    return okx_sym

OKX_INTERVAL_MAP = {
    "1m":"1m","3m":"3m","5m":"5m","15m":"15m","30m":"30m",
    "1h":"1H","2h":"2H","4h":"4H","6h":"6H","12h":"12H","1d":"1D","1w":"1W",
}
def to_okx_interval(interval):
    return OKX_INTERVAL_MAP.get(interval, interval)

def smart_round(v, sig=5):
    """根据价格大小自动决定小数位，避免小价格显示0.00"""
    if v == 0:
        return 0
    import math
    digits = sig - int(math.floor(math.log10(abs(v)))) - 1
    digits = max(2, min(digits, 10))
    return round(v, digits)


def _find_confirmed_pivot(highs, lows, wing=2, confirm=2):
    """
    寻找已确认的 pivot low 和 pivot high。
    pivot low[i]：low[i] < 左右各 wing 根，且右边 confirm 根没有继续创新低
    pivot high[i]：high[i] > 左右各 wing 根，且右边 confirm 根没有继续创新高
    返回 (pivot_lows_list, pivot_highs_list)，每个元素是 (index, price)
    最多返回最新 5 个
    """
    n = len(lows)
    pivot_lows = []
    pivot_highs = []
    # 需要 wing 在两边 + confirm 在右边，所以 i 的范围到 n-wing-confirm
    limit = n - wing - confirm
    if limit <= wing:
        return [], []
    for i in range(wing, limit):
        # pivot low 判断
        is_pl = (all(lows[i] <= lows[i - j] for j in range(1, wing + 1)) and
                 all(lows[i] <= lows[i + j] for j in range(1, wing + 1)))
        if is_pl:
            # 确认：右边 confirm 根没有比 lows[i] 更低
            confirmed = all(lows[i + k] >= lows[i] for k in range(1, confirm + 1))
            if confirmed:
                pivot_lows.append((i, lows[i]))
        # pivot high 判断
        is_ph = (all(highs[i] >= highs[i - j] for j in range(1, wing + 1)) and
                 all(highs[i] >= highs[i + j] for j in range(1, wing + 1)))
        if is_ph:
            confirmed = all(highs[i + k] <= highs[i] for k in range(1, confirm + 1))
            if confirmed:
                pivot_highs.append((i, highs[i]))
    # 最多返回最新 5 个
    return pivot_lows[-5:], pivot_highs[-5:]


def _get_4h_bias(symbol):
    """
    获取 4h 级别方向偏置。
    多头偏置：price > EMA20_4h > EMA60_4h，且 EMA20 不在下降中
    空头偏置反过来
    返回 {"bias": "多"/"空"/"中性", "ema20": x, "ema60": x, "price": x}
    """
    try:
        closes_4h, highs_4h, lows_4h, _ = get_klines(symbol, "4h", limit=60)
        if len(closes_4h) < 62:
            return {"bias": "中性"}
        c = closes_4h[:-1]  # 已收盘
        ema20_arr = calc_ema(c, 20)
        ema60_arr = calc_ema(c, 60)
        price = c[-1]
        e20 = ema20_arr[-1]
        e60 = ema60_arr[-1]
        # EMA20 下降中判断：最近3根持续下跌
        e20_declining = (len(ema20_arr) >= 3 and
                         ema20_arr[-1] < ema20_arr[-2] < ema20_arr[-3])
        e20_rising = (len(ema20_arr) >= 3 and
                      ema20_arr[-1] > ema20_arr[-2] > ema20_arr[-3])
        if price > e20 > e60 and not e20_declining:
            bias = "多"
        elif price < e20 < e60 and not e20_rising:
            bias = "空"
        else:
            bias = "中性"
        return {"bias": bias, "ema20": smart_round(e20), "ema60": smart_round(e60), "price": smart_round(price)}
    except Exception:
        return {"bias": "中性"}


def _get_structure_zones(highs, lows, atr, interval):
    """
    计算结构支撑/阻力区间，基于 confirmed pivot。
    返回 {"support_zone": [low, high], "resistance_zone": [low, high],
           "pivot_lows": [...], "pivot_highs": [...],
           "structure_low": price, "structure_high": price}
    """
    c_highs = highs[:-1]  # 已收盘
    c_lows  = lows[:-1]
    pivot_lows, pivot_highs = _find_confirmed_pivot(c_highs, c_lows)

    # 取最新 pivot 价格作为结构锚点
    if pivot_lows:
        structure_low = pivot_lows[-1][1]
    else:
        structure_low = min(c_lows[-20:]) if len(c_lows) >= 20 else min(c_lows)

    if pivot_highs:
        structure_high = pivot_highs[-1][1]
    else:
        structure_high = max(c_highs[-20:]) if len(c_highs) >= 20 else max(c_highs)

    price = c_lows[-1] if c_lows else structure_low

    # zone_width = max(0.35 * atr, price * min_zone_pct)
    if price >= 100:
        min_zone_pct = 0.004
    elif price >= 1:
        min_zone_pct = 0.008
    else:
        min_zone_pct = 0.012

    zone_width = max(0.35 * atr, price * min_zone_pct)

    support_zone    = [smart_round(structure_low - zone_width / 2),
                       smart_round(structure_low + zone_width)]
    resistance_zone = [smart_round(structure_high - zone_width),
                       smart_round(structure_high + zone_width / 2)]

    return {
        "support_zone":    support_zone,
        "resistance_zone": resistance_zone,
        "pivot_lows":      [(idx, smart_round(p)) for idx, p in pivot_lows],
        "pivot_highs":     [(idx, smart_round(p)) for idx, p in pivot_highs],
        "structure_low":   smart_round(structure_low),
        "structure_high":  smart_round(structure_high),
    }


def _find_structure_sl(trend, highs, lows, atr, lookback=20):
    """
    结构止损定位：找最近 lookback 根 K 线中的确认结构低点（多）或高点（空）。
    算法：
      1. 取近 lookback 根已收盘 K 线
      2. 寻找局部极值点（swing low/high）：一个 low[i] 低于其左右各2根
      3. 取最近的2个 swing low/high 中更靠近当前价的那个
      4. 若找不到 swing，退化到 min/max（但至少保证距离 >= 1.5 ATR）
    多单止损 = 结构低点 - atr * buf
    空单止损 = 结构高点 + atr * buf
    buf 比 ATR_BUF 稍大，保证在结构外面
    """
    import math
    wing = 2   # swing 判定窗口
    buf  = 0.8 # ATR缓冲（结构外）

    c_highs = highs[:-1][-lookback:]  # 已收盘
    c_lows  = lows[:-1][-lookback:]

    n = len(c_lows)
    if n < wing * 2 + 1:
        # 数据不足，退化
        sl_anchor = min(c_lows) if trend == "多" else max(c_highs)
    else:
        swings = []
        if trend == "多":
            for i in range(wing, n - wing):
                if all(c_lows[i] <= c_lows[i-j] for j in range(1, wing+1)) and \
                   all(c_lows[i] <= c_lows[i+j] for j in range(1, wing+1)):
                    swings.append(c_lows[i])
        else:
            for i in range(wing, n - wing):
                if all(c_highs[i] >= c_highs[i-j] for j in range(1, wing+1)) and \
                   all(c_highs[i] >= c_highs[i+j] for j in range(1, wing+1)):
                    swings.append(c_highs[i])

        if swings:
            # 取最近两个 swing 中绝对值较小的（更近的结构位，止损更合理）
            recent = swings[-2:] if len(swings) >= 2 else swings
            sl_anchor = max(recent) if trend == "多" else min(recent)
        else:
            # 没有 swing，退化到 min/max
            sl_anchor = min(c_lows) if trend == "多" else max(c_highs)

    if trend == "多":
        sl = smart_round(sl_anchor - atr * buf)
    else:
        sl = smart_round(sl_anchor + atr * buf)

    return sl, sl_anchor




# 中文名 -> 交易对 映射（用户搜中文时自动补全）
CN_NAME_MAP = {
    "比特币": "BTC", "以太坊": "ETH", "以太": "ETH",
    "索拉纳": "SOL", "狗狗币": "DOGE", "柴犬": "SHIB",
    "波卡": "DOT", "链接": "LINK", "雪崩": "AVAX",
    "波场": "TRX", "莱特币": "LTC", "币安币": "BNB",
    "人生": "NIGHT", "夜晚": "NIGHT",
    "OP": "OP", "ARB": "ARB", "PEPE": "PEPE",
    "土狗": "", "聪明钱": "", "鲨鱼": "DOGE",
}

HIGHER_INTERVAL = {
    "5m": "15m",
    "15m": "1h",
    "1h": "4h",
    "4h": "1d",
    "1d": "1w"
}

# ===== 执行层常量 =====
# 15m 改归 trade 模型（允许主交易单）
TRADE_STYLE = {"5m": "scalp", "15m": "trade", "1h": "trade", "4h": "trade", "1d": "trade"}

# 失效K线数（按实际K线时间计，不按HTTP调用次数）
INVALID_BARS = {"5m": 12, "15m": 16, "1h": 12, "4h": 6, "1d": 3}
IS_OKX = True   # OKX版专属，放宽容错参数

SL_PCT_LIMIT = {"scalp": 3.0, "trade": 5.0}

# market直进的sl_pct上限
MARKET_SL_LIMIT = {"5m": 1.5, "15m": 1.8, "1h": 2.5}  # 4h/1d不允许market

EXPECTED_HOLD = {
    "5m":  "15分钟 ~ 1.5小时",
    "15m": "30分钟 ~ 3小时",
    "1h":  "2小时 ~ 12小时",
    "4h":  "半天 ~ 2天",
    "1d":  "1天 ~ 5天",
}

# TP/SL 两套模型参数
# scalp（仅5m）：旧模型
SCALP_MIN_RISK_ATR  = 0.3
SCALP_TP1_RISK_MULT = 1.2;  SCALP_TP2_RISK_MULT = 2.0;  SCALP_TP3_RISK_MULT = 3.0
SCALP_TP1_ATR_FLOOR = 0.8;  SCALP_TP2_ATR_FLOOR = 1.6;  SCALP_TP3_ATR_FLOOR = 2.5
# trade（15m/1h/4h/1d）：新模型
TRADE_MIN_RISK_ATR  = {"5m": 0.3, "15m": 1.5, "1h": 1.5, "4h": 1.8, "1d": 2.0}
TRADE_TP1_RISK_MULT = 1.5;  TRADE_TP2_RISK_MULT = 2.5;  TRADE_TP3_RISK_MULT = 4.0
TRADE_TP1_ATR_FLOOR = 1.3;  TRADE_TP2_ATR_FLOOR = 2.2;  TRADE_TP3_ATR_FLOOR = 3.5

# 最小止损距离（百分比），按波动量级分层，小于此距离的信号降为"仅观察"
# 大币（BTC/ETH类高价）取偏小值，小币/低价币取偏大值
# 实际使用时根据 price 段动态选取
MIN_SL_PCT = {
    "5m":  {"high": 0.005, "mid": 0.008, "low": 0.012},  # scalp
    "15m": {"high": 0.007, "mid": 0.010, "low": 0.015},  # trade
    "1h":  {"high": 0.008, "mid": 0.012, "low": 0.018},
    "4h":  {"high": 0.010, "mid": 0.015, "low": 0.020},
    "1d":  {"high": 0.012, "mid": 0.018, "low": 0.025},
}

def get_min_sl_pct(interval, price):
    """根据周期和价格段动态返回最小止损百分比
    price >= 100  → high（大币）
    price 1~100   → mid
    price < 1     → low（低价小币）
    """
    tiers = MIN_SL_PCT.get(interval, MIN_SL_PCT["15m"])
    if price >= 100:
        return tiers["high"]
    elif price >= 1:
        return tiers["mid"]
    else:
        return tiers["low"]

# 进场锁定期（active后禁止失效判断的K线数）
LOCK_BARS = {"5m": 2, "15m": 2, "1h": 1, "4h": 1, "1d": 1}

# ===== signal_state 追踪器（内存态，重启归零；字段全为基础类型，便于后续JSON持久化） =====
_signal_tracker = {}

def _tracker_key(symbol, interval, trend, signal_grade):
    return (symbol, interval, trend, signal_grade)

def _tracker_get(symbol, interval, trend, signal_grade):
    return _signal_tracker.get(_tracker_key(symbol, interval, trend, signal_grade))

def _tracker_init(symbol, interval, trend, signal_grade, entry_type, entry_zone, entry_confirm):
    """初始化或更新 tracker（pending时累计bar_count；已active/closed不重置）"""
    key = _tracker_key(symbol, interval, trend, signal_grade)
    now = int(time.time())
    rec = _signal_tracker.get(key)
    if rec and rec.get("state") in ("pending", "active"):
        rec["entry_zone"]    = entry_zone if rec["state"] == "pending" else rec.get("entry_zone")
        rec["entry_confirm"] = entry_confirm if rec["state"] == "pending" else rec.get("entry_confirm")
        rec["entry_type"]    = entry_type if rec["state"] == "pending" else rec.get("entry_type")
        rec["bar_count"]     = rec.get("bar_count", 0) + 1
        return rec
    _signal_tracker[key] = {
        "state":            "pending",
        "first_seen_ts":    now,
        "active_ts":        None,
        "entry_trigger_ts": None,
        "lock_bars":        LOCK_BARS.get(interval, 1),
        "lock_until_bar":   None,
        "bar_count":        1,
        "closed_reason":    None,
        "entry_zone":       entry_zone,
        "entry_confirm":    entry_confirm,
        "entry_type":       entry_type,
        # 执行窗口：信号进入A/B主区后的冻结期，期间软条件变化不导致下架
        "exec_window_bars": LOCK_BARS.get(interval, 2),  # 复用 LOCK_BARS 常量
        "exec_window_until": None,  # bar_count <= 此值时处于执行窗口内
    }
    return _signal_tracker[key]

def _tracker_try_activate(symbol, interval, trend, signal_grade, market_price):
    """若 pending 信号价格满足进场条件则切换 active"""
    key = _tracker_key(symbol, interval, trend, signal_grade)
    rec = _signal_tracker.get(key)
    if not rec or rec["state"] != "pending":
        return rec
    et, zone, conf = rec.get("entry_type","pullback"), rec.get("entry_zone"), rec.get("entry_confirm")
    now = int(time.time())
    triggered = False
    if et == "market":
        triggered = True
    elif et == "pullback" and zone:
        # 执行窗口内容忍度放宽至 ±0.2ATR（由 zone 宽度隐含，这里直接判断 ±10% zone 宽度容忍）
        triggered = (zone[0] <= market_price <= zone[1])
    elif et == "breakout" and conf is not None:
        triggered = (market_price >= conf) if trend == "多" else (market_price <= conf)
    if triggered:
        rec["state"]             = "active"
        rec["active_ts"]         = now
        rec["entry_trigger_ts"]  = now
        rec["lock_until_bar"]    = rec["bar_count"] + rec["lock_bars"]
        rec["exec_window_until"] = rec["bar_count"] + rec.get("exec_window_bars", 2)
    return rec

def _is_hard_stale(rec, market_price, atr, trend):
    """执行窗口内只允许硬失效条件让信号下架
    硬失效 = 价格明确破坏 entry_zone 或 breakout 跑远
    软变化（趋势变弱/RSI/结构）在窗口内不算失效
    返回 (is_stale: bool, reason: str)
    """
    if not rec:
        return False, ""
    bar_count = rec.get("bar_count", 0)
    exec_until = rec.get("exec_window_until")
    in_window = (exec_until is not None and bar_count <= exec_until)
    et   = rec.get("entry_type", "pullback")
    zone = rec.get("entry_zone")
    conf = rec.get("entry_confirm")

    if in_window:
        # 窗口内：只检查硬失效
        if et == "pullback" and zone:
            lo, hi = zone
            # 硬失效：价格明确超出失效线（比容忍区间更远）
            # 做多：price < zone[0] - atr*0.5（被明确破坏）
            # 做空：price > zone[1] + atr*0.5
            if trend == "多" and market_price < lo - atr * 0.5:
                return True, f"执行窗口内价格跌破进场区间下沿（失效线），硬失效"
            if trend == "空" and market_price > hi + atr * 0.5:
                return True, f"执行窗口内价格突破进场区间上沿（失效线），硬失效"
        elif et == "breakout" and conf is not None:
            if trend == "多" and market_price > conf + atr * 0.25:
                return True, f"breakout后价格偏离确认位超过0.25ATR，已跑远"
            if trend == "空" and market_price < conf - atr * 0.25:
                return True, f"breakout后价格偏离确认位超过0.25ATR，已跑远"
        # 其他软变化在窗口内全部忽略，不失效
        return False, ""
    else:
        # 窗口结束后：执行窗口耗尽仍未入场 → 失效
        if rec["state"] == "pending":
            invalid_bars = rec.get("lock_bars", 2)  # 重用 lock_bars 作为 invalid_bars
            if bar_count > invalid_bars:
                return True, f"执行窗口结束，已超{invalid_bars}根K线仍未触发入场"
        return False, ""

def _tracker_close(symbol, interval, trend, signal_grade, reason):
    key = _tracker_key(symbol, interval, trend, signal_grade)
    rec = _signal_tracker.get(key)
    if rec:
        rec["state"] = "closed"; rec["closed_reason"] = reason

def _position_management(trend, market_price, entry_price, stop_loss, tp1, tp2, tp3):
    """active 阶段仓位管理：只用价格触发，绝不输出失效相关内容"""
    if not all([entry_price, stop_loss, tp1]):
        return {"action": "持仓观察", "detail": "等待价格信号"}
    risk = abs(entry_price - stop_loss)
    if risk <= 0:
        return {"action": "持仓观察", "detail": "止损设置异常，请检查"}
    be_trigger = (entry_price + risk * 0.8) if trend == "多" else (entry_price - risk * 0.8)
    r_pct = lambda p: round(abs(p - entry_price) / risk * 100, 0)
    if trend == "多":
        if market_price <= stop_loss:
            return {"action": "⚠️ 止损", "detail": f"当前价{market_price} ≤ 止损{stop_loss}，建议立即止损"}
        if tp3 and market_price >= tp3:
            return {"action": "🎯 TP3达标", "detail": f"当前价{market_price}≥TP3 {tp3}，可全平或趋势持有"}
        if tp2 and market_price >= tp2:
            return {"action": "🎯 TP2达标", "detail": f"当前价{market_price}≥TP2 {tp2}，止损上移至TP1 {tp1}，余仓持有"}
        if market_price >= tp1:
            return {"action": "🎯 TP1达标", "detail": f"当前价{market_price}≥TP1 {tp1}，建议减仓30-50%，止损上移至入场价{smart_round(entry_price)}"}
        if market_price >= be_trigger:
            return {"action": "📍 移本建议", "detail": f"浮盈{r_pct(market_price):.0f}%R，建议止损上移至入场价{smart_round(entry_price)}"}
        return {"action": "持仓观察", "detail": f"持仓中，浮动{r_pct(market_price):.0f}%R，止损在{stop_loss}"}
    else:
        if market_price >= stop_loss:
            return {"action": "⚠️ 止损", "detail": f"当前价{market_price} ≥ 止损{stop_loss}，建议立即止损"}
        if tp3 and market_price <= tp3:
            return {"action": "🎯 TP3达标", "detail": f"当前价{market_price}≤TP3 {tp3}，可全平或趋势持有"}
        if tp2 and market_price <= tp2:
            return {"action": "🎯 TP2达标", "detail": f"当前价{market_price}≤TP2 {tp2}，止损下移至TP1 {tp1}，余仓持有"}
        if market_price <= tp1:
            return {"action": "🎯 TP1达标", "detail": f"当前价{market_price}≤TP1 {tp1}，建议减仓30-50%，止损下移至入场价{smart_round(entry_price)}"}
        if market_price <= be_trigger:
            return {"action": "📍 移本建议", "detail": f"浮盈{r_pct(market_price):.0f}%R，建议止损下移至入场价{smart_round(entry_price)}"}
        return {"action": "持仓观察", "detail": f"持仓中，浮动{r_pct(market_price):.0f}%R，止损在{stop_loss}"}


def _calc_entry_model(trend, entry, atr, interval, signal_grade, higher_trend,
                      support, resistance, signal_age_bars, tp1, stop_loss,
                      entry_type_override=None,
                      support_zone=None, resistance_zone=None,
                      structure_low=None, structure_high=None):
    """计算进场模型：entry_type / entry_zone / entry_confirm / execution_tag 等
    升级版：entry_type 默认 pullback，entry_zone 基于结构区间（support_zone/resistance_zone）
    market 只保留给严格条件的 A+ 信号
    """
    trade_style = TRADE_STYLE.get(interval, "intraday")
    invalid_bars = INVALID_BARS.get(interval, 2)
    expected_hold = EXPECTED_HOLD.get(interval, "--")

    # tp1_pct / sl_pct
    tp1_pct = round(abs(tp1 - entry) / entry * 100, 3) if tp1 and entry else None
    sl_pct  = round(abs(stop_loss - entry) / entry * 100, 3) if stop_loss and entry else None
    rr_real = round(abs(tp1 - entry) / abs(stop_loss - entry), 2) if (tp1 and stop_loss and abs(stop_loss - entry) > 0) else None

    # ---- 进场区间：优先用结构 zone，兜底用 ATR ----
    entry_type = "pullback"   # 默认 pullback
    entry_zone_basis = "structure_zone"

    if trend == "多":
        # 多单：entry_zone = support_zone，entry_price = support_zone[1]（上沿）
        if support_zone:
            entry_zone = list(support_zone)
            entry_price_suggested = support_zone[1]
            entry_zone_basis = "support_zone"
        else:
            entry_zone = [smart_round(entry - atr * 0.3), smart_round(entry)]
            entry_price_suggested = smart_round(entry)
            entry_zone_basis = "atr_pullback"
        entry_zone_invalid_price = smart_round(entry_zone[0] - atr * 0.5)
        entry_confirm_candidate = smart_round(resistance + atr * 0.17)
    elif trend == "空":
        # 空单：entry_zone = resistance_zone，entry_price = resistance_zone[0]（下沿）
        if resistance_zone:
            entry_zone = list(resistance_zone)
            entry_price_suggested = resistance_zone[0]
            entry_zone_basis = "resistance_zone"
        else:
            entry_zone = [smart_round(entry), smart_round(entry + atr * 0.3)]
            entry_price_suggested = smart_round(entry)
            entry_zone_basis = "atr_pullback"
        entry_zone_invalid_price = smart_round(entry_zone[1] + atr * 0.5)
        entry_confirm_candidate = smart_round(support - atr * 0.17)
    else:
        return None

    # market 白名单（严格条件：A+信号）
    sl_pct_limit_market = MARKET_SL_LIMIT.get(interval)
    can_market = (
        signal_grade == "A"
        and higher_trend == trend
        and signal_age_bars == 0
        and tp1_pct is not None and tp1_pct >= 1.2
        and sl_pct is not None
        and sl_pct_limit_market is not None
        and sl_pct <= sl_pct_limit_market
        and trade_style != "swing"
    )
    if can_market:
        entry_type = "market"
        entry_zone_basis = "market_direct"
        entry_zone = [smart_round(entry), smart_round(entry)]
        entry_price_suggested = smart_round(entry)

    # ---- execution_tag 优先级链（命中即停止）----
    stale_signal = signal_age_bars > invalid_bars

    # 容忍度判断
    near_entry = False
    if entry_type == "pullback" and entry_zone:
        lo, hi = entry_zone
        near_entry = (lo - atr * 0.2 <= entry <= hi + atr * 0.2)
    elif entry_type == "breakout":
        ec = entry_confirm_candidate
        if ec is not None:
            near_entry = (abs(entry - ec) <= atr * 0.3)
    elif entry_type == "market":
        near_entry = True

    # 级1：已失效
    if stale_signal and not near_entry:
        tag = "已失效"
        reason = f"信号已持续{signal_age_bars}根K线（上限{invalid_bars}根），当前价也已偏离进场区间"
    elif stale_signal and near_entry:
        tag = "轻仓试单"
        reason = f"信号持续{signal_age_bars}根K线（稍超上限{invalid_bars}根），但当前价仍接近进场区间，可轻仓参与"
        stale_signal = False
    elif (sl_pct is not None) and (sl_pct > SL_PCT_LIMIT.get(trade_style, 3.0)):
        tag = "风险过高"
        reason = f"止损幅度{sl_pct:.2f}%，超过{trade_style}上限{SL_PCT_LIMIT.get(trade_style,3.0):.1f}%"
    elif tp1_pct is not None and tp1_pct < 0.6:
        tag = "仅观察"
        reason = f"TP1仅{tp1_pct:.2f}%，利润空间偏小，建议等待更好机会"
    else:
        waiting_tag = ("等待回踩" if entry_type == "pullback" else
                       "等待突破确认" if entry_type == "breakout" else None)
        if trade_style == "scalp":
            tag = waiting_tag if waiting_tag else "轻仓试单"
            reason = (f"scalp快线，TP1 {tp1_pct:.2f}%，严控仓位，等待进入区间后轻仓"
                      if waiting_tag else f"scalp快线，TP1 {tp1_pct:.2f}%，严控仓位，快进快出")
        else:
            rr_ok = (rr_real is not None and rr_real >= 1.1)
            tp_ok = (tp1_pct is not None and tp1_pct >= 1.2)
            if tp_ok and rr_ok and not waiting_tag:
                tag = "主交易单"
                reason = f"TP1 {tp1_pct:.2f}%，止损{sl_pct:.2f}%，RR={rr_real}，时效与结构均通过"
            elif tp1_pct is not None and tp1_pct >= 0.6 and rr_ok:
                tag = waiting_tag if waiting_tag else "轻仓试单"
                reason = (f"TP1 {tp1_pct:.2f}%，等待进入区间后轻仓参与" if waiting_tag
                          else f"TP1 {tp1_pct:.2f}%，RR={rr_real}，空间尚可，轻仓试探")
            elif waiting_tag:
                tag = waiting_tag
                reason = f"TP1 {tp1_pct:.2f}%，等待{tag[2:]}后参与"
            else:
                tag = "轻仓试单"
                reason = f"TP1 {tp1_pct:.2f}%，结构有效，轻仓参考"

    return {
        "entry_type":               entry_type,
        "entry_zone":               entry_zone,
        "entry_zone_low":           entry_zone[0] if entry_zone else None,
        "entry_zone_high":          entry_zone[1] if entry_zone else None,
        "entry_price_suggested":    entry_price_suggested,
        "entry_zone_basis":         entry_zone_basis,
        "entry_zone_invalid_price": entry_zone_invalid_price,
        "entry_zone_invalid_bars":  invalid_bars,
        "entry_confirm":            entry_confirm_candidate if entry_type == "breakout" else None,
        "tp1_pct":                  tp1_pct,
        "sl_pct":                   sl_pct,
        "rr_real":                  rr_real,
        "trade_style":              trade_style,
        "execution_tag":            tag,
        "execution_reason":         reason,
        "signal_age_bars":          signal_age_bars,
        "stale_signal":             stale_signal,
        "expected_hold_time":       expected_hold,
        "structure_low":            structure_low,
        "structure_high":           structure_high,
        "support_zone":             support_zone,
        "resistance_zone":          resistance_zone,
    }


def okx_get(path, params=None, timeout=6):
    """所有对OKX的请求走这里"""
    url = OKX_BASE + path
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent":"okx-widget/1.0","Accept":"application/json"})
    import socket
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    finally:
        socket.setdefaulttimeout(old_timeout)


def get_symbols():
    data = okx_get("/api/v5/public/instruments", {"instType": "SWAP"})
    syms = []
    for s in data.get("data", []):
        if s.get("settleCcy") == "USDT" and s.get("state") == "live":
            converted = from_okx_sym(s["instId"])
            if converted.endswith("USDT"):
                syms.append(converted)
    return syms


def get_klines(symbol, interval, limit=120):
    okx_sym = to_okx_sym(symbol)
    okx_bar = to_okx_interval(interval)
    data = okx_get("/api/v5/market/candles", {"instId": okx_sym, "bar": okx_bar, "limit": str(limit)})
    rows = list(reversed(data.get("data", [])))
    closes  = [float(k[4]) for k in rows]
    highs   = [float(k[2]) for k in rows]
    lows    = [float(k[3]) for k in rows]
    volumes = [float(k[5]) for k in rows]
    return closes, highs, lows, volumes


def calc_ema(prices, period):
    ema = []
    k = 2 / (period + 1)
    for i, p in enumerate(prices):
        if i == 0:
            ema.append(p)
        else:
            ema.append(p * k + ema[-1] * (1 - k))
    return ema


def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def calc_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(len(trs), period)


ATR_BUF = {"5m": 0.35, "15m": 0.35, "1h": 0.5, "4h": 0.8, "1d": 0.8, "1w": 0.8}


def analyze_trend(closes, highs, lows, interval="15m"):
    """趋势分析：使用已收盘K线（忽略最后一根），EMA20/55，趋势强度过滤"""
    # 忽略最后一根未收盘K线
    c = closes[:-1]
    h = highs[:-1]
    l = lows[:-1]

    if len(c) < 56:
        return None  # 数据不足

    ema20 = calc_ema(c, 20)
    ema55 = calc_ema(c, 55)
    rsi   = calc_rsi(c)
    atr   = calc_atr(h, l, c)

    price = c[-1]
    e20   = ema20[-1]
    e55   = ema55[-1]

    # 趋势方向
    if price > e20 > e55:
        raw_trend = "多"
    elif price < e20 < e55:
        raw_trend = "空"
    else:
        raw_trend = "中性"

    # 趋势强度过滤：EMA间距必须 >= ATR*0.2
    ema_gap = abs(e20 - e55)
    ema_strong = (atr > 0) and (ema_gap >= atr * 0.2)
    trend = raw_trend if ema_strong else "中性"

    # 价格结构（最近5根，基于已收盘）
    r_highs = h[-5:]
    r_lows  = l[-5:]
    structure_up = (r_highs[-1] > r_highs[0]) or (r_lows[-1] > r_lows[0])
    structure_dn = (r_highs[-1] < r_highs[0]) or (r_lows[-1] < r_lows[0])
    if trend == "多":
        structure_ok = structure_up
    elif trend == "空":
        structure_ok = structure_dn
    else:
        structure_ok = False

    # 量能（用K线成交量，不额外请求接口）
    vol_ok = False  # 默认False，需要volumes参数，这里占位

    # 支撑阻力
    sorted_lows  = sorted(l[-20:])
    sorted_highs = sorted(h[-20:])
    support    = sum(sorted_lows[:3])  / 3
    resistance = sum(sorted_highs[-3:]) / 3

    # 状态描述
    if rsi > 70:
        state = "超买"
    elif rsi < 30:
        state = "超卖"
    elif trend == "多" and rsi > 50:
        state = "多头延续"
    elif trend == "空" and rsi < 50:
        state = "空头延续"
    elif trend == "多":
        state = "多头偏弱"
    elif trend == "空":
        state = "空头偏弱"
    else:
        state = "震荡"

    return {
        "trend":        trend,
        "raw_trend":    raw_trend,
        "ema_strong":   ema_strong,
        "ema_gap":      round(ema_gap, 6),
        "structure_ok": structure_ok,
        "rsi":          round(rsi, 2),
        "atr":          atr,
        "support":      smart_round(support),
        "resistance":   smart_round(resistance),
        "state":        state,
        "current_price": smart_round(price),
        "ema20":        smart_round(e20),
        "ema55":        smart_round(e55),
    }


def get_oi_change(symbol):
    try:
        ccy = symbol.replace("USDT","")
        data = okx_get("/api/v5/rubik/stat/contracts/open-interest-volume", {"ccy": ccy, "period": "5m"})
        rows = data.get("data", [])
        if len(rows) >= 2:
            oi_now  = float(rows[0][1])
            oi_prev = float(rows[1][1])
            if oi_prev > 0:
                return round((oi_now - oi_prev) / oi_prev * 100, 4)
    except Exception:
        pass
    return 0.0


def get_taker_ratio(symbol):
    try:
        ccy = symbol.replace("USDT","")
        data = okx_get("/api/v5/rubik/stat/taker-volume", {"ccy": ccy, "instType": "CONTRACTS", "period": "5m"})
        rows = data.get("data", [])
        if rows:
            buy  = float(rows[0][1])
            sell = float(rows[0][2])
            if sell > 0:
                return round(buy / sell, 4)
    except Exception:
        pass
    return 1.0


def get_top_position_ratio(symbol):
    try:
        okx_sym = to_okx_sym(symbol)
        data = okx_get("/api/v5/rubik/stat/contracts/long-short-account-ratio-contract-top-trader", {"instId": okx_sym, "period": "5m"})
        rows = data.get("data", [])
        if rows:
            return round(float(rows[0][1]), 4)
    except Exception:
        pass
    return 1.0


def get_funding_rate(symbol):
    try:
        okx_sym = to_okx_sym(symbol)
        data = okx_get("/api/v5/public/funding-rate", {"instId": okx_sym})
        rows = data.get("data", [])
        if rows:
            return round(float(rows[0].get("fundingRate", 0)), 8)
    except Exception:
        pass
    return 0.0


def get_market_price(symbol):
    """OKX 实时标记价格（markPx）"""
    try:
        okx_sym = to_okx_sym(symbol)
        data = okx_get("/api/v5/public/mark-price", {"instId": okx_sym, "instType": "SWAP"}, timeout=3)
        rows = data.get("data", [])
        if rows:
            p = float(rows[0].get("markPx", 0))
            return p if p > 0 else None
    except Exception:
        return None


def analyze(symbol, interval):
    closes, highs, lows, volumes = get_klines(symbol, interval, limit=80)
    chart = [smart_round(c) for c in closes[-60:]]

    current = analyze_trend(closes, highs, lows, interval)
    if not current:
        return {"symbol": symbol, "error": "数据不足"}

    kline_close   = current["current_price"]   # 该周期K线收盘价（仅用于趋势/ATR/结构）
    atr           = current["atr"]
    trend         = current["trend"]
    rsi           = current["rsi"]

    # 统一市场价（markPrice）—— 所有执行层计算基准
    market_price = get_market_price(symbol)
    if market_price is None:
        market_price = kline_close
    market_price = smart_round(market_price)

    # 量能（用已收盘成交量）
    vols    = volumes[:-1]
    vol_ma10 = sum(vols[-10:]) / min(len(vols), 10)
    recent3  = vols[-3:]
    vol_ok   = sum(1 for v in recent3 if v > vol_ma10 * 1.1) >= 2

    # 上级周期
    higher_interval = HIGHER_INTERVAL.get(interval, "1d")
    h_closes, h_highs, h_lows, _ = get_klines(symbol, higher_interval, limit=70)
    higher = analyze_trend(h_closes, h_highs, h_lows, higher_interval)
    higher_trend = higher["trend"] if higher else "中性"

    # 链上数据（详情页保留）
    oi_change = get_oi_change(symbol)
    taker     = get_taker_ratio(symbol)
    top_pos   = get_top_position_ratio(symbol)
    last_fr   = get_funding_rate(symbol)

    fr_status = "偏多" if last_fr > 0.0005 else ("偏空" if last_fr < -0.0005 else "中性")

    # ---- 4h 偏置（新增）----
    bias_4h = _get_4h_bias(symbol)

    # ---- 结构区间：用上级周期（15m用1h，1h用4h）作结构来源 ----
    _struct_iv_map = {"15m": "1h", "1h": "4h", "4h": "1d", "5m": "15m"}
    _struct_iv = _struct_iv_map.get(interval, interval)
    if _struct_iv != interval:
        try:
            _sc, _sh, _sl, _ = get_klines(symbol, _struct_iv, limit=65)
            _st = analyze_trend(_sc, _sh, _sl, _struct_iv)
            _satr = _st["atr"] if _st else atr
            zones = _get_structure_zones(_sh, _sl, _satr, _struct_iv)
            struct_highs_a = _sh; struct_lows_a = _sl; struct_atr_a = _satr
        except Exception:
            zones = _get_structure_zones(highs, lows, atr, interval)
            struct_highs_a = highs; struct_lows_a = lows; struct_atr_a = atr
    else:
        zones = _get_structure_zones(highs, lows, atr, interval)
        struct_highs_a = highs; struct_lows_a = lows; struct_atr_a = atr
    support_zone    = zones.get("support_zone")
    resistance_zone = zones.get("resistance_zone")
    structure_low   = zones.get("structure_low")
    structure_high  = zones.get("structure_high")

    # 信号评分
    _, direction_label, signal_score, higher_ok = _score_signal(
        trend, higher_trend, current["ema_strong"], current["structure_ok"], vol_ok, interval
    )

    # 4h 偏置影响评分
    bias_dir = bias_4h.get("bias", "中性")
    if bias_dir == trend and trend != "中性":
        signal_score += 1  # 同向加分

    support    = current["support"]
    resistance = current["resistance"]
    entry      = market_price          # ← 统一市场价作为执行基准
    highs5     = highs[:-1][-5:]
    lows5      = lows[:-1][-5:]
    min_risk_atr = TRADE_MIN_RISK_ATR.get(interval, 1.5)

    # 用结构区间计算 risk/space 用于分档
    if trend == "多":
        sl_for_grade = smart_round(structure_low - max(0.35 * atr, entry * get_min_sl_pct(interval, entry))) if structure_low else None
        if sl_for_grade is None:
            sl_for_grade, _ = _find_structure_sl("多", highs, lows, atr, lookback=20)
        min_sl_d = entry * get_min_sl_pct(interval, entry)
        if abs(entry - sl_for_grade) < min_sl_d:
            sl_for_grade = smart_round(entry - min_sl_d)
        risk  = max(abs(entry - sl_for_grade), atr * min_risk_atr)
        _res_space = resistance_zone[0] if resistance_zone else resistance
        space = max(_res_space - entry, atr * 1.0) if _res_space > entry else atr * 1.0
    elif trend == "空":
        sl_for_grade = smart_round(structure_high + max(0.35 * atr, entry * get_min_sl_pct(interval, entry))) if structure_high else None
        if sl_for_grade is None:
            sl_for_grade, _ = _find_structure_sl("空", highs, lows, atr, lookback=20)
        min_sl_d = entry * get_min_sl_pct(interval, entry)
        if abs(sl_for_grade - entry) < min_sl_d:
            sl_for_grade = smart_round(entry + min_sl_d)
        risk  = max(abs(sl_for_grade - entry), atr * min_risk_atr)
        _sup_space = support_zone[1] if support_zone else support
        space = max(entry - _sup_space, atr * 1.0) if _sup_space < entry else atr * 1.0
    else:
        risk  = atr * 1.5
        space = atr * 1.0

    # RR 用于分档
    tp1_dist_for_grade = max(risk * 1.2, atr * 0.8)
    rr_for_grade = round(tp1_dist_for_grade / risk, 2) if risk > 0 else 0.0

    cost      = entry * 0.0008 * 2 + atr * 0.05
    tp2_dist_for_grade = max(risk * 2.0, atr * 1.6)
    cost_ok   = (tp1_dist_for_grade > cost * 2) and (tp2_dist_for_grade > cost * 3)

    # 分档
    if direction_label in ("逆势观察", "观察") or trend == "中性":
        signal_grade = "C"
    elif signal_score >= 3 and space >= atr * 1.0 and rr_for_grade >= 1.4 and cost_ok:
        signal_grade = "A"
    elif signal_score >= 2 and higher_ok and space >= atr * 0.7 and rr_for_grade >= 1.1 and cost_ok:
        signal_grade = "B"
    else:
        signal_grade = "C"

    # 4h 逆向降级（A→B，B→C，C不降）
    if bias_dir not in ("中性",) and bias_dir != trend and trend != "中性":
        if signal_grade == "A":
            signal_grade = "B"
        elif signal_grade == "B":
            signal_grade = "C"

    # entry_price：结构区间进场点（zone中部，不再是 markPrice）
    _ez_entry = market_price  # fallback
    if signal_grade in ("A", "B"):
        if trend == "多" and support_zone:
            ez_lo_a, ez_hi_a = support_zone
            entry_price = smart_round(ez_lo_a + (ez_hi_a - ez_lo_a) * 0.6)
        elif trend == "空" and resistance_zone:
            ez_lo_a, ez_hi_a = resistance_zone
            entry_price = smart_round(ez_lo_a + (ez_hi_a - ez_lo_a) * 0.4)
        else:
            entry_price = smart_round(market_price)
        _ez_entry = entry_price
    else:
        entry_price = None
        _ez_entry = market_price

    # 计算最终止盈止损（新版：传入结构区间 + entry_price）
    stop_loss_final, tp1, tp2, tp3, _, _, rr = _calc_tp_sl(
        trend, _ez_entry, struct_atr_a, interval, signal_grade,
        highs=struct_highs_a, lows=struct_lows_a, highs5=highs5, lows5=lows5,
        support_zone=support_zone, resistance_zone=resistance_zone,
        structure_low=structure_low, structure_high=structure_high,
        support=support, resistance=resistance
    )
    stop_loss   = stop_loss_final
    # fallback：不允许 stop_loss/tp1 为 None 或 <= 0
    _min_sl_pct_a = get_min_sl_pct(interval, _ez_entry) if _ez_entry else 0.01
    if not stop_loss or stop_loss <= 0:
        if trend == "多":
            stop_loss = smart_round(_ez_entry * (1 - _min_sl_pct_a)) if _ez_entry else None
        elif trend == "空":
            stop_loss = smart_round(_ez_entry * (1 + _min_sl_pct_a)) if _ez_entry else None
    if not tp1 or tp1 <= 0:
        if trend == "多" and _ez_entry:
            tp1 = smart_round(_ez_entry * 1.01)
        elif trend == "空" and _ez_entry:
            tp1 = smart_round(_ez_entry * 0.99)
    if not tp2 or tp2 <= 0:
        if trend == "多" and tp1 and _ez_entry:
            tp2 = smart_round(tp1 + (tp1 - _ez_entry))
        elif trend == "空" and tp1 and _ez_entry:
            tp2 = smart_round(tp1 - (_ez_entry - tp1))
        else:
            tp2 = None

    # entry_zone
    if trend == "多" and support_zone:
        entry_zone = support_zone
    elif trend == "空" and resistance_zone:
        entry_zone = resistance_zone
    else:
        entry_zone = None

    # ---- v10 增强字段 ----
    ap_score, ap_reasons = _a_plus_score(trend, closes, highs, lows, atr,
                                         current["ema_strong"], risk, signal_grade)
    is_aplus = (
        (signal_grade == "A" and ap_score >= 3) or
        (signal_grade == "B" and ap_score >= 4 and rr_for_grade >= 1.2)
    )
    pos_sug = _position_suggestion(signal_grade, rr_for_grade, risk, atr, ap_score)
    dur_bars, dur_text = _signal_duration(symbol, interval, trend, signal_grade)
    # 更新旧模拟统计（保留兼容）
    _sim_update(symbol, interval, signal_grade, trend,
                entry_price, stop_loss, tp1, tp2, rr_for_grade,
                closes[-10:])
    # Phase A：策略统计 tick（先用 high/low 推进已有记录的状态机）
    if highs and lows and len(highs) >= 1 and len(lows) >= 1:
        _strategy_tick(symbol, interval, float(highs[-1]), float(lows[-1]), int(time.time()))

    # ---- 执行层：进场模型 + signal_state ----
    entry_model    = None
    signal_state   = "none"
    position_mgmt  = None
    in_exec_window = False

    if signal_grade in ("A", "B") and entry_price and stop_loss and tp1:
        age_bars = dur_bars - 1 if dur_bars and dur_bars > 0 else 0

        # 先算 entry_model（含 entry_type / entry_zone 等，传入结构区间）
        entry_model = _calc_entry_model(
            trend, entry, atr, interval, signal_grade, higher_trend,
            support, resistance, age_bars, tp1, stop_loss,
            support_zone=support_zone, resistance_zone=resistance_zone,
            structure_low=structure_low, structure_high=structure_high
        )
        # breakout 判断（前一根K线是否已突破关键位）
        prev_close = closes[-2] if len(closes) >= 2 else None
        if entry_model and prev_close:
            if trend == "空" and prev_close < support:
                entry_model["entry_type"] = "breakout"
                entry_model["entry_zone_basis"] = "support_breakout_confirm"
                entry_model["entry_confirm"] = smart_round(support - atr * 0.17)
            elif trend == "多" and prev_close > resistance:
                entry_model["entry_type"] = "breakout"
                entry_model["entry_zone_basis"] = "resistance_breakout_confirm"
                entry_model["entry_confirm"] = smart_round(resistance + atr * 0.17)

        et   = entry_model["entry_type"] if entry_model else "pullback"
        zone = entry_model.get("entry_zone") if entry_model else None
        conf = entry_model.get("entry_confirm") if entry_model else None

        # 初始化/更新 tracker
        rec = _tracker_init(symbol, interval, trend, signal_grade, et, zone, conf)
        # 首次进入：设置执行窗口截止bar
        if rec.get("exec_window_until") is None:
            rec["exec_window_until"] = rec["bar_count"] + rec.get("exec_window_bars", 2)

        # pending：尝试自动进入 active
        if rec["state"] == "pending":
            rec = _tracker_try_activate(symbol, interval, trend, signal_grade, market_price)

        signal_state   = rec["state"]
        bar_count      = rec.get("bar_count", 0)
        exec_until     = rec.get("exec_window_until", 0)
        in_exec_window = (bar_count <= exec_until)

        if signal_state == "active":
            # ★ active：彻底绕开失效判断，只运行仓位管理
            position_mgmt = _position_management(
                trend, market_price, entry_price, stop_loss, tp1, tp2, tp3
            )
            if position_mgmt["action"].startswith("⚠️ 止损"):
                _tracker_close(symbol, interval, trend, signal_grade, "sl_hit")
                signal_state = "closed"
            elif position_mgmt["action"].startswith("🎯 TP3"):
                _tracker_close(symbol, interval, trend, signal_grade, "tp3_hit")
                signal_state = "closed"
            if entry_model:
                entry_model["stale_signal"]     = False
                entry_model["execution_tag"]    = f"持仓中 · {position_mgmt['action']}"
                entry_model["execution_reason"] = position_mgmt["detail"]

        elif signal_state == "pending":
            # ★ pending：使用硬失效检查（执行窗口内只允许硬条件失效）
            hard_stale, hard_reason = _is_hard_stale(rec, market_price, atr, trend)
            if hard_stale:
                if entry_model:
                    entry_model["stale_signal"]     = True
                    entry_model["execution_tag"]    = "已失效"
                    entry_model["execution_reason"] = hard_reason
            else:
                if entry_model:
                    entry_model["stale_signal"] = False
            # Phase A：非失效的 pending 信号注册到策略统计
            if entry_model and not entry_model.get("stale_signal"):
                _strategy_record(
                    symbol, interval, signal_grade, entry_model.get("execution_tag",""),
                    trend, entry_price, stop_loss, tp1, tp2, tp3,
                    round(abs(tp1-entry_price)/abs(stop_loss-entry_price),2) if (tp1 and stop_loss and abs(stop_loss-entry_price)>0) else None
                )
            # Phase B：模拟自动交易——尝试开仓（pending 信号触发入场时）
            if entry_model and not entry_model.get("stale_signal"):
                exec_tag_b = entry_model.get("execution_tag","")
                if exec_tag_b in ("主交易单","轻仓试单") and signal_state != "active":
                    # 开仓（重复开仓由 _sim_open_position 内部过滤）
                    _sim_open_position(
                        symbol, interval, trend, et,
                        entry_price, stop_loss, tp1, tp2, tp3,
                        signal_grade, exec_tag_b, market_price, strategy="main"
                    )
                    # 反向策略同步开仓（mode=reverse 或 both）
                    rev = _calc_reverse_signal(trend, entry_price, stop_loss, tp1, tp2, tp3)
                    if rev and _sim_account["config"].get("mode") in ("reverse","both"):
                        _sim_open_position(
                            symbol, interval, rev["trend"], et,
                            rev["entry"], rev["sl"], rev["tp1"], rev["tp2"], rev["tp3"],
                            signal_grade, exec_tag_b, market_price, strategy="reverse"
                        )
            # 对 active 信号也推进已有仓位的 tick（价格已进场时持续管理）
            if market_price and highs and lows and len(highs) >= 1:
                _sim_tick_positions(symbol, interval, float(highs[-1]), float(lows[-1]), market_price, int(time.time()))

    # ---- end v10 ----

    # 动态保护提示
    if tp1 and stop_loss:
        protect_tips = (
            f"到达TP1({tp1})后将止损移至保本价({smart_round(entry)})；"
            f"到达TP2({tp2})后将止损移至TP1({tp1})"
        )
    else:
        protect_tips = "当前无开单方案，无需保护"
    protection_status = "未触发"

    # 大白话
    reasons_plain = []
    if higher_trend == "多":
        reasons_plain.append(f"大方向（{higher_interval}）：偏多，大均线向上，价格在上方")
    elif higher_trend == "空":
        reasons_plain.append(f"大方向（{higher_interval}）：偏空，大均线向下，价格在下方")
    else:
        reasons_plain.append(f"大方向（{higher_interval}）：中性，均线纠缠，方向不明")

    momentum = "偏强" if rsi > 55 else ("偏弱" if rsi < 45 else "中性")
    reasons_plain.append(f"当前盘面（{interval}）：{current['state']}，RSI {rsi:.0f}，动能{momentum}")

    if current["ema_strong"]:
        reasons_plain.append(f"EMA趋势明确：EMA20({current['ema20']})与EMA55({current['ema55']})间距充分，趋势确立")
    else:
        reasons_plain.append(f"EMA趋势偏弱：EMA20与EMA55过近，可能处于震荡，信号可信度低")

    if vol_ok:
        reasons_plain.append("量能放大：最近3根K中有2根以上成交量高于均值，资金在推动")
    else:
        reasons_plain.append("量能不足：最近成交量低于均值，缺乏资金推动")

    if current["structure_ok"]:
        reasons_plain.append(f"结构方向明确：高低点{'递增' if trend=='多' else '递减'}，结构支持{'做多' if trend=='多' else '做空'}")
    else:
        reasons_plain.append("结构不明：高低点方向不一致，结构信号弱")

    oi_desc    = "新资金进场" if oi_change > 0.2 else ("仓位在撤退" if oi_change < -0.2 else "仓位平稳")
    taker_desc = "主动买盘强" if taker > 1.05 else ("主动砸盘强" if taker < 0.95 else "买卖力量相当")
    top_desc   = "大户偏多" if top_pos > 1.05 else ("大户偏空" if top_pos < 0.95 else "大户中性")
    reasons_plain.append(f"链上：OI{oi_change:+.2f}%({oi_desc})，Taker{taker:.3f}({taker_desc})，大户{top_pos:.3f}({top_desc})")
    reasons_plain.append(f"资金费率：{last_fr*100:.4f}%，{fr_status}")

    if signal_grade == "A":
        summary = f"【A档】方向明确，空间充足，RR={rr}，可以考虑入场"
    elif signal_grade == "B":
        summary = f"【B档】大周期顺势，条件基本满足，RR={rr}，可轻仓试探"
    elif direction_label == "逆势观察":
        summary = f"【C档-逆势】当前{interval}偏{trend}但大周期({higher_interval})偏{higher_trend}，不建议开单"
    else:
        summary = f"【C档-观察】条件未达标（评分{signal_score}/4，空间{round(space/atr,2) if atr>0 else 0}ATR，RR{rr_for_grade}），不开单"
    reasons_plain.append(summary)

    return {
        "symbol":           symbol,
        "interval":         interval,
        "close":            smart_round(kline_close),    # K线周期收盘价（趋势/ATR信号计算基准）
        "market_price":     market_price,                # 统一市场价（markPrice，执行层基准）
        "trend":            trend,
        "signal_grade":     signal_grade,
        "direction_label":  direction_label,
        "signal_score":     signal_score,
        "rsi":              rsi,
        "atr":              smart_round(atr),
        "support":          smart_round(support),
        "resistance":       smart_round(resistance),
        "state":            current["state"],
        "ema_strong":       current["ema_strong"],
        "structure_ok":     current["structure_ok"],
        "vol_ok":           vol_ok,
        "higher_interval":  higher_interval,
        "higher_trend":     higher_trend,
        "space":            smart_round(space),
        "rr":               rr if signal_grade in ("A","B") else rr_for_grade,
        "summary":          summary,
        "reasons_plain":    reasons_plain,
        "oi_change":        oi_change,
        "taker_ratio":      taker,
        "top_pos_ratio":    top_pos,
        "last_funding_rate": last_fr,
        "funding_state":    fr_status,
        "entry_price":      entry_price,
        "stop_loss":        smart_round(stop_loss) if stop_loss else None,
        "tp1":              smart_round(tp1) if tp1 else None,
        "tp2":              smart_round(tp2) if tp2 else None,
        "tp3":              smart_round(tp3) if tp3 else None,
        "protection_status": protection_status,
        "protect_tips":     protect_tips,
        "chart":            chart,
        "timestamp":        int(time.time()),
        # 结构区间 + 4h 偏置（新增）
        "structure_low":    structure_low,
        "structure_high":   structure_high,
        "support_zone":     support_zone,
        "resistance_zone":  resistance_zone,
        "entry_zone":       entry_zone,
        "entry_price_suggested": entry_price,
        "bias_4h":          bias_4h,
        # v10 新增字段
        "a_plus_score":           ap_score,
        "a_plus_reasons":         ap_reasons,
        "is_aplus":               is_aplus,
        "position_suggestion":    pos_sug,
        "signal_duration_bars":   dur_bars,
        "signal_duration_text":   dur_text,
        # 执行层
        "entry_model":            entry_model,
        "signal_state":           signal_state,
        "in_exec_window":         in_exec_window,
        "position_mgmt":          position_mgmt,
    }


# ========== v10 信号增强层 ==========

# 信号持续时间追踪（独立 dict，不与 signal_state tracker 混用）
_dur_tracker = {}

def _a_plus_score(trend, closes, highs, lows, atr, ema_strong, risk, signal_grade):
    """A+ 优选评分 0-5 分，只对 A/B 档调用"""
    if signal_grade not in ("A", "B"):
        return 0, []
    score = 0; reasons = []
    c = closes[:-1]; h = highs[:-1]; l = lows[:-1]
    price = c[-1]
    def ema_f(p,n):
        k=2/(n+1);e=p[0]
        for v in p[1:]: e=v*k+e*(1-k)
        return e
    e20 = ema_f(c, 20)
    # 1. 延迟确认：前一根K方向与趋势一致
    if len(c) >= 2:
        prev_bull = c[-1] > c[-2]
        if (trend=="多" and prev_bull) or (trend=="空" and not prev_bull):
            score += 1; reasons.append("延迟确认✓")
    # 2. 趋势强度
    if ema_strong:
        score += 1; reasons.append("趋势强✓")
    # 3. 位置合理：偏离EMA20 < ATR*0.5
    if atr > 0 and abs(price - e20) < atr * 0.5:
        score += 1; reasons.append("位置好✓")
    # 4. 非追单：最近K振幅 < ATR*1.2
    if len(h) >= 1 and atr > 0 and (h[-1] - l[-1]) < atr * 1.2:
        score += 1; reasons.append("非追单✓")
    # 5. 结构质量：risk <= ATR*1.0
    if atr > 0 and risk <= atr * 1.0:
        score += 1; reasons.append("结构清晰✓")
    return score, reasons


def _position_suggestion(signal_grade, rr_for_grade, risk, atr, a_plus_sc):
    """仓位建议，展示字段，不影响信号生成"""
    if signal_grade == "C":
        return "谨慎观望"
    if signal_grade == "A" and a_plus_sc >= 3 and rr_for_grade >= 1.3 and risk <= atr * 1.0:
        return "标准仓位"
    return "轻仓试探"


def _signal_duration(symbol, interval, trend, signal_grade):
    """内存追踪信号持续K数，返回 (bars, text)
    注意：使用独立的 _dur_tracker，不与 _signal_tracker 混用
    """
    key = (symbol, interval, trend, signal_grade)
    now = int(time.time())
    bar_secs = {"1m":60,"3m":180,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}.get(interval,900)
    entry = _dur_tracker.get(key)
    if entry:
        # 按实际K线时间计数：每经过一个bar_secs才+1（防止HTTP轮询快速累加）
        elapsed_bars = int((now - entry["first_seen"]) / bar_secs)
        entry["bars"] = max(1, elapsed_bars)
        entry["last_seen"] = now
    else:
        # 清除同 symbol/interval 的其他方向记录
        for k in list(_dur_tracker.keys()):
            if k[0] == symbol and k[1] == interval and k != key:
                del _dur_tracker[k]
        _dur_tracker[key] = {"bars": 1, "first_seen": now, "last_seen": now}
        entry = _dur_tracker[key]
    bars = entry["bars"]
    bar_mins = {"1m":1,"3m":3,"5m":5,"15m":15,"30m":30,"1h":60,"4h":240,"1d":1440}.get(interval,15)
    mins = bars * bar_mins
    if mins < 60:
        text = f"持续约{mins}分钟"
    else:
        h_, m_ = divmod(mins, 60)
        text = f"持续约{h_}小时{m_}分钟" if m_ else f"持续约{h_}小时"
    return bars, f"第{bars}根K · {text}"


# =========================================================================
# ========== Phase A：策略统计对照系统 ==========
# =========================================================================
_strategy_log = []
_strategy_log_lock = threading.Lock()
MAX_STRATEGY_LOG = 200

def _calc_reverse_signal(trend, entry, sl, tp1, tp2, tp3):
    """结构镜像反向信号"""
    if not entry or not sl or not tp1:
        return None
    rev_trend = "空" if trend == "多" else "多"
    rev_entry = entry
    rev_sl    = tp1
    rev_tp1   = sl
    risk_step = abs(entry - sl)
    if rev_trend == "空":
        rev_tp2 = smart_round(rev_tp1 - risk_step)
        rev_tp3 = smart_round(rev_tp1 - risk_step * 2)
    else:
        rev_tp2 = smart_round(rev_tp1 + risk_step)
        rev_tp3 = smart_round(rev_tp1 + risk_step * 2)
    rev_rr = round(abs(rev_tp1 - rev_entry) / abs(rev_sl - rev_entry), 2) if abs(rev_sl - rev_entry) > 0 else None
    return {
        "trend": rev_trend, "entry": rev_entry,
        "sl": rev_sl, "tp1": rev_tp1, "tp2": rev_tp2, "tp3": rev_tp3,
        "rr": rev_rr, "state": "waiting", "result": None,
        "open_ts": None, "close_ts": None, "hold_seconds": None,
    }

def _strategy_record(symbol, interval, signal_grade, exec_tag,
                     trend, entry, sl, tp1, tp2, tp3, rr):
    """注册一条策略统计记录（主+反向）"""
    if exec_tag not in ("主交易单", "轻仓试单"):
        return
    if not entry or not sl or not tp1:
        return
    now = int(time.time())
    with _strategy_log_lock:
        for rec in _strategy_log:
            if (rec["symbol"] == symbol and rec["interval"] == interval
                    and rec["trend"] == trend
                    and rec["state"] in ("waiting", "open")):
                return
        rev = _calc_reverse_signal(trend, entry, sl, tp1, tp2, tp3)
        record = {
            "symbol": symbol, "interval": interval,
            "signal_grade": signal_grade, "exec_tag": exec_tag,
            "trend": trend, "entry": entry, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr,
            "created_ts": now, "state": "waiting",
            "result": None, "open_ts": None, "close_ts": None, "hold_seconds": None,
            "reverse": rev,
        }
        _strategy_log.append(record)
        if len(_strategy_log) > MAX_STRATEGY_LOG:
            active = [r for r in _strategy_log if r["state"] in ("waiting","open")]
            closed = sorted([r for r in _strategy_log if r["state"] == "closed"],
                            key=lambda x: x.get("close_ts") or 0)
            _strategy_log[:] = active + closed[-(MAX_STRATEGY_LOG - len(active)):]

def _tick_one(rec, high, low, current_ts):
    """单条信号状态机 tick"""
    state = rec.get("state", "waiting")
    if state == "closed":
        return
    entry = rec.get("entry"); sl = rec.get("sl"); tp1 = rec.get("tp1")
    tp2   = rec.get("tp2");   tp3 = rec.get("tp3"); trend = rec.get("trend")
    if not entry or not sl or not tp1:
        return
    if state == "waiting":
        if low <= entry <= high:
            rec["state"] = "open"; rec["open_ts"] = current_ts; state = "open"
    if state == "open":
        sl_hit  = (trend=="多" and low<=sl)  or (trend=="空" and high>=sl)
        tp1_hit = (trend=="多" and high>=tp1) or (trend=="空" and low<=tp1)
        tp2_hit = tp2 and ((trend=="多" and high>=tp2) or (trend=="空" and low<=tp2))
        tp3_hit = tp3 and ((trend=="多" and high>=tp3) or (trend=="空" and low<=tp3))
        res = None
        if sl_hit:            res = "sl"     # 止损优先
        elif tp3_hit:         res = "tp3"
        elif tp2_hit:         res = "tp2"
        elif tp1_hit:         res = "tp1"
        if res:
            rec["state"] = "closed"; rec["result"] = res; rec["close_ts"] = current_ts
            rec["hold_seconds"] = current_ts - (rec.get("open_ts") or rec.get("created_ts", current_ts))

def _strategy_tick(symbol, interval, high, low, current_ts):
    """每次 analyze() 后推进信号状态机"""
    with _strategy_log_lock:
        for rec in _strategy_log:
            if rec["symbol"] != symbol or rec["interval"] != interval:
                continue
            _tick_one(rec, high, low, current_ts)
            if rec.get("reverse"):
                _tick_one(rec["reverse"], high, low, current_ts)

def _strategy_stats_slice(records):
    """对一批记录计算统计指标"""
    total = len(records)
    if total == 0:
        return {"total": 0}
    settled  = [r for r in records if r.get("state") == "closed"]
    n_set = len(settled)
    tp1_h = sum(1 for r in settled if r.get("result") == "tp1")
    tp2_h = sum(1 for r in settled if r.get("result") == "tp2")
    tp3_h = sum(1 for r in settled if r.get("result") == "tp3")
    sl_h  = sum(1 for r in settled if r.get("result") == "sl")
    win   = tp1_h + tp2_h + tp3_h
    rrs   = [r["rr"] for r in records if r.get("rr") is not None]
    holds = [r["hold_seconds"] for r in settled if r.get("hold_seconds")]
    wr    = round(win / n_set * 100, 1) if n_set > 0 else None
    ar    = round(sum(rrs)/len(rrs), 2) if rrs else None
    ev    = round((wr/100)*ar - (1-wr/100), 3) if (wr is not None and ar is not None) else None
    return {
        "total": total, "settled": n_set,
        "waiting": sum(1 for r in records if r.get("state")=="waiting"),
        "open":    sum(1 for r in records if r.get("state")=="open"),
        "win_rate": wr, "tp1_rate": round(tp1_h/n_set*100,1) if n_set else None,
        "tp2_rate": round(tp2_h/n_set*100,1) if n_set else None,
        "tp3_rate": round(tp3_h/n_set*100,1) if n_set else None,
        "sl_rate":  round(sl_h/n_set*100,1)  if n_set else None,
        "avg_rr": ar, "avg_hold_s": round(sum(holds)/len(holds)) if holds else None,
        "ev": ev,
        "long_count":  sum(1 for r in records if r.get("trend")=="多"),
        "short_count": sum(1 for r in records if r.get("trend")=="空"),
    }

def get_strategy_stats():
    """返回多维策略统计"""
    with _strategy_log_lock:
        log = list(_strategy_log)
    if not log:
        return {"total": 0, "note": "暂无统计数据（可交易信号出现后自动累积）"}
    overall       = _strategy_stats_slice(log)
    by_interval   = {iv: _strategy_stats_slice([r for r in log if r["interval"]==iv])
                     for iv in ("15m","1h","4h") if any(r["interval"]==iv for r in log)}
    by_exec_tag   = {tg: _strategy_stats_slice([r for r in log if r["exec_tag"]==tg])
                     for tg in ("主交易单","轻仓试单") if any(r["exec_tag"]==tg for r in log)}
    by_direction  = {"多": _strategy_stats_slice([r for r in log if r["trend"]=="多"]),
                     "空": _strategy_stats_slice([r for r in log if r["trend"]=="空"])}
    rev_recs      = [r["reverse"] for r in log if r.get("reverse")]
    rev_overall   = _strategy_stats_slice(rev_recs)
    rev_by_interval = {iv: _strategy_stats_slice([r["reverse"] for r in log
                                                   if r["interval"]==iv and r.get("reverse")])
                        for iv in ("15m","1h","4h") if any(r["interval"]==iv for r in log)}
    me = overall.get("ev"); re = rev_overall.get("ev")
    comparison = {
        "main_ev": me, "rev_ev": re,
        "better": ("主策略" if (me or 0)>=(re or 0) else "反向策略") if (me is not None or re is not None) else None,
        "note": (f"{'主策略' if (me or 0)>=(re or 0) else '反向策略'}期望值更高（差距{round(abs((me or 0)-(re or 0)),3)}）"
                 if (me is not None and re is not None) else "结算数据不足"),
    }
    return {
        "total": len(log),
        "note": f"共{len(log)}条信号，{overall.get('settled',0)}条已结算",
        "overall": overall, "by_interval": by_interval,
        "by_exec_tag": by_exec_tag, "by_direction": by_direction,
        "rev_overall": rev_overall, "rev_by_interval": rev_by_interval,
        "comparison": comparison,
        "records": sorted(log, key=lambda x: x.get("created_ts",0), reverse=True)[:100],
    }


# ========== 旧模拟统计（保留兼容） ==========
_sim_log = []   # 最近50条 A/B 信号快照

def _sim_update(symbol, interval, signal_grade, trend, entry, sl, tp1, tp2, rr, closes_recent):
    """追加/结算模拟信号记录"""
    global _sim_log
    if signal_grade not in ("A","B"):
        return
    now = int(time.time())
    # 结算旧未结算记录
    for rec in _sim_log:
        if rec["symbol"]==symbol and rec["result"] is None and rec["entry"] and rec["sl"] and rec["tp1"]:
            e=float(rec["entry"]); s=float(rec["sl"]); t1=float(rec["tp1"])
            for cv in closes_recent:
                if rec["trend"]=="多":
                    if cv>=t1: rec["result"]="tp1"; break
                    if cv<=s:  rec["result"]="sl";  break
                else:
                    if cv<=t1: rec["result"]="tp1"; break
                    if cv>=s:  rec["result"]="sl";  break
    # 同币同方向同档位不重复追加
    last = next((r for r in reversed(_sim_log) if r["symbol"]==symbol and r["interval"]==interval), None)
    if last and last["trend"]==trend and last["signal_grade"]==signal_grade:
        return
    _sim_log.append({"symbol":symbol,"interval":interval,"signal_grade":signal_grade,
                     "trend":trend,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
                     "rr":rr,"ts":now,"result":None})
    if len(_sim_log) > 50:
        _sim_log = _sim_log[-50:]


def get_sim_stats():
    """返回模拟统计概览"""
    total = len(_sim_log)
    if total == 0:
        return {"total":0,"win_rate":None,"tp1_rate":None,"sl_rate":None,
                "avg_rr":None,"long_count":0,"short_count":0,"pending":0,
                "note":"暂无信号记录（服务启动后自动积累）"}
    settled = [r for r in _sim_log if r["result"] is not None]
    pending = total - len(settled)
    tp1_hits = sum(1 for r in settled if r["result"]=="tp1")
    sl_hits  = sum(1 for r in settled if r["result"]=="sl")
    rrs = [float(r["rr"]) for r in _sim_log if r.get("rr") is not None]
    return {
        "total":       total,
        "pending":     pending,
        "win_rate":    round(tp1_hits/len(settled)*100,1) if settled else None,
        "tp1_rate":    round(tp1_hits/len(settled)*100,1) if settled else None,
        "sl_rate":     round(sl_hits /len(settled)*100,1) if settled else None,
        "avg_rr":      round(sum(rrs)/len(rrs),2) if rrs else None,
        "long_count":  sum(1 for r in _sim_log if r["trend"]=="多"),
        "short_count": sum(1 for r in _sim_log if r["trend"]=="空"),
        "note":        f"已记录{total}条，{len(settled)}条已结算，{pending}条待结算",
    }


# =========================================================================
# ========== Phase B：模拟自动交易系统 ==========
# =========================================================================
# 全内存，重启归零；字段均基础类型便于 JSON 持久化升级
# =========================================================================

# 全局 pending orders 字典：symbol → [pending_order, ...]
_sim_pending_orders: dict = {}
_sim_pending_lock = threading.Lock()


def _sim_check_pending(symbol, market_price):
    """
    扫描 symbol 的 pending orders，判断是否触发成交。
    触发条件：mp 进入 entry_zone 或距 zone 最近边界 < 0.8%。
    过期：以实际时间判断（created_ts + interval_secs × expire_bars），不按调用次数。
    """
    triggered = []
    now_ts = int(time.time())
    with _sim_pending_lock:
        orders = _sim_pending_orders.get(symbol, [])
        remaining = []
        for order in orders:
            ivl = order.get("interval", "15m")
            bar_secs = {"1m":60,"3m":180,"5m":300,"15m":900,"30m":1800,
                        "1h":3600,"4h":14400,"1d":86400}.get(ivl, 900)
            max_live_secs = bar_secs * order.get("expire_bars", 16)
            created_ts    = order.get("created_ts", now_ts)
            if (now_ts - created_ts) > max_live_secs:
                order["status"] = "expired"
                continue  # 按实际时间过期

            ez = order.get("entry_zone")
            if not ez:
                remaining.append(order)
                continue

            ez_lo, ez_hi = ez
            # 触发条件：在 zone 内 OR 距最近边界 < 0.8%（模拟回踩触碰）
            in_zone = ez_lo <= market_price <= ez_hi
            near_zone = False
            trend = order.get("trend", "多")
            if trend == "多":
                # 多单：mp 接近 zone 上沿（价格从上往下回踩）
                near_zone = (ez_lo * 0.992 <= market_price <= ez_hi * 1.002)
            else:
                # 空单：mp 接近 zone 下沿（价格从下往上反弹）
                near_zone = (ez_lo * 0.998 <= market_price <= ez_hi * 1.008)

            if in_zone or near_zone:
                order["status"]     = "triggered"
                order["fill_price"] = order.get("entry_price") or market_price
                triggered.append(order)
            else:
                remaining.append(order)
        _sim_pending_orders[symbol] = remaining
    return triggered


SIM_DEFAULTS = {
    "enabled":          True,   # 默认开启模拟盘
    "mode":             "main",       # main / reverse / both
    "initial_equity":   10000.0,      # USDT
    "margin_per_trade": 100.0,        # 每单保证金
    "leverage":         10,
    "max_positions":    30,   # 最大持仓数
    "allow_same_symbol": False,
    "allow_add_same_dir": False,
    "tp_mode":          "partial",    # partial（分批止盈） / tp1_only
    "sl_mode":          "strict",     # strict / breakeven（保本上移）
    "fee_rate":         0.0005,       # 0.05%
    "slippage_rate":    0.0003,       # 0.03%
    "breakeven_r":      0.8,          # 触达 0.8R 后移本
}

_sim_account = {
    "config":            dict(SIM_DEFAULTS),
    "total_equity":      SIM_DEFAULTS["initial_equity"],
    "available_balance": SIM_DEFAULTS["initial_equity"],
    "used_margin":       0.0,
    "unrealized_pnl":    0.0,
    "realized_pnl":      0.0,
    "max_drawdown":      0.0,
    "peak_equity":       SIM_DEFAULTS["initial_equity"],
    "liquidation_count": 0,
    "open_positions":    [],     # 当前持仓列表
    "closed_positions":  [],     # 历史平仓列表（最近200条）
    "today_pnl":         0.0,
    "today_date":        "",     # YYYY-MM-DD
}
_sim_account_lock = threading.Lock()


def _sim_apply_config(cfg: dict):
    """更新模拟交易配置并重置账户（如果 reset=True）"""
    with _sim_account_lock:
        reset = cfg.pop("reset", False)
        for k, v in cfg.items():
            if k in _sim_account["config"]:
                # fee_rate 前端传的是百分比形式（如0.10表示0.1%），需转为小数
                if k == "fee_rate" and v is not None:
                    v = float(v) / 100.0 if float(v) > 0.1 else float(v)
                _sim_account["config"][k] = v
        if reset:
            init_eq = _sim_account["config"]["initial_equity"]
            _sim_account.update({
                "total_equity": init_eq, "available_balance": init_eq,
                "used_margin": 0.0, "unrealized_pnl": 0.0,
                "realized_pnl": 0.0, "max_drawdown": 0.0,
                "peak_equity": init_eq, "liquidation_count": 0,
                "open_positions": [], "closed_positions": [],
                "today_pnl": 0.0, "today_date": "",
            })


def _sim_liquidation_price(trend, entry, leverage):
    """近似爆仓价"""
    if leverage <= 0:
        return None
    if trend == "多":
        return smart_round(entry * (1 - 1 / leverage))
    else:
        return smart_round(entry * (1 + 1 / leverage))


def _sim_open_position(symbol, interval, trend, entry_type,
                       entry_price, stop_loss, tp1, tp2, tp3,
                       signal_grade, exec_tag, market_price, strategy="main"):
    """尝试开仓：检查资金/持仓限制，成功则扣保证金并写入 open_positions"""
    with _sim_account_lock:
        cfg = _sim_account["config"]
        if not cfg["enabled"]:
            return False, "模拟系统未启用"

        # 策略模式过滤
        if cfg["mode"] == "main" and strategy != "main":
            return False, "当前仅主策略模式"
        if cfg["mode"] == "reverse" and strategy != "reverse":
            return False, "当前仅反向策略模式"

        avail = _sim_account["available_balance"]
        margin = cfg["margin_per_trade"]
        lev    = cfg["leverage"]
        fee_r  = cfg["fee_rate"]
        slip_r = cfg["slippage_rate"]

        if avail < margin:
            return False, "可用余额不足"
        if len(_sim_account["open_positions"]) >= cfg["max_positions"]:
            return False, "达到最大持仓数"

        # 同币种检查：反向策略(strategy=reverse)与主策略(main)共存同一symbol是允许的
        same_sym = [p for p in _sim_account["open_positions"] if p["symbol"] == symbol]
        same_strategy_sym = [p for p in same_sym if p["strategy"] == strategy]
        if same_strategy_sym and not cfg["allow_same_symbol"]:
            return False, "禁止同策略同币种重复开仓"
        # 同策略同方向加仓检查
        same_dir = [p for p in same_strategy_sym if p["trend"] == trend]
        if same_dir and not cfg["allow_add_same_dir"]:
            return False, "禁止同方向加仓"

        # 成交价 = market_price ± slippage
        slip = market_price * slip_r
        fill_price = smart_round(market_price + slip if trend == "空" else market_price - slip)

        # 名义仓位 & 数量
        notional  = margin * lev
        qty       = notional / fill_price

        # 手续费（开仓）
        open_fee = notional * fee_r

        # 扣保证金+手续费
        _sim_account["available_balance"] -= (margin + open_fee)
        _sim_account["used_margin"]       += margin

        liq_price = _sim_liquidation_price(trend, fill_price, lev)
        now = int(time.time())

        pos = {
            "id":            now,
            "symbol":        symbol,
            "interval":      interval,
            "strategy":      strategy,        # main / reverse
            "signal_grade":  signal_grade,
            "exec_tag":      exec_tag,
            "trend":         trend,
            "entry_price":   fill_price,
            "stop_loss":     stop_loss,
            "sl_moved":      False,           # 是否已移本
            "tp1":           tp1,
            "tp2":           tp2,
            "tp3":           tp3,
            "tp1_closed":    False,
            "tp2_closed":    False,
            "leverage":      lev,
            "margin":        margin,
            "notional":      notional,
            "qty":           round(qty, 8),
            "qty_remaining": round(qty, 8),
            "open_fee":      round(open_fee, 4),
            "slippage":      round(slip, 6),
            "open_ts":       now,
            "close_ts":      None,
            "realized_pnl":  0.0,
            "liquidated":    False,
            "liq_price":     liq_price,
        }
        _sim_account["open_positions"].append(pos)
        return True, pos


def _sim_close_partial(pos, qty_to_close, close_price, reason, fee_rate, slippage_rate):
    """平仓部分或全部，返回 realized_pnl"""
    slip = close_price * slippage_rate
    fill = smart_round(close_price - slip if pos["trend"] == "多" else close_price + slip)
    pnl_per_unit = (fill - pos["entry_price"]) if pos["trend"] == "多" else (pos["entry_price"] - fill)
    pnl  = pnl_per_unit * qty_to_close
    fee  = fill * qty_to_close * fee_rate
    net  = round(pnl - fee, 4)
    pos["qty_remaining"] = round(pos["qty_remaining"] - qty_to_close, 8)
    pos["realized_pnl"]  = round(pos["realized_pnl"] + net, 4)
    return net, fill


def _sim_tick_positions(symbol, interval, high, low, current_price, current_ts):
    """每次 analyze() 后推进所有相关持仓的 TP/SL/爆仓逻辑"""
    with _sim_account_lock:
        cfg  = _sim_account["config"]
        fee  = cfg["fee_rate"]
        slip = cfg["slippage_rate"]
        be_r = cfg["breakeven_r"]
        to_close = []   # (pos_id, 原因)

        for pos in list(_sim_account["open_positions"]):
            if pos["symbol"] != symbol or pos["interval"] != interval:
                continue
            trend = pos["trend"]
            ep    = pos["entry_price"]
            sl    = pos["stop_loss"]

            # 爆仓检查（优先）
            liq = pos.get("liq_price")
            if liq:
                liqd = (trend == "多" and low <= liq) or (trend == "空" and high >= liq)
                if liqd:
                    pos["liquidated"] = True
                    pnl, _ = _sim_close_partial(pos, pos["qty_remaining"], liq, "liquidation", fee, slip)
                    to_close.append((pos, "liquidation", pnl))
                    continue

            # 止损检查（优先于止盈）
            sl_hit = (trend == "多" and low <= sl) or (trend == "空" and high >= sl)
            if sl_hit:
                pnl, _ = _sim_close_partial(pos, pos["qty_remaining"], sl, "sl", fee, slip)
                to_close.append((pos, "sl", pnl))
                continue

            # 保本上移检查（0.8R）
            risk = abs(ep - pos["stop_loss"])
            if cfg["sl_mode"] == "breakeven" and not pos["sl_moved"] and risk > 0:
                be_price = smart_round(ep + risk * be_r if trend == "多" else ep - risk * be_r)
                be_touched = (trend == "多" and high >= be_price) or (trend == "空" and low <= be_price)
                if be_touched:
                    pos["stop_loss"] = ep   # 移本
                    pos["sl_moved"]  = True

            # TP1（平50%）
            tp1 = pos.get("tp1")
            if tp1 and not pos["tp1_closed"]:
                tp1_hit = (trend == "多" and high >= tp1) or (trend == "空" and low <= tp1)
                if tp1_hit:
                    if cfg["tp_mode"] == "tp1_only":
                        pnl, _ = _sim_close_partial(pos, pos["qty_remaining"], tp1, "tp1", fee, slip)
                        to_close.append((pos, "tp1", pnl))
                        continue
                    else:
                        qty50 = round(pos["qty_remaining"] * 0.5, 8)
                        pnl, _ = _sim_close_partial(pos, qty50, tp1, "tp1", fee, slip)
                        pos["tp1_closed"] = True
                        _sim_account["realized_pnl"]      += pnl
                        _sim_account["total_equity"]      += pnl
                        _sim_account["available_balance"] += pnl
                        _update_today_pnl(pnl)

            # TP2（平30%）
            tp2 = pos.get("tp2")
            if tp2 and pos["tp1_closed"] and not pos["tp2_closed"]:
                tp2_hit = (trend == "多" and high >= tp2) or (trend == "空" and low <= tp2)
                if tp2_hit:
                    qty30 = round(pos["qty_remaining"] * (0.3 / 0.5), 8)  # 剩余的60%
                    pnl, _ = _sim_close_partial(pos, qty30, tp2, "tp2", fee, slip)
                    pos["tp2_closed"] = True
                    _sim_account["realized_pnl"]      += pnl
                    _sim_account["total_equity"]      += pnl
                    _sim_account["available_balance"] += pnl
                    _update_today_pnl(pnl)

            # TP3（平剩余全部）
            tp3 = pos.get("tp3")
            if tp3 and pos["tp1_closed"] and pos["tp2_closed"]:
                tp3_hit = (trend == "多" and high >= tp3) or (trend == "空" and low <= tp3)
                if tp3_hit:
                    pnl, _ = _sim_close_partial(pos, pos["qty_remaining"], tp3, "tp3", fee, slip)
                    to_close.append((pos, "tp3", pnl))
                    continue

        # 处理需要完全平仓的仓位
        for pos, reason, pnl in to_close:
            _sim_account["open_positions"].remove(pos)
            pos["close_ts"]  = current_ts
            pos["close_reason"] = reason
            _sim_account["realized_pnl"]      += pnl
            _sim_account["total_equity"]      += pnl
            _sim_account["available_balance"] += (pos["margin"] + pnl)
            _sim_account["used_margin"]       -= pos["margin"]
            if reason == "liquidation":
                _sim_account["liquidation_count"] += 1
                _sim_account["available_balance"] -= pos["margin"]  # 爆仓损失全部保证金
                _sim_account["total_equity"]      -= pos["margin"]
            _update_today_pnl(pnl)
            # 更新峰值和最大回撤
            eq = _sim_account["total_equity"]
            if eq > _sim_account["peak_equity"]:
                _sim_account["peak_equity"] = eq
            dd = (_sim_account["peak_equity"] - eq) / _sim_account["peak_equity"] * 100 if _sim_account["peak_equity"] > 0 else 0
            if dd > _sim_account["max_drawdown"]:
                _sim_account["max_drawdown"] = round(dd, 2)
            # 归档
            _sim_account["closed_positions"].append(pos)
            if len(_sim_account["closed_positions"]) > 200:
                _sim_account["closed_positions"] = _sim_account["closed_positions"][-200:]
            # 权益归零则停止
            if _sim_account["total_equity"] <= 0:
                _sim_account["config"]["enabled"] = False
                _sim_account["total_equity"] = 0

        # 计算浮动盈亏
        upnl = 0.0
        for pos in _sim_account["open_positions"]:
            pnl_per = (current_price - pos["entry_price"]) if pos["trend"] == "多" else (pos["entry_price"] - current_price)
            upnl += pnl_per * pos["qty_remaining"]
        _sim_account["unrealized_pnl"] = round(upnl, 4)


def _update_today_pnl(pnl):
    """累计今日盈亏"""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if _sim_account["today_date"] != today:
        _sim_account["today_date"] = today
        _sim_account["today_pnl"]  = 0.0
    _sim_account["today_pnl"] = round(_sim_account["today_pnl"] + pnl, 4)


def get_sim_account():
    """返回当前虚拟账户快照（供 API），实时用 markPrice 刷新浮盈"""
    with _sim_account_lock:
        cfg  = _sim_account["config"]
        ops  = list(_sim_account["open_positions"])
        cls  = list(_sim_account["closed_positions"])
        settled = [p for p in cls if not p.get("liquidated")]
        wins    = [p for p in settled if p.get("realized_pnl", 0) > 0]
        win_rate = round(len(wins) / len(settled) * 100, 1) if settled else None
        total_fee = sum(p.get("open_fee",0) for p in cls+ops)

        # 实时拉 markPrice 重算每仓浮盈
        total_upnl = 0.0
        for pos in ops:
            try:
                mp = get_market_price(pos["symbol"])
                if mp and mp > 0:
                    pnl_per = (mp - pos["entry_price"]) if pos["trend"] == "多" else (pos["entry_price"] - mp)
                    upnl_pos = round(pnl_per * pos["qty_remaining"], 4)
                    upnl_pct = round(pnl_per / pos["entry_price"] * 100 * pos["leverage"], 2) if pos["entry_price"] > 0 else 0
                    pos["_cur_price"]   = mp
                    pos["_upnl"]        = upnl_pos
                    pos["_upnl_pct"]    = upnl_pct
                    total_upnl += upnl_pos
                else:
                    pos["_cur_price"]   = pos.get("_cur_price", pos["entry_price"])
                    pos["_upnl"]        = pos.get("_upnl", 0.0)
                    pos["_upnl_pct"]    = pos.get("_upnl_pct", 0.0)
                    total_upnl += pos.get("_upnl", 0.0)
            except Exception:
                pos["_upnl"] = 0.0
                pos["_upnl_pct"] = 0.0
        _sim_account["unrealized_pnl"] = round(total_upnl, 4)

        return {
            "enabled":           cfg["enabled"],
            "config":            dict(cfg),
            "total_equity":      round(_sim_account["total_equity"] + total_upnl, 2),
            "available_balance": round(_sim_account["available_balance"], 2),
            "used_margin":       round(_sim_account["used_margin"], 2),
            "unrealized_pnl":    round(total_upnl, 4),
            "realized_pnl":      round(_sim_account["realized_pnl"], 4),
            "today_pnl":         round(_sim_account["today_pnl"] + total_upnl, 4),
            "max_drawdown":      _sim_account["max_drawdown"],
            "peak_equity":       round(_sim_account["peak_equity"], 2),
            "liquidation_count": _sim_account["liquidation_count"],
            "open_count":        len(ops),
            "closed_count":      len(cls),
            "win_rate":          win_rate,
            "total_fee":         round(total_fee, 4),
            "open_positions":    ops,
            "closed_positions":  cls[-50:],
        }


def _score_signal(trend, higher_trend, ema_strong, structure_ok, vol_ok, interval):
    """统一信号评分逻辑（analyze和_layer2_full共用）
    返回：signal_grade, direction_label, signal_score, higher_ok
    """
    higher_ok = (trend == higher_trend and trend != "中性")
    opposite  = (trend != "中性" and higher_trend != "中性" and trend != higher_trend)

    if opposite:
        return "C", "逆势观察", 0, False

    if trend == "中性":
        return "C", "观察", 0, False

    ema_ok       = ema_strong
    signal_score = sum([higher_ok, ema_ok, structure_ok, vol_ok])

    direction_label = "做多" if trend == "多" else "做空"
    # 暂返回score，分档在调用方做（需要space/RR）
    return None, direction_label, signal_score, higher_ok


def _calc_tp_sl(trend, entry, atr, interval, signal_grade,
                highs=None, lows=None, highs5=None, lows5=None,
                support_zone=None, resistance_zone=None,
                structure_low=None, structure_high=None,
                support=None, resistance=None,
                pivot_highs=None, pivot_lows=None):
    """统一止损止盈计算（升级版：结构止损 + 结构TP）
    止损锚点用 structure_low/structure_high + buffer
    TP1 优先用结构区间前沿，兜底用 ATR 倍数
    返回 stop_loss, tp1, tp2, tp3, risk, space, rr
    """
    is_scalp = (interval == "5m")
    min_risk_atr = SCALP_MIN_RISK_ATR if is_scalp else TRADE_MIN_RISK_ATR.get(interval, 1.5)

    # fallback support/resistance
    if support is None:
        support = structure_low if structure_low else entry * 0.98
    if resistance is None:
        resistance = structure_high if structure_high else entry * 1.02

    min_sl_pct_val = get_min_sl_pct(interval, entry)
    buffer = max(0.5 * atr, entry * min_sl_pct_val)

    # ---- 止损：用 structure_low/structure_high + buffer ----
    if trend == "多":
        if structure_low is not None:
            stop_loss = smart_round(structure_low - buffer)
        elif highs is not None and lows is not None:
            stop_loss, _ = _find_structure_sl("多", highs, lows, atr, lookback=20)
        elif lows5:
            stop_loss = smart_round(min(lows5) - atr * 0.8)
        else:
            stop_loss = smart_round(support - atr * 0.8)
        risk = max(abs(entry - stop_loss), atr * min_risk_atr)
        min_sl = entry * min_sl_pct_val
        if abs(entry - stop_loss) < min_sl:
            stop_loss = smart_round(entry - min_sl)
            risk = max(abs(entry - stop_loss), atr * min_risk_atr)
        # space：用结构高点计算
        _res_for_space = (resistance_zone[0] if resistance_zone else resistance)
        space = max(_res_for_space - entry, atr * 1.0) if _res_for_space > entry else atr * 1.0

    elif trend == "空":
        if structure_high is not None:
            stop_loss = smart_round(structure_high + buffer)
        elif highs is not None and lows is not None:
            stop_loss, _ = _find_structure_sl("空", highs, lows, atr, lookback=20)
        elif highs5:
            stop_loss = smart_round(max(highs5) + atr * 0.8)
        else:
            stop_loss = smart_round(resistance + atr * 0.8)
        risk = max(abs(stop_loss - entry), atr * min_risk_atr)
        min_sl = entry * min_sl_pct_val
        if abs(stop_loss - entry) < min_sl:
            stop_loss = smart_round(entry + min_sl)
            risk = max(abs(stop_loss - entry), atr * min_risk_atr)
        _sup_for_space = (support_zone[1] if support_zone else support)
        space = max(entry - _sup_for_space, atr * 1.0) if _sup_for_space < entry else atr * 1.0

    else:
        return None, None, None, None, atr * 0.3, atr * 1.0, 0.0

    if signal_grade not in ("A", "B"):
        return None, None, None, None, risk, space, 0.0

    # ---- 止盈：TP1/TP2 必须来自结构 swing high/low ----
    def _pick_swing_tp(ph, pl, tr, ep, sl_p):
        """
        选出满足 RR>=1.0 且 TP>=0.5% 的最低/最高 swing 点作为 TP1。
        ph/pl = [(idx,price)...] 已经按 idx 排序，price可能乱序。
        """
        _r = abs(ep - sl_p) if sl_p else 0
        if _r <= 0:
            return None, None
        if tr == "多":
            cands = sorted(
                [pv for _, pv in (ph or []) if pv > ep and (pv - ep) / ep >= 0.005],
            )
            # 找满足 RR>=1.0 的最低目标
            tp1_p = next((pv for pv in cands if (pv - ep) / _r >= 1.0), None)
            if tp1_p is None and cands:
                tp1_p = cands[-1]   # 兜底取最高
            tp2_p = next((pv for pv in cands if tp1_p and pv > tp1_p), None)
        else:
            cands = sorted(
                [pv for _, pv in (pl or []) if pv < ep and (ep - pv) / ep >= 0.005],
                reverse=True
            )
            tp1_p = next((pv for pv in cands if (ep - pv) / _r >= 1.0), None)
            if tp1_p is None and cands:
                tp1_p = cands[-1]
            tp2_p = next((pv for pv in cands if tp1_p and pv < tp1_p), None)
        return tp1_p, tp2_p

    tp1_raw, tp2_raw = _pick_swing_tp(pivot_highs, pivot_lows, trend, entry, stop_loss)

    # 兜底：无 pivot 数据时用 resistance_zone / support_zone
    if tp1_raw is None:
        if trend == "多":
            tp1_raw = (resistance_zone[0] if resistance_zone and resistance_zone[0] > entry else
                       (structure_high if structure_high and structure_high > entry else None))
        else:
            tp1_raw = (support_zone[1] if support_zone and support_zone[1] < entry else
                       (structure_low if structure_low and structure_low < entry else None))

    if tp1_raw is None:
        return None, None, None, None, risk, space, 0.0

    tp1 = smart_round(tp1_raw)
    tp2 = smart_round(tp2_raw) if tp2_raw else None
    tp3 = None

    # TP < 0.5% → 丢弃
    if entry and abs(tp1 - entry) / entry * 100 < 0.5:
        return None, None, None, None, risk, space, 0.0

    # RR 只用 TP1 计算
    rr_val = round(abs(tp1 - entry) / risk, 2) if risk > 0 else 0.0
    # rr_val < 1.0 → 丢弃；1.0~1.2 → 返回但 exec_tag 会标"仅观察"
    if rr_val < 1.0:
        return None, None, None, None, risk, space, rr_val

    # 统一价格校验：任何字段 <= 0 都不允许输出
    min_sl_pct_val_check = get_min_sl_pct(interval, entry)
    if stop_loss is None or stop_loss <= 0:
        # fallback SL
        if trend == "多":
            stop_loss = smart_round(entry * (1 - min_sl_pct_val_check))
        else:
            stop_loss = smart_round(entry * (1 + min_sl_pct_val_check))
    if tp1 is None or tp1 <= 0:
        # fallback TP1（保守值）
        if trend == "多":
            tp1 = smart_round(entry * 1.01)
        else:
            tp1 = smart_round(entry * 0.99)
    # tp2 兜底
    if tp2 is None or tp2 <= 0:
        if trend == "多":
            tp2 = smart_round(tp1 + (tp1 - entry))
        else:
            tp2 = smart_round(tp1 - (entry - tp1))
    # 方向校验
    if trend == "多" and not (stop_loss < entry < tp1):
        return None, None, None, None, risk, space, 0.0
    if trend == "空" and not (tp1 < entry < stop_loss):
        return None, None, None, None, risk, space, 0.0

    return stop_loss, tp1, tp2, tp3, risk, space, rr_val

def _layer1_filter(top_n=50):
    """第一层：1次请求拿全市场数据，相对分位数筛选候选池（无K线请求）
    使用相对分位数（前70%分位线）作为最低门槛，避免 OKX 合约池稀少时全部过滤
    top_n 控制主通道数量，bonus通道固定10个（高波动补充）
    """
    tickers = okx_get("/api/v5/market/tickers", {"instType": "SWAP"}, timeout=8)
    candidates = []
    for t in tickers.get("data", []):
        inst = t.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        sym = from_okx_sym(inst)
        try:
            qvol   = float(t.get("volCcy24h", 0))
            price  = float(t.get("last", 0))
            open24 = float(t.get("open24h", price))
            high   = float(t.get("high24h", price))
            low    = float(t.get("low24h", price))
            change_pct = round((price - open24) / open24 * 100, 4) if open24 > 0 else 0
            if price <= 0 or low <= 0:
                continue
            volatility = (high - low) / low * 100
            candidates.append({
                "symbol": sym,
                "price": price,
                "qvol": qvol,
                "change_pct": change_pct,
                "volatility": volatility,
            })
        except Exception:
            continue

    if not candidates:
        return []

    # 按 qvol 排序
    by_vol = sorted(candidates, key=lambda x: -x["qvol"])

    # 相对分位数：取前 70% 的分位线作为最低门槛
    # 即：至少保留前 70% 的合约（按量排序），不会因绝对量值过高而过滤掉
    cutoff_idx = max(1, int(len(by_vol) * 0.70))
    min_qvol = by_vol[cutoff_idx - 1]["qvol"] if cutoff_idx <= len(by_vol) else 0

    # 过滤极低流动性（仅去掉后30%中价格为0或量极低的）
    qualified = [c for c in by_vol if c["qvol"] >= min_qvol or by_vol.index(c) < top_n]

    # 主通道：按 top_n 取
    main_pool = qualified[:top_n]
    main_syms = {c["symbol"] for c in main_pool}

    # 补漏通道：高波动但不在主通道，固定限10个
    bonus = [c for c in qualified[top_n:] if c["volatility"] > 5.0][:10]
    # 如果 bonus 不够，再从全量 by_vol 中补（避免 OKX 稀少时 bonus 为空）
    if len(bonus) < 5:
        extra = [c for c in by_vol if c["symbol"] not in main_syms
                 and c not in bonus][:max(0, 10 - len(bonus))]
        bonus = (bonus + extra)[:10]

    return main_pool + bonus


def _layer2_full(symbol, interval, ticker_data=None):
    """第二层：与详情页同一套算法，只拉K线，量能用成交量（升级版：含4h偏置+结构区间）"""
    try:
        closes, highs, lows, volumes = get_klines(symbol, interval, limit=65)
        higher_interval = HIGHER_INTERVAL.get(interval, "1d")
        h_closes, h_highs, h_lows, _ = get_klines(symbol, higher_interval, limit=65)

        current = analyze_trend(closes, highs, lows, interval)
        higher  = analyze_trend(h_closes, h_highs, h_lows, higher_interval)
        if not current or not higher:
            return None

        trend        = current["trend"]
        higher_trend = higher["trend"]
        atr          = current["atr"]

        # 量能（已收盘成交量）
        vols     = volumes[:-1]
        vol_ma10 = sum(vols[-10:]) / min(len(vols), 10)
        vol_ok   = sum(1 for v in vols[-3:] if v > vol_ma10 * 1.1) >= 2

        _, direction_label, signal_score, higher_ok = _score_signal(
            trend, higher_trend, current["ema_strong"], current["structure_ok"], vol_ok, interval
        )

        support    = current["support"]
        resistance = current["resistance"]
        kline_close = current["current_price"]

        # 统一市场价
        market_p = get_market_price(symbol)
        entry = market_p if market_p else kline_close
        entry = float(entry)

        # ---- 4h 偏置（新增）----
        bias_4h = _get_4h_bias(symbol)

        # 4h 偏置影响 signal_score / signal_grade
        bias_dir = bias_4h.get("bias", "中性")
        if bias_dir == trend and trend != "中性":
            signal_score += 1   # 同向加分
        # signal_grade 降级在分档后处理

        # ---- 结构区间：以1h为主结构来源，仅15m触发时抬高结构级别 ----
        # struct_interval: 15m用1h，1h用4h，其他用自身
        struct_interval_map = {"15m": "1h", "1h": "4h", "4h": "1d", "5m": "15m"}
        struct_interval = struct_interval_map.get(interval, interval)

        if struct_interval != interval:
            try:
                s_closes, s_highs, s_lows, _ = get_klines(symbol, struct_interval, limit=65)
                s_trend = analyze_trend(s_closes, s_highs, s_lows, struct_interval)
                s_atr   = s_trend["atr"] if s_trend else atr
                zones   = _get_structure_zones(s_highs, s_lows, s_atr, struct_interval)
                struct_highs = s_highs
                struct_lows  = s_lows
                struct_atr   = s_atr
            except Exception:
                zones = _get_structure_zones(highs, lows, atr, interval)
                struct_highs = highs
                struct_lows  = lows
                struct_atr   = atr
        else:
            zones = _get_structure_zones(highs, lows, atr, interval)
            struct_highs = highs
            struct_lows  = lows
            struct_atr   = atr

        support_zone    = zones.get("support_zone")
        resistance_zone = zones.get("resistance_zone")
        structure_low   = zones.get("structure_low")
        structure_high  = zones.get("structure_high")
        _pivot_highs    = [(i, pv) for i, pv in zones.get("pivot_highs", [])]
        _pivot_lows     = [(i, pv) for i, pv in zones.get("pivot_lows",  [])]

        highs5 = highs[:-1][-5:]
        lows5  = lows[:-1][-5:]

        # 结构止损估算（用上级结构）
        min_sl_v = entry * get_min_sl_pct(interval, entry)
        s_buf    = max(0.35 * struct_atr, min_sl_v)
        if trend == "多":
            _sl_est = smart_round(structure_low - s_buf) if structure_low else None
            if _sl_est is None:
                _sl_est, _ = _find_structure_sl("多", struct_highs, struct_lows, struct_atr, lookback=20)
            if abs(entry - _sl_est) < min_sl_v:
                _sl_est = smart_round(entry - min_sl_v)
            risk  = max(abs(entry - _sl_est), struct_atr * TRADE_MIN_RISK_ATR.get(interval, 1.5))
            _res_space = resistance_zone[0] if resistance_zone else resistance
            space = max(_res_space - entry, struct_atr * 1.0) if _res_space > entry else struct_atr * 1.0
        elif trend == "空":
            _sl_est = smart_round(structure_high + s_buf) if structure_high else None
            if _sl_est is None:
                _sl_est, _ = _find_structure_sl("空", struct_highs, struct_lows, struct_atr, lookback=20)
            if abs(_sl_est - entry) < min_sl_v:
                _sl_est = smart_round(entry + min_sl_v)
            risk  = max(abs(_sl_est - entry), struct_atr * TRADE_MIN_RISK_ATR.get(interval, 1.5))
            _sup_space = support_zone[1] if support_zone else support
            space = max(entry - _sup_space, struct_atr * 1.0) if _sup_space < entry else struct_atr * 1.0
        else:
            risk  = struct_atr * 1.5
            space = struct_atr * 1.0

        tp1_dist_g = max(risk * 1.2, atr * 0.8)
        tp2_dist_g = max(risk * 2.0, atr * 1.6)
        rr_for_grade = round(tp1_dist_g / risk, 2) if risk > 0 else 0.0
        cost     = entry * 0.0008 * 2 + atr * 0.05
        cost_ok  = (tp1_dist_g > cost * 2) and (tp2_dist_g > cost * 3)

        if direction_label in ("逆势观察", "观察") or trend == "中性":
            signal_grade = "C"
        elif signal_score >= 3 and space >= atr * 1.0 and rr_for_grade >= 1.4 and cost_ok:
            signal_grade = "A"
        elif signal_score >= 2 and higher_ok and space >= atr * 0.7 and rr_for_grade >= 1.1 and cost_ok:
            signal_grade = "B"
        else:
            signal_grade = "C"

        # 4h 逆向降级（A→B，B→C，C不降）
        if bias_dir not in ("中性",) and bias_dir != trend and trend != "中性":
            if signal_grade == "A":
                signal_grade = "B"
            elif signal_grade == "B":
                signal_grade = "C"

        result = {
            "symbol":           symbol,
            "close":            smart_round(kline_close),
            "market_price":     smart_round(entry),
            "trend":            trend,
            "signal_grade":     signal_grade,
            "direction_label":  direction_label,
            "score":            signal_score,
            "rsi":              round(current["rsi"], 1),
            "higher_trend":     higher_trend,
            "space":            smart_round(space),
            "rr":               rr_for_grade if signal_grade in ("A","B") else None,
            "atr":              smart_round(atr),
            "bias_4h":          bias_4h,
            "structure_low":    structure_low,
            "structure_high":   structure_high,
            "support_zone":     support_zone,
            "resistance_zone":  resistance_zone,
        }

        # v10：为扫描层追加 A+/仓位/持续时间/执行层
        if signal_grade in ("A", "B"):
            ap_sc, ap_rs = _a_plus_score(trend, closes, highs, lows, atr,
                                         current["ema_strong"], risk, signal_grade)
            is_ap = (signal_grade=="A" and ap_sc>=3) or (signal_grade=="B" and ap_sc>=4 and rr_for_grade>=1.2)
            dur_b, dur_t = _signal_duration(symbol, interval, trend, signal_grade)
            pos_s = _position_suggestion(signal_grade, rr_for_grade, risk, atr, ap_sc)

            # 计算 invalid_price（结构外+0.5%缓冲，OKX再×1.5）
            _inv_buffer_pct = 0.005  # 0.5% 基础缓冲
            if IS_OKX:
                _inv_buffer_pct = 0.0075  # OKX: 0.75%（×1.5）
            if trend == "多" and structure_low:
                invalid_price_l2 = smart_round(structure_low * (1 - _inv_buffer_pct))
            elif trend == "空" and structure_high:
                invalid_price_l2 = smart_round(structure_high * (1 + _inv_buffer_pct))
            else:
                invalid_price_l2 = None

            # 执行层：使用新版 _calc_tp_sl（含结构区间）
            # entry_zone 基于结构区间，入场点改为 zone 中部
            # entry = zone 中点（0.5），不跟随 markPrice
            if trend == "多":
                entry_zone = support_zone if support_zone else (
                    [smart_round(structure_low * 1.001), smart_round(structure_low * 1.008)] if structure_low else
                    [smart_round(entry * 0.985), smart_round(entry * 0.99)]
                )
                if entry_zone:
                    ez_lo, ez_hi = entry_zone
                    entry_price_scan = smart_round(ez_lo + (ez_hi - ez_lo) * 0.5)
                else:
                    entry_price_scan = smart_round(entry * 0.985)
            else:
                entry_zone = resistance_zone if resistance_zone else (
                    [smart_round(structure_high * 0.992), smart_round(structure_high * 0.999)] if structure_high else
                    [smart_round(entry * 1.01), smart_round(entry * 1.015)]
                )
                if entry_zone:
                    ez_lo, ez_hi = entry_zone
                    entry_price_scan = smart_round(ez_hi - (ez_hi - ez_lo) * 0.5)
                else:
                    entry_price_scan = smart_round(entry * 1.015)

            stop_loss_f, tp1_raw, tp2_raw, tp3_raw, risk_f, space_f, rr_f = _calc_tp_sl(
                trend, entry_price_scan, struct_atr, interval, signal_grade,
                highs=struct_highs, lows=struct_lows,
                support_zone=support_zone, resistance_zone=resistance_zone,
                structure_low=structure_low, structure_high=structure_high,
                support=support, resistance=resistance,
                pivot_highs=_pivot_highs, pivot_lows=_pivot_lows
            )
            sl_raw = stop_loss_f

            tp1_pct_scan = round(abs(tp1_raw - entry_price_scan) / entry_price_scan * 100, 2) if tp1_raw and entry_price_scan else None
            sl_pct_scan  = round(abs(sl_raw - entry_price_scan) / entry_price_scan * 100, 2) if sl_raw and entry_price_scan else None
            rr_scan      = round(abs(tp1_raw - entry_price_scan) / abs(sl_raw - entry_price_scan), 2) if (tp1_raw and sl_raw and entry_price_scan and abs(sl_raw - entry_price_scan) > 0) else None

            trade_style_s = TRADE_STYLE.get(interval, "trade")
            invalid_bars = INVALID_BARS.get(interval, 2)
            if IS_OKX:
                invalid_bars = int(invalid_bars * 1.5) + 2  # OKX: expire_bars * 1.5 + 2
            age_bars     = max(0, dur_b - 1) if dur_b else 0

            # entry_zone 容忍度判断
            if entry_zone:
                ez_lo, ez_hi = entry_zone
                near_entry_s = (ez_lo - atr * 0.5 <= entry <= ez_hi + atr * 0.5)  # 放大容忍范围
            else:
                near_entry_s = True

            # 30分钟绝对保护：信号存活不足30分钟不允许失效
            _sig_key = (symbol, interval, trend, signal_grade)
            _sig_entry = _dur_tracker.get(_sig_key)
            _alive_secs = int(time.time()) - _sig_entry["first_seen"] if _sig_entry else 0
            _min_alive_secs = 1800  # 30分钟
            if IS_OKX:
                _min_alive_secs = 2700  # OKX额外+15分钟=45分钟
            _too_young = _alive_secs < _min_alive_secs

            # stale：时间超限 且 不在zone附近 且 存活≥30分钟
            stale = (age_bars > invalid_bars) and not near_entry_s and not _too_young

            # 扫描层 execution_tag
            # entry 距现价过远（超过threshold）降为观察
            _entry_dist_pct = round(abs(entry_price_scan - entry) / entry * 100, 2) if entry else 0
            _entry_dist_limit = {  # 各价格区间最大容忍距离
                "high": 5.0,  # 主流 <=5%
                "mid":  8.0,  # 中等 <=8%
                "low":  12.0, # 小币 <=12%
            }
            _price_tier = "high" if entry >= 500 else ("mid" if entry >= 1 else "low")
            _entry_too_far = _entry_dist_pct > _entry_dist_limit[_price_tier]

            _rr_fail    = (sl_raw is None or tp1_raw is None)
            _tp_tiny    = (tp1_pct_scan is not None and tp1_pct_scan < 0.5)
            # entry 与现价距离 < 0.3% → 刚生成就能成交，不允许
            _too_close  = (entry_price_scan and entry and
                           abs(entry_price_scan - entry) / entry * 100 < 0.3)

            if stale:
                exec_tag = "已失效"
            elif _rr_fail or _tp_tiny:
                exec_tag = "仅观察"
            elif _too_close:
                exec_tag = "仅观察"
            elif _entry_too_far:
                exec_tag = "仅观察"
            elif sl_pct_scan and sl_pct_scan > SL_PCT_LIMIT.get(trade_style_s, 5.0):
                exec_tag = "风险过高"
            elif rr_scan and rr_scan >= 1.5 and tp1_pct_scan and tp1_pct_scan >= 0.5:
                exec_tag = "主交易单"
            elif rr_scan and rr_scan >= 1.1:
                exec_tag = "轻仓试单"
            else:
                exec_tag = "仅观察"

            result.update({
                "a_plus_score":          ap_sc,
                "a_plus_reasons":        ap_rs,
                "is_aplus":              is_ap,
                "position_suggestion":   pos_s,
                "signal_duration_bars":  dur_b,
                "signal_duration_text":  dur_t,
                "tp1_pct":               tp1_pct_scan,
                "sl_pct":                sl_pct_scan,
                "rr":                    rr_scan if rr_scan else rr_for_grade,
                "trade_style":           trade_style_s,
                "signal_age_bars":       age_bars,
                "stale_signal":          stale,
                "execution_tag":         exec_tag,
                "entry_zone":            entry_zone,
                "entry_price_suggested": entry_price_scan,
                # 供模拟开仓直接使用（统一校验：不允许 0 价格）
                "_sl":   sl_raw  if (sl_raw  and sl_raw  > 0) else None,
                "_tp1":  tp1_raw if (tp1_raw and tp1_raw > 0) else None,
                "_tp2":  tp2_raw if (tp2_raw and tp2_raw > 0) else None,
                "_tp3":  tp3_raw if (tp3_raw and tp3_raw > 0) else None,
                "invalid_price": invalid_price_l2,
            })
        else:
            result.update({
                "a_plus_score": 0, "a_plus_reasons": [],
                "is_aplus": False,
                "position_suggestion": "谨慎观望",
                "signal_duration_bars": None, "signal_duration_text": None,
                "tp1_pct": None, "sl_pct": None, "trade_style": TRADE_STYLE.get(interval,"intraday"),
                "signal_age_bars": 0, "stale_signal": False,
                "entry_zone": None, "entry_price_suggested": None,
            })
        if ticker_data:
            result["change_pct"] = round(ticker_data.get("change_pct", 0), 2)
            result["qvol"]       = ticker_data.get("qvol", 0)
        return result
    except Exception:
        return None


def get_overview(interval="15m", top_n=50):
    """扫描：首页A/B进主区，C进观察区"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        candidates = _layer1_filter(top_n)
    except Exception:
        candidates = []

    if not candidates:
        return {
            "interval": interval, "total_scanned": 0,
            "longs": [], "shorts": [], "longs_watch": [], "shorts_watch": [],
            "timestamp": int(time.time())
        }

    ticker_map = {c["symbol"]: c for c in candidates}
    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {
            ex.submit(_layer2_full, sym, interval, ticker_map.get(sym)): sym
            for sym in ticker_map
        }
        for f in as_completed(futures, timeout=45):
            try:
                r = f.result()
                if r:
                    results.append(r)
            except Exception:
                pass

    grade_order = {"A": 3, "B": 2, "C": 1}

    def sort_key(x):
        return (-grade_order.get(x["signal_grade"], 0), -(x.get("space") or 0), -(x.get("rr") or 0))

    # 主区：A/B且未失效（stale_signal=False）
    def _price_ok(r):
        """价格安全检查：不允许 0 值进入首页"""
        ep  = r.get("entry_price_suggested") or 0
        sl  = r.get("_sl") or 0
        tp1 = r.get("_tp1") or 0
        if ep <= 0 or sl <= 0 or tp1 <= 0:
            return False  # 有0价格，过滤
        trend = r.get("trend")
        if trend == "多"  and not (sl < ep < tp1):
            return False  # 多单方向错
        if trend == "空"  and not (tp1 < ep < sl):
            return False  # 空单方向错
        return True

    longs        = sorted([r for r in results
                           if r["trend"] == "多"  and r["signal_grade"] in ("A","B")
                           and not r.get("stale_signal") and _price_ok(r)],
                          key=sort_key)
    shorts       = sorted([r for r in results
                           if r["trend"] == "空"  and r["signal_grade"] in ("A","B")
                           and not r.get("stale_signal") and _price_ok(r)],
                          key=sort_key)
    # 已失效归档区：原A/B但已失效
    longs_stale  = sorted([r for r in results
                           if r["trend"] == "多"  and r["signal_grade"] in ("A","B") and r.get("stale_signal")],
                          key=sort_key)
    shorts_stale = sorted([r for r in results
                           if r["trend"] == "空"  and r["signal_grade"] in ("A","B") and r.get("stale_signal")],
                          key=sort_key)
    # C档观察区
    longs_watch  = sorted([r for r in results if r["trend"] == "多"  and r["signal_grade"] == "C"], key=sort_key)
    shorts_watch = sorted([r for r in results if r["trend"] == "空"  and r["signal_grade"] == "C"], key=sort_key)

    # A+ 优选列表
    aplus_list = sorted(
        [r for r in results if r.get("is_aplus") and not r.get("stale_signal")],
        key=lambda x: (-x.get("a_plus_score", 0), -(x.get("rr") or 0))
    )

    # ===== Phase B：overview 扫描层直接触发模拟开仓（升级版：entry_zone 挂单）=====
    # 如果信号有 entry_zone，先创建 pending order；当 market_price 进入区间时才开仓
    if _sim_account["config"].get("enabled"):
        tradeable = [r for r in results
                     if r["signal_grade"] in ("A","B")
                     and not r.get("stale_signal")
                     and r.get("execution_tag") in ("主交易单","轻仓试单")]
        for r in tradeable:
            sym_r    = r["symbol"]
            trend_r  = r["trend"]
            entry_r  = r.get("entry_price_suggested") or r.get("market_price") or r.get("close")
            mp_r     = r.get("market_price", entry_r)
            sl_r     = r.get("_sl")
            tp1_r    = r.get("_tp1")
            tp2_r    = r.get("_tp2")
            tp3_r    = r.get("_tp3")
            ez_r     = r.get("entry_zone")

            # fallback：sl/tp 用 pct 反推
            if not sl_r and r.get("sl_pct") and entry_r:
                sl_pct_v = r["sl_pct"] / 100
                sl_r = smart_round(entry_r * (1 - sl_pct_v) if trend_r == "多" else entry_r * (1 + sl_pct_v))
            if not tp1_r and r.get("tp1_pct") and entry_r:
                tp1_pct_v = r["tp1_pct"] / 100
                tp1_r = smart_round(entry_r * (1 + tp1_pct_v) if trend_r == "多" else entry_r * (1 - tp1_pct_v))
            atr_r = r.get("atr", 0)
            if tp1_r and sl_r and entry_r and not tp2_r:
                risk_r = abs(entry_r - sl_r)
                if trend_r == "多":
                    tp2_r = smart_round(entry_r + max(risk_r * TRADE_TP2_RISK_MULT, atr_r * TRADE_TP2_ATR_FLOOR))
                    tp3_r = smart_round(entry_r + max(risk_r * TRADE_TP3_RISK_MULT, atr_r * TRADE_TP3_ATR_FLOOR))
                else:
                    tp2_r = smart_round(entry_r - max(risk_r * TRADE_TP2_RISK_MULT, atr_r * TRADE_TP2_ATR_FLOOR))
                    tp3_r = smart_round(entry_r - max(risk_r * TRADE_TP3_RISK_MULT, atr_r * TRADE_TP3_ATR_FLOOR))

            if entry_r and sl_r and tp1_r:
                if ez_r:
                    # 有 entry_zone → 创建 pending order（如果已有相同 symbol+trend+interval 的 pending，跳过）
                    with _sim_pending_lock:
                        existing = _sim_pending_orders.get(sym_r, [])
                        dup = any(o.get("trend") == trend_r and o.get("interval") == interval
                                  for o in existing)
                        if not dup:
                            pending_order = {
                                "symbol":      sym_r,
                                "interval":    interval,
                                "trend":       trend_r,
                                "entry_zone":  ez_r,
                                "entry_price": entry_r,
                                "sl":          sl_r,
                                "tp1":         tp1_r,
                                "tp2":         tp2_r,
                                "tp3":         tp3_r,
                                "signal_grade": r["signal_grade"],
                                "exec_tag":    r["execution_tag"],
                                "expire_bars": INVALID_BARS.get(interval, 16),  # 跟信号存活期一致
                                "status":      "pending",
                                "created_ts":  int(time.time()),
                            }
                            _sim_pending_orders.setdefault(sym_r, []).append(pending_order)

                    # 检查当前 tick 是否有 pending orders 触发
                    triggered_orders = _sim_check_pending(sym_r, float(mp_r)) if mp_r else []
                    for trig in triggered_orders:
                        _sim_open_position(
                            trig["symbol"], trig["interval"], trig["trend"], "pullback",
                            trig["fill_price"], trig["sl"], trig["tp1"], trig["tp2"], trig["tp3"],
                            trig["signal_grade"], trig["exec_tag"], trig["fill_price"], strategy="main"
                        )
                        if _sim_account["config"].get("mode") in ("reverse","both"):
                            rev = _calc_reverse_signal(trig["trend"], trig["fill_price"],
                                                       trig["sl"], trig["tp1"], trig["tp2"], trig["tp3"])
                            if rev:
                                _sim_open_position(
                                    trig["symbol"], trig["interval"], rev["trend"], "pullback",
                                    rev["entry"], rev["sl"], rev["tp1"], rev["tp2"], rev["tp3"],
                                    trig["signal_grade"], trig["exec_tag"], trig["fill_price"], strategy="reverse"
                                )
                else:
                    # 无 entry_zone（market 类型）→ 直接开仓
                    _sim_open_position(
                        sym_r, interval, trend_r, "market",
                        entry_r, sl_r, tp1_r, tp2_r, tp3_r,
                        r["signal_grade"], r["execution_tag"], entry_r, strategy="main"
                    )
                    if _sim_account["config"].get("mode") in ("reverse","both"):
                        rev = _calc_reverse_signal(trend_r, entry_r, sl_r, tp1_r, tp2_r, tp3_r)
                        if rev:
                            _sim_open_position(
                                sym_r, interval, rev["trend"], "market",
                                rev["entry"], rev["sl"], rev["tp1"], rev["tp2"], rev["tp3"],
                                r["signal_grade"], r["execution_tag"], entry_r, strategy="reverse"
                            )

            # tick 已有仓位
            if mp_r:
                _sim_tick_positions(sym_r, interval,
                                    mp_r * 1.0005,
                                    mp_r * 0.9995,
                                    mp_r,
                                    int(time.time()))
    # ===== end Phase B =====

    return {
        "interval":      interval,
        "total_scanned": len(ticker_map),
        "longs":         longs,
        "shorts":        shorts,
        "longs_stale":   longs_stale,
        "shorts_stale":  shorts_stale,
        "longs_watch":   longs_watch,
        "shorts_watch":  shorts_watch,
        "aplus":         aplus_list,
        "sim_stats":     get_sim_stats(),
        "timestamp":     int(time.time()),
    }


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程HTTP服务器：每个请求独立线程，扫描不阻塞其他请求"""
    daemon_threads = True


class WidgetHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默日志

    def send_json(self, data, status=200):
        # ensure_ascii=True 避免中文字符导致Content-Length计算错误
        body = json.dumps(data, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/health":
            self.send_text("ok")

        elif path == "/download/btc_widget_v10.2.zip":
            zip_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_widget_v10.2.zip")
            if os.path.exists(zip_path):
                with open(zip_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="btc_widget_v10.2.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_text("file not found", 404)

        elif path == "/download/okx_widget_v1.0.zip":
            zip_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "okx_widget_v1.0.zip")
            if os.path.exists(zip_path):
                with open(zip_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", 'attachment; filename="okx_widget_v1.0.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_text("file not found", 404)

        elif path == "/":
            # 返回UI
            # v10_dev：使用开发版 UI
            ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "widget_okx_ui.html")
            if os.path.exists(ui_path):
                with open(ui_path, "r", encoding="utf-8") as f:
                    html = f.read()
                self.send_html(html)
            else:
                self.send_text("widget_okx_ui.html not found", 404)

        elif path == "/api/symbols":
            try:
                symbols = get_symbols()
                # 加入中文别名，方便搜索
                cn_entries = []
                for cn, en in CN_NAME_MAP.items():
                    if en:
                        matches = [s for s in symbols if s.startswith(en+'USDT') or s == en+'USDT']
                        for m in matches:
                            cn_entries.append({"symbol": m, "cn": cn})
                # 把含中文的symbol也加进cn_entries
                for s in symbols:
                    if any('一' <= c <= '鿿' for c in s):
                        cn_entries.append({"symbol": s, "cn": s})
                self.send_json({"symbols": symbols, "cn_map": cn_entries})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/overview":
            interval = params.get("interval", ["15m"])[0]
            top_n = int(params.get("top_n", ["50"])[0])
            try:
                result = get_overview(interval, top_n)
                self.send_json(result)
            except Exception as e:
                import traceback
                self.send_json({"error": str(e), "trace": traceback.format_exc()}, 500)

        elif path == "/api/analyze":
            symbol = params.get("symbol", ["BTCUSDT"])[0].upper()
            interval = params.get("interval", ["15m"])[0]
            try:
                result = analyze(symbol, interval)
                self.send_json(result)
            except Exception as e:
                import traceback
                self.send_json({"error": str(e), "trace": traceback.format_exc()}, 500)

        elif path == "/api/price":
            symbol = params.get("symbol", ["BTCUSDT"])[0].upper()
            try:
                okx_sym = to_okx_sym(symbol)
                tick = okx_get("/api/v5/market/ticker", {"instId": okx_sym}, timeout=3)
                mark = okx_get("/api/v5/public/mark-price", {"instId": okx_sym, "instType": "SWAP"}, timeout=3)
                tick_data = tick.get("data", [{}])[0]
                mark_data = mark.get("data", [{}])[0]
                self.send_json({
                    "symbol":     symbol,
                    "last_price": smart_round(float(mark_data.get("markPx", tick_data.get("last", 0)))),
                    "bid":        smart_round(float(tick_data.get("bidPx", 0))),
                    "ask":        smart_round(float(tick_data.get("askPx", 0))),
                    "ts":         int(time.time() * 1000),
                })
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/strategy_stats":
            try:
                self.send_json(get_strategy_stats())
            except Exception as e:
                import traceback
                self.send_json({"error": str(e), "trace": traceback.format_exc()}, 500)

        elif path == "/api/sim_account":
            try:
                self.send_json(get_sim_account())
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        else:
            self.send_text("Not Found", 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}

        if path == "/api/sim_config":
            try:
                _sim_apply_config(data)
                self.send_json({"ok": True, "config": _sim_account["config"]})
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        else:
            self.send_text("Not Found", 404)


def run_server(port=8765, host="0.0.0.0"):
    server = ThreadedHTTPServer((host, port), WidgetHandler)
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print(f"[BTC Widget] Server running at http://0.0.0.0:{port} (多线程模式)")
    print(f"[BTC Widget] 本机访问: http://127.0.0.1:{port}")
    print(f"[BTC Widget] 局域网访问: http://{local_ip}:{port}")
    server.serve_forever()
    server.serve_forever()


def launch_with_pywebview(port=8765):
    import webview
    window = webview.create_window(
        "BTC Widget",
        f"http://127.0.0.1:{port}",
        width=900,
        height=900,
        resizable=True,
    )
    webview.start()


def launch_with_tkinter_fallback():
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.title("BTC Widget - 缺少依赖")
        root.geometry("400x200")
        label = tk.Label(
            root,
            text="请安装 pywebview 以使用桌面窗口模式：\n\npip install pywebview\n\n或直接浏览器访问：\nhttp://127.0.0.1:8765",
            justify="center",
            pady=20,
        )
        label.pack(expand=True)
        btn = tk.Button(root, text="关闭", command=root.destroy)
        btn.pack(pady=10)
        root.mainloop()
    except Exception:
        print("请安装 pywebview: pip install pywebview")
        print("或直接浏览器访问: http://127.0.0.1:8765")


if __name__ == "__main__":
    PORT = 8766

    # 先启动服务器线程
    t = threading.Thread(target=run_server, args=(PORT,), daemon=True)
    t.start()
    time.sleep(0.5)  # 等待服务器就绪

    # 尝试 pywebview 启动
    try:
        import webview
        print("[BTC Widget] 使用 pywebview 桌面窗口")
        launch_with_pywebview(PORT)
    except ImportError:
        print("[BTC Widget] 未安装 pywebview，使用 tkinter 提示")
        launch_with_tkinter_fallback()
        # tkinter 退出后，服务器仍在后台，保持主线程存活
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n[BTC Widget] 已停止")
