"""
Backtest Engine — runs the NIFTY 50 CPR option trading strategy against historical Upstox data.

P&L MODEL:
  - SL and TP are NIFTY INDEX price levels (same as live bot)
  - P&L = index_points_moved × lots × LOT_SIZE (from settings, currently 65)
  - A single trade loss is HARD CAPPED at DAILY_LOSS_LIMIT
  - If a trade's SL loss would exceed the daily limit, the trade exits
    at the exact price that hits the daily limit instead
  - After any trade closes, if cumulative day P&L <= -DAILY_LOSS_LIMIT,
    no further trades are taken that day
"""
import time
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Any
import logging

from config.settings import settings
from strategies.nifty_cpr_option_strategy import (
    calculate_cpr_levels,
    find_trade_signal,
    get_previous_cpr_widths,
)

logger = logging.getLogger("CPR_System.Backtest")

LOT_SIZE = settings.NIFTY_LOT_SIZE


def _trading_days(start: date, end: date) -> List[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _simulate_exit(
    candles: List[Dict],
    entry_bar_idx: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    trade_type: str,
    lots: int,
    day_realized_pnl: float,
    daily_loss_limit: float,
) -> Dict:
    """
    Walk candles after entry bar to find first SL, TP, or loss-limit hit.

    Loss-limit cap logic:
      remaining_loss_budget = daily_loss_limit - abs(day_realized_pnl)  [always > 0 here]
      max_loss_points = remaining_loss_budget / (lots * LOT_SIZE)
      loss_exit_price = entry ± max_loss_points  (direction depends on trade_type)

    If the loss-limit exit price is hit before normal SL, we exit there instead,
    ensuring the day never loses more than daily_loss_limit in total.
    """
    qty = lots * LOT_SIZE
    remaining_budget = daily_loss_limit - abs(min(day_realized_pnl, 0))
    max_loss_pts = remaining_budget / qty if qty > 0 else float('inf')

    # Price level that would exhaust remaining budget
    if trade_type == "SELL":   # Short — loss when price rises
        loss_limit_price = entry_price + max_loss_pts
        # Effective SL = tighter of normal SL and loss-limit price
        effective_sl = min(stop_loss, loss_limit_price)
    else:                       # Long — loss when price falls
        loss_limit_price = entry_price - max_loss_pts
        effective_sl = max(stop_loss, loss_limit_price)

    squareoff_cutoff = settings.SQUAREOFF_HOUR * 60 + settings.SQUAREOFF_MIN

    for i in range(entry_bar_idx + 1, len(candles)):
        c = candles[i]
        bar_hi   = c["high"]
        bar_lo   = c["low"]
        time_str = c["time"]

        # 3 PM squareoff check
        try:
            cdt = datetime.fromisoformat(time_str.replace("+05:30", ""))
            if cdt.hour * 60 + cdt.minute >= squareoff_cutoff:
                exit_p = c["close"]
                if trade_type == "SELL":
                    pnl = (entry_price - exit_p) * qty
                else:
                    pnl = (exit_p - entry_price) * qty
                return {"exit_price": round(exit_p, 2), "exit_time": time_str,
                        "status": "CLOSED_EOD", "pnl": round(pnl, 2)}
        except Exception:
            pass

        if trade_type == "SELL":
            # SL / loss-limit hit: price rose to or above effective_sl
            if bar_hi >= effective_sl:
                pnl = (entry_price - effective_sl) * qty
                status = "CLOSED_SL_LIMIT" if effective_sl < stop_loss else "CLOSED_SL"
                return {"exit_price": round(effective_sl, 2), "exit_time": time_str,
                        "status": status, "pnl": round(pnl, 2)}
            # TP hit
            if bar_lo <= take_profit:
                pnl = (entry_price - take_profit) * qty
                return {"exit_price": round(take_profit, 2), "exit_time": time_str,
                        "status": "CLOSED_TP", "pnl": round(pnl, 2)}
        else:
            # SL / loss-limit hit: price fell to or below effective_sl
            if bar_lo <= effective_sl:
                pnl = (effective_sl - entry_price) * qty
                status = "CLOSED_SL_LIMIT" if effective_sl > stop_loss else "CLOSED_SL"
                return {"exit_price": round(effective_sl, 2), "exit_time": time_str,
                        "status": status, "pnl": round(pnl, 2)}
            # TP hit
            if bar_hi >= take_profit:
                pnl = (take_profit - entry_price) * qty
                return {"exit_price": round(take_profit, 2), "exit_time": time_str,
                        "status": "CLOSED_TP", "pnl": round(pnl, 2)}

    # EOD — no SL/TP hit, close at last candle
    last_close = candles[-1]["close"]
    last_time  = candles[-1]["time"]
    if trade_type == "SELL":
        pnl = (entry_price - last_close) * qty
    else:
        pnl = (last_close - entry_price) * qty
    return {"exit_price": round(last_close, 2), "exit_time": last_time,
            "status": "CLOSED_EOD", "pnl": round(pnl, 2)}


def run_backtest(
    start_date: date,
    end_date: date,
    upstox_client,
    max_trades_per_day: int = 2,
    daily_loss_limit: float = None,
    failure_window: int = None,
    retest_window: int = None,
    confirm_window: int = None,
    entry_window: int = None,
    retest_tol: float = None,
    sl_buffer: float = None,
    tp_buffer: float = None,
) -> Dict[str, Any]:
    fw  = failure_window  or settings.FAILURE_WINDOW
    rw  = retest_window   or settings.RETEST_WINDOW
    cw  = confirm_window  or settings.CONFIRMATION_WINDOW
    ew  = entry_window    or settings.ENTRY_TRIGGER_WINDOW
    rt  = retest_tol      if retest_tol  is not None else settings.RETEST_TOLERANCE
    dll = daily_loss_limit if daily_loss_limit is not None else settings.DAILY_LOSS_LIMIT
    lots = settings.POSITION_LOTS

    days = _trading_days(start_date, end_date)
    all_trades:    List[Dict] = []
    day_summaries: List[Dict] = []
    skipped_days:  List[str]  = []

    no_entry_cutoff  = settings.NO_ENTRY_AFTER_HOUR * 60 + settings.NO_ENTRY_AFTER_MIN
    squareoff_cutoff = settings.SQUAREOFF_HOUR * 60 + settings.SQUAREOFF_MIN

    for trading_date in days:
        logger.info(f"[BACKTEST] Processing {trading_date} ...")
        time.sleep(0.3)

        prev_ohlc = upstox_client.get_previous_day_ohlc_for_date(trading_date)
        if not prev_ohlc:
            logger.warning(f"[BACKTEST] No prev OHLC for {trading_date} — skipping.")
            skipped_days.append(str(trading_date))
            continue

        levels = calculate_cpr_levels(prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"])

        # CPR levels for the trading day (based on previous-day OHLC)
        # Also load the 'yesterday' CPR (day before previous) for pivot direction comparison
        prev_prev_date = trading_date - timedelta(days=1)
        prev_prev_ohlc = upstox_client.get_previous_day_ohlc_for_date(prev_prev_date)
        if not prev_prev_ohlc:
            logger.warning(f"[BACKTEST] No prev-prev OHLC for {trading_date} — skipping.")
            skipped_days.append(str(trading_date))
            continue
        yesterday_levels = calculate_cpr_levels(prev_prev_ohlc["high"], prev_prev_ohlc["low"], prev_prev_ohlc["close"])

        # PDH / PDL for the day are taken from the prev_ohlc we fetched above
        pdh = prev_ohlc["high"]
        pdl = prev_ohlc["low"]
        pdc = prev_ohlc["close"]

        # Compute average CPR width (used for market classification in strategy)
        widths = get_previous_cpr_widths(upstox_client, trading_date)
        if not widths:
            logger.warning(f"[BACKTEST] Unable to compute avg CPR width for {trading_date} — skipping.")
            skipped_days.append(str(trading_date))
            continue
        avg_width = sum(widths) / len(widths)

        candles = upstox_client.get_nifty_historical_5m_for_day(trading_date)
        if not candles:
            logger.warning(f"[BACKTEST] No candles for {trading_date} — skipping.")
            skipped_days.append(str(trading_date))
            continue

        day_trades: List[Dict] = []
        trade_count: int = 0
        open_trade: Optional[Dict] = None
        day_realized_pnl: float = 0.0
        open_trade_exit_time: Optional[str] = None

        for idx, candle in enumerate(candles):
            time_str = candle.get("time", "")
            try:
                cdt = datetime.fromisoformat(time_str.replace("+05:30", ""))
                bar_mins = cdt.hour * 60 + cdt.minute
            except Exception:
                bar_mins = 0

            # 3 PM — force close any open trade then stop
            if bar_mins >= squareoff_cutoff:
                if open_trade:
                    exit_p = candle["close"]
                    qty = lots * LOT_SIZE
                    if open_trade["trade_type"] == "SELL":
                        pnl = (open_trade["entry_price"] - exit_p) * qty
                    else:
                        pnl = (exit_p - open_trade["entry_price"]) * qty
                    open_trade.update({
                        "exit_price": round(exit_p, 2),
                        "exit_time": time_str,
                        "status": "CLOSED_EOD",
                        "pnl": round(pnl, 2)
                    })
                    day_realized_pnl += pnl
                break

            # Clear open_trade once candle time has passed the trade's exit_time
            if open_trade and open_trade_exit_time and time_str >= open_trade_exit_time:
                open_trade = None
                open_trade_exit_time = None

            # Already in a trade — skip this candle
            if open_trade:
                continue

            # Max trades reached
            if trade_count >= max_trades_per_day:
                break

            # No entries after 2 PM
            if bar_mins >= no_entry_cutoff:
                continue

            # Daily loss limit already hit from previous trade(s)
            if day_realized_pnl <= -dll:
                logger.info(f"[BACKTEST] {trading_date} day loss limit hit (₹{day_realized_pnl:.0f}) — no more trades.")
                break

            considered_candles = candles[: idx + 1]
            signal = find_trade_signal(considered_candles, levels, yesterday_levels, avg_width, pdh, pdl)
            if signal is None:
                continue

            entry_price = candle["close"]
            sl = signal.stop_loss
            tp = signal.take_profit
            trade_type = signal.trade_type
            strategy_name = signal.strategy_name

            # Simulate exit — with loss cap built in
            exit_info = _simulate_exit(
                candles, idx, entry_price, sl, tp,
                trade_type, lots, day_realized_pnl, dll
            )

            trade_rec = {
                "date":        str(trading_date),
                "setup_name":  strategy_name,
                "trade_type":  trade_type,
                "option_type": signal.option_type,
                "entry_price": round(entry_price, 2),
                "stop_loss":   round(sl, 2),
                "take_profit": round(tp, 2),
                "exit_price":  exit_info["exit_price"],
                "status":      exit_info["status"],
                "pnl":         exit_info["pnl"],
                "entry_time":  candle["time"],
                "exit_time":   exit_info["exit_time"],
                "cpr_r1":      levels.r1,
                "cpr_tc":      levels.tc,
                "cpr_bc":      levels.bc,
                "cpr_s1":      levels.s1,
                "cpr_pivot":   levels.pivot,
            }
            day_trades.append(trade_rec)
            all_trades.append(trade_rec)
            trade_count += 1
            day_realized_pnl += exit_info["pnl"]
            open_trade_exit_time = exit_info["exit_time"]
            open_trade = trade_rec

            logger.info(
                f"[BACKTEST] {trading_date} {strategy_name} {trade_type} "
                f"Entry={entry_price} SL={sl} TP={tp} → "
                f"{exit_info['status']} PNL=₹{exit_info['pnl']} "
                f"DayPNL=₹{day_realized_pnl:.0f}"
            )

        # Day summary
        day_pnl  = sum(t["pnl"] for t in day_trades)
        day_wins = sum(1 for t in day_trades if t["status"] == "CLOSED_TP")
        day_loss = sum(1 for t in day_trades if t["status"] in ("CLOSED_SL","CLOSED_SL_LIMIT"))
        day_summaries.append({
            "date":      str(trading_date),
            "trades":    len(day_trades),
            "wins":      day_wins,
            "losses":    day_loss,
            "eod_exits": len(day_trades) - day_wins - day_loss,
            "net_pnl":   round(day_pnl, 2),
            "cpr_r1":    levels.r1,
            "cpr_tc":    levels.tc,
            "cpr_bc":    levels.bc,
            "cpr_s1":    levels.s1,
        })

    # Aggregate metrics
    total   = len(all_trades)
    wins    = sum(1 for t in all_trades if t["status"] == "CLOSED_TP")
    losses  = sum(1 for t in all_trades if t["status"] in ("CLOSED_SL","CLOSED_SL_LIMIT"))
    eod     = total - wins - losses
    net_pnl = round(sum(t["pnl"] for t in all_trades), 2)

    strategy_stats: Dict[str, Any] = {}
    for t in all_trades:
        name = t["setup_name"]
        if name not in strategy_stats:
            strategy_stats[name] = {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0}
        strategy_stats[name]["trades"] += 1
        if t["status"] == "CLOSED_TP":
            strategy_stats[name]["wins"] += 1
        if t["status"] in ("CLOSED_SL", "CLOSED_SL_LIMIT"):
            strategy_stats[name]["losses"] += 1
        strategy_stats[name]["net_pnl"] += t["pnl"]

    for stats in strategy_stats.values():
        stats["win_rate"] = round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] else 0.0
        stats["net_pnl"] = round(stats["net_pnl"], 2)

    return {
        "start_date":      str(start_date),
        "end_date":        str(end_date),
        "days_processed":  len(days) - len(skipped_days),
        "days_skipped":    skipped_days,
        "metrics": {
            "total_trades":       total,
            "wins":               wins,
            "losses":             losses,
            "eod_exits":          eod,
            "win_rate":           round(wins / total * 100, 1) if total else 0.0,
            "net_pnl":            net_pnl,
            "gross_profit":       round(sum(t["pnl"] for t in all_trades if t["pnl"] > 0), 2),
            "gross_loss":         round(sum(t["pnl"] for t in all_trades if t["pnl"] < 0), 2),
            "avg_pnl_per_trade":  round(net_pnl / total, 2) if total else 0.0,
            "daily_loss_limit":   dll,
        },
        "strategy_breakdown": strategy_stats,
        "day_summaries":   day_summaries,
        "trades":          all_trades,
    }
