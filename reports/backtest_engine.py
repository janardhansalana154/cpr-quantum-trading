"""
Backtest Engine — runs all 4 CPR setups against historical Upstox data.
Fetches previous-day OHLC to compute CPR levels, then replays 5-min candles
through the state machines exactly as the live bot does.
"""
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional, Any
import logging

import time
from config.settings import settings
from strategies.cpr_strategy import SetupStateMachine, calculate_cpr_levels

logger = logging.getLogger("CPR_System.Backtest")

NIFTY_LOT_SIZE = 75  # index options lot size for P&L estimation

def _trading_days(start: date, end: date) -> List[date]:
    """Return weekdays (Mon-Fri) between start and end inclusive."""
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # Monday=0 … Friday=4
            days.append(d)
        d += timedelta(days=1)
    return days


def _simulate_exit(
    candles: List[Dict],
    entry_bar_idx: int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    trade_type: str,   # "SELL" (short/PE) or "BUY" (long/CE)
) -> Dict:
    """
    Walk candles after entry to find the first SL or TP hit.
    Returns exit dict with price, time, status, pnl.
    """
    for i in range(entry_bar_idx + 1, len(candles)):
        c = candles[i]
        hi = c["high"]
        lo = c["low"]
        time_str = c["time"]

        if trade_type == "SELL":  # short — profit if price falls
            # SL hit: price goes above stop_loss
            if hi >= stop_loss:
                pnl = (entry_price - stop_loss) * NIFTY_LOT_SIZE
                return {"exit_price": stop_loss, "exit_time": time_str,
                        "status": "CLOSED_SL", "pnl": round(pnl, 2)}
            # TP hit: price falls to take_profit
            if lo <= take_profit:
                pnl = (entry_price - take_profit) * NIFTY_LOT_SIZE
                return {"exit_price": take_profit, "exit_time": time_str,
                        "status": "CLOSED_TP", "pnl": round(pnl, 2)}

        else:  # BUY — long — profit if price rises
            # SL hit: price drops below stop_loss
            if lo <= stop_loss:
                pnl = (stop_loss - entry_price) * NIFTY_LOT_SIZE
                return {"exit_price": stop_loss, "exit_time": time_str,
                        "status": "CLOSED_SL", "pnl": round(pnl, 2)}
            # TP hit: price rises to take_profit
            if hi >= take_profit:
                pnl = (take_profit - entry_price) * NIFTY_LOT_SIZE
                return {"exit_price": take_profit, "exit_time": time_str,
                        "status": "CLOSED_TP", "pnl": round(pnl, 2)}

    # No exit hit by end of day — use last candle close
    last = candles[-1]
    last_close = last["close"]
    if trade_type == "SELL":
        pnl = (entry_price - last_close) * NIFTY_LOT_SIZE
    else:
        pnl = (last_close - entry_price) * NIFTY_LOT_SIZE
    return {"exit_price": last_close, "exit_time": last["time"],
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
    """
    Main backtest runner. Fetches real historical NIFTY 5m data from Upstox
    for each trading day and replays all 4 setups through the state machine.
    """
    # Use settings defaults if not overridden
    fw  = failure_window  or settings.FAILURE_WINDOW
    rw  = retest_window   or settings.RETEST_WINDOW
    cw  = confirm_window  or settings.CONFIRMATION_WINDOW
    ew  = entry_window    or settings.ENTRY_TRIGGER_WINDOW
    rt  = retest_tol      if retest_tol is not None else settings.RETEST_TOLERANCE
    slb = sl_buffer       if sl_buffer  is not None else settings.SL_BUFFER
    tpb = tp_buffer       if tp_buffer  is not None else settings.TARGET_BUFFER
    dll = daily_loss_limit if daily_loss_limit is not None else settings.DAILY_LOSS_LIMIT

    days = _trading_days(start_date, end_date)
    all_trades: List[Dict] = []
    day_summaries: List[Dict] = []
    skipped_days: List[str] = []

    for trading_date in days:
        logger.info(f"[BACKTEST] Processing {trading_date} ...")
        time.sleep(0.3)   # small delay to avoid Upstox rate limiting across days

        # 1. Fetch previous day OHLC for CPR calculation
        prev_ohlc = upstox_client.get_previous_day_ohlc_for_date(trading_date)
        if not prev_ohlc:
            logger.warning(f"[BACKTEST] No prev OHLC for {trading_date} — skipping.")
            skipped_days.append(str(trading_date))
            continue

        levels = calculate_cpr_levels(
            prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"]
        )

        # 2. Fetch 5-min candles for this trading day
        candles = upstox_client.get_nifty_historical_5m_for_day(trading_date)
        if not candles:
            logger.warning(f"[BACKTEST] No candles for {trading_date} — skipping.")
            skipped_days.append(str(trading_date))
            continue

        # 3. Fresh state machines for each day
        setups = {
            "SETUP_A": SetupStateMachine("SETUP_A"),
            "SETUP_B": SetupStateMachine("SETUP_B"),
            "SETUP_C": SetupStateMachine("SETUP_C"),
            "SETUP_D": SetupStateMachine("SETUP_D"),
        }
        # Override windows from backtest params
        for sm in setups.values():
            sm.fail_win = fw
            sm.ret_win  = rw
            sm.con_win  = cw
            sm.ent_win  = ew
            sm.ret_tol  = rt

        day_trades: List[Dict] = []
        trade_count = 0
        open_trade: Optional[Dict] = None
        day_realized_loss = 0.0   # track cumulative loss for daily limit

        for idx, candle in enumerate(candles):
            # ── Time cutoff checks ──
            candle_time_str = candle.get("time", "")
            try:
                candle_dt = datetime.fromisoformat(candle_time_str.replace("+05:30", ""))
                candle_hour_min = candle_dt.hour * 60 + candle_dt.minute
            except Exception:
                candle_hour_min = 0

            no_entry_cutoff = settings.NO_ENTRY_AFTER_HOUR * 60 + settings.NO_ENTRY_AFTER_MIN
            squareoff_cutoff = settings.SQUAREOFF_HOUR * 60 + settings.SQUAREOFF_MIN

            # ── 3 PM force squareoff ──
            if candle_hour_min >= squareoff_cutoff and open_trade:
                exit_price = candle["close"]
                qty = NIFTY_LOT_SIZE
                if open_trade["trade_type"] == "SELL":
                    pnl = (open_trade["entry_price"] - exit_price) * qty
                else:
                    pnl = (exit_price - open_trade["entry_price"]) * qty
                open_trade["exit_price"] = exit_price
                open_trade["exit_time"]  = candle_time_str
                open_trade["status"]     = "CLOSED_EOD"
                open_trade["pnl"]        = round(pnl, 2)
                day_realized_loss += pnl
                open_trade = None
                break

            if candle_hour_min >= squareoff_cutoff:
                break   # No more entries or processing after 3 PM

            # Only 1 open trade at a time
            if open_trade:
                # Check if this candle's time has passed the open trade's exit_time
                if open_trade.get("exit_time") and candle_time_str >= open_trade["exit_time"]:
                    open_trade = None
                else:
                    continue

            if trade_count >= max_trades_per_day:
                break

            # ── No new entries after 2 PM ──
            if candle_hour_min >= no_entry_cutoff:
                continue

            for name, sm in setups.items():
                if open_trade:
                    break
                triggered, details = sm.update(candle, idx, levels)

                if triggered and details:
                    # Daily loss limit check — same as live bot
                    if day_realized_loss <= -abs(dll):
                        logger.info(f"[BACKTEST] {trading_date} Daily loss limit ₹{dll} hit — skipping further trades.")
                        break

                    entry_price = candle["close"]
                    sl = details["stop_loss"]
                    tp = details["take_profit"]
                    trade_type = details["trade_type"]

                    # Simulate exit
                    exit_info = _simulate_exit(
                        candles, idx, entry_price, sl, tp, trade_type
                    )

                    trade_rec = {
                        "date":        str(trading_date),
                        "setup_name":  name,
                        "trade_type":  trade_type,
                        "option_type": "PE" if trade_type == "SELL" else "CE",
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
                    open_trade = trade_rec
                    # Accumulate realized P&L for loss limit check on next trade
                    day_realized_loss += exit_info["pnl"]
                    logger.info(
                        f"[BACKTEST] {trading_date} {name} {trade_type} "
                        f"Entry={entry_price} SL={sl} TP={tp} → {exit_info['status']} PNL=₹{exit_info['pnl']}"
                    )

            # open_trade clearance is handled at the top of the loop

        # Day summary
        day_pnl = sum(t["pnl"] for t in day_trades)
        day_wins = sum(1 for t in day_trades if t["status"] == "CLOSED_TP")
        day_losses = sum(1 for t in day_trades if t["status"] == "CLOSED_SL")
        day_summaries.append({
            "date":         str(trading_date),
            "trades":       len(day_trades),
            "wins":         day_wins,
            "losses":       day_losses,
            "eod_exits":    len(day_trades) - day_wins - day_losses,
            "net_pnl":      round(day_pnl, 2),
            "cpr_r1":       levels.r1,
            "cpr_tc":       levels.tc,
            "cpr_bc":       levels.bc,
            "cpr_s1":       levels.s1,
        })

    # Overall metrics
    total = len(all_trades)
    wins  = sum(1 for t in all_trades if t["status"] == "CLOSED_TP")
    losses= sum(1 for t in all_trades if t["status"] == "CLOSED_SL")
    eod   = total - wins - losses
    net_pnl = round(sum(t["pnl"] for t in all_trades), 2)
    gross_profit = round(sum(t["pnl"] for t in all_trades if t["pnl"] > 0), 2)
    gross_loss   = round(sum(t["pnl"] for t in all_trades if t["pnl"] < 0), 2)

    # Per-setup breakdown
    setup_stats: Dict[str, Any] = {}
    for sname in ["SETUP_A", "SETUP_B", "SETUP_C", "SETUP_D"]:
        st = [t for t in all_trades if t["setup_name"] == sname]
        sw = sum(1 for t in st if t["status"] == "CLOSED_TP")
        setup_stats[sname] = {
            "trades":   len(st),
            "wins":     sw,
            "losses":   sum(1 for t in st if t["status"] == "CLOSED_SL"),
            "win_rate": round(sw / len(st) * 100, 1) if st else 0.0,
            "net_pnl":  round(sum(t["pnl"] for t in st), 2),
        }

    return {
        "start_date":    str(start_date),
        "end_date":      str(end_date),
        "days_processed": len(days) - len(skipped_days),
        "days_skipped":  skipped_days,
        "metrics": {
            "total_trades":  total,
            "wins":          wins,
            "losses":        losses,
            "eod_exits":     eod,
            "win_rate":      round(wins / total * 100, 1) if total else 0.0,
            "net_pnl":       net_pnl,
            "gross_profit":  gross_profit,
            "gross_loss":    gross_loss,
            "avg_pnl_per_trade": round(net_pnl / total, 2) if total else 0.0,
        },
        "setup_breakdown": setup_stats,
        "day_summaries":   day_summaries,
        "trades":          all_trades,
    }
