from datetime import datetime, timedelta
from typing import Dict, Any, List

from sqlalchemy.orm import Session

from database.models import Trade


def generate_historical_report(target_date: str, db: Session) -> Dict[str, Any]:
    """
    Generate a report of real trades executed on `target_date`.
    `target_date` is expected in YYYY-MM-DD (ISO) format and is interpreted
    as a calendar day in UTC (matches how `entry_time` is stored).

    Returns a dict with trades and aggregated metrics.
    """
    try:
        day = datetime.strptime(target_date, "%Y-%m-%d")
    except Exception:
        raise ValueError("Invalid date format. Use YYYY-MM-DD")

    start = datetime(day.year, day.month, day.day)
    end = start + timedelta(days=1)

    trades: List[Trade] = db.query(Trade).filter(Trade.entry_time >= start, Trade.entry_time < end).order_by(Trade.entry_time.asc()).all()

    result_trades: List[Dict[str, Any]] = []

    wins = 0
    losses = 0
    gross_profit = 0.0
    gross_loss = 0.0
    net_pnl = 0.0

    for t in trades:
        rec = {
            "id": t.id,
            "setup_name": t.setup_name,
            "trade_type": t.trade_type,
            "option_symbol": t.option_symbol,
            "strike_price": t.strike_price,
            "option_type": t.option_type,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "stop_loss": t.stop_loss,
            "take_profit": t.take_profit,
            "lots": t.lots,
            "status": t.status,
            "pnl": t.pnl,
            "entry_time": t.entry_time.isoformat() if t.entry_time else None,
            "exit_time": t.exit_time.isoformat() if t.exit_time else None,
            "is_paper": bool(t.is_paper),
        }
        result_trades.append(rec)

        pnl = t.pnl or 0.0
        net_pnl += pnl
        if pnl > 0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0:
            losses += 1
            gross_loss += pnl

    metrics = {
        "total_trades": len(result_trades),
        "wins": wins,
        "losses": losses,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(net_pnl, 2),
        "win_rate": round((wins / len(result_trades) * 100), 2) if result_trades else 0.0,
    }

    return {"date": target_date, "trades": result_trades, "metrics": metrics}
