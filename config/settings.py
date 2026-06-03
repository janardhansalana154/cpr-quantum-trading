import os
import logging
from typing import Literal
from pydantic_settings import BaseSettings
from pydantic import Field

class Settings(BaseSettings):
    # Upstox API Credentials
    UPSTOX_API_KEY: str = Field(default="mock_api_key", description="Upstox API Client ID")
    UPSTOX_API_SECRET: str = Field(default="mock_secret", description="Upstox Client Secret")
    UPSTOX_REDIRECT_URI: str = Field(default="http://localhost:3000/api/v1/callback", description="OAuth Redirect URI")
    UPSTOX_SECRETS_PATH: str = Field(default="", description="Resolved path for Upstox secrets storage")
    
    # Telegram configuration
    TELEGRAM_BOT_TOKEN: str = Field(default="", description="Telegram Bot Token")
    TELEGRAM_CHAT_ID: str = Field(default="", description="Telegram Chat ID or User ID")

    # Bot runtime parameters
    TRADING_MODE: Literal["paper", "live"] = Field(default="paper", description="Trading mode ('paper' or 'live')")
    MOCK_MODE: bool = Field(default=False, description="Enable dashboard-driven mock trading mode with simulated candles and paper order execution")
    DATABASE_URL: str = Field(default="sqlite:///./cpr_trading.db", description="Database connection string")
    LOG_LEVEL: str = Field(default="INFO", description="Global log level")
    
    # CPR Strategy specifics
    FAILURE_WINDOW: int = Field(default=10, ge=1)
    RETEST_WINDOW: int = Field(default=10, ge=1)
    CONFIRMATION_WINDOW: int = Field(default=10, ge=1)
    ENTRY_TRIGGER_WINDOW: int = Field(default=10, ge=1)
    RETEST_TOLERANCE: float = Field(default=5.0, ge=0.0)
    SL_BUFFER: float = Field(default=3.0, ge=0.0)
    TARGET_BUFFER: float  = Field(default=3.0, ge=0.0)
    REWARD_RATIO:  float  = Field(default=2.0, ge=0.5, description="Risk:Reward multiplier. 2.0 = 1:2 RR, 1.5 = 1:1.5 RR, 3.0 = 1:3 RR")
    
    # Risk settings
    DAILY_LOSS_LIMIT: float = Field(default=2000.0, description="Daily Stop-Loss limit in currency units (INR)")
    NO_ENTRY_AFTER_HOUR: int   = Field(default=14, description="No new entries after this IST hour (14 = 2 PM)")
    NO_ENTRY_AFTER_MIN:  int   = Field(default=0,  description="No new entries after this IST minute")
    SQUAREOFF_HOUR: int        = Field(default=15, description="Force square-off all positions at this IST hour (15 = 3 PM)")
    SQUAREOFF_MIN:  int        = Field(default=0,  description="Force square-off at this IST minute")
    MAX_DAILY_TRADES: int = Field(default=2, description="Maximum number of trade executions allowed per day")
    POSITION_LOTS: int   = Field(default=1,  description="Number of lots to trade per order")
    NIFTY_LOT_SIZE: int  = Field(default=65, description="NIFTY options lot size (NSE defined)")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

# Instantiate settings
def _resolve_upstox_secrets_path() -> str:
    import os
    paths_to_try = [
        os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(__file__)), "upstox_secrets.json")),
        os.path.join(os.getcwd(), "upstox_secrets.json"),
        "/app/upstox_secrets.json",
        "/tmp/upstox_secrets.json"
    ]
    for p in paths_to_try:
        try:
            if os.path.exists(p):
                return p
            parent = os.path.dirname(p)
            if parent and os.path.exists(parent) and os.access(parent, os.W_OK):
                # Ensure we can actually write a test file here
                test_file = os.path.join(parent, ".upstox_write_test")
                with open(test_file, "w") as tf:
                    tf.write("write_test")
                os.remove(test_file)
                return p
        except:
            continue
    return "/tmp/upstox_secrets.json"

try:
    settings = Settings()
    settings.UPSTOX_SECRETS_PATH = _resolve_upstox_secrets_path()
except Exception as e:
    # Handle if some required variables failed validation (fallback to safe defaults for local test)
    print(f"Warning loading settings: {e}. Falling back to default settings.")
    class FallbackSettings:
        UPSTOX_API_KEY = os.environ.get("UPSTOX_API_KEY", "mock_key")
        UPSTOX_API_SECRET = os.environ.get("UPSTOX_API_SECRET", "mock_secret")
        UPSTOX_REDIRECT_URI = os.environ.get("UPSTOX_REDIRECT_URI", "http://localhost:3000/api/v1/callback")
        UPSTOX_SECRETS_PATH = _resolve_upstox_secrets_path()
        TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
        TRADING_MODE = os.environ.get("TRADING_MODE", "paper")
        MOCK_MODE = os.environ.get("MOCK_MODE", "false").lower() in ("1","true","yes")
        DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./cpr_trading.db")
        LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
        FAILURE_WINDOW = int(os.environ.get("FAILURE_WINDOW", 10))
        RETEST_WINDOW = int(os.environ.get("RETEST_WINDOW", 10))
        CONFIRMATION_WINDOW = int(os.environ.get("CONFIRMATION_WINDOW", 10))
        ENTRY_TRIGGER_WINDOW = int(os.environ.get("ENTRY_TRIGGER_WINDOW", 10))
        RETEST_TOLERANCE = float(os.environ.get("RETEST_TOLERANCE", 5.0))
        SL_BUFFER = float(os.environ.get("SL_BUFFER", 3.0))
        TARGET_BUFFER  = float(os.environ.get("TARGET_BUFFER", 3.0))
        REWARD_RATIO   = float(os.environ.get("REWARD_RATIO", 2.0))
        DAILY_LOSS_LIMIT        = float(os.environ.get("DAILY_LOSS_LIMIT", 2000.0))
        NO_ENTRY_AFTER_HOUR     = int(os.environ.get("NO_ENTRY_AFTER_HOUR", 14))
        NO_ENTRY_AFTER_MIN      = int(os.environ.get("NO_ENTRY_AFTER_MIN", 0))
        SQUAREOFF_HOUR          = int(os.environ.get("SQUAREOFF_HOUR", 15))
        SQUAREOFF_MIN           = int(os.environ.get("SQUAREOFF_MIN", 0))
        MAX_DAILY_TRADES = int(os.environ.get("MAX_DAILY_TRADES", 2))
        POSITION_LOTS   = int(os.environ.get("POSITION_LOTS", 1))
        NIFTY_LOT_SIZE  = int(os.environ.get("NIFTY_LOT_SIZE", 65))
    settings = FallbackSettings()

# Configure logging
log_numeric_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
logging.basicConfig(
    level=log_numeric_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("cpr_trading_system.log")
    ]
)
logger = logging.getLogger("CPR_System")
