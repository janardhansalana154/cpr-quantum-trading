import requests
import logging
from typing import Dict, List, Optional, Tuple, Literal
from datetime import datetime, date, time, timedelta, timezone
from urllib.parse import quote
from config.settings import settings

logger = logging.getLogger("CPR_System.Upstox")

# ---------------------------------------------------------------------------
# NSE Market Hours helper (IST = UTC+05:30)
# ---------------------------------------------------------------------------
_IST = timezone(timedelta(hours=5, minutes=30))

# NSE holidays 2025-2026
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
# Token expiry helpers
# FIX: unified naming — was _token_expires_at_ist in one place,
#      _token_expires_at_midnight_ist in another → caused NameError crash
# ---------------------------------------------------------------------------
def _token_expires_at_midnight_ist(authenticated_at: datetime) -> datetime:
    """
    Returns the token expiry time in UTC.
    Upstox tokens expire around 3:00 AM IST the following day (safe conservative cutoff).
    """
    auth_ist = authenticated_at.replace(tzinfo=timezone.utc).astimezone(_IST)
    expiry_ist = auth_ist.replace(hour=3, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return expiry_ist.astimezone(timezone.utc).replace(tzinfo=None)

# Keep old name as alias so any other code that references it still works
_token_expires_at_ist = _token_expires_at_midnight_ist

def _is_token_expired(last_auth: datetime) -> bool:
    """True if the token has crossed its 3 AM IST expiry."""
    expiry_utc = _token_expires_at_midnight_ist(last_auth)
    return datetime.utcnow() >= expiry_utc


# ---------------------------------------------------------------------------
# TOTP auto-token generation
# Set UPSTOX_TOTP_SECRET in your .env to enable fully automatic daily login.
# ---------------------------------------------------------------------------
def _try_totp_auto_login(upstox_client: "UpstoxClient") -> bool:
    """
    Attempt to generate a new Upstox token automatically using TOTP.
    Requires:
      - UPSTOX_TOTP_SECRET in .env (base32 secret from Upstox TOTP setup)
      - UPSTOX_MOBILE in .env (your Upstox registered mobile number)
      - UPSTOX_PIN in .env (your 6-digit Upstox PIN)
      - pip install upstox-totp
    Returns True if a new token was obtained and saved.
    """
    import os
    totp_secret = os.getenv("UPSTOX_TOTP_SECRET", "").strip()
    mobile      = os.getenv("UPSTOX_MOBILE", "").strip()
    pin         = os.getenv("UPSTOX_PIN", "").strip()

    if not totp_secret or not mobile or not pin:
        logger.info("[TOTP] UPSTOX_TOTP_SECRET / UPSTOX_MOBILE / UPSTOX_PIN not set — skipping auto-login.")
        return False

    try:
        from upstox_totp import UpstoxTOTP
        logger.info("[TOTP] Attempting automatic daily token generation via TOTP...")
        upx = UpstoxTOTP(
            mobile=mobile,
            pin=pin,
            totp_secret=totp_secret,
            api_key=upstox_client.api_key,
            api_secret=upstox_client.api_secret,
            redirect_uri=upstox_client.redirect_uri,
        )
        result = upx.get_access_token()
        token = result.access_token if hasattr(result, "access_token") else result.get("access_token")
        if not token:
            logger.error("[TOTP] TOTP login returned no access_token.")
            return False

        # Persist to DB
        from database.db import SessionLocal
        from database.models import UpstoxToken
        if SessionLocal:
            db = SessionLocal()
            try:
                db.query(UpstoxToken).delete()
                now = datetime.utcnow()
                db.add(UpstoxToken(
                    access_token=token,
                    status="Connected",
                    expiry_time=_token_expires_at_midnight_ist(now),
                    last_authenticated_at=now,
                ))
                db.commit()
                logger.info("[TOTP] ✅ New token saved to DB successfully.")
            except Exception as e:
                logger.error(f"[TOTP] DB write error: {e}")
                return False
            finally:
                db.close()

        # Cache in memory
        upstox_client._access_token = token
        upstox_client._token_loaded_at = datetime.utcnow()
        upstox_client.data_source = "UPSTOX LIVE"
        upstox_client.websocket_status = "Connected"
        logger.info("[TOTP] ✅ Auto-login successful. Token active.")
        return True

    except ImportError:
        logger.warning("[TOTP] upstox-totp package not installed. Run: pip install upstox-totp")
        return False
    except Exception as e:
        logger.error(f"[TOTP] Auto-login failed: {e}")
        return False


# ---------------------------------------------------------------------------
# UpstoxClient
# ---------------------------------------------------------------------------
class UpstoxClient:
    def __init__(self):
        import os, json
        self.api_key      = settings.UPSTOX_API_KEY
        self.api_secret   = settings.UPSTOX_API_SECRET
        self.redirect_uri = settings.UPSTOX_REDIRECT_URI
        self.base_url     = "https://api.upstox.com/v2"

        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_loaded_at: Optional[datetime] = None   # when we last loaded/stored it
        self._token_expires_at: Optional[datetime] = None

        self.last_live_candle_time: Optional[str] = None
        self.websocket_status: str = "Disconnected"
        self.data_source: str = "DISCONNECTED"

        # Load saved API credentials
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
                if s.get("refresh_token"):
                    self._refresh_token = s["refresh_token"]
                logger.info(f"Loaded Upstox credentials from {secrets_path}")
            except Exception as e:
                logger.error(f"Failed to load credentials: {e}")

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------
    def _clear_token(self):
        self._access_token = None
        self._refresh_token = None
        self._token_loaded_at = None
        self._token_expires_at = None
        self.data_source = "DISCONNECTED"
        self.websocket_status = "Disconnected"

    def _expiry_from_token_row(self, tok) -> Optional[datetime]:
        if getattr(tok, "expiry_time", None):
            return tok.expiry_time
        last_auth = tok.last_authenticated_at or tok.created_at
        return _token_expires_at_midnight_ist(last_auth) if last_auth else None

    def _is_cached_token_valid(self) -> bool:
        if not self._access_token:
            return False
        if self._token_expires_at and datetime.utcnow() >= self._token_expires_at:
            return False
        if self._token_loaded_at and _is_token_expired(self._token_loaded_at):
            return False
        return True
    
    def _is_token_expiring_soon(self, buffer_hours: int = 2) -> bool:
        """Check if token will expire within buffer_hours."""
        if not self._token_expires_at:
            return False
        time_until_expiry = self._token_expires_at - datetime.utcnow()
        return time_until_expiry.total_seconds() < (buffer_hours * 3600)
    
    @staticmethod
    def _token_expiring_soon_static(expiry_at: datetime, buffer_hours: int = 2) -> bool:
        """Static version: check if expiry_at is within buffer_hours from now."""
        if not expiry_at:
            return False
        time_until_expiry = expiry_at - datetime.utcnow()
        return time_until_expiry.total_seconds() < (buffer_hours * 3600)

    def _refresh_access_token(self) -> Optional[str]:
        if not self._refresh_token:
            return None

        url = f"{self.base_url}/login/authorization/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": self.api_key,
            "client_secret": self.api_secret,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}

        try:
            resp = requests.post(url, data=data, headers=headers, timeout=10)
            if resp.status_code != 200:
                logger.warning(f"[TOKEN] Refresh failed (HTTP {resp.status_code}): {resp.text}")
                return None

            payload = resp.json()
            token = payload.get("access_token")
            if not token:
                logger.error("[TOKEN] Refresh response missing access_token")
                return None

            refresh = payload.get("refresh_token")
            expires_in = payload.get("expires_in")
            now = datetime.utcnow()
            expiry_at = now + timedelta(seconds=int(expires_in)) if expires_in else _token_expires_at_midnight_ist(now)

            self._access_token = token
            self._refresh_token = refresh or self._refresh_token
            self._token_loaded_at = now
            self._token_expires_at = expiry_at

            from database.db import SessionLocal
            from database.models import UpstoxToken
            if SessionLocal:
                db = SessionLocal()
                try:
                    tok = db.query(UpstoxToken).order_by(UpstoxToken.id.desc()).first()
                    if tok:
                        tok.access_token = token
                        tok.refresh_token = self._refresh_token
                        tok.expiry_time = expiry_at
                        tok.last_authenticated_at = now
                        tok.status = "Connected"
                        db.commit()
                except Exception as e:
                    logger.error(f"[TOKEN] Failed to persist refreshed token: {e}")
                finally:
                    db.close()

            if self._refresh_token and getattr(settings, "UPSTOX_SECRETS_PATH", None):
                try:
                    import json
                    with open(settings.UPSTOX_SECRETS_PATH, "r+") as f:
                        data = json.load(f)
                        data["refresh_token"] = self._refresh_token
                        f.seek(0)
                        f.truncate()
                        json.dump(data, f)
                except Exception:
                    pass

            logger.info("[TOKEN] Access token refreshed successfully.")
            return token
        except Exception as e:
            logger.error(f"[TOKEN] Refresh network error: {e}")
            return None

    def get_token(self) -> Optional[str]:
        """
        Returns a valid access token, or None.

        FIX 1: Check token expiry using stored expiry time or midnight IST rules.
        FIX 2: Always re-read from DB when in-memory token is absent (handles server restarts).
        FIX 3: Attempt refresh when refresh token is available.
        FIX 4: Proactively refresh if token expiring within 2 hours to avoid gap.
        """
        if self._access_token:
            if self._is_token_expiring_soon(buffer_hours=2):
                logger.warning(
                    "[TOKEN] Token expiring within 2 hours. "
                    "Attempting proactive refresh."
                )
                refreshed = self._refresh_access_token()
                if refreshed:
                    return refreshed
                self._clear_token()
                return None
            
            if not self._is_cached_token_valid():
                logger.warning(
                    "[TOKEN] In-memory token is expired or invalid. "
                    "Attempting refresh if available."
                )
                refreshed = self._refresh_access_token()
                if refreshed:
                    return refreshed
                self._clear_token()
                return None
            return self._access_token

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

            expiry_at = self._expiry_from_token_row(tok)
            
            if expiry_at and self._token_expiring_soon_static(expiry_at, buffer_hours=2):
                logger.warning(
                    "[TOKEN] DB token expiring within 2 hours. "
                    "Attempting proactive refresh."
                )
                refreshed = self._refresh_access_token()
                if refreshed:
                    return refreshed
                return None
            
            if expiry_at and datetime.utcnow() >= expiry_at:
                logger.warning(
                    "[TOKEN] DB token has expired. "
                    "Attempting refresh if refresh token is available."
                )
                refreshed = self._refresh_access_token()
                if refreshed:
                    return refreshed
                return None

            self._access_token = tok.access_token
            self._refresh_token = tok.refresh_token or self._refresh_token
            self._token_loaded_at = last_auth
            self._token_expires_at = expiry_at
            self.data_source = "UPSTOX LIVE"
            self.websocket_status = "Connected"
            logger.info("[TOKEN] Valid token loaded from DB.")
            return self._access_token

        except Exception as e:
            logger.error(f"Error loading token from DB: {e}")
            return None
        finally:
            db.close()

    def _is_authenticated(self) -> bool:
        if self.api_key in ("mock_api_key", "mock_key", "", None):
            return False
        return self.get_token() is not None

    # ------------------------------------------------------------------
    # Daily auto-reconnect (called by scheduler at 8:55 AM IST)
    # ------------------------------------------------------------------
    def daily_auto_reconnect(self):
        """
        Called every morning at 8:55 AM IST by the scheduler.
        Clears the expired token and attempts TOTP auto-login.
        Falls back gracefully — manual OAuth login still works if TOTP not configured.
        """
        logger.info("[AUTO-RECONNECT] Daily token refresh starting...")
        self._clear_token()

        if _try_totp_auto_login(self):
            logger.info("[AUTO-RECONNECT] ✅ TOTP auto-login successful.")
            try:
                from telegram.bot import notify_signal_detected
                notify_signal_detected("SYSTEM", "✅ Upstox auto-reconnected via TOTP. System ready.")
            except Exception:
                pass
        else:
            logger.warning(
                "[AUTO-RECONNECT] TOTP not configured or failed. "
                "Please log in manually via the Dashboard → Upstox tab."
            )
            try:
                from telegram.bot import notify_system_error
                notify_system_error(
                    "⚠️ Upstox token expired. TOTP auto-login not available.\n"
                    "Please log in via Dashboard → Upstox tab before 9:15 AM."
                )
            except Exception:
                pass

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
            expiry_utc = tok.expiry_time or _token_expires_at_midnight_ist(last_auth)
            expired = datetime.utcnow() >= expiry_utc
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
        token = self.get_token()
        if token is None:
            self.data_source = "DISCONNECTED"
            self.websocket_status = "Disconnected"
            return False

        now_ist = datetime.now(_IST)
        market_open  = now_ist.replace(hour=9,  minute=0,  second=0, microsecond=0)
        market_close = now_ist.replace(hour=15, minute=45, second=0, microsecond=0)
        is_market_hours = market_open <= now_ist <= market_close and now_ist.weekday() < 5

        if is_market_hours:
            url = f"{self.base_url}/market-quote/ltp"
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            params  = {"instrument_key": "NSE_INDEX|Nifty 50"}
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=5)
                if resp.status_code == 401:
                    logger.warning("[KEEPALIVE] Token rejected (401) during market hours. Clearing.")
                    self._clear_token()
                    self._invalidate_db_token()
                    self.data_source = "DISCONNECTED"
                    self.websocket_status = "Disconnected — re-authenticate"
                    return False
                logger.debug(f"[KEEPALIVE] LTP validation OK (status {resp.status_code})")
            except Exception as e:
                logger.warning(f"[KEEPALIVE] LTP validation network error: {e} — keeping token")

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
                payload = resp.json()
                token = payload.get("access_token")
                if not token:
                    logger.error("OAuth response missing access_token")
                    return False

                refresh_token = payload.get("refresh_token")
                expires_in = payload.get("expires_in")
                now = datetime.utcnow()
                expiry_at = (now + timedelta(seconds=int(expires_in))) if expires_in else _token_expires_at_midnight_ist(now)

                # Persist to DB
                from database.db import SessionLocal
                from database.models import UpstoxToken
                if SessionLocal:
                    db = SessionLocal()
                    try:
                        db.query(UpstoxToken).delete()
                        db.add(UpstoxToken(
                            access_token=token,
                            refresh_token=refresh_token,
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

                self._access_token = token
                self._refresh_token = refresh_token
                self._token_loaded_at = now
                self._token_expires_at = expiry_at
                self.data_source = "UPSTOX LIVE"
                self.websocket_status = "Connected"

                if self._refresh_token and getattr(settings, "UPSTOX_SECRETS_PATH", None):
                    try:
                        import json
                        with open(settings.UPSTOX_SECRETS_PATH, "r+") as f:
                            data = json.load(f)
                            data["refresh_token"] = self._refresh_token
                            f.seek(0)
                            f.truncate()
                            json.dump(data, f)
                    except Exception:
                        pass

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
        if settings.MOCK_MODE:
            self.data_source = "SIMULATION"
            self.websocket_status = "Connected"
            return self.get_mock_nifty_ohlc_5m()

        if not self._is_authenticated():
            logger.warning("[DATA_SOURCE=DISCONNECTED] Not authenticated — no candles fetched.")
            self.data_source = "DISCONNECTED"
            self.websocket_status = "Disconnected"
            return []

        token = self.get_token()
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
                one_min.reverse()

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
                self.data_source = "DISCONNECTED"
                self.websocket_status = "Disconnected — reconnect via dashboard"
                return []

            else:
                logger.error(f"[LIVE] Candle fetch failed (HTTP {resp.status_code}): {resp.text}")
                self.data_source = "DISCONNECTED"
                return []

        except Exception as e:
            logger.error(f"[LIVE] Network error fetching candles: {e}")
            self.data_source = "DISCONNECTED"
            return []

    def get_mock_nifty_ohlc_5m(self) -> List[Dict]:
        # A deterministic mock candle sequence designed to trigger a valid Setup A entry
        # on the synthetic CPR levels defined by get_mock_previous_day_ohlc().
        demo_bars = [
            {"time": "2026-06-04T09:15:00+05:30", "open": 24080.0, "high": 24090.0, "low": 24078.0, "close": 24088.0, "volume": 120},
            {"time": "2026-06-04T09:20:00+05:30", "open": 24088.0, "high": 24089.0, "low": 24077.0, "close": 24084.0, "volume": 110},
            {"time": "2026-06-04T09:25:00+05:30", "open": 24084.0, "high": 24087.0, "low": 24080.0, "close": 24085.0, "volume": 100},
            {"time": "2026-06-04T09:30:00+05:30", "open": 24085.0, "high": 24086.0, "low": 24079.0, "close": 24083.0, "volume": 105},
        ]
        return [dict(bar) for bar in demo_bars]

    def get_mock_previous_day_ohlc(self) -> Optional[Dict]:
        return {"high": 24095.0, "low": 24018.0, "close": 24044.0}

    def get_mock_nifty_price(self) -> float:
        return 24088.0

    def get_mock_option_ltp(self, option_symbol: str) -> float:
        strike = 120.0
        try:
            import re
            match = re.search(r"(\d+)(?:CE|PE)$", option_symbol)
            if match:
                strike = float(match.group(1))
        except Exception:
            pass
        return round(110.0 + ((strike - 24000.0) / 500.0) * 2.0, 2)

    def _aggregate_to_5min(self, one_min_candles: List[Dict]) -> List[Dict]:
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
            if i == len(keys) - 1:
                continue
            if len(bars) < 5:
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
        today = datetime.now(_IST).date()

        if trading_date >= today:
            url = f"{self.base_url}/historical-candle/intraday/{instrument_key}/1minute"
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                resp = requests.get(url, headers=headers, timeout=15)
            except Exception as e:
                logger.error(f"[HISTORICAL] Network error (intraday) for {trading_date}: {e}")
                return []
        else:
            date_str = trading_date.strftime("%Y-%m-%d")
            url = f"{self.base_url}/historical-candle/{instrument_key}/1minute/{date_str}/{date_str}"
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            logger.info(f"[HISTORICAL] Fetching 1m candles for {trading_date} via: {url}")
            try:
                resp = requests.get(url, headers=headers, timeout=15)
            except Exception as e:
                logger.error(f"[HISTORICAL] Network error (historical) for {trading_date}: {e}")
                return []

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
            logger.info(f"[HISTORICAL] Got {len(one_min)} 1m candles for {trading_date}")
            return one_min
        elif resp.status_code == 401:
            now_ist = datetime.now(_IST)
            is_mkt = (now_ist.replace(hour=9,minute=0,second=0,microsecond=0)
                      <= now_ist <=
                      now_ist.replace(hour=15,minute=45,second=0,microsecond=0)
                      and now_ist.weekday() < 5)
            if is_mkt:
                logger.error("[TOKEN] Historical candle 401 during market hours — clearing token.")
                self._clear_token()
                self._invalidate_db_token()
            else:
                logger.warning("[TOKEN] Historical candle 401 outside market hours — keeping token.")
            return []
        elif resp.status_code == 429:
            import time
            logger.warning(f"[HISTORICAL] Rate limited (429) for {trading_date}. Sleeping 2s.")
            time.sleep(2)
            return []
        else:
            logger.error(f"[HISTORICAL] Candle fetch failed (HTTP {resp.status_code}) for {trading_date}: {resp.text[:200]}")
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
                now_ist = datetime.now(_IST)
                is_mkt = (now_ist.replace(hour=9,minute=0,second=0,microsecond=0)
                          <= now_ist <=
                          now_ist.replace(hour=15,minute=45,second=0,microsecond=0)
                          and now_ist.weekday() < 5)
                if is_mkt:
                    logger.error("[TOKEN] Prev OHLC 401 during market hours — clearing token.")
                    self._clear_token()
                    self._invalidate_db_token()
                else:
                    logger.warning("[TOKEN] Prev OHLC 401 outside market hours — keeping token.")
                return None
            else:
                logger.error(f"[HISTORICAL] Prev OHLC failed (HTTP {resp.status_code}): {resp.text}")
                return None
        except Exception as e:
            logger.error(f"[HISTORICAL] Error fetching prev OHLC for {target_date}: {e}")
            return None

    def get_nifty_price(self) -> Optional[float]:
        if settings.MOCK_MODE:
            return self.get_mock_nifty_price()

        if not self._is_authenticated():
            logger.debug("[CMP_SOURCE=DISCONNECTED] Not authenticated — LTP skipped.")
            return None

        token = self.get_token()
        url = f"{self.base_url}/market-quote/ltp"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        params = {"instrument_key": "NSE_INDEX|Nifty 50"}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=8)
            if resp.status_code == 200:
                d = resp.json().get("data", {})
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
                now_ist = datetime.now(_IST)
                is_mkt = (now_ist.replace(hour=9,minute=0,second=0,microsecond=0)
                          <= now_ist <=
                          now_ist.replace(hour=15,minute=45,second=0,microsecond=0)
                          and now_ist.weekday() < 5)
                if is_mkt:
                    logger.error("[TOKEN] LTP call rejected (401) during market hours. Clearing token.")
                    self._clear_token()
                    self._invalidate_db_token()
                else:
                    logger.warning("[TOKEN] LTP call rejected (401) outside market hours — keeping token.")
                return None
            else:
                logger.error(f"[LIVE] LTP fetch failed (HTTP {resp.status_code}): {resp.text}")
                return None
        except Exception as e:
            logger.error(f"Error fetching LTP: {e}")
            return None

    def get_previous_day_ohlc(self) -> Optional[Dict]:
        if settings.MOCK_MODE:
            return self.get_mock_previous_day_ohlc()

        if not self._is_authenticated():
            logger.warning("[DATA_SOURCE=DISCONNECTED] Not authenticated — prev OHLC skipped.")
            return None

        token = self.get_token()
        today = datetime.now(_IST).date()
        from_d = (today - timedelta(days=7)).strftime("%Y-%m-%d")
        to_d = today.strftime("%Y-%m-%d")
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
                    if candle_date >= today.strftime("%Y-%m-%d"):
                        continue
                    previous = candle
                    break

                if previous:
                    h, l, c = float(previous[2]), float(previous[3]), float(previous[4])
                    logger.info(f"[LIVE] Prev day OHLC: H={h} L={l} C={c}")
                    return {"high": h, "low": l, "close": c}

                logger.warning("[LIVE] No prior trading day OHLC returned.")
                return None

            elif resp.status_code == 401:
                now_ist = datetime.now(_IST)
                is_mkt = (now_ist.replace(hour=9,minute=0,second=0,microsecond=0)
                          <= now_ist <=
                          now_ist.replace(hour=15,minute=45,second=0,microsecond=0)
                          and now_ist.weekday() < 5)
                if is_mkt:
                    logger.error("[TOKEN] Prev OHLC 401 during market hours — clearing token.")
                    self._clear_token()
                    self._invalidate_db_token()
                else:
                    logger.warning("[TOKEN] Prev OHLC 401 outside market hours — keeping token.")
                return None
            else:
                logger.error(f"[LIVE] Prev OHLC failed (HTTP {resp.status_code}): {resp.text}")
                return None

        except Exception as e:
            logger.error(f"Error fetching prev OHLC: {e}")
            return None

    def _invalidate_db_token(self):
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

    def get_option_ltp(self, option_symbol: str) -> Optional[float]:
        if settings.MOCK_MODE:
            return self.get_mock_option_ltp(option_symbol)

        token = self.get_token()
        if not token:
            return None

        url = f"{self.base_url}/market-quote/ltp"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        params = {"instrument_key": f"NSE_FO|{option_symbol}"}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=8)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                option_data = (data.get(f"NSE_FO:{option_symbol}")
                               or data.get(f"NSE_FO|{option_symbol}")
                               or next(iter(data.values()), {}))
                ltp = option_data.get("last_price") or option_data.get("ltp")
                if ltp is not None:
                    return float(ltp)
                logger.warning(f"Option LTP missing for {option_symbol}: {data}")
                return None

            if resp.status_code == 401:
                self._clear_token()
                self._invalidate_db_token()
                return None

            logger.error(f"Option LTP request failed for {option_symbol} (HTTP {resp.status_code}): {resp.text}")
            return None
        except Exception as e:
            logger.error(f"Error fetching option LTP for {option_symbol}: {e}")
            return None

    def _get_nearest_weekly_expiry_str(self) -> str:
        expiry = self._get_nearest_weekly_expiry_date()
        return expiry.strftime("%y%b%d").upper()

    def _get_nearest_weekly_expiry_date(self, now_ist: Optional[datetime] = None) -> date:
        now_ist = now_ist or datetime.now(_IST)
        today = now_ist.date()
        weekday = today.weekday()  # Monday=0, Tuesday=1, ..., Thursday=3, Friday=4

        if weekday <= 3:
            days_ahead = 3 - weekday
        else:
            days_ahead = 10 - weekday

        expiry = today + timedelta(days=days_ahead)
        if weekday == 3 and now_ist.time() >= time(15, 30):
            expiry += timedelta(days=7)

        while expiry.weekday() not in (3, 4) or expiry.strftime("%Y-%m-%d") in _NSE_HOLIDAYS:
            expiry += timedelta(days=1)

        return expiry

    def place_order(self, option_symbol: str, action: Literal["BUY", "SELL"], lots: int, paper: bool = True) -> Dict:
        qty = lots * settings.NIFTY_LOT_SIZE
        if settings.MOCK_MODE:
            paper = True
        if paper:
            logger.info(f"[PAPER ORDER] {action} {qty} units of {option_symbol}")
            avg_price = self.get_option_ltp(option_symbol)
            if avg_price is None:
                avg_price = 120.50
            return {
                "status": "success",
                "order_id": f"PAPER-{int(datetime.utcnow().timestamp())}",
                "avg_price": round(avg_price, 2),
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
