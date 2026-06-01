import os
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from database.db import get_db, init_db
from database.models import Trade, DailyState, StrategyState
from brokers.upstox_client import UpstoxClient, is_market_open, get_market_status_detail
from risk.manager import RiskManager
from strategies.cpr_strategy import calculate_cpr_levels, is_inside_cpr, SetupStateMachine, CPRLevels
from telegram.bot import notify_signal_detected, notify_order_placed, notify_sl_hit, notify_tp_hit, notify_system_error

from reports.historical_report import generate_historical_report
from fastapi.responses import HTMLResponse
from fastapi import Request

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

# FIX: Create UpstoxClient AFTER init_db() runs (done in startup_event).
# The module-level reference is declared here but populated in startup_event.
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
    """
    Returns CPR levels computed from previous-day OHLC via Upstox.
    RULE 3: Returns None if Upstox is not authenticated — no hardcoded fallback.
    Caches until end of day (recomputes each new calendar day).
    """
    global _today_cpr_levels, _cpr_date
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    if _today_cpr_levels is not None and _cpr_date == today_str:
        return _today_cpr_levels  # valid cache

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
        _cpr_date = today_str   # don't hammer the API every tick
        logger.warning(
            "[DATA_SOURCE=DISCONNECTED] CPR levels unavailable — "
            "authenticate Upstox to enable live CPR."
        )
    return _today_cpr_levels


@app.on_event("startup")
def startup_event():
    global upstox

    # FIX: Init DB FIRST so UpstoxClient can load persisted token from DB on startup
    init_db()

    # NOW create UpstoxClient — DB is ready so token reload works
    upstox = UpstoxClient()
    logger.info("[STARTUP] Database initialised. UpstoxClient created.")

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        monitor_interval_tick,
        "cron",
        minute="*/5",
        second=30,
        id="nifty_monitor_job",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        check_active_position_targets,
        "interval",
        seconds=30,
        id="targets_monitor_job",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        keep_upstox_alive,
        "interval",
        minutes=10,
        id="upstox_keepalive_job",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("[STARTUP] Scheduler running: 5m strategy tick + 30s SL/TP checker + 10m Upstox keepalive.")


# ------------------------------------------------------------------
# Upstox keepalive (runs every 10 minutes)
# ------------------------------------------------------------------
def keep_upstox_alive():
    if upstox is None:
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
# SL / TP monitor (runs every 30s)
# ------------------------------------------------------------------
def check_active_position_targets():
    if upstox is None:
        return

    mkt = get_market_status_detail()
    if not mkt["market_open"]:
        return

    from database.db import SessionLocal
    db = SessionLocal()
    try:
        open_trade = db.query(Trade).filter(Trade.status == "OPEN").first()
        if not open_trade:
            return

        ltp = upstox.get_nifty_price()
        if ltp is None:
            logger.warning("[CMP_SOURCE=DISCONNECTED] Cannot check SL/TP — no live LTP.")
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
# Main strategy tick (runs every 5 minutes)
# ------------------------------------------------------------------
_last_processed_candle_time: Optional[str] = None


def monitor_interval_tick():
    global _last_processed_candle_time

    if upstox is None:
        return

    # RULE 1: Market hours gate
    mkt = get_market_status_detail()
    logger.info(
        f"[MARKET_OPEN={mkt['market_open']}] Tick at {mkt['current_ist']} "
        f"({mkt['weekday']}, holiday={mkt['is_holiday']})"
    )
    if not mkt["market_open"]:
        logger.info("[MARKET_OPEN=False] [STRATEGY_ALLOWED=False] Tick suppressed — market closed.")
        return

    # RULE 3: Auth gate
    if not upstox._is_authenticated():
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

        # RULE 2: Trade limit gate
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

                if not rm.can_trade():
                    continue

                # Double-lock: re-read trade count atomically before placing
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
                        rec.stop_loss  = details["stop_loss"]
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

    ltp = upstox.get_nifty_price()
    cmp_source = "UPSTOX_LTP" if ltp is not None else "DISCONNECTED"
    cmp_ts = datetime.utcnow().isoformat() if ltp is not None else None

    auth = upstox._is_authenticated()
    strategy_allowed = (
        mkt["market_open"]
        and auth
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
        "market_status": mkt["market_status"],
        "market_open": mkt["market_open"],
        "market_detail": mkt,
        "data_source": upstox.data_source,
        "nifty_ltp": ltp,
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
    """Mark an active trade CLOSED_MANUAL when you manually exit it in the broker."""
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

    return {
        "status": "success",
        "message": "Trade marked closed manually.",
        "trade_id": closed.id,
    }


@app.post("/api/trading/pause")
def pause_trading(db: Session = Depends(get_db)):
    """Pause automated trading for the current day."""
    rm = RiskManager(db)
    state = rm.get_or_create_daily_state()
    state.is_blocked = True
    db.commit()
    return {"status": "success", "message": "Trading paused for today."}


@app.post("/api/trading/resume")
def resume_trading(db: Session = Depends(get_db)):
    """Resume automated trading for the current day."""
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
    return _oauth_success_response() if success else HTTPException(400, "Authentication failed")


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
            except:
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
                json.dump(secrets, f)
            logger.info(f"Credentials persisted to {sp}")
        except Exception as e:
            raise HTTPException(500, f"Credential write error: {e}")

    for key, attr in [("failure_window","FAILURE_WINDOW"),("retest_window","RETEST_WINDOW"),
                      ("confirmation_window","CONFIRMATION_WINDOW"),("entry_trigger_window","ENTRY_TRIGGER_WINDOW")]:
        if key in data:
            setattr(settings, attr, int(data[key]))
    if "retest_tolerance" in data:
        settings.RETEST_TOLERANCE = float(data["retest_tolerance"])
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
    """Clear today's trade count — for testing purposes only."""
    from datetime import date as date_cls
    today = date_cls.today().strftime("%Y-%m-%d")
    state = db.query(DailyState).filter(DailyState.trade_date == today).first()
    if state:
        state.trade_count = 0
        state.is_blocked  = False
        db.commit()
    return {"status": "success", "message": f"Daily state reset for {today}."}


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
    status = upstox.get_connection_status()
    status["calculated_redirect_uri"] = _resolve_redirect_uri(request)
    status["upstox_api_key"] = settings.UPSTOX_API_KEY
    status["env_redirect_uri"] = os.environ.get("UPSTOX_REDIRECT_URI", "")
    status["is_localhost_fallback"] = "localhost" in settings.UPSTOX_REDIRECT_URI
    return status


@app.get("/api/debug/cpr")
def debug_cpr(date: Optional[date] = Query(None, description="Target trading date to inspect (YYYY-MM-DD)")):
    """Return previous-day OHLC and computed CPR levels for diagnostics.

    If `date` is provided, the previous trading day's OHLC for that date will be fetched.
    Otherwise the most recent previous-day OHLC is returned.
    """
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
    """
    Return actual executed trades from the DB for the given date and aggregated metrics.
    If `date` is omitted, defaults to today's date (UTC).
    """
    if date is None:
        date = datetime.utcnow().date()
    try:
        report = generate_historical_report(date.isoformat(), db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return report


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
    except:
        return {"status": "received_raw"}


@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()
        notify_signal_detected(body.get("source","Webhook"), body.get("message","Alert"))
        return {"status": "ok"}
    except:
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
