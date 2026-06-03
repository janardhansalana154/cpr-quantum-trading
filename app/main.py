import os
import logging
import requests
from datetime import datetime, date, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from database.db import get_db, init_db
from database.models import Trade, DailyState, StrategyState
from brokers.upstox_client import UpstoxClient, is_market_open, get_market_status_detail
from risk.manager import RiskManager
from strategies.cpr_strategy import calculate_cpr_levels, is_inside_cpr, SetupStateMachine, CPRLevels
from telegram.bot import notify_signal_detected, notify_order_placed, notify_sl_hit, notify_tp_hit, notify_system_error
from telegram.bot import send_telegram_message

from reports.historical_report import generate_historical_report
from reports.backtest_engine import run_backtest
from fastapi.responses import HTMLResponse
from fastapi import Request

_IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger("CPR_System.Main")

app = FastAPI(
    title="CPR 4-Setup Automated Trading System",
    description="Live Nifty 50 automated CPR strategy engine",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

upstox: Optional[UpstoxClient] = None

setups = {
    "SETUP_A": SetupStateMachine("SETUP_A"),
    "SETUP_B": SetupStateMachine("SETUP_B"),
    "SETUP_C": SetupStateMachine("SETUP_C"),
    "SETUP_D": SetupStateMachine("SETUP_D"),
}

_today_cpr_levels: Optional[CPRLevels] = None
_cpr_date: Optional[str] = None


def get_today_cpr_levels(db: Session) -> Optional[CPRLevels]:
    global _today_cpr_levels, _cpr_date
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    if _today_cpr_levels is not None and _cpr_date == today_str:
        return _today_cpr_levels

    prev = upstox.get_previous_day_ohlc() if upstox else None
    if prev:
        _today_cpr_levels = calculate_cpr_levels(prev["high"], prev["low"], prev["close"])
        _cpr_date = today_str
        logger.info(
            f"[LIVE] CPR computed: Pivot={_today_cpr_levels.pivot:.2f} "
            f"TC={_today_cpr_levels.tc:.2f} BC={_today_cpr_levels.bc:.2f} "
            f"R1={_today_cpr_levels.r1:.2f} S1={_today_cpr_levels.s1:.2f}"
        )
    else:
        _today_cpr_levels = None
        _cpr_date = today_str
        logger.warning("[DATA_SOURCE=DISCONNECTED] CPR levels unavailable — authenticate Upstox.")
    return _today_cpr_levels


@app.on_event("startup")
def startup_event():
    global upstox

    # Init DB FIRST so UpstoxClient can load token from DB on startup
    init_db()
    upstox = UpstoxClient()
    logger.info("[STARTUP] Database initialised. UpstoxClient created.")

    if upstox._is_authenticated():
        logger.info("[STARTUP] Upstox token restored and authenticated.")
    else:
        logger.warning("[STARTUP] No valid Upstox session available on startup.")

    scheduler = BackgroundScheduler()

    # Strategy tick — every 5 minutes
    scheduler.add_job(
        monitor_interval_tick,
        "cron",
        minute="*/5",
        second=30,
        id="nifty_monitor_job",
        max_instances=1,
        coalesce=True,
    )

    # SL/TP monitor — every 30 seconds
    scheduler.add_job(
        check_active_position_targets,
        "interval",
        seconds=30,
        id="targets_monitor_job",
        max_instances=1,
        coalesce=True,
    )

    # Upstox keepalive — every 10 minutes
    scheduler.add_job(
        keep_upstox_alive,
        "interval",
        minutes=10,
        id="upstox_keepalive_job",
        max_instances=1,
        coalesce=True,
    )

    # FIX: Daily auto-reconnect at 8:55 AM IST
    # Clears expired token and attempts TOTP auto-login before market opens.
    # Requires UPSTOX_TOTP_SECRET + UPSTOX_MOBILE + UPSTOX_PIN in .env
    scheduler.add_job(
        daily_token_refresh,
        "cron",
        hour=3,       # 8:55 AM IST = 3:25 AM UTC
        minute=25,
        id="daily_token_refresh_job",
        max_instances=1,
        coalesce=True,
    )

    # Also reset CPR cache at midnight UTC so new day levels are fetched fresh
    scheduler.add_job(
        reset_daily_cpr_cache,
        "cron",
        hour=0,
        minute=1,
        id="daily_cpr_reset_job",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    keep_upstox_alive()
    logger.info(
        "[STARTUP] Scheduler running: 5m strategy tick + 30s SL/TP + "
        "10m keepalive + 8:55AM IST daily token refresh."
    )


# ------------------------------------------------------------------
# Daily token refresh (8:55 AM IST = 3:25 AM UTC)
# ------------------------------------------------------------------
def daily_token_refresh():
    """Auto-reconnect Upstox every morning before market opens."""
    if upstox is None:
        return
    logger.info("[DAILY-REFRESH] Running daily Upstox token refresh at 8:55 AM IST...")
    upstox.daily_auto_reconnect()


def reset_daily_cpr_cache():
    """Clear CPR cache at midnight so fresh levels are fetched on the new trading day."""
    global _today_cpr_levels, _cpr_date
    _today_cpr_levels = None
    _cpr_date = None
    logger.info("[CPR] Daily CPR cache cleared for new trading day.")


# ------------------------------------------------------------------
# Upstox keepalive (every 10 minutes)
# ------------------------------------------------------------------
def keep_upstox_alive():
    if upstox is None:
        return
    
    # Skip keepalive outside market hours to avoid 401 errors from closed markets
    mkt = get_market_status_detail()
    if not mkt["market_open"]:
        logger.debug("[UPSTOX] Keepalive skipped — market is closed.")
        return
    
    if not upstox.ensure_authenticated():
        logger.info("[UPSTOX] Keepalive skipped — no valid token available.")
        return
    ltp = upstox.get_nifty_price()
    if ltp is not None:
        logger.debug(f"[UPSTOX] Keepalive success — Nifty LTP {ltp}.")
    else:
        logger.debug("[UPSTOX] Keepalive probe complete — token still valid.")


# ------------------------------------------------------------------
# SL / TP monitor (every 30s)
# ------------------------------------------------------------------
def check_active_position_targets():
    if upstox is None:
        return

    from database.db import SessionLocal
    now_ist = datetime.now(_IST)

    db = SessionLocal()
    try:
        open_trade = db.query(Trade).filter(Trade.status == "OPEN").first()

        squareoff_cutoff = now_ist.replace(
            hour=settings.SQUAREOFF_HOUR, minute=settings.SQUAREOFF_MIN,
            second=0, microsecond=0
        )
        if now_ist >= squareoff_cutoff and open_trade:
            ltp = upstox.get_nifty_price()
            exit_price = ltp if ltp else open_trade.entry_price
            rm = RiskManager(db)
            closed = rm.register_trade_exit(open_trade.id, exit_price, "CLOSED_EOD")
            if closed:
                upstox.place_order(open_trade.option_symbol, "SELL", open_trade.lots, paper=open_trade.is_paper)
                logger.warning(f"[EOD SQUAREOFF] Trade {open_trade.id} force-closed at {settings.SQUAREOFF_HOUR}:{settings.SQUAREOFF_MIN:02d} IST. Exit={exit_price}")
            return

        if not open_trade:
            return

        mkt = get_market_status_detail()
        if not mkt["market_open"]:
            return

        ltp = upstox.get_nifty_price()
        if ltp is None:
            logger.warning("[CMP_SOURCE=DISCONNECTED] Cannot check SL/TP — no live LTP.")
            return

        rm_check = RiskManager(db)
        day_state = rm_check.get_or_create_daily_state()
        qty = open_trade.lots * settings.NIFTY_LOT_SIZE
        if open_trade.setup_name in ["SETUP_B", "SETUP_C"]:
            unrealised = (ltp - open_trade.entry_price) * qty
        else:
            unrealised = (open_trade.entry_price - ltp) * qty
        total_day_pnl = day_state.realized_pnl + unrealised
        if total_day_pnl <= -settings.DAILY_LOSS_LIMIT:
            closed = rm_check.register_trade_exit(open_trade.id, ltp, "CLOSED_SL_LIMIT")
            if closed:
                upstox.place_order(open_trade.option_symbol, "SELL", open_trade.lots, paper=open_trade.is_paper)
                logger.warning(
                    f"[LOSS LIMIT] Trade {open_trade.id} force-closed. "
                    f"Day P&L ₹{total_day_pnl:.2f} breached ₹{-settings.DAILY_LOSS_LIMIT:.2f}"
                )
                notify_sl_hit(open_trade.setup_name, open_trade.option_symbol, abs(unrealised), total_day_pnl)
            return

        is_sl = is_tp = False
        if open_trade.setup_name in ["SETUP_B", "SETUP_C"]:
            is_sl = ltp <= open_trade.stop_loss
            is_tp = ltp >= open_trade.take_profit
        else:
            is_sl = ltp >= open_trade.stop_loss
            is_tp = ltp <= open_trade.take_profit

        if not (is_sl or is_tp):
            return

        exit_status = "CLOSED_SL" if is_sl else "CLOSED_TP"
        premium_chg  = (ltp - open_trade.entry_price) * (1 if open_trade.trade_type == "BUY" else -1)
        exit_premium = max(5.0, open_trade.entry_price + premium_chg * 0.5)

        upstox.place_order(open_trade.option_symbol, "SELL", open_trade.lots, paper=open_trade.is_paper)
        rm = RiskManager(db)
        closed = rm.register_trade_exit(open_trade.id, exit_premium, exit_status)
        daily  = rm.get_or_create_daily_state()
        if is_sl:
            notify_sl_hit(closed.setup_name, closed.option_symbol, abs(closed.pnl), daily.realized_pnl)
        else:
            notify_tp_hit(closed.setup_name, closed.option_symbol, closed.pnl, daily.realized_pnl)

    except Exception as e:
        logger.error(f"check_active_position_targets error: {e}")
    finally:
        db.close()


# ------------------------------------------------------------------
# Main strategy tick (every 5 minutes)
# ------------------------------------------------------------------
_last_processed_candle_time: Optional[str] = None


def monitor_interval_tick():
    global _last_processed_candle_time

    if upstox is None:
        return

    mkt = get_market_status_detail()
    logger.info(
        f"[MARKET_OPEN={mkt['market_open']}] Tick at {mkt['current_ist']} "
        f"({mkt['weekday']}, holiday={mkt['is_holiday']})"
    )
    if not mkt["market_open"]:
        logger.info("[MARKET_OPEN=False] [STRATEGY_ALLOWED=False] Tick suppressed — market closed.")
        return

    if not upstox._is_authenticated() and not settings.MOCK_MODE:
        logger.warning(
            "[DATA_SOURCE=DISCONNECTED] [STRATEGY_ALLOWED=False] "
            "Tick suppressed — Upstox not authenticated. No simulation fallback."
        )
        return

    from database.db import SessionLocal
    db = SessionLocal()
    try:
        rm = RiskManager(db)
        daily = rm.get_or_create_daily_state()

        logger.info(
            f"[TRADES_TODAY={daily.trade_count}/{settings.MAX_DAILY_TRADES}] "
            f"[DATA_SOURCE={upstox.data_source}]"
        )
        if daily.trade_count >= settings.MAX_DAILY_TRADES or daily.is_blocked:
            logger.warning(
                f"[STRATEGY_ALLOWED=False] Trade limit reached "
                f"({daily.trade_count}/{settings.MAX_DAILY_TRADES}). Tick suppressed."
            )
            return

        logger.info("[STRATEGY_ALLOWED=True] Fetching live candles.")

        candles = upstox.get_nifty_ohlc_5m()
        logger.info(f"[DATA_SOURCE={upstox.data_source}] {len(candles)} 5m candles received.")

        if not candles:
            logger.warning("[LIVE] No candles — market pre-open or no completed bars yet.")
            return

        latest = candles[-1]
        latest_time = latest.get("time", "")

        if latest_time == _last_processed_candle_time:
            logger.debug(f"[LIVE] Candle {latest_time} already processed — skipping.")
            return

        levels = get_today_cpr_levels(db)
        if levels is None:
            logger.warning("[DATA_SOURCE=DISCONNECTED] CPR levels unavailable — tick skipped.")
            return

        ltp = upstox.get_nifty_price()
        cmp_src = "UPSTOX_LTP" if ltp is not None else "UNAVAILABLE"
        logger.info(f"[CMP_SOURCE={cmp_src}] Nifty LTP: {ltp}")

        for i, candle in enumerate(candles):
            is_latest = (i == len(candles) - 1)
            for name, machine in setups.items():
                triggered, details = machine.update(candle, i, levels)
                if not (triggered and is_latest):
                    continue

                logger.info(f"[LIVE] {name} TRIGGERED | candle={latest_time} close={latest['close']}")

                _now_ist = datetime.now(_IST)
                _cutoff  = _now_ist.replace(
                    hour=settings.NO_ENTRY_AFTER_HOUR,
                    minute=settings.NO_ENTRY_AFTER_MIN,
                    second=0, microsecond=0
                )
                if _now_ist >= _cutoff:
                    logger.warning(
                        f"[TIME CUTOFF] {name} signal at {_now_ist.strftime('%H:%M IST')} "
                        f"rejected — no new entries after "
                        f"{settings.NO_ENTRY_AFTER_HOUR:02d}:{settings.NO_ENTRY_AFTER_MIN:02d} IST."
                    )
                    continue

                if not rm.can_trade():
                    continue

                daily_now = rm.get_or_create_daily_state()
                if daily_now.trade_count >= settings.MAX_DAILY_TRADES:
                    logger.warning(
                        f"[STRATEGY_ALLOWED=False] Limit hit ({daily_now.trade_count}/"
                        f"{settings.MAX_DAILY_TRADES}) — aborting order."
                    )
                    return

                opt_sym, strike, opt_type = upstox.select_atm_option(
                    candle["close"], details["trade_type"]
                )
                logger.info(
                    f"[LIVE] Signal: {name} {details['trade_type']} | "
                    f"Option={opt_sym} SL={details['stop_loss']} TP={details['take_profit']}"
                )
                notify_signal_detected(name, f"Entry at {candle['close']}. Buying {opt_sym}.")

                order = upstox.place_order(
                    opt_sym, "BUY", details["lots"],
                    paper=(settings.TRADING_MODE == "paper"),
                )
                if order["status"] == "success":
                    rec = rm.register_trade_entry(
                        setup_name=name, trade_type=details["trade_type"],
                        option_symbol=opt_sym, strike=strike, option_type=opt_type,
                        entry_price=order["avg_price"],
                        is_paper=(settings.TRADING_MODE == "paper"),
                    )
                    if rec:
                        rec.stop_loss   = details["stop_loss"]
                        rec.take_profit = details["take_profit"]
                        db.commit()
                        logger.info(
                            f"[TRADES_TODAY={daily_now.trade_count+1}/{settings.MAX_DAILY_TRADES}] "
                            f"Trade recorded: {name} | {opt_sym}"
                        )
                    notify_order_placed(
                        setup=name, buy_sell="BUY",
                        details=(
                            f"Option: `{opt_sym}`\nLots: `1`\n"
                            f"Premium: `₹{order['avg_price']:.2f}`\n"
                            f"SL: `{details['stop_loss']:.2f}`\n"
                            f"TP: `{details['take_profit']:.2f}`"
                        ),
                    )
                else:
                    notify_system_error(f"Order failed for {name}: {order['message']}")

        _last_processed_candle_time = latest_time
        logger.info(
            f"[LIVE] Tick complete. candle={latest_time} "
            f"[TRADES_TODAY={rm.get_or_create_daily_state().trade_count}/{settings.MAX_DAILY_TRADES}]"
        )

    except Exception as e:
        logger.error(f"monitor_interval_tick error: {e}")
        notify_system_error(f"Candle tick error: {e}")
    finally:
        db.close()


# ------------------------------------------------------------------
# API ENDPOINTS
# ------------------------------------------------------------------

@app.get("/api/status")
def get_system_status(db: Session = Depends(get_db)):
    if upstox is None:
        return {"status": "Starting", "message": "Server still initialising, retry in a moment."}

    levels = get_today_cpr_levels(db)
    rm = RiskManager(db)
    daily = rm.get_or_create_daily_state()
    mkt = get_market_status_detail()

    auth = upstox._is_authenticated()
    if settings.MOCK_MODE:
        upstox.data_source = "SIMULATION"
        upstox.websocket_status = "Connected"
    elif auth:
        upstox.data_source = "UPSTOX LIVE"
        upstox.websocket_status = "Connected"
    else:
        upstox.data_source = "DISCONNECTED"
        upstox.websocket_status = "Disconnected"

    nifty_ltp = None
    cmp_source = "DISCONNECTED"
    cmp_ts = None
    if settings.MOCK_MODE:
        cmp_source = "DISCONNECTED"
    elif auth:
        nifty_ltp = upstox.get_nifty_price()
        if nifty_ltp is not None:
            cmp_source = "UPSTOX_LTP"
            cmp_ts = datetime.utcnow().isoformat()
        else:
            cmp_source = "DISCONNECTED"
            cmp_ts = None

    strategy_allowed = (
        mkt["market_open"]
        and (settings.MOCK_MODE or auth)
        and daily.trade_count < settings.MAX_DAILY_TRADES
        and not daily.is_blocked
    )

    logger.info(
        f"[MARKET_OPEN={mkt['market_open']}] [DATA_SOURCE={upstox.data_source}] "
        f"[TRADES_TODAY={daily.trade_count}/{settings.MAX_DAILY_TRADES}] "
        f"[STRATEGY_ALLOWED={strategy_allowed}] [CMP_SOURCE={cmp_source}]"
    )

    return {
        "status": "Running",
        "timestamp": datetime.utcnow().isoformat(),
        "trading_mode": settings.TRADING_MODE,
        "mock_mode": settings.MOCK_MODE,
        "market_status": mkt["market_status"],
        "market_open": mkt["market_open"],
        "market_detail": mkt,
        "data_source": upstox.data_source,
        "nifty_ltp": nifty_ltp,
        "cmp_source": cmp_source,
        "cmp_last_updated": cmp_ts,
        "last_live_candle_time": upstox.last_live_candle_time,
        "websocket_status": upstox.websocket_status,
        "strategy_allowed": strategy_allowed,
        "cpr_levels": levels,
        "daily_summary": {
            "date": daily.trade_date,
            "trade_count": daily.trade_count,
            "max_trades": settings.MAX_DAILY_TRADES,
            "realized_pnl": daily.realized_pnl,
            "is_blocked": daily.is_blocked,
        },
        "limits": {
            "max_trades": settings.MAX_DAILY_TRADES,
            "loss_limit": settings.DAILY_LOSS_LIMIT,
            "lots": settings.POSITION_LOTS,
        },
    }


class AssistantQuery(BaseModel):
    question: str


def _assistant_find_setup_states(setups: Dict[str, Any]) -> Dict[str, Any]:
    armed = [name for name, data in setups.items() if data["state"] == 4]
    retested = [name for name, data in setups.items() if data["state"] == 3]
    recovered = [name for name, data in setups.items() if data["state"] == 2]
    broken = [name for name, data in setups.items() if data["state"] == 1]
    return {"armed": armed, "retested": retested, "recovered": recovered, "broken": broken}


def _assistant_summary(status: Dict[str, Any], setups: Dict[str, Any]) -> str:
    lines = [
        f"Market is {'OPEN' if status['market_open'] else 'CLOSED'}.",
        f"Data source: {status['data_source']}.",
        f"Strategy allowed: {'YES' if status['strategy_allowed'] else 'NO'}.",
        f"Trades today: {status['daily_summary']['trade_count']}/{settings.MAX_DAILY_TRADES}.",
    ]
    if status['cpr_levels']:
        cpr = status['cpr_levels']
        lines.append(f"Current CPR: R1={cpr['r1']}, TC={cpr['tc']}, BC={cpr['bc']}, S1={cpr['s1']}.")
    state_info = _assistant_find_setup_states(setups)
    if state_info['armed']:
        lines.append(f"Armed setup(s): {', '.join(state_info['armed'])}.")
    elif state_info['retested']:
        lines.append(f"Retested but waiting for confirmation: {', '.join(state_info['retested'])}.")
    elif state_info['recovered']:
        lines.append(f"Recovered setups awaiting retest: {', '.join(state_info['recovered'])}.")
    elif state_info['broken']:
        lines.append(f"Breakouts detected but not yet retested: {', '.join(state_info['broken'])}.")
    return " ".join(lines)


def _assistant_answer_question(question: str, status: Dict[str, Any], setups: Dict[str, Any]) -> str:
    q = question.strip().lower()
    state_info = _assistant_find_setup_states(setups)
    if "why" in q and ("enter" in q or "entry" in q or "trade" in q):
        if not status['market_open']:
            return "No entry can occur while the market is closed. Wait until NSE opens at 09:15 IST and the engine will resume if Upstox remains authenticated."
        if status['data_source'] == 'DISCONNECTED':
            return "Upstox is disconnected, so the strategy cannot receive live candles or trigger any orders. Reconnect Upstox before trading can continue."
        if not status['strategy_allowed']:
            if status['daily_summary']['trade_count'] >= settings.MAX_DAILY_TRADES:
                return f"The system has already reached the daily trade limit ({status['daily_summary']['trade_count']}/{settings.MAX_DAILY_TRADES}), so no further entries are allowed today."
            if status['daily_summary']['is_blocked']:
                return "Trading is blocked for the day by risk control. Restore the daily state to resume strategy execution."
        if state_info['armed']:
            armed = ", ".join(state_info['armed'])
            return f"A trade is ready in {armed}. The system is waiting for a confirmation candle that triggers the entry price."
        if state_info['retested']:
            return f"The strategy has retested one or more levels: {', '.join(state_info['retested'])}. It is waiting for the next confirmation move to complete the setup."
        if state_info['recovered']:
            return f"A breakout has recovered inside the CPR and is waiting for a valid retest. Active recovered setups: {', '.join(state_info['recovered'])}."
        if state_info['broken']:
            return f"The system has at least one valid breakout, but it has not yet completed the recovery and retest sequence. Breakout setup(s): {', '.join(state_info['broken'])}."
        return _assistant_summary(status, setups)

    if "upstox" in q or "token" in q or "connected" in q:
        if status['data_source'] == 'DISCONNECTED':
            return "Upstox is currently disconnected. Authenticate again to restore live data and trading capability."
        expiry = upstox.token_expiry if hasattr(upstox, 'token_expiry') else status.get('cmp_last_updated')
        return f"Upstox is connected. Data source is live, and token expiry info is available in the dashboard. Last CMP update: {status.get('cmp_last_updated') or 'unknown'}."

    if "max trade" in q or "trade limit" in q or "daily trade" in q:
        return f"Daily max trades is {settings.MAX_DAILY_TRADES}. Today {status['daily_summary']['trade_count']} trades have been taken. When the limit is reached, the engine stops taking new entries."

    if "cpr" in q or "levels" in q or "r1" in q or "tc" in q or "bc" in q or "s1" in q:
        if status['cpr_levels']:
            cpr = status['cpr_levels']
            return f"Current CPR levels are R1={cpr['r1']}, TC={cpr['tc']}, BC={cpr['bc']}, S1={cpr['s1']}."
        return "CPR levels are not available right now because previous-day OHLC data is not loaded. Authenticate Upstox or wait for the next tick."

    for setup_key in ['setup_a', 'setup_b', 'setup_c', 'setup_d']:
        if setup_key in q:
            key = setup_key.upper()
            if key in setups:
                s = setups[key]
                return f"{key} is currently in state {s['state']} with reason: {s['last_reason']}."

    if "help" in q or "rule" in q or "how" in q:
        return _assistant_summary(status, setups)

    return _assistant_summary(status, setups)


@app.post("/api/assistant")
def assistant_endpoint(request: AssistantQuery, db: Session = Depends(get_db)):
    question = request.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required.")
    if upstox is None:
        return {"question": question, "answer": "Server is still starting. Try again in a few seconds."}
    status = get_system_status(db)
    setups_state = get_active_setups()
    answer = _assistant_answer_question(question, status, setups_state)
    return {"question": question, "answer": answer}


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "upstox_authenticated": upstox._is_authenticated() if upstox else False,
        "keepalive_url": settings.KEEPALIVE_URL,
    }


@app.get("/api/setups")
def get_active_setups():
    return {
        name: {
            "state": m.state,
            "state_bar": m.state_bar,
            "elapsed_bars": m.bars_elapsed(m.state_bar),
            "retest_high": m.r_high,
            "retest_low": m.r_low,
            "confirmation_high": m.c_high,
            "confirmation_low": m.c_low,
            "last_reason": m.last_reason,
            "configs": {"fail_win": m.fail_win, "ret_win": m.ret_win,
                        "con_win": m.con_win, "ent_win": m.ent_win, "ret_tol": m.ret_tol},
        }
        for name, m in setups.items()
    }


@app.get("/api/trades")
def get_recent_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).order_by(Trade.entry_time.desc()).all()
    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    total  = len(trades)
    return {
        "trades": trades,
        "metrics": {
            "total_trades": total,
            "win_rate": round(len(wins) / total * 100, 2) if total else 0.0,
            "wins": len(wins),
            "losses": len(losses),
            "gross_profit": sum(t.pnl for t in wins),
            "gross_loss": sum(t.pnl for t in losses),
            "net_pnl": sum(t.pnl for t in trades),
        },
    }


