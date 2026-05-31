import os
import logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Depends, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from database.db import get_db, init_db
from database.models import Trade, DailyState, StrategyState
from brokers.upstox_client import UpstoxClient
from risk.manager import RiskManager
from strategies.cpr_strategy import calculate_cpr_levels, is_inside_cpr, SetupStateMachine, CPRLevels
from telegram.bot import notify_signal_detected, notify_order_placed, notify_sl_hit, notify_tp_hit, notify_system_error

from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi import Request

logger = logging.getLogger("CPR_System.Main")

app = FastAPI(
    title="CPR 4-Setup Automated Trading System",
    description="FastAPI terminal back-end executing NIFTY 5m automated CPR strategy"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global clients and state machines
upstox = UpstoxClient()
setups = {
    "SETUP_A": SetupStateMachine("SETUP_A"),
    "SETUP_B": SetupStateMachine("SETUP_B"),
    "SETUP_C": SetupStateMachine("SETUP_C"),
    "SETUP_D": SetupStateMachine("SETUP_D"),
}

# ---------------------------------------------------------------
# CPR Level management — uses LIVE previous-day OHLC when possible
# ---------------------------------------------------------------
_today_cpr_levels: Optional[CPRLevels] = None
_cpr_date: Optional[str] = None  # Track which date CPR was last computed for

def get_today_cpr_levels(db: Session) -> CPRLevels:
    global _today_cpr_levels, _cpr_date
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Recompute CPR each new trading day
    if _today_cpr_levels is None or _cpr_date != today_str:
        prev_ohlc = upstox.get_previous_day_ohlc()
        if prev_ohlc:
            _today_cpr_levels = calculate_cpr_levels(
                prev_ohlc["high"], prev_ohlc["low"], prev_ohlc["close"]
            )
            _cpr_date = today_str
            logger.info(
                f"[LIVE] CPR levels computed from live previous-day OHLC: "
                f"Pivot={_today_cpr_levels.pivot}, TC={_today_cpr_levels.tc}, "
                f"BC={_today_cpr_levels.bc}, R1={_today_cpr_levels.r1}, S1={_today_cpr_levels.s1}"
            )
        else:
            # Fallback hardcoded values when no live data available
            prev_hi, prev_lo, prev_cl = 19600.0, 19400.0, 19520.0
            _today_cpr_levels = calculate_cpr_levels(prev_hi, prev_lo, prev_cl)
            _cpr_date = today_str
            logger.warning(
                "CPR levels computed from HARDCODED fallback values (no live OHLC available). "
                "Authenticate with Upstox to get accurate CPR levels."
            )

    return _today_cpr_levels


@app.on_event("startup")
def startup_event():
    init_db()
    scheduler = BackgroundScheduler()
    # Run on completed 5-minute candle boundaries (offset +30s to let candle fully close)
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
    scheduler.start()
    logger.info(
        "[LIVE] Scheduler started: monitoring NIFTY 50 on 5-minute candle boundaries (cron */5 +30s offset) "
        "& checking SL/TP targets every 30s."
    )


def check_active_position_targets():
    """
    Checks active open positions and evaluates SL / TP limits against current LTP.
    """
    from database.db import SessionLocal
    db = SessionLocal()
    try:
        if not upstox.is_market_open():
            logger.info("MARKET_OPEN=False — skipping active position target checks until market reopens.")
            return
        if upstox.data_source == "DISCONNECTED":
            logger.warning("DATA_SOURCE=DISCONNECTED — cannot evaluate active positions without live Upstox data.")
            return

        open_trade = db.query(Trade).filter(Trade.status == "OPEN").first()
        if not open_trade:
            return

        current_ltp = upstox.get_nifty_price()
        is_sl_hit = False
        is_tp_hit = False

        if open_trade.setup_name in ["SETUP_B", "SETUP_C"]:
            if current_ltp <= open_trade.stop_loss:
                is_sl_hit = True
            elif current_ltp >= open_trade.take_profit:
                is_tp_hit = True
        elif open_trade.setup_name in ["SETUP_A", "SETUP_D"]:
            if current_ltp >= open_trade.stop_loss:
                is_sl_hit = True
            elif current_ltp <= open_trade.take_profit:
                is_tp_hit = True

        if is_sl_hit or is_tp_hit:
            exit_status = "CLOSED_SL" if is_sl_hit else "CLOSED_TP"
            premium_change = (current_ltp - open_trade.entry_price) * (
                1 if open_trade.trade_type == "BUY" else -1
            )
            exit_premium = max(5.0, open_trade.entry_price + (premium_change * 0.5))

            logger.info(
                f"TARGET TRIGGERED for {open_trade.setup_name} (ID: {open_trade.id})! "
                f"SL Hit: {is_sl_hit}, TP Hit: {is_tp_hit}. Index LTP: {current_ltp}"
            )

            upstox.place_order(open_trade.option_symbol, "SELL", open_trade.lots, paper=open_trade.is_paper)

            rm = RiskManager(db)
            closed_trade = rm.register_trade_exit(open_trade.id, exit_premium, exit_status)

            if is_sl_hit:
                notify_sl_hit(
                    closed_trade.setup_name, closed_trade.option_symbol,
                    abs(closed_trade.pnl), rm.get_or_create_daily_state().realized_pnl
                )
            else:
                notify_tp_hit(
                    closed_trade.setup_name, closed_trade.option_symbol,
                    closed_trade.pnl, rm.get_or_create_daily_state().realized_pnl
                )

    except Exception as e:
        logger.error(f"Error checking active position targets: {e}")
    finally:
        db.close()


# Track the last processed candle time to avoid reprocessing the same candle
_last_processed_candle_time: Optional[str] = None


def monitor_interval_tick():
    """
    Fetches the latest COMPLETED 5-minute candles from Upstox and feeds ONLY
    the newest completed candle to the strategy engine.

    Architecture:
        Upstox Live Data
            ↓
        5-minute Candle Builder (Upstox intraday API)
            ↓
        CPR Strategy Engine (processes only newly completed candles)
            ↓
        Dashboard
    """
    global _last_processed_candle_time

    from database.db import SessionLocal
    db = SessionLocal()
    try:
        market_open = upstox.is_market_open()
        logger.info(f"MARKET_OPEN={market_open} DATA_SOURCE={upstox.data_source} CMP_SOURCE={upstox.cmp_source}")
        if not market_open:
            logger.info("Market is currently closed — skipping candle processing and strategy evaluation.")
            return
        if upstox.data_source == "DISCONNECTED":
            logger.warning("Live Upstox data unavailable (DISCONNECTED). Strategy execution paused until connection is restored.")
            return

        # 1. Fetch all of today's completed 5m candles from Upstox
        candles = upstox.get_nifty_ohlc_5m()
        if not candles:
            logger.warning("[LIVE] No candles returned from Upstox — skipping strategy tick.")
            return

        logger.info(
            f"[LIVE] Strategy tick: {len(candles)} candles available. "
            f"Data source: {upstox.data_source}"
        )

        # 2. Identify the latest COMPLETED candle
        # The last candle in the list is the most recently completed 5m bar
        latest_candle = candles[-1]
        latest_candle_time = latest_candle.get("time", "")

        # 3. Skip if we already processed this candle (avoid duplicate evaluations)
        if latest_candle_time == _last_processed_candle_time:
            logger.debug(
                f"[LIVE] Candle at {latest_candle_time} already processed — skipping."
            )
            return

        # 4. Get today's CPR levels
        levels = get_today_cpr_levels(db)

        # 5. Feed ALL candles to state machines to synchronise intermediate states,
        #    but only ACT on signals from the latest completed candle.
        rm = RiskManager(db)

        for i, candle in enumerate(candles):
            is_latest = (i == len(candles) - 1)
            for name, machine in setups.items():
                triggered, details = machine.update(candle, i, levels)

                if triggered and is_latest:
                    logger.info(
                        f"[LIVE] Strategy evaluated — {name} TRIGGERED on candle "
                        f"{latest_candle_time} | Close={latest_candle['close']}"
                    )

                    if _trading_paused:
                        logger.warning(f"[PAUSED] Signal detected for {name} but trading is PAUSED by user. Skipping order.")
                        continue

                    if rm.can_trade():
                        opt_sym, strike, opt_type = upstox.select_atm_option(
                            candle["close"], details["trade_type"]
                        )

                        logger.info(
                            f"[LIVE] Signal generated — {name} {details['trade_type']} "
                            f"| Option: {opt_sym} | SL: {details['stop_loss']} | TP: {details['take_profit']}"
                        )
                        notify_signal_detected(
                            name,
                            f"Entry triggered at Index {candle['close']}. Buying option {opt_sym}."
                        )

                        order = upstox.place_order(
                            opt_sym, "BUY", details["lots"],
                            paper=(settings.TRADING_MODE == "paper")
                        )

                        if order["status"] == "success":
                            recorded_trade = rm.register_trade_entry(
                                setup_name=name,
                                trade_type=details["trade_type"],
                                option_symbol=opt_sym,
                                strike=strike,
                                option_type=opt_type,
                                entry_price=order["avg_price"],
                                is_paper=(settings.TRADING_MODE == "paper")
                            )
                            recorded_trade.stop_loss = details["stop_loss"]
                            recorded_trade.take_profit = details["take_profit"]
                            db.commit()

                            notify_order_placed(
                                setup=name,
                                buy_sell="BUY",
                                details=(
                                    f"Option: `{opt_sym}`\nLot Size: `1`\n"
                                    f"Premium: `₹{order['avg_price']:.2f}`\n"
                                    f"Index SL: `{details['stop_loss']:.2f}`\n"
                                    f"Index TP: `{details['take_profit']:.2f}`"
                                )
                            )
                        else:
                            notify_system_error(f"Failed to execute order for {name}: {order['message']}")

        # 6. Mark this candle as processed
        _last_processed_candle_time = latest_candle_time
        logger.info(f"[LIVE] Strategy evaluated for candle at {latest_candle_time}.")

    except Exception as e:
        logger.error(f"Error in monitor_interval_tick: {e}")
        notify_system_error(f"Error in candle tick processor: {e}")
    finally:
        db.close()


# ---------------------------------------------------------------
# API ENDPOINTS
# ---------------------------------------------------------------

@app.get("/api/status")
def get_system_status(db: Session = Depends(get_db)):
    levels = get_today_cpr_levels(db)
    rm = RiskManager(db)
    daily = rm.get_or_create_daily_state()
    conn_status = upstox.get_connection_status()

    # Use the cached last-known LTP if live fetch is unavailable to avoid
    # forcing a network call during status requests (and to surface the
    # most recent CMP when market is closed).
    current_ltp = upstox.last_known_ltp
    # If the client has gone disconnected but a cached LTP exists, indicate that
    # the value is a cached source so the frontend can label it accordingly.
    cmp_source = upstox.cmp_source if upstox.cmp_source != "DISCONNECTED" else (
        "CACHED" if upstox.last_known_ltp is not None else "DISCONNECTED"
    )

    return {
        "status": "Running",
        "timestamp": datetime.utcnow().isoformat(),
        "trading_mode": settings.TRADING_MODE,
        "market_status": "OPEN" if upstox.is_market_open() else "CLOSED",
        "cpr_levels": levels,
        "nifty_ltp": current_ltp,
        "cmp_source": cmp_source,
        "last_cmp_update_time": upstox.last_cmp_update_time,
        "data_source": upstox.data_source,
        "last_live_candle_time": upstox.last_live_candle_time,
        "websocket_status": upstox.websocket_status,
        "strategy_allowed": (
            upstox.is_market_open() and
            upstox.data_source == "UPSTOX LIVE" and
            not daily.is_blocked and
            daily.trade_count < settings.MAX_DAILY_TRADES and
            daily.realized_pnl > -settings.DAILY_LOSS_LIMIT
        ),
        "daily_summary": {
            "date": daily.trade_date,
            "trade_count": daily.trade_count,
            "realized_pnl": daily.realized_pnl,
            "is_blocked": daily.is_blocked
        },
        "limits": {
            "max_trades": settings.MAX_DAILY_TRADES,
            "loss_limit": settings.DAILY_LOSS_LIMIT,
            "lots": settings.POSITION_LOTS
        }
    }


@app.get("/api/setups")
def get_active_setups():
    data = {}
    for name, m in setups.items():
        data[name] = {
            "state": m.state,
            "state_bar": m.state_bar,
            "elapsed_bars": m.bars_elapsed(m.state_bar),
            "retest_high": m.r_high,
            "retest_low": m.r_low,
            "confirmation_high": m.c_high,
            "confirmation_low": m.c_low,
            "configs": {
                "fail_win": m.fail_win,
                "ret_win": m.ret_win,
                "con_win": m.con_win,
                "ent_win": m.ent_win,
                "ret_tol": m.ret_tol
            }
        }
    return data


@app.get("/api/trades")
def get_recent_trades(db: Session = Depends(get_db)):
    trades = db.query(Trade).order_by(Trade.entry_time.desc()).all()
    win_count = len([t for t in trades if t.pnl > 0])
    loss_count = len([t for t in trades if t.pnl < 0])
    total = len(trades)
    win_rate = (win_count / total * 100) if total > 0 else 0.0

    return {
        "trades": trades,
        "metrics": {
            "total_trades": total,
            "win_rate": round(win_rate, 2),
            "wins": win_count,
            "losses": loss_count,
            "gross_profit": sum([t.pnl for t in trades if t.pnl > 0]),
            "gross_loss": sum([t.pnl for t in trades if t.pnl < 0]),
            "net_pnl": sum([t.pnl for t in trades])
        }
    }


@app.get("/api/v1/login-url")
def get_upstox_login(request: Request):
    redirect_uri = settings.UPSTOX_REDIRECT_URI
    if not os.environ.get("UPSTOX_REDIRECT_URI") or "localhost" in settings.UPSTOX_REDIRECT_URI:
        if "localhost" not in request.url.hostname and "127.0.0.1" not in request.url.hostname:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            redirect_uri = f"{scheme}://{request.url.netloc}/callback"
    logger.info(f"Dynamic Resolve: calculated login redirect_uri is {redirect_uri}")
    return {"url": upstox.get_login_url(override_redirect_uri=redirect_uri)}


@app.get("/api/v1/callback")
def upstox_callback(request: Request, code: str, db: Session = Depends(get_db)):
    redirect_uri = settings.UPSTOX_REDIRECT_URI
    if not os.environ.get("UPSTOX_REDIRECT_URI") or "localhost" in settings.UPSTOX_REDIRECT_URI:
        if "localhost" not in request.url.hostname and "127.0.0.1" not in request.url.hostname:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            redirect_uri = f"{scheme}://{request.url.netloc}/callback"
    success = upstox.authenticate(code, redirect_uri=redirect_uri)
    if success:
        return _oauth_success_response()
    else:
        raise HTTPException(status_code=400, detail="Authentication failed with code provided")


@app.post("/api/config")
def update_config(data: dict, db: Session = Depends(get_db)):
    """Allows client-side UI to live adjust thresholds, buffers and trading modes."""
    global _today_cpr_levels, _cpr_date

    if "upstox_api_key" in data or "upstox_api_secret" in data:
        import json
        secrets = {}
        secrets_path = getattr(settings, "UPSTOX_SECRETS_PATH", None)
        if secrets_path and os.path.exists(secrets_path):
            try:
                with open(secrets_path, "r") as f:
                    secrets = json.load(f)
            except:
                pass

        if "upstox_api_key" in data:
            key_val = str(data["upstox_api_key"]).strip()
            settings.UPSTOX_API_KEY = key_val
            upstox.api_key = key_val
            secrets["api_key"] = key_val
        if "upstox_api_secret" in data:
            sec_val = str(data["upstox_api_secret"]).strip()
            settings.UPSTOX_API_SECRET = sec_val
            upstox.api_secret = sec_val
            secrets["api_secret"] = sec_val

        if not secrets_path:
            secrets_path = os.path.abspath(
                os.path.join(os.path.dirname(os.path.dirname(__file__)), "upstox_secrets.json")
            )
        try:
            with open(secrets_path, "w") as f:
                json.dump(secrets, f)
            logger.info(f"Persisted dynamic Upstox API credentials inside {secrets_path} successfully.")
        except Exception as e:
            logger.error(f"Error persisting dynamic seeds file inside {secrets_path}: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Write permission error writing credentials into {secrets_path}: {e}"
            )

    if "failure_window" in data:
        settings.FAILURE_WINDOW = int(data["failure_window"])
    if "retest_window" in data:
        settings.RETEST_WINDOW = int(data["retest_window"])
    if "confirmation_window" in data:
        settings.CONFIRMATION_WINDOW = int(data["confirmation_window"])
    if "entry_trigger_window" in data:
        settings.ENTRY_TRIGGER_WINDOW = int(data["entry_trigger_window"])
    if "retest_tolerance" in data:
        settings.RETEST_TOLERANCE = float(data["retest_tolerance"])
    if "trading_mode" in data:
        new_mode = data["trading_mode"]
        if new_mode in ["paper", "live"]:
            settings.TRADING_MODE = new_mode

    for m in setups.values():
        m.fail_win = settings.FAILURE_WINDOW
        m.ret_win = settings.RETEST_WINDOW
        m.con_win = settings.CONFIRMATION_WINDOW
        m.ent_win = settings.ENTRY_TRIGGER_WINDOW
        m.ret_tol = settings.RETEST_TOLERANCE

    logger.info(f"Updated live strategy parameters. Active Mode: {settings.TRADING_MODE}")
    return {"status": "success", "message": "Configurations updated successfully."}


@app.post("/api/simulate-candle")
def simulate_candle(data: dict, db: Session = Depends(get_db)):
    """
    Playground endpoint: Receives custom OHLC data, feeds it into strategy engine.
    Only for manual testing — the live engine uses get_nifty_ohlc_5m().
    """
    o = float(data.get("open", 19500))
    h = float(data.get("high", 19520))
    l = float(data.get("low", 19480))
    c = float(data.get("close", 19510))
    idx = int(data.get("index", 1))

    levels = get_today_cpr_levels(db)
    candle = {"open": o, "high": h, "low": l, "close": c}

    triggers = {}
    for name, machine in setups.items():
        triggered, details = machine.update(candle, idx, levels)
        triggers[name] = {
            "triggered": triggered,
            "details": details,
            "current_state": machine.state,
            "retest_high": machine.r_high,
            "retest_low": machine.r_low,
            "confirmation_high": machine.c_high,
            "confirmation_low": machine.c_low
        }
    return {"candle": candle, "cpr_levels": levels, "indicators": triggers}


@app.post("/api/reset-strategy")
def reset_strategy_states():
    """Resets all strategy state machines to IDLE (state 0)"""
    for name, m in setups.items():
        m.reset_state(0, "User manual reset")
    return {"status": "success", "message": "All strategy setup state machines reset to IDLE."}


# ---------------------------------------------------------------
# FEATURE 1: Pause / Resume Trading
# ---------------------------------------------------------------
_trading_paused: bool = False

@app.post("/api/pause-trading")
def pause_trading():
    """Pauses all live trade execution. Strategy engine still runs but will not place any orders."""
    global _trading_paused
    _trading_paused = True
    logger.warning("TRADING PAUSED by user via dashboard. No new orders will be placed.")
    return {"status": "paused", "message": "Trading paused. No new orders will be placed until resumed."}

@app.post("/api/resume-trading")
def resume_trading():
    """Resumes live trade execution after a pause."""
    global _trading_paused
    _trading_paused = False
    logger.info("TRADING RESUMED by user via dashboard.")
    return {"status": "active", "message": "Trading resumed. System will place orders on valid signals."}

@app.get("/api/trading-paused")
def get_pause_state():
    return {"paused": _trading_paused}


# ---------------------------------------------------------------
# FEATURE 2: Manual Close — mark an open trade as closed in DB
# ---------------------------------------------------------------
@app.post("/api/trades/{trade_id}/manual-close")
def manual_close_trade(trade_id: int, data: dict, db: Session = Depends(get_db)):
    """
    Marks an OPEN trade as manually closed in the system DB.
    Call this after you square off a position directly in your broker app
    so the system stops trying to manage or re-exit that trade.
    Body: { "exit_price": 123.45 }  (the premium you exited at, optional — defaults to entry price)
    """
    trade = db.query(Trade).filter(Trade.id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail=f"Trade ID {trade_id} not found.")
    if trade.status != "OPEN":
        raise HTTPException(status_code=400, detail=f"Trade ID {trade_id} is already closed (status: {trade.status}).")

    exit_price = float(data.get("exit_price", trade.entry_price))
    qty = trade.lots * settings.NIFTY_LOT_SIZE
    pnl = (exit_price - trade.entry_price) * qty

    trade.exit_price = exit_price
    trade.exit_time = datetime.utcnow()
    trade.status = "CLOSED_MANUAL"
    trade.pnl = pnl

    daily_state = db.query(DailyState).filter(
        DailyState.trade_date == date.today().strftime("%Y-%m-%d")
    ).first()
    if daily_state:
        daily_state.realized_pnl += pnl

    db.commit()
    logger.info(f"Trade ID {trade_id} manually closed via dashboard. Exit price: {exit_price}, PNL: ₹{pnl:.2f}")
    return {
        "status": "success",
        "message": f"Trade {trade_id} marked as CLOSED_MANUAL. System will no longer manage this position.",
        "pnl": round(pnl, 2)
    }


# ---------------------------------------------------------------
# FEATURE 3: Keepalive ping endpoint (prevents Render free tier sleep)
# ---------------------------------------------------------------
@app.get("/ping")
def ping():
    """Lightweight keepalive endpoint. Hit this every 10 minutes to prevent Render free tier sleep."""
    return {"pong": True, "ts": datetime.utcnow().isoformat()}


@app.get("/health")
@app.get("/api/health")
def health_endpoint():
    return {"status": "ok"}


@app.get("/api/v1/upstox-status")
def view_upstox_session_status(request: Request):
    """Returns detailed login metrics, expiry times, and session health for the client dashboard."""
    status = upstox.get_connection_status()

    redirect_uri = settings.UPSTOX_REDIRECT_URI
    is_fallback = False
    if not os.environ.get("UPSTOX_REDIRECT_URI") or "localhost" in settings.UPSTOX_REDIRECT_URI:
        if "localhost" not in request.url.hostname and "127.0.0.1" not in request.url.hostname:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            redirect_uri = f"{scheme}://{request.url.netloc}/callback"
            is_fallback = True

    status["calculated_redirect_uri"] = redirect_uri
    status["upstox_api_key"] = settings.UPSTOX_API_KEY
    status["env_redirect_uri"] = os.environ.get("UPSTOX_REDIRECT_URI", "")
    status["is_localhost_fallback"] = is_fallback
    return status


# ---------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------

def _oauth_success_response():
    return HTMLResponse(content="""
    <!DOCTYPE html><html><head><title>Upstox Connected</title>
    <style>
      body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#0b0f19;color:#cbd5e1;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
      .c{background:#111827;border:1px solid #10b981;border-radius:12px;padding:2.5rem;text-align:center;max-width:450px;box-shadow:0 10px 15px -3px rgba(0,0,0,.3)}
      h2{color:#10b981;margin-top:0;font-size:1.5rem}
      p{color:#94a3b8;font-size:.875rem;line-height:1.5;margin:1rem 0 1.5rem}
    </style></head><body>
    <div class="c"><h2>⚡ Connected Successfully!</h2>
    <p>Upstox account authenticated. This window will now close.</p>
    <script>
      if(window.opener){window.opener.postMessage({type:'OAUTH_AUTH_SUCCESS'},'*');setTimeout(()=>window.close(),1000);}
      else{window.location.href='/?upstox=success';}
    </script></div></body></html>""")


@app.get("/callback")
def upstox_root_callback_handler(request: Request, code: str, db: Session = Depends(get_db)):
    redirect_uri = settings.UPSTOX_REDIRECT_URI
    if not os.environ.get("UPSTOX_REDIRECT_URI") or "localhost" in settings.UPSTOX_REDIRECT_URI:
        if "localhost" not in request.url.hostname and "127.0.0.1" not in request.url.hostname:
            scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
            redirect_uri = f"{scheme}://{request.url.netloc}/callback"
    success = upstox.authenticate(code, redirect_uri=redirect_uri)
    if success:
        return _oauth_success_response()
    else:
        logger.error("Root /callback OAuth code exchange failed.")
        return HTMLResponse(content="""
        <html><body style="font-family:sans-serif;background:#0b0f19;color:#cbd5e1;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
        <div style="background:#111827;border:1px solid #dc2626;border-radius:12px;padding:2.5rem;text-align:center;max-width:450px">
        <h2 style="color:#ef4444">Authentication Failed</h2>
        <p style="color:#94a3b8">Unable to exchange authorization code. Check API secret and redirect URI settings.</p>
        <a href="/" style="background:#4f46e5;color:white;padding:.75rem 1.5rem;border-radius:6px;text-decoration:none">Return to Dashboard</a>
        </div></body></html>""", status_code=400)


@app.post("/postback")
async def upstox_postback_handler(request: Request):
    try:
        body = await request.json()
        logger.info(f"Incoming /postback payload: {body}")
        order_status = body.get("order_status", "UNKNOWN")
        order_key = body.get("order_id", "UNKNOWN")
        side = body.get("transaction_type", "BUY")
        return {"status": "received", "order_id": order_key, "action": side, "status_detail": order_status}
    except Exception as e:
        logger.error(f"Error parsing /postback payload: {e}")
        raw_body = await request.body()
        logger.warning(f"Raw postback: {raw_body.decode(errors='ignore')}")
        return {"status": "received_raw", "message": "Failed JSON parse, logged raw body."}


@app.post("/webhook")
async def external_webhook_trigger_handler(request: Request):
    try:
        body = await request.json()
        logger.info(f"Incoming /webhook alert: {body}")
        from telegram.bot import notify_signal_detected
        notify_signal_detected(body.get("source", "External Webhook"), body.get("message", "Strategy alert"))
        return {"status": "ok", "message": "Webhook received and logged."}
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return {"status": "ok", "message": "Raw webhook logged."}


# Mount frontend React SPA
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

if os.path.exists("./dist"):
    app.mount("/assets", StaticFiles(directory="./dist/assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_single_page_app(full_path: str):
        skip = ("api/", "api", "callback", "postback", "health", "webhook")
        if any(full_path.startswith(s) for s in skip):
            raise HTTPException(status_code=404, detail="API route not found")
        item_path = os.path.join("./dist", full_path)
        if os.path.exists(item_path) and os.path.isfile(item_path):
            return FileResponse(item_path)
        return FileResponse("./dist/index.html")
