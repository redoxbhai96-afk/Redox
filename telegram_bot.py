"""
Website Screenshot Bot — Multi-user, Multi-URL, GIF/Timelapse, Stealth mode
"""

import asyncio
import io
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from PIL import Image
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN           = "8746229546:AAGQcQcjiYUhAaTOnLuB7mtrpdyx_BItcF4"
DEFAULT_INTERVAL    = 30          # seconds between auto-screenshots
JPEG_QUALITY        = 80
VIEWPORT_W, VIEWPORT_H = 1280, 720
MAX_FRAMES          = 20          # GIF frame buffer per URL
GIF_FPS             = 2           # frames per second in generated GIF
GIF_RESIZE          = (960, 540)  # GIF frame size (smaller = smaller file)

CHROMIUM_PATH = (
    "/nix/store/qa9cnw4v5xkxyip6mb9kxqfq1z4x2dx1-chromium-138.0.7204.100/bin/chromium"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver',  { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',    { get: () => [1, 2, 3] });
    Object.defineProperty(navigator, 'languages',  { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
"""
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.WARNING)
logger = logging.getLogger(__name__)


# ─── PER-USER SESSION ─────────────────────────────────────────────────────────

@dataclass
class URLSlot:
    """Tracks one monitored URL for a user."""
    url:         str
    page:        Optional[Page]  = field(default=None, repr=False)
    msg_id:      Optional[int]   = None   # Telegram message id (edited in-place)
    shot_count:  int             = 0
    frames:      list            = field(default_factory=list)  # JPEG bytes for GIF


class Session:
    def __init__(self, chat_id: int):
        self.chat_id    = chat_id
        self.slots:     list[URLSlot] = []
        self.running    = False
        self.paused     = False
        self.interval   = DEFAULT_INTERVAL

        self.playwright  = None
        self.browser:    Optional[Browser]        = None
        self.context:    Optional[BrowserContext] = None
        self.loop_task:  Optional[asyncio.Task]   = None

        self.panel_msg_id: Optional[int] = None

    # ── helpers ──────────────────────────────────────────────────────────────

    def slot_by_url(self, url: str) -> Optional[URLSlot]:
        return next((s for s in self.slots if s.url == url), None)

    def total_shots(self) -> int:
        return sum(s.shot_count for s in self.slots)

    def url_list_text(self) -> str:
        if not self.slots:
            return "_Koi URL nahi_"
        lines = []
        for i, s in enumerate(self.slots, 1):
            lines.append(f"  {i}. `{s.url}` — {s.shot_count} shots")
        return "\n".join(lines)

    def panel_text(self) -> str:
        status = "⏸ Paused" if self.paused else ("🟢 Live" if self.running else "🔴 Stopped")
        ts = datetime.now().strftime("%H:%M:%S")
        return (
            f"🎛 *Control Panel*\n\n"
            f"📊 Status: {status}  •  ⏱ {self.interval}s\n"
            f"📸 Total screenshots: *{self.total_shots()}*\n"
            f"🕐 Last update: {ts}\n\n"
            f"🌐 *Monitored URLs:*\n{self.url_list_text()}"
        )

    def control_keyboard(self) -> InlineKeyboardMarkup:
        pause_label = "▶ Resume" if self.paused else "⏸ Pause"
        row1 = [
            InlineKeyboardButton("📸 Snap All",   callback_data="snap"),
            InlineKeyboardButton(pause_label,      callback_data="toggle_pause"),
            InlineKeyboardButton("⛔ Stop",        callback_data="stop"),
        ]
        row2 = [
            InlineKeyboardButton("🎬 GIF",        callback_data="gif"),
            InlineKeyboardButton("➕ Add URL",     callback_data="addurl_hint"),
            InlineKeyboardButton("📋 URL List",   callback_data="urllist"),
        ]
        iv = self.interval
        row3 = [
            InlineKeyboardButton(f"{'✅' if iv==10  else ''}10s",  callback_data="iv_10"),
            InlineKeyboardButton(f"{'✅' if iv==30  else ''}30s",  callback_data="iv_30"),
            InlineKeyboardButton(f"{'✅' if iv==60  else ''}1min", callback_data="iv_60"),
            InlineKeyboardButton(f"{'✅' if iv==300 else ''}5min", callback_data="iv_300"),
        ]
        return InlineKeyboardMarkup([row1, row2, row3])

    async def refresh_panel(self, app: Application):
        if not self.panel_msg_id:
            return
        try:
            await app.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.panel_msg_id,
                text=self.panel_text(),
                parse_mode="Markdown",
                reply_markup=self.control_keyboard(),
            )
        except (BadRequest, RetryAfter):
            pass

    # ── browser lifecycle ─────────────────────────────────────────────────────

    async def launch(self) -> bool:
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                executable_path=CHROMIUM_PATH,
                args=[
                    "--no-sandbox", "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars", f"--window-size={VIEWPORT_W},{VIEWPORT_H}",
                ],
            )
            self.context = await self.browser.new_context(
                viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                user_agent=USER_AGENT,
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                ignore_https_errors=True,
            )
            await self.context.add_init_script(STEALTH_JS)
            return True
        except Exception as e:
            logger.error(f"launch: {e}")
            return False

    async def open_slot(self, slot: URLSlot) -> bool:
        try:
            if slot.page:
                await slot.page.close()
            slot.page = await self.context.new_page()
            await slot.page.goto(slot.url, wait_until="domcontentloaded", timeout=30_000)
            await _human_behaviour(slot.page)
            return True
        except Exception as e:
            logger.error(f"open_slot({slot.url}): {e}")
            return False

    async def close(self):
        for s in self.slots:
            try:
                if s.page: await s.page.close()
            except Exception:
                pass
            s.page = None
        try:
            if self.context: await self.context.close()
            if self.browser:  await self.browser.close()
            if self.playwright: await self.playwright.stop()
        except Exception as e:
            logger.error(f"close: {e}")
        self.browser = self.context = self.playwright = None

    # ── screenshot & recovery ─────────────────────────────────────────────────

    async def capture(self, slot: URLSlot) -> Optional[bytes]:
        if not slot.page:
            return None
        try:
            await slot.page.reload(wait_until="domcontentloaded", timeout=30_000)
            await _human_behaviour(slot.page)
            return await slot.page.screenshot(type="jpeg", quality=JPEG_QUALITY)
        except Exception as e:
            logger.warning(f"capture({slot.url}): {e}")
            return None

    async def recover(self, slot: URLSlot) -> bool:
        """Try to recover a crashed page; full browser restart if needed."""
        try:
            slot.page = await self.context.new_page()
            await slot.page.goto(slot.url, wait_until="domcontentloaded", timeout=30_000)
            return True
        except Exception:
            await self.close()
            ok = await self.launch()
            if not ok:
                return False
            for s in self.slots:
                await self.open_slot(s)
            return slot.page is not None

    async def push_shot(self, app: Application, slot: URLSlot, instant: bool = False):
        """Take screenshot and edit-in-place the slot's Telegram message."""
        data = await self.capture(slot)
        if data is None:
            ok = await self.recover(slot)
            if not ok:
                await app.bot.send_message(
                    self.chat_id,
                    f"❌ `{slot.url}` recover nahi ho saka.",
                    parse_mode="Markdown",
                )
                return
            data = await self.capture(slot)
            if data is None:
                return

        # Store frame for GIF
        slot.frames.append(data)
        if len(slot.frames) > MAX_FRAMES:
            slot.frames.pop(0)

        slot.shot_count += 1
        ts    = datetime.now().strftime("%H:%M:%S")
        label = "⚡ Snap" if instant else f"🔄 #{slot.shot_count}"
        short_url = slot.url.replace("https://", "").replace("http://", "")[:40]
        caption = f"{label}  •  {short_url}  •  {ts}"

        bio = io.BytesIO(data)
        bio.name = "ss.jpg"

        if slot.msg_id is None:
            msg = await app.bot.send_photo(
                chat_id=self.chat_id, photo=bio, caption=caption
            )
            slot.msg_id = msg.message_id
        else:
            try:
                await app.bot.edit_message_media(
                    chat_id=self.chat_id,
                    message_id=slot.msg_id,
                    media=InputMediaPhoto(media=bio, caption=caption),
                )
            except BadRequest as e:
                if "not modified" not in str(e).lower():
                    msg = await app.bot.send_photo(
                        chat_id=self.chat_id, photo=bio, caption=caption
                    )
                    slot.msg_id = msg.message_id
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)

    async def snap_all(self, app: Application, instant: bool = False):
        for slot in self.slots:
            await self.push_shot(app, slot, instant=instant)
        await self.refresh_panel(app)

    # ── GIF builder ───────────────────────────────────────────────────────────

    def build_gif(self, slot: URLSlot) -> Optional[bytes]:
        if len(slot.frames) < 2:
            return None
        try:
            images = []
            for raw in slot.frames:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                img = img.resize(GIF_RESIZE, Image.LANCZOS)
                images.append(img)
            out = io.BytesIO()
            images[0].save(
                out, format="GIF", save_all=True,
                append_images=images[1:],
                loop=0,
                duration=1000 // GIF_FPS,
                optimize=True,
            )
            out.seek(0)
            return out.getvalue()
        except Exception as e:
            logger.error(f"build_gif: {e}")
            return None

    # ── auto loop ─────────────────────────────────────────────────────────────

    async def run_loop(self, app: Application):
        while self.running:
            await asyncio.sleep(self.interval)
            if not self.running:
                break
            if self.paused or not self.slots:
                continue
            await self.snap_all(app)

    async def do_stop(self, app: Application):
        self.running = False
        if self.loop_task and not self.loop_task.done():
            self.loop_task.cancel()
        await self.close()

        if self.panel_msg_id:
            try:
                await app.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.panel_msg_id,
                    text=(
                        f"⛔ *Session band hua*\n\n"
                        f"📸 Total screenshots: *{self.total_shots()}*\n"
                        f"🌐 URLs:\n{self.url_list_text()}"
                    ),
                    parse_mode="Markdown",
                )
            except BadRequest:
                pass

        self.slots.clear()
        self.panel_msg_id = None
        self.running = self.paused = False
        self.loop_task = None


