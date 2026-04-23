#!/usr/bin/env python3
import asyncio
import logging
import os
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from api_quotex import AsyncQuotexClient, OrderDirection, get_ssid

# ==================== CONFIGURATION ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
QUOTEX_EMAIL = os.environ.get("QUOTEX_EMAIL")
QUOTEX_PASSWORD = os.environ.get("QUOTEX_PASSWORD")
ACCOUNT_TYPE = "demo"           # "demo" or "live"

# Default settings
DEFAULT_AMOUNT = 5.0
DEFAULT_DURATION = 60           # seconds (5–60)
DEFAULT_DIRECTION = "CALL"      # "CALL" or "PUT"
AUTO_DELAY = 5                  # seconds between auto cycles
# =======================================================

if not TELEGRAM_BOT_TOKEN or not QUOTEX_EMAIL or not QUOTEX_PASSWORD:
    raise ValueError("Missing required environment variables.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global state
auto_trading_active = False
auto_trade_task = None
application = None
current_ssid = None

# Per-user settings
user_settings = {}

def get_user_settings(user_id: int) -> dict:
    if user_id not in user_settings:
        user_settings[user_id] = {
            "amount": DEFAULT_AMOUNT,
            "duration": DEFAULT_DURATION,
            "direction": DEFAULT_DIRECTION
        }
    return user_settings[user_id]

async def fetch_fresh_ssid():
    try:
        logger.info("Fetching fresh SSID from Quotex...")
        ssid_info = await asyncio.to_thread(
            get_ssid, email=QUOTEX_EMAIL, password=QUOTEX_PASSWORD
        )
        ssid = ssid_info.get("demo") if ACCOUNT_TYPE == "demo" else ssid_info.get("live")
        if ssid:
            logger.info("✅ Fresh SSID obtained.")
            return ssid
        else:
            logger.error("❌ Failed to obtain SSID.")
            return None
    except Exception as e:
        logger.error(f"❌ SSID fetch error: {e}")
        return None

async def get_valid_client():
    global current_ssid
    if not current_ssid:
        current_ssid = await fetch_fresh_ssid()
        if not current_ssid:
            return None
    is_demo = ACCOUNT_TYPE == "demo"
    client = AsyncQuotexClient(ssid=current_ssid, is_demo=is_demo)
    if await client.connect():
        return client
    logger.warning("Connection failed. Fetching new SSID...")
    current_ssid = await fetch_fresh_ssid()
    if not current_ssid:
        return None
    client = AsyncQuotexClient(ssid=current_ssid, is_demo=is_demo)
    if await client.connect():
        return client
    return None

async def execute_single_trade(chat_id: int, amount: float, duration: int, direction_str: str):
    await application.bot.send_message(chat_id=chat_id, text="🤖 Analyzing market...")
    client = await get_valid_client()
    if not client:
        await application.bot.send_message(chat_id=chat_id, text="❌ Failed to connect to Quotex.")
        return
    try:
        assets = await client.get_assets()
        best_asset = None
        best_payout = 0
        for asset in assets:
            if asset.payout and asset.payout > best_payout:
                best_payout = asset.payout
                best_asset = asset
        if not best_asset:
            await application.bot.send_message(chat_id=chat_id, text="⚠️ No tradable asset found.")
            return
        asset_symbol = best_asset.symbol
        payout = best_asset.payout
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"🎯 **Best Asset:** {asset_symbol}\n💰 **Payout:** {payout}%"
        )
        direction = OrderDirection.CALL if direction_str.upper() == "CALL" else OrderDirection.PUT
        direction_emoji = "⬆️" if direction == OrderDirection.CALL else "⬇️"
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"{direction_emoji} Placing **{direction.name}** order (${amount:.2f}, {duration}s)..."
        )
        order = await client.place_order(
            asset=asset_symbol, amount=amount, direction=direction, duration=duration
        )
        await application.bot.send_message(chat_id=chat_id, text=f"⏳ Order placed. Waiting {duration}s...")
        await asyncio.sleep(duration)
        profit, status = await client.check_win(order.order_id)
        emoji = "✅" if status == "WIN" else "❌"
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"{emoji} **Trade Result**\nAsset: {asset_symbol}\nDirection: {direction.name}\nOutcome: {status}\nP/L: ${profit:.2f}"
        )
    except Exception as e:
        logger.error(f"Error: {e}")
        await application.bot.send_message(chat_id=chat_id, text=f"🔥 Error: {e}")
    finally:
        await client.disconnect()