@app.post("/api/trades/{trade_id}/manual-close")
def manual_close_trade(trade_id: int, data: Dict[str, Any], db: Session = Depends(get_db)):
    trade = db.query(Trade).filter(Trade.id == trade_id, Trade.status == "OPEN").first()
    if not trade:
        raise HTTPException(status_code=404, detail="Open trade not found.")

    payload = data or {}
    exit_price = payload.get("exit_price")
    if exit_price is None:
        exit_price = trade.entry_price
    try:
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="exit_price must be numeric.")

    rm = RiskManager(db)
    closed = rm.register_trade_exit(trade.id, exit_price, "CLOSED_MANUAL")
    if not closed:
        raise HTTPException(status_code=500, detail="Failed to close trade.")

    return {"status": "success", "message": "Trade marked closed manually.", "trade_id": closed.id}


@app.post("/api/trading/pause")
def pause_trading(db: Session = Depends(get_db)):
    rm = RiskManager(db)
    state = rm.get_or_create_daily_state()
    state.is_blocked = True
    db.commit()
    return {"status": "success", "message": "Trading paused for today."}


@app.post("/api/trading/resume")
def resume_trading(db: Session = Depends(get_db)):
    rm = RiskManager(db)
    state = rm.get_or_create_daily_state()
    state.is_blocked = False
    db.commit()
    return {"status": "success", "message": "Trading resumed for today."}