# ─── GLOBAL SESSION REGISTRY ──────────────────────────────────────────────────

_sessions: dict[int, Session] = {}

def get_session(chat_id: int) -> Session:
    if chat_id not in _sessions:
        _sessions[chat_id] = Session(chat_id)
    return _sessions[chat_id]


# ─── STEALTH HELPERS ──────────────────────────────────────────────────────────

async def _human_behaviour(page: Page):
    try:
        await page.mouse.move(
            random.randint(200, 900), random.randint(100, 500),
            steps=random.randint(5, 12),
        )
        dy = random.randint(60, 200)
        await page.evaluate(f"window.scrollBy(0, {dy})")
        await asyncio.sleep(random.uniform(0.3, 0.7))
        await page.evaluate(f"window.scrollBy(0, -{dy})")
    except Exception:
        pass


# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Website Screenshot Bot*\n\n"
        "Har user ka alag session hota hai — koi kisi ka data nahi dekh sakta.\n\n"
        "*Commands:*\n"
        "📌 `/open <url>` — Pehla URL kholein, session shuru karein\n"
        "➕ `/addurl <url>` — Aur URLs add karein (sab saath monitor honge)\n"
        "❌ `/removeurl <url>` — Koi URL hatayein\n"
        "📸 `/snap` — Sabka turant screenshot\n"
        "🎬 `/gif [url]` — GIF banao (last screenshots se)\n"
        "⏱ `/setinterval <sec>` — Refresh time badlein\n"
        "⛔ `/stop` — Sab band karein\n"
        "ℹ️ `/status` — Current halat\n\n"
        "_Control panel ke buttons se bhi sab control ho sakta hai._",
        parse_mode="Markdown",
    )


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)

    if not context.args:
        await update.message.reply_text("⚠️ URL dein: `/open https://example.com`", parse_mode="Markdown")
        return

    url = _fix_url(context.args[0])

    # Stop existing session first
    if sess.running:
        await sess.do_stop(context.application)

    sess.__init__(chat_id)   # fresh state

    msg = await update.message.reply_text(
        f"⏳ Stealth browser shuru kar raha hoon...\n🔗 `{url}`",
        parse_mode="Markdown",
    )

    ok = await sess.launch()
    if not ok:
        await msg.edit_text("❌ Browser launch nahi hua. Dobara try karein.")
        return

    slot = URLSlot(url=url)
    ok = await sess.open_slot(slot)
    if not ok:
        await msg.edit_text("❌ URL open nahi ho saka. URL sahi hai?")
        await sess.close()
        return

    sess.slots.append(slot)
    await msg.delete()

    # Send control panel
    panel = await context.bot.send_message(
        chat_id=chat_id,
        text=sess.panel_text(),
        parse_mode="Markdown",
        reply_markup=sess.control_keyboard(),
    )
    sess.panel_msg_id = panel.message_id
    sess.running = True

    await sess.snap_all(context.application)
    sess.loop_task = asyncio.create_task(sess.run_loop(context.application))


