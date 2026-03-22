import os
import re
import logging
import secrets
import time
import urllib.parse
import asyncio
from typing import Tuple, List, Optional
from io import BytesIO
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.error import BadRequest, NetworkError, TelegramError

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Constants ----------
API_URL = "https://pnbapi.paynearby.in/v1/retailers/lookup"
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Connection": "keep-alive",
    "Origin": "https://retailerportal.paynearby.in",
    "Platform": "nbt-agent-angular",
    "Referer": "https://retailerportal.paynearby.in/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
    "VersionId": "1.0.0",
    "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
    "sec-ch-ua-mobile": "?1",
    "sec-ch-ua-platform": '"Android"',
}
REQUEST_DELAY = 0.5          # seconds between sequential requests
MAX_CONCURRENT = 5           # parallel concurrency
PAGE_SIZE = 10               # results per page
MAX_RETRIES = 3              # number of retries for network errors
RETRY_BACKOFF = 1            # initial backoff seconds

# ---------- Helper Functions ----------
def generate_device_id() -> str:
    return secrets.token_hex(32)


def validate_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", phone))


def validate_proxy(proxy: str) -> bool:
    """Basic proxy URL validation."""
    if not proxy.startswith(("http://", "https://")):
        return False
    try:
        parsed = urllib.parse.urlparse(proxy)
        return bool(parsed.hostname)
    except Exception:
        return False


def test_proxy(proxy: str) -> Tuple[bool, str]:
    """Test if proxy is reachable and returns a response."""
    test_url = "https://api.telegram.org/bot"  # just a fast endpoint, we only need connection
    proxies = {"http": proxy, "https": proxy}
    try:
        response = requests.get(test_url, proxies=proxies, timeout=5)
        return True, "Proxy is reachable."
    except requests.exceptions.RequestException as e:
        return False, f"Proxy test failed: {str(e)}"


def check_registration(phone: str, proxy: str = None, retries: int = MAX_RETRIES) -> Tuple[str, str, str]:
    """
    Check if a phone number is registered.
    Returns (phone, status, details)
    status: 'registered', 'not_registered', 'error'
    """
    device_id = generate_device_id()
    headers = DEFAULT_HEADERS.copy()
    headers["DeviceId"] = device_id
    params = {"phone_number": phone}
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # Prepare session with retries
    session = requests.Session()
    retry_strategy = Retry(
        total=retries,
        backoff_factor=RETRY_BACKOFF,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        response = session.get(API_URL, params=params, headers=headers, proxies=proxies, timeout=10)
        if response.status_code == 404:
            try:
                data = response.json()
                error_msg = data.get("errors", {}).get("error_description", "Not registered")
            except Exception:
                error_msg = "This mobile number is not registered with us."
            return phone, "not_registered", error_msg

        response.raise_for_status()
        data = response.json()

        if "data" in data and isinstance(data["data"], dict):
            if data["data"].get("phone_no_verified") is True:
                return phone, "registered", "✅ Phone number is verified."
            else:
                return phone, "not_registered", "❌ Phone number not verified (maybe incomplete)."

        if "errors" in data and data["errors"].get("error_type") == "not_found":
            return phone, "not_registered", data["errors"].get("error_description", "Not registered")

        return phone, "error", f"⚠️ Unexpected response: {data}"
    except requests.exceptions.Timeout:
        return phone, "error", "⏰ Request timed out. Please try again later."
    except requests.exceptions.ConnectionError:
        return phone, "error", "🔌 Connection error. Check your network or proxy settings."
    except requests.exceptions.RequestException as e:
        return phone, "error", f"📡 Network error: {str(e)}"
    except ValueError as e:
        return phone, "error", f"📄 Invalid JSON response: {str(e)}"
    except Exception as e:
        return phone, "error", f"⚠️ Unknown error: {str(e)}"


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


async def safe_edit_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, text: str, **kwargs) -> bool:
    try:
        await context.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="HTML", **kwargs)
        return True
    except BadRequest as e:
        logger.warning(f"Failed to edit message: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error editing message: {e}")
        return False