@app.get("/api/market-status")
def market_status_endpoint():
    return get_market_status_detail()


@app.get("/api/v1/login-url")
def get_upstox_login(request: Request):
    if upstox is None:
        raise HTTPException(status_code=503, detail="Server still initialising")
    redirect_uri = _resolve_redirect_uri(request)
    logger.info(f"Login URL redirect_uri: {redirect_uri}")
    return {"url": upstox.get_login_url(override_redirect_uri=redirect_uri)}


@app.get("/api/v1/callback")
def upstox_callback(request: Request, code: str, db: Session = Depends(get_db)):
    if upstox is None:
        raise HTTPException(status_code=503, detail="Server still initialising")
    success = upstox.authenticate(code, redirect_uri=_resolve_redirect_uri(request))
    if success:
        return _oauth_success_response()
    raise HTTPException(status_code=400, detail="Authentication failed")


@app.post("/api/telegram/test")
def telegram_test_alert(message: Dict[str, str] = None):
    """Send a test Telegram alert using current runtime settings."""
    msg = (message or {}).get("message") or "Test alert from CPR system"
    try:
        ok = send_telegram_message(msg)
        if ok:
            return {"status": "success", "message": "Telegram test sent."}
        raise HTTPException(status_code=500, detail="Telegram API call failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config")