async def cmd_addurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)

    if not context.args:
        await update.message.reply_text("⚠️ URL dein: `/addurl https://example.com`", parse_mode="Markdown")
        return

    if not sess.running:
        await update.message.reply_text("⚠️ Pehle `/open <url>` se session shuru karein.", parse_mode="Markdown")
        return

    url = _fix_url(context.args[0])
    if sess.slot_by_url(url):
        await update.message.reply_text(f"ℹ️ Yeh URL pehle se hai:\n`{url}`", parse_mode="Markdown")
        return

    msg = await update.message.reply_text(f"⏳ Add kar raha hoon: `{url}`", parse_mode="Markdown")
    slot = URLSlot(url=url)
    ok = await sess.open_slot(slot)
    if not ok:
        await msg.edit_text(f"❌ URL open nahi ho saka: `{url}`", parse_mode="Markdown")
        return

    sess.slots.append(slot)
    await msg.delete()

    # Take first screenshot for this slot
    await sess.push_shot(context.application, slot, instant=True)
    await sess.refresh_panel(context.application)

    await update.message.reply_text(
        f"✅ Add ho gaya!\n`{url}`\nAb *{len(sess.slots)}* URLs monitor ho rahe hain.",
        parse_mode="Markdown",
    )


async def cmd_removeurl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)

    if not context.args:
        await update.message.reply_text("⚠️ URL dein: `/removeurl https://example.com`", parse_mode="Markdown")
        return

    url = _fix_url(context.args[0])
    slot = sess.slot_by_url(url)
    if not slot:
        await update.message.reply_text(f"❌ Yeh URL list mein nahi hai:\n`{url}`", parse_mode="Markdown")
        return

    if slot.page:
        try: await slot.page.close()
        except Exception: pass

    sess.slots.remove(slot)
    await sess.refresh_panel(context.application)
    await update.message.reply_text(f"🗑 Remove ho gaya:\n`{url}`", parse_mode="Markdown")