async def auto_trade_loop(chat_id: int, amount: float, duration: int, direction: str):
    global auto_trading_active
    while auto_trading_active:
        await execute_single_trade(chat_id, amount, duration, direction)
        if auto_trading_active:
            await application.bot.send_message(chat_id=chat_id, text=f"⏱️ Waiting {AUTO_DELAY}s before next cycle...")
            await asyncio.sleep(AUTO_DELAY)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! Commands:\n"
        "/set_amount <value> - Set trade amount\n"
        "/set_duration <seconds> - Set duration (5-60)\n"
        "/set_direction <call/put> - Set trade direction\n"
        "/status - Show current settings\n"
        "/trade - Execute one trade and stop\n"
        "/start_auto - Start continuous auto-trading\n"
        "/stop - Stop auto-trading\n"
        "/help - Show this help"
    )

async def set_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        amount = float(context.args[0])
        if amount <= 0: raise ValueError
        get_user_settings(user_id)["amount"] = amount
        await update.message.reply_text(f"✅ Trade amount set to ${amount:.2f}")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: /set_amount <number> (e.g., /set_amount 10)")

async def set_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        duration = int(context.args[0])
        if duration < 5 or duration > 60: raise ValueError
        get_user_settings(user_id)["duration"] = duration
        await update.message.reply_text(f"✅ Trade duration set to {duration} seconds")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: /set_duration <5-60> (e.g., /set_duration 30)")

async def set_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        direction_input = context.args[0].upper()
        if direction_input not in ["CALL", "PUT"]: raise ValueError
        get_user_settings(user_id)["direction"] = direction_input
        emoji = "⬆️" if direction_input == "CALL" else "⬇️"
        await update.message.reply_text(f"{emoji} Trade direction set to **{direction_input}**")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Usage: /set_direction call OR /set_direction put")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)
    direction_emoji = "⬆️" if settings["direction"] == "CALL" else "⬇️"
    await update.message.reply_text(
        f"📊 **Current Settings**\n"
        f"Amount: ${settings['amount']:.2f}\nDuration: {settings['duration']}s\n"
        f"Direction: {direction_emoji} {settings['direction']}\n"
        f"Auto-trading: {'🟢 Active' if auto_trading_active else '🔴 Inactive'}"
    )

async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    settings = get_user_settings(user_id)
    direction_emoji = "⬆️" if settings["direction"] == "CALL" else "⬇️"
    await update.message.reply_text(
        f"🔔 Starting **single trade cycle**\n"
        f"Amount: ${settings['amount']:.2f} | Duration: {settings['duration']}s | Direction: {direction_emoji} {settings['direction']}"
    )
    await execute_single_trade(chat_id, settings["amount"], settings["duration"], settings["direction"])
    await update.message.reply_text("🛑 Trade cycle completed. Bot is now idle.")

async def start_auto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_active, auto_trade_task
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    settings = get_user_settings(user_id)
    if auto_trading_active:
        await update.message.reply_text("⚠️ Auto-trading is already running.")
        return
    auto_trading_active = True
    auto_trade_task = asyncio.create_task(
        auto_trade_loop(chat_id, settings["amount"], settings["duration"], settings["direction"])
    )
    direction_emoji = "⬆️" if settings["direction"] == "CALL" else "⬇️"
    await update.message.reply_text(
        f"🔄 **Auto-trading started!**\n"
        f"Amount: ${settings['amount']:.2f} | Duration: {settings['duration']}s | Direction: {direction_emoji} {settings['direction']}\n"
        f"Use /stop to halt."
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_active, auto_trade_task
    if not auto_trading_active:
        await update.message.reply_text("ℹ️ Auto-trading is not active.")
        return
    auto_trading_active = False
    if auto_trade_task:
        auto_trade_task.cancel()
        auto_trade_task = None
    await update.message.reply_text("🛑 Auto-trading stopped.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "**Commands:**\n"
        "/set_amount <value> - Set trade amount\n"
        "/set_duration <seconds> - Set duration 5-60s\n"
        "/set_direction <call/put> - Set direction\n"
        "/status - Show current settings\n"
        "/trade - Execute one trade and stop\n"
        "/start_auto - Start continuous auto-trading\n"
        "/stop - Stop auto-trading\n"
        "/help - Show this help"
    )

def main():
    global application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_amount", set_amount))
    application.add_handler(CommandHandler("set_duration", set_duration))
    application.add_handler(CommandHandler("set_direction", set_direction))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("trade", trade_command))
    application.add_handler(CommandHandler("start_auto", start_auto_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("help", help_command))
    logger.info("🤖 Full-control Quotex bot is running. Send /start on Telegram.")
    application.run_polling()

if __name__ == "__main__":
    main()