async def send_progress_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE, processed: int, total: int, current_phone: str = None) -> None:
    percent = int(processed / total * 100) if total else 0
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    text = f"🔍 <b>Processing…</b> {processed}/{total} numbers ({percent}%)\n<code>{bar}</code>"
    if current_phone:
        text += f"\n📞 Currently checking: <code>{escape_html(current_phone)}</code>"

    if "progress_msg_id" in context.user_data:
        msg_id = context.user_data["progress_msg_id"]
        success = await safe_edit_message(context, chat_id, msg_id, text)
        if not success:
            msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            context.user_data["progress_msg_id"] = msg.message_id
    else:
        msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        context.user_data["progress_msg_id"] = msg.message_id


# ---------- Processing Functions ----------
async def process_numbers_sequential(numbers: List[str], proxy: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> List[Tuple[str, str, str]]:
    total = len(numbers)
    results = []
    for i, phone in enumerate(numbers):
        await send_progress_message(chat_id, context, i, total, current_phone=phone)
        phone, status, details = check_registration(phone, proxy)
        results.append((phone, status, details))
        time.sleep(REQUEST_DELAY)
    return results


async def process_numbers_parallel(numbers: List[str], proxy: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> List[Tuple[str, str, str]]:
    total = len(numbers)
    results = [None] * total
    processed = 0
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def check_one(index: int, phone: str):
        nonlocal processed
        async with semaphore:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, check_registration, phone, proxy)
            results[index] = result
            processed += 1
            await send_progress_message(chat_id, context, processed, total, current_phone=phone)

    tasks = [check_one(i, phone) for i, phone in enumerate(numbers)]
    await asyncio.gather(*tasks)
    return results


# ---------- Command Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("📖 Help", callback_data="help")],
        [InlineKeyboardButton("🌐 Set Proxy", callback_data="set_proxy")],
        [InlineKeyboardButton("🗑 Clear Proxy", callback_data="clear_proxy")],
        [InlineKeyboardButton("ℹ️ Status", callback_data="status")],
    ]
    await update.message.reply_text(
        "👋 <b>Welcome to the PayNearby Registration Checker Bot!</b>\n\n"
        "I can check whether mobile numbers are registered with PayNearby.\n\n"
        "<b>Commands:</b>\n"
        "/start – Show this message\n"
        "/help – Detailed help\n"
        "/proxy &lt;url&gt; – Set an HTTP/HTTPS proxy\n"
        "/proxy clear – Remove the proxy\n"
        "/check &lt;number&gt; – Check a single 10‑digit number\n"
        "/status – Show current proxy and pending jobs\n"
        "/cancel – Cancel any ongoing bulk operation\n\n"
        "Or send me a <code>.txt</code> file with one phone number per line to check multiple numbers.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📘 <b>How to use this bot</b>\n\n"
        "• <b>Single number</b>: <code>/check 9876543210</code>\n"
        "• <b>Bulk check</b>: Send a <code>.txt</code> file with one 10‑digit number per line.\n"
        "  After upload, you'll be asked to choose <b>Sequential</b> or <b>Parallel</b> mode.\n"
        "  - Sequential: slower but gentle on the server (0.5s delay).\n"
        "  - Parallel: faster but uses more concurrency (max 5 at once).\n"
        "• <b>Proxy</b>: If needed, set a proxy with <code>/proxy http://user:pass@host:port</code>\n"
        "  The bot will test the proxy before saving.\n"
        "• <b>Clear proxy</b>: <code>/proxy clear</code>\n"
        "• <b>Cancel</b>: <code>/cancel</code> to stop an ongoing bulk check.\n"
        "• <b>Progress</b>: During bulk checks, a progress bar shows the current number and percentage.\n"
        "• <b>Results</b>: After processing, you can download the full results and retry failed numbers.\n\n"
        "All requests use a random device ID to avoid blocking.\n"
        "Sequential mode adds a 0.5‑second delay between requests.",
        parse_mode="HTML",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel any ongoing bulk operation."""
    if context.user_data.get("pending_results"):
        context.user_data["pending_results"] = False
        context.user_data.pop("pending_numbers", None)
        context.user_data.pop("pending_filename", None)
        context.user_data.pop("progress_msg_id", None)
        await update.message.reply_text("✅ Ongoing bulk check cancelled.")
    else:
        await update.message.reply_text("No ongoing bulk check to cancel.")


async def set_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text(
            "Please provide a proxy URL.\n"
            "Example: <code>/proxy http://user:pass@proxy.example.com:8080</code>",
            parse_mode="HTML",
        )
        return
    proxy = args[0]
    if not validate_proxy(proxy):
        await update.message.reply_text(
            "❌ Invalid proxy URL. Must start with <code>http://</code> or <code>https://</code> and contain a host.",
            parse_mode="HTML",
        )
        return

    # Test proxy before saving
    status_msg = await update.message.reply_text("🔍 Testing proxy...")
    ok, msg = test_proxy(proxy)
    if not ok:
        await status_msg.edit_text(f"❌ Proxy test failed.\n{msg}")
        return
    await status_msg.edit_text(f"✅ Proxy test passed. {msg}")

    context.user_data["proxy"] = proxy
    await update.message.reply_text(f"✅ Proxy set to:\n<code>{escape_html(proxy)}</code>", parse_mode="HTML")


async def clear_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if "proxy" in context.user_data:
        del context.user_data["proxy"]
    if update.callback_query:
        await update.callback_query.edit_message_text("🗑 Proxy cleared.")
        await update.callback_query.answer()
    else:
        await update.message.reply_text("🗑 Proxy cleared.")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    proxy = context.user_data.get("proxy", "Not set")
    pending = context.user_data.get("pending_results", False)
    status_msg = f"🔧 <b>Current proxy</b>:\n<code>{escape_html(proxy)}</code>\n\n"
    status_msg += "⏳ There is a pending bulk check. Please wait for it to finish." if pending else "✅ No ongoing bulk check."
    await update.message.reply_text(status_msg, parse_mode="HTML")


async def check_single(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        await update.message.reply_text("Please provide a phone number. Example: <code>/check 9876543210</code>", parse_mode="HTML")
        return
    phone = args[0]
    if not validate_phone(phone):
        await update.message.reply_text("❌ Invalid phone number. Must be 10 digits.")
        return
    proxy = context.user_data.get("proxy")
    status_msg = await update.message.reply_text(f"🔎 Checking <code>{phone}</code>...", parse_mode="HTML")
    phone, status, details = check_registration(phone, proxy)
    emoji = "✅" if status == "registered" else "❌" if status == "not_registered" else "⚠️"
    await status_msg.edit_text(
        f"{emoji} <code>{phone}</code>: <b>{status.upper()}</b>\n{escape_html(details)}",
        parse_mode="HTML",
    )


# ---------- Bulk Processing ----------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("pending_results"):
        await update.message.reply_text("⏳ A bulk check is already in progress. Please wait for it to finish or use /cancel.")
        return

    document = update.message.document
    if not document.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a <code>.txt</code> file.", parse_mode="HTML")
        return

    file = await document.get_file()
    temp_file = f"temp_{update.effective_user.id}_{secrets.token_hex(4)}.txt"
    try:
        await file.download_to_drive(temp_file)
    except Exception as e:
        await update.message.reply_text(f"❌ Error downloading file: {e}")
        return

    numbers = []
    try:
        with open(temp_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if validate_phone(line):
                numbers.append(line)
            elif line:
                logger.warning(f"Skipped invalid phone: {line}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading file: {e}")
        return
    finally:
        os.remove(temp_file)

    if not numbers:
        await update.message.reply_text("No valid phone numbers found in the file.")
        return

    context.user_data["pending_numbers"] = numbers
    context.user_data["pending_filename"] = document.file_name

    keyboard = [
        [InlineKeyboardButton("🚀 Sequential Mode", callback_data="mode_sequential")],
        [InlineKeyboardButton("⚡ Parallel Mode", callback_data="mode_parallel")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_bulk")],
    ]
    await update.message.reply_text(
        f"📄 File <b>{escape_html(document.file_name)}</b> contains <b>{len(numbers)}</b> valid phone numbers.\n\n"
        "Choose processing mode:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )


async def mode_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    mode = query.data.split("_")[1]  # "sequential" or "parallel"
    numbers = context.user_data.get("pending_numbers")
    if not numbers:
        await query.edit_message_text("No numbers to process. Please send a file again.")
        try:
            await query.answer()
        except BadRequest:
            pass
        return

    # Clear previous state
    context.user_data.pop("pending_numbers", None)
    context.user_data.pop("pending_filename", None)
    context.user_data.pop("results", None)
    context.user_data.pop("temp_results", None)
    context.user_data.pop("progress_msg_id", None)
    context.user_data.pop("current_page", None)
    context.user_data.pop("total_pages", None)
    context.user_data["pending_results"] = True

    total = len(numbers)
    await query.edit_message_text(f"📂 Found {total} valid numbers. Starting <b>{mode}</b> check...", parse_mode="HTML")
    try:
        await query.answer()
    except BadRequest:
        pass

    chat_id = query.message.chat_id
    proxy = context.user_data.get("proxy")
    try:
        if mode == "sequential":
            results = await process_numbers_sequential(numbers, proxy, chat_id, context)
        else:
            results = await process_numbers_parallel(numbers, proxy, chat_id, context)
    except Exception as e:
        logger.exception("Processing error")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Processing failed: {str(e)}. You can retry by sending the file again."
        )
        context.user_data["pending_results"] = False
        return

    # Delete progress message
    if "progress_msg_id" in context.user_data:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data["progress_msg_id"])
        except Exception:
            pass
        del context.user_data["progress_msg_id"]

    context.user_data["results"] = results
    context.user_data["total_numbers"] = total
    context.user_data["pending_results"] = False

    # Summary
    registered = sum(1 for _, status, _ in results if status == "registered")
    not_registered = sum(1 for _, status, _ in results if status == "not_registered")
    errors = sum(1 for _, status, _ in results if status == "error")
    summary = (
        f"✅ <b>Done!</b>\n\n"
        f"📊 <b>Summary:</b>\n"
        f"• Registered: {registered}\n"
        f"• Not registered: {not_registered}\n"
        f"• Errors: {errors}\n"
        f"• Total processed: {total}\n\n"
    )
    keyboard = [[InlineKeyboardButton("📥 Download Results", callback_data="download_results")]]
    if errors > 0:
        keyboard.append([InlineKeyboardButton("🔄 Retry Failed Numbers", callback_data="retry_errors")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_results")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(chat_id=chat_id, text=summary, reply_markup=reply_markup, parse_mode="HTML")

    context.user_data["page_size"] = PAGE_SIZE
    context.user_data["total_pages"] = (total + PAGE_SIZE - 1) // PAGE_SIZE
    context.user_data["current_page"] = 0
    await send_results_page(update, context)


async def send_results_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    results = context.user_data.get("results", [])
    page = context.user_data.get("current_page", 0)
    page_size = context.user_data.get("page_size", PAGE_SIZE)
    total_pages = context.user_data.get("total_pages", 0)

    if not results:
        if update.callback_query:
            await update.callback_query.edit_message_text("No results to display.")
            try:
                await update.callback_query.answer()
            except BadRequest:
                pass
        else:
            await update.message.reply_text("No results to display.")
        return

    start = page * page_size
    end = start + page_size
    page_results = results[start:end]

    message = f"📋 <b>Results (Page {page+1}/{total_pages})</b>\n\n"
    for phone, status, details in page_results:
        emoji = "✅" if status == "registered" else "❌" if status == "not_registered" else "⚠️"
        message += f"{emoji} <code>{escape_html(phone)}</code>: <b>{status.upper()}</b>\n"
        if len(message) + len(details) + 5 < 4096:
            message += f"   <i>{escape_html(details)}</i>\n"

    keyboard = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀ Previous", callback_data="prev_page"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ▶", callback_data="next_page"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("📥 Download Results", callback_data="download_results")])
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close_results")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(message, reply_markup=reply_markup, parse_mode="HTML")
            if update.callback_query.data in ("prev_page", "next_page"):
                try:
                    await update.callback_query.answer()
                except BadRequest:
                    pass
        except BadRequest as e:
            logger.warning(f"Failed to edit message: {e}")
            await update.callback_query.message.reply_text(message, reply_markup=reply_markup, parse_mode="HTML")
    else:
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode="HTML")


async def download_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    results = context.user_data.get("results")
    if not results:
        await query.edit_message_text("No results available to download.")
        try:
            await query.answer()
        except BadRequest:
            pass
        return

    lines = [f"{phone}: {status.upper()} - {details}" for phone, status, details in results]
    content = "\n".join(lines)
    file_io = BytesIO(content.encode("utf-8"))
    file_io.name = "paynearby_results.txt"

    try:
        await query.message.reply_document(document=file_io, filename="paynearby_results.txt", caption="📄 Here are your full results.")
    except Exception as e:
        logger.error(f"Failed to send document: {e}")
        await query.message.reply_text("❌ Failed to generate download file.")
    try:
        await query.answer()
    except BadRequest:
        pass


async def retry_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    results = context.user_data.get("results")
    if not results:
        await query.edit_message_text("No results found. Please send a file again.")
        try:
            await query.answer()
        except BadRequest:
            pass
        return

    error_numbers = [phone for phone, status, _ in results if status == "error"]
    if not error_numbers:
        await query.edit_message_text("No errors to retry.")
        try:
            await query.answer()
        except BadRequest:
            pass
        return

    keyboard = [
        [InlineKeyboardButton("✅ Retry", callback_data="confirm_retry")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_retry")],
    ]
    await query.edit_message_text(
        f"Found {len(error_numbers)} numbers with errors. Do you want to retry them?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    try:
        await query.answer()
    except BadRequest:
        pass
    context.user_data["retry_numbers"] = error_numbers


async def confirm_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    error_numbers = context.user_data.get("retry_numbers")
    if not error_numbers:
        await query.edit_message_text("No numbers to retry.")
        try:
            await query.answer()
        except BadRequest:
            pass
        return

    context.user_data.pop("retry_numbers", None)
    context.user_data["pending_results"] = True

    await query.edit_message_text(f"Retrying {len(error_numbers)} numbers...")
    try:
        await query.answer()
    except BadRequest:
        pass

    chat_id = query.message.chat_id
    proxy = context.user_data.get("proxy")
    new_results = []
    total = len(error_numbers)
    for i, phone in enumerate(error_numbers):
        percent = int((i + 1) / total * 100)
        bar = "█" * int(percent / 10) + "░" * (10 - int(percent / 10))
        progress_text = f"🔄 Retrying... {i+1}/{total} ({percent}%)\n<code>{bar}</code>\n📞 Checking: <code>{escape_html(phone)}</code>"
        try:
            await query.edit_message_text(progress_text, parse_mode="HTML")
        except Exception:
            pass
        phone, status, details = check_registration(phone, proxy)
        new_results.append((phone, status, details))
        time.sleep(REQUEST_DELAY)

    original_results = context.user_data.get("results", [])
    error_indices = [i for i, (p, s, _) in enumerate(original_results) if s == "error"]
    for idx, (phone, status, details) in zip(error_indices, new_results):
        original_results[idx] = (phone, status, details)

    context.user_data["results"] = original_results
    context.user_data["pending_results"] = False

    registered = sum(1 for _, status, _ in original_results if status == "registered")
    not_registered = sum(1 for _, status, _ in original_results if status == "not_registered")
    errors = sum(1 for _, status, _ in original_results if status == "error")
    summary = (
        f"✅ <b>Retry completed!</b>\n\n"
        f"📊 <b>Updated Summary:</b>\n"
        f"• Registered: {registered}\n"
        f"• Not registered: {not_registered}\n"
        f"• Errors: {errors}\n"
        f"• Total processed: {len(original_results)}\n\n"
    )
    keyboard = [
        [InlineKeyboardButton("📥 Download Results", callback_data="download_results")],
        [InlineKeyboardButton("❌ Close", callback_data="close_results")],
    ]
    await query.edit_message_text(summary, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

    context.user_data["total_pages"] = (len(original_results) + PAGE_SIZE - 1) // PAGE_SIZE
    context.user_data["current_page"] = 0
    await send_results_page(update, context)


async def cancel_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.pop("retry_numbers", None)
    await query.edit_message_text("Retry cancelled.")
    try:
        await query.answer()
    except BadRequest:
        pass


async def cancel_bulk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    context.user_data.pop("pending_numbers", None)
    context.user_data.pop("pending_filename", None)
    await query.edit_message_text("Bulk check cancelled.")
    try:
        await query.answer()
    except BadRequest:
        pass


# ---------- Callback Handler ----------
async def pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if data in ("prev_page", "next_page", "download_results", "close_results") and "results" not in context.user_data:
        await query.edit_message_text("Results have been cleared. Please start a new check.")
        try:
            await query.answer()
        except BadRequest:
            pass
        return

    if data == "next_page":
        context.user_data["current_page"] = context.user_data.get("current_page", 0) + 1
        await send_results_page(update, context)
    elif data == "prev_page":
        context.user_data["current_page"] = context.user_data.get("current_page", 0) - 1
        await send_results_page(update, context)
    elif data == "close_results":
        context.user_data.pop("results", None)
        context.user_data.pop("current_page", None)
        context.user_data.pop("total_pages", None)
        await query.edit_message_text("✅ Results cleared.")
        try:
            await query.answer()
        except BadRequest:
            pass
    elif data == "download_results":
        await download_results(update, context)
    elif data == "retry_errors":
        await retry_errors(update, context)
    elif data == "confirm_retry":
        await confirm_retry(update, context)
    elif data == "cancel_retry":
        await cancel_retry(update, context)
    elif data == "mode_sequential":
        await mode_selection(update, context)
    elif data == "mode_parallel":
        await mode_selection(update, context)
    elif data == "cancel_bulk":
        await cancel_bulk(update, context)
    elif data == "help":
        await query.edit_message_text(
            "📘 <b>Help</b>\n\n"
            "• <code>/check &lt;number&gt;</code> – Check a single 10‑digit number.\n"
            "• Send a <code>.txt</code> file with one number per line for bulk checks.\n"
            "  After upload, choose <b>Sequential</b> or <b>Parallel</b> mode.\n"
            "• <code>/proxy &lt;url&gt;</code> – Set a proxy (e.g., <code>http://user:pass@host:port</code>).\n"
            "  The bot will test the proxy before saving.\n"
            "• <code>/proxy clear</code> – Remove proxy.\n"
            "• <code>/status</code> – Show current proxy and job status.\n"
            "• <code>/cancel</code> – Cancel ongoing bulk check.\n\n"
            "Results are paginated; you can download them or retry failed numbers.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_start")]]),
        )
        try:
            await query.answer()
        except BadRequest:
            pass
    elif data == "set_proxy":
        await query.edit_message_text(
            "To set a proxy, use the command:\n<code>/proxy http://user:pass@host:port</code>\n\n"
            "You can also clear it with <code>/proxy clear</code>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_start")]]),
        )
        try:
            await query.answer()
        except BadRequest:
            pass
    elif data == "clear_proxy":
        await clear_proxy_command(update, context)
    elif data == "status":
        proxy = context.user_data.get("proxy", "Not set")
        pending = context.user_data.get("pending_results", False)
        status_msg = f"🔧 <b>Current proxy</b>:\n<code>{escape_html(proxy)}</code>\n\n"
        status_msg += "⏳ There is a pending bulk check. Please wait for it to finish." if pending else "✅ No ongoing bulk check."
        await query.edit_message_text(status_msg, parse_mode="HTML")
        try:
            await query.answer()
        except BadRequest:
            pass
    elif data == "back_to_start":
        keyboard = [
            [InlineKeyboardButton("📖 Help", callback_data="help")],
            [InlineKeyboardButton("🌐 Set Proxy", callback_data="set_proxy")],
            [InlineKeyboardButton("🗑 Clear Proxy", callback_data="clear_proxy")],
            [InlineKeyboardButton("ℹ️ Status", callback_data="status")],
        ]
        await query.edit_message_text(
            "👋 <b>Welcome to the PayNearby Registration Checker Bot!</b>\n\n"
            "I can check whether mobile numbers are registered with PayNearby.\n\n"
            "Use the buttons below or type <code>/help</code> for commands.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        try:
            await query.answer()
        except BadRequest:
            pass


# ---------- Error Handler ----------
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    if update and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ An internal error occurred. Please try again later or contact the bot owner.",
            )
        except Exception:
            pass


# ---------- Main ----------
def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("No TELEGRAM_BOT_TOKEN found in environment.")
    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("proxy", set_proxy))
    application.add_handler(CommandHandler("proxy", clear_proxy_command, filters.Regex("clear$")))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("check", check_single))
    application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_document))

    callback_pattern = "|".join([
        "mode_sequential", "mode_parallel", "cancel_bulk",
        "prev_page", "next_page", "close_results", "download_results",
        "retry_errors", "confirm_retry", "cancel_retry",
        "help", "set_proxy", "clear_proxy", "status", "back_to_start"
    ])
    application.add_handler(CallbackQueryHandler(pagination_callback, pattern=f"^({callback_pattern})$"))
    application.add_error_handler(error_handler)

    application.run_polling()


if __name__ == "__main__":
    main()