async def cmd_snap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    if not sess.running:
        await update.message.reply_text("⚠️ Koi session nahi chal raha. `/open <url>` karo.", parse_mode="Markdown")
        return
    await update.message.delete()
    await sess.snap_all(context.application, instant=True)


async def cmd_gif(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)

    if not sess.slots:
        await update.message.reply_text("⚠️ Koi URL monitor nahi ho raha.", parse_mode="Markdown")
        return

    # Which slot? First arg = URL, else first slot
    slot = sess.slots[0]
    if context.args:
        url = _fix_url(context.args[0])
        s = sess.slot_by_url(url)
        if s:
            slot = s

    if len(slot.frames) < 2:
        await update.message.reply_text(
            f"⏳ Abhi sirf *{len(slot.frames)}* frames hain.\n"
            "Thoda wait karo taaki zyada screenshots aa jayein, phir /gif karo.",
            parse_mode="Markdown",
        )
        return

    msg = await update.message.reply_text("🎬 GIF bana raha hoon...")
    gif_bytes = sess.build_gif(slot)
    if not gif_bytes:
        await msg.edit_text("❌ GIF banana mein error aaya.")
        return

    short = slot.url.replace("https://", "").replace("http://", "")[:35]
    await msg.delete()
    await context.bot.send_animation(
        chat_id=chat_id,
        animation=io.BytesIO(gif_bytes),
        caption=(
            f"🎬 *Timelapse GIF*\n"
            f"🔗 `{short}`\n"
            f"📸 {len(slot.frames)} frames  •  {GIF_FPS} FPS"
        ),
        parse_mode="Markdown",
        filename="timelapse.gif",
    )