def update_config(data: dict, db: Session = Depends(get_db)):
    global _today_cpr_levels, _cpr_date

    if "upstox_api_key" in data or "upstox_api_secret" in data:
        import json
        secrets = {}
        sp = getattr(settings, "UPSTOX_SECRETS_PATH", None)
        if sp and os.path.exists(sp):
            try:
                with open(sp) as f:
                    secrets = json.load(f)
            except Exception:
                pass

        if "upstox_api_key" in data:
            v = str(data["upstox_api_key"]).strip()
            settings.UPSTOX_API_KEY = v
            if upstox: upstox.api_key = v
            secrets["api_key"] = v
        if "upstox_api_secret" in data:
            v = str(data["upstox_api_secret"]).strip()
            settings.UPSTOX_API_SECRET = v
            if upstox: upstox.api_secret = v
            secrets["api_secret"] = v

        if not sp:
            sp = os.path.abspath(
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "upstox_secrets.json")
            )
        try:
            with open(sp, "w") as f:
                import json
                json.dump(secrets, f)
            logger.info(f"Credentials persisted to {sp}")
        except Exception as e:
            raise HTTPException(500, f"Credential write error: {e}")

    # Accept Telegram credentials from dashboard and persist alongside other secrets
    if "telegram_bot_token" in data or "telegram_chat_id" in data:
        try:
            sp = getattr(settings, "UPSTOX_SECRETS_PATH", None) or os.path.abspath(
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "upstox_secrets.json")
            )
            with open(sp, "r+") as f:
                import json
                try:
                    file_data = json.load(f)
                except Exception:
                    file_data = {}
                if "telegram_bot_token" in data:
                    v = str(data["telegram_bot_token"]).strip()
                    settings.TELEGRAM_BOT_TOKEN = v
                    file_data["telegram_bot_token"] = v
                if "telegram_chat_id" in data:
                    v = str(data["telegram_chat_id"]).strip()
                    settings.TELEGRAM_CHAT_ID = v
                    file_data["telegram_chat_id"] = v
                f.seek(0)
                f.truncate()
                json.dump(file_data, f)
            logger.info("Telegram credentials persisted to secrets file.")
        except Exception:
            # Best-effort: at minimum update runtime settings
            if "telegram_bot_token" in data:
                settings.TELEGRAM_BOT_TOKEN = str(data["telegram_bot_token"]).strip()
            if "telegram_chat_id" in data:
                settings.TELEGRAM_CHAT_ID = str(data["telegram_chat_id"]).strip()

    for key, attr in [("failure_window","FAILURE_WINDOW"),("retest_window","RETEST_WINDOW"),
                      ("confirmation_window","CONFIRMATION_WINDOW"),("entry_trigger_window","ENTRY_TRIGGER_WINDOW")]:
        if key in data:
            setattr(settings, attr, int(data[key]))
    if "retest_tolerance" in data:
        settings.RETEST_TOLERANCE = float(data["retest_tolerance"])
    if "mock_mode" in data:
        settings.MOCK_MODE = bool(data["mock_mode"])
    if "trading_mode" in data and data["trading_mode"] in ("paper","live"):
        settings.TRADING_MODE = data["trading_mode"]

    for m in setups.values():
        m.fail_win = settings.FAILURE_WINDOW
        m.ret_win  = settings.RETEST_WINDOW
        m.con_win  = settings.CONFIRMATION_WINDOW
        m.ent_win  = settings.ENTRY_TRIGGER_WINDOW
        m.ret_tol  = settings.RETEST_TOLERANCE

    return {"status": "success", "message": "Config updated."}


