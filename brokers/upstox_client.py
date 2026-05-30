import requests
import logging
from typing import Dict, List, Optional, Tuple, Literal
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from config.settings import settings

logger = logging.getLogger("CPR_System.Upstox")


class UpstoxClient:
    def __init__(self):
        import os
        import json
        self.api_key = settings.UPSTOX_API_KEY
        self.api_secret = settings.UPSTOX_API_SECRET
        self.redirect_uri = settings.UPSTOX_REDIRECT_URI
        self.access_token: Optional[str] = None
        self.base_url = "https://api.upstox.com/v2"

        # Track last live candle metadata for dashboard status cards
        self.last_live_candle_time: Optional[str] = None
        self.last_cmp_update_time: Optional[str] = None
        # Cache the last successfully fetched LTP so the dashboard can show
        # the most recently known CMP even when live fetching fails.
        self.last_known_ltp: Optional[float] = None
        self.cmp_source: str = "DISCONNECTED"
        self.websocket_status: str = "Disconnected"
        self.data_source: str = "DISCONNECTED"  # "UPSTOX LIVE" | "HISTORICAL REPLAY" | "SIMULATION" | "DISCONNECTED"
        self.market_status: str = "CLOSED"

        # Dynamically load saved client credentials if configured via UI dashboard
        secrets_path = getattr(settings, "UPSTOX_SECRETS_PATH", None)
        if secrets_path and os.path.exists(secrets_path):
            try:
                with open(secrets_path, "r") as f:
                    secrets = json.load(f)
                    if secrets.get("api_key"):
                        self.api_key = secrets["api_key"]
                        settings.UPSTOX_API_KEY = secrets["api_key"]
                    if secrets.get("api_secret"):
                        self.api_secret = secrets["api_secret"]
                        settings.UPSTOX_API_SECRET = secrets["api_secret"]
                logger.info(f"Successfully loaded dynamic Upstox Client credentials from {secrets_path}")
            except Exception as e:
                logger.error(f"Failed to load dynamic credentials from {secrets_path}: {e}")

    def get_token(self) -> Optional[str]:
        """Loads and returns active access token, checking DB if memory is stale or uninitialized."""
        if self.access_token:
            return self.access_token

        from database.db import SessionLocal
        from database.models import UpstoxToken
        if SessionLocal is None:
            return None

        db = SessionLocal()
        try:
            tok = db.query(UpstoxToken).order_by(UpstoxToken.id.desc()).first()
            if tok and tok.access_token:
                last_auth = tok.last_authenticated_at or tok.created_at
                if (datetime.utcnow() - last_auth) < timedelta(hours=24):
                    self.access_token = tok.access_token
                    return self.access_token
                else:
                    logger.warning("DB Upstox token has expired (older than 24 hours).")
        except Exception as e:
            logger.error(f"Error retrieving Upstox access token from SQL DB: {e}")
        finally:
            db.close()
        return None

    def _now_ist(self) -> datetime:
        return datetime.utcnow().astimezone(ZoneInfo("Asia/Kolkata"))

    def is_market_open(self) -> bool:
        """Returns True when NSE is currently open for intraday trading."""
        local_dt = self._now_ist()
        if local_dt.weekday() >= 5:
            return False
        open_time = time(9, 15)
        close_time = time(15, 30)
        return open_time <= local_dt.time() < close_time

    def get_connection_status(self) -> dict:
        """Evaluates active connection metadata and outputs live status for the React dashboard."""
        from database.db import SessionLocal
        from database.models import UpstoxToken

        if SessionLocal is None:
            return {
                "connected": False,
                "token_status": "Missing",
                "expiry_status": "No DB Session Engine configured",
                "last_authenticated": None,
                "token_preview": "None",
                "data_source": self.data_source,
                "last_live_candle_time": self.last_live_candle_time,
                "websocket_status": self.websocket_status,
                "market_status": "OPEN" if self.is_market_open() else "CLOSED",
                "cmp_source": self.cmp_source,
                "last_cmp_update_time": self.last_cmp_update_time,
            }

        db = SessionLocal()
        try:
            tok = db.query(UpstoxToken).order_by(UpstoxToken.id.desc()).first()
            if not tok or not tok.access_token:
                return {
                    "connected": False,
                    "token_status": "Missing",
                    "expiry_status": "Access token not stored, click Connect to pair",
                    "last_authenticated": None,
                    "token_preview": "None",
                    "data_source": self.data_source,
                    "last_live_candle_time": self.last_live_candle_time,
                    "websocket_status": self.websocket_status,
                    "market_status": "OPEN" if self.is_market_open() else "CLOSED",
                    "cmp_source": self.cmp_source,
                    "last_cmp_update_time": self.last_cmp_update_time,
                }

            last_auth = tok.last_authenticated_at or tok.created_at
            now = datetime.utcnow()
            expires_at = last_auth + timedelta(hours=24)
            time_left = expires_at - now

            if time_left.total_seconds() <= 0:
                status = "Expired"
                expiry_desc = "Expired (Authentication token older than 24h)"
                connected = False
            else:
                status = "Active"
                hours = int(time_left.total_seconds() // 3600)
                minutes = int((time_left.total_seconds() % 3600) // 60)
                expiry_desc = f"Active (Valid for {hours} hours, {minutes} minutes)"
                connected = True

            token_preview = tok.access_token[:8] + "..." + tok.access_token[-8:] if len(tok.access_token) > 16 else "Valid Token"

            return {
                "connected": connected,
                "token_status": status,
                "expiry_status": expiry_desc,
                "expires_at": expires_at.isoformat(),
                "last_authenticated": last_auth.isoformat() if last_auth else None,
                "token_preview": token_preview,
                "data_source": self.data_source,
                "last_live_candle_time": self.last_live_candle_time,
                "websocket_status": self.websocket_status,
                "market_status": "OPEN" if self.is_market_open() else "CLOSED",
                "cmp_source": self.cmp_source,
                "last_cmp_update_time": self.last_cmp_update_time,
            }
        except Exception as e:
            logger.error(f"Error pulling custom Upstox state properties: {e}")
            return {
                "connected": False,
                "token_status": "Database Error",
                "expiry_status": str(e),
                "last_authenticated": None,
                "token_preview": "Error",
                "data_source": self.data_source,
                "last_live_candle_time": self.last_live_candle_time,
                "websocket_status": self.websocket_status,
                "market_status": "OPEN" if self.is_market_open() else "CLOSED",
                "cmp_source": self.cmp_source,
                "last_cmp_update_time": self.last_cmp_update_time,
            }
        finally:
            db.close()

    def get_login_url(self, override_redirect_uri: Optional[str] = None) -> str:
        """Generates the URL to redirect the user to for OAuth consent."""
        r_uri = override_redirect_uri or self.redirect_uri
        logger.info(f"Generating OAuth Login Redirect flow with CLIENT_ID: {self.api_key} and REDIRECT_URI: {r_uri}")
        return f"https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id={self.api_key}&redirect_uri={r_uri}"

    def authenticate(self, auth_code: str, redirect_uri: Optional[str] = None) -> bool:
        """Exchanges auth code for an access token and stores it persistently in the database."""
        url = f"{self.base_url}/login/authorization/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }
        r_uri = redirect_uri or self.redirect_uri
        data = {
            "code": auth_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": r_uri,
            "grant_type": "authorization_code"
        }

        try:
            logger.info("Initiating Upstox API token exchange process with authorization code.")
            response = requests.post(url, data=data, headers=headers, timeout=10)
            if response.status_code == 200:
                resp_json = response.json()
                self.access_token = resp_json.get("access_token")
                logger.info("[LIVE] Connected to Upstox — OAuth token exchange successful.")

                from database.db import SessionLocal
                from database.models import UpstoxToken
                if SessionLocal:
                    db = SessionLocal()
                    try:
                        db.query(UpstoxToken).delete()
                        new_tok = UpstoxToken(
                            access_token=self.access_token,
                            status="Connected",
                            last_authenticated_at=datetime.utcnow()
                        )
                        db.add(new_tok)
                        db.commit()
                        logger.info("Upstox token records written successfully to database.")
                    except Exception as sqle:
                        logger.error(f"SQL database update failed during access token registration: {sqle}")
                    finally:
                        db.close()
                return True
            else:
                logger.error(f"Upstox OAuth Authorization Handshake Failed (HTTP {response.status_code}): {response.text}")
                return False
        except Exception as e:
            logger.error(f"Upstox network exchange failed with terminal error: {e}")
            return False

    def _is_mock_mode(self) -> bool:
        """Returns True when there is no valid token or credentials are still at default mock values."""
        token = self.get_token()
        if not token:
            return True
        if self.api_key in ("mock_api_key", "mock_key", ""):
            return True
        return False

    def get_nifty_ohlc_5m(self) -> List[Dict]:
        """
        Fetches today's 1-minute intraday candles from Upstox and aggregates
        them into 5-minute candles aligned to market open (09:15 IST).
        Upstox intraday endpoint only supports 1minute and 30minute intervals —
        there is no native 5minute interval, so we build it ourselves.
        Falls back to mock data only when unauthenticated.
        """
        if self._is_mock_mode():
            logger.warning(
                "Upstox client is unauthenticated or credentials are still at default mock values. "
                "Live data unavailable — DISCONNECTED. Strategy execution must pause until valid credentials are provided."
            )
            self.data_source = "DISCONNECTED"
            self.websocket_status = "Disconnected"
            self.cmp_source = "DISCONNECTED"
            self.last_live_candle_time = None
            return []

        token = self.get_token()
        instrument_key = "NSE_INDEX|Nifty 50"
        # Upstox intraday endpoint only supports 1minute and 30minute
        url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/1minute"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                candles_raw = data.get("data", {}).get("candles", [])
                if not candles_raw:
                    logger.warning("[LIVE] Upstox returned 0 intraday 1m candles (market may be pre-open or closed).")
                    self.data_source = "UPSTOX LIVE"
                    self.websocket_status = "Connected"
                    self.last_live_candle_time = None
                    return []

                # Parse 1-minute candles — Upstox returns newest first
                one_min = []
                for c in candles_raw:
                    one_min.append({
                        "time":   c[0],
                        "open":   float(c[1]),
                        "high":   float(c[2]),
                        "low":    float(c[3]),
                        "close":  float(c[4]),
                        "volume": int(c[5]),
                    })
                one_min.reverse()  # chronological order oldest→newest

                # Aggregate 1m bars into 5m candles
                five_min = self._aggregate_to_5min(one_min)

                if five_min:
                    self.data_source = "UPSTOX LIVE"
                    self.websocket_status = "Connected"
                    self.last_live_candle_time = five_min[-1]["time"]
                    logger.info(
                        f"[LIVE] New candle received — {len(five_min)} completed 5m candles built "
                        f"from {len(one_min)} 1m bars. "
                        f"Latest close: {five_min[-1]['close']} at {five_min[-1]['time']}"
                    )
                else:
                    self.data_source = "UPSTOX LIVE"
                    self.websocket_status = "Connected"
                    self.last_live_candle_time = None
                    logger.warning("[LIVE] No completed 5m candles yet (insufficient 1m bars).")

                return five_min

            elif response.status_code == 401:
                logger.error("[LIVE] Upstox token rejected (HTTP 401). Re-authenticate via the dashboard.")
                self.access_token = None
                self.websocket_status = "Disconnected"
                self.data_source = "DISCONNECTED"
                self.cmp_source = "DISCONNECTED"
                self.last_live_candle_time = None
                return []
            else:
                logger.error(
                    f"[LIVE] Failed to fetch 1m intraday candles (HTTP {response.status_code}): {response.text}"
                )
                self.websocket_status = "Disconnected"
                self.data_source = "DISCONNECTED"
                self.cmp_source = "DISCONNECTED"
                self.last_live_candle_time = None
                return []

        except Exception as e:
            logger.error(f"[LIVE] Network error fetching intraday candles: {e}")
            self.websocket_status = "Disconnected"
            self.data_source = "DISCONNECTED"
            self.cmp_source = "DISCONNECTED"
            self.last_live_candle_time = None
            return []

    def _aggregate_to_5min(self, one_min_candles: List[Dict]) -> List[Dict]:
        """
        Aggregates 1-minute candles into 5-minute candles.
        Bars are aligned to 09:15, 09:20, 09:25 ... IST (standard NSE grid).
        Only returns fully completed 5m bars (all 5 constituent 1m bars present).
        The in-progress (current) bar is excluded so strategy only sees closed candles.
        """
        from collections import defaultdict
        buckets: dict = defaultdict(list)

        for c in one_min_candles:
            ts_str = c["time"]
            try:
                ts_clean = ts_str[:19]  # "2026-05-30T09:15:00"
                dt = datetime.strptime(ts_clean, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue

            # Which 5-minute slot does this 1m bar belong to?
            total_minutes = dt.hour * 60 + dt.minute
            bucket_minutes = (total_minutes // 5) * 5
            bucket_dt = dt.replace(
                hour=bucket_minutes // 60,
                minute=bucket_minutes % 60,
                second=0
            )
            buckets[bucket_dt].append(c)

        # Build candles — only completed buckets (5 bars)
        five_min = []
        sorted_keys = sorted(buckets.keys())
        for i, bucket_dt in enumerate(sorted_keys):
            bars = buckets[bucket_dt]
            is_last = (i == len(sorted_keys) - 1)
            # Skip the last (possibly incomplete/live) bucket
            if is_last:
                continue
            if len(bars) < 5:
                continue  # incomplete historical bucket — skip

            five_min.append({
                "time":   bucket_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
                "open":   bars[0]["open"],
                "high":   max(b["high"] for b in bars),
                "low":    min(b["low"]  for b in bars),
                "close":  bars[-1]["close"],
                "volume": sum(b["volume"] for b in bars),
            })

        return five_min

    def get_nifty_price(self) -> float:
        """Gets current LTP of Nifty 50 index."""
        token = self.get_token()
        if self._is_mock_mode() or not token:
            logger.warning("Upstox LTP unavailable in disconnected mode. CMP source set to DISCONNECTED.")
            self.data_source = "DISCONNECTED"
            self.cmp_source = "DISCONNECTED"
            self.websocket_status = "Disconnected"
            self.last_cmp_update_time = None
            return None

        url = f"{self.base_url}/market-quote/ltp?instrument_key=NSE_INDEX|Nifty 50"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        try:
            response = requests.get(url, headers=headers, timeout=8)
            if response.status_code == 200:
                data = response.json()
                last_price = data.get("data", {}).get("NSE_INDEX|Nifty 50", {}).get("last_price")
                if last_price is not None:
                    # Update cached LTP and metadata
                    self.last_known_ltp = float(last_price)
                    self.cmp_source = "UPSTOX_LTP"
                    self.last_cmp_update_time = datetime.utcnow().isoformat()
                    self.data_source = "UPSTOX LIVE"
                    self.websocket_status = "Connected"
                    logger.info(f"CMP_SOURCE={self.cmp_source} MARKET_OPEN={self.is_market_open()} DATA_SOURCE={self.data_source}")
                    return float(last_price)
                logger.warning("Upstox LTP response missing last_price. CMP source set to DISCONNECTED.")
        except Exception as e:
            logger.error(f"Error fetching LTP: {e}")
        # On failure to fetch live LTP, keep the previous cached LTP intact
        # so the dashboard can continue to show the last-known CMP.
        self.data_source = "DISCONNECTED"
        self.websocket_status = "Disconnected"
        # Do not clear `last_known_ltp` here; preserve the last value.
        # Only clear `last_cmp_update_time` if no cached LTP exists.
        if not self.last_known_ltp:
            self.cmp_source = "DISCONNECTED"
            self.last_cmp_update_time = None
        else:
            # Indicate that the source is a cached value
            self.cmp_source = "CACHED"
        return None

    def get_previous_day_ohlc(self) -> Optional[Dict]:
        """
        Fetches previous trading day's OHLC for Nifty 50 to calculate today's CPR levels.
        Returns dict with keys: high, low, close — or None on failure.
        """
        token = self.get_token()
        if self._is_mock_mode():
            logger.warning("Mock mode: using hardcoded previous day OHLC for CPR calculation.")
            return None

        today = date.today()
        # Get the last 2 trading days' daily candles
        from_date = (today - timedelta(days=5)).strftime("%Y-%m-%d")
        to_date = today.strftime("%Y-%m-%d")
        instrument_key = "NSE_INDEX|Nifty 50"
        url = f"{self.base_url}/historical-candle/{instrument_key}/day/{to_date}/{from_date}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                candles_raw = data.get("data", {}).get("candles", [])
                # Sorted newest first by Upstox; skip today (index 0), take yesterday (index 1)
                if len(candles_raw) >= 2:
                    prev = candles_raw[1]  # [timestamp, open, high, low, close, vol, oi]
                    logger.info(
                        f"[LIVE] Previous day OHLC fetched for CPR: H={prev[2]}, L={prev[3]}, C={prev[4]}"
                    )
                    return {"high": float(prev[2]), "low": float(prev[3]), "close": float(prev[4])}
                elif len(candles_raw) == 1:
                    prev = candles_raw[0]
                    return {"high": float(prev[2]), "low": float(prev[3]), "close": float(prev[4])}
        except Exception as e:
            logger.error(f"Error fetching previous day OHLC: {e}")
        return None

    def select_atm_option(self, nifty_price: float, trade_type: Literal["BUY", "SELL"]) -> Tuple[str, float, str]:
        """Determines the ATM Strike, Option Contract Symbol, and type (CE/PE)."""
        strike_price = round(nifty_price / 50) * 50
        option_type = "CE" if trade_type == "BUY" else "PE"
        expiry_str = self._get_nearest_weekly_expiry_str()
        option_symbol = f"NIFTY{expiry_str}{int(strike_price)}{option_type}"
        return option_symbol, float(strike_price), option_type

    def place_order(self, option_symbol: str, action: Literal["BUY", "SELL"], lots: int, paper: bool = True) -> Dict:
        """Places a market order. Paper mode returns a mock response."""
        qty = lots * 75

        if paper:
            logger.info(f"[PAPER ORDER] {action} {qty} units of {option_symbol}")
            return {
                "status": "success",
                "order_id": f"PAPER-{int(datetime.utcnow().timestamp())}",
                "avg_price": self._get_mock_option_price(option_symbol),
                "message": "Paper order processed successfully"
            }

        url = f"{self.base_url}/order/place"
        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        payload = {
            "quantity": qty,
            "product": "I",
            "validity": "DAY",
            "price": 0.0,
            "tag": "cpr-bot",
            "instrument_token": f"NSE_FO|{option_symbol}",
            "order_type": "MARKET",
            "transaction_type": action,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False
        }

        try:
            logger.info(f"[LIVE] Placing LIVE Upstox order: {action} {qty} units of {option_symbol}")
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code == 200:
                resp_json = response.json()
                order_id = resp_json.get("data", {}).get("order_id", "LIVE-ORDER-X")
                avg_price = self._get_executed_order_price(order_id)
                return {
                    "status": "success",
                    "order_id": order_id,
                    "avg_price": avg_price if avg_price > 0 else 100.0,
                    "message": "Live order executed."
                }
            else:
                logger.error(f"Upstox live order placement failed: {response.text}")
                return {"status": "error", "message": f"Upstox failure: {response.text}"}
        except Exception as e:
            logger.error(f"Exception during live order placement: {e}")
            return {"status": "error", "message": str(e)}

    def _get_executed_order_price(self, order_id: str) -> float:
        """Fetches the actual execution average price of an order ID."""
        token = self.get_token()
        if not token:
            return 100.0

        url = f"{self.base_url}/order/history?order_id={order_id}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            response = requests.get(url, headers=headers, timeout=8)
            if response.status_code == 200:
                orders = response.json().get("data", [])
                if orders:
                    return float(orders[0].get("average_price", 0.0))
        except Exception as e:
            logger.error(f"Error fetching order price history: {e}")
        return 0.0

    def _get_nearest_weekly_expiry_str(self) -> str:
        today = datetime.today()
        return today.strftime("%y%b%d").upper()

    def _get_mock_option_price(self, option_symbol: str) -> float:
        return 120.50

    def _generate_mock_candles(self) -> List[Dict]:
        """Fallback simulation candles — used ONLY when unauthenticated."""
        import numpy as np
        np.random.seed(42)
        prices = [19450.0]
        for _ in range(30):
            prices.append(prices[-1] + np.random.normal(0, 15))

        candles = []
        base_time = datetime.now()
        for i, p in enumerate(prices):
            o = p
            c = p + np.random.normal(0, 5)
            h = max(o, c) + abs(np.random.normal(0, 4))
            l = min(o, c) - abs(np.random.normal(0, 4))
            candles.append({
                "time": base_time.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c, 2),
                "volume": 12000 + int(np.random.randint(0, 5000))
            })
        return candles