async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Example: `/setinterval 30`", parse_mode="Markdown")
        return
    secs = int(context.args[0])
    if secs < 5:
        await update.message.reply_text("⚠️ Minimum 5 seconds hone chahiye.")
        return
    sess.interval = secs
    await sess.refresh_panel(context.application)
    await update.message.reply_text(f"✅ Interval set: *{secs} seconds*", parse_mode="Markdown")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    if not sess.running:
        await update.message.reply_text("ℹ️ Kuch chal nahi raha.")
        return
    await sess.do_stop(context.application)
    await update.message.reply_text("⛔ Band kar diya! `/open <url>` se dobara shuru karo.", parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    if not sess.running:
        await update.message.reply_text("ℹ️ Koi active session nahi hai.")
        return
    await update.message.reply_text(sess.panel_text(), parse_mode="Markdown")


# ─── INLINE BUTTON HANDLER ────────────────────────────────────────────────────

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    sess = get_session(chat_id)
    data = query.data

    if data == "snap":
        if not sess.running:
            await query.answer("Session chal nahi raha!", show_alert=True)
            return
        await sess.snap_all(context.application, instant=True)

    elif data == "toggle_pause":
        if not sess.running:
            await query.answer("Session chal nahi raha!", show_alert=True)
            return
        sess.paused = not sess.paused
        await query.answer("▶ Resume kiya" if not sess.paused else "⏸ Pause kiya")
        await sess.refresh_panel(context.application)

    elif data == "stop":
        if not sess.running:
            await query.answer("Pehle se band hai!", show_alert=True)
            return
        await sess.do_stop(context.application)
        await query.answer("⛔ Band kar diya!")

    elif data == "gif":
        if not sess.slots:
            await query.answer("Koi URL nahi!", show_alert=True)
            return
        slot = sess.slots[0]
        if len(slot.frames) < 2:
            await query.answer(f"Sirf {len(slot.frames)} frames — thoda wait karo!", show_alert=True)
            return
        await query.answer("🎬 GIF bana raha hoon...")
        gif_bytes = sess.build_gif(slot)
        if gif_bytes:
            short = slot.url.replace("https://","").replace("http://","")[:35]
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=io.BytesIO(gif_bytes),
                caption=(
                    f"🎬 *Timelapse GIF*\n`{short}`\n"
                    f"📸 {len(slot.frames)} frames  •  {GIF_FPS} FPS"
                ),
                parse_mode="Markdown",
                filename="timelapse.gif",
            )

    elif data == "addurl_hint":
        await query.answer(
            "Naya URL add karne ke liye:\n/addurl https://example.com",
            show_alert=True,
        )

    elif data == "urllist":
        await query.answer(
            f"Monitored URLs ({len(sess.slots)}):\n" +
            "\n".join(s.url for s in sess.slots) or "Koi nahi",
            show_alert=True,
        )

    elif data.startswith("iv_"):
        secs = int(data.split("_")[1])
        sess.interval = secs
        await query.answer(f"✅ Interval: {secs}s")
        await sess.refresh_panel(context.application)


# ─── UTILS ────────────────────────────────────────────────────────────────────

def _fix_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("open",        cmd_open))
    app.add_handler(CommandHandler("addurl",      cmd_addurl))
    app.add_handler(CommandHandler("removeurl",   cmd_removeurl))
    app.add_handler(CommandHandler("snap",        cmd_snap))
    app.add_handler(CommandHandler("gif",         cmd_gif))
    app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    app.add_handler(CommandHandler("stop",        cmd_stop))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CallbackQueryHandler(on_button))
    logger.warning("Bot chal raha hai — sabka alag session hai!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
