import logging
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from config.settings import settings

logger = logging.getLogger("CPR_System.Database")

Base = declarative_base()
engine = None
SessionLocal = None

def _ensure_upstox_refresh_column(engine):
    inspector = inspect(engine)
    if "upstox_tokens" not in inspector.get_table_names():
        return
    existing_cols = [col["name"] for col in inspector.get_columns("upstox_tokens")]
    if "refresh_token" in existing_cols:
        return

    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE upstox_tokens ADD COLUMN IF NOT EXISTS refresh_token VARCHAR(500)"))
            else:
                conn.execute(text("ALTER TABLE upstox_tokens ADD COLUMN refresh_token VARCHAR(500)"))
        logger.info("Database migration: added refresh_token column to upstox_tokens.")
    except Exception as ex:
        logger.error(f"Failed to add refresh_token column to upstox_tokens: {ex}")


def init_db():
    global engine, SessionLocal
    db_url = settings.DATABASE_URL
    
    # Check if we should fallback to SQLite
    if not db_url.startswith("postgresql"):
        logger.info(f"Using local testing database (SQLite): {db_url}")
        # In SQLite, we need to allow multi-threaded access
        engine = create_engine(db_url, connect_args={"check_same_thread": False})
    else:
        logger.info("Initializing PostgreSQL database connections...")
        try:
            engine = create_engine(db_url, pool_pre_ping=True)
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}. Falling back to SQLite...")
            db_url = "sqlite:///./cpr_trading_fallback.db"
            engine = create_engine(db_url, connect_args={"check_same_thread": False})
            
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    # Create tables if SQLite (Postgres migrations are preferred but this is robust auto-fallback)
    try:
        from database.models import Trade, DailyState, StrategyState, UpstoxToken
        Base.metadata.create_all(bind=engine)
        _ensure_upstox_refresh_column(engine)
        logger.info("Database schemas validated and tables created successfully.")
    except Exception as ex:
        logger.error(f"Error during database table creation: {ex}")

def get_db():
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