@app.post("/api/reset-strategy")
def reset_strategy_states():
    for m in setups.values():
        m.reset_state(0, "User reset")
    return {"status": "success", "message": "All setups reset to IDLE."}


@app.post("/api/reset-daily")
def reset_daily_state(db: Session = Depends(get_db)):
    from datetime import date as date_cls
    today = date_cls.today().strftime("%Y-%m-%d")
    state = db.query(DailyState).filter(DailyState.trade_date == today).first()
    if state:
        state.trade_count = 0
        state.is_blocked  = False
        db.commit()
    return {"status": "success", "message": f"Daily state reset for {today}."}


@app.post("/api/mock/run")
def run_mock_tick():
    """Trigger a single strategy tick when Mock Mode is enabled (dashboard button).
    This runs the same code path as the scheduled monitor_interval_tick but synchronously.
    """
    if not settings.MOCK_MODE:
        raise HTTPException(status_code=400, detail="Mock mode not enabled on server.")
    try:
        monitor_interval_tick()
        return {"status": "success", "message": "Mock tick executed."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
@app.get("/api/health")
def health():
    mkt = get_market_status_detail()
    ds  = upstox.data_source if upstox else "STARTING"
    return {"status": "ok", "market_status": mkt["market_status"], "data_source": ds}


@app.get("/api/v1/upstox-status")
def upstox_session_status(request: Request):
    if upstox is None:
        return {"connected": False, "token_status": "Starting", "expiry_status": "Server initialising"}
    upstox.ensure_authenticated()
    status = upstox.get_connection_status()
    status["calculated_redirect_uri"] = _resolve_redirect_uri(request)
    status["upstox_api_key"] = settings.UPSTOX_API_KEY
    status["env_redirect_uri"] = os.environ.get("UPSTOX_REDIRECT_URI", "")
    status["is_localhost_fallback"] = "localhost" in settings.UPSTOX_REDIRECT_URI
    return status


@app.get("/api/debug/cpr")
def debug_cpr(date: Optional[date] = Query(None, description="Target trading date YYYY-MM-DD")):
    if upstox is None:
        raise HTTPException(status_code=503, detail="Server still initialising")

    if date:
        prev = upstox.get_previous_day_ohlc_for_date(date)
    else:
        prev = upstox.get_previous_day_ohlc()

    if not prev:
        raise HTTPException(status_code=404, detail="Previous-day OHLC not available (authenticate Upstox)")

    levels = calculate_cpr_levels(prev["high"], prev["low"], prev["close"])
    return {"previous_ohlc": prev, "cpr_levels": levels.dict()}


@app.get("/api/report/historical")
def historical_report(date: Optional[date] = Query(None, description="Target date YYYY-MM-DD"), db: Session = Depends(get_db)):
    if date is None:
        date = datetime.utcnow().date()
    try:
        report = generate_historical_report(date.isoformat(), db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return report


@app.get("/api/backtest")
def run_backtest_endpoint(
    start: str = Query(..., description="Start date YYYY-MM-DD"),
    end:   str = Query(..., description="End date YYYY-MM-DD"),
    db: Session = Depends(get_db)
):
    try:
        start_date = date.fromisoformat(start)
        end_date   = date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end date must be >= start date")

    delta = (end_date - start_date).days
    if delta > 90:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 90 days")

    result = run_backtest(start_date, end_date, upstox)
    return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _resolve_redirect_uri(request: Request) -> str:
    uri = settings.UPSTOX_REDIRECT_URI
    if not os.environ.get("UPSTOX_REDIRECT_URI") or "localhost" in uri:
        host = request.url.hostname or ""
        if "localhost" not in host and "127.0.0.1" not in host:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            uri = f"{scheme}://{request.url.netloc}/callback"
    return uri


def _oauth_success_response():
    return HTMLResponse(content="""
    <!DOCTYPE html><html><head><title>Upstox Connected</title>
    <style>
      body{font-family:-apple-system,sans-serif;background:#0b0f19;color:#cbd5e1;
           display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
      .c{background:#111827;border:1px solid #10b981;border-radius:12px;padding:2.5rem;
         text-align:center;max-width:450px}
      h2{color:#10b981;margin-top:0}p{color:#94a3b8;font-size:.875rem}
    </style></head><body>
    <div class="c"><h2>⚡ Connected Successfully!</h2>
    <p>Upstox authenticated. This window will close automatically.</p>
    <script>
      if(window.opener){
        window.opener.postMessage({type:'OAUTH_AUTH_SUCCESS'},'*');
        setTimeout(()=>window.close(),1500);
      } else { window.location.href='/?upstox=success'; }
    </script></div></body></html>""")


@app.get("/callback")
def root_callback(request: Request, code: str, db: Session = Depends(get_db)):
    if upstox is None:
        raise HTTPException(503, "Server still initialising")
    success = upstox.authenticate(code, redirect_uri=_resolve_redirect_uri(request))
    if success:
        return _oauth_success_response()
    return HTMLResponse(content="""<html><body style="background:#0b0f19;color:#fff;font-family:sans-serif;
      display:flex;align-items:center;justify-content:center;height:100vh">
      <div style="text-align:center"><h2 style="color:#ef4444">Authentication Failed</h2>
      <a href="/" style="background:#4f46e5;color:#fff;padding:.75rem 1.5rem;border-radius:6px;
      text-decoration:none">Return to Dashboard</a></div></body></html>""", status_code=400)


@app.post("/postback")
async def postback(request: Request):
    try:
        body = await request.json()
        logger.info(f"/postback: {body}")
        return {"status": "received"}
    except Exception:
        return {"status": "received_raw"}


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        notify_signal_detected(body.get("source","Webhook"), body.get("message","Alert"))
        return {"status": "ok"}
    except Exception:
        return {"status": "ok"}


# Mount React SPA
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

if os.path.exists("./dist"):
    app.mount("/assets", StaticFiles(directory="./dist/assets"), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        skip = ("api/","api","callback","postback","health","webhook")
        if any(full_path.startswith(s) for s in skip):
            raise HTTPException(404)
        p = os.path.join("./dist", full_path)
        return FileResponse(p) if (os.path.exists(p) and os.path.isfile(p)) else FileResponse("./dist/index.html")
