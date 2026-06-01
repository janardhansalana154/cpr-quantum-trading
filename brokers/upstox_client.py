import requests
import logging
from typing import Dict, List, Optional, Tuple, Literal
from datetime import datetime, date, timedelta, timezone
from urllib.parse import quote
from config.settings import settings

logger = logging.getLogger("CPR_System.Upstox")

# ---------------------------------------------------------------------------
# NSE Market Hours helper (IST = UTC+05:30)
# ---------------------------------------------------------------------------
_IST = timezone(timedelta(hours=5, minutes=30))

# NSE holidays 2025-2026 — add/remove as needed
_NSE_HOLIDAYS = {
    "2025-01-26","2025-02-26","2025-03-14","2025-03-31",
    "2025-04-10","2025-04-14","2025-04-18","2025-05-01",
    "2025-08-15","2025-08-27","2025-10-02","2025-10-20",
    "2025-10-23","2025-11-05","2025-11-14","2025-12-25",
    "2026-01-26","2026-03-19","2026-04-02","2026-04-03",
    "2026-04-14","2026-04-17","2026-05-01","2026-08-15",
    "2026-10-02","2026-10-08","2026-10-09","2026-11-04",
    "2026-12-25",
}

def is_market_open() -> bool:
    now = datetime.now(_IST)
    if now.weekday() >= 5:
        return False
    if now.strftime("%Y-%m-%d") in _NSE_HOLIDAYS:
        return False
    open_t  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t

def get_market_status_detail() -> dict:
    now = datetime.now(_IST)
    return {
        "market_open":   is_market_open(),
        "market_status": "OPEN" if is_market_open() else "CLOSED",
        "current_ist":   now.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
        "weekday":       now.strftime("%A"),
        "is_holiday":    now.strftime("%Y-%m-%d") in _NSE_HOLIDAYS,
    }

# ---------------------------------------------------------------------------
# Token expiry helper
# Upstox tokens expire at MIDNIGHT IST every day — NOT 24h rolling.
# ---------------------------------------------------------------------------
def _token_expires_at_midnight_ist(authenticated_at: datetime) -> datetime:
    """Returns the midnight IST cutoff after the day the token was issued."""
    auth_ist = authenticated_at.replace(tzinfo=timezone.utc).astimezone(_IST)
    midnight_ist = auth_ist.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return midnight_ist.astimezone(timezone.utc).replace(tzinfo=None)  # back to naive UTC

def _is_token_expired(last_auth: datetime) -> bool:
    """True if the Upstox token has crossed its midnight-IST expiry."""
    expiry_utc = _token_expires_at_midnight_ist(last_auth)
    return datetime.utcnow() >= expiry_utc


