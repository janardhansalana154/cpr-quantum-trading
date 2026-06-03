import requests
import logging
from config.settings import settings

logger = logging.getLogger("CPR_System.Telegram")

def send_telegram_message(message: str) -> bool:
    """
    Sends a formatted telegram message securely.
    Supports markdown syntax.
    """
    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID
    
    if not token or not chat_id:
        logger.warning(f"Telegram Config Missing. Skip sending: {message[:60]}...")
        return False
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2"
    }
    
    # Simple formatting of markdown to Telegram MarkdownV2 spec (basic escaping)
    # We must escape special characters outside code blocks / formatting in MarkdownV2.
    # To prevent API failures, we will try MarkdownV2 first, and if it fails, fallback to simple HTML or plain text.
    try:
        response = requests.post(url, json=payload, timeout=8)
        if response.status_code == 200:
            return True
        else:
            logger.warning(f"Telegram MarkdownV2 failed (status {response.status_code}): {response.text}. Retrying in plain text...")
            fallback_payload = {
                "chat_id": chat_id,
                "text": message.replace("*", "").replace("`", "").replace("_", ""),
            }
            fallback_resp = requests.post(url, json=fallback_payload, timeout=8)
            if fallback_resp.status_code == 200:
                return True
            logger.error(f"Telegram plain-text retry failed (status {fallback_resp.status_code}): {fallback_resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram API Exception: {e}")
        return False

# Convenience wrappers for standardized messages
def notify_signal_detected(setup: str, details: str):
    msg = f"🔔 *SIGNAL DETECTED* 🔔\n\n*Setup:* `{setup}`\n*Details:* {details}"
    if not send_telegram_message(msg):
        logger.warning(f"Telegram signal notification failed for setup {setup}.")

def notify_order_placed(setup: str, buy_sell: str, details: str):
    emoji = "🟢" if buy_sell == "BUY" else "🔴"
    msg = f"{emoji} *ORDER PLACED ({buy_sell})* {emoji}\n\n*Setup:* `{setup}`\n*Details:* {details}"
    if not send_telegram_message(msg):
        logger.warning(f"Telegram order placement notification failed for setup {setup}.")

def notify_sl_hit(setup: str, symbol: str, loss: float, pnl: float):
    msg = f"🛑 *STOP LOSS HIT* 🛑\n\n*Setup:* `{setup}`\n*Symbol:* `{symbol}`\n*Loss P&L:* `₹{loss:.2f}`\n*Today's Net:* `₹{pnl:.2f}`"
    if not send_telegram_message(msg):
        logger.warning(f"Telegram stop-loss notification failed for setup {setup}.")

def notify_tp_hit(setup: str, symbol: str, profit: float, pnl: float):
    msg = f"🎯 *TAKE PROFIT HIT* 🎯\n\n*Setup:* `{setup}`\n*Symbol:* `{symbol}`\n*Profit P&L:* `₹{profit:.2f}`\n*Today's Net:* `₹{pnl:.2f}`"
    if not send_telegram_message(msg):
        logger.warning(f"Telegram take-profit notification failed for setup {setup}.")

def notify_limit_reached(reason: str, today_pnl: float):
    msg = f"⚠️ *DAILY LIMIT REACHED* ⚠️\n\n*Reason:* {reason}\n*Today's Net:* `₹{today_pnl:.2f}`\nTrading is suspended for today."
    if not send_telegram_message(msg):
        logger.warning("Telegram daily limit notification failed.")

def notify_system_error(error_msg: str):
    msg = f"❌ *SYSTEM ERROR* ❌\n\n`{error_msg}`"
    if not send_telegram_message(msg):
        logger.warning("Telegram system error notification failed.")