# ---------------------------------------------------------------------------
# UpstoxClient
# ---------------------------------------------------------------------------
class UpstoxClient:
    def __init__(self):
        import os, json
        self.api_key    = settings.UPSTOX_API_KEY
        self.api_secret = settings.UPSTOX_API_SECRET
        self.redirect_uri = settings.UPSTOX_REDIRECT_URI
        self.base_url   = "https://api.upstox.com/v2"

        # In-memory token cache — cleared on 401 or midnight expiry
        self._access_token: Optional[str] = None
        self._token_loaded_at: Optional[datetime] = None   # when we last loaded/stored it

        # Dashboard status fields
        self.last_live_candle_time: Optional[str] = None
        self.websocket_status: str = "Disconnected"
        self.data_source: str = "DISCONNECTED"

        # Load saved API credentials (written by /api/config)
        secrets_path = getattr(settings, "UPSTOX_SECRETS_PATH", None)
        if secrets_path and os.path.exists(secrets_path):
            try:
                with open(secrets_path) as f:
                    s = json.load(f)
                if s.get("api_key"):
                    self.api_key = s["api_key"]
                    settings.UPSTOX_API_KEY = s["api_key"]
                if s.get("api_secret"):
                    self.api_secret = s["api_secret"]
                    settings.UPSTOX_API_SECRET = s["api_secret"]
                logger.info(f"Loaded Upstox credentials from {secrets_path}")
            except Exception as e:
                logger.error(f"Failed to load credentials: {e}")

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------
    def _clear_token(self):
        """Wipe the in-memory token so the next call re-loads from DB."""
        self._access_token = None
        self._token_loaded_at = None
        self.data_source = "DISCONNECTED"
        self.websocket_status = "Disconnected"

    def get_token(self) -> Optional[str]:
        """
        Returns a valid access token, or None.

        FIX 1: Check midnight-IST expiry, not just 24h rolling window.
        FIX 2: Always re-read from DB when in-memory token is absent (handles server restarts).
        FIX 3: Clear stale cached token when expiry detected.
        """
        # If we have a cached token, verify it hasn't crossed midnight IST
        if self._access_token and self._token_loaded_at:
            if _is_token_expired(self._token_loaded_at):
                logger.warning(
                    "[TOKEN] In-memory token has crossed Upstox midnight-IST expiry. "
                    "Clearing cache — re-authenticate via dashboard."
                )
                self._clear_token()
                return None
            return self._access_token

        # No cached token — try to load from DB
        from database.db import SessionLocal
        if SessionLocal is None:
            return None

        db = SessionLocal()
        try:
            from database.models import UpstoxToken
            tok = db.query(UpstoxToken).order_by(UpstoxToken.id.desc()).first()
            if not tok or not tok.access_token:
                return None

            last_auth = tok.last_authenticated_at or tok.created_at
            if last_auth is None:
                return None

            # FIX 1: Use midnight-IST expiry, not 24h rolling
            if _is_token_expired(last_auth):
                logger.warning(
                    "[TOKEN] DB token has crossed Upstox midnight-IST expiry. "
                    "Re-authenticate via dashboard."
                )
                return None

            # Token is valid — cache it
            self._access_token = tok.access_token
            self._token_loaded_at = last_auth
            logger.info("[TOKEN] Valid token loaded from DB.")
            return self._access_token

        except Exception as e:
            logger.error(f"Error loading token from DB: {e}")
            return None
        finally:
            db.close()

    def _is_authenticated(self) -> bool:
        """True only when token is valid and credentials are real (not mock defaults)."""
        if self.api_key in ("mock_api_key", "mock_key", "", None):
            return False
        return self.get_token() is not None

    # ------------------------------------------------------------------
    # Connection status (for dashboard)
    # ------------------------------------------------------------------
    def get_connection_status(self) -> dict:
        from database.db import SessionLocal
        mkt = get_market_status_detail()
        base = {
            "data_source": self.data_source,
            "last_live_candle_time": self.last_live_candle_time,
            "websocket_status": self.websocket_status,
            **mkt,
        }

        if SessionLocal is None:
            return {"connected": False, "token_status": "DB not ready",
                    "expiry_status": "Database not initialised yet",
                    "last_authenticated": None, "token_preview": "None", **base}

        db = SessionLocal()
        try:
            from database.models import UpstoxToken
            tok = db.query(UpstoxToken).order_by(UpstoxToken.id.desc()).first()
            if not tok or not tok.access_token:
                return {"connected": False, "token_status": "Missing",
                        "expiry_status": "No token — click Connect Upstox",
                        "last_authenticated": None, "token_preview": "None", **base}

            last_auth = tok.last_authenticated_at or tok.created_at
            expired = _is_token_expired(last_auth)
            expiry_utc = _token_expires_at_midnight_ist(last_auth)
            # Convert to IST for display
            expiry_ist = expiry_utc.replace(tzinfo=timezone.utc).astimezone(_IST)

            if expired:
                status = "Expired"
                expiry_desc = f"Expired at {expiry_ist.strftime('%H:%M IST')} — reconnect required"
                connected = False
            else:
                time_left = expiry_utc - datetime.utcnow()
                h = int(time_left.total_seconds() // 3600)
                m = int((time_left.total_seconds() % 3600) // 60)
                status = "Active"
                expiry_desc = f"Active — expires {expiry_ist.strftime('%H:%M IST')} ({h}h {m}m left)"
                connected = True
                base["data_source"] = "UPSTOX LIVE"
                base["websocket_status"] = "Connected"

            preview = tok.access_token[:8] + "..." + tok.access_token[-8:] if len(tok.access_token) > 16 else "Valid"
            return {
                "connected": connected,
                "token_status": status,
                "expiry_status": expiry_desc,
                "expires_at": expiry_utc.isoformat(),
                "last_authenticated": last_auth.isoformat(),
                "token_preview": preview,
                **base,
            }
        except Exception as e:
            logger.error(f"get_connection_status error: {e}")
            return {"connected": False, "token_status": "Error",
                    "expiry_status": str(e), "last_authenticated": None,
                    "token_preview": "Error", **base}
        finally:
            db.close()

    def ensure_authenticated(self) -> bool:
        """Load a valid token from DB and mark Upstox as live if available."""
        token = self.get_token()
        if token is None:
            self.data_source = "DISCONNECTED"
            self.websocket_status = "Disconnected"
            return False

        self.data_source = "UPSTOX LIVE"
        self.websocket_status = "Connected"
        return True

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------
    def get_login_url(self, override_redirect_uri: Optional[str] = None) -> str:
        r_uri = override_redirect_uri or self.redirect_uri
        logger.info(f"OAuth login URL: client_id={self.api_key} redirect={r_uri}")
        return (f"https://api.upstox.com/v2/login/authorization/dialog"
                f"?response_type=code&client_id={self.api_key}&redirect_uri={r_uri}")

    def authenticate(self, auth_code: str, redirect_uri: Optional[str] = None) -> bool:
        """Exchange auth code for access token and persist to DB."""
        url = f"{self.base_url}/login/authorization/token"
        r_uri = redirect_uri or self.redirect_uri
        data = {
            "code": auth_code,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
            "redirect_uri": r_uri,
            "grant_type": "authorization_code",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}

        try:
            resp = requests.post(url, data=data, headers=headers, timeout=10)
            if resp.status_code == 200:
                token = resp.json().get("access_token")
                if not token:
                    logger.error("OAuth response missing access_token")
                    return False

                # Persist to DB
                from database.db import SessionLocal
                from database.models import UpstoxToken
                if SessionLocal:
                    db = SessionLocal()
                    try:
                        db.query(UpstoxToken).delete()
                        now = datetime.utcnow()
                        expiry_at = _token_expires_at_midnight_ist(now)
                        db.add(UpstoxToken(
                            access_token=token,
                            status="Connected",
                            expiry_time=expiry_at,
                            last_authenticated_at=now,
                        ))
                        db.commit()
                        logger.info("[TOKEN] Token persisted to DB.")
                    except Exception as e:
                        logger.error(f"DB token write error: {e}")
                    finally:
                        db.close()

                # Cache in memory
                self._access_token = token
                self._token_loaded_at = datetime.utcnow()
                self.data_source = "UPSTOX LIVE"
                self.websocket_status = "Connected"
                logger.info("[LIVE] Upstox authenticated successfully.")
                return True
            else:
                logger.error(f"OAuth failed (HTTP {resp.status_code}): {resp.text}")
                return False
        except Exception as e:
            logger.error(f"OAuth network error: {e}")
            return False

    # ------------------------------------------------------------------
    # Live data methods
    # ------------------------------------------------------------------
    def get_nifty_ohlc_5m(self) -> List[Dict]:
        """
        Fetch today's completed 5-minute NIFTY candles from Upstox.

        FIX: URL-encode the instrument key so '|' and ' ' are safe in the path.
        FIX: Clear token on 401 so subsequent calls force re-auth.
        RULE 3: Return [] on any failure — no mock/simulation fallback ever.

        Endpoint: GET /v2/historical-candle/intraday/{instrument_key}/1minute
        Upstox candle format: [timestamp, open, high, low, close, volume, oi]
        Newest candle first — reversed to chronological order before aggregation.
        """
        if not self._is_authenticated():
            logger.warning("[DATA_SOURCE=DISCONNECTED] Not authenticated — no candles fetched.")
            self.data_source = "DISCONNECTED"
            self.websocket_status = "Disconnected"
            return []

        token = self.get_token()
        # FIX: URL-encode the instrument key (| and space must be encoded in URL path)
        instrument_key = quote("NSE_INDEX|Nifty 50", safe="")
        url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/1minute"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            resp = requests.get(url, headers=headers, timeout=10)

            if resp.status_code == 200:
                raw = resp.json().get("data", {}).get("candles", [])
                if not raw:
                    logger.warning("[LIVE] Upstox returned 0 1m candles (market may be pre-open).")
                    self.data_source = "UPSTOX LIVE"
                    self.websocket_status = "Connected"
                    return []

                one_min = [
                    {"time": c[0], "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4]), "volume": int(c[5])}
                    for c in raw
                ]
                one_min.reverse()   # Upstox returns newest first → make chronological

                five_min = self._aggregate_to_5min(one_min)
                self.data_source = "UPSTOX LIVE"
                self.websocket_status = "Connected"
                if five_min:
                    self.last_live_candle_time = five_min[-1]["time"]
                    logger.info(
                        f"[LIVE] {len(five_min)} 5m candles from {len(one_min)} 1m bars. "
                        f"Latest: {five_min[-1]['close']} @ {five_min[-1]['time']}"
                    )
                return five_min

            elif resp.status_code == 401:
                logger.error("[TOKEN] Upstox rejected token (401). Clearing cache — re-authenticate.")
                self._clear_token()
                self._invalidate_db_token()
                return []

            else:
                logger.error(f"[LIVE] Candle fetch failed (HTTP {resp.status_code}): {resp.text}")
                self.data_source = "DISCONNECTED"
                return []

        except Exception as e:
            logger.error(f"[LIVE] Network error fetching candles: {e}")
            self.data_source = "DISCONNECTED"
            return []

    def _aggregate_to_5min(self, one_min_candles: List[Dict]) -> List[Dict]:
        """
        Aggregate 1-minute bars into 5-minute bars aligned to 09:15 NSE grid.
        Only returns fully completed bars (skips the last/in-progress bar).
        """
        from collections import defaultdict
        buckets: dict = defaultdict(list)

        for c in one_min_candles:
            ts = c["time"][:19]
            try:
                dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
            total_m = dt.hour * 60 + dt.minute
            slot_m = (total_m // 5) * 5
            bucket = dt.replace(hour=slot_m // 60, minute=slot_m % 60, second=0)
            buckets[bucket].append(c)

        five_min = []
        keys = sorted(buckets)
        for i, bucket in enumerate(keys):
            bars = buckets[bucket]
            if i == len(keys) - 1:  # skip last (possibly incomplete) bar
                continue
            if len(bars) < 5:       # incomplete historical bucket
                continue
            five_min.append({
                "time":   bucket.strftime("%Y-%m-%dT%H:%M:%S+05:30"),
                "open":   bars[0]["open"],
                "high":   max(b["high"] for b in bars),
                "low":    min(b["low"]  for b in bars),
                "close":  bars[-1]["close"],
                "volume": sum(b["volume"] for b in bars),
            })
        return five_min

    def _fetch_intraday_1m_for_date(self, trading_date: date, token: str) -> List[Dict]:
        instrument_key = quote("NSE_INDEX|Nifty 50", safe="")
        url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/1minute"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        params = {
            "from": trading_date.strftime("%Y-%m-%d 09:15:00"),
            "to": trading_date.strftime("%Y-%m-%d 15:30:00"),
        }

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                raw = resp.json().get("data", {}).get("candles", [])
                if not raw:
                    logger.warning(f"[HISTORICAL] No 1m candles returned for {trading_date}.")
                    return []
                one_min = [
                    {"time": c[0], "open": float(c[1]), "high": float(c[2]),
                     "low": float(c[3]), "close": float(c[4]), "volume": int(c[5])}
                    for c in raw
                ]
                one_min.reverse()
                return one_min
            elif resp.status_code == 401:
                logger.error("[TOKEN] Historical candle fetch rejected (401). Clearing cache.")
                self._clear_token()
                self._invalidate_db_token()
                return []
            else:
                logger.error(f"[HISTORICAL] Candle fetch failed (HTTP {resp.status_code}): {resp.text}")
                return []
        except Exception as e:
            logger.error(f"[HISTORICAL] Network error fetching candles for {trading_date}: {e}")
            return []

    def get_nifty_historical_5m_for_day(self, trading_date: date) -> List[Dict]:
        if not self._is_authenticated():
            logger.warning("[DATA_SOURCE=DISCONNECTED] Not authenticated — historical 5m candles skipped.")
            return []

        token = self.get_token()
        if token is None:
            return []

        one_min = self._fetch_intraday_1m_for_date(trading_date, token)
        return self._aggregate_to_5min(one_min)

    def get_previous_day_ohlc_for_date(self, target_date: date) -> Optional[Dict]:
        if not self._is_authenticated():
            logger.warning("[DATA_SOURCE=DISCONNECTED] Not authenticated — prev OHLC skipped.")
            return None

        token = self.get_token()
        if token is None:
            return None

        from_d = (target_date - timedelta(days=7)).strftime("%Y-%m-%d")
        to_d = target_date.strftime("%Y-%m-%d")
        instrument_key = quote("NSE_INDEX|Nifty 50", safe="")
        url = f"{self.base_url}/historical-candle/{instrument_key}/day/{to_d}/{from_d}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                raw = resp.json().get("data", {}).get("candles", [])
                previous = None
                for candle in raw:
                    if not isinstance(candle, list) or len(candle) < 5:
                        continue
                    candle_date = str(candle[0])[:10]
                    if candle_date >= target_date.strftime("%Y-%m-%d"):
                        continue
                    previous = candle
                    break
                if previous:
                    h, l, c = float(previous[2]), float(previous[3]), float(previous[4])
                    logger.info(f"[HISTORICAL] Prev day OHLC for {target_date}: H={h} L={l} C={c}")
                    return {"high": h, "low": l, "close": c}
                logger.warning(f"[HISTORICAL] No prior trading day OHLC returned for {target_date}.")
                return None
            elif resp.status_code == 401:
                logger.error("[TOKEN] Prev OHLC call rejected (401). Clearing token.")
                self._clear_token()
                self._invalidate_db_token()
                return None
            else:
                logger.error(f"[HISTORICAL] Prev OHLC failed (HTTP {resp.status_code}): {resp.text}")
                return None
        except Exception as e:
            logger.error(f"[HISTORICAL] Error fetching prev OHLC for {target_date}: {e}")
            return None

    def get_nifty_price(self) -> Optional[float]:
        """
        Fetch current Nifty 50 LTP from Upstox.

        FIX: Use params={} so requests URL-encodes the instrument key automatically.
        FIX: Clear token on 401.
        RULE 3: Return None if unauthenticated or API error — no fallback value.

        Endpoint: GET /v2/market-quote/ltp
        Response: {"data": {"NSE_INDEX:Nifty 50": {"last_price": 24105.35}}}
        Note: Upstox returns ':' not '|' in the response key.
        """
        if not self._is_authenticated():
            logger.debug("[CMP_SOURCE=DISCONNECTED] Not authenticated — LTP skipped.")
            return None

        token = self.get_token()
        url = f"{self.base_url}/market-quote/ltp"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        # FIX: Pass as params dict — requests handles URL-encoding automatically
        params = {"instrument_key": "NSE_INDEX|Nifty 50"}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=8)
            if resp.status_code == 200:
                d = resp.json().get("data", {})
                # Upstox returns the key with ':' in the response body
                ltp_data = (d.get("NSE_INDEX:Nifty 50")
                            or d.get("NSE_INDEX|Nifty 50")
                            or next(iter(d.values()), {}))
                ltp = ltp_data.get("last_price")
                if ltp is not None:
                    logger.info(f"[CMP_SOURCE=UPSTOX_LTP] Nifty LTP: {ltp}")
                    return float(ltp)
                logger.warning(f"[LIVE] LTP key not found in response: {d}")
                return None

            elif resp.status_code == 401:
                logger.error("[TOKEN] LTP call rejected (401). Clearing token cache.")
                self._clear_token()
                self._invalidate_db_token()
                return None
            else:
                logger.error(f"[LIVE] LTP fetch failed (HTTP {resp.status_code}): {resp.text}")
                return None

        except Exception as e:
            logger.error(f"Error fetching LTP: {e}")
            return None

    def get_previous_day_ohlc(self) -> Optional[Dict]:
        """
        Fetch previous trading day OHLC for CPR calculation.
        FIX: URL-encode instrument key in path.
        RULE 3: Return None if unauthenticated — no hardcoded fallback.

        Endpoint: GET /v2/historical-candle/{instrument_key}/day/{to}/{from}
        Candle format: [timestamp, open, high, low, close, volume, oi]
        Newest first.
        """
        if not self._is_authenticated():
            logger.warning("[DATA_SOURCE=DISCONNECTED] Not authenticated — prev OHLC skipped.")
            return None

        token = self.get_token()
        today = date.today()
        from_d = (today - timedelta(days=7)).strftime("%Y-%m-%d")  # wider window for holidays
        to_d   = today.strftime("%Y-%m-%d")
        instrument_key = quote("NSE_INDEX|Nifty 50", safe="")
        url = f"{self.base_url}/historical-candle/{instrument_key}/day/{to_d}/{from_d}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                raw = resp.json().get("data", {}).get("candles", [])
                # raw[0] = today (or latest), raw[1] = previous session
                prev = None
                if len(raw) >= 2:
                    prev = raw[1]
                elif len(raw) == 1:
                    prev = raw[0]

                if prev:
                    h, l, c = float(prev[2]), float(prev[3]), float(prev[4])
                    logger.info(f"[LIVE] Prev day OHLC: H={h} L={l} C={c}")
                    return {"high": h, "low": l, "close": c}

                logger.warning("[LIVE] No daily candles returned for prev OHLC.")
                return None

            elif resp.status_code == 401:
                logger.error("[TOKEN] Prev OHLC call rejected (401). Clearing token.")
                self._clear_token()
                self._invalidate_db_token()
                return None
            else:
                logger.error(f"[LIVE] Prev OHLC failed (HTTP {resp.status_code}): {resp.text}")
                return None

        except Exception as e:
            logger.error(f"Error fetching prev OHLC: {e}")
            return None

    def _invalidate_db_token(self):
        """Mark the DB token as expired so the UI shows correct state."""
        try:
            from database.db import SessionLocal
            from database.models import UpstoxToken
            if SessionLocal is None:
                return
            db = SessionLocal()
            try:
                tok = db.query(UpstoxToken).order_by(UpstoxToken.id.desc()).first()
                if tok:
                    tok.status = "Expired"
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error invalidating DB token: {e}")

    def select_atm_option(self, nifty_price: float, trade_type: Literal["BUY", "SELL"]) -> Tuple[str, float, str]:
        strike = round(nifty_price / 50) * 50
        opt_type = "CE" if trade_type == "BUY" else "PE"
        expiry = self._get_nearest_weekly_expiry_str()
        return f"NIFTY{expiry}{int(strike)}{opt_type}", float(strike), opt_type

    def place_order(self, option_symbol: str, action: Literal["BUY", "SELL"], lots: int, paper: bool = True) -> Dict:
        qty = lots * 75
        if paper:
            logger.info(f"[PAPER ORDER] {action} {qty} units of {option_symbol}")
            return {
                "status": "success",
                "order_id": f"PAPER-{int(datetime.utcnow().timestamp())}",
                "avg_price": 120.50,
                "message": "Paper order processed",
            }

        token = self.get_token()
        if not token:
            return {"status": "error", "message": "No valid auth token for live order"}

        url = f"{self.base_url}/order/place"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
        payload = {
            "quantity": qty, "product": "I", "validity": "DAY",
            "price": 0.0, "tag": "cpr-bot",
            "instrument_token": f"NSE_FO|{option_symbol}",
            "order_type": "MARKET", "transaction_type": action,
            "disclosed_quantity": 0, "trigger_price": 0.0, "is_amo": False,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code == 200:
                order_id = resp.json().get("data", {}).get("order_id", "LIVE-X")
                avg = self._get_executed_order_price(order_id)
                return {"status": "success", "order_id": order_id,
                        "avg_price": avg if avg > 0 else 100.0, "message": "Live order executed."}
            elif resp.status_code == 401:
                self._clear_token()
                self._invalidate_db_token()
                return {"status": "error", "message": "Token expired during order placement"}
            else:
                logger.error(f"Live order failed: {resp.text}")
                return {"status": "error", "message": resp.text}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _get_executed_order_price(self, order_id: str) -> float:
        token = self.get_token()
        if not token:
            return 100.0
        try:
            resp = requests.get(
                f"{self.base_url}/order/history?order_id={order_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=8,
            )
            if resp.status_code == 200:
                orders = resp.json().get("data", [])
                if orders:
                    return float(orders[0].get("average_price", 0.0))
        except Exception as e:
            logger.error(f"Error fetching order price: {e}")
        return 0.0

    def _get_nearest_weekly_expiry_str(self) -> str:
        return datetime.today().strftime("%y%b%d").upper()
