
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import random
import re
import secrets
import shutil
import signal
import string
import subprocess
import sys
import importlib
import tarfile
import tempfile
import threading
import time
import traceback
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────
# AUTO-INSTALLER — install missing deps on first run, no manual step
# IMPORTANT: this runs BEFORE any third-party import below so that
# fresh hosts (which have no Pillow / pyTelegramBotAPI / etc.) auto-
# repair themselves on first boot instead of crashing.
# ─────────────────────────────────────────────────────────────────

_REQUIRED_PKGS = [
    ("telebot",             "pyTelegramBotAPI"),
    ("requests",            "requests"),
    ("cryptography.fernet", "cryptography"),
    ("flask",               "flask"),
    ("apscheduler",         "APScheduler"),
    ("github",              "PyGithub"),
    ("psutil",              "psutil"),
    ("PIL",                 "Pillow"),
]


def _auto_install_missing() -> None:
    import importlib
    missing: List[str] = []
    for mod, pip_name in _REQUIRED_PKGS:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pip_name)
    if not missing:
        return
    print(f"[setup] installing missing packages: {', '.join(missing)}")
    # Try several install strategies — different hosts have different
    # restrictions (PEP 668 externally-managed, no root, sandboxed pip,
    # etc.). The first one that succeeds wins.
    strategies = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", *missing],
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet",
         "--break-system-packages", *missing],
        [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "--quiet", *missing],
        [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "--quiet",
         "--break-system-packages", *missing],
    ]
    last_err: Optional[Exception] = None
    for cmd in strategies:
        try:
            subprocess.run(cmd, check=True)
            print("[setup] install ok — continuing boot")
            return
        except Exception as e:
            last_err = e
            continue
    sys.exit(f"[x] auto-install failed after {len(strategies)} attempts: {last_err}. "
             f"Run manually: pip install {' '.join(missing)}")


_auto_install_missing()

# Now safe to import third-party modules.
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException
import requests
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify

# ── TELEGRAM BOT API 9.4 — BUTTON STYLE SUPPORT ──────────────────
# style="primary" = Blue | style="success" = Green | style="danger" = Red
# Graceful fallback: if Telegram ignores the field, buttons work normally.
class Btn(types.InlineKeyboardButton):
    """InlineKeyboardButton with optional style support (Bot API 9.4+)."""
    def __init__(self, *args, style: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        if style:
            self.style = style  # type: ignore[attr-defined]

    def to_dict(self):
        d = super().to_dict()
        if getattr(self, "style", ""):
            d["style"] = self.style
        return d
# ─────────────────────────────────────────────────────────────────

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter  # type: ignore
    _PIL_OK = True
except Exception:
    Image = ImageDraw = ImageFont = ImageFilter = None  # type: ignore
    _PIL_OK = False

try:
    import psutil
except ImportError:
    psutil = None  # graceful — used only for CPU/RAM telemetry


# ═════════════════════════════════════════════════════════════════
#  1. CONSTANTS & CONFIG
# ═════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent

DIRS: Dict[str, Path] = {
    "uploads":  BASE_DIR / "storage" / "uploads",
    "encfiles": BASE_DIR / "storage" / "encfiles",
    "data":     BASE_DIR / "storage" / "data",
    "logs":     BASE_DIR / "storage" / "logs",
    "backups":  BASE_DIR / "storage" / "backups",
    "sandbox":  BASE_DIR / "sandbox",
    "tickets":  BASE_DIR / "storage" / "tickets",
    "bot_data": BASE_DIR / "storage" / "bot_data",
    "photos":   BASE_DIR / "storage" / "photos",
}
for _p in DIRS.values():
    _p.mkdir(parents=True, exist_ok=True)

DB_FILE       = DIRS["data"] / "panel_db.json"
SETTINGS_FILE = DIRS["data"] / "panel_settings.json"
AUDIT_FILE    = DIRS["data"] / "audit.log"
KEYRING_FILE  = DIRS["data"] / "keyring.json"   # tiny local cache only

TOKEN = "8783127825:AAEgez2XciC649GXvAj-5bheP_UeyzFywpQ"
try:
    OWNER_ID = 8703382327  # yahan apna owner id likho
except (TypeError, ValueError):
    OWNER_ID = 0
if not TOKEN:
    sys.exit("[x] BOT_TOKEN env var not set.")
# OWNER_ID is optional. If not set, the very first user to send /start
# automatically becomes the panel owner and is persisted to settings.
# This lets you deploy with ONLY BOT_TOKEN and claim ownership in one tap.

ANNOUNCE_CHANNEL = os.environ.get("ANNOUNCE_CHANNEL", "").strip()
try:
    KEEPALIVE_PORT = int(os.environ.get("PORT", 47832))
except (TypeError, ValueError):
    KEEPALIVE_PORT = 47832

BRAND       = "ѕιмяαη нoѕтιηg ＲΒOT"
BRAND_VER   = "v2.1"
BRAND_TAG   = f"{BRAND} {BRAND_VER}"
SUPPORT_USR = "@nur7871"
UPDATE_CH   = "https://t.me/+MXtA9ufCgok3Yjc1"
FOOTER      = f"\n\n<blockquote>{BRAND_TAG}</blockquote>"

# ─── glyphs (smart contextual symbols + emojis for the UI) ──────
G = {
    # core status / decisions
    "ok":         "✓",        # ✔
    "no":         "\u2718",        # ✘
    "warn":       "\u26A0",        # ⚠
    "arrow":      "\u2192",        # →
    "bullet":     "\u2022",        # •
    "tri":        "\u25B8",        # ▸
    "diamond":    "\u25C6",        # ◆
    "star":       "\u2605",        # ★
    "spark":      "\u2726",        # ✦
    "back":       "↲",        # ◀
    "fwd":        "\u25B6",        # ▶
    "plus":       "\u2295",        # ⊕
    "minus":      "\u2296",        # ⊖
    "rec":        "\u25C9",        # ◉
    "rec_off":    "\u25CB",        # ○

    # dividers / borders
    "div":        "\u2501" * 16,   # ━━━…
    "div_eq":     "\u2550" * 16,   # ═══…
    "div_dash":   "\u2508" * 16,   # ┈┈┈…
    "block_on":   "\u25A0",        # ■
    "block_off":  "\u25A1",        # □
    "border_top": "\u2550" * 16,   # ═══…
    "border_mid": "\u2501" * 16,   # ━━━…
    "border_bot": "\u2550" * 16,   # ═══…

    # process state
    "play":        "‣",        # ▶
    "stop":        "\u25A0",        # ■
    "pause":       "\u2759\u2759",  # ❙❙
    "refresh":     "\u21BB",        # ↻
    "running":     "\u25B6",        # ▶
    "stopped":     "■",        # ■
    "restarting":  "\u21BB",        # ↻
    "stop_bot":    "■",        # ■

    # security / access
    "lock":     "\u25A3",       # ▣
    "unlock":   "\u25A2",       # ▢
    "secure":   "\u25C8",       # ◈
    "key":      "\u2756",       # ❖
    "shield":   "\u25C7",       # ◇
    "ban":      "\u2694",       # ⚔
    "trash":    "\u2716",       # ✖
    "eye":      "\u25C9",       # ◉

    # people
    "user":   "\u25C8",         # ◈
    "users":  "\u25CE",         # ◎
    "crown":  "\u2654",         # ♔

    # money / commerce
    "wallet":   "\u25C6",       # ◆
    "premium":  "⌬",       #⌬
    "lifetime": "\u2736",       # ✶
    "gift":     "\u2726",       # ✦
    "ticket":   "\u273F",       # ✿
    "trophy":   "\u2605",       # ★

    # data / analytics
    "graph":    "\u25AA",       # ▪
    "stats":    "\u25AA",       # ▪
    "chart_up": "\u25B2",       # ▲
    "plan":     "\u25A4",       # ▤

    # comms
    "broadcast": "⚑",      
    "chat":      "\u25AB",      # ▫

    # storage / files
    "folder":   "\u25B8",       # ▸
    "upload":   "\u25B4",       # ▴
    "download": "\u25BE",       # ▾
    "cloud":    "\u2601",       # ☁

    # tools / time / energy
    "settings": "⚙",       # ⚙
    "cog":      "\u2699",       # ⚙
    "bolt":     "\u26A1",       # ⚡
    "clock":    "\u23F1",       # ⏱
}

PLAN_LIMITS: Dict[str, Dict[str, Any]] = {
    "free":       {"name": "Free",       "max_bots": 2,   "ram": 128,  "auto_restart": False, "price": 0,    "days": 0},
    "starter":    {"name": "Starter",    "max_bots": 4,   "ram": 256,  "auto_restart": True,  "price": 99,   "days": 30},
    "basic":      {"name": "Basic",      "max_bots": 6,  "ram": 512,  "auto_restart": True,  "price": 199,  "days": 30},
    "pro":        {"name": "Pro",        "max_bots": 8,  "ram": 2048, "auto_restart": True,  "price": 499,  "days": 30},
    "enterprise": {"name": "Enterprise", "max_bots": 10,  "ram": 4096, "auto_restart": True,  "price": 999,  "days": 30},
    "lifetime":   {"name": "Lifetime",   "max_bots": 15, "ram": 8192, "auto_restart": True,  "price": 1999, "days": 36500},
}

PAYMENT_METHODS: Dict[str, Dict[str, Any]] = {
    "bkash":   {"name": "bKash",       "number": "01306633616",         "type": "Send Money",       "tag": "[B]"},
    "nagad":   {"name": "Nagad",       "number": "01306633616",         "type": "Send Money",       "tag": "[N]"},
    "rocket":  {"name": "Rocket",      "number": "01306633616",         "type": "Send Money",       "tag": "[R]"},
    "upay":    {"name": "Upay",        "number": "01306633616",         "type": "Send Money",       "tag": "[U]"},
    "binance": {"name": "Binance Pay", "number": "Binance ID 758637628","type": "USDT (BEP20/TRC20)","tag": "[BP]"},
    "bank":    {"name": "Bank",        "number": "Contact admin",       "type": "Bank Transfer",    "tag": "[BK]"},
}

SECRET_ENV_NAMES = {
    "BOT_TOKEN", "OWNER_ID", "ERROR_BOT_TOKEN",
    "MONGO_URL", "MONGO_URL_BACKUP",
    "GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_BRANCH", "GITHUB_KEY_REPO",
    "OWNER_IDS", "SESSION_SECRET",
    "DATABASE_URL", "PGDATABASE", "PGHOST", "PGPORT", "PGUSER", "PGPASSWORD",
    "REPLIT_DB_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
    "ANNOUNCE_CHANNEL",
}

ENTRY_NODE = ("index.js", "bot.js", "main.js", "app.js")
ENTRY_PY   = ("bot.py", "main.py", "app.py", "run.py")
LOG_RING   = 200
MAX_LOG_SEND = 50
MAX_UPLOAD_BYTES = 75 * 1024 * 1024  # 75 MB hard cap

# Per-menu photos (URLs). Replaceable; safe placeholders included.
# Each menu has its own banner image. We render these locally with
# Pillow at startup so we don't depend on any external image host
# (placehold.co was returning HTML/redirects on Telegram's fetcher,
# which produced "wrong type of the web page content" and made banners
# invisible). After the first upload Telegram gives us a file_id that
# we cache and reuse for all later sends.
_PHOTO_SPECS: Dict[str, Tuple[str, str, str]] = {
    # key:        (headline,        accent-hex, sub-text)
    "welcome":   ("Wᴇʟᴄᴏᴍᴇ",         "#0F172A", "Sɪᴍʀᴀɴ Hᴏꜱᴛɪɴɢ"),
    "main":      ("Mᴀɪɴ Mᴇɴᴜ",       "#1E1B4B", "Cʜᴏᴏꜱᴇ Aɴ Oᴘᴛɪᴏɴ"),
    "tunnel":    ("Pᴜʙʟɪᴄ Uʀʟ",      "#0E7490", "Cʟᴏᴜᴅꜰʟᴀʀᴇ Tᴜɴɴᴇʟ"),
    "bots":      ("Yᴏᴜʀ Bᴏᴛꜱ",       "#0E7490", "Mᴀɴᴀɢᴇ & Dᴇᴘʟᴏʏ"),
    "upload":    ("Uᴘʟᴏᴀᴅ & Dᴇᴘʟᴏʏ", "#4338CA", "Sᴇɴᴅ Yᴏᴜʀ Fɪʟᴇꜱ"),
    "plans":     ("Pʟᴀɴꜱ ",         "#B45309", "Pɪᴄᴋ A Tɪᴇʀ"),
    "buy":       ("Bᴜʏ Pʟᴀɴ",        "#065F46", "Cʜᴇᴄᴋᴏᴜᴛ"),
    "pay":       ("Pᴀʏᴍᴇɴᴛ",         "#0E7490", "Sᴇɴᴅ Pʀᴏᴏꜰ"),
    "profile":   ("Pʀᴏꜰɪʟᴇ",         "#1E3A8A", "Yᴏᴜʀ Aᴄᴄᴏᴜɴᴛ"),
    "wallet":    ("Wᴀʟʟᴇᴛ",          "#047857", "Tᴏᴘ-Uᴘ & Bᴀʟᴀɴᴄᴇ"),
    "referral":  ("Rᴇꜰᴇʀʀᴀʟ",        "#9333EA", "Iɴᴠɪᴛᴇ & Eᴀʀɴ"),
    "help":      ("Hᴇʟᴘ",            "#334155", "Hᴏᴡ Iᴛ Wᴏʀᴋꜱ"),
    "support":   ("Sᴜᴘᴘᴏʀᴛ",         "#0F766E", "Tᴀʟᴋ Tᴏ Uꜱ"),
    "ticket":    ("Tɪᴄᴋᴇᴛꜱ",         "#0F766E", "Oᴘᴇɴ A Tɪᴄᴋᴇᴛ"),
    "admin":     ("Aᴅᴍɪɴ Pᴀɴᴇʟ",     "#7C2D12", "Rᴇꜱᴛʀɪᴄᴛᴇᴅ Aʀᴇᴀ"),
    "stats":     ("Sᴛᴀᴛꜱ",           "#14532D", "Lɪᴠᴇ Nᴜᴍʙᴇʀꜱ"),
    "github":    ("Gɪᴛʜᴜʙ Bᴀᴄᴋᴜᴘ",   "#24292E", "Sʏɴᴄ & Rᴇꜱᴛᴏʀᴇ"),
    "security":  ("Sᴇᴄᴜʀɪᴛʏ",        "#991B1B", "Aᴜᴅɪᴛ & Kᴇʏꜱ"),
    "bot":       ("Bᴏᴛ Cᴏɴᴛʀᴏʟ",     "#1F2937", "Sᴛᴀʀᴛ • Sᴛᴏᴘ • Lᴏɢꜱ"),
    "logs":      ("Lɪᴠᴇ Lᴏɢꜱ",       "#0F172A", "Sᴛᴅᴏᴜᴛ / Sᴛᴅᴇʀʀ"),
    "trial":     ("Fʀᴇᴇ Tʀɪᴀʟ",      "#A21CAF", "Tʀʏ Pʀᴇᴍɪᴜᴍ Fʀᴇᴇ"),
    "coupon":    ("Cᴏᴜᴘᴏɴ",          "#B91C1C", "Rᴇᴅᴇᴇᴍ Cᴏᴅᴇ"),
    "gift":      ("Gɪꜰᴛ Pʟᴀɴ",       "#9D174D", "Sᴇɴᴅ Tᴏ A Fʀɪᴇɴᴅ"),
    "broadcast": ("Bʀᴏᴀᴅᴄᴀꜱᴛ",       "#1E40AF", "Rᴇᴀᴄʜ Aʟʟ Uꜱᴇʀꜱ"),
    "maint":     ("Mᴀɪɴᴛᴇɴᴀɴᴄᴇ",     "#451A03", "Rᴇᴀᴅ-Oɴʟʏ Mᴏᴅᴇ"),
}

# Filled in by _build_local_photos() at startup. Keys are the same
# as _PHOTO_SPECS; values are local file paths (str) that telebot can
# upload directly. After the first send_photo, _PHOTO_FILE_IDS caches
# the returned file_id so subsequent sends reuse it (zero re-upload).
PHOTOS: Dict[str, str] = {}
_PHOTO_FILE_IDS: Dict[str, str] = {}

_PHOTO_ICONS: Dict[str, str] = {
    "welcome":"✦","main":"◈","tunnel":"⬡","bots":"▸","upload":"▴",
    "plans":"★","buy":"◆","pay":"◉","profile":"◈","wallet":"◆",
    "referral":"✦","help":"◇","support":"▫","ticket":"✿","admin":"⚔",
    "stats":"▲","github":"⬡","security":"▣","bot":"▶","logs":"▸",
    "trial":"✶","coupon":"◉","gift":"✦","broadcast":"⚑","maint":"⚙",
}


def _build_local_photos() -> None:
    """Render every banner once into storage/photos/<key>.png. Safe to
    call repeatedly — existing files are reused. Falls back gracefully
    if Pillow or fonts are unavailable (PHOTOS gets a "" placeholder so
    show_menu's text-only branch still renders the menu instead of
    raising KeyError)."""
    # Guarantee all keys exist so PHOTOS["main"] etc. never KeyErrors.
    for k in _PHOTO_SPECS:
        PHOTOS.setdefault(k, "")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        print(f"[photos] Pillow unavailable: {e}", file=sys.stderr, flush=True)
        return
    out_dir = DIRS["photos"]
    out_dir.mkdir(parents=True, exist_ok=True)

    # Pick the first usable bold TTF.
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/run/current-system/sw/share/X11/fonts/DejaVuSans-Bold.ttf",
    ]
    font_path: Optional[str] = None
    for fp in font_candidates:
        if Path(fp).exists():
            font_path = fp
            break

    def _hex(c: str) -> Tuple[int, int, int]:
        c = c.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)

    for key, (text, color, sub) in _PHOTO_SPECS.items():
        # ── Custom admin-uploaded photo takes priority over generated one ──
        # replace_menu_photo() always writes custom_<key>.png as the
        # persistent marker, so this survives restarts and GitHub restores.
        custom_out = out_dir / f"custom_{key}.png"
        if custom_out.exists() and custom_out.stat().st_size > 1024:
            PHOTOS[key] = str(custom_out)
            continue
        out = out_dir / f"{key}.png"
        if out.exists() and out.stat().st_size > 1024:
            PHOTOS[key] = str(out)
            continue
        try:
            r, g, b = _hex(color)
            # Vertical gradient: lighten the top, darken the bottom.
            img = Image.new("RGB", (900, 460), (r, g, b))
            d = ImageDraw.Draw(img)
            for y in range(460):
                t = y / 459.0
                k = 1.0 - 0.55 * t  # darken toward bottom
                d.line(
                    [(0, y), (900, y)],
                    fill=(int(r * k), int(g * k), int(b * k)),
                )
            # Soft accent stripe along the bottom.
            d.rectangle([(0, 430), (900, 460)], fill=(255, 255, 255))
            d.rectangle([(0, 432), (900, 458)], fill=(r, g, b))

            big = (
                ImageFont.truetype(font_path, 78) if font_path
                else ImageFont.load_default()
            )
            small = (
                ImageFont.truetype(font_path, 28) if font_path
                else ImageFont.load_default()
            )

            def _wh(s: str, f) -> Tuple[int, int]:
                try:
                    bb = d.textbbox((0, 0), s, font=f)
                    return bb[2] - bb[0], bb[3] - bb[1]
                except Exception:
                    return d.textsize(s, font=f)  # type: ignore[attr-defined]

            tw, th = _wh(text, big)
            sw, sh = _wh(sub, small)
            cy = (460 - (th + sh + 18)) // 2
            # Drop-shadow for the headline.
            d.text(((900 - tw) // 2 + 3, cy + 3), text, fill=(0, 0, 0), font=big)
            d.text(((900 - tw) // 2, cy), text, fill=(255, 255, 255), font=big)
            d.text(((900 - sw) // 2, cy + th + 18), sub,
                   fill=(230, 230, 230), font=small)

            img.save(out, "PNG", optimize=True)
            PHOTOS[key] = str(out)
        except Exception as e:
            print(f"[photos] {key} failed: {e}", file=sys.stderr, flush=True)


_build_local_photos()


def _resolve_photo(ref: str):
    """Convert a PHOTOS[...] entry into something telebot's send_photo
    can accept. Order: cached file_id → local file handle → URL."""
    fid = _PHOTO_FILE_IDS.get(ref)
    if fid:
        return fid
    if isinstance(ref, str) and ref.startswith(("http://", "https://")):
        return ref
    try:
        return open(ref, "rb")
    except Exception:
        return ref


def _remember_file_id(ref: str, msg) -> None:
    """Stash the file_id Telegram returned so the next send is a single
    cheap reference instead of a full upload."""
    try:
        if msg and getattr(msg, "photo", None):
            _PHOTO_FILE_IDS[ref] = msg.photo[-1].file_id
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════
#  2. STYLED TEXT HELPERS  (small-caps + serif maps)
# ═════════════════════════════════════════════════════════════════

_SC_MAP = str.maketrans(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘQʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘQʀꜱᴛᴜᴠᴡxʏᴢ",
)


def sc(text: Any) -> str:
    """Render text in Unicode small-caps."""
    return str(text).translate(_SC_MAP)


def divider(width: int = 22, ch: str = "\u2501") -> str:
    return ch * width


def bullet(label: str, value: Any, glyph: str = G["bullet"]) -> str:
    return f"{glyph}  <b>{esc(label)}</b>: <code>{esc(value)}</code>"


# ═════════════════════════════════════════════════════════════════
#  3. JSON DB  (atomic writes, RLock-guarded)
# ═════════════════════════════════════════════════════════════════

_db_lock = threading.RLock()


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON atomically. Falls back to copy+rename if `replace` fails
    across filesystem boundaries (some Docker volume setups)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        tmp.replace(path)
    except OSError:
        # Cross-device or permission issue — fall back to copy+unlink
        try:
            shutil.copyfile(str(tmp), str(path))
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except TypeError:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # corrupt — keep a copy and reset
        try:
            path.replace(path.with_suffix(".corrupt"))
        except Exception:
            pass
        return default


# ── in-memory cache for db / settings (mtime-invalidated) ─────────
# JSON disk reads were happening on EVERY db_load() call (3-5 times per
# button click). With many users this turns the bot into molasses.
# We cache the parsed dict and only re-read from disk when the file's
# mtime changes (i.e. someone wrote to it). Cache entries are
# `(mtime, data)`. Writes bump mtime so other readers refresh.
_DB_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cached_load_ro(path: Path, default: Any) -> Any:
    """Return the cached parsed JSON at `path` WITHOUT a defensive
    copy. Caller MUST NOT mutate the result. Use for hot read-only
    paths (get_setting, is_admin, find_bot, …) — this avoids the
    enormous deepcopy cost on every callback."""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        mtime = 0.0
    cached = _DB_CACHE.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    d = _load_json(path, default)
    _DB_CACHE[key] = (mtime, d)
    return d


def _cached_load(path: Path, default: Any) -> Any:
    """Defensive variant: returns a deep copy so callers can mutate
    safely without poisoning the cache. ~5-10× faster than the old
    json round-trip."""
    return copy.deepcopy(_cached_load_ro(path, default))


def _cache_invalidate(path: Path) -> None:
    _DB_CACHE.pop(str(path), None)


# Default skeleton applied to a freshly-loaded `user_data.json`. Kept
# at module scope so we can install it once into the cached object
# (`db_load_ro`) and skip the per-call setdefault loop entirely.
_DB_DEFAULT_KEYS: Tuple[Tuple[str, Any], ...] = (
    ("users", {}),
    ("bots", {}),
    ("payments", []),
    ("admins", {}),
    ("audit", []),
    ("coupons", {}),
    ("tickets", {}),
    ("scheduled_broadcasts", []),
    ("notes", {}),
    ("rate_violations", {}),
)


def _ensure_db_defaults(d: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in _DB_DEFAULT_KEYS:
        if k not in d:
            d[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
    return d


def db_load() -> Dict[str, Any]:
    """Load a MUTABLE copy of the user database. Use when you intend
    to mutate and `db_save()` back. For pure reads, use db_load_ro()
    — much faster."""
    with _db_lock:
        d = _cached_load(DB_FILE, {})
    return _ensure_db_defaults(d)


def db_load_ro() -> Dict[str, Any]:
    """Read-only DB access. NEVER mutate the result — it's the cached
    object itself. Mutation will silently corrupt every other reader
    sharing the cache."""
    with _db_lock:
        d = _cached_load_ro(DB_FILE, {})
    return _ensure_db_defaults(d)


def db_save(d: Dict[str, Any]) -> None:
    with _db_lock:
        _atomic_write(DB_FILE, d)
        _cache_invalidate(DB_FILE)


def settings_load() -> Dict[str, Any]:
    with _db_lock:
        return _cached_load(SETTINGS_FILE, {})


def settings_load_ro() -> Dict[str, Any]:
    """Read-only fast path — DO NOT mutate."""
    with _db_lock:
        return _cached_load_ro(SETTINGS_FILE, {})


def settings_save(d: Dict[str, Any]) -> None:
    with _db_lock:
        _atomic_write(SETTINGS_FILE, d)
        _cache_invalidate(SETTINGS_FILE)


def get_setting(key: str, default: Any = None) -> Any:
    # Hot path. Use the no-copy reader because we only `.get()` —
    # we never mutate the dict.
    return settings_load_ro().get(key, default)


def set_setting(key: str, value: Any) -> None:
    s = settings_load()
    s[key] = value
    settings_save(s)


def cache_clear_all() -> None:
    """Drop every cached load so the next read re-parses from disk.
    Used by the Settings → Reload button after manual file edits."""
    with _db_lock:
        _DB_CACHE.clear()


# ═════════════════════════════════════════════════════════════════
#  4. UTILITY  HELPERS
# ═════════════════════════════════════════════════════════════════

def esc(s: Any = "") -> str:
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts_iso() -> str:
    return now_utc().isoformat()


def safe_name(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s or "").strip("_")
    return (s or "bot")[:48]


def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_dur(ms: int) -> str:
    if ms is None or ms < 0:
        return "—"
    s = ms // 1000
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts: List[str] = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def fmt_ts(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(iso)


def rmrf(p: str | Path) -> None:
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def rand_token(n: int = 8) -> str:
    return "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def safe_path_join(root: Path, *parts: str) -> Path:
    """Path-traversal safe join. Raises ValueError if escape detected."""
    final = (root / Path(*parts)).resolve()
    rootp = root.resolve()
    if rootp not in final.parents and final != rootp:
        raise ValueError("path traversal detected")
    return final


def is_owner(uid: int) -> bool:
    return int(uid) == OWNER_ID


def is_admin(uid: int) -> bool:
    if is_owner(uid):
        return True
    # Read-only fast path — no deepcopy.
    return str(uid) in db_load_ro().get("admins", {})


def admin_role(uid: int) -> str:
    if is_owner(uid):
        return "owner"
    return db_load_ro().get("admins", {}).get(str(uid), {}).get("role", "")


def admin_can(uid: int, action: str) -> bool:
    """
    Permission matrix.
      owner          → everything
      full-access    → everything except adding admins
      manage-users   → ban / give-plan / view users / approve payments / reply tickets
      view-only      → view stats only
    """
    role = admin_role(uid)
    if role == "owner":
        return True
    if role == "full-access":
        return action != "manage_admins"
    if role == "manage-users":
        return action in {
            "view_stats", "view_users", "find_user", "ban_user", "give_plan",
            "approve_payment", "reply_ticket", "broadcast_view", "user_note",
        }
    if role == "view-only":
        return action in {"view_stats", "view_users", "find_user"}
    return False


# ═════════════════════════════════════════════════════════════════
#  5. AUDIT LOG  (admin actions)
# ═════════════════════════════════════════════════════════════════

def audit(uid: int, action: str, detail: str = "") -> None:
    line = f"[{ts_iso()}] uid={uid} action={action} {detail}\n"
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    with _db_lock:
        d = db_load()
        d["audit"].append({"ts": ts_iso(), "uid": uid, "action": action, "detail": detail})
        d["audit"] = d["audit"][-500:]
        db_save(d)


# ═════════════════════════════════════════════════════════════════
#  6. ENCRYPTION   +   GITHUB-BACKED KEY RING
# ═════════════════════════════════════════════════════════════════
#
#  Every uploaded user file is encrypted with a unique Fernet key.
#  Keys live ONLY in a private GitHub key-repo (or a memory cache
#  if GitHub keyring is not configured — see warn() below).
#  Local disk only ever stores ciphertext.
# ═════════════════════════════════════════════════════════════════

class KeyRing:
    """Encryption key store. Tries GitHub first, then in-memory cache."""

    def __init__(self) -> None:
        self._mem: Dict[str, bytes] = {}
        self._lock = threading.Lock()

    # ── GitHub config ────────────────────────────────────────────
    @staticmethod
    def _gh_token() -> str:
        return (os.environ.get("GITHUB_TOKEN") or get_setting("github_token", "") or "").strip()

    @staticmethod
    def _gh_key_repo() -> str:
        # Prefer a separate repo for keys; falls back to backup repo
        return (
            os.environ.get("GITHUB_KEY_REPO")
            or get_setting("github_key_repo", "")
            or os.environ.get("GITHUB_REPO")
            or get_setting("github_repo", "")
            or ""
        ).strip()

    def gh_enabled(self) -> bool:
        return bool(self._gh_token() and "/" in self._gh_key_repo())

    def _gh_request(self, method: str, path: str, **kw) -> Optional[requests.Response]:
        if not self.gh_enabled():
            return None
        url = f"https://api.github.com/repos/{self._gh_key_repo()}/{path.lstrip('/')}"
        h = kw.pop("headers", {}) or {}
        h.setdefault("Authorization", f"token {self._gh_token()}")
        h.setdefault("Accept", "application/vnd.github+json")
        h.setdefault("User-Agent", "simran-hosting-rbot/2.1")
        try:
            return requests.request(method, url, headers=h, timeout=30, **kw)
        except Exception:
            return None

    # ── public API ───────────────────────────────────────────────
    def new_key(self) -> bytes:
        return Fernet.generate_key()

    def store(self, key_id: str, key: bytes, meta: Dict[str, Any]) -> bool:
        """Push key+meta to GitHub. Memory-cache as fallback only."""
        with self._lock:
            self._mem[key_id] = key

        body = {"key": key.decode(), "meta": meta, "ts": ts_iso()}
        payload = json.dumps(body, indent=2).encode()
        if not self.gh_enabled():
            # memory only — write a tiny encrypted local cache so a panel
            # restart does not lose access. The cache is encrypted with a
            # key derived from BOT_TOKEN+OWNER_ID, never plain text.
            self._cache_local(key_id, key)
            return True

        gh_path = f"keys/{key_id}.json"
        sha: Optional[str] = None
        r = self._gh_request("GET", f"contents/{gh_path}")
        if r is not None and r.status_code == 200:
            try:
                sha = r.json().get("sha")
            except Exception:
                pass
        put_body: Dict[str, Any] = {
            "message": f"key {key_id} stored {ts_iso()}",
            "content": base64.b64encode(payload).decode(),
        }
        if sha:
            put_body["sha"] = sha
        r2 = self._gh_request("PUT", f"contents/{gh_path}", json=put_body)
        ok = r2 is not None and r2.status_code in (200, 201)
        if not ok:
            # last-ditch local encrypted cache so we don't lose access
            self._cache_local(key_id, key)
        return ok

    def fetch(self, key_id: str) -> Optional[bytes]:
        with self._lock:
            cached = self._mem.get(key_id)
        if cached:
            return cached
        if self.gh_enabled():
            r = self._gh_request("GET", f"contents/keys/{key_id}.json")
            if r is not None and r.status_code == 200:
                try:
                    raw = base64.b64decode(r.json()["content"])
                    blob = json.loads(raw.decode())
                    key = blob["key"].encode()
                    with self._lock:
                        self._mem[key_id] = key
                    return key
                except Exception:
                    pass
        # local encrypted cache fallback
        return self._uncache_local(key_id)

    def wipe(self, key_id: str) -> None:
        with self._lock:
            self._mem.pop(key_id, None)

    def remove(self, key_id: str) -> None:
        """Delete key everywhere."""
        self.wipe(key_id)
        kp = DIRS["data"] / "keycache" / f"{key_id}.bin"
        try:
            if kp.exists():
                kp.unlink()
        except Exception:
            pass
        if self.gh_enabled():
            r = self._gh_request("GET", f"contents/keys/{key_id}.json")
            if r is not None and r.status_code == 200:
                try:
                    sha = r.json().get("sha")
                    if sha:
                        self._gh_request(
                            "DELETE",
                            f"contents/keys/{key_id}.json",
                            json={"message": f"remove {key_id}", "sha": sha},
                        )
                except Exception:
                    pass

    # ── fallback local encrypted cache ────────────────────────────
    def _local_master(self) -> bytes:
        material = f"{TOKEN}|{OWNER_ID}".encode()
        digest = hashlib.sha256(material).digest()
        return base64.urlsafe_b64encode(digest)

    def _cache_local(self, key_id: str, key: bytes) -> None:
        try:
            d = DIRS["data"] / "keycache"
            d.mkdir(parents=True, exist_ok=True)
            f = Fernet(self._local_master())
            (d / f"{key_id}.bin").write_bytes(f.encrypt(key))
        except Exception:
            pass

    def _uncache_local(self, key_id: str) -> Optional[bytes]:
        p = DIRS["data"] / "keycache" / f"{key_id}.bin"
        if not p.exists():
            return None
        try:
            f = Fernet(self._local_master())
            key = f.decrypt(p.read_bytes())
            with self._lock:
                self._mem[key_id] = key
            return key
        except Exception:
            return None


KEYRING = KeyRing()


def encrypt_file(plain: bytes) -> Tuple[str, bytes, bytes]:
    """
    Returns (key_id, key, ciphertext).
    Caller is responsible for storing key via KEYRING.store(key_id, key, meta).
    """
    key = KEYRING.new_key()
    f = Fernet(key)
    cipher = f.encrypt(plain)
    key_id = secrets.token_urlsafe(16)
    return key_id, key, cipher


def decrypt_with(key: bytes, cipher: bytes) -> bytes:
    return Fernet(key).decrypt(cipher)


def write_encrypted(path: Path, key: bytes, plain: bytes) -> None:
    f = Fernet(key)
    path.write_bytes(f.encrypt(plain))


def read_encrypted(path: Path, key: bytes) -> bytes:
    return Fernet(key).decrypt(path.read_bytes())


# ═════════════════════════════════════════════════════════════════
#  7. RATE LIMITER  +  SUSPICIOUS-ACTIVITY  WATCHDOG
# ═════════════════════════════════════════════════════════════════

class RateLimiter:
    def __init__(self, max_actions: int = 30, window_s: int = 60) -> None:
        self.max = max_actions
        self.window = window_s
        self._bucket: Dict[int, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, uid: int) -> bool:
        now = time.time()
        with self._lock:
            q = self._bucket[uid]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.max:
                return False
            q.append(now)
            return True

    def hits(self, uid: int) -> int:
        with self._lock:
            return len(self._bucket.get(uid, []))


RATE = RateLimiter(max_actions=40, window_s=60)
UPLOAD_RATE = RateLimiter(max_actions=8, window_s=300)


def maybe_auto_ban(uid: int, reason: str) -> None:
    """If a user repeatedly trips rate limits, auto-ban them and notify owner."""
    d = db_load()
    rv = d.get("rate_violations", {})
    rv[str(uid)] = int(rv.get(str(uid), 0)) + 1
    d["rate_violations"] = rv
    db_save(d)
    if rv[str(uid)] >= 5:
        u = d["users"].get(str(uid))
        if u and not u.get("banned"):
            u["banned"] = True
            u["ban_reason"] = f"auto: {reason}"
            db_save(d)
            audit(0, "auto_ban", f"uid={uid} reason={reason}")
            notify_owner(
                f"<b>{G['warn']} sᴜsᴘɪᴄɪᴏᴜs ᴀᴄᴛɪᴠɪᴛʏ</b>\n\n"
                f"User <code>{uid}</code> auto-banned ({esc(reason)})."
            )


# ═════════════════════════════════════════════════════════════════
#  8. BOT INSTANCE  +  KEEP-ALIVE  WEB SERVER
# ═════════════════════════════════════════════════════════════════

bot = telebot.TeleBot(TOKEN, parse_mode="HTML", threaded=True, num_threads=8)

# ───────────────────────────────────────────────────────────────────
# UI style wrapper — every outgoing message/caption is rendered as a
# bold blockquote so the panel feels uniform. Only applies when the
# parse mode is HTML (the default for this bot).
# ───────────────────────────────────────────────────────────────────
_QUOTE_OPEN  = "<blockquote><b>"
_QUOTE_CLOSE = "</b></blockquote>"

def _is_html_mode(pm) -> bool:
    if pm is None:
        return True  # bot default is HTML
    try:
        return str(pm).strip().lower() == "html"
    except Exception:
        return False

def _wrap_quote_bold(text):
    if text is None:
        return text
    s = str(text)
    if not s.strip():
        return s
    if s.startswith(_QUOTE_OPEN):
        return s
    return f"{_QUOTE_OPEN}{s}{_QUOTE_CLOSE}"

def _patch_bot_styling(b):
    orig_send         = b.send_message
    orig_reply        = b.reply_to
    orig_edit_text    = b.edit_message_text
    orig_edit_caption = b.edit_message_caption
    orig_send_photo   = b.send_photo
    orig_send_video   = b.send_video
    orig_send_doc     = b.send_document
    orig_send_anim    = getattr(b, "send_animation", None)

    def send_message(chat_id, text, *args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")):
            text = _wrap_quote_bold(text)
        return orig_send(chat_id, text, *args, **kwargs)

    def reply_to(message, text, *args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")):
            text = _wrap_quote_bold(text)
        return orig_reply(message, text, *args, **kwargs)

    def edit_message_text(text, *args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")):
            text = _wrap_quote_bold(text)
        return orig_edit_text(text, *args, **kwargs)

    def edit_message_caption(*args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")):
            if "caption" in kwargs:
                kwargs["caption"] = _wrap_quote_bold(kwargs.get("caption"))
        return orig_edit_caption(*args, **kwargs)

    def send_photo(chat_id, photo, *args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")) and kwargs.get("caption"):
            kwargs["caption"] = _wrap_quote_bold(kwargs["caption"])
        return orig_send_photo(chat_id, photo, *args, **kwargs)

    def send_video(chat_id, video, *args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")) and kwargs.get("caption"):
            kwargs["caption"] = _wrap_quote_bold(kwargs["caption"])
        return orig_send_video(chat_id, video, *args, **kwargs)

    def send_document(chat_id, document, *args, **kwargs):
        if _is_html_mode(kwargs.get("parse_mode")) and kwargs.get("caption"):
            kwargs["caption"] = _wrap_quote_bold(kwargs["caption"])
        return orig_send_doc(chat_id, document, *args, **kwargs)

    b.send_message         = send_message
    b.reply_to             = reply_to
    b.edit_message_text    = edit_message_text
    b.edit_message_caption = edit_message_caption
    b.send_photo           = send_photo
    b.send_video           = send_video
    b.send_document        = send_document
    if orig_send_anim is not None:
        def send_animation(chat_id, animation, *args, **kwargs):
            if _is_html_mode(kwargs.get("parse_mode")) and kwargs.get("caption"):
                kwargs["caption"] = _wrap_quote_bold(kwargs["caption"])
            return orig_send_anim(chat_id, animation, *args, **kwargs)
        b.send_animation = send_animation

_patch_bot_styling(bot)
USER_STATES: Dict[int, Dict[str, Any]] = {}
START_TS = int(time.time() * 1000)

# ── Flask keep-alive ─────────────────────────────────────────────
_ka = Flask(__name__)


@_ka.route("/")
def _ka_root() -> Any:  # noqa: D401
    return jsonify(
        {
            "ok": True,
            "brand": BRAND_TAG,
            "uptime_ms": int(time.time() * 1000) - START_TS,
            "running_bots": len(RUNNING) if "RUNNING" in globals() else 0,
        }
    )


@_ka.route("/health")
def _ka_health() -> Any:
    return jsonify({"status": "alive"})


def _start_keepalive() -> None:
    def _run() -> None:
        try:
            _ka.run(host="0.0.0.0", port=KEEPALIVE_PORT, debug=False, use_reloader=False)
        except Exception as e:
            print(f"[keepalive] {e}")
    threading.Thread(target=_run, daemon=True).start()


# ═════════════════════════════════════════════════════════════════
#  9. UI HELPERS  —  show_menu (edit, never spam) + keyboards
# ═════════════════════════════════════════════════════════════════

# ── PATCHED: ghost-delete fix ─────────────────────────────────────
# Logs send/edit failures to stderr instead of swallowing them.
def _log_err(where: str, exc: BaseException) -> None:
    try:
        print(f"[show_menu:{where}] {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
    except Exception:
        pass


# HTML-safe truncation: never cut a message in the middle of an open tag.
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)(\s[^>]*)?>")

def _html_safe_truncate(s: str, limit: int = 1024) -> str:
    if len(s) <= limit:
        return s
    cut = s[: limit - 1]
    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]
    stack: List[str] = []
    for m in _TAG_RE.finditer(cut):
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    closes = "".join(f"</{t}>" for t in reversed(stack))
    return cut + "…" + closes


def show_menu(
    chat_id: int,
    photo_url: str,
    caption: str,
    kb: types.InlineKeyboardMarkup,
    call: Optional[types.CallbackQuery] = None,
) -> None:
    """Send/edit a photo + caption + buttons. Tries to edit when from a
    callback. NEVER deletes the old message until the replacement has
    been confirmed sent — prevents 'ghost delete' bug."""
    cap = _html_safe_truncate(caption, 1024)

    # Any in-flight loading animation on this message is now stale —
    # we are about to overwrite the message with the real menu.
    if call and call.message:
        _cancel_loading(call.message.chat.id, call.message.message_id)

    # ── 1. Try in-place edits when the previous message is a photo ──
    if call and call.message and call.message.content_type == "photo":
        msg = call.message

        # 1a. Try to swap photo + caption together.
        # Use a cached file_id if we have one; otherwise resolve the
        # ref through `_resolve_photo` so local file paths are uploaded
        # as a real file handle instead of being mistaken for a URL
        # (which causes Telegram's "URL host is empty" error).
        cached_fid = _PHOTO_FILE_IDS.get(photo_url)
        media_ref = cached_fid if cached_fid else _resolve_photo(photo_url)
        try:
            bot.edit_message_media(
                media=types.InputMediaPhoto(media_ref, caption=cap, parse_mode="HTML"),
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=kb,
            )
            return
        except ApiTelegramException as e:
            if "message is not modified" in str(e).lower():
                return
            _log_err("edit_message_media", e)
        except Exception as e:
            _log_err("edit_message_media", e)
        finally:
            try:
                if hasattr(media_ref, "close"):
                    media_ref.close()
            except Exception:
                pass

        # 1b. Photo swap failed — keep existing photo, change only caption.
        # This is the safe path when Telegram can't fetch the new photo URL.
        try:
            bot.edit_message_caption(
                cap,
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=kb,
                parse_mode="HTML",
            )
            return
        except ApiTelegramException as e:
            if "message is not modified" in str(e).lower():
                return
            _log_err("edit_message_caption", e)
        except Exception as e:
            _log_err("edit_message_caption", e)

        # 1c. HTML parse blew up — retry caption WITHOUT parse_mode.
        try:
            plain = re.sub(r"<[^>]+>", "", cap)
            bot.edit_message_caption(
                plain,
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=kb,
            )
            return
        except Exception as e:
            _log_err("edit_message_caption(plain)", e)

    # ── 2. Send a brand-new message FIRST, then delete the old one. ──
    new_msg_id: Optional[int] = None

    try:
        m = bot.send_photo(chat_id, _resolve_photo(photo_url), caption=cap,
                           parse_mode="HTML", reply_markup=kb)
        new_msg_id = m.message_id
        _remember_file_id(photo_url, m)
    except Exception as e:
        _log_err("send_photo", e)

    if new_msg_id is None:
        try:
            m = bot.send_message(
                chat_id, cap, parse_mode="HTML", reply_markup=kb,
                disable_web_page_preview=True,
            )
            new_msg_id = m.message_id
        except Exception as e:
            _log_err("send_message(html)", e)

    if new_msg_id is None:
        try:
            plain = re.sub(r"<[^>]+>", "", cap)
            m = bot.send_message(
                chat_id, plain or "…", reply_markup=kb,
                disable_web_page_preview=True,
            )
            new_msg_id = m.message_id
        except Exception as e:
            _log_err("send_message(plain)", e)

    # Only NOW is it safe to remove the old message.
    if new_msg_id is not None and call and call.message:
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            _log_err("delete_message", e)


def show_text(
    chat_id: int, text: str, kb: Optional[types.InlineKeyboardMarkup] = None,
    call: Optional[types.CallbackQuery] = None,
) -> None:
    """Send/edit a plain-text message with the same delete-after-send
    safety as show_menu."""
    text = _html_safe_truncate(text, 4096)

    if call and call.message:
        _cancel_loading(call.message.chat.id, call.message.message_id)

    if call and call.message and call.message.content_type == "text":
        try:
            bot.edit_message_text(
                text, chat_id=chat_id, message_id=call.message.message_id,
                reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True,
            )
            return
        except ApiTelegramException as e:
            if "message is not modified" in str(e).lower():
                return
            _log_err("edit_message_text", e)
        except Exception as e:
            _log_err("edit_message_text", e)

        try:
            plain = re.sub(r"<[^>]+>", "", text)
            bot.edit_message_text(
                plain, chat_id=chat_id, message_id=call.message.message_id,
                reply_markup=kb, disable_web_page_preview=True,
            )
            return
        except Exception as e:
            _log_err("edit_message_text(plain)", e)

    new_msg_id: Optional[int] = None
    try:
        m = bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb,
                             disable_web_page_preview=True)
        new_msg_id = m.message_id
    except Exception as e:
        _log_err("send_message(html)", e)

    if new_msg_id is None:
        try:
            plain = re.sub(r"<[^>]+>", "", text)
            m = bot.send_message(chat_id, plain or "…", reply_markup=kb,
                                 disable_web_page_preview=True)
            new_msg_id = m.message_id
        except Exception as e:
            _log_err("send_message(plain)", e)

    if (new_msg_id is not None and call and call.message
            and call.message.content_type != "text"):
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception as e:
            _log_err("delete_message", e)


# ── keyboards ──────────────────────────────────────────────────
def main_menu_kb(admin: bool = False) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"  Mʏ Bᴏᴛꜱ",   callback_data="menu_bots",     style="primary"),
        Btn(f" Uᴘʟᴏᴀᴅ Bᴏᴛ",   callback_data="menu_upload",   style="primary"),
    )
    kb.add(
        Btn(f"Pʟᴀɴꜱ",        callback_data="menu_plans",    style="primary"),
        Btn(f" Bᴜʏ Pʟᴀɴ",    callback_data="menu_buy",      style="primary"),
    )
    kb.add(
        Btn(f"Rᴇꜰᴇʀʀᴀʟ",    callback_data="menu_referral", style="primary"),
        Btn(f"Pʀᴏꜰɪʟᴇ",      callback_data="menu_profile",  style="primary"),
    )
    kb.add(
        Btn(f" Wᴀʟʟᴇᴛ",     callback_data="menu_wallet",   style="primary"),
        Btn(f"Tɪᴄᴋᴇᴛꜱ",    callback_data="menu_tickets",  style="primary"),
    )
    kb.add(
        Btn(f" Fʀᴇᴇ Tʀɪᴀʟ",    callback_data="menu_trial",    style="primary"),
        Btn(f" Cᴏᴜᴘᴏɴ",        callback_data="menu_coupon",   style="primary"),
    )
    kb.add(
        Btn(f"Hᴇʟᴘ",          callback_data="menu_help",     style="primary"),
        Btn(f"Sᴜᴘᴘᴏʀᴛ", callback_data="menu_support",  style="primary"),
    )
    kb.add(
        Btn(f" Mʏ Sᴛᴀᴛꜱ",    callback_data="menu_stats",    style="primary"),
    )
    if admin:
        kb.add(Btn(f"Aᴅᴍɪɴ Pᴀɴᴇʟ", callback_data="menu_admin", style="danger"))

    return kb


def back_main_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup().add(
        Btn(f"{G['back']}  Mᴀɪɴ Mᴇɴᴜ", callback_data="menu_main", style="danger"))


def back_admin_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup().add(
        Btn(f"{G['back']}  Aᴅᴍɪɴ", callback_data="menu_admin", style="primary"))


def back_kb(target: str, label: str = "Back") -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup().add(
        Btn(f"{G['back']}  {sc(label)}", callback_data=target, style="danger"))


def plans_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for k, v in PLAN_LIMITS.items():
        price = "Free" if v["price"] == 0 else f"{v['price']}\u09F3"
        style = "success" if v["price"] == 0 else "primary"
        kb.add(Btn(
            f"{G['star']}  {sc(v['name'])}  {G['bullet']}  {price}",
            callback_data=f"plan_view_{k}", style=style))
    kb.add(Btn(f"{G['back']}  Mᴀɪɴ Mᴇɴᴜ", callback_data="menu_main", style="danger"))
    return kb


def payments_kb(plan: Optional[str] = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    suffix = f"_{plan}" if plan else ""
    for k, v in PAYMENT_METHODS.items():
        kb.add(Btn(f"{v['tag']}  {sc(v['name'])}", callback_data=f"pay_{k}{suffix}", style="success"))
    kb.add(Btn(f"{G['back']}  Pʟᴀɴꜱ", callback_data="menu_plans", style="primary"))
    return kb


def admin_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"{G['graph']}  Sᴛᴀᴛꜱ",         callback_data="adm_stats",    style="primary"),
        Btn(f"{G['users']}  Uꜱᴇʀꜱ",         callback_data="adm_users",    style="primary"),
    )
    kb.add(
        Btn(f"{G['diamond']}  Aʟʟ Bᴏᴛꜱ",    callback_data="adm_allbots",  style="primary"),
        Btn(f"{G['wallet']}  Pᴀʏᴍᴇɴᴛꜱ",     callback_data="adm_payments", style="success"),
    )
    kb.add(
        Btn(f"{G['broadcast']}  Bʀᴏᴀᴅᴄᴀꜱᴛ", callback_data="adm_broadcast",style="success"),
        Btn(f"{G['no']}  Bᴀɴ / Uɴʙᴀɴ",      callback_data="adm_ban",      style="danger"),
    )
    kb.add(
        Btn(f"{G['plus']}  Gɪᴠᴇ Pʟᴀɴ",      callback_data="adm_giveplan", style="success"),
        Btn(f"{G['ok']}  Aᴘᴘʀᴏᴠᴇ Pᴀʏ",      callback_data="adm_approve",  style="success"),
    )
    kb.add(
        Btn(f"{G['key']}  Cᴏᴜᴘᴏɴꜱ",         callback_data="adm_coupons",  style="primary"),
        Btn(f"{G['ticket']}  Tɪᴄᴋᴇᴛꜱ",      callback_data="adm_tickets",  style="primary"),
    )
    kb.add(
        Btn(f"{G['shield']}  Aᴅᴍɪɴꜱ",       callback_data="adm_admins",   style="primary"),
        Btn(f"{G['eye']}  Aᴜᴅɪᴛ Lᴏɢ",       callback_data="adm_audit",    style="primary"),
    )
    kb.add(
        Btn(f"{G['cog']}  Gɪᴛʜᴜʙ Bᴀᴄᴋᴜᴘ",   callback_data="adm_github",   style="primary"),
        Btn(f"{G['lock']}  Sᴇᴄᴜʀɪᴛʏ",       callback_data="adm_security", style="danger"),
    )
    kb.add(
        Btn(f"{G['warn']}  Mᴀɪɴᴛᴇɴᴀɴᴄᴇ",    callback_data="adm_maint",    style="danger"),
        Btn(f"{G['settings']}  Sᴇᴛᴛɪɴɢꜱ",   callback_data="adm_settings", style="primary"),
    )
    appr_on = bool(get_setting("approval_required", True))
    pend_n = len(get_setting("pending_uploads", {}) or {})
    kb.add(
        Btn(
            f"{G['ok'] if appr_on else G['no']}  Aᴘᴘʀᴏᴠᴀʟ: {'ON' if appr_on else 'OFF'}",
            callback_data="adm_approval_toggle",
            style="success" if appr_on else "danger"),
        Btn(
            f"{G['eye']}  Pᴇɴᴅɪɴɢ" + (f" ({pend_n})" if pend_n else ""),
            callback_data="adm_pending", style="primary"),
    )
    kb.add(
        Btn(f"{G['upload']}  Mᴇɴᴜ Pʜᴏᴛᴏꜱ",  callback_data="adm_photos",       style="primary"),
        Btn(f"{G['refresh']}  Fᴏʀᴄᴇ Bᴀᴄᴋᴜᴘ", callback_data="adm_force_backup", style="success"),
    )
    kb.add(Btn(f"{G['back']}  Mᴀɪɴ Mᴇɴᴜ", callback_data="menu_main", style="primary"))
    return kb


def github_kb(status: Dict[str, Any]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(Btn(f"{G['plus']}  Bᴀᴄᴋᴜᴘ Nᴏᴡ",      callback_data="gh_backup_now",  style="success"))
    kb.add(Btn(f"{G['refresh']}  Rᴇꜱᴛᴏʀᴇ Lᴀᴛᴇꜱᴛ", callback_data="gh_restore_now", style="primary"))
    kb.add(Btn(
        f"{G['rec'] if status['autoEnabled'] else G['rec_off']}  "
        f"Auto Backup: {'ON' if status['autoEnabled'] else 'OFF'}",
        callback_data="gh_toggle_auto",
        style="success" if status["autoEnabled"] else "danger"))
    kb.add(
        Btn(f"{G['key']}  {sc('Change Token' if status['tokenSet'] else 'Set Token')}",
            callback_data="gh_set_token", style="primary"),
        Btn(f"{G['diamond']}  {sc('Change Repo' if status['repoSet'] else 'Set Repo')}",
            callback_data="gh_set_repo",  style="primary"),
    )
    kb.add(
        Btn(f"{G['tri']}  Sᴇᴛ Bʀᴀɴᴄʜ",  callback_data="gh_set_branch",   style="primary"),
        Btn(f"{G['cog']}  Iɴᴛᴇʀᴠᴀʟ",    callback_data="gh_set_interval", style="primary"),
    )
    kb.add(Btn(f"{G['no']}  Cʟᴇᴀʀ Cᴏɴꜰɪɢ", callback_data="gh_clear",     style="danger"))
    kb.add(Btn(f"{G['refresh']}  Rᴇꜰʀᴇꜱʜ",   callback_data="adm_github",  style="primary"))
    kb.add(Btn(f"{G['back']}  Aᴅᴍɪɴ",       callback_data="menu_admin",  style="primary"))
    return kb


def bot_actions_kb(bot_id: str, running: bool, premium: bool = False) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            Btn(f"{G['stop']}  Sᴛᴏᴘ",       callback_data=f"bot_stop_{bot_id}",    style="danger"),
            Btn(f"{G['refresh']}  Rᴇꜱᴛᴀʀᴛ", callback_data=f"bot_restart_{bot_id}", style="success"),
        )
    else:
        kb.add(
            Btn(f"{G['play']}  Sᴛᴀʀᴛ",      callback_data=f"bot_start_{bot_id}",   style="success"),
            Btn(f"{G['refresh']}  Rᴇꜱᴛᴀʀᴛ", callback_data=f"bot_restart_{bot_id}", style="primary"),
        )
    kb.add(
        Btn(f"{G['bolt']}  Lɪᴠᴇ Lᴏɢꜱ", callback_data=f"bot_logs_{bot_id}", style="primary"),
        Btn(f"{G['eye']}  Iɴꜰᴏ",       callback_data=f"bot_info_{bot_id}", style="primary"),
    )
    kb.add(
        Btn(f"{G['settings']}  Eɴᴠ Vᴀʀꜱ", callback_data=f"bot_env_{bot_id}",  style="primary"),
        Btn(f"{G['cog']}  Cʀᴏɴ",          callback_data=f"bot_cron_{bot_id}", style="primary"),
    )
    kb.add(
        Btn(f"{G['download']}  Iɴꜱᴛᴀʟʟ Pᴋɢ", callback_data=f"bot_pip_{bot_id}",   style="primary"),
        Btn(f"{G['plus']}  Cʟᴏɴᴇ",           callback_data=f"bot_clone_{bot_id}", style="primary"),
    )
    if premium:
        is_open = bot_id in TUNNELS and TUNNELS[bot_id].get("proc") and TUNNELS[bot_id]["proc"].poll() is None
        label = "Stop Public URL" if is_open else "Public URL"
        glyph = G['no'] if is_open else G['cloud']
        kb.add(Btn(f"{glyph}  {label}", callback_data=f"bot_tunnel_{bot_id}",
                   style="danger" if is_open else "success"))
    kb.add(Btn(f"{G['arrow']}  Dᴏᴡɴʟᴏᴀᴅ", callback_data=f"bot_dl_{bot_id}", style="primary"))
    kb.add(Btn(f"{G['no']}  Dᴇʟᴇᴛᴇ",       callback_data=f"bot_delete_{bot_id}", style="danger"))
    kb.add(Btn(f"{G['back']}  Mʏ Bᴏᴛꜱ",    callback_data="menu_bots",            style="primary"))
    return kb


def confirm_kb(yes_cb: str, no_cb: str = "menu_main", yes_label: str = "Confirm",
               no_label: str = "Cancel") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"{G['ok']}  {sc(yes_label)}", callback_data=yes_cb, style="success"),
        Btn(f"{G['no']}  {sc(no_label)}",  callback_data=no_cb,  style="danger"),
    )
    return kb


# ═════════════════════════════════════════════════════════════════
# 10. SANDBOX RUNNER  (subprocess pool, secret-stripped env)
# ═════════════════════════════════════════════════════════════════

RUNNING: Dict[str, Dict[str, Any]] = {}    # bot_id -> {proc, kind, started, log, ...}
START_TIME: float = time.time()            # panel boot time, for uptime card
_LOCK_FH_KEEPALIVE: Any = None             # singleton-lock fd, kept alive for the process lifetime
_runner_lock = threading.Lock()


_SKIP_DIR_PARTS = {".deps", "node_modules", ".tmp_run", "__pycache__",
                   ".git", "venv", ".venv", "env"}


def _iter_user_files(bot_dir: Path, suffix: str) -> List[Path]:
    """Recursive scan that skips dependency / cache / VCS folders."""
    out: List[Path] = []
    for p in bot_dir.rglob(f"*{suffix}"):
        if any(part in _SKIP_DIR_PARTS for part in p.parts):
            continue
        out.append(p)
    return sorted(out, key=lambda x: (len(x.parts), str(x)))


def detect_entry(bot_dir: Path) -> Tuple[Optional[str], Optional[str]]:
    """Find the entry file. Returns (kind, relative_path_from_bot_dir).
    Searches the bot dir recursively — many users zip their bot inside
    a wrapper folder (e.g. `MyBot/bot.py`), and the old shallow `glob`
    missed those."""
    # 1. Standard entry names — check shallow first, then recursive
    for n in ENTRY_NODE:
        p = bot_dir / n
        if p.exists():
            return ("node", n)
    for n in ENTRY_PY:
        p = bot_dir / n
        if p.exists():
            return ("python", n)
    # Recursive: prefer files closer to the root (shorter path)
    for n in ENTRY_PY:
        for p in _iter_user_files(bot_dir, ".py"):
            if p.name == n:
                return ("python", str(p.relative_to(bot_dir)))
    for n in ENTRY_NODE:
        for p in _iter_user_files(bot_dir, ".js"):
            if p.name == n:
                return ("node", str(p.relative_to(bot_dir)))
    # 2. Any .py file (recursive, skipping deps)
    py_files = _iter_user_files(bot_dir, ".py")
    if py_files:
        return ("python", str(py_files[0].relative_to(bot_dir)))
    # 3. Any .js file (recursive)
    js_files = _iter_user_files(bot_dir, ".js")
    if js_files:
        return ("node", str(js_files[0].relative_to(bot_dir)))
    # 4. Inner .zip — extract once then re-scan
    zip_files = [p for p in bot_dir.rglob("*.zip")
                 if not any(part in _SKIP_DIR_PARTS for part in p.parts)]
    if zip_files:
        import zipfile as _zf
        try:
            with _zf.ZipFile(zip_files[0], "r") as z:
                z.extractall(bot_dir)
        except Exception:
            return (None, None)
        # recursive re-check
        py_files = _iter_user_files(bot_dir, ".py")
        if py_files:
            return ("python", str(py_files[0].relative_to(bot_dir)))
        js_files = _iter_user_files(bot_dir, ".js")
        if js_files:
            return ("node", str(js_files[0].relative_to(bot_dir)))
    return (None, None)


def safe_env(bot_dir: Path, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV_NAMES}
    env["HOME"]    = str(bot_dir)
    env["TMPDIR"]  = str(bot_dir / ".tmp_run")
    env["PATH"]    = "/usr/local/bin:/usr/bin:/bin"
    env.setdefault("NODE_ENV", "production")
    deps_dir = str(bot_dir / ".deps")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{deps_dir}:{existing_pp}" if existing_pp else deps_dir
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    Path(deps_dir).mkdir(parents=True, exist_ok=True)
    if extra:
        for k, v in extra.items():
            if k in SECRET_ENV_NAMES:
                continue
            env[str(k)] = str(v)
    return env


# ── module-name → PyPI package-name mapping ───────────────────────
# Many third-party libs are imported under a name that differs from
# their pip package. Without this mapping pip would 404 (e.g. `cv2` is
# really `opencv-python`). This is the most common reason "auto-install
# nahi chala" — and why uploaded bots crashed at import time.
_PYPI_ALIAS: Dict[str, str] = {
    "telebot":       "pyTelegramBotAPI",
    # `from telegram import Update` belongs to python-telegram-bot.
    # The bare `telegram` package on PyPI is an unrelated tiny shim
    # that does NOT expose Update / Bot — installing it by accident
    # is the most common source of the
    #   ImportError: cannot import name 'Update' from 'telegram'
    # crash. We map it to the real package and additionally validate
    # the installed copy in `_filter_third_party`.
    "telegram":      "python-telegram-bot",
    "telethon":      "Telethon",
    "pyrogram":      "Pyrogram",
    "pyromod":       "pyromod",
    "tgcrypto":      "TgCrypto",
    "PIL":           "Pillow",
    "cv2":           "opencv-python",
    "bs4":           "beautifulsoup4",
    "yaml":          "PyYAML",
    "dotenv":        "python-dotenv",
    "Crypto":        "pycryptodome",
    "Cryptodome":    "pycryptodomex",
    "dateutil":      "python-dateutil",
    "magic":         "python-magic",
    "skimage":       "scikit-image",
    "sklearn":       "scikit-learn",
    "google":        "google-api-python-client",
    "googletrans":   "googletrans",
    "OpenSSL":       "pyOpenSSL",
    "wx":            "wxPython",
    "psycopg2":      "psycopg2-binary",
    "MySQLdb":       "mysqlclient",
    "serial":        "pyserial",
    "win32api":      "pywin32",
    "ujson":         "ujson",
    "uvloop":        "uvloop",
    "discord":       "discord.py",
    "httpx":         "httpx",
    "aiohttp":       "aiohttp",
    "aiogram":       "aiogram",
    "fastapi":       "fastapi",
    "flask":         "flask",
    "starlette":     "starlette",
    "redis":         "redis",
    "pymongo":       "pymongo",
    "motor":         "motor",
    "psutil":        "psutil",
    "schedule":      "schedule",
    "apscheduler":   "APScheduler",
    "cryptography":  "cryptography",
    "github":        "PyGithub",
    "requests":      "requests",
    # extra safety net — pip name ≠ import name
    "nacl":          "PyNaCl",
    "git":           "GitPython",
    "jose":          "python-jose",
    "pkg_resources": "setuptools",
    "lxml":          "lxml",
    "chardet":       "chardet",
}


# Modules whose installed copy must expose specific symbols to be
# considered "really installed". Catches the wrong-package-on-PyPI trap
# (e.g. the `telegram` shim that lacks `Update`).
_VALIDATE_SYMBOLS: Dict[str, List[str]] = {
    "telegram": ["Update", "Bot"],
}


def _purge_bad_install(deps_dir: Path, mod_name: str) -> None:
    """Remove a wrong-package install (and its dist-info) from a bot's
    `.deps` so the next pip install can put the correct one in its
    place. Used when `_VALIDATE_SYMBOLS` says the cached package is
    not the one we actually need."""
    try:
        if not deps_dir.exists():
            return
        target = deps_dir / mod_name
        if target.exists():
            try:
                shutil.rmtree(str(target), ignore_errors=True)
            except Exception:
                pass
        for child in list(deps_dir.iterdir()):
            n = child.name.lower()
            if n.endswith((".dist-info", ".egg-info")) and \
                    n.startswith(mod_name.lower()):
                try:
                    shutil.rmtree(str(child), ignore_errors=True)
                except Exception:
                    try:
                        child.unlink()
                    except Exception:
                        pass
    except Exception as e:
        print(f"[purge_bad_install] {mod_name}: {e}", file=sys.stderr)


def _scan_imports(bot_dir: Path) -> List[str]:
    """Recursively scan every .py file for top-level module imports."""
    import ast as _ast
    found: set = set()
    for pyfile in bot_dir.rglob("*.py"):
        # Skip our own .deps cache so we don't mistake installed libs
        # for the bot's own imports.
        if ".deps" in pyfile.parts:
            continue
        try:
            tree = _ast.parse(pyfile.read_text(errors="ignore"))
        except Exception:
            continue
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for n in node.names:
                    if n.name:
                        found.add(n.name.split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import — local package
                if node.module:
                    found.add(node.module.split(".")[0])
    return sorted(found)


def _filter_third_party(modules: List[str], bot_dir: Path) -> List[str]:
    """Drop stdlib, local module names, and modules already importable
    from the bot's .deps cache. Returns only installable PyPI names that
    are still missing."""
    import importlib.util as _ilu
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    skip = stdlib | {"__future__", ""}
    # local modules (any .py file or package dir at the top level OR
    # any subdir — covers zipped wrappers like `MyBot/utils.py`)
    deps_dir = bot_dir / ".deps"
    for child in bot_dir.iterdir():
        if child == deps_dir:
            continue
        if child.suffix == ".py":
            skip.add(child.stem)
        elif child.is_dir() and (child / "__init__.py").exists():
            skip.add(child.name)
    # Make .deps importable for the find_spec check below so we don't
    # re-install something that's already cached locally.
    deps_str = str(deps_dir)
    deps_in_path = deps_str in sys.path
    if deps_dir.exists() and not deps_in_path:
        sys.path.insert(0, deps_str)

    out: List[str] = []
    seen: set = set()
    try:
        for m in modules:
            if not m or m in skip:
                continue
            # Already importable (stdlib was caught above; this catches
            # things like cv2 already installed in .deps/).
            try:
                if _ilu.find_spec(m) is not None:
                    # Even if importable, validate that the installed
                    # copy is the RIGHT package (not the wrong-name
                    # PyPI shim). If it isn't, nuke it so pip can
                    # reinstall the correct one below.
                    needed = _VALIDATE_SYMBOLS.get(m)
                    if needed:
                        try:
                            _real = importlib.import_module(m)
                            if all(hasattr(_real, s) for s in needed):
                                continue
                        except Exception:
                            pass
                        # Wrong package — purge and force a reinstall.
                        try:
                            del sys.modules[m]
                        except KeyError:
                            pass
                        _purge_bad_install(deps_dir, m)
                    else:
                        continue
            except (ImportError, ValueError):
                pass
            pip_name = _PYPI_ALIAS.get(m, m)
            if pip_name in seen:
                continue
            seen.add(pip_name)
            out.append(pip_name)
    finally:
        if deps_dir.exists() and not deps_in_path:
            try:
                sys.path.remove(deps_str)
            except ValueError:
                pass
    return out


def _pip_env(deps_dir: Path) -> Dict[str, str]:
    """Env for pip subprocesses: silence root warnings, keep installs
    confined to the bot's `.deps/` so we never trip on permissions.

    NOTE: We intentionally do NOT set PYTHONUSERBASE — that conflicts
    with `--target` and pip refuses to combine them ("Can not combine
    '--user' and '--target'"). We rely on `--target` alone."""
    env = {**os.environ,
           "PIP_DISABLE_PIP_VERSION_CHECK": "1",
           "PIP_NO_INPUT": "1",
           "PIP_ROOT_USER_ACTION": "ignore"}
    env.pop("PYTHONUSERBASE", None)
    env.pop("PIP_USER", None)
    return env


_PIP_BASE_FLAGS = ["--upgrade", "--no-input", "--no-warn-script-location",
                   "--disable-pip-version-check"]


def install_deps(bot_dir: Path, kind: str, log: List[str]) -> bool:
    try:
        if kind == "python":
            deps_dir = bot_dir / ".deps"
            deps_dir.mkdir(parents=True, exist_ok=True)
            req = bot_dir / "requirements.txt"
            pip_env = _pip_env(deps_dir)

            # 1) requirements.txt (if present)
            if req.exists():
                log.append(f"{G['div']} pip install (requirements.txt) {G['div']}")
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--target", str(deps_dir), *_PIP_BASE_FLAGS,
                     "-r", str(req)],
                    cwd=str(bot_dir), timeout=600, capture_output=True, text=True,
                    env=pip_env,
                )
                for line in (r.stdout or "").splitlines()[-15:]:
                    log.append(line)
                for line in (r.stderr or "").splitlines()[-10:]:
                    log.append(line)
                log.append(f"[{G['ok']}] requirements.txt done (rc={r.returncode})")

            # 2) AST-scan imports and install anything still missing.
            #    We always do this so a bot that adds a new `import foo`
            #    after upload doesn't crash on next start.
            try:
                modules = _scan_imports(bot_dir)
                third_party = _filter_third_party(modules, bot_dir)
                if third_party:
                    log.append(f"{G['div']} auto-install (scanned imports) {G['div']}")
                    log.append(f"📦 packages: {', '.join(third_party)}")
                    r2 = subprocess.run(
                        [sys.executable, "-m", "pip", "install",
                         "--target", str(deps_dir), *_PIP_BASE_FLAGS,
                         *third_party],
                        cwd=str(bot_dir), timeout=600, capture_output=True, text=True,
                        env=pip_env,
                    )
                    for line in (r2.stdout or "").splitlines()[-15:]:
                        log.append(line)
                    for line in (r2.stderr or "").splitlines()[-10:]:
                        log.append(line)
                    log.append(f"[{G['ok']}] auto-install done (rc={r2.returncode})")
            except Exception as e:
                log.append(f"[{G['warn']}] auto-install scan error: {e}")
            return True
        if kind == "node":
            pkg = bot_dir / "package.json"
            if not pkg.exists():
                return False
            if (bot_dir / "node_modules").exists():
                log.append(f"[{G['ok']}] node_modules cached, skipping npm install")
                return False
            log.append(f"{G['div']} npm install {G['div']}")
            r = subprocess.run(
                ["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=str(bot_dir), timeout=300, capture_output=True, text=True,
            )
            for line in (r.stdout or "").splitlines()[-15:]:
                log.append(line)
            for line in (r.stderr or "").splitlines()[-10:]:
                log.append(line)
            log.append(f"[{G['ok']}] npm done (rc={r.returncode})")
            return True
    except subprocess.TimeoutExpired:
        log.append(f"[{G['warn']}] dependency install timeout (>5min)")
    except FileNotFoundError as e:
        log.append(f"[{G['warn']}] tool not found: {e}")
    except Exception as e:
        log.append(f"[{G['warn']}] install error: {e}")
    return False


def _drain_proc(bot_id: str, proc: subprocess.Popen, log: List[str]) -> None:
    try:
        if not proc.stdout:
            return
        for line in iter(proc.stdout.readline, b""):
            try:
                txt = line.decode("utf-8", "replace").rstrip()
            except Exception:
                txt = repr(line)
            log.append(txt)
            if len(log) > LOG_RING:
                del log[: len(log) - LOG_RING]
    except Exception:
        pass
    # crash-watch — auto-restart if plan supports it
    try:
        rc = proc.wait()
        log.append(f"{G['div']} process exited rc={rc} {G['div']}")
        info = RUNNING.get(bot_id)
        was_manual = (info is None) or info.get("manual_stop", False)
        b_doc = find_bot(bot_id)

        # capture last error lines so the bot view can surface them
        if b_doc is not None:
            tail = [ln for ln in log[-15:] if ln and not ln.startswith(G["div"])]
            err_text = "\n".join(tail[-8:])[:1500]
            b_doc["last_error"] = err_text
            b_doc["last_exit_code"] = int(rc) if rc is not None else None
            b_doc["last_exit_at"] = ts_iso()
            if rc not in (0, None) and not was_manual:
                b_doc["status"] = "crashed"
            try:
                save_bot(b_doc)
            except Exception:
                pass

        if not info:
            return
        if not b_doc:
            return
        owner = db_load()["users"].get(str(b_doc["owner"]))
        plan = (owner or {}).get("plan", "free")
        if PLAN_LIMITS.get(plan, {}).get("auto_restart") and not was_manual:
            log.append(f"[{G['refresh']}] auto-restart in 3s...")
            time.sleep(3)
            start_child(b_doc)
    except Exception:
        pass


def start_child(b: Dict[str, Any]) -> Dict[str, Any]:
    bid = b["_id"]
    # Approval gate — never start a bot still waiting for admin review.
    if (b or {}).get("approval_status") == "pending":
        return {"ok": False, "error": "Bot is waiting for admin approval."}
    if (b or {}).get("approval_status") == "rejected":
        return {"ok": False, "error": "Bot was rejected by admin."}
    with _runner_lock:
        existing = RUNNING.get(bid)
        if existing and existing["proc"].poll() is None:
            return {"ok": False, "error": "Already running."}
    bot_dir = Path(b["dir"])
    if not bot_dir.exists():
        return {"ok": False, "error": "Bot folder missing."}

    # decrypt encrypted source files into bot_dir at run time
    try:
        materialize_bot_files(b)
    except Exception as e:
        return {"ok": False, "error": f"decrypt failed: {e}"}

    kind, entry = detect_entry(bot_dir)
    if not kind:
        return {"ok": False, "error": "No entry file (index.js / bot.py)."}

    log: List[str] = [f"{G['div_eq']} START {ts_iso()} {G['div_eq']}"]
    install_deps(bot_dir, kind, log)
    cmd = ["node", entry] if kind == "node" else [sys.executable, "-u", entry]

    extra_env = b.get("env") or {}
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(bot_dir), env=safe_env(bot_dir, extra_env),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name == "posix" else None,
        )
    except Exception as e:
        return {"ok": False, "error": f"spawn: {e}"}

    info = {
        "proc": proc, "kind": kind, "started": time.time() * 1000,
        "log": log, "dir": str(bot_dir), "name": b["name"],
        "owner": b["owner"], "manual_stop": False,
    }
    with _runner_lock:
        RUNNING[bid] = info
    threading.Thread(target=_drain_proc, args=(bid, proc, log), daemon=True).start()

    # update doc — clear any prior crash so bot view shows clean state
    b["status"] = "running"
    b["last_started"] = ts_iso()
    b["last_error"] = ""
    b["last_exit_code"] = None
    save_bot(b)
    return {"ok": True, "pid": proc.pid, "kind": kind}


def stop_child(bot_id: str, manual: bool = True) -> Dict[str, Any]:
    with _runner_lock:
        info = RUNNING.get(bot_id)
    if not info:
        # Even if we don't have it tracked, make sure DB says stopped
        b = find_bot(bot_id)
        if b and b.get("status") != "stopped":
            b["status"] = "stopped"
            save_bot(b)
        return {"ok": True}
    info["manual_stop"] = manual
    proc = info["proc"]

    # Collect every descendant PID *before* we start signalling so a
    # double-fork bot can't escape us.
    child_pids: List[int] = []
    if psutil is not None:
        try:
            parent = psutil.Process(proc.pid)
            for ch in parent.children(recursive=True):
                child_pids.append(ch.pid)
        except Exception:
            pass

    def _kill_pid(pid: int, sig: int) -> None:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        except Exception:
            pass

    try:
        # 1) polite SIGTERM to the whole process group
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            for pid in child_pids:
                _kill_pid(pid, signal.SIGTERM)
        else:
            proc.terminate()

        # 2) wait briefly — most well-behaved bots exit here
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # 3) hard SIGKILL the group + every descendant we noted
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                for pid in child_pids:
                    _kill_pid(pid, signal.SIGKILL)
                # one more sweep for any new grand-children spawned
                # between our snapshot and the kill signal
                if psutil is not None:
                    try:
                        for ch in psutil.Process(proc.pid).children(recursive=True):
                            _kill_pid(ch.pid, signal.SIGKILL)
                    except Exception:
                        pass
            else:
                proc.kill()
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
    except ProcessLookupError:
        pass
    except Exception as e:
        # Even on partial failure, drop our handle so the user can
        # try again instead of being stuck "running".
        with _runner_lock:
            RUNNING.pop(bot_id, None)
        b = find_bot(bot_id)
        if b:
            b["status"] = "stopped"
            save_bot(b)
        return {"ok": False, "error": str(e)}

    # Tear down any cloudflared tunnel we opened for this bot
    try:
        _stop_tunnel(bot_id)
    except Exception:
        pass

    with _runner_lock:
        RUNNING.pop(bot_id, None)
    b = find_bot(bot_id)
    if b:
        b["status"] = "stopped"
        save_bot(b)
    return {"ok": True}


# ────────────────────────────── Cloudflared "trycloudflare" tunnels ─
# Premium-only feature: gives a user a public URL like
# https://random-words-1234.trycloudflare.com that proxies straight to
# their bot's local port. We download the official cloudflared binary on
# first use and cache it under ~/.cache/cloudflared so this works on any
# host without the user needing root.

TUNNELS: Dict[str, Dict[str, Any]] = {}     # bot_id -> {proc, port, url, started}
_tunnel_lock = threading.Lock()

CLOUDFLARED_CACHE = Path.home() / ".cache" / "cloudflared"
CLOUDFLARED_BIN   = CLOUDFLARED_CACHE / "cloudflared"

_CF_DOWNLOAD = {
    ("linux",  "x86_64"):  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    ("linux",  "aarch64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64",
    ("linux",  "armv7l"):  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm",
    ("darwin", "x86_64"):  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
    ("darwin", "arm64"):   "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz",
}


def _ensure_cloudflared() -> Optional[Path]:
    """Return path to a working cloudflared binary, downloading once."""
    # Already cached?
    if CLOUDFLARED_BIN.exists() and os.access(CLOUDFLARED_BIN, os.X_OK):
        return CLOUDFLARED_BIN
    # Already on PATH?
    on_path = shutil.which("cloudflared")
    if on_path:
        return Path(on_path)
    # Download a fresh copy
    try:
        import platform
        sysname = platform.system().lower()
        machine = platform.machine().lower()
        url = _CF_DOWNLOAD.get((sysname, machine))
        if not url:
            return None
        CLOUDFLARED_CACHE.mkdir(parents=True, exist_ok=True)
        tmp = CLOUDFLARED_BIN.with_suffix(".part")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        tmp.chmod(0o755)
        tmp.rename(CLOUDFLARED_BIN)
        return CLOUDFLARED_BIN
    except Exception:
        return None


def _port_in_use(port: int) -> bool:
    """True if *something* is already listening on this TCP port."""
    import socket as _s
    for fam, typ, addr in (
        (_s.AF_INET,  _s.SOCK_STREAM, ("127.0.0.1", port)),
        (_s.AF_INET6, _s.SOCK_STREAM, ("::1",       port)),
    ):
        try:
            with _s.socket(fam, typ) as sk:
                sk.settimeout(0.4)
                if sk.connect_ex(addr) == 0:
                    return True
        except Exception:
            continue
    return False


_TRYCLOUDFLARE_RE = re.compile(r"https?://[a-z0-9-]+\.trycloudflare\.com", re.I)


def _start_tunnel(bot_id: str, port: int) -> Dict[str, Any]:
    """Spin up `cloudflared tunnel --url http://localhost:<port>` and
    capture the public trycloudflare URL from its stderr."""
    if not (1 <= port <= 65535):
        return {"ok": False, "error": "Port must be between 1 and 65535"}

    with _tunnel_lock:
        existing = TUNNELS.get(bot_id)
        if existing and existing.get("proc") and existing["proc"].poll() is None:
            return {"ok": False, "error": "Tunnel already running for this bot. Stop it first."}

    if not _port_in_use(port):
        return {"ok": False,
                "error": f"Nothing is listening on port {port}. "
                         f"Start your bot's web server on that port first, "
                         f"or pick another port."}

    bin_path = _ensure_cloudflared()
    if not bin_path:
        return {"ok": False,
                "error": "Could not download cloudflared binary on this host. "
                         "Please install cloudflared manually."}

    log_buf: Deque[str] = deque(maxlen=200)
    try:
        proc = subprocess.Popen(
            [str(bin_path), "tunnel", "--no-autoupdate",
             "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid if os.name == "posix" else None,
        )
    except Exception as e:
        return {"ok": False, "error": f"Failed to launch cloudflared: {e}"}

    rec: Dict[str, Any] = {
        "proc":    proc,
        "port":    port,
        "url":     None,
        "started": int(time.time()),
        "log":     log_buf,
    }
    with _tunnel_lock:
        TUNNELS[bot_id] = rec

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            log_buf.append(line)
            if rec["url"] is None:
                m = _TRYCLOUDFLARE_RE.search(line)
                if m:
                    rec["url"] = m.group(0)

    threading.Thread(target=_drain, daemon=True, name=f"cf-{bot_id}").start()

    # Wait up to ~15s for the URL to appear
    deadline = time.time() + 15
    while time.time() < deadline and rec["url"] is None and proc.poll() is None:
        time.sleep(0.3)

    if proc.poll() is not None and rec["url"] is None:
        # process died early — usually port issue or network
        tail = "\n".join(list(log_buf)[-6:]) or "(no output)"
        with _tunnel_lock:
            TUNNELS.pop(bot_id, None)
        return {"ok": False, "error": f"cloudflared exited early.\n{tail}"}

    if rec["url"] is None:
        # No URL within 15s and process still alive — kill it so we don't
        # leave an orphan cloudflared process running forever.
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        except Exception:
            pass
        with _tunnel_lock:
            TUNNELS.pop(bot_id, None)
        tail = "\n".join(list(log_buf)[-6:]) or "(no output)"
        return {"ok": False,
                "error": f"Tunnel timed out — no URL after 15s.\n{tail}"}

    return {"ok": True, "url": rec["url"], "port": port}


def _stop_tunnel(bot_id: str) -> bool:
    with _tunnel_lock:
        rec = TUNNELS.pop(bot_id, None)
    if not rec:
        return False
    proc = rec.get("proc")
    if not proc:
        return True
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                pass
    except Exception:
        pass
    return True


def restart_child(b: Dict[str, Any]) -> Dict[str, Any]:
    stop_child(b["_id"], manual=False)
    time.sleep(1)
    return start_child(b)


def child_status(bot_id: str, b_doc: Dict[str, Any]) -> Dict[str, Any]:
    info = RUNNING.get(bot_id)
    running = bool(info and info["proc"].poll() is None)
    bot_dir = Path(b_doc.get("dir") or "")
    kind, _ = detect_entry(bot_dir) if bot_dir.exists() else (None, None)
    sz = 0
    try:
        for root, _, files in os.walk(bot_dir):
            for f in files:
                try:
                    sz += (Path(root) / f).stat().st_size
                except OSError:
                    pass
    except Exception:
        pass
    cpu = mem = 0.0
    if running and psutil is not None:
        try:
            p = psutil.Process(info["proc"].pid)
            cpu = p.cpu_percent(interval=0.05)
            mem = p.memory_info().rss
        except Exception:
            pass
    return {
        "running":   running,
        "pid":       info["proc"].pid if running else None,
        "kind":      (info["kind"] if info else kind) or "—",
        "uptimeMs":  int(time.time() * 1000 - info["started"]) if running else 0,
        "sizeBytes": sz,
        "logs":      info["log"] if info else [],
        "cpuPct":    cpu,
        "memBytes":  mem,
        "sandboxed": True,
    }


# ════════════════════════════════════════════════
# 11. ENCRYPTED  BOT  STORAGE
# ═════════════════════════════════════════════════════

def store_uploaded_file(uploader: types.User, filename: str, plain: bytes) -> Dict[str, Any]:
    """
    Encrypt + persist an uploaded file. Returns metadata describing
    where the encrypted blob lives and which key_id unlocks it.
    """
    safe = safe_name(filename)
    key_id, key, cipher = encrypt_file(plain)
    rel = f"{uploader.id}/{int(time.time())}_{safe}.enc"
    out = DIRS["encfiles"] / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cipher)

    meta = {
        "filename": filename,
        "uploader_id": uploader.id,
        "uploader_username": uploader.username or "",
        "size": len(plain),
        "uploaded": ts_iso(),
        "stored_at": str(out),
    }
    KEYRING.store(key_id, key, meta)

    # notify_owner HATA DIYA — ab upload handler mein sirf ek summary msg aayega
    return {"key_id": key_id, "path": str(out), "size": len(plain)}


def materialize_bot_files(b: Dict[str, Any]) -> None:
    """Decrypt every encrypted file for this bot into its sandbox dir."""
    bot_dir = Path(b["dir"])
    bot_dir.mkdir(parents=True, exist_ok=True)
    files = b.get("enc_files") or []
    for f in files:
        key = KEYRING.fetch(f["key_id"])
        if not key:
            raise RuntimeError(f"missing key {f['key_id']}")
        try:
            plain = read_encrypted(Path(f["enc_path"]), key)
        except InvalidToken:
            raise RuntimeError(f"key mismatch for {f.get('filename')}")
        # write into bot_dir
        rel = f.get("rel_path") or f["filename"]
        rel = rel.lstrip("/")
        try:
            tgt = safe_path_join(bot_dir, rel)
        except ValueError:
            continue
        tgt.parent.mkdir(parents=True, exist_ok=True)
        tgt.write_bytes(plain)
        # wipe key from memory after using it
        plain = b""
    # KEYRING memory wipe (re-fetched on next run)
    for f in files:
        KEYRING.wipe(f["key_id"])


def encrypted_dump_for_download(b: Dict[str, Any]) -> Optional[Path]:
    """Build a zip of the *encrypted* blobs for this bot. Useless without keys."""
    files = b.get("enc_files") or []
    if not files:
        return None
    out = Path(tempfile.gettempdir()) / f"enc_{b['_id']}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            p = Path(f["enc_path"])
            if p.exists():
                z.write(p, arcname=f.get("rel_path") or f["filename"])
        z.writestr(
            "_README.txt",
            f"These files are encrypted with Fernet/AES-128.\n"
            f"They cannot be read without the per-file key, which is\n"
            f"stored in a private GitHub repository owned by {BRAND_TAG}.\n",
        )
    return out



# 12. GITHUB  BACKUP / RESTORE  (panel state)

GH = {
    "token": "", "repo": "", "branch": "main",
    "intervalMin": 360,
    "lastBackup": None, "lastError": None,
    "inProgress": False, "autoEnabled": True,
}


def gh_load_config() -> None:
    GH["token"]  = os.environ.get("GITHUB_TOKEN")  or get_setting("github_token", "")  or ""
    GH["repo"]   = os.environ.get("GITHUB_REPO")   or get_setting("github_repo", "")   or ""
    GH["branch"] = os.environ.get("GITHUB_BRANCH") or get_setting("github_branch", "main") or "main"
    try:
        ivl = int(os.environ.get("GITHUB_AUTO_INTERVAL_MIN") or get_setting("github_interval_min", 360))
    except Exception:
        ivl = 360
    GH["intervalMin"] = ivl if ivl > 0 else 360


def gh_set_config(patch: Dict[str, Any]) -> None:
    keymap = {"token": "github_token", "repo": "github_repo",
              "branch": "github_branch", "intervalMin": "github_interval_min"}
    for k, v in patch.items():
        if k not in keymap:
            continue
        if k == "intervalMin":
            try:
                v = int(v)
            except Exception:
                v = 360
        GH[k] = v
        set_setting(keymap[k], v)


def gh_enabled() -> bool:
    return bool(GH["token"] and GH["repo"] and "/" in GH["repo"])


def gh_status() -> Dict[str, Any]:
    return {
        "enabled":     gh_enabled(),
        "repo":        GH["repo"], "branch": GH["branch"],
        "intervalMin": GH["intervalMin"],
        "autoEnabled": GH["autoEnabled"],
        "lastBackup":  GH["lastBackup"],
        "lastError":   GH["lastError"],
        "inProgress":  GH["inProgress"],
        "tokenSet":    bool(GH["token"]),
        "repoSet":     bool(GH["repo"]),
    }


def _gh(method: str, url: str, **kw) -> requests.Response:
    h = kw.pop("headers", {}) or {}
    h.setdefault("Authorization", f"token {GH['token']}")
    h.setdefault("Accept", "application/vnd.github+json")
    h.setdefault("User-Agent", "simran-hosting-rbot/2.1")
    return requests.request(method, url, headers=h, timeout=60, **kw)


def _gh_repo_url(p: str = "") -> str:
    return f"https://api.github.com/repos/{GH['repo']}/{p.lstrip('/')}"


def _gh_ensure_branch() -> bool:
    r = _gh("GET", _gh_repo_url(f"branches/{GH['branch']}"))
    if r.status_code == 200:
        return True
    if r.status_code != 404:
        return False
    info = _gh("GET", _gh_repo_url())
    if info.status_code != 200:
        return False
    default = info.json().get("default_branch", "main")
    ref = _gh("GET", _gh_repo_url(f"git/ref/heads/{default}"))
    if ref.status_code != 200:
        return False
    sha = ref.json()["object"]["sha"]
    _gh("POST", _gh_repo_url("git/refs"),
        json={"ref": f"refs/heads/{GH['branch']}", "sha": sha})
    return True


def _gh_put_file(path: str, content: bytes, message: str) -> bool:
    sha: Optional[str] = None
    g = _gh("GET", _gh_repo_url(f"contents/{path}"), params={"ref": GH["branch"]})
    if g.status_code == 200:
        sha = g.json().get("sha")
    elif g.status_code != 404:
        return False
    body: Dict[str, Any] = {
        "message": message, "branch": GH["branch"],
        "content": base64.b64encode(content).decode(),
    }
    if sha:
        body["sha"] = sha
    r = _gh("PUT", _gh_repo_url(f"contents/{path}"), json=body)
    return r.status_code in (200, 201)


def _make_tarball() -> Path:
    tmp = Path(tempfile.gettempdir()) / f"panel-backup-{int(time.time())}.tar.gz"
    excludes = ("node_modules", ".deps", ".tmp_run", "__pycache__")

    def _filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
        if any(x in ti.name.split("/") for x in excludes):
            return None
        if ti.name.endswith(".log"):
            return None
        return ti

    with tarfile.open(tmp, "w:gz") as tf:
        # Backup storage/ — users, bots DB, encrypted files, keys, tickets
        storage_dir = BASE_DIR / "storage"
        if storage_dir.exists():
            tf.add(str(storage_dir), arcname="storage", filter=_filter)
        # Backup sandbox/ — bot env vars, cron config (not .deps to save space)
        sandbox_dir = BASE_DIR / "sandbox"
        if sandbox_dir.exists():
            tf.add(str(sandbox_dir), arcname="sandbox", filter=_filter)
    return tmp


def gh_backup_now() -> Dict[str, Any]:
    if not gh_enabled():
        return {"ok": False, "error": "Not configured."}
    if GH["inProgress"]:
        return {"ok": False, "error": "Backup already running."}
    GH["inProgress"] = True
    tar: Optional[Path] = None
    try:
        if not _gh_ensure_branch():
            raise RuntimeError(f"Branch {GH['branch']} unavailable")
        tar = _make_tarball()
        buf = tar.read_bytes()
        size_mb = len(buf) / 1024 / 1024
        if size_mb > 95:
            raise RuntimeError(f"Backup {size_mb:.1f} MB > 95 MB GitHub limit")
        ts = ts_iso().replace(":", "-").replace(".", "-")
        ok1 = _gh_put_file("backups/latest.tar.gz", buf, f"chore(panel): backup {ts}")
        ok2 = _gh_put_file(f"backups/{ts}.tar.gz", buf, f"chore(panel): snapshot {ts}")
        manifest = json.dumps({"lastBackup": ts, "sizeBytes": len(buf)}, indent=2)
        _gh_put_file("backups/manifest.json", manifest.encode(), f"chore(panel): manifest {ts}")
        if not (ok1 and ok2):
            raise RuntimeError("upload failed")
        GH["lastBackup"] = ts
        GH["lastError"] = None
        return {"ok": True, "sizeMB": f"{size_mb:.2f}", "ts": ts}
    except Exception as e:
        GH["lastError"] = str(e)
        return {"ok": False, "error": str(e)}
    finally:
        if tar and tar.exists():
            try:
                tar.unlink()
            except Exception:
                pass
        GH["inProgress"] = False


def gh_restore_now(overwrite: bool = True) -> Dict[str, Any]:
    if not gh_enabled():
        return {"ok": False, "error": "Not configured."}
    r = _gh("GET", _gh_repo_url("contents/backups/latest.tar.gz"),
            params={"ref": GH["branch"]})
    if r.status_code == 404:
        return {"ok": False, "error": "No backup found yet."}
    if r.status_code != 200:
        return {"ok": False, "error": f"GitHub HTTP {r.status_code}"}
    buf = base64.b64decode(r.json()["content"])
    tmp = Path(tempfile.gettempdir()) / f"panel-restore-{int(time.time())}.tar.gz"
    tmp.write_bytes(buf)
    try:
        if overwrite:
            # Wipe both storage and sandbox before restoring
            for folder in ("storage", "sandbox"):
                d = BASE_DIR / folder
                if d.exists():
                    for sub in d.iterdir():
                        rmrf(sub)
        with tarfile.open(tmp, "r:gz") as tf:
            tf.extractall(str(BASE_DIR))
        # Re-create required dirs in case they were missing in backup
        for _p in DIRS.values():
            _p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "sizeBytes": len(buf)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def gh_auto_loop() -> None:
    while True:
        try:
            time.sleep(max(60, GH["intervalMin"] * 60))
            if gh_enabled() and GH["autoEnabled"]:
                res = gh_backup_now()
                if not res.get("ok"):
                    err = res.get("error", "unknown")
                    print(f"[gh_auto_loop] backup failed: {err}", flush=True)
                    try:
                        notify_owner(
                            f"<b>{G['warn']} {sc('GitHub auto-backup failed')}</b>\n"
                            f"{bullet('Error', esc(err))}"
                        )
                    except Exception:
                        pass
                else:
                    print(f"[gh_auto_loop] backup ok ({res.get('sizeMB')} MB)",
                          flush=True)
        except Exception as e:
            print(f"[gh_auto_loop] loop error: {e}", flush=True)
            traceback.print_exc()


_GH_UPTIME_BACKUP_THRESHOLD = 10 * 60  # seconds — only back up bots running >=10 min


_GH_USER_DATA_LAST_PUSH = [0.0]


def gh_uptime_backup_loop() -> None:
    """Per-bot GitHub backup that fires only after a bot has been
    running uninterrupted for >=10 minutes. This avoids polluting the
    backup repo with broken uploads / quick test runs.

    Re-syncs a bot only when its encrypted files have been modified
    since the last successful sync (so editing env vars or restarting
    doesn't spam GitHub)."""
    while True:
        try:
            time.sleep(60)
            if not (gh_enabled() and GH.get("autoEnabled", True)):
                continue
            now = time.time()
            # Refresh the master DB index every 5 minutes so plan
            # changes / new users / approval toggles get backed up
            # even if no bot files changed.
            if now - _GH_USER_DATA_LAST_PUSH[0] > 5 * 60:
                try:
                    if gh_sync_user_data():
                        _GH_USER_DATA_LAST_PUSH[0] = now
                except Exception:
                    pass
            with _runner_lock:
                items = list(RUNNING.items())
            for bot_id, info in items:
                proc = info.get("proc")
                if not proc or proc.poll() is not None:
                    continue
                started = info.get("started", now)
                if (now - started) < _GH_UPTIME_BACKUP_THRESHOLD:
                    continue
                b = find_bot(bot_id)
                if not b:
                    continue
                last = float(b.get("gh_synced_at") or 0)
                # Latest mtime across all encrypted files
                file_mtime = 0.0
                for f in b.get("enc_files") or []:
                    p = Path(f.get("enc_path", ""))
                    try:
                        if p.exists():
                            file_mtime = max(file_mtime, p.stat().st_mtime)
                    except Exception:
                        pass
                if last and file_mtime and file_mtime <= last:
                    continue   # nothing new since last successful sync
                try:
                    _gh_sync_bot_files(b)
                    b["gh_synced_at"] = int(now)
                    save_bot(b)
                    print(f"[gh_uptime_backup] synced bot={bot_id} "
                          f"(uptime={int(now - started)}s)", flush=True)
                except Exception as e:
                    print(f"[gh_uptime_backup] {bot_id} failed: {e}", flush=True)
                # Pace the loop: GitHub's contents API rate-limits at
                # ~5000 req/hr per token. With many bots running, hammering
                # the API back-to-back risks 403s. A small inter-bot sleep
                # spreads the load and gives other threads CPU room.
                time.sleep(1.5)
        except Exception as e:
            print(f"[gh_uptime_backup] loop error: {e}", flush=True)
            traceback.print_exc()


def gh_auto_restore_on_boot() -> Optional[Dict[str, Any]]:
    """Restore from GitHub on boot ONLY when local storage is empty.

    Order of preference:
      1) New per-file layout (user_data.json + user_uploads/<uid>/<bid>/...)
      2) Legacy tarball at backups/latest.tar.gz  (full overwrite)

    We never overwrite a non-empty local DB — that would clobber any
    changes the user made between the last sync and this restart.

    Custom admin banner photos (storage/photos/custom_*.png) are ALWAYS
    pulled from GitHub on boot when missing locally — independent of the
    DB-empty check — so a wiped photos folder is rebuilt on restart."""
    if not gh_enabled():
        return None
    if not GH.get("autoEnabled", False):
        return None
    # Always try to repopulate admin-set banner photos first; this is safe
    # because gh_restore_custom_photos() never overwrites an existing local
    # file and only ever touches storage/photos/.
    try:
        photos_res = gh_restore_custom_photos()
        if photos_res.get("ok") and photos_res.get("restored", 0):
            print(f"[gh_restore] photos: {photos_res['restored']} banners restored",
                  flush=True)
    except Exception as _pe:
        print(f"[gh_restore] photos failed: {_pe}", flush=True)
    try:
        if DB_FILE.exists():
            data = json.loads(DB_FILE.read_text(encoding="utf-8") or "{}")
            users = data.get("users") or {}
            bots = data.get("bots") or {}
            if users or bots:
                return {"ok": False, "skip": True,
                        "reason": "local data present, not restoring"}
    except Exception:
        pass
    # Try new layout first
    res = gh_restore_user_uploads()
    if res.get("ok"):
        try:
            print(f"[gh_restore] new-layout: {res.get('bots',0)} bots, "
                  f"{res.get('files',0)} files restored", flush=True)
        except Exception:
            pass
        return res
    # Fallback: legacy tarball
    return gh_restore_now(overwrite=True)

def _gh_bot_dir(b: Dict[str, Any]) -> str:
    """Per-bot folder layout requested by the user:
       user_uploads/<user_id>/<bot_id>/..."""
    return f"user_uploads/{b.get('owner', 0)}/{b['_id']}"


def _gh_get_file(path: str) -> Optional[bytes]:
    if not gh_enabled():
        return None
    try:
        r = _gh("GET", _gh_repo_url(f"contents/{path}"),
                params={"ref": GH["branch"]})
        if r.status_code != 200:
            return None
        return base64.b64decode(r.json()["content"])
    except Exception:
        return None


def _gh_delete_path(path: str, message: str) -> bool:
    """Best-effort delete of a single file path."""
    try:
        r = _gh("GET", _gh_repo_url(f"contents/{path}"),
                params={"ref": GH["branch"]})
        if r.status_code != 200:
            return False
        sha = r.json().get("sha")
        if not sha:
            return False
        d = _gh("DELETE", _gh_repo_url(f"contents/{path}"),
                json={"message": message, "sha": sha, "branch": GH["branch"]})
        return d.status_code in (200, 204)
    except Exception:
        return False


def gh_sync_user_data() -> bool:
    """Push the master DB (user_data.json) to the backup repo. This is
    the single source of truth for users + bot metadata, and is small
    enough that we can re-upload it whenever something stable changes."""
    if not gh_enabled():
        return False
    try:
        if not _gh_ensure_branch():
            return False
        if not DB_FILE.exists():
            return False
        buf = DB_FILE.read_bytes()
        ok = _gh_put_file("user_data.json", buf,
                          f"sync: user_data {ts_iso()}")
        # Also push settings (photos config, approval flag, etc.)
        if SETTINGS_FILE.exists():
            try:
                _gh_put_file("settings.json", SETTINGS_FILE.read_bytes(),
                             f"sync: settings {ts_iso()}")
            except Exception:
                pass
        return ok
    except Exception as e:
        print(f"[gh_sync_user_data] {e}")
        return False


def _gh_sync_bot_files(b: Dict[str, Any]) -> None:
    """Per-bot file sync to user_uploads/<owner>/<bot_id>/.
    Triggered from the uptime loop only AFTER the bot has been running
    for >=10 min — so broken/test uploads never reach GitHub."""
    if not gh_enabled():
        return
    try:
        _gh_ensure_branch()
        bot_dir = _gh_bot_dir(b)
        for f in b.get("enc_files") or []:
            p = Path(f["enc_path"])
            if not p.exists():
                continue
            # Use the on-disk filename (already includes timestamp suffix
            # via store_uploaded_file -> "<ts>_<name>.enc")
            gh_path = f"{bot_dir}/{p.name}"
            _gh_put_file(gh_path, p.read_bytes(),
                         f"upload: bot={b['_id']} file={p.name}")
        meta = json.dumps({
            "bot_id":    b["_id"],
            "owner":     b.get("owner"),
            "name":      b.get("name"),
            "enc_files": b.get("enc_files", []),
            "env":       b.get("env", {}),
            "cron":      b.get("cron", {}),
            "status":    b.get("status"),
            "created":   b.get("created"),
            "synced":    ts_iso(),
        }, indent=2).encode()
        _gh_put_file(f"{bot_dir}/bot_meta.json", meta,
                     f"meta: bot={b['_id']}")
        # Each successful per-bot sync also pushes the latest user_data.json
        # so that on a full restore we get an up-to-date users + bots index.
        gh_sync_user_data()
    except Exception as e:
        print(f"[gh_sync] {e}")


def _gh_delete_bot_files(b: Dict[str, Any]) -> None:
    if not gh_enabled():
        return
    try:
        bot_dir = _gh_bot_dir(b)
        for f in b.get("enc_files") or []:
            p = Path(f["enc_path"])
            _gh_delete_path(f"{bot_dir}/{p.name}",
                            f"delete: bot={b['_id']} file={p.name}")
        _gh_delete_path(f"{bot_dir}/bot_meta.json",
                        f"delete: bot={b['_id']} meta")
    except Exception as e:
        print(f"[gh_delete] {e}")


def _gh_list_dir(path: str) -> List[Dict[str, Any]]:
    """List immediate children of a directory in the repo."""
    if not gh_enabled():
        return []
    try:
        r = _gh("GET", _gh_repo_url(f"contents/{path}"),
                params={"ref": GH["branch"]})
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []


def gh_restore_user_uploads() -> Dict[str, Any]:
    """Restore the new-style backup: user_data.json + the per-bot
    encrypted files under user_uploads/<uid>/<bot_id>/*.

    Falls back gracefully if the layout isn't present (e.g. a fresh
    repo) — caller can then try the legacy tarball restore."""
    if not gh_enabled():
        return {"ok": False, "error": "Not configured."}
    user_data = _gh_get_file("user_data.json")
    if user_data is None:
        return {"ok": False, "error": "No user_data.json in repo (new-style backup not found)."}
    files_restored = 0
    bots_restored = 0
    try:
        # 1) Restore the master DB first so we know which bots/owners exist.
        DB_FILE.parent.mkdir(parents=True, exist_ok=True)
        DB_FILE.write_bytes(user_data)
        _cache_invalidate(DB_FILE)
        # Restore settings if present
        s_buf = _gh_get_file("settings.json")
        if s_buf is not None:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_bytes(s_buf)
            _cache_invalidate(SETTINGS_FILE)
        # 2) Walk every bot in the DB and pull its encrypted files back.
        db = db_load()
        for bot_id, b in (db.get("bots") or {}).items():
            owner = b.get("owner") or 0
            bot_dir_local = Path(b.get("dir") or (DIRS["sandbox"] / f"{owner}_{bot_id}"))
            bot_dir_local.mkdir(parents=True, exist_ok=True)
            gh_dir = f"user_uploads/{owner}/{bot_id}"
            entries = _gh_list_dir(gh_dir)
            for ent in entries:
                name = ent.get("name") or ""
                if not name.endswith(".enc"):
                    continue  # bot_meta.json etc. handled separately
                buf = _gh_get_file(f"{gh_dir}/{name}")
                if buf is None:
                    continue
                # Restore encrypted blob to its original location
                # (DIRS["encfiles"]/<owner>/<filename>.enc) so that the
                # paths stored inside enc_files[].enc_path keep working.
                target_dir = DIRS["encfiles"] / str(owner)
                target_dir.mkdir(parents=True, exist_ok=True)
                (target_dir / name).write_bytes(buf)
                files_restored += 1
            bots_restored += 1
        return {"ok": True, "bots": bots_restored, "files": files_restored}
    except Exception as e:
        return {"ok": False, "error": f"restore error: {e}"}


# 13. NOTIFY OWNER  /  ANNOUNCEMENTS

def notify_owner(html: str) -> None:
    if not OWNER_ID:
        return
    try:
        bot.send_message(OWNER_ID, html, parse_mode="HTML")
    except Exception as e:
        print(f"[notify_owner] {e}")


def post_announcement(html: str) -> None:
    if not ANNOUNCE_CHANNEL:
        return
    try:
        bot.send_message(ANNOUNCE_CHANNEL, html, parse_mode="HTML")
    except Exception as e:
        print(f"[announce] {e}")



# 14. USER  MANAGEMENT

def get_or_create_user(u: types.User, ref: Optional[int] = None) -> Tuple[Dict[str, Any], bool]:
    db = db_load()
    key = str(u.id)
    is_new = key not in db["users"]
    if is_new:
        db["users"][key] = {
            "_id": u.id, "name": u.first_name or "", "username": u.username or "",
            "plan": "free", "plan_expires": None,
            "joined": ts_iso(), "last_seen": ts_iso(),
            "banned": False, "ban_reason": "",
            "wallet": 0, "kyc": False,
            "verified": False, "verified_at": None,
            "ref_by": ref if ref and ref != u.id else None,
            "ref_count": 0, "ref_credit": 0, "trial_used": False,
            "bot_slots_bonus": 0,
            "stats": {"commands": 0, "bots_uploaded": 0, "logins": 1},
        }
        db_save(db)
        if ref and ref != u.id and str(ref) in db["users"]:
            db["users"][str(ref)]["ref_count"] = int(db["users"][str(ref)].get("ref_count", 0)) + 1
            db["users"][str(ref)]["ref_credit"] = int(db["users"][str(ref)].get("ref_credit", 0)) + 1
            db["users"][str(ref)]["bot_slots_bonus"] = int(
                db["users"][str(ref)].get("bot_slots_bonus", 0)) + 1
            db_save(db)
            try:
                bot.send_message(
                    ref,
                    f"<b>{G['plus']} {sc('You earned a referral bonus')}</b>\n"
                    f"{bullet('From', f'@{u.username or u.first_name}')}\n"
                    f"{bullet('Bonus', '+1 bot slot, +1 wallet credit')}",
                )
            except Exception:
                pass
        notify_owner(
            f"<b>{G['plus']} {sc('New user joined')}</b>\n"
            f"{bullet('Name', u.first_name)}\n"
            f"{bullet('Username', '@' + (u.username or '—'))}\n"
            f"{bullet('User ID', u.id)}"
        )
    else:
        db["users"][key]["last_seen"] = ts_iso()
        db["users"][key]["stats"]["logins"] = int(
            db["users"][key]["stats"].get("logins", 0)) + 1
        db_save(db)
    return db["users"][key], is_new


def list_user_bots(uid: int) -> List[Dict[str, Any]]:
    # Return deep-copies so callers can mutate without corrupting the
    # shared cache.
    return [copy.deepcopy(b) for b in db_load_ro()["bots"].values()
            if b.get("owner") == uid]


def find_bot(bot_id: str) -> Optional[Dict[str, Any]]:
    b = db_load_ro()["bots"].get(bot_id)
    return copy.deepcopy(b) if b is not None else None


def save_bot(doc: Dict[str, Any]) -> Dict[str, Any]:
    d = db_load()
    d["bots"][doc["_id"]] = doc
    db_save(d)
    # Per-bot JSON backup
    try:
        bot_json = DIRS["bot_data"] / f"{doc['_id']}.json"
        _atomic_write(bot_json, {
            "bot_id":    doc["_id"],
            "owner":     doc.get("owner"),
            "name":      doc.get("name"),
            "status":    doc.get("status"),
            "env":       doc.get("env", {}),
            "cron":      doc.get("cron", {}),
            "enc_files": doc.get("enc_files", []),
            "dir":       doc.get("dir"),
            "created":   doc.get("created"),
            "last_started": doc.get("last_started"),
            "updated":   ts_iso(),
        })
    except Exception:
        pass
    return doc


def delete_bot_doc(bot_id: str) -> None:
    d = db_load()
    d["bots"].pop(bot_id, None)
    db_save(d)
    # Per-bot JSON bhi delete karo
    try:
        (DIRS["bot_data"] / f"{bot_id}.json").unlink(missing_ok=True)
    except Exception:
        pass


def user_max_bots(u: Dict[str, Any]) -> int:
    plan = u.get("plan", "free")
    default = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["max_bots"]
    # Honor admin override from Settings → Plans Editor.
    base = int(get_setting(f"plan_max_bots_{plan}", default))
    return base + int(u.get("bot_slots_bonus", 0))


def user_plan_active(u: Dict[str, Any]) -> bool:
    if u.get("plan") == "free":
        return True
    exp = u.get("plan_expires")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(str(exp).replace("Z", "+00:00")) > now_utc()
    except Exception:
        return False


def downgrade_expired_users() -> None:
    d = db_load()
    changed = False
    for uid, u in d["users"].items():
        if u.get("plan") == "free":
            continue
        if not user_plan_active(u):
            u["plan"] = "free"
            u["plan_expires"] = None
            changed = True
            try:
                bot.send_message(
                    int(uid),
                    f"<b>{G['warn']} {sc('Plan expired')}</b>\n\n"
                    f"Your plan has expired. You have been downgraded to <b>Free</b>.\n"
                    f"Renew anytime from the Buy Plan menu.{FOOTER}",
                )
            except Exception:
                pass
    if changed:
        db_save(d)


def expiry_reminders() -> None:
    d = db_load()
    today = now_utc()
    for uid, u in d["users"].items():
        if u.get("plan") == "free":
            continue
        exp = u.get("plan_expires")
        if not exp:
            continue
        try:
            ed = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        except Exception:
            continue
        days_left = (ed - today).days
        last_warn = u.get("last_expiry_warn", -1)
        for threshold in (7, 3, 1):
            if days_left == threshold and last_warn != threshold:
                try:
                    bot.send_message(
                        int(uid),
                        f"<b>{G['warn']} {sc('Plan ending soon')}</b>\n\n"
                        f"Your <b>{esc(PLAN_LIMITS.get(u['plan'], {}).get('name'))}</b> plan "
                        f"expires in <b>{days_left} day(s)</b>.\n"
                        f"Renew now to avoid downgrade.{FOOTER}",
                    )
                    u["last_expiry_warn"] = threshold
                    db_save(d)
                except Exception:
                    pass


def grant_plan(uid: int, plan: str, days: Optional[int] = None) -> bool:
    d = db_load()
    key = str(uid)
    if key not in d["users"] or plan not in PLAN_LIMITS:
        return False
    u = d["users"][key]
    pl = PLAN_LIMITS[plan]
    days = days if days is not None else pl["days"]
    if plan == "free":
        u["plan"] = "free"
        u["plan_expires"] = None
    else:
        u["plan"] = plan
        # extend if same plan; else set fresh
        try:
            cur_exp = datetime.fromisoformat(str(u.get("plan_expires") or "").replace("Z", "+00:00"))
        except Exception:
            cur_exp = now_utc()
        if cur_exp < now_utc() or u.get("plan") != plan:
            cur_exp = now_utc()
        u["plan_expires"] = (cur_exp + timedelta(days=days)).isoformat()
        u["last_expiry_warn"] = -1
    db_save(d)
    try:
        bot.send_message(
            uid,
            f"<b>{G['ok']} {sc('Plan activated')}</b>\n\n"
            f"{bullet('Plan', pl['name'])}\n"
            f"{bullet('Bots',  pl['max_bots'])}\n"
            f"{bullet('RAM',   '{} MB'.format(pl['ram']))}\n"
            f"{bullet('Until', fmt_ts(u.get('plan_expires')) if u.get('plan_expires') else 'Lifetime')}"
            f"{FOOTER}",
        )
    except Exception:
        pass
    return True


# ═════════════════════════════════════════════════════════════════
# 15. CALLBACK / HANDLER  COMMON HELPERS
# ═════════════════════════════════════════════════════════════════

def ack(call: types.CallbackQuery, text: str = "") -> None:
    try:
        bot.answer_callback_query(call.id, text=text)
    except Exception:
        pass


# ── Animated progress-bar loading indicator ──────────────────────
# Active per-message animations live here so we can stop them when the
# real menu re-renders. Key: (chat_id, message_id) → threading.Event.
_LOADING_STOPS: Dict[Tuple[int, int], "threading.Event"] = {}
_LOADING_LOCK = threading.Lock()


def _progress_bar(pct: int, width: int = 20) -> str:
    """`▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░ 70%` style bar."""
    pct = max(0, min(100, int(pct)))
    filled = int(round(width * pct / 100))
    return "▓" * filled + "░" * (width - filled) + f" {pct:>3}%"


def _cancel_loading(chat_id: int, message_id: int) -> None:
    """Stop any animation thread attached to this message."""
    with _LOADING_LOCK:
        evt = _LOADING_STOPS.pop((chat_id, message_id), None)
    if evt:
        evt.set()


def loading(call: types.CallbackQuery, label: str = "Loading") -> None:
    """Show an animated progress bar (▓▓▓░░░ 45 %) the instant a slow
    callback starts, so the user sees their tap was received.

    The bar is rendered into the same message that triggered the
    callback (caption-edit for photo menus, text-edit for plain
    messages) and is then advanced by a daemon thread until the
    handler finishes. The next show_menu / show_text call on that
    message stops the animation automatically — handlers do not need
    to call anything to clean up.
    """
    if not (call and call.message):
        try:
            bot.answer_callback_query(call.id, text=f"⏳ {label}…")
        except Exception:
            pass
        return

    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    is_photo = call.message.content_type == "photo"
    label_safe = esc(label)

    # Cancel any previous animation on this message before starting a
    # new one (defensive — show_menu also cancels on re-render).
    _cancel_loading(chat_id, msg_id)

    # Toast on the button itself.
    try:
        bot.answer_callback_query(call.id, text=f"↻ {label}…")
    except Exception:
        pass

    def _render(pct: int) -> bool:
        """Push the current bar to Telegram. Returns False if the
        message can no longer be edited (deleted, replaced, etc.) so
        the caller can stop the animation early."""
        body = (
            f"<b>↻ {label_safe}…</b>\n"
            f"{G['div']}\n"
            f"<code>{_progress_bar(pct)}</code>\n"
            f"<i>{sc('Please wait')}</i>{FOOTER}"
        )
        try:
            if is_photo:
                bot.edit_message_caption(
                    body, chat_id=chat_id, message_id=msg_id,
                    parse_mode="HTML",
                )
            else:
                bot.edit_message_text(
                    body, chat_id=chat_id, message_id=msg_id,
                    parse_mode="HTML", disable_web_page_preview=True,
                )
            return True
        except ApiTelegramException as e:
            s = str(e).lower()
            if "message is not modified" in s:
                return True
            if "message to edit not found" in s or "message can't be edited" in s:
                return False
            return True
        except Exception:
            return True

    # Initial frame: visible feedback within ~1 telegram round-trip.
    _render(15)

    stop_evt = threading.Event()
    with _LOADING_LOCK:
        _LOADING_STOPS[(chat_id, msg_id)] = stop_evt

    def _animate() -> None:
        # Advance from 15% → ~92% over a few seconds. We never reach
        # 100% on our own — the handler completing and re-rendering is
        # the real "done" signal.
        steps = [25, 38, 52, 65, 78, 88, 92]
        for pct in steps:
            if stop_evt.wait(0.7):
                return
            if not _render(pct):
                return
        # Hold at 92% until cancelled.
        while not stop_evt.wait(1.5):
            pass

    threading.Thread(target=_animate, daemon=True).start()


def admin_only_call(call: types.CallbackQuery, action: str = "view_stats") -> bool:
    if not is_admin(call.from_user.id):
        ack(call, "Owner / admin only.")
        return False
    if not admin_can(call.from_user.id, action):
        ack(call, "Insufficient permission.")
        return False
    return True


def maintenance_block(uid: int) -> bool:
    """Return True if user is blocked by maintenance mode."""
    if get_setting("maintenance", False) and not is_admin(uid):
        return True
    return False


def banned_block(call_or_msg: Any) -> bool:
    uid = call_or_msg.from_user.id
    u = db_load_ro()["users"].get(str(uid))
    if u and u.get("banned"):
        try:
            chat = call_or_msg.message.chat.id if hasattr(call_or_msg, "message") else call_or_msg.chat.id
            bot.send_message(
                chat,
                f"<b>{G['no']} {sc('You are banned')}</b>\n"
                f"{bullet('Reason', u.get('ban_reason') or '—')}\n"
                f"Contact {SUPPORT_USR} to appeal.",
            )
        except Exception:
            pass
        return True
    return False


# ═════════════════════════════════════════════════════════════════
# 15.5  HUMAN VERIFICATION  (captcha + animated progress bar)
# ═════════════════════════════════════════════════════════════════
#
# Flow on a brand-new user's first /start:
#   1. an "loading 10% → 100%" progress bar (one message, edited live)
#   2. a CAPTCHA photo: 4 random characters, ONE has a red circle on it
#   3. inline buttons (the 4 chars + 2 distractors, shuffled) — user
#      must tap the *circled* one
#   4. on success → user.verified = True, main menu shown
# After verification the captcha is never shown again for that user.

VERIFY_STATES: Dict[int, Dict[str, Any]] = {}
_verify_lock = threading.Lock()

# Visually unambiguous alphanumeric pool (no I/O/0/1, no Q vs O confusion)
_CAPTCHA_POOL = "ABCDEFGHJKLMNPRSTUVWXYZ23456789"

# Try a few well-known TTF locations; fall back to PIL's default bitmap
_CAPTCHA_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _captcha_font(size: int):
    if not _PIL_OK:
        return None
    for fp in _CAPTCHA_FONT_PATHS:
        try:
            if os.path.exists(fp):
                return ImageFont.truetype(fp, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def _gen_captcha_image() -> Tuple[Optional[bytes], str, List[str]]:
    """Generate captcha PNG bytes + the correct (circled) character +
    the 6 button options (shuffled, includes correct + 3 captcha chars
    + 2 distractors)."""
    text = "".join(random.choice(_CAPTCHA_POOL) for _ in range(4))
    correct_idx = random.randrange(4)
    correct_ch = text[correct_idx]

    options = list(set(text))
    while len(options) < 6:
        c = random.choice(_CAPTCHA_POOL)
        if c not in options:
            options.append(c)
    random.shuffle(options)

    if not _PIL_OK:
        return None, correct_ch, options

    W, H = 720, 320
    bg = (15, 23, 42)  # slate-900
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    # background noise — diagonal bands
    for _ in range(10):
        x1, y1 = random.randint(-50, W), random.randint(-50, H)
        x2, y2 = x1 + random.randint(150, 400), y1 + random.randint(-80, 80)
        draw.line([(x1, y1), (x2, y2)],
                  fill=(40, 50, 70), width=random.randint(2, 4))
    # speckle noise
    for _ in range(450):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        v = random.randint(80, 200)
        draw.point((x, y), fill=(v, v, v))

    font = _captcha_font(140)

    # draw each char on its own RGBA tile, rotate, paste
    char_centers: List[Tuple[int, int]] = []
    slot_w = W // 4
    palette = [
        (250, 204, 21),   # amber
        (96, 165, 250),   # blue
        (236, 72, 153),   # pink
        (52, 211, 153),   # green
        (244, 114, 182),  # rose
        (251, 146, 60),   # orange
    ]
    for i, ch in enumerate(text):
        tile = Image.new("RGBA", (200, 240), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        col = random.choice(palette)
        try:
            td.text((30, 30), ch, font=font, fill=col + (255,))
        except Exception:
            td.text((30, 30), ch, fill=col + (255,))
        tile = tile.rotate(random.randint(-22, 22),
                           resample=Image.BILINEAR)
        cx = slot_w * i + slot_w // 2 - 100 + random.randint(-10, 10)
        cy = (H - 240) // 2 + random.randint(-15, 15)
        img.paste(tile, (cx, cy), tile)
        char_centers.append((cx + 100, cy + 120))

    # red circle on the chosen char
    cx, cy = char_centers[correct_idx]
    r = 90
    for dr in range(0, 5):
        draw.ellipse(
            [cx - r - dr, cy - r - dr, cx + r + dr, cy + r + dr],
            outline=(239, 68, 68),
        )

    # bottom hint strip
    hint_font = _captcha_font(28)
    hint = "tap the circled character"
    try:
        bbox = draw.textbbox((0, 0), hint, font=hint_font)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = len(hint) * 10
    draw.rectangle([0, H - 44, W, H], fill=(30, 41, 59))
    try:
        draw.text(((W - tw) // 2, H - 38), hint,
                  font=hint_font, fill=(226, 232, 240))
    except Exception:
        pass

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue(), correct_ch, options


def _progress_bar_text(pct: int) -> str:
    pct = max(0, min(100, pct))
    filled = pct // 10
    bar = "▰" * filled + "▱" * (10 - filled)
    return (
        f"<b>{G['shield']} {sc('Verifying you')}…</b>\n"
        f"{G['div']}\n"
        f"<b><code>[{bar}] {pct:3d}%</code></b>"
    )


def _send_progress_then_captcha(chat_id: int, uid: int) -> None:
    """Phase 1: animated progress bar (one message, edited).
       Phase 2: same message edited to 'solve captcha' — no delete."""
    msg_id: Optional[int] = None
    try:
        m = bot.send_message(chat_id, _progress_bar_text(10),
                             parse_mode="HTML")
        msg_id = m.message_id
    except Exception:
        pass

    for pct in (25, 45, 65, 85, 100):
        time.sleep(0.45)
        if msg_id is None:
            break
        try:
            bot.edit_message_text(
                _progress_bar_text(pct), chat_id, msg_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    # Delete NAHI — edit karo
    if msg_id is not None:
        try:
            bot.edit_message_text(
                f"<b>{G['shield']} {sc('Verification loading')}… {sc('solve captcha below')} ↓</b>",
                chat_id, msg_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    _send_captcha(chat_id, uid)


def _send_captcha(chat_id: int, uid: int) -> None:
    png, correct, opts = _gen_captcha_image()
    kb = types.InlineKeyboardMarkup()
    btns = [Btn(c, callback_data=f"verify_{c}")
            for c in opts]
    for i in range(0, len(btns), 3):
        kb.row(*btns[i:i + 3])
    kb.row(
        Btn(
            f"{G.get('refresh', '↻')} {sc('New captcha')}",
            callback_data="verify_new",
        )
    )

    cap = (
        f"<b>{G['shield']} {sc('Human verification')}</b>\n"
        f"{G['div']}\n"
        f"{sc('Look at the image above')}.\n"
        f"{sc('One character has a red circle around it')}.\n"
        f"<b>{sc('Tap that exact character below')}.</b>\n"
        f"{G['div']}\n"
        f"{bullet('Tries', '3')}\n"
        f"{bullet('Tip', sc('use New captcha if unreadable'))}"
        f"{FOOTER}"
    )

    sent_id: Optional[int] = None
    try:
        if png is not None:
            m = bot.send_photo(
                chat_id, png, caption=cap,
                parse_mode="HTML", reply_markup=kb,
            )
            sent_id = m.message_id
        else:
            # PIL unavailable — text-only fallback
            text_cap = (
                f"<b>{G['shield']} {sc('Human verification')}</b>\n"
                f"{G['div']}\n"
                f"{sc('Tap this exact character')}: <b><code>{esc(correct)}</code></b>"
                f"{FOOTER}"
            )
            m = bot.send_message(
                chat_id, text_cap, parse_mode="HTML", reply_markup=kb,
            )
            sent_id = m.message_id
    except Exception as e:
        print(f"[verify] send failed: {e}", flush=True)
        return

    with _verify_lock:
        prev = VERIFY_STATES.get(uid) or {}
        VERIFY_STATES[uid] = {
            "answer": correct,
            "options": opts,
            "msg_id": sent_id,
            "chat_id": chat_id,
            "tries": 0,
            # carry regens forward so the regen rate-limit isn't reset
            "regens": int(prev.get("regens", 0)),
            "ts": time.time(),
        }


def _verify_state_janitor() -> None:
    """Drop captcha sessions older than 10 minutes — prevents
    VERIFY_STATES from growing unbounded if users abandon."""
    while True:
        try:
            time.sleep(120)
            cutoff = time.time() - 600
            with _verify_lock:
                stale = [u for u, s in VERIFY_STATES.items()
                         if s.get("ts", 0) < cutoff]
                for u in stale:
                    VERIFY_STATES.pop(u, None)
            if stale:
                print(f"[verify] cleaned {len(stale)} stale captcha state(s)",
                      flush=True)
        except Exception as e:
            print(f"[verify] janitor error: {e}", flush=True)


# ─── Group Join Verification ─────────────────────────────────────
REQUIRED_GROUPS = [
    {"id": -1003715566556, "link": "https://t.me/+OClpzDTPSGxkZWU1", "name": "Group 1"},
    {"id": -1003776599179, "link": "https://t.me/autolikegcrbot",     "name": "Group 2"},
]

def _check_group_membership(uid: int) -> List[Dict]:
    """Returns list of groups the user has NOT joined yet."""
    not_joined = []
    for grp in REQUIRED_GROUPS:
        try:
            member = bot.get_chat_member(grp["id"], uid)
            if member.status in ("left", "kicked", "banned"):
                not_joined.append(grp)
        except Exception:
            not_joined.append(grp)
    return not_joined

def _send_join_verification(chat_id: int, uid: int, not_joined: List[Dict]) -> None:
    """Send group join buttons to user."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    for grp in not_joined:
        kb.add(Btn(
            f"{G['fwd']}  Jᴏɪɴ {grp['name']}", url=grp["link"]))
    kb.add(Btn(
        f"{G['ok']}  Vᴇʀɪꜰɪᴄᴀᴛɪᴏɴ", callback_data="group_verify_check"))
    cap = (
        f"<b>{G['shield']} {sc('Group Join Required')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('You must join the following groups to use this bot')}:\n"
        f"{G['div']}\n"
        + "\n".join(f"{G['bullet']} <a href='{g['link']}'>{esc(g['name'])}</a>" for g in not_joined)
        + f"\n{G['div']}\n"
        f"{sc('After joining, tap')} <b>{sc('Verification')}</b> {sc('below')}."
        f"{FOOTER}"
    )
    try:
        bot.send_message(chat_id, cap, parse_mode="HTML", reply_markup=kb,
                         disable_web_page_preview=True)
    except Exception as e:
        print(f"[group_verify] send failed: {e}", flush=True)

def require_group_membership(chat_id: int, uid: int) -> bool:
    """Returns True if user has joined all required groups.
    Otherwise sends join prompt and returns False."""
    if uid == OWNER_ID and OWNER_ID > 0:
        return True
    if is_admin(uid):
        return True
    not_joined = _check_group_membership(uid)
    if not not_joined:
        return True
    _send_join_verification(chat_id, uid, not_joined)
    return False
# ─────────────────────────────────────────────────────────────────

def _is_verified(uid: int) -> bool:
    if uid == OWNER_ID and OWNER_ID > 0:
        return True
    u = db_load_ro()["users"].get(str(uid)) or {}
    return bool(u.get("verified"))


def _mark_verified(uid: int) -> None:
    db = db_load()
    if str(uid) in db["users"]:
        db["users"][str(uid)]["verified"] = True
        db["users"][str(uid)]["verified_at"] = ts_iso()
        db_save(db)


def require_verified(chat_id: int, uid: int) -> bool:
    """Returns True if the user is already verified.
    Otherwise launches the progress-bar + captcha flow and returns False.
    Callers should `return` immediately on False.

    Anti-spam: if a captcha session is already pending for this user
    (progress bar still animating or buttons still on screen) we silently
    drop the duplicate /start instead of stacking another progress bar."""
    if _is_verified(uid):
        return True
    with _verify_lock:
        st = VERIFY_STATES.get(uid)
        now = time.time()
        # Active session = either started in last 6s (progress bar phase)
        # or has a captcha message id (buttons still up).
        if st and (st.get("msg_id") or now - st.get("ts", 0) < 6):
            return False
        # Reserve the slot so the second /start lands in the branch above.
        VERIFY_STATES[uid] = {
            "answer": "", "options": [], "msg_id": None,
            "chat_id": chat_id, "tries": 0, "regens": 0,
            "ts": now, "starting": True,
        }
    threading.Thread(
        target=_send_progress_then_captcha,
        args=(chat_id, uid),
        daemon=True,
    ).start()
    return False


@bot.callback_query_handler(func=lambda c: c.data == "group_verify_check")
def cb_group_verify(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    chat_id = call.message.chat.id
    not_joined = _check_group_membership(uid)
    if not_joined:
        ack(call, "You have not joined all groups yet!")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        _send_join_verification(chat_id, uid, not_joined)
    else:
        ack(call, "✓ Verified! Welcome.")
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        render_main_menu(chat_id, uid)


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("verify_"))
def cb_verify(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    chat_id = call.message.chat.id
    data = call.data[len("verify_"):]

    # NEW captcha (regen)
    if data == "new":
        with _verify_lock:
            st = VERIFY_STATES.get(uid)
            if st and st.get("regens", 0) >= 5:
                ack(call, "Too many regenerations.")
                return
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass
        ack(call, "New captcha…")
        _send_captcha(chat_id, uid)
        with _verify_lock:
            if uid in VERIFY_STATES:
                VERIFY_STATES[uid]["regens"] = (
                    VERIFY_STATES[uid].get("regens", 0) + 1
                )
        return

    with _verify_lock:
        state = VERIFY_STATES.get(uid)

    if not state:
        ack(call, "Session expired — send /start again.")
        return

    if data == state["answer"]:
        with _verify_lock:
            VERIFY_STATES.pop(uid, None)
        _mark_verified(uid)
        ack(call, "✓ Verified")
        try:
            bot.delete_message(chat_id, state["msg_id"])
        except Exception:
            pass
        intro = (
            f"<b>{G['ok']} {sc('Verification complete')}</b> — "
            f"{sc('welcome')}, <b>{esc(call.from_user.first_name or 'friend')}</b>!"
        )
        try:
            audit(uid, "captcha_pass",
                  f"verified after {state.get('tries', 0)} try(s)")
        except Exception:
            pass
        render_main_menu(chat_id, uid, intro=intro)
        return

    # wrong answer
    state["tries"] = state.get("tries", 0) + 1
    left = max(0, 3 - state["tries"])
    if state["tries"] >= 3:
        with _verify_lock:
            VERIFY_STATES.pop(uid, None)
        try:
            bot.delete_message(chat_id, state["msg_id"])
        except Exception:
            pass
        ack(call, "Wrong 3 times — new captcha.")
        _send_captcha(chat_id, uid)
    else:
        ack(call, f"Wrong character. {left} try(s) left.")


# ═════════════════════════════════════════════════════════════════
# 16. /start  AND  MAIN MENU
# ═════════════════════════════════════════════════════════════════

def render_main_menu(chat_id: int, uid: int,
                     call: Optional[types.CallbackQuery] = None,
                     intro: Optional[str] = None) -> None:
    u = db_load()["users"].get(str(uid)) or {}
    plan = PLAN_LIMITS.get(u.get("plan", "free"), PLAN_LIMITS["free"])
    bots = list_user_bots(uid)
    running = sum(1 for b in bots if b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None)
    intro_block = f"{intro}\n{G['div']}\n" if intro else ""
    cap = (
        f"<b>{esc(BRAND)} {esc(BRAND_VER)}</b>\n"
        f"{G['div_eq']}\n"
        f"{intro_block}"
        f"<b>{sc('Welcome')}</b>, {esc(u.get('name') or 'friend')}\n"
        f"{bullet('Plan',  plan['name'])}\n"
        f"{bullet('Until', fmt_ts(u.get('plan_expires')) if u.get('plan_expires') else 'Forever' if plan['price'] == 0 else '—')}\n"
        f"{bullet('Bots',  f'{len(bots)} / {user_max_bots(u)}  (running {running})')}\n"
        f"{bullet('Wallet', '{}$'.format(u.get('wallet', 0)))}\n"
        f"{G['div']}\n"
        f"Choose an option below.{FOOTER}"
    )
    show_menu(chat_id, PHOTOS["main"], cap, main_menu_kb(is_admin(uid)), call=call)


# ─── Silent mode in groups — bot will not respond in any group/channel ───────
def _is_private(m) -> bool:
    """Returns True only for private chats."""
    try:
        return m.chat.type == "private"
    except Exception:
        return True
# ─────────────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(m: types.Message) -> None:
    if not _is_private(m):
        return  # silent in groups
    uid = m.from_user.id
    if not RATE.allow(uid):
        maybe_auto_ban(uid, "rate")
        return
    if banned_block(m):
        return
    # ── auto-claim ownership: first /start with no OWNER_ID env wins ──
    global OWNER_ID
    if OWNER_ID <= 0:
        stored = int(get_setting("owner_id", 0) or 0)
        if stored > 0:
            OWNER_ID = stored
        else:
            OWNER_ID = uid
            set_setting("owner_id", uid)
            audit(uid, "owner_claim", f"first /start, uid={uid}")
            try:
                bot.send_message(
                    m.chat.id,
                    f"<b>{G['crown']} {sc('You are now the panel owner')}</b>\n"
                    f"{G['div']}\n"
                    f"{bullet('Owner ID', uid)}\n"
                    f"{sc('Set OWNER_ID env var to lock ownership permanently')}.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
    ref: Optional[int] = None
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].isdigit():
        ref = int(parts[1])
    u, is_new = get_or_create_user(m.from_user, ref=ref)
    if maintenance_block(uid):
        bot.send_message(
            m.chat.id,
            f"<b>{G['warn']} {sc('Panel under maintenance')}</b>\n\n"
            f"We will be back shortly. {SUPPORT_USR} for urgent issues.",
        )
        return
    # Human verification — first /start ever for this user shows a
    # progress bar (10% → 100%) followed by a captcha photo. Once the
    # captcha is solved, render_main_menu is called from cb_verify.
    if not require_verified(m.chat.id, uid):
        return

    # Group join verification — user must join required groups
    if not require_group_membership(m.chat.id, uid):
        return

    # Single message: welcome line is folded into the main-menu caption,
    # so /start always sends exactly ONE photo + menu.
    intro = (
        f"{sc('You are now registered')}. "
        f"Tap <b>{sc('Plans')}</b> or <b>{sc('Upload Bot')}</b> to begin."
        if is_new else
        f"{sc('Welcome back')}, <b>{esc(m.from_user.first_name or 'friend')}</b>!"
    )
    render_main_menu(m.chat.id, uid, intro=intro)


@bot.message_handler(commands=["help"])
def cmd_help(m: types.Message) -> None:
    if not _is_private(m):
        return
    if banned_block(m):
        return
    if not require_verified(m.chat.id, m.from_user.id):
        return
    txt = (
        f"<b>{esc(BRAND_TAG)} — {sc('Quick Help')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Upload',  'Send a .py / .js / .zip file or use Upload Bot menu.')}\n"
        f"{bullet('Manage',  'My Bots → pick a bot → Start / Stop / Logs.')}\n"
        f"{bullet('Plans',   'Plans → Buy Plan → choose method → send proof.')}\n"
        f"{bullet('Wallet',  'Top-up via admin, then spend on plans.')}\n"
        f"{bullet('Refer',   'Invite friends with your /start link to earn slots.')}\n"
        f"{bullet('Trial',   'One-time 48-hour Pro trial in the Trial menu.')}\n"
        f"{bullet('Support', f'Open a ticket from the Tickets menu, or DM {SUPPORT_USR}.')}\n"
        f"{G['div']}{FOOTER}"
    )
    bot.send_message(m.chat.id, txt, parse_mode="HTML",
                     reply_markup=back_main_kb(), disable_web_page_preview=True)


@bot.message_handler(commands=["menu"])
def cmd_menu(m: types.Message) -> None:
    if not _is_private(m):
        return
    if banned_block(m):
        return
    get_or_create_user(m.from_user)
    if not require_verified(m.chat.id, m.from_user.id):
        return
    render_main_menu(m.chat.id, m.from_user.id)


@bot.message_handler(commands=["id"])
def cmd_id(m: types.Message) -> None:
    if not _is_private(m):
        return
    bot.reply_to(m, f"<code>{m.from_user.id}</code>")


@bot.message_handler(commands=["cancel"])
def cmd_cancel(m: types.Message) -> None:
    if not _is_private(m):
        return
    USER_STATES.pop(m.from_user.id, None)
    bot.reply_to(m, f"{G['ok']} {sc('Cancelled')}")


# ═════════════════════════════════════════════════════════════════
# 17. CALLBACK ROUTER  (top level)
# ═════════════════════════════════════════════════════════════════

# ─── callback de-duplication ─────────────────────────────────────
# Telegram occasionally re-delivers the same callback (rapid double-clicks,
# leftover webhook still active alongside polling, two bot instances polling
# the same token, etc.). We keep a tiny in-memory cache of recently-seen
# callback IDs and silently drop duplicates so the user only ever sees a
# single response per button press.
_CB_SEEN: "deque[Tuple[str, float]]" = deque(maxlen=512)
_CB_SEEN_LOCK = threading.Lock()
_CB_DEDUP_WINDOW = 12.0  # seconds


def _is_duplicate_callback(call_id: str) -> bool:
    if not call_id:
        return False
    now = time.time()
    with _CB_SEEN_LOCK:
        # purge expired entries
        while _CB_SEEN and now - _CB_SEEN[0][1] > _CB_DEDUP_WINDOW:
            _CB_SEEN.popleft()
        for cid, _ in _CB_SEEN:
            if cid == call_id:
                return True
        _CB_SEEN.append((call_id, now))
    return False


@bot.callback_query_handler(func=lambda c: True)
def cb_root(call: types.CallbackQuery) -> None:
    # silently drop duplicate deliveries of the same callback
    if _is_duplicate_callback(getattr(call, "id", "")):
        try:
            bot.answer_callback_query(call.id)
        except Exception:
            pass
        return

    uid = call.from_user.id
    if not RATE.allow(uid):
        ack(call, "Slow down.")
        maybe_auto_ban(uid, "callback rate")
        return
    if banned_block(call):
        ack(call)
        return
    get_or_create_user(call.from_user)
    if maintenance_block(uid):
        ack(call, "Maintenance mode")
        return
    # Block menu navigation for unverified users — they must solve the
    # captcha first. The verify_* callbacks are handled by an earlier
    # registered handler so they bypass this gate.
    if not _is_verified(uid):
        ack(call, "Please solve the captcha first — send /start.")
        return
    data = call.data or ""
    try:
        _route_callback(call, data)
    except Exception as e:
        traceback.print_exc()
        try:
            bot.send_message(call.message.chat.id, f"<b>{G['no']}</b> Eʀʀᴏʀ: <code>{esc(e)}</code>")
        except Exception:
            pass


def _route_callback(call: types.CallbackQuery, data: str) -> None:
    # ─── core menu navigation ──────────────────────────────────
    if data == "menu_main":
        ack(call); render_main_menu(call.message.chat.id, call.from_user.id, call); return
    if data == "menu_bots":
        ack(call); render_bots_menu(call); return
    if data == "menu_upload":
        ack(call); render_upload_menu(call); return
    if data == "menu_plans":
        ack(call); render_plans_menu(call); return
    if data == "menu_buy":
        ack(call); render_buy_menu(call); return
    if data == "menu_profile":
        ack(call); render_profile(call); return
    if data == "menu_referral":
        ack(call); render_referral(call); return
    if data == "menu_wallet":
        ack(call); render_wallet(call); return
    if data == "menu_help":
        ack(call); render_help(call); return
    if data == "menu_support":
        ack(call); render_support(call); return
    if data == "menu_tickets":
        ack(call); render_user_tickets(call); return
    if data == "menu_trial":
        ack(call); render_trial(call); return
    if data == "menu_coupon":
        ack(call); render_coupon(call); return
    if data == "menu_stats":
        ack(call); render_user_stats(call); return
    if data == "menu_admin":
        ack(call); render_admin(call); return

    # ─── plan view + buy ───────────────────────────────────────
    if data.startswith("plan_view_"):
        ack(call); render_plan_detail(call, data.split("_", 2)[2]); return
    if data.startswith("plan_buy_"):
        ack(call); render_payment_methods_for(call, data.split("_", 2)[2]); return

    # ─── pay methods ───────────────────────────────────────────
    if data.startswith("pay_"):
        ack(call); render_payment_screen(call, data); return
    if data == "pay_proof":
        ack(call); start_proof_flow(call); return

    # ─── bot actions ───────────────────────────────────────────
    if data.startswith("bot_view_"):
        ack(call); render_bot_view(call, data.split("_", 2)[2]); return
    if data.startswith("bot_start_"):
        ack(call); action_bot_start(call, data.split("_", 2)[2]); return
    if data.startswith("bot_stop_"):
        ack(call); action_bot_stop(call, data.split("_", 2)[2]); return
    if data.startswith("bot_restart_"):
        ack(call); action_bot_restart(call, data.split("_", 2)[2]); return
    if data.startswith("bot_logs_"):
        ack(call); action_bot_logs(call, data.split("_", 2)[2]); return
    if data.startswith("bot_info_"):
        ack(call); action_bot_info(call, data.split("_", 2)[2]); return
    if data.startswith("bot_env_"):
        ack(call); render_env_menu(call, data.split("_", 2)[2]); return
    if data.startswith("env_add_"):
        ack(call); start_env_add(call, data.split("_", 2)[2]); return
    if data.startswith("env_del_"):
        parts = data.split("_", 3)
        if len(parts) >= 4:
            ack(call); action_env_delete(call, parts[2], parts[3]); return
    if data.startswith("bot_cron_"):
        ack(call); render_cron(call, data.split("_", 2)[2]); return
    if data.startswith("bot_clone_"):
        ack(call); action_bot_clone(call, data.split("_", 2)[2]); return
    if data.startswith("bot_dl_"):
        ack(call); action_bot_download(call, data.split("_", 2)[2]); return
    if data.startswith("bot_pip_"):
        ack(call); start_pip_install_flow(call, data.split("_", 2)[2]); return
    if data.startswith("bot_tunnel_"):
        ack(call); start_tunnel_flow(call, data.split("_", 2)[2]); return
    if data.startswith("bot_delete_"):
        ack(call); render_bot_delete_confirm(call, data.split("_", 2)[2]); return
    if data.startswith("bot_delyes_"):
        ack(call); action_bot_delete(call, data.split("_", 2)[2]); return
    if data.startswith("bot_delfiles_"):
        ack(call); render_bot_delfiles_confirm(call, data.split("_", 2)[2]); return
    if data.startswith("bot_delall_"):
        ack(call); render_bot_delall_confirm(call, data.split("_", 2)[2]); return
    if data.startswith("bot_delfilesyes_"):
        ack(call); action_bot_delfiles(call, data.split("_", 2)[2]); return
    if data.startswith("bot_delalyes_"):
        ack(call); action_bot_delall(call, data.split("_", 2)[2]); return

    # ─── approval system (admin only) ──────────────────────────
    if data.startswith("appr_ok_"):
        if not admin_only_call(call, "approve_payment"):
            return
        bid = data[len("appr_ok_"):]
        res = approve_bot(bid, call.from_user.id)
        ack(call, "Approved" if res.get("ok") else f"Err: {res.get('error')}")
        try:
            bot.edit_message_reply_markup(call.message.chat.id,
                                          call.message.message_id, reply_markup=None)
        except Exception:
            pass
        try:
            bot.send_message(
                call.message.chat.id,
                f"<b>{G['ok']} {sc('Bot approved')}</b>\n"
                f"{bullet('Bot ID', bid)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return
    if data.startswith("appr_no_"):
        if not admin_only_call(call, "approve_payment"):
            return
        bid = data[len("appr_no_"):]
        res = reject_bot(bid, call.from_user.id, reason="rejected by admin")
        ack(call, "Rejected" if res.get("ok") else f"Err: {res.get('error')}")
        try:
            bot.edit_message_reply_markup(call.message.chat.id,
                                          call.message.message_id, reply_markup=None)
        except Exception:
            pass
        try:
            bot.send_message(
                call.message.chat.id,
                f"<b>{G['no']} {sc('Bot rejected')}</b>\n"
                f"{bullet('Bot ID', bid)}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ─── admin sub-actions ─────────────────────────────────────
    if data.startswith("adm_"):
        if not admin_only_call(call, "view_stats"):
            return
        ack(call); render_admin_subroute(call, data); return
    if data.startswith("gh_"):
        if not admin_only_call(call, "view_stats"):
            return
        ack(call); render_github_subroute(call, data); return

    # ─── trial ────────────────────────────────────────────────
    if data == "trial_claim":
        ack(call); action_trial_claim(call); return

    # ─── coupon redeem ─────────────────────────────────────────
    if data == "coupon_redeem":
        ack(call); start_coupon_flow(call); return

    # ─── tickets ──────────────────────────────────────────────
    if data == "ticket_open":
        ack(call); start_ticket_flow(call); return
    if data.startswith("ticket_view_"):
        ack(call); render_ticket_view(call, data.split("_", 2)[2]); return
    if data.startswith("ticket_close_"):
        ack(call); action_ticket_close(call, data.split("_", 2)[2]); return
    if data.startswith("ticket_reply_"):
        ack(call); start_ticket_reply(call, data.split("_", 2)[2]); return

    # ─── wallet top-up request ────────────────────────────────
    if data == "wallet_topup":
        ack(call); start_wallet_topup(call); return
    if data == "wallet_gift":
        ack(call); start_wallet_gift(call); return

    # ─── admin payment approve/reject ─────────────────────────
    if data.startswith("payapprove_"):
        ack(call); action_payment_approve(call, data.split("_", 1)[1]); return
    if data.startswith("payreject_"):
        ack(call); action_payment_reject(call, data.split("_", 1)[1]); return

    # ─── unknown ──────────────────────────────────────────────
    ack(call, "?")


# ═════════════════════════════════════════════════════════════════
# 18. MENU RENDERS
# ═════════════════════════════════════════════════════════════════

def render_bots_menu(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    bots = list_user_bots(uid)
    u = db_load()["users"][str(uid)]
    cap = (
        f"<b>{G['diamond']} {sc('Your Bots')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Slots', f'{len(bots)} / {user_max_bots(u)}')}\n"
    )
    kb = types.InlineKeyboardMarkup()
    if not bots:
        cap += f"\n{sc('You have not deployed any bots yet')}.\n{sc('Tap upload bot to begin')}."
    else:
        for b in sorted(bots, key=lambda x: x.get("name", "")):
            running = b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None
            mark = G["play"] if running else G["stop"]
            kb.add(Btn(
                f"{mark}  {sc(b['name'])[:30]}",
                callback_data=f"bot_view_{b['_id']}"))
    kb.add(
        Btn(f"{G['plus']}  {sc('Upload')}",   callback_data="menu_upload", style="success"),
        Btn(f"{G['back']}  {sc('Main Menu')}", callback_data="menu_main", style="primary"),
    )
    show_menu(call.message.chat.id, PHOTOS["bots"], cap + FOOTER, kb, call=call)


def render_upload_menu(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    u = db_load()["users"][str(uid)]
    used = len(list_user_bots(uid))
    cap = (
        f"<b>{G['plus']} {sc('Upload Bot')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Plan',  PLAN_LIMITS[u['plan']]['name'])}\n"
        f"{bullet('Slots', f'{used} / {user_max_bots(u)}')}\n"
        f"{G['div']}\n"
        f"<b>{sc('Send your bot file as a document')}.</b>\n"
        f"Accepted: <code>.zip  .py  .js</code>\n"
        f"Entry detection: <code>bot.py</code>, <code>main.py</code>, "
        f"<code>app.py</code>, <code>index.js</code>, <code>bot.js</code>.\n"
        f"All files are <b>encrypted at rest</b> with Fernet/AES-128 — keys live in our private key vault."
    )
    USER_STATES[uid] = {"flow": "await_upload"}
    show_menu(call.message.chat.id, PHOTOS["upload"], cap + FOOTER,
              back_main_kb(), call=call)


def render_plans_menu(call: types.CallbackQuery) -> None:
    lines = []
    for v in PLAN_LIMITS.values():
        price_txt = "Free" if v["price"] == 0 else f"{v['price']}\u09F3"
        detail = f"{v['max_bots']} bots {G['bullet']} {v['ram']} MB RAM {G['bullet']} {price_txt}"
        lines.append(bullet(v['name'], detail))
    cap = (
        f"<b>{G['star']} {sc('Plans')}</b>\n"
        f"{G['div_eq']}\n"
        + "\n".join(lines)
        + f"\n{G['div']}\nTap a plan for full details.{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["plans"], cap, plans_kb(), call=call)


def render_plan_detail(call: types.CallbackQuery, plan: str) -> None:
    p = PLAN_LIMITS.get(plan)
    if not p:
        ack(call, "Unknown plan"); return
    cap = (
        f"<b>{G['star']} {esc(p['name'])} {sc('Plan')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Max bots',     p['max_bots'])}\n"
        f"{bullet('RAM per bot',  '{} MB'.format(p['ram']))}\n"
        f"{bullet('Auto-restart', 'Yes' if p['auto_restart'] else 'No')}\n"
        f"{bullet('Duration',     'Lifetime' if plan == 'lifetime' else '{} days'.format(p['days']))}\n"
        f"{bullet('Price',        'Free' if p['price'] == 0 else '{}$'.format(p['price']))}\n"
        f"{G['div']}\n"
        f"{sc('Tap buy to choose a payment method')}.{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    if plan != "free":
        kb.add(Btn(
            f"{G['spark']}  {sc('Buy')} {p['name']}",
            callback_data=f"plan_buy_{plan}"))
    kb.add(Btn(
        f"{G['back']}  {sc('Plans')}", callback_data="menu_plans"))
    show_menu(call.message.chat.id, PHOTOS["buy"], cap, kb, call=call)


def render_buy_menu(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['spark']} {sc('Buy a Plan')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Pick a plan first')}.{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["buy"], cap, plans_kb(), call=call)


def render_payment_methods_for(call: types.CallbackQuery, plan: str) -> None:
    p = PLAN_LIMITS.get(plan)
    if not p:
        ack(call, "Unknown plan"); return
    cap = (
        f"<b>{G['wallet']} {sc('Choose Payment Method')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Plan',  p['name'])}\n"
        f"{bullet('Price', '{}$'.format(p['price']))}\n"
        f"{G['div']}\n"
        f"{sc('Pick the method you will pay with')}.{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["pay"], cap, payments_kb(plan), call=call)


def render_payment_screen(call: types.CallbackQuery, data: str) -> None:
    # data is pay_<method> or pay_<method>_<plan>
    parts = data.split("_")
    method = parts[1]
    plan = parts[2] if len(parts) >= 3 else None
    pm = PAYMENT_METHODS.get(method)
    if not pm:
        ack(call, "Unknown method"); return
    p = PLAN_LIMITS.get(plan or "")
    cap = (
        f"<b>{pm['tag']} {esc(pm['name'])} — {sc('Payment')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Number', pm['number'])}\n"
        f"{bullet('Type',   pm['type'])}\n"
    )
    if p:
        cap += f"{bullet('Plan', p['name'])}\n{bullet('Amount', '{}$'.format(p['price']))}\n"
    cap += (
        f"{G['div']}\n"
        f"<b>{sc('How to pay')}:</b>\n"
        f"1. {sc('Send the exact amount to the number above')}.\n"
        f"2. {sc('Tap send proof and forward your receipt screenshot')}.\n"
        f"3. {sc('Wait for admin approval')} ({sc('usually within 1 hour')}).\n"
        f"{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    USER_STATES[call.from_user.id] = {
        "flow": "await_payment_proof", "method": method, "plan": plan,
    }
    kb.add(Btn(
        f"{G['plus']}  {sc('Send Proof')}", callback_data="pay_proof"))
    kb.add(Btn(
        f"{G['back']}  {sc('Methods')}",
        callback_data=f"plan_buy_{plan}" if plan else "menu_buy"))
    show_menu(call.message.chat.id, PHOTOS["pay"], cap, kb, call=call)


def start_proof_flow(call: types.CallbackQuery) -> None:
    st = USER_STATES.get(call.from_user.id) or {}
    if st.get("flow") != "await_payment_proof":
        st = {"flow": "await_payment_proof"}
        USER_STATES[call.from_user.id] = st
    bot.send_message(
        call.message.chat.id,
        f"{G['plus']} {sc('Send your payment screenshot or transaction id text now')}.\n"
        f"{sc('Use')} /cancel {sc('to abort')}.",
    )


def render_profile(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    u = db_load()["users"][str(uid)]
    p = PLAN_LIMITS.get(u["plan"], PLAN_LIMITS["free"])
    bots = list_user_bots(uid)
    cap = (
        f"<b>{G['user']} {sc('Profile')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Name',     u.get('name'))}\n"
        f"{bullet('Username', '@' + (u.get('username') or '—'))}\n"
        f"{bullet('User ID',  uid)}\n"
        f"{bullet('Plan',     p['name'])}\n"
        f"{bullet('Until',    fmt_ts(u.get('plan_expires')) if u.get('plan_expires') else ('Forever' if p['price'] == 0 else '—'))}\n"
        f"{bullet('Wallet',   '{}$'.format(u.get('wallet', 0)))}\n"
        f"{bullet('Bots',     f'{len(bots)} / {user_max_bots(u)}')}\n"
        f"{bullet('Joined',   fmt_ts(u.get('joined')))}\n"
        f"{bullet('KYC',      'Verified' if u.get('kyc') else 'No')}\n"
        f"{bullet('Referrals', u.get('ref_count', 0))}\n"
        f"{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["profile"], cap, back_main_kb(), call=call)


def render_referral(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    u = db_load()["users"][str(uid)]
    me = bot.get_me()
    link = f"https://t.me/{me.username}?start={uid}"
    cap = (
        f"<b>{G['users']} {sc('Referral')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Your link', link)}\n"
        f"{bullet('Referrals', u.get('ref_count', 0))}\n"
        f"{bullet('Bonus slots', u.get('bot_slots_bonus', 0))}\n"
        f"{G['div']}\n"
        f"{sc('Each friend who joins via your link gives you')} +1 {sc('bot slot and')} +1\u09F3 {sc('credit')}.\n"
        f"{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["referral"], cap, back_main_kb(), call=call)


def render_wallet(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    u = db_load()["users"][str(uid)]
    cap = (
        f"<b>{G['wallet']} {sc('Wallet')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Balance', '{}$'.format(u.get('wallet', 0)))}\n"
        f"{G['div']}\n"
        f"{sc('Top up by sending payment proof. Admin will credit your wallet')}.\n"
        f"{sc('You can also gift your active plan to another user')}.{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(
        f"{G['plus']}  {sc('Top Up')}", callback_data="wallet_topup"))
    if u.get("plan") not in ("free",):
        kb.add(Btn(
            f"{G['spark']}  {sc('Gift Plan')}", callback_data="wallet_gift"))
    kb.add(Btn(
        f"{G['back']}  {sc('Main Menu')}", callback_data="menu_main"))
    show_menu(call.message.chat.id, PHOTOS["wallet"], cap, kb, call=call)


def render_help(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['rec']} {sc('Help')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Upload',  'Send a .py / .js / .zip file')}\n"
        f"{bullet('Run',     'My Bots → pick → Start')}\n"
        f"{bullet('Logs',    'My Bots → pick → Live Logs')}\n"
        f"{bullet('Env',     'My Bots → pick → Env Vars')}\n"
        f"{bullet('Plans',   'Plans → Buy Plan → method')}\n"
        f"{bullet('Coupon',  'Coupon menu → Redeem')}\n"
        f"{bullet('Trial',   'One-time 48h Pro trial')}\n"
        f"{bullet('Refer',   'Earn slots by inviting friends')}\n"
        f"{bullet('Tickets', 'Open a private support ticket')}\n"
        f"{G['div']}\n"
        f"Updates channel: {UPDATE_CH}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["help"], cap, back_main_kb(), call=call)


def render_support(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['broadcast']} {sc('Support')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('DM',      SUPPORT_USR)}\n"
        f"{bullet('Channel', UPDATE_CH)}\n"
        f"{G['div']}\n"
        f"{sc('Or open a ticket from the Tickets menu for tracked help')}.{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["support"], cap, back_main_kb(), call=call)


def render_trial(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    u = db_load()["users"][str(uid)]
    cap = (
        f"<b>{G['eye']} {sc('Free Trial')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Get a free 48-hour Pro trial — one time per account')}.\n"
        f"{bullet('Status', 'Already used' if u.get('trial_used') else 'Available')}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    if not u.get("trial_used"):
        kb.add(Btn(
            f"{G['ok']}  {sc('Claim 48h Pro Trial')}", callback_data="trial_claim"))
    kb.add(Btn(
        f"{G['back']}  {sc('Main Menu')}", callback_data="menu_main"))
    show_menu(call.message.chat.id, PHOTOS["trial"], cap, kb, call=call)


def action_trial_claim(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    d = db_load()
    u = d["users"][str(uid)]
    if u.get("trial_used"):
        ack(call, "Already used"); return
    u["trial_used"] = True
    db_save(d)
    grant_plan(uid, "pro", days=2)
    audit(0, "trial_grant", f"uid={uid}")
    ack(call, "Trial activated")
    render_main_menu(call.message.chat.id, uid, call)


def render_coupon(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['key']} {sc('Coupon')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Have a discount code? Tap redeem and send the code')}.{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(
        f"{G['plus']}  {sc('Redeem Code')}", callback_data="coupon_redeem"))
    kb.add(Btn(
        f"{G['back']}  {sc('Main Menu')}", callback_data="menu_main"))
    show_menu(call.message.chat.id, PHOTOS["coupon"], cap, kb, call=call)


def render_user_stats(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    d = db_load()
    u = d["users"][str(uid)]
    p = PLAN_LIMITS.get(u.get("plan", "free"), PLAN_LIMITS["free"])
    bots = list_user_bots(uid)
    running = sum(1 for b in bots if b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None)
    stopped = len(bots) - running

    # payments
    pays = [x for x in d.get("payments", []) if x.get("uid") == uid and x.get("status") == "approved"]
    last_pay = max((x.get("at", "") for x in pays), default=None)

    # tickets
    tickets = d.get("tickets", {})
    my_tickets = [t for t in tickets.values() if t.get("uid") == uid]
    open_tickets   = sum(1 for t in my_tickets if t.get("status") == "open")
    closed_tickets = sum(1 for t in my_tickets if t.get("status") != "open")

    # storage
    storage_size = 0
    for b in bots:
        bot_dir = BASE_DIR / "storage" / "uploads" / str(b["_id"])
        if bot_dir.exists():
            for root, _, files in os.walk(bot_dir):
                for f in files:
                    try:
                        storage_size += (Path(root) / f).stat().st_size
                    except OSError:
                        pass

    plan_expires = u.get("plan_expires")
    if plan_expires:
        expires_txt = fmt_ts(plan_expires)
    elif p["price"] == 0:
        expires_txt = "Forever"
    else:
        expires_txt = "—"

    cap = (
        f"<b>{G['graph']} {sc('My Stats')}</b>\n"
        f"{G['div_eq']}\n"
        f"<b>{sc('Account')}</b>\n"
        f"{bullet('Name',       u.get('name', '—'))}\n"
        f"{bullet('User ID',    uid)}\n"
        f"{bullet('Joined',     fmt_ts(u.get('joined')))}\n"
        f"{bullet('KYC',        'Verified' if u.get('kyc') else 'No')}\n"
        f"{G['div']}\n"
        f"<b>{sc('Plan')}</b>\n"
        f"{bullet('Current Plan',  p['name'])}\n"
        f"{bullet('Plan Expires',  expires_txt)}\n"
        f"{bullet('RAM Limit',     str(p['ram']) + ' MB')}\n"
        f"{bullet('Auto Restart',  'Yes' if p['auto_restart'] else 'No')}\n"
        f"{G['div']}\n"
        f"<b>{sc('Bots')}</b>\n"
        f"{bullet('Total Bots',    len(bots))}\n"
        f"{bullet('Running',       running)}\n"
        f"{bullet('Stopped',       stopped)}\n"
        f"{bullet('Slots Used',    str(len(bots)) + ' / ' + str(user_max_bots(u)))}\n"
        f"{bullet('Storage Used',  fmt_bytes(storage_size))}\n"
        f"{G['div']}\n"
        f"<b>{sc('Payments')}</b>\n"
        f"{bullet('Total Payments', len(pays))}\n"
        f"{bullet('Last Payment',   fmt_ts(last_pay) if last_pay else '—')}\n"
        f"{bullet('Wallet Balance', '{}$'.format(u.get('wallet', 0)))}\n"
        f"{G['div']}\n"
        f"<b>{sc('Other')}</b>\n"
        f"{bullet('Referrals',     u.get('ref_count', 0))}\n"
        f"{bullet('Bonus Slots',   u.get('bot_slots_bonus', 0))}\n"
        f"{bullet('Free Trial',    'Used' if u.get('trial_used') else 'Available')}\n"
        f"{bullet('Open Tickets',  open_tickets)}\n"
        f"{bullet('Closed Tickets', closed_tickets)}\n"
        f"{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["stats"], cap, back_main_kb(), call=call)


def start_coupon_flow(call: types.CallbackQuery) -> None:
    USER_STATES[call.from_user.id] = {"flow": "await_coupon"}
    bot.send_message(
        call.message.chat.id,
        f"{G['key']} {sc('Send your coupon code')} (Tᴇxᴛ Oɴʟʏ). /cancel {sc('to abort')}.",
    )


def start_wallet_topup(call: types.CallbackQuery) -> None:
    USER_STATES[call.from_user.id] = {"flow": "await_topup_proof"}
    bot.send_message(
        call.message.chat.id,
        f"{G['plus']} {sc('Send a screenshot of your top-up payment')}.\n"
        f"{sc('Include the amount in the caption')}, e.g.  <code>200</code>.",
        parse_mode="HTML",
    )


def start_wallet_gift(call: types.CallbackQuery) -> None:
    USER_STATES[call.from_user.id] = {"flow": "await_gift_target"}
    bot.send_message(
        call.message.chat.id,
        f"{G['spark']} {sc('Send the user id of the person you want to gift your plan to')}.",
    )


# ═════════════════════════════════════════════════════════════════
# 19. BOT MANAGEMENT VIEWS
# ═════════════════════════════════════════════════════════════════

def render_bot_view(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    st = child_status(bot_id, b)
    # surface the most recent crash if the bot is stopped
    err_block = ""
    if not st["running"]:
        rc = b.get("last_exit_code")
        last_err = (b.get("last_error") or "").strip()
        if last_err or (rc not in (None, 0)):
            head = f"{G['no']} {sc('Last error')}"
            if rc not in (None, 0):
                head += f"  (exit {rc})"
            err_block = (
                f"\n{G['div']}\n"
                f"<b>{head}</b>\n"
                f"<pre>{esc(last_err or '(no log captured)')[:900]}</pre>"
            )
    appr = (b.get("approval_status") or "").lower()
    if appr == "pending":
        status_lbl = "Pending approval"
    elif appr == "rejected":
        status_lbl = "Rejected"
    elif st["running"]:
        status_lbl = "Running"
    elif b.get("status") == "crashed":
        status_lbl = "Crashed"
    else:
        status_lbl = "Stopped"
    cap = (
        f"<b>{G['diamond']} {esc(b['name'])}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Status',  status_lbl)}\n"
        f"{bullet('Kind',    st['kind'] or '—')}\n"
        f"{bullet('PID',     '••••' if st['pid'] else '—')}\n"
        f"{bullet('Uptime',  fmt_dur(st['uptimeMs']))}\n"
        f"{bullet('Size',    fmt_bytes(st['sizeBytes']))}\n"
        f"{bullet('CPU',     '{:.1f}%'.format(st['cpuPct']))}\n"
        f"{bullet('Memory',  fmt_bytes(st['memBytes']))}\n"
        f"{bullet('Created', fmt_ts(b.get('created')))}"
        f"{err_block}\n"
        f"{G['div']}{FOOTER}"
    )
    owner_doc = db_load()["users"].get(str(b["owner"])) or {}
    is_premium = owner_doc.get("plan", "free") != "free" and user_plan_active(owner_doc)
    # Surface the active tunnel URL in the caption when one is open
    tun = TUNNELS.get(bot_id)
    if tun and tun.get("proc") and tun["proc"].poll() is None and tun.get("url"):
        cap = (
            cap[: -len(FOOTER)]
            + f"\n{G['div']}\n"
            + f"{bullet('Public URL', tun['url'])}\n"
            + f"{bullet('Port',       tun.get('port', '—'))}"
            + FOOTER
        )
    show_menu(call.message.chat.id, PHOTOS["bot"], cap,
              bot_actions_kb(bot_id, st["running"], premium=is_premium), call=call)


def action_bot_start(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    loading(call, "Starting bot")
    res = start_child(b)
    ack(call, "Started" if res["ok"] else f"Err: {res.get('error')}")
    render_bot_view(call, bot_id)


def action_bot_stop(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    loading(call, "Stopping bot")
    stop_child(bot_id, manual=True)
    ack(call, "Stopped")
    render_bot_view(call, bot_id)


def action_bot_restart(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    loading(call, "Restarting bot")
    res = restart_child(b)
    ack(call, "Restarted" if res["ok"] else f"Err: {res.get('error')}")
    render_bot_view(call, bot_id)


def action_bot_logs(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    info = RUNNING.get(bot_id)
    log = info["log"] if info else []
    last = log[-MAX_LOG_SEND:] if log else [f"({sc('no logs yet')})"]
    txt = (
        f"<b>{G['bolt']} {sc('Live Logs')} — {esc(b['name'])}</b>\n"
        f"{G['div_eq']}\n<pre>"
        + esc("\n".join(last))[:3500]
        + f"</pre>\n{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        Btn(
            f"{G['refresh']}  {sc('Refresh Logs')}",
            callback_data=f"bot_logs_{bot_id}",
        ),
        Btn(
            f"{G['back']}  {sc('Back')}",
            callback_data=f"bot_view_{bot_id}",
        ),
    )
    show_text(call.message.chat.id, txt, kb, call=call)


def action_bot_info(call: types.CallbackQuery, bot_id: str) -> None:
    render_bot_view(call, bot_id)


def render_bot_delete_confirm(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    cap = (
        f"<b>{G['no']} {sc('Delete Bot')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Bot', b['name'])}\n\n"
        f"{G['warn']}  <b>{sc('Choose delete type')}:</b>\n\n"
        f"{G['bullet']} <b>{sc('Delete Bot Files')}</b> — {sc('removes files and keys only')}\n"
        f"{G['bullet']} <b>{sc('Delete All Data')}</b> — {sc('removes files keys AND GitHub backup')}\n\n"
        f"{sc('This cannot be undone')}.{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        Btn(
            f"{G['trash']}  {sc('Delete Bot Files')}",
            callback_data=f"bot_delfiles_{bot_id}"),
        Btn(
            f"{G['no']}  {sc('Delete All Data')}",
            callback_data=f"bot_delall_{bot_id}"),
        Btn(
            f"{G['back']}  {sc('Cancel')}",
            callback_data=f"bot_view_{bot_id}"),
    )
    show_menu(call.message.chat.id, PHOTOS["bot"], cap, kb, call=call)


def render_bot_delfiles_confirm(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    cap = (
        f"<b>{G['trash']} {sc('Delete Bot Files')} — {esc(b['name'])}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Removes encrypted files and keys only.')}\n"
        f"{sc('GitHub backup will NOT be deleted.')}\n\n"
        f"{sc('Are you sure?')}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["bot"], cap,
              confirm_kb(f"bot_delfilesyes_{bot_id}", f"bot_view_{bot_id}", "Yes Delete", "Cancel"),
              call=call)


def render_bot_delall_confirm(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    cap = (
        f"<b>{G['no']} {sc('Delete All Data')} — {esc(b['name'])}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Removes files, keys AND deletes from GitHub.')}\n"
        f"{G['warn']} <b>{sc('Everything will be permanently gone.')}</b>\n\n"
        f"{sc('Are you sure?')}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["bot"], cap,
              confirm_kb(f"bot_delalyes_{bot_id}", f"bot_view_{bot_id}", "Yes Delete All", "Cancel"),
              call=call)


def action_bot_delete(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    loading(call, "Deleting bot")
    stop_child(bot_id, manual=True)
    for f in b.get("enc_files") or []:
        try:
            Path(f["enc_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        KEYRING.remove(f["key_id"])
    rmrf(b.get("dir") or "")
    delete_bot_doc(bot_id)
    ack(call, "Deleted")
    audit(call.from_user.id, "bot_delete", f"bot={bot_id}")
    render_bots_menu(call)


def action_bot_delfiles(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    loading(call, "Deleting bot files")
    stop_child(bot_id, manual=True)
    for f in b.get("enc_files") or []:
        try:
            Path(f["enc_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        KEYRING.remove(f["key_id"])
    rmrf(b.get("dir") or "")
    delete_bot_doc(bot_id)
    ack(call, "Bot files deleted")
    audit(call.from_user.id, "bot_delfiles", f"bot={bot_id}")
    render_bots_menu(call)


def action_bot_delall(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    loading(call, "Deleting all data")
    stop_child(bot_id, manual=True)
    for f in b.get("enc_files") or []:
        try:
            Path(f["enc_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        KEYRING.remove(f["key_id"])
    rmrf(b.get("dir") or "")
    threading.Thread(target=_gh_delete_bot_files, args=(b,), daemon=True).start()
    delete_bot_doc(bot_id)
    ack(call, "All data deleted")
    audit(call.from_user.id, "bot_delall", f"bot={bot_id}")
    render_bots_menu(call)


def action_bot_clone(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    u = db_load()["users"][str(call.from_user.id)]
    if len(list_user_bots(call.from_user.id)) >= user_max_bots(u):
        ack(call, "Slot limit reached"); return
    loading(call, "Cloning bot")
    new_id = secrets.token_hex(8)
    new_dir = DIRS["sandbox"] / f"{call.from_user.id}_{new_id}"
    new_dir.mkdir(parents=True, exist_ok=True)
    new_doc = {
        "_id": new_id, "owner": call.from_user.id,
        "name": f"{b['name']}_clone",
        "dir": str(new_dir), "created": ts_iso(),
        "enc_files": [], "env": dict(b.get("env") or {}), "status": "stopped",
    }
    for f in b.get("enc_files") or []:
        key = KEYRING.fetch(f["key_id"])
        if not key:
            continue
        try:
            plain = read_encrypted(Path(f["enc_path"]), key)
        except InvalidToken:
            continue
        kid, k2, cipher = encrypt_file(plain)
        rel = f"{call.from_user.id}/{int(time.time())}_{safe_name(f['filename'])}.enc"
        out = DIRS["encfiles"] / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(cipher)
        meta = dict(f); meta.update({"clone_of": b["_id"], "stored_at": str(out)})
        KEYRING.store(kid, k2, meta)
        new_doc["enc_files"].append({
            "key_id": kid, "enc_path": str(out),
            "filename": f["filename"], "rel_path": f.get("rel_path") or f["filename"],
        })
    save_bot(new_doc)
    audit(call.from_user.id, "bot_clone", f"src={bot_id} dst={new_id}")
    ack(call, "Cloned")
    render_bots_menu(call)


def action_bot_download(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    files = b.get("enc_files") or []
    if not files:
        ack(call, "No files"); return
    loading(call, "Preparing download")
    out = Path(tempfile.gettempdir()) / f"dl_{b['_id']}.zip"
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                key = KEYRING.fetch(f["key_id"])
                if not key:
                    continue
                try:
                    plain = read_encrypted(Path(f["enc_path"]), key)
                except Exception:
                    continue
                z.writestr(f.get("rel_path") or f["filename"], plain)
        with open(out, "rb") as fh:
            bot.send_document(
                call.message.chat.id, fh,
                caption=f"{G['download']} {sc('Bot files')} — {esc(b['name'])}",
                visible_file_name=f"{safe_name(b['name'])}.zip",
            )
        ack(call, "Sent")
    except Exception as e:
        ack(call, f"Error: {e}")
    finally:
        try:
            out.unlink()
        except Exception:
            pass
    # Restore the bot view so the loading caption isn't left on screen.
    try:
        render_bot_view(call, bot_id)
    except Exception:
        pass


def render_env_menu(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    env = b.get("env") or {}
    rows = "\n".join(f"{bullet(k, v)}" for k, v in env.items()) or f"<i>{sc('no variables yet')}</i>"
    cap = (
        f"<b>{G['settings']} {sc('Env Vars')} — {esc(b['name'])}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(
        f"{G['plus']}  {sc('Add Variable')}", callback_data=f"env_add_{bot_id}"))
    for k in env:
        kb.add(Btn(
            f"{G['no']}  {sc('Delete')} {k}", callback_data=f"env_del_{bot_id}_{k}"))
    kb.add(Btn(
        f"{G['back']}  {sc('Bot')}", callback_data=f"bot_view_{bot_id}"))
    show_menu(call.message.chat.id, PHOTOS["bot"], cap, kb, call=call)


def start_env_add(call: types.CallbackQuery, bot_id: str) -> None:
    USER_STATES[call.from_user.id] = {"flow": "await_env_kv", "bot_id": bot_id}
    bot.send_message(
        call.message.chat.id,
        f"{G['plus']} {sc('Send the variable as')} <code>KEY=VALUE</code>.\n"
        f"/cancel {sc('to abort')}.",
        parse_mode="HTML",
    )


def start_tunnel_flow(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    owner_doc = db_load()["users"].get(str(b["owner"])) or {}
    if owner_doc.get("plan", "free") == "free" or not user_plan_active(owner_doc):
        bot.send_message(
            call.message.chat.id,
            f"{G['no']} <b>{sc('Public URL is a premium feature')}.</b>\n"
            f"{sc('Upgrade your plan to unlock cloudflared tunnels')}.{FOOTER}",
            parse_mode="HTML",
        )
        return

    # Toggle: if already running, stop it.
    cur = TUNNELS.get(bot_id)
    if cur and cur.get("proc") and cur["proc"].poll() is None:
        _stop_tunnel(bot_id)
        bot.send_message(
            call.message.chat.id,
            f"{G['ok']} {sc('Public URL closed')}.{FOOTER}",
            parse_mode="HTML",
        )
        try:
            render_bot_view(call, bot_id)
        except Exception:
            pass
        return

    USER_STATES[call.from_user.id] = {"flow": "await_tunnel_port", "bot_id": bot_id}
    bot.send_message(
        call.message.chat.id,
        f"<b>{G['cloud']} {sc('Open a Public URL')}</b>\n"
        f"{G['div']}\n"
        f"{sc('Send the local port your bot is listening on')} "
        f"({sc('e.g.')} <code>8080</code>).\n"
        f"{sc('A random')} <code>*.trycloudflare.com</code> {sc('URL will proxy to that port')}.\n\n"
        f"{sc('If the port is already in use by another tunnel, pick a different one')}.\n"
        f"/cancel {sc('to abort')}.",
        parse_mode="HTML",
    )


def _handle_tunnel_port(m: types.Message, st: Dict[str, Any]) -> None:
    USER_STATES.pop(m.from_user.id, None)
    txt = (m.text or "").strip()
    if not txt.isdigit():
        bot.reply_to(m, f"{G['no']} {sc('Port must be a number')}.")
        return
    port = int(txt)
    if not (1 <= port <= 65535):
        bot.reply_to(m, f"{G['no']} {sc('Port must be between 1 and 65535')}.")
        return
    b = find_bot(st["bot_id"])
    if not b:
        bot.reply_to(m, f"{G['no']} {sc('Bot not found')}."); return
    if b["owner"] != m.from_user.id and not is_admin(m.from_user.id):
        bot.reply_to(m, f"{G['no']} {sc('Not yours')}."); return

    # Refuse if any other bot already holds this port via a tunnel
    for other_id, rec in list(TUNNELS.items()):
        if other_id == b["_id"]:
            continue
        if rec.get("port") == port and rec.get("proc") and rec["proc"].poll() is None:
            bot.reply_to(
                m,
                f"{G['no']} <b>{sc('Port')} {port} {sc('is already in use by another tunnel')}.</b>\n"
                f"{sc('Please pick a different port')}.",
                parse_mode="HTML",
            )
            return

    status = bot.reply_to(
        m,
        f"{G['refresh']} {sc('Opening tunnel on port')} <code>{port}</code> ...",
        parse_mode="HTML",
    )
    res = _start_tunnel(b["_id"], port)
    if not res.get("ok"):
        try:
            bot.edit_message_text(
                f"{G['no']} <b>{sc('Tunnel failed')}.</b>\n"
                f"<code>{esc(res.get('error', 'unknown error'))}</code>",
                chat_id=status.chat.id, message_id=status.message_id,
                parse_mode="HTML",
            )
        except Exception:
            pass
        return
    url = res.get("url") or "(provisioning…)"
    try:
        bot.edit_message_text(
            f"{G['ok']} <b>{sc('Public URL is live')}</b>\n"
            f"{G['div']}\n"
            f"{bullet('URL',  url)}\n"
            f"{bullet('Port', port)}\n\n"
            f"{sc('Tap the bot menu Public URL button again to stop it')}.{FOOTER}",
            chat_id=status.chat.id, message_id=status.message_id,
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception:
        pass


def start_pip_install_flow(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    USER_STATES[call.from_user.id] = {"flow": "await_pip_install", "bot_id": bot_id}
    bot.send_message(
        call.message.chat.id,
        f"<b>{G['download']} {sc('Install Python package')}</b>\n"
        f"{G['div']}\n"
        f"{sc('Send one or more package names separated by spaces')}.\n"
        f"{sc('Examples')}:\n"
        f"  <code>requests</code>\n"
        f"  <code>numpy pandas</code>\n"
        f"  <code>flask==3.0.0</code>\n\n"
        f"/cancel {sc('to abort')}.",
        parse_mode="HTML",
    )


def action_env_delete(call: types.CallbackQuery, bot_id: str, key: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    if b["owner"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    env = b.get("env") or {}
    env.pop(key, None)
    b["env"] = env
    save_bot(b)
    ack(call, "Deleted")
    render_env_menu(call, bot_id)


def render_cron(call: types.CallbackQuery, bot_id: str) -> None:
    b = find_bot(bot_id)
    if not b:
        ack(call, "Not found"); return
    cron = b.get("cron") or {}
    cap = (
        f"<b>{G['cog']} {sc('Cron')} — {esc(b['name'])}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Restart every', cron.get('restart_hours', '—'))}\n"
        f"{bullet('Backup every',  cron.get('backup_hours', '—'))}\n"
        f"{G['div']}\n"
        f"{sc('Send a message like')} <code>restart=6 backup=12</code> {sc('to set hours')}.\n"
        f"{sc('Send')} <code>off</code> {sc('to disable cron')}.{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_cron", "bot_id": bot_id}
    show_menu(call.message.chat.id, PHOTOS["bot"], cap,
              back_kb(f"bot_view_{bot_id}", "Back"), call=call)


# ═════════════════════════════════════════════════════════════════
# 20. ADMIN PANEL  RENDERS
# ═════════════════════════════════════════════════════════════════

def render_admin(call: types.CallbackQuery) -> None:
    if not admin_only_call(call, "view_stats"):
        return
    role = admin_role(call.from_user.id)
    cap = (
        f"<b>{G['shield']} {sc('Admin Panel')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Role',  role)}\n"
        f"{bullet('Users', len(db_load()['users']))}\n"
        f"{bullet('Bots',  len(db_load()['bots']))}\n"
        f"{bullet('Run',   sum(1 for x in RUNNING.values() if x['proc'].poll() is None))}\n"
        f"{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, admin_kb(), call=call)


def render_admin_subroute(call: types.CallbackQuery, data: str) -> None:
    if data == "adm_stats":
        return render_adm_stats(call)
    if data == "adm_users":
        return render_adm_users(call)
    if data == "adm_allbots":
        return render_adm_allbots(call)
    if data == "adm_payments":
        return render_adm_payments(call)
    if data == "adm_broadcast":
        return render_adm_broadcast(call)
    if data == "adm_ban":
        return render_adm_ban(call)
    if data == "adm_giveplan":
        return render_adm_giveplan(call)
    if data == "adm_approve":
        return render_adm_payments(call)
    if data == "adm_coupons":
        return render_adm_coupons(call)
    if data == "adm_tickets":
        return render_adm_tickets(call)
    if data == "adm_admins":
        return render_adm_admins(call)
    if data == "adm_audit":
        return render_adm_audit(call)
    if data == "adm_github":
        return render_adm_github(call)
    if data == "adm_security":
        return render_adm_security(call)
    if data == "adm_maint":
        return render_adm_maintenance(call)
    if data == "adm_maint_toggle":
        cur = bool(get_setting("maintenance", False))
        set_setting("maintenance", not cur)
        audit(call.from_user.id, "maintenance_toggle", f"now={not cur}")
        ack(call, f"Maintenance: {'ON' if not cur else 'OFF'}")
        return render_adm_maintenance(call)
    if data == "adm_settings":
        return render_adm_settings(call)
    if data == "adm_approval_toggle":
        cur = approval_required()
        set_approval_required(not cur)
        audit(call.from_user.id, "approval_toggle", f"now={not cur}")
        ack(call, f"Approval Mode: {'ON' if not cur else 'OFF'}")
        return render_admin(call)
    if data == "adm_pending":
        return render_adm_pending(call)
    if data == "adm_photos":
        return render_adm_photos(call)
    if data.startswith("adm_photo_"):
        key = data[len("adm_photo_"):]
        return render_adm_photo_one(call, key)
    if data == "adm_force_backup":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        ack(call, "Backing up…")
        def _bg() -> None:
            try:
                ok1 = gh_sync_user_data()
                pushed = 0
                for b in db_load()["bots"].values():
                    if (b.get("approval_status") in (None, "approved")) and b.get("enc_files"):
                        try:
                            _gh_sync_bot_files(b)
                            b["gh_synced_at"] = int(time.time())
                            save_bot(b)
                            pushed += 1
                        except Exception:
                            pass
                try:
                    bot.send_message(
                        call.from_user.id,
                        f"<b>{G['ok']} {sc('Force backup done')}</b>\n"
                        f"{bullet('user_data.json', 'OK' if ok1 else 'FAIL')}\n"
                        f"{bullet('Bots pushed', pushed)}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            except Exception as e:
                try:
                    bot.send_message(call.from_user.id,
                                     f"{G['no']} {sc('Backup error')}: <code>{esc(e)}</code>",
                                     parse_mode="HTML")
                except Exception:
                    pass
        threading.Thread(target=_bg, daemon=True).start()
        return

    # ── advanced settings ──────────────────────────────────────────
    if data == "adm_set_sysinfo":
        return render_adm_sysinfo(call)
    if data == "adm_set_plans":
        return render_adm_plans(call)
    if data == "adm_set_plans_reset":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        s = settings_load()
        for k in list(s.keys()):
            if k.startswith("plan_max_bots_"):
                s.pop(k, None)
        settings_save(s)
        audit(call.from_user.id, "plans_reset", "")
        ack(call, "Plans reset")
        return render_adm_plans(call)
    if data.startswith("adm_set_plan_show_"):
        ack(call, "Use ➕ / ➖ to adjust"); return
    if data.startswith("adm_set_plan_inc_") or data.startswith("adm_set_plan_dec_"):
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        inc = data.startswith("adm_set_plan_inc_")
        key = data.split("_")[-1]
        if key not in PLAN_LIMITS:
            ack(call, "Unknown plan"); return
        cur = int(get_setting(f"plan_max_bots_{key}",
                              PLAN_LIMITS[key]["max_bots"]))
        cur = max(1, cur + (1 if inc else -1))
        set_setting(f"plan_max_bots_{key}", cur)
        audit(call.from_user.id, "plan_edit", f"{key} max_bots={cur}")
        ack(call, f"{PLAN_LIMITS[key]['name']}: {cur}")
        return render_adm_plans(call)
    if data == "adm_set_reload":
        if not is_admin(call.from_user.id):
            ack(call, "No permission"); return
        cache_clear_all()
        audit(call.from_user.id, "reload_caches", "")
        ack(call, "Caches dropped — next read = disk")
        return render_adm_settings(call)
    if data == "adm_set_brand":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        USER_STATES[call.from_user.id] = {"flow": "await_set_brand"}
        bot.send_message(call.message.chat.id,
                         f"{G['settings']} {sc('Send the new brand tag')} "
                         f"(<i>{sc('plain text, will appear in headers')}</i>):",
                         parse_mode="HTML")
        return
    if data == "adm_set_announce":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        USER_STATES[call.from_user.id] = {"flow": "await_set_announce"}
        bot.send_message(call.message.chat.id,
                         f"{G['broadcast']} {sc('Send the announce channel handle')} "
                         f"(<code>@channel</code> or <code>-</code> {sc('to clear')}):",
                         parse_mode="HTML")
        return
    if data == "adm_set_owner":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        USER_STATES[call.from_user.id] = {"flow": "await_set_owner"}
        bot.send_message(call.message.chat.id,
                         f"{G['shield']} {sc('Send the new owner numeric Telegram ID')}.\n"
                         f"<i>{sc('You will lose owner rights after this')}.</i>",
                         parse_mode="HTML")
        return
    if data == "adm_set_restart_all":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        return render_adm_confirm(call, "adm_set_restart_all", "Restart all running bots")
    if data == "adm_set_restart_all_yes":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        ack(call, "Restarting…")
        def _rb() -> None:
            ok, fail = _do_restart_all_bots(call.from_user.id)
            try:
                bot.send_message(call.from_user.id,
                                 f"{G['ok']} {sc('Restart-all done')}: "
                                 f"{ok} ok, {fail} fail.")
            except Exception:
                pass
        threading.Thread(target=_rb, daemon=True).start()
        return
    if data == "adm_set_stop_all":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        return render_adm_confirm(call, "adm_set_stop_all", "Stop every running bot")
    if data == "adm_set_stop_all_yes":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        ack(call, "Stopping…")
        def _sb() -> None:
            n = _do_stop_all_bots(call.from_user.id)
            try:
                bot.send_message(call.from_user.id,
                                 f"{G['ok']} {sc('Stopped')} {n} {sc('bot(s)')}.")
            except Exception:
                pass
        threading.Thread(target=_sb, daemon=True).start()
        return
    if data == "adm_set_clean_orphans":
        if not is_admin(call.from_user.id):
            ack(call, "No permission"); return
        ack(call, "Scanning…")
        def _co() -> None:
            dirs, files = _do_clean_orphans()
            audit(call.from_user.id, "clean_orphans",
                  f"sandboxes={dirs} files={files}")
            try:
                bot.send_message(call.from_user.id,
                                 f"{G['ok']} {sc('Cleaned')}: "
                                 f"{dirs} {sc('sandbox(es)')}, "
                                 f"{files} {sc('orphan file(s)')}.")
            except Exception:
                pass
        threading.Thread(target=_co, daemon=True).start()
        return
    if data == "adm_set_export":
        if not is_owner(call.from_user.id):
            ack(call, "Owner only"); return
        ack(call, "Packing export…")
        def _ex() -> None:
            try:
                p = _do_export_data(call.from_user.id)
                with p.open("rb") as fh:
                    bot.send_document(
                        call.from_user.id, fh,
                        caption=f"{G['ok']} {sc('Encrypted DB export')} "
                                f"({p.stat().st_size // 1024} KB)")
            except Exception as e:
                try:
                    bot.send_message(
                        call.from_user.id,
                        f"{G['no']} {sc('Export error')}: <code>{esc(e)}</code>",
                        parse_mode="HTML")
                except Exception:
                    pass
        threading.Thread(target=_ex, daemon=True).start()
        return

    ack(call, "?")


def render_adm_stats(call: types.CallbackQuery) -> None:
    d = db_load()
    users = d["users"]
    bots  = d["bots"]
    pays  = d["payments"]
    revenue = sum(p.get("amount", 0) for p in pays if p.get("status") == "approved")
    today_str = now_utc().strftime("%Y-%m-%d")
    new_today = sum(1 for u in users.values() if str(u.get("joined", "")).startswith(today_str))
    week_ago = now_utc() - timedelta(days=7)
    new_week = 0
    for u in users.values():
        try:
            if datetime.fromisoformat(str(u.get("joined")).replace("Z", "+00:00")) >= week_ago:
                new_week += 1
        except Exception:
            pass
    plan_counts: Dict[str, int] = defaultdict(int)
    for u in users.values():
        plan_counts[u.get("plan", "free")] += 1
    rss = 0
    if psutil is not None:
        try:
            rss = psutil.Process(os.getpid()).memory_info().rss
        except Exception:
            pass
    storage_size = 0
    for root, _, files in os.walk(BASE_DIR / "storage"):
        for f in files:
            try:
                storage_size += (Path(root) / f).stat().st_size
            except OSError:
                pass

    cap = (
        f"<b>{G['graph']} {sc('System Stats')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Total users',  len(users))}\n"
        f"{bullet('New today',    new_today)}\n"
        f"{bullet('New this week', new_week)}\n"
        f"{bullet('Total bots',   len(bots))}\n"
        f"{bullet('Bots running', sum(1 for x in RUNNING.values() if x['proc'].poll() is None))}\n"
        f"{bullet('Revenue',      '{}$'.format(revenue))}\n"
        f"{bullet('Storage',      fmt_bytes(storage_size))}\n"
        f"{bullet('Panel RSS',    fmt_bytes(rss))}\n"
        f"{bullet('Uptime',       fmt_dur(int(time.time() * 1000) - START_TS))}\n"
        f"{G['div']}\n"
        + "\n".join(f"{bullet(PLAN_LIMITS[p]['name'], n)}" for p, n in plan_counts.items())
        + FOOTER
    )
    show_menu(call.message.chat.id, PHOTOS["stats"], cap, back_admin_kb(), call=call)


def render_adm_users(call: types.CallbackQuery) -> None:
    d = db_load()["users"]
    items = sorted(d.values(), key=lambda u: u.get("joined", ""), reverse=True)[:20]
    rows = "\n".join(
        f"{G['bullet']} <code>{u['_id']}</code> — {esc(u.get('name'))} "
        f"(@{esc(u.get('username') or '—')}) "
        f"{G['bullet']} <i>{esc(PLAN_LIMITS.get(u.get('plan'), {}).get('name', u.get('plan')))}</i>"
        for u in items
    ) or f"<i>{sc('no users yet')}</i>"
    cap = (
        f"<b>{G['users']} {sc('Recent Users')} ({len(d)} {sc('total')})</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}\n"
        f"{sc('Send a numeric user id to look one up')}.{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_admin_finduser"}
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)


def render_adm_allbots(call: types.CallbackQuery) -> None:
    d = db_load()["bots"]
    items = list(d.values())[:25]
    rows = "\n".join(
        f"{G['bullet']} <code>{b['_id']}</code> — {esc(b['name'])} "
        f"{G['bullet']} <i>uid {b['owner']}</i> "
        f"{G['bullet']} {'run' if b['_id'] in RUNNING and RUNNING[b['_id']]['proc'].poll() is None else 'idle'}"
        for b in items
    ) or f"<i>{sc('no bots')}</i>"
    cap = (
        f"<b>{G['diamond']} {sc('All Bots')} ({len(d)})</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)


def render_adm_payments(call: types.CallbackQuery) -> None:
    d = db_load()
    pays = [p for p in d["payments"] if p.get("status") == "pending"][-15:]
    rows = "\n".join(
        f"{G['bullet']} <code>{p['id']}</code> {G['bullet']} uid {p['uid']} "
        f"{G['bullet']} {esc(p.get('plan', '—'))} {G['bullet']} {esc(p.get('method'))}"
        for p in pays
    ) or f"<i>{sc('no pending payments')}</i>"
    cap = (
        f"<b>{G['wallet']} {sc('Pending Payments')}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}\n"
        f"{sc('Tap a payment id from the inbox notification to approve or reject')}.{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)


def render_adm_broadcast(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['broadcast']} {sc('Broadcast')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Send the message text now')}.\n"
        f"<b>{sc('Optional prefix')}:</b>\n"
        f"  <code>plan:pro</code> — {sc('only pro users')}\n"
        f"  <code>plan:free</code> — {sc('only free users')}\n"
        f"  <code>at:YYYY-MM-DD HH:MM</code> — {sc('schedule')}\n"
        f"  {sc('Otherwise message goes to everyone now')}.{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_broadcast"}
    show_menu(call.message.chat.id, PHOTOS["broadcast"], cap, back_admin_kb(), call=call)


def render_adm_ban(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['no']} {sc('Ban / Unban')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Send')} <code>ban &lt;user_id&gt; &lt;reason&gt;</code>\n"
        f"{sc('Send')} <code>unban &lt;user_id&gt;</code>{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_ban_cmd"}
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)


def render_adm_giveplan(call: types.CallbackQuery) -> None:
    cap = (
        f"<b>{G['plus']} {sc('Give Plan')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Send')} <code>&lt;user_id&gt; &lt;plan&gt; [days]</code>\n"
        f"{sc('Plans')}: {', '.join(PLAN_LIMITS.keys())}{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_giveplan"}
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)


def render_adm_coupons(call: types.CallbackQuery) -> None:
    d = db_load()["coupons"]
    rows = "\n".join(
        f"{G['bullet']} <code>{esc(code)}</code> — {esc(c.get('percent'))}% "
        f"{G['bullet']} {esc(c.get('uses_left'))} {sc('uses left')}"
        for code, c in d.items()
    ) or f"<i>{sc('no coupons yet')}</i>"
    cap = (
        f"<b>{G['key']} {sc('Coupons')}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}\n"
        f"{sc('Send')} <code>add CODE PERCENT USES</code> {sc('to create')}.\n"
        f"{sc('Send')} <code>del CODE</code> {sc('to remove')}.{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_coupon_admin"}
    show_menu(call.message.chat.id, PHOTOS["coupon"], cap, back_admin_kb(), call=call)


def render_adm_tickets(call: types.CallbackQuery) -> None:
    d = db_load()["tickets"]
    open_t = [t for t in d.values() if t.get("status") == "open"][-15:]
    rows = "\n".join(
        f"{G['bullet']} <code>{t['id']}</code> uid {t['uid']} — {esc(t.get('subject'))[:40]}"
        for t in open_t
    ) or f"<i>{sc('no open tickets')}</i>"
    cap = (
        f"<b>{G['ticket']} {sc('Open Tickets')}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    for t in open_t:
        kb.add(Btn(
            f"{G['eye']}  #{t['id']}", callback_data=f"ticket_view_{t['id']}"))
    kb.add(Btn(
        f"{G['back']}  {sc('Admin')}", callback_data="menu_admin"))
    show_menu(call.message.chat.id, PHOTOS["ticket"], cap, kb, call=call)


def render_adm_admins(call: types.CallbackQuery) -> None:
    if not is_owner(call.from_user.id):
        ack(call, "Owner only"); return
    d = db_load()["admins"]
    rows = "\n".join(
        f"{G['bullet']} <code>{uid}</code> — {esc(a.get('role'))}"
        for uid, a in d.items()
    ) or f"<i>{sc('no extra admins yet')}</i>"
    cap = (
        f"<b>{G['shield']} {sc('Admins')}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}\n"
        f"{sc('Send')} <code>add &lt;uid&gt; &lt;role&gt;</code>\n"
        f"  {sc('Roles')}: <code>view-only</code>, <code>manage-users</code>, <code>full-access</code>\n"
        f"{sc('Send')} <code>del &lt;uid&gt;</code>{FOOTER}"
    )
    USER_STATES[call.from_user.id] = {"flow": "await_admin_admins"}
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)


def render_adm_audit(call: types.CallbackQuery) -> None:
    d = db_load()["audit"][-25:]
    rows = "\n".join(
        f"{G['bullet']} {esc(a['ts'][11:19])} uid {a['uid']} → {esc(a['action'])} {esc(a.get('detail', ''))[:60]}"
        for a in reversed(d)
    ) or f"<i>{sc('no audit entries yet')}</i>"
    cap = (
        f"<b>{G['eye']} {sc('Recent Audit')}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["security"], cap, back_admin_kb(), call=call)


def render_adm_pending(call: types.CallbackQuery) -> None:
    """List of bot uploads waiting for approval. Each row links back
    to a quick approve / reject pair for that upload."""
    if not admin_only_call(call, "approve_payment"):
        return
    items = pending_list()
    if not items:
        cap = (
            f"<b>{G['eye']} {sc('Pending Uploads')}</b>\n"
            f"{G['div_eq']}\n<i>{sc('Inbox is empty — nothing waiting for approval')}.</i>\n"
            f"{G['div']}{FOOTER}"
        )
        show_menu(call.message.chat.id, PHOTOS["admin"], cap, back_admin_kb(), call=call)
        return
    rows = []
    kb = types.InlineKeyboardMarkup(row_width=2)
    for bid, info in items[:15]:
        b = find_bot(bid)
        nm = (b or {}).get("name") or info.get("file_name") or bid
        rows.append(
            f"{G['bullet']} <code>{esc(bid)}</code> — {esc(nm)} "
            f"{G['bullet']} uid {info.get('user_id')} "
            f"{G['bullet']} {fmt_bytes(info.get('size', 0))}"
        )
        kb.add(
            Btn(f"{G['ok']}  {sc('OK')} {esc(nm)[:18]}",
                                       callback_data=f"appr_ok_{bid}"),
            Btn(f"{G['no']}  {sc('No')} {esc(nm)[:18]}",
                                       callback_data=f"appr_no_{bid}"),
        )
    kb.add(Btn(
        f"{G['back']}  {sc('Admin')}", callback_data="menu_admin"))
    cap = (
        f"<b>{G['eye']} {sc('Pending Uploads')} ({len(items)})</b>\n"
        f"{G['div_eq']}\n" + "\n".join(rows) + f"\n{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, kb, call=call)


def render_adm_photos(call: types.CallbackQuery) -> None:
    """List every menu photo key. Tapping one prompts the admin to
    send a fresh photo, which replaces that banner."""
    if not is_owner(call.from_user.id) and not admin_can(call.from_user.id, "manage_admins"):
        # Allow only owner / full-access admins to change branding.
        ack(call, "Owner / full-access only.")
        return
    cap = (
        f"<b>{G['upload']} {sc('Menu Photos')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Tap any menu below, then send a photo to replace its banner')}.\n"
        f"{sc('Photos are saved locally and synced to GitHub on next backup')}.\n"
        f"{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    items = sorted(PHOTO_KEYS_FRIENDLY.items())
    pairs: List[types.InlineKeyboardButton] = []
    for key, label in items:
        if key not in _PHOTO_SPECS:
            continue
        pairs.append(Btn(
            f"{G['cog']}  {sc(label)}", callback_data=f"adm_photo_{key}"))
    # 2 per row
    for i in range(0, len(pairs), 2):
        kb.add(*pairs[i:i + 2])
    kb.add(Btn(
        f"{G['back']}  {sc('Admin')}", callback_data="menu_admin"))
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, kb, call=call)


def render_adm_photo_one(call: types.CallbackQuery, key: str) -> None:
    """Prompt the admin to send the next photo as the banner for `key`."""
    if not is_owner(call.from_user.id) and not admin_can(call.from_user.id, "manage_admins"):
        ack(call, "Owner / full-access only.")
        return
    if key not in _PHOTO_SPECS:
        ack(call, "Unknown photo key.")
        return
    USER_STATES[call.from_user.id] = {"flow": "await_admin_photo", "photo_key": key}
    label = PHOTO_KEYS_FRIENDLY.get(key, key)
    cap = (
        f"<b>{G['upload']} {sc('Replace banner')}: {esc(label)}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('Send the new photo now (as a photo, not a file)')}.\n"
        f"{sc('Send /cancel to abort')}.\n"
        f"{G['div']}{FOOTER}"
    )
    # Show the current banner so the admin sees what they're replacing.
    cur = PHOTOS.get(key) or PHOTOS.get("admin", "")
    show_menu(call.message.chat.id, cur, cap, back_admin_kb(), call=call)


def render_adm_github(call: types.CallbackQuery) -> None:
    s = gh_status()
    cap = (
        f"<b>{G['cog']} {sc('GitHub Backup')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Configured', 'Yes' if s['enabled'] else 'No')}\n"
        f"{bullet('Repo',       s['repo'] or '—')}\n"
        f"{bullet('Branch',     s['branch'])}\n"
        f"{bullet('Interval',   '{} min'.format(s['intervalMin']))}\n"
        f"{bullet('Auto',       'On' if s['autoEnabled'] else 'Off')}\n"
        f"{bullet('Last',       fmt_ts(s['lastBackup']))}\n"
        f"{bullet('Last err',   s['lastError'] or '—')}\n"
        f"{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["github"], cap, github_kb(s), call=call)


def render_github_subroute(call: types.CallbackQuery, data: str) -> None:
    if data == "gh_backup_now":
        threading.Thread(target=lambda: _gh_backup_thread(call), daemon=True).start()
        ack(call, "Backup started"); return
    if data == "gh_restore_now":
        threading.Thread(target=lambda: _gh_restore_thread(call), daemon=True).start()
        ack(call, "Restore started"); return
    if data == "gh_toggle_auto":
        GH["autoEnabled"] = not GH["autoEnabled"]
        set_setting("github_auto_enabled", GH["autoEnabled"])
        ack(call, f"Auto: {'ON' if GH['autoEnabled'] else 'OFF'}")
        render_adm_github(call); return
    if data == "gh_set_token":
        USER_STATES[call.from_user.id] = {"flow": "await_gh_token"}
        bot.send_message(call.message.chat.id, f"{G['key']} {sc('Send the GitHub token now')} (Tᴇxᴛ)."); return
    if data == "gh_set_repo":
        USER_STATES[call.from_user.id] = {"flow": "await_gh_repo"}
        bot.send_message(call.message.chat.id, f"{G['diamond']} {sc('Send the repo as')} <code>Oᴡɴᴇʀ/repo</code>.", parse_mode="HTML"); return
    if data == "gh_set_branch":
        USER_STATES[call.from_user.id] = {"flow": "await_gh_branch"}
        bot.send_message(call.message.chat.id, f"{G['tri']} {sc('Send the branch name')}."); return
    if data == "gh_set_interval":
        USER_STATES[call.from_user.id] = {"flow": "await_gh_interval"}
        bot.send_message(call.message.chat.id, f"{G['cog']} {sc('Send interval in minutes (>=15)')}."); return
    if data == "gh_clear":
        gh_set_config({"token": "", "repo": "", "branch": "main", "intervalMin": 360})
        gh_load_config()
        ack(call, "Cleared")
        render_adm_github(call); return
    ack(call, "?")


def _gh_backup_thread(call: types.CallbackQuery) -> None:
    res = gh_backup_now()
    msg = (f"{G['ok']} {sc('backup ok')} ({res.get('sizeMB')} MB)"
           if res["ok"] else f"{G['no']} {esc(res.get('error'))}")
    try:
        bot.send_message(call.message.chat.id, msg)
    except Exception:
        pass


def _gh_restore_thread(call: types.CallbackQuery) -> None:
    res = gh_restore_now(overwrite=True)
    msg = (f"{G['ok']} {sc('restore ok')} ({fmt_bytes(res.get('sizeBytes', 0))})"
           if res["ok"] else f"{G['no']} {esc(res.get('error'))}")
    try:
        bot.send_message(call.message.chat.id, msg)
    except Exception:
        pass


def render_adm_security(call: types.CallbackQuery) -> None:
    d = db_load()
    cap = (
        f"<b>{G['lock']} {sc('Security')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Banned users', sum(1 for u in d['users'].values() if u.get('banned')))}\n"
        f"{bullet('Rate violators', sum(1 for n in d.get('rate_violations', {}).values() if int(n) > 0))}\n"
        f"{bullet('Encryption',   'Fernet (AES-128-CBC) per file')}\n"
        f"{bullet('Key storage',  'GitHub' if KEYRING.gh_enabled() else 'Local cache')}\n"
        f"{bullet('Path-traversal','blocked (safe_path_join)')}\n"
        f"{bullet('Secret env strip','active')}\n"
        f"{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["security"], cap, back_admin_kb(), call=call)


def render_adm_maintenance(call: types.CallbackQuery) -> None:
    cur = bool(get_setting("maintenance", False))
    cap = (
        f"<b>{G['warn']} {sc('Maintenance Mode')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('State', 'ON' if cur else 'OFF')}\n"
        f"{sc('When ON, only admins can use the bot')}.{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    label = "Turn OFF" if cur else "Turn ON"
    kb.add(Btn(
        f"{G['refresh']}  {sc(label)}", callback_data="adm_maint_toggle",
        style="danger" if cur else "success"))
    kb.add(Btn(
        f"{G['back']}  {sc('Admin')}", callback_data="menu_admin", style="primary"))
    show_menu(call.message.chat.id, PHOTOS["maint"], cap, kb, call=call)


def render_adm_settings(call: types.CallbackQuery) -> None:
    running_n = sum(1 for x in RUNNING.values() if x['proc'].poll() is None)
    total_bots = len(db_load_ro()['bots'])
    cap = (
        f"<b>{G['settings']} {sc('Settings & Advanced')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Brand',          BRAND_TAG)}\n"
        f"{bullet('Owner ID',       OWNER_ID)}\n"
        f"{bullet('Announce chan',  ANNOUNCE_CHANNEL or '—')}\n"
        f"{bullet('Keep-alive port', KEEPALIVE_PORT)}\n"
        f"{bullet('GitHub keys',    'GitHub' if KEYRING.gh_enabled() else 'Local cache')}\n"
        f"{bullet('GitHub backup',  'On' if gh_enabled() and GH['autoEnabled'] else 'Off')}\n"
        f"{bullet('Bots running',   f'{running_n} / {total_bots}')}\n"
        f"{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    # ── live tunables ───────────────────────────────────────────────
    kb.add(
        Btn(f"{G['settings']}  {sc('Edit Brand')}",
            callback_data="adm_set_brand",   style="primary"),
        Btn(f"{G['broadcast']}  {sc('Announce Chan')}",
            callback_data="adm_set_announce", style="primary"),
    )
    kb.add(
        Btn(f"{G['shield']}  {sc('Transfer Owner')}",
            callback_data="adm_set_owner",   style="primary"),
        Btn(f"{G['diamond']}  {sc('Plans Editor')}",
            callback_data="adm_set_plans",   style="primary"),
    )
    # ── ops actions ─────────────────────────────────────────────────
    kb.add(
        Btn(f"{G['refresh']}  {sc('Reload Caches')}",
            callback_data="adm_set_reload",  style="success"),
        Btn(f"{G['eye']}  {sc('System Info')}",
            callback_data="adm_set_sysinfo", style="primary"),
    )
    kb.add(
        Btn(f"{G['refresh']}  {sc('Restart All Bots')}",
            callback_data="adm_set_restart_all", style="success"),
        Btn(f"{G['no']}  {sc('Stop All Bots')}",
            callback_data="adm_set_stop_all",    style="danger"),
    )
    kb.add(
        Btn(f"{G['warn']}  {sc('Clean Orphans')}",
            callback_data="adm_set_clean_orphans", style="danger"),
        Btn(f"{G['upload']}  {sc('Export Data')}",
            callback_data="adm_set_export",        style="primary"),
    )
    kb.add(Btn(f"{G['back']}  {sc('Admin')}", callback_data="menu_admin", style="primary"))
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, kb, call=call)


# ───────────────────────────────────────────────────────────────────
#  Advanced settings — sub-renderers + handlers
# ───────────────────────────────────────────────────────────────────

def _set_back_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(
        f"{G['back']}  {sc('Settings')}", callback_data="adm_settings"))
    return kb


def render_adm_sysinfo(call: types.CallbackQuery) -> None:
    """Live system info — RAM, disk, uptime, child processes."""
    rss = vms = pct = 0
    if psutil is not None:
        try:
            p = psutil.Process(os.getpid())
            mi = p.memory_info()
            rss, vms = mi.rss, mi.vms
            pct = p.cpu_percent(interval=0.2)
        except Exception:
            pass
    storage_size = 0
    storage_files = 0
    for root, _, files in os.walk(BASE_DIR / "storage"):
        for f in files:
            try:
                storage_size += (Path(root) / f).stat().st_size
                storage_files += 1
            except OSError:
                pass
    sandbox_size = 0
    sandbox_dirs = 0
    sandbox_root = BASE_DIR / "sandbox"
    if sandbox_root.exists():
        for entry in sandbox_root.iterdir():
            if entry.is_dir():
                sandbox_dirs += 1
                for root, _, files in os.walk(entry):
                    for f in files:
                        try:
                            sandbox_size += (Path(root) / f).stat().st_size
                        except OSError:
                            pass
    up_secs = int(time.time() - START_TIME) if "START_TIME" in globals() else 0
    days, rem = divmod(up_secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    running_n = sum(1 for x in RUNNING.values() if x['proc'].poll() is None)
    cap = (
        f"<b>{G['eye']} {sc('System Info')}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Uptime',       f'{days}d {hours}h {mins}m')}\n"
        f"{bullet('Panel RSS',    f'{rss / 1024 / 1024:.1f} MB')}\n"
        f"{bullet('Panel VMS',    f'{vms / 1024 / 1024:.1f} MB')}\n"
        f"{bullet('CPU sample',   f'{pct:.1f}%')}\n"
        f"{bullet('Bots live',    running_n)}\n"
        f"{bullet('Storage',      f'{storage_size / 1024 / 1024:.1f} MB ({storage_files} files)')}\n"
        f"{bullet('Sandboxes',    f'{sandbox_dirs} dirs, {sandbox_size / 1024 / 1024:.1f} MB')}\n"
        f"{bullet('Cache entries', len(_DB_CACHE))}\n"
        f"{bullet('PID',          os.getpid())}\n"
        f"{G['div']}{FOOTER}"
    )
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, _set_back_kb(), call=call)


def render_adm_plans(call: types.CallbackQuery) -> None:
    """Live plan editor — adjust max_bots per plan tier."""
    rows = []
    for k, v in PLAN_LIMITS.items():
        live = int(get_setting(f"plan_max_bots_{k}", v["max_bots"]))
        rows.append(f"{bullet(v['name'], f'max_bots = {live}')}")
    cap = (
        f"<b>{G['diamond']} {sc('Plans Editor')}</b>\n"
        f"{G['div_eq']}\n"
        + "\n".join(rows) + "\n"
        f"{G['div']}\n"
        f"<i>{sc('Tap a plan to bump its bot quota')}.</i>{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup(row_width=3)
    for k, v in PLAN_LIMITS.items():
        live = int(get_setting(f"plan_max_bots_{k}", v["max_bots"]))
        kb.add(
            Btn(f"➖ {sc(v['name'])}",
                                       callback_data=f"adm_set_plan_dec_{k}"),
            Btn(f"{live}",
                                       callback_data=f"adm_set_plan_show_{k}"),
            Btn(f"➕ {sc(v['name'])}",
                                       callback_data=f"adm_set_plan_inc_{k}"),
        )
    kb.add(Btn(
        f"{G['refresh']}  {sc('Reset Defaults')}",
        callback_data="adm_set_plans_reset"))
    kb.add(Btn(
        f"{G['back']}  {sc('Settings')}", callback_data="adm_settings"))
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, kb, call=call)


def render_adm_confirm(call: types.CallbackQuery, action: str, label: str) -> None:
    cap = (
        f"<b>{G['warn']} {sc('Confirm')}</b>\n"
        f"{G['div_eq']}\n"
        f"{sc('You are about to')}: <b>{esc(label)}</b>.\n"
        f"{sc('This affects every running bot. Continue')}?{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"{G['ok']}  {sc('Yes, do it')}",
                                   callback_data=f"{action}_yes"),
        Btn(f"{G['no']}  {sc('Cancel')}",
                                   callback_data="adm_settings"),
    )
    show_menu(call.message.chat.id, PHOTOS["admin"], cap, kb, call=call)


def _do_restart_all_bots(admin_uid: int) -> Tuple[int, int]:
    """Restart every bot that is currently running. Returns (ok, fail)."""
    ok = fail = 0
    for bid in list(RUNNING.keys()):
        b = find_bot(bid)
        if not b:
            continue
        try:
            r = restart_child(b)
            if r.get("ok"):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    audit(admin_uid, "restart_all_bots", f"ok={ok} fail={fail}")
    return ok, fail


def _do_stop_all_bots(admin_uid: int) -> int:
    n = 0
    for bid in list(RUNNING.keys()):
        try:
            r = stop_child(bid, manual=True)
            if r.get("ok"):
                n += 1
        except Exception:
            pass
    audit(admin_uid, "stop_all_bots", f"stopped={n}")
    return n


def _do_clean_orphans() -> Tuple[int, int]:
    """Delete sandbox dirs and bot_data files with no matching bot
    record. Returns (sandboxes_removed, files_removed)."""
    valid_sandbox_keys: set = set()
    valid_bot_ids: set = set(db_load_ro()["bots"].keys())
    for b in db_load_ro()["bots"].values():
        owner = b.get("owner")
        bid = b.get("_id")
        if owner and bid:
            valid_sandbox_keys.add(f"{owner}_{bid}")
    removed_dirs = 0
    sandbox_root = BASE_DIR / "sandbox"
    if sandbox_root.exists():
        for entry in sandbox_root.iterdir():
            if entry.is_dir() and entry.name not in valid_sandbox_keys:
                try:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed_dirs += 1
                except Exception:
                    pass
    removed_files = 0
    bot_data_dir = BASE_DIR / "storage" / "bot_data"
    if bot_data_dir.exists():
        for f in bot_data_dir.iterdir():
            if f.is_file() and f.suffix == ".json" and f.stem not in valid_bot_ids:
                try:
                    f.unlink()
                    removed_files += 1
                except Exception:
                    pass
    return removed_dirs, removed_files


def _do_export_data(admin_uid: int) -> Path:
    """Bundle DB + settings + audit + bot_data into a single zip and
    return its path."""
    out = BASE_DIR / "exports"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = out / f"simran_export_{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("user_data.json", "settings.json", "audit.log",
                     "github_config.json"):
            p = BASE_DIR / "storage" / name
            if p.exists():
                zf.write(p, arcname=name)
        bot_data = BASE_DIR / "storage" / "bot_data"
        if bot_data.exists():
            for f in bot_data.iterdir():
                if f.is_file():
                    zf.write(f, arcname=f"bot_data/{f.name}")
    audit(admin_uid, "export_data", f"file={target.name}")
    return target


# ═════════════════════════════════════════════════════════════════
# 21. TICKETS
# ═════════════════════════════════════════════════════════════════

def render_user_tickets(call: types.CallbackQuery) -> None:
    uid = call.from_user.id
    d = db_load()["tickets"]
    mine = [t for t in d.values() if t.get("uid") == uid][-10:]
    rows = "\n".join(
        f"{G['bullet']} <code>{t['id']}</code> {G['bullet']} {esc(t.get('status'))} "
        f"{G['bullet']} {esc(t.get('subject'))[:40]}"
        for t in mine
    ) or f"<i>{sc('no tickets yet')}</i>"
    cap = (
        f"<b>{G['ticket']} {sc('Your Tickets')}</b>\n"
        f"{G['div_eq']}\n{rows}\n{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(Btn(
        f"{G['plus']}  {sc('Open Ticket')}", callback_data="ticket_open"))
    for t in mine:
        kb.add(Btn(
            f"{G['eye']}  #{t['id']}", callback_data=f"ticket_view_{t['id']}"))
    kb.add(Btn(
        f"{G['back']}  {sc('Main Menu')}", callback_data="menu_main"))
    show_menu(call.message.chat.id, PHOTOS["ticket"], cap, kb, call=call)


def start_ticket_flow(call: types.CallbackQuery) -> None:
    USER_STATES[call.from_user.id] = {"flow": "await_ticket_subject"}
    bot.send_message(call.message.chat.id,
                     f"{G['ticket']} {sc('Send the subject of your ticket (one line)')}.")


def render_ticket_view(call: types.CallbackQuery, tid: str) -> None:
    d = db_load()
    t = d["tickets"].get(tid)
    if not t:
        ack(call, "Not found"); return
    if t["uid"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    msgs = "\n".join(
        f"<b>{esc(m['from'])}</b>: {esc(m['text'])[:200]}"
        for m in t.get("messages", [])
    )
    cap = (
        f"<b>{G['ticket']} #{t['id']}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('From',    t['uid'])}\n"
        f"{bullet('Status',  t['status'])}\n"
        f"{bullet('Subject', t['subject'])}\n"
        f"{G['div']}\n{msgs}\n{G['div']}{FOOTER}"
    )
    kb = types.InlineKeyboardMarkup()
    if t["status"] == "open":
        kb.add(Btn(
            f"{G['plus']}  {sc('Reply')}", callback_data=f"ticket_reply_{tid}"))
        kb.add(Btn(
            f"{G['no']}  {sc('Close')}", callback_data=f"ticket_close_{tid}"))
    kb.add(Btn(
        f"{G['back']}  {sc('Tickets')}",
        callback_data="adm_tickets" if is_admin(call.from_user.id) else "menu_tickets"))
    show_menu(call.message.chat.id, PHOTOS["ticket"], cap, kb, call=call)


def start_ticket_reply(call: types.CallbackQuery, tid: str) -> None:
    USER_STATES[call.from_user.id] = {"flow": "await_ticket_reply", "tid": tid}
    bot.send_message(call.message.chat.id,
                     f"{G['plus']} {sc('Send your reply now')}. /cancel {sc('to abort')}.")


def action_ticket_close(call: types.CallbackQuery, tid: str) -> None:
    d = db_load()
    t = d["tickets"].get(tid)
    if not t:
        ack(call, "Not found"); return
    if t["uid"] != call.from_user.id and not is_admin(call.from_user.id):
        ack(call, "Not yours"); return
    t["status"] = "closed"
    t["closed_at"] = ts_iso()
    db_save(d)
    audit(call.from_user.id, "ticket_close", f"tid={tid}")
    try:
        bot.send_message(t["uid"], f"<b>{G['ok']} {sc('Ticket closed')} #{tid}</b>")
    except Exception:
        pass
    ack(call, "Closed")
    render_ticket_view(call, tid)


# ═════════════════════════════════════════════════════════════════
# 22. MESSAGE/DOC HANDLERS  (state-driven flows)
# ═════════════════════════════════════════════════════════════════

@bot.message_handler(content_types=["document"])
def on_document(m: types.Message) -> None:
    if not _is_private(m):
        return
    if banned_block(m):
        return
    uid = m.from_user.id
    if not RATE.allow(uid):
        maybe_auto_ban(uid, "rate")
        return
    if not UPLOAD_RATE.allow(uid):
        bot.reply_to(m, f"{G['warn']} {sc('Too many uploads, slow down')}.")
        maybe_auto_ban(uid, "upload spam")
        return
    if maintenance_block(uid):
        return
    get_or_create_user(m.from_user)
    if not require_verified(m.chat.id, uid):
        return
    st = USER_STATES.get(uid) or {}
    if st.get("flow") == "await_payment_proof":
        return _handle_payment_proof(m, st)
    if st.get("flow") == "await_topup_proof":
        return _handle_topup_proof(m)
    # default: bot upload
    _handle_bot_upload(m)


@bot.message_handler(content_types=["photo"])
def on_photo(m: types.Message) -> None:
    if not _is_private(m):
        return
    if banned_block(m):
        return
    uid = m.from_user.id
    if not RATE.allow(uid):
        return
    get_or_create_user(m.from_user)
    if not require_verified(m.chat.id, uid):
        return
    st = USER_STATES.get(uid) or {}
    # ── admin sent a banner replacement ──
    if st.get("flow") == "await_admin_photo" and is_admin(uid):
        key = st.get("photo_key") or ""
        if key not in _PHOTO_SPECS:
            bot.reply_to(m, f"{G['no']} {sc('Unknown photo key')}.")
            USER_STATES.pop(uid, None)
            return
        try:
            ph = m.photo[-1]
            f = bot.get_file(ph.file_id)
            raw = bot.download_file(f.file_path)
        except Exception as e:
            bot.reply_to(m, f"{G['no']} {sc('download error')}: <code>{esc(e)}</code>",
                         parse_mode="HTML")
            return
        ok = replace_menu_photo(key, raw)
        USER_STATES.pop(uid, None)
        label = PHOTO_KEYS_FRIENDLY.get(key, key)
        if ok:
            audit(uid, "menu_photo_replace", f"key={key} bytes={len(raw)}")
            bot.reply_to(
                m,
                f"<b>{G['ok']} {sc('Banner updated')}</b>\n"
                f"{bullet('Menu', label)}\n"
                f"{bullet('Size', fmt_bytes(len(raw)))}",
                parse_mode="HTML",
            )
        else:
            bot.reply_to(m, f"{G['no']} {sc('Failed to save photo')}.")
        return
    if st.get("flow") == "await_payment_proof":
        _handle_payment_proof(m, st); return
    if st.get("flow") == "await_topup_proof":
        _handle_topup_proof(m); return


@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(m: types.Message) -> None:
    if not _is_private(m):
        return
    if banned_block(m):
        return
    uid = m.from_user.id
    if not RATE.allow(uid):
        maybe_auto_ban(uid, "rate")
        return
    text = (m.text or "").strip()
    if text.startswith("/"):
        return  # handled by command handlers
    get_or_create_user(m.from_user)
    if maintenance_block(uid):
        return
    if not require_verified(m.chat.id, uid):
        return

    st = USER_STATES.get(uid) or {}
    flow = st.get("flow")
    try:
        if flow == "await_env_kv":
            return _handle_env_kv(m, st)
        if flow == "await_pip_install":
            return _handle_pip_install(m, st)
        if flow == "await_tunnel_port":
            return _handle_tunnel_port(m, st)
        if flow == "await_cron":
            return _handle_cron(m, st)
        if flow == "await_admin_finduser":
            return _handle_admin_finduser(m)
        if flow == "await_ban_cmd":
            return _handle_ban_cmd(m)
        if flow == "await_giveplan":
            return _handle_giveplan_cmd(m)
        if flow == "await_broadcast":
            return _handle_broadcast(m)
        if flow == "await_coupon":
            return _handle_coupon_user(m)
        if flow == "await_coupon_admin":
            return _handle_coupon_admin(m)
        if flow == "await_admin_admins":
            return _handle_admin_admins(m)
        if flow == "await_ticket_subject":
            return _handle_ticket_subject(m)
        if flow == "await_ticket_body":
            return _handle_ticket_body(m, st)
        if flow == "await_ticket_reply":
            return _handle_ticket_reply(m, st)
        if flow == "await_payment_proof":
            return _handle_payment_proof_text(m, st)
        if flow == "await_topup_proof":
            return _handle_topup_proof(m)
        if flow == "await_gift_target":
            return _handle_gift_target(m, st)
        if flow == "await_gift_confirm":
            return _handle_gift_confirm(m, st)
        if flow == "await_gh_token":
            gh_set_config({"token": text}); gh_load_config()
            USER_STATES.pop(uid, None); bot.reply_to(m, f"{G['ok']} {sc('token saved')}"); return
        if flow == "await_gh_repo":
            gh_set_config({"repo": text}); gh_load_config()
            USER_STATES.pop(uid, None); bot.reply_to(m, f"{G['ok']} {sc('repo saved')}"); return
        if flow == "await_gh_branch":
            gh_set_config({"branch": text}); gh_load_config()
            USER_STATES.pop(uid, None); bot.reply_to(m, f"{G['ok']} {sc('branch saved')}"); return
        if flow == "await_gh_interval":
            try:
                v = max(15, int(text))
            except Exception:
                v = 360
            gh_set_config({"intervalMin": v}); gh_load_config()
            USER_STATES.pop(uid, None); bot.reply_to(m, f"{G['ok']} {sc('interval saved')}"); return
        if flow == "await_set_brand":
            if not is_owner(uid):
                USER_STATES.pop(uid, None); return
            new = (text or "").strip()[:64]
            if not new:
                bot.reply_to(m, f"{G['no']} {sc('empty — cancelled')}")
                USER_STATES.pop(uid, None); return
            global BRAND_TAG
            BRAND_TAG = new
            set_setting("brand_tag", new)
            audit(uid, "set_brand", new)
            USER_STATES.pop(uid, None)
            bot.reply_to(m, f"{G['ok']} {sc('Brand updated to')}: <b>{esc(new)}</b>",
                         parse_mode="HTML")
            return
        if flow == "await_set_announce":
            if not is_owner(uid):
                USER_STATES.pop(uid, None); return
            v = (text or "").strip()
            if v == "-" or not v:
                v = ""
            elif not v.startswith("@") and not v.lstrip("-").isdigit():
                bot.reply_to(m, f"{G['no']} {sc('use @handle or numeric chat id, or - to clear')}")
                return
            global ANNOUNCE_CHANNEL
            ANNOUNCE_CHANNEL = v
            set_setting("announce_channel", v)
            audit(uid, "set_announce", v or "(cleared)")
            USER_STATES.pop(uid, None)
            bot.reply_to(m, f"{G['ok']} {sc('Announce channel set to')}: "
                            f"<code>{esc(v) if v else '—'}</code>",
                         parse_mode="HTML")
            return
        if flow == "await_set_owner":
            if not is_owner(uid):
                USER_STATES.pop(uid, None); return
            try:
                new_owner = int((text or "").strip())
                if new_owner <= 0:
                    raise ValueError
            except Exception:
                bot.reply_to(m, f"{G['no']} {sc('invalid id — send a positive integer')}")
                return
            global OWNER_ID
            OWNER_ID = new_owner
            set_setting("owner_id", new_owner)
            audit(uid, "transfer_owner", f"new={new_owner}")
            USER_STATES.pop(uid, None)
            bot.reply_to(m,
                f"{G['ok']} {sc('Ownership transferred to')} <code>{new_owner}</code>.\n"
                f"<i>{sc('You are no longer the owner. New owner can use')} /start.</i>",
                parse_mode="HTML")
            return
    except Exception as e:
        traceback.print_exc()
        bot.reply_to(m, f"{G['no']} {sc('error')}: <code>{esc(e)}</code>", parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════
# 22.5  APPROVAL SYSTEM (admin-gated bot uploads)
# ═════════════════════════════════════════════════════════════════
#
# When admin toggles "Approval Mode: ON", every uploaded bot is held
# until an admin Approves or Rejects it. While pending, the bot is
# NEVER auto-started, even if its files decrypt cleanly.
#
# storage layout:
#   settings.approval_required          : bool (default True)
#   settings.pending_uploads            : { bot_id -> { file_id, msg_id,
#                                                       chat_id, user_id, name,
#                                                       file_count, size,
#                                                       file_name, ts } }
# bot doc:
#   doc["approval_status"]   : "pending" | "approved" | "rejected" | None
#   doc["approval_reason"]   : str (filled when rejected)
# ═════════════════════════════════════════════════════════════════

def approval_required() -> bool:
    return bool(get_setting("approval_required", True))


def set_approval_required(on: bool) -> None:
    set_setting("approval_required", bool(on))


def _pending_load() -> Dict[str, Any]:
    return dict(get_setting("pending_uploads", {}) or {})


def _pending_save(d: Dict[str, Any]) -> None:
    set_setting("pending_uploads", d)


def pending_add(bot_id: str, info: Dict[str, Any]) -> None:
    p = _pending_load()
    p[bot_id] = info
    _pending_save(p)


def pending_remove(bot_id: str) -> Optional[Dict[str, Any]]:
    p = _pending_load()
    info = p.pop(bot_id, None)
    _pending_save(p)
    return info


def pending_list() -> List[Tuple[str, Dict[str, Any]]]:
    return list(_pending_load().items())


def is_bot_blocked_by_approval(b: Dict[str, Any]) -> bool:
    """Returns True if the bot is held in the approval queue."""
    return (b or {}).get("approval_status") == "pending"


def _send_approval_request_to_admins(b: Dict[str, Any], info: Dict[str, Any],
                                     forwarded_msg: Optional[types.Message]) -> None:
    """Notify every admin (owner + extra admins) about a new upload
    waiting for review. Each admin gets the forwarded file + Approve/
    Reject buttons."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        Btn(f"{G['ok']}  {sc('Approve')}",
                                   callback_data=f"appr_ok_{b['_id']}"),
        Btn(f"{G['no']}  {sc('Reject')}",
                                   callback_data=f"appr_no_{b['_id']}"),
    )
    txt = (
        f"<b>{G['warn']} {sc('New bot upload — awaiting approval')}</b>\n"
        f"{G['div']}\n"
        f"{bullet('User',     '{} (@{})'.format(info.get('user_name') or '', info.get('user_username') or '-'))}\n"
        f"{bullet('User ID',  info.get('user_id'))}\n"
        f"{bullet('Bot Name', b.get('name'))}\n"
        f"{bullet('Bot ID',   b['_id'])}\n"
        f"{bullet('File',     info.get('file_name'))}\n"
        f"{bullet('Files',    info.get('file_count'))}\n"
        f"{bullet('Size',     fmt_bytes(info.get('size', 0)))}\n"
        f"{G['div']}"
    )
    targets: List[int] = []
    if OWNER_ID:
        targets.append(OWNER_ID)
    for uid_str in (db_load().get("admins") or {}).keys():
        try:
            uid_i = int(uid_str)
            if uid_i not in targets:
                targets.append(uid_i)
        except Exception:
            pass
    for tgt in targets:
        try:
            if forwarded_msg:
                bot.forward_message(tgt, info.get("chat_id"), info.get("msg_id"))
        except Exception:
            pass
        try:
            bot.send_message(tgt, txt, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


def approve_bot(bot_id: str, admin_uid: int) -> Dict[str, Any]:
    b = find_bot(bot_id)
    if not b:
        return {"ok": False, "error": "Bot not found."}
    pending_remove(bot_id)
    b["approval_status"] = "approved"
    b["approval_reason"] = ""
    b["status"] = "stopped"
    save_bot(b)
    audit(admin_uid, "approve_bot", f"bot={bot_id}")
    # Notify uploader
    try:
        owner = b.get("owner")
        if owner:
            bot.send_message(
                owner,
                f"<b>{G['ok']} {sc('Your bot was approved')}</b>\n"
                f"{bullet('Bot', b.get('name'))}\n"
                f"{sc('Starting it now')}…",
                parse_mode="HTML",
            )
    except Exception:
        pass
    # Auto-start in background
    def _bg() -> None:
        try:
            res = start_child(b)
            if not res.get("ok") and b.get("owner"):
                try:
                    bot.send_message(
                        b["owner"],
                        f"<b>{G['no']} {sc('Auto-start failed after approval')}</b>\n"
                        f"{bullet('Error', esc(res.get('error', '')))}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        except Exception as e:
            print(f"[approve_bot bg] {e}")
    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True}


def reject_bot(bot_id: str, admin_uid: int, reason: str = "") -> Dict[str, Any]:
    b = find_bot(bot_id)
    if not b:
        return {"ok": False, "error": "Bot not found."}
    pending_remove(bot_id)
    b["approval_status"] = "rejected"
    b["approval_reason"] = reason or "rejected by admin"
    b["status"] = "rejected"
    save_bot(b)
    # Wipe the encrypted blobs + bot dir — rejected uploads should not
    # linger on disk.
    try:
        for f in b.get("enc_files") or []:
            try:
                Path(f.get("enc_path", "")).unlink(missing_ok=True)
            except Exception:
                pass
        rmrf(b.get("dir", ""))
    except Exception:
        pass
    # Remove the bot entry so the user's slot frees up
    try:
        db = db_load()
        db["bots"].pop(bot_id, None)
        db_save(db)
    except Exception:
        pass
    audit(admin_uid, "reject_bot", f"bot={bot_id} reason={reason}")
    try:
        owner = b.get("owner")
        if owner:
            bot.send_message(
                owner,
                f"<b>{G['no']} {sc('Your bot was rejected')}</b>\n"
                f"{bullet('Bot', b.get('name'))}\n"
                f"{bullet('Reason', reason or 'No reason given')}",
                parse_mode="HTML",
            )
    except Exception:
        pass
    return {"ok": True}


# ═════════════════════════════════════════════════════════════════
# 22.6  PHOTO CUSTOMIZATION (admin-uploaded menu banners)
# ═════════════════════════════════════════════════════════════════
#
# Admin clicks "Menu Photos" → picks a key (main / admin / plans / …)
# → next photo they send replaces that menu's banner. The PNG is saved
# to storage/photos/<key>.png (overwriting the auto-generated banner)
# and the cached file_id for that key is invalidated so the new image
# is uploaded on the next show_menu.

PHOTO_KEYS_FRIENDLY: Dict[str, str] = {
    "main":      "Main Menu",
    "admin":     "Admin Panel",
    "plans":     "Plans",
    "buy":       "Buy Plan",
    "wallet":    "Wallet",
    "bots":      "My Bots",
    "bot":       "Bot View",
    "upload":    "Upload Bot",
    "stats":     "Stats",
    "support":   "Support",
    "about":     "About",
    "broadcast": "Broadcast",
    "ticket":    "Tickets",
    "coupon":    "Coupons",
    "security":  "Security",
}


def replace_menu_photo(key: str, file_bytes: bytes) -> bool:
    """Persist an admin-uploaded photo as the banner for `key`.

    Custom photos are saved with a 'custom_' prefix so _build_local_photos()
    always picks them over the auto-generated fallback — even after a restart
    or a GitHub restore that overwrites the plain <key>.png.

    The bytes are ALSO mirrored to GitHub at storage/photos/custom_<key>.png
    immediately, so a fresh deploy or restart that wipes local storage can
    restore the admin-set banner via gh_restore_custom_photos()."""
    if key not in _PHOTO_SPECS:
        return False
    out_dir = DIRS["photos"]
    out_dir.mkdir(parents=True, exist_ok=True)
    # Save with custom_ prefix — this is the persistent marker
    custom_out = out_dir / f"custom_{key}.png"
    # Also overwrite the plain key.png so existing code paths work
    plain_out  = out_dir / f"{key}.png"
    try:
        custom_out.write_bytes(file_bytes)
        plain_out.write_bytes(file_bytes)
        PHOTOS[key] = str(custom_out)
        # Invalidate the cached file_id so the next send re-uploads.
        _PHOTO_FILE_IDS.pop(key, None)
        _PHOTO_FILE_IDS.pop(str(plain_out), None)
        _PHOTO_FILE_IDS.pop(str(custom_out), None)
        # ── Mirror to GitHub right away so it survives restarts ──────
        try:
            if gh_enabled():
                threading.Thread(
                    target=lambda: _gh_put_file(
                        f"storage/photos/custom_{key}.png",
                        file_bytes,
                        f"chore(photos): admin updated banner '{key}'",
                    ),
                    daemon=True,
                ).start()
        except Exception as _e:
            print(f"[replace_menu_photo] gh mirror skipped: {_e}")
        return True
    except Exception as e:
        print(f"[replace_menu_photo] {key}: {e}")
        return False


def gh_restore_custom_photos() -> Dict[str, Any]:
    """Restore admin-uploaded banner photos from GitHub. Runs on every boot
    (even when the local DB is non-empty) so a wiped storage/photos/ folder
    can be repopulated. Existing local custom_<key>.png files are kept;
    only missing or empty ones are pulled. Returns a small summary dict."""
    if not gh_enabled():
        return {"ok": False, "skip": True, "reason": "gh disabled"}
    out_dir = DIRS["photos"]
    out_dir.mkdir(parents=True, exist_ok=True)
    restored = 0
    failed: List[str] = []
    try:
        listing = _gh(
            "GET", _gh_repo_url("contents/storage/photos"),
            params={"ref": GH["branch"]},
        )
        if listing.status_code == 404:
            return {"ok": True, "restored": 0, "note": "no remote photos dir"}
        if listing.status_code != 200:
            return {"ok": False, "error": f"list http {listing.status_code}"}
        items = listing.json() or []
    except Exception as e:
        return {"ok": False, "error": str(e)}
    for it in items:
        try:
            name = (it or {}).get("name") or ""
            if not (name.startswith("custom_") and name.endswith(".png")):
                continue
            local = out_dir / name
            if local.exists() and local.stat().st_size > 1024:
                continue  # local copy already present
            r = _gh(
                "GET", _gh_repo_url(f"contents/storage/photos/{name}"),
                params={"ref": GH["branch"]},
            )
            if r.status_code != 200:
                failed.append(name); continue
            payload = r.json() or {}
            blob = base64.b64decode(payload.get("content") or "")
            if len(blob) < 1024:
                failed.append(name); continue
            local.write_bytes(blob)
            # Mirror onto plain <key>.png so legacy paths also resolve.
            key = name[len("custom_"):-len(".png")]
            plain = out_dir / f"{key}.png"
            try:
                plain.write_bytes(blob)
            except Exception:
                pass
            if key in _PHOTO_SPECS:
                PHOTOS[key] = str(local)
                _PHOTO_FILE_IDS.pop(key, None)
                _PHOTO_FILE_IDS.pop(str(local), None)
                _PHOTO_FILE_IDS.pop(str(plain), None)
            restored += 1
        except Exception as e:
            failed.append(f"{(it or {}).get('name','?')}:{e}")
    return {"ok": True, "restored": restored, "failed": failed}


# ─── upload handler ───────────────────────────────────────────────
def _handle_bot_upload(m: types.Message) -> None:
    uid = m.from_user.id
    u = db_load()["users"][str(uid)]
    if len(list_user_bots(uid)) >= user_max_bots(u):
        bot.reply_to(m, f"{G['no']} {sc('You hit your bot slot limit')}. {sc('Upgrade or delete one')}.")
        return
    doc = m.document
    if not doc:
        return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        bot.reply_to(m, f"{G['no']} {sc('File too big')} (>{MAX_UPLOAD_BYTES // (1024*1024)} Mʙ).")
        return
    fname = doc.file_name or "upload.bin"
    if not re.match(r"^[A-Za-z0-9._\-]+$", fname):
        bot.reply_to(m, f"{G['warn']} {sc('Suspicious filename, please rename')}.")
        return
    try:
        f = bot.get_file(doc.file_id)
        raw = bot.download_file(f.file_path)
    except Exception as e:
        bot.reply_to(m, f"{G['no']} {sc('download error')}: <code>{esc(e)}</code>", parse_mode="HTML")
        return

    bot_id = secrets.token_hex(8)
    bot_dir = DIRS["sandbox"] / f"{uid}_{bot_id}"
    bot_dir.mkdir(parents=True, exist_ok=True)
    name = safe_name(Path(fname).stem)
    doc_db = {
        "_id": bot_id, "owner": uid, "name": name,
        "dir": str(bot_dir), "created": ts_iso(),
        "enc_files": [], "env": {}, "status": "stopped", "cron": {},
    }

    # Determine content: zip vs single file
    files_added: List[Tuple[str, bytes]] = []
    if fname.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    rel = member.filename.replace("\\", "/")
                    if rel.startswith("/") or ".." in rel.split("/"):
                        continue
                    try:
                        # path-traversal check
                        safe_path_join(bot_dir, rel)
                    except ValueError:
                        continue
                    files_added.append((rel, zf.read(member)))
        except zipfile.BadZipFile:
            bot.reply_to(m, f"{G['no']} {sc('not a valid zip')}")
            rmrf(bot_dir); return
    else:
        files_added.append((fname, raw))

    # encrypt and store each
    for rel, plain in files_added:
        meta = store_uploaded_file(m.from_user, rel, plain)
        doc_db["enc_files"].append({
            "key_id": meta["key_id"],
            "enc_path": meta["path"],
            "filename": Path(rel).name,
            "rel_path": rel,
        })

    # NOTE: We deliberately do NOT push to GitHub right after upload.
    # Most upload failures are caught only when the bot starts running,
    # so we wait until the bot has been running for >= 10 minutes
    # (see `gh_uptime_backup_loop`) before backing up. This keeps the
    # backup repo clean of broken/test uploads.
    doc_db["gh_synced_at"] = 0  # reset; loop will re-sync once stable
    total_size = sum(len(p) for _, p in files_added)

    # ── Approval gate ───────────────────────────────────────────
    needs_approval = approval_required() and not is_admin(uid) and OWNER_ID > 0
    if needs_approval:
        doc_db["approval_status"] = "pending"
        doc_db["status"] = "pending_approval"
    save_bot(doc_db)
    db = db_load()
    db["users"][str(uid)]["stats"]["bots_uploaded"] = int(
        db["users"][str(uid)]["stats"].get("bots_uploaded", 0)) + 1
    db_save(db)
    USER_STATES.pop(uid, None)

    if needs_approval:
        info = {
            "user_id":       uid,
            "user_name":     m.from_user.first_name or "",
            "user_username": m.from_user.username or "",
            "chat_id":       m.chat.id,
            "msg_id":        m.message_id,
            "file_name":     fname,
            "file_count":    len(files_added),
            "size":          total_size,
            "ts":            ts_iso(),
        }
        pending_add(bot_id, info)
        try:
            _send_approval_request_to_admins(doc_db, info, m)
        except Exception as e:
            print(f"[approval notify] {e}")
        bot.reply_to(
            m,
            f"<b>{G['warn']} {sc('Pending admin approval')}</b>\n"
            f"{G['div']}\n"
            f"{bullet('Bot Name', name)}\n"
            f"{bullet('Files',    len(files_added))}\n"
            f"{bullet('Size',     fmt_bytes(total_size))}\n"
            f"{G['div']}\n"
            f"{sc('Your bot will start automatically once an admin approves it')}.",
            parse_mode="HTML",
        )
        return

    # Forward uploaded file directly to admin (non-approval flow)
    try:
        bot.forward_message(OWNER_ID, m.chat.id, m.message_id)
    except Exception:
        pass
    notify_owner(
        f"<b>{G['upload']} ɴᴇᴡ ʙᴏᴛ ᴜᴘʟᴏᴀᴅ</b>\n"
        f"{G['div']}\n"
        f"{bullet('File',     fname)}\n"
        f"{bullet('User',     '{} (@{})'.format(m.from_user.first_name or '', m.from_user.username or '-'))}\n"
        f"{bullet('User ID',  uid)}\n"
        f"{bullet('Bot Name', name)}\n"
        f"{bullet('Files',    len(files_added))}\n"
        f"{bullet('Size',     fmt_bytes(total_size))}\n"
        f"{G['div']}"
    )

    kind, _ = detect_entry(bot_dir)  # speculative — might be encrypted-only

    def _make_bar(pct: int, status: str, kind_str: str = "") -> str:
        filled = int(pct / 5)
        bar    = "▓" * filled + "░" * (20 - filled)
        return (
            f"<b>{G['ok']} {sc('Bot stored encrypted')}</b>\n"
            f"{bullet('Name',  name)}\n"
            f"{bullet('Files', len(files_added))}\n"
            f"{bullet('Kind',  kind_str or kind or 'auto-detect on start')}\n"
            f"<code>{bar} {pct}%</code>\n"
            f"{status}"
        )

    # Ek message bhejo — phir edit karte rahenge (spam nahi hoga)
    sent   = bot.reply_to(m, _make_bar(0, sc("Starting...")), parse_mode="HTML")
    msg_id = sent.message_id
    cid    = m.chat.id

    def _edit(pct: int, status: str, kind_str: str = "") -> None:
        try:
            bot.edit_message_text(
                _make_bar(pct, status, kind_str),
                chat_id=cid, message_id=msg_id,
                parse_mode="HTML",
            )
        except Exception:
            pass

    # ── auto-start the freshly uploaded bot ──────────────────────
    def _bg_start(doc: Dict[str, Any]) -> None:
        try:
            _edit(10, sc("Decrypting files..."))
            time.sleep(0.8)
            _edit(30, sc("Installing dependencies..."))
            time.sleep(0.8)
            _edit(50, sc("Setting up environment..."))
            time.sleep(0.8)
            _edit(70, sc("Launching bot..."))
            res = start_child(doc)
            if res.get("ok"):
                _edit(100,
                      f"<b>{G['play']} {sc('Bot is running!')}</b>",
                      res.get("kind", ""))
                time.sleep(1.5)
                # loading msg delete karo
                try:
                    bot.delete_message(cid, msg_id)
                except Exception:
                    pass
                # My Bots menu bhejo
                bots = list_user_bots(uid)
                u = db_load()["users"][str(uid)]
                cap = (
                    f"<b>{G['diamond']} {sc('Your Bots')}</b>\n"
                    f"{G['div_eq']}\n"
                    f"{bullet('Slots', f'{len(bots)} / {user_max_bots(u)}')}\n"
                )
                kb = types.InlineKeyboardMarkup()
                for b in sorted(bots, key=lambda x: x.get("name", "")):
                    running = b["_id"] in RUNNING and RUNNING[b["_id"]]["proc"].poll() is None
                    mark = G["play"] if running else G["stop"]
                    kb.add(Btn(
                        f"{mark}  {sc(b['name'])[:30]}",
                        callback_data=f"bot_view_{b['_id']}"))
                kb.add(
                    Btn(f"{G['plus']}  {sc('Upload')}",   callback_data="menu_upload", style="success"),
                    Btn(f"{G['back']}  {sc('Main Menu')}", callback_data="menu_main", style="primary"),
                )
                bot.send_message(cid, cap + FOOTER, parse_mode="HTML", reply_markup=kb)
            else:
                _edit(0,
                      f"<b>{G['no']} {sc('Auto-start failed')}</b>\n"
                      f"{bullet('Error', esc(res.get('error', '')))}\n"
                      f"{sc('Open My Bots → Live Logs to see why')}.")
        except Exception as e:
            try:
                _edit(0, f"{G['no']} {sc('Auto-start error')}: <code>{esc(str(e))}</code>")
            except Exception:
                pass

    threading.Thread(target=_bg_start, args=(doc_db,), daemon=True).start()


# ─── env vars flow ────────────────────────────────────────────────
def _handle_env_kv(m: types.Message, st: Dict[str, Any]) -> None:
    text = m.text.strip()
    if "=" not in text:
        bot.reply_to(m, f"{G['no']} {sc('Use')} <code>Kᴇʏ=Vᴀʟᴜᴇ</code>.", parse_mode="HTML"); return
    key, _, value = text.partition("=")
    key = key.strip(); value = value.strip()
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
        bot.reply_to(m, f"{G['no']} {sc('Invalid key')}."); return
    if key in SECRET_ENV_NAMES:
        bot.reply_to(m, f"{G['no']} {sc('That env name is protected')}."); return
    b = find_bot(st["bot_id"])
    if not b:
        bot.reply_to(m, f"{G['no']} {sc('Bot not found')}."); return
    env = b.get("env") or {}
    env[key] = value
    b["env"] = env
    save_bot(b)
    USER_STATES.pop(m.from_user.id, None)
    bot.reply_to(m, f"{G['ok']} {sc('Saved')} <code>{esc(key)}</code>", parse_mode="HTML")


# ─── pip install flow ─────────────────────────────────────────────
def _handle_pip_install(m: types.Message, st: Dict[str, Any]) -> None:
    text = (m.text or "").strip()
    USER_STATES.pop(m.from_user.id, None)
    if not text:
        bot.reply_to(m, f"{G['no']} {sc('Nothing to install')}."); return
    # only allow safe package spec characters; block flags/shell metas
    pkgs = [p for p in text.split() if p]
    bad = [p for p in pkgs if not re.match(r"^[A-Za-z0-9_\-\.\[\]=<>!~,+]+$", p) or p.startswith("-")]
    if bad:
        bot.reply_to(
            m,
            f"{G['no']} {sc('Invalid package spec')}: <code>{esc(' '.join(bad))}</code>",
            parse_mode="HTML",
        ); return
    if len(pkgs) > 15:
        bot.reply_to(m, f"{G['no']} {sc('Too many packages at once (max 15)')}."); return
    b = find_bot(st["bot_id"])
    if not b:
        bot.reply_to(m, f"{G['no']} {sc('Bot not found')}."); return
    if b["owner"] != m.from_user.id and not is_admin(m.from_user.id):
        bot.reply_to(m, f"{G['no']} {sc('Not yours')}."); return
    # Use the bot's own sandbox dir — installing into a fixed `ROOT/bots/...`
    # used to NameError out, then fall back to root site-packages, which on
    # most hosts requires sudo. `--target deps_dir` keeps everything in the
    # bot's own folder so no permissions are ever needed.
    bot_dir = Path(b["dir"])
    deps_dir = bot_dir / ".deps"
    deps_dir.mkdir(parents=True, exist_ok=True)
    status = bot.reply_to(
        m,
        f"{G['refresh']} {sc('Installing')} <code>{esc(' '.join(pkgs))}</code> ...",
        parse_mode="HTML",
    )
    pip_env = _pip_env(deps_dir)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--target", str(deps_dir), *_PIP_BASE_FLAGS] + pkgs,
            capture_output=True, text=True, timeout=180, env=pip_env,
        )
        ok = (proc.returncode == 0)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        tail = "\n".join([ln for ln in out.splitlines() if ln.strip()][-10:])[:1500]
        head = f"{G['ok']} {sc('Installed')}" if ok else f"{G['no']} {sc('Install failed')}"
        try:
            bot.edit_message_text(
                f"<b>{head}</b>\n"
                f"{G['div']}\n"
                f"<b>{sc('Packages')}:</b> <code>{esc(' '.join(pkgs))}</code>\n"
                f"<pre>{esc(tail) or '(no output)'}</pre>",
                chat_id=status.chat.id, message_id=status.message_id,
                parse_mode="HTML",
            )
        except Exception:
            bot.send_message(m.chat.id, f"{head}\n<pre>{esc(tail)}</pre>", parse_mode="HTML")
        audit(m.from_user.id, "pip_install",
              f"bot={b['_id']} pkgs={' '.join(pkgs)} rc={proc.returncode}")
    except subprocess.TimeoutExpired:
        bot.send_message(m.chat.id, f"{G['no']} {sc('Install timed out after 180s')}.")
    except Exception as e:
        bot.send_message(m.chat.id, f"{G['no']} {sc('Install error')}: <code>{esc(str(e))}</code>", parse_mode="HTML")


# ─── cron flow ────────────────────────────────────────────────────
def _handle_cron(m: types.Message, st: Dict[str, Any]) -> None:
    text = m.text.strip().lower()
    b = find_bot(st["bot_id"])
    if not b:
        bot.reply_to(m, f"{G['no']} {sc('Bot not found')}."); return
    if text == "off":
        b["cron"] = {}; save_bot(b); USER_STATES.pop(m.from_user.id, None)
        bot.reply_to(m, f"{G['ok']} {sc('Cron disabled')}"); return
    cron = b.get("cron") or {}
    for tok in text.split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if k not in {"restart", "backup"}:
            continue
        try:
            iv = int(v)
        except Exception:
            continue
        if iv <= 0:
            continue
        cron[f"{k}_hours"] = iv
    b["cron"] = cron
    save_bot(b)
    USER_STATES.pop(m.from_user.id, None)
    bot.reply_to(m, f"{G['ok']} {sc('Cron updated')}: <code>{esc(json.dumps(cron))}</code>",
                 parse_mode="HTML")


# ─── admin: find user ─────────────────────────────────────────────
def _handle_admin_finduser(m: types.Message) -> None:
    if not is_admin(m.from_user.id):
        return
    USER_STATES.pop(m.from_user.id, None)
    text = m.text.strip()
    if not text.lstrip("@").lstrip("-").isdigit() and not text.startswith("@"):
        return
    d = db_load()
    target = None
    if text.startswith("@"):
        for u in d["users"].values():
            if (u.get("username") or "").lower() == text[1:].lower():
                target = u; break
    else:
        try:
            target = d["users"].get(str(int(text)))
        except Exception:
            target = None
    if not target:
        bot.reply_to(m, f"{G['no']} {sc('No such user')}."); return
    bots = list_user_bots(target["_id"])
    txt = (
        f"<b>{G['user']} {sc('User')} {target['_id']}</b>\n"
        f"{G['div_eq']}\n"
        f"{bullet('Name',     target.get('name'))}\n"
        f"{bullet('Username', '@' + (target.get('username') or '—'))}\n"
        f"{bullet('Plan',     PLAN_LIMITS.get(target.get('plan'), {}).get('name'))}\n"
        f"{bullet('Until',    fmt_ts(target.get('plan_expires')))}\n"
        f"{bullet('Wallet',   '{}$'.format(target.get('wallet', 0)))}\n"
        f"{bullet('Banned',   target.get('banned'))}\n"
        f"{bullet('KYC',      target.get('kyc'))}\n"
        f"{bullet('Bots',     len(bots))}\n"
        f"{bullet('Joined',   fmt_ts(target.get('joined')))}\n"
        f"{bullet('LastSeen', fmt_ts(target.get('last_seen')))}\n"
        f"{bullet('Note',     d.get('notes', {}).get(str(target['_id']), '—'))}\n"
        f"{G['div']}{FOOTER}"
    )
    bot.reply_to(m, txt, parse_mode="HTML", reply_markup=back_admin_kb())


# ─── admin: ban / unban ──────────────────────────────────────────
def _handle_ban_cmd(m: types.Message) -> None:
    if not is_admin(m.from_user.id):
        return
    if not admin_can(m.from_user.id, "ban_user"):
        bot.reply_to(m, f"{G['no']} {sc('insufficient permission')}"); return
    USER_STATES.pop(m.from_user.id, None)
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        bot.reply_to(m, f"{G['no']} {sc('format')}: <code>Bᴀɴ &lt;Uɪᴅ&gt; &lt;Rᴇᴀꜱᴏɴ&gt;</code>",
                     parse_mode="HTML"); return
    op = parts[0].lower()
    try:
        uid = int(parts[1])
    except Exception:
        bot.reply_to(m, f"{G['no']} {sc('bad uid')}"); return
    reason = parts[2] if len(parts) > 2 else ""
    d = db_load()
    if str(uid) not in d["users"]:
        bot.reply_to(m, f"{G['no']} {sc('no such user')}"); return
    if op == "ban":
        d["users"][str(uid)]["banned"] = True
        d["users"][str(uid)]["ban_reason"] = reason
        db_save(d)
        audit(m.from_user.id, "ban_user", f"uid={uid} reason={reason}")
        try:
            bot.send_message(uid,
                             f"<b>{G['no']} {sc('You have been banned')}</b>\n{bullet('Reason', reason)}",
                             parse_mode="HTML")
        except Exception:
            pass
        bot.reply_to(m, f"{G['ok']} {sc('banned')} {uid}"); return
    if op == "unban":
        d["users"][str(uid)]["banned"] = False
        d["users"][str(uid)]["ban_reason"] = ""
        db_save(d)
        audit(m.from_user.id, "unban_user", f"uid={uid}")
        try:
            bot.send_message(uid,
                             f"<b>{G['ok']} {sc('You have been unbanned')}</b>",
                             parse_mode="HTML")
        except Exception:
            pass
        bot.reply_to(m, f"{G['ok']} {sc('unbanned')} {uid}"); return


# ─── admin: give plan ─────────────────────────────────────────────
def _handle_giveplan_cmd(m: types.Message) -> None:
    if not is_admin(m.from_user.id):
        return
    if not admin_can(m.from_user.id, "give_plan"):
        bot.reply_to(m, f"{G['no']} {sc('insufficient permission')}"); return
    USER_STATES.pop(m.from_user.id, None)
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, f"{G['no']} {sc('format')}: <code>Uɪᴅ Pʟᴀɴ [Dᴀʏꜱ]</code>",
                     parse_mode="HTML"); return
    try:
        uid = int(parts[0])
    except Exception:
        bot.reply_to(m, f"{G['no']} {sc('bad uid')}"); return
    plan = parts[1]
    if plan not in PLAN_LIMITS:
        bot.reply_to(m, f"{G['no']} {sc('bad plan')}"); return
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
    if not grant_plan(uid, plan, days=days):
        bot.reply_to(m, f"{G['no']} {sc('failed')}"); return
    audit(m.from_user.id, "give_plan", f"uid={uid} plan={plan} days={days}")
    bot.reply_to(m, f"{G['ok']} {sc('granted')} {plan} {sc('to')} {uid}")


# ─── admin: broadcast ─────────────────────────────────────────────
def _handle_broadcast(m: types.Message) -> None:
    if not is_admin(m.from_user.id):
        return
    USER_STATES.pop(m.from_user.id, None)
    text = m.text or ""
    target_plan: Optional[str] = None
    schedule_at: Optional[datetime] = None

    # parse first-line directives
    while True:
        head, _, rest = text.partition("\n")
        head = head.strip()
        if head.startswith("plan:"):
            target_plan = head.split(":", 1)[1].strip().lower()
            text = rest
        elif head.startswith("at:"):
            try:
                schedule_at = datetime.strptime(head[3:].strip(),
                                                "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except Exception:
                bot.reply_to(m, f"{G['no']} {sc('bad time format, use YYYY-MM-DD HH:MM UTC')}")
                return
            text = rest
        else:
            break

    text = text.strip()
    if not text:
        bot.reply_to(m, f"{G['no']} {sc('empty broadcast')}"); return

    if schedule_at:
        d = db_load()
        d["scheduled_broadcasts"].append({
            "at": schedule_at.isoformat(),
            "text": text,
            "plan": target_plan,
            "by": m.from_user.id,
        })
        db_save(d)
        audit(m.from_user.id, "broadcast_schedule",
              f"at={schedule_at.isoformat()} plan={target_plan}")
        bot.reply_to(m, f"{G['ok']} {sc('scheduled for')} {fmt_ts(schedule_at.isoformat())}")
        return

    sent, skipped = _send_broadcast(text, target_plan)
    audit(m.from_user.id, "broadcast", f"sent={sent} skipped={skipped} plan={target_plan}")
    bot.reply_to(m, f"{G['ok']} {sc('broadcast done')} — Sᴇɴᴛ {sent}, Sᴋɪᴘᴘᴇᴅ {skipped}")


def _send_broadcast(text: str, target_plan: Optional[str]) -> Tuple[int, int]:
    sent = skipped = 0
    d = db_load()
    for u in d["users"].values():
        if u.get("banned"):
            skipped += 1; continue
        if target_plan and u.get("plan") != target_plan:
            skipped += 1; continue
        try:
            bot.send_message(int(u["_id"]), text, parse_mode="HTML",
                             disable_web_page_preview=True)
            sent += 1
            time.sleep(0.04)  # gentle throttle
        except Exception:
            skipped += 1
    return sent, skipped


# ─── coupons ──────────────────────────────────────────────────────
def _handle_coupon_user(m: types.Message) -> None:
    USER_STATES.pop(m.from_user.id, None)
    code = m.text.strip().upper()
    d = db_load()
    c = d["coupons"].get(code)
    if not c or int(c.get("uses_left", 0)) <= 0:
        bot.reply_to(m, f"{G['no']} {sc('invalid or expired code')}"); return
    pct = int(c.get("percent", 0))
    u = d["users"][str(m.from_user.id)]
    u["wallet"] = int(u.get("wallet", 0)) + pct  # treat % as wallet credit (simple)
    c["uses_left"] = int(c["uses_left"]) - 1
    db_save(d)
    bot.reply_to(m, f"{G['ok']} {sc('redeemed')} +{pct}\u09F3 {sc('to wallet')}")


def _handle_coupon_admin(m: types.Message) -> None:
    if not is_admin(m.from_user.id):
        return
    USER_STATES.pop(m.from_user.id, None)
    parts = m.text.split()
    if len(parts) < 2:
        bot.reply_to(m, f"{G['no']} {sc('format')}: <code>Aᴅᴅ Cᴏᴅᴇ Pᴄᴛ Uꜱᴇꜱ</code> | <code>Dᴇʟ Cᴏᴅᴇ</code>",
                     parse_mode="HTML"); return
    op = parts[0].lower()
    d = db_load()
    if op == "add" and len(parts) >= 4:
        code = parts[1].upper()
        try:
            pct = int(parts[2]); uses = int(parts[3])
        except Exception:
            bot.reply_to(m, f"{G['no']} {sc('bad numbers')}"); return
        d["coupons"][code] = {"percent": pct, "uses_left": uses}
        db_save(d)
        audit(m.from_user.id, "coupon_add", f"code={code} pct={pct} uses={uses}")
        bot.reply_to(m, f"{G['ok']} {sc('added')} {code}"); return
    if op == "del" and len(parts) >= 2:
        code = parts[1].upper()
        if d["coupons"].pop(code, None):
            db_save(d)
            audit(m.from_user.id, "coupon_del", f"code={code}")
            bot.reply_to(m, f"{G['ok']} {sc('removed')} {code}"); return
        bot.reply_to(m, f"{G['no']} {sc('no such code')}"); return


# ─── admin: admins ───────────────────────────────────────────────
def _handle_admin_admins(m: types.Message) -> None:
    if not is_owner(m.from_user.id):
        return
    USER_STATES.pop(m.from_user.id, None)
    parts = m.text.split()
    if len(parts) < 2:
        return
    op = parts[0].lower()
    d = db_load()
    if op == "add" and len(parts) >= 3:
        try:
            uid = int(parts[1])
        except Exception:
            bot.reply_to(m, f"{G['no']} {sc('bad uid')}"); return
        role = parts[2]
        if role not in {"view-only", "manage-users", "full-access"}:
            bot.reply_to(m, f"{G['no']} {sc('bad role')}"); return
        d["admins"][str(uid)] = {"role": role, "added": ts_iso(), "by": m.from_user.id}
        db_save(d)
        audit(m.from_user.id, "admin_add", f"uid={uid} role={role}")
        bot.reply_to(m, f"{G['ok']} {sc('added admin')} {uid} ({role})"); return
    if op == "del" and len(parts) >= 2:
        try:
            uid = int(parts[1])
        except Exception:
            bot.reply_to(m, f"{G['no']} {sc('bad uid')}"); return
        if d["admins"].pop(str(uid), None):
            db_save(d)
            audit(m.from_user.id, "admin_del", f"uid={uid}")
            bot.reply_to(m, f"{G['ok']} {sc('removed')} {uid}"); return


# ─── tickets ──────────────────────────────────────────────────────
def _handle_ticket_subject(m: types.Message) -> None:
    USER_STATES[m.from_user.id] = {"flow": "await_ticket_body", "subject": m.text.strip()[:120]}
    bot.reply_to(m, f"{G['ticket']} {sc('Now send the ticket body')}.")


def _handle_ticket_body(m: types.Message, st: Dict[str, Any]) -> None:
    subject = st.get("subject") or "Support"
    d = db_load()
    tid = rand_token(6)
    d["tickets"][tid] = {
        "id": tid, "uid": m.from_user.id, "subject": subject, "status": "open",
        "messages": [{"from": "user", "text": m.text, "ts": ts_iso()}],
        "opened_at": ts_iso(),
    }
    db_save(d)
    USER_STATES.pop(m.from_user.id, None)
    bot.reply_to(m, f"<b>{G['ok']} {sc('Ticket opened')} #{tid}</b>", parse_mode="HTML")
    notify_owner(
        f"<b>{G['ticket']} ɴᴇᴡ ᴛɪᴄᴋᴇᴛ #{tid}</b>\n"
        f"{bullet('From', m.from_user.id)}\n"
        f"{bullet('Subject', subject)}\n"
        f"{bullet('Body', m.text[:400])}"
    )


def _handle_ticket_reply(m: types.Message, st: Dict[str, Any]) -> None:
    tid = st.get("tid")
    d = db_load()
    t = d["tickets"].get(tid)
    if not t:
        USER_STATES.pop(m.from_user.id, None); return
    if t["uid"] != m.from_user.id and not is_admin(m.from_user.id):
        USER_STATES.pop(m.from_user.id, None); return
    who = "admin" if is_admin(m.from_user.id) and t["uid"] != m.from_user.id else "user"
    t.setdefault("messages", []).append({"from": who, "text": m.text, "ts": ts_iso()})
    db_save(d)
    USER_STATES.pop(m.from_user.id, None)
    target = OWNER_ID if who == "user" else t["uid"]
    try:
        bot.send_message(
            target,
            f"<b>{G['ticket']} {sc('Ticket')} #{tid}</b> — {sc(who + ' replied')}\n"
            f"{esc(m.text)[:1000]}",
            parse_mode="HTML",
        )
    except Exception:
        pass
    bot.reply_to(m, f"{G['ok']} {sc('reply sent')}")


# ─── payment proof ───────────────────────────────────────────────
def _handle_payment_proof(m: types.Message, st: Dict[str, Any]) -> None:
    method = st.get("method") or "unknown"
    plan = st.get("plan")
    p = PLAN_LIMITS.get(plan or "")
    pid = rand_token(8)
    d = db_load()
    d["payments"].append({
        "id": pid, "uid": m.from_user.id, "method": method, "plan": plan,
        "amount": (p or {}).get("price", 0),
        "status": "pending", "ts": ts_iso(),
        "telegram_msg_id": m.message_id,
    })
    db_save(d)
    USER_STATES.pop(m.from_user.id, None)
    # forward proof to owner
    try:
        bot.forward_message(OWNER_ID, m.chat.id, m.message_id)
    except Exception:
        pass
    notify_owner(
        f"<b>{G['wallet']} ɴᴇᴡ ᴘᴀʏᴍᴇɴᴛ ᴘʀᴏᴏғ</b>\n"
        f"{bullet('ID',     pid)}\n"
        f"{bullet('From',   m.from_user.id)}\n"
        f"{bullet('Method', method)}\n"
        f"{bullet('Plan',   plan or '—')}\n"
        f"{bullet('Amount', '{}$'.format((p or {}).get('price', 0)))}\n"
        f"{sc('Tap below to approve or reject')}.",
    )
    kb = types.InlineKeyboardMarkup()
    kb.add(
        Btn(f"{G['ok']}  {sc('Approve')}", callback_data=f"payapprove_{pid}"),
        Btn(f"{G['no']}  {sc('Reject')}",  callback_data=f"payreject_{pid}"),
    )
    try:
        bot.send_message(OWNER_ID, f"<b>{sc('Decide')} #{pid}</b>",
                         parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    bot.reply_to(m, f"<b>{G['ok']} {sc('proof received')}</b>\n#{pid} — {sc('await admin')}",
                 parse_mode="HTML")


def _handle_payment_proof_text(m: types.Message, st: Dict[str, Any]) -> None:
    # text-only proofs (e.g. tx ids)
    _handle_payment_proof(m, st)


def _handle_topup_proof(m: types.Message) -> None:
    pid = rand_token(8)
    cap = (m.caption or m.text or "").strip()
    amt = 0
    if cap.isdigit():
        amt = int(cap)
    else:
        ms = re.search(r"\d+", cap)
        if ms:
            amt = int(ms.group(0))
    d = db_load()
    d["payments"].append({
        "id": pid, "uid": m.from_user.id, "method": "topup", "plan": None,
        "amount": amt, "status": "pending", "ts": ts_iso(),
        "telegram_msg_id": m.message_id, "kind": "wallet_topup",
    })
    db_save(d)
    USER_STATES.pop(m.from_user.id, None)
    try:
        bot.forward_message(OWNER_ID, m.chat.id, m.message_id)
    except Exception:
        pass
    kb = types.InlineKeyboardMarkup()
    kb.add(
        Btn(f"{G['ok']}  {sc('Approve')}", callback_data=f"payapprove_{pid}"),
        Btn(f"{G['no']}  {sc('Reject')}",  callback_data=f"payreject_{pid}"),
    )
    notify_owner(
        f"<b>{G['wallet']} ᴡᴀʟʟᴇᴛ ᴛᴏᴘᴜᴘ</b>\n"
        f"{bullet('ID',     pid)}\n"
        f"{bullet('From',   m.from_user.id)}\n"
        f"{bullet('Amount', '{}$'.format(amt))}"
    )
    try:
        bot.send_message(OWNER_ID, f"<b>{sc('Decide')} #{pid}</b>",
                         parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    bot.reply_to(m, f"<b>{G['ok']} {sc('top-up proof received')}</b>", parse_mode="HTML")


def action_payment_approve(call: types.CallbackQuery, pid: str) -> None:
    if not admin_only_call(call, "approve_payment"):
        return
    d = db_load()
    pay = next((x for x in d["payments"] if x.get("id") == pid), None)
    if not pay:
        ack(call, "Not found"); return
    # Idempotency: rapid double-tap on Approve must not credit twice or
    # grant the plan twice. Refuse if status is already a terminal one.
    if pay.get("status") in ("approved", "rejected"):
        ack(call, f"Already {pay['status']}.")
        return
    loading(call, "Approving payment")
    pay["status"] = "approved"
    pay["approved_by"] = call.from_user.id
    pay["approved_at"] = ts_iso()
    db_save(d)
    if pay.get("kind") == "wallet_topup":
        u = d["users"].get(str(pay["uid"]))
        if u:
            u["wallet"] = int(u.get("wallet", 0)) + int(pay.get("amount", 0))
            db_save(d)
            try:
                bot.send_message(pay["uid"],
                                 f"<b>{G['ok']} {sc('Wallet credited')}</b>\n"
                                 f"{bullet('Amount', '{}$'.format(pay['amount']))}",
                                 parse_mode="HTML")
            except Exception:
                pass
    elif pay.get("plan"):
        grant_plan(pay["uid"], pay["plan"])
        post_announcement(
            f"<b>{G['spark']} ɴᴇᴡ ᴀᴄᴛɪᴠᴀᴛɪᴏɴ</b>\n"
            f"{bullet('Plan', PLAN_LIMITS[pay['plan']]['name'])}\n"
            f"{bullet('User', '@hidden')}"
        )
    audit(call.from_user.id, "pay_approve", f"pid={pid}")
    ack(call, "Approved")
    try:
        bot.edit_message_text(f"<b>{G['ok']} {sc('Approved')} #{pid}</b>",
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id, parse_mode="HTML")
    except Exception:
        pass


def action_payment_reject(call: types.CallbackQuery, pid: str) -> None:
    if not admin_only_call(call, "approve_payment"):
        return
    d = db_load()
    pay = next((x for x in d["payments"] if x.get("id") == pid), None)
    if not pay:
        ack(call, "Not found"); return
    if pay.get("status") in ("approved", "rejected"):
        ack(call, f"Already {pay['status']}.")
        return
    loading(call, "Rejecting payment")
    pay["status"] = "rejected"
    pay["rejected_by"] = call.from_user.id
    pay["rejected_at"] = ts_iso()
    db_save(d)
    audit(call.from_user.id, "pay_reject", f"pid={pid}")
    try:
        bot.send_message(pay["uid"],
                         f"<b>{G['no']} {sc('Payment rejected')}</b> #{pid}\n"
                         f"{sc('Contact')} {SUPPORT_USR}",
                         parse_mode="HTML")
    except Exception:
        pass
    ack(call, "Rejected")
    try:
        bot.edit_message_text(f"<b>{G['no']} {sc('Rejected')} #{pid}</b>",
                              chat_id=call.message.chat.id,
                              message_id=call.message.message_id, parse_mode="HTML")
    except Exception:
        pass


# ─── gift plan flow ───────────────────────────────────────────────
def _handle_gift_target(m: types.Message, st: Dict[str, Any]) -> None:
    try:
        tgt = int(m.text.strip())
    except Exception:
        bot.reply_to(m, f"{G['no']} {sc('bad uid')}"); return
    d = db_load()
    if str(tgt) not in d["users"]:
        bot.reply_to(m, f"{G['no']} {sc('user not found')}"); return
    USER_STATES[m.from_user.id] = {"flow": "await_gift_confirm", "target": tgt}
    bot.reply_to(
        m,
        f"<b>{G['warn']} {sc('Confirm gift')}</b>\n"
        f"{bullet('To',   tgt)}\n"
        f"{bullet('Plan', d['users'][str(m.from_user.id)].get('plan'))}\n"
        f"{sc('Send')} <code>YES</code> {sc('to confirm or anything else to cancel')}.",
        parse_mode="HTML",
    )


def _handle_gift_confirm(m: types.Message, st: Dict[str, Any]) -> None:
    USER_STATES.pop(m.from_user.id, None)
    if (m.text or "").strip().upper() != "YES":
        bot.reply_to(m, f"{G['no']} {sc('cancelled')}"); return
    tgt = int(st["target"])
    d = db_load()
    me = d["users"][str(m.from_user.id)]
    if me.get("plan") in ("free", None):
        bot.reply_to(m, f"{G['no']} {sc('no active plan to gift')}"); return
    plan = me["plan"]; exp = me.get("plan_expires")
    me["plan"] = "free"; me["plan_expires"] = None
    if str(tgt) in d["users"]:
        d["users"][str(tgt)]["plan"] = plan
        d["users"][str(tgt)]["plan_expires"] = exp
    db_save(d)
    audit(m.from_user.id, "plan_gift", f"to={tgt} plan={plan}")
    bot.reply_to(m, f"{G['ok']} {sc('plan gifted to')} {tgt}")
    try:
        bot.send_message(tgt,
                         f"<b>{G['spark']} {sc('You received a gift plan')}</b>\n"
                         f"{bullet('Plan', PLAN_LIMITS[plan]['name'])}",
                         parse_mode="HTML")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════
# 23. SCHEDULER  (background loops)
# ═════════════════════════════════════════════════════════════════

def cron_runner() -> None:
    """Every minute: cron jobs, expiry reminders, scheduled broadcasts, downgrades."""
    last_per_bot: Dict[str, Dict[str, float]] = {}
    while True:
        try:
            now = time.time()
            d = db_load()

            # plan expiry + reminders
            downgrade_expired_users()
            expiry_reminders()

            # scheduled broadcasts
            sb = d.get("scheduled_broadcasts", [])
            kept: List[Dict[str, Any]] = []
            for b in sb:
                try:
                    when = datetime.fromisoformat(str(b["at"]).replace("Z", "+00:00"))
                except Exception:
                    continue
                if when <= now_utc():
                    _send_broadcast(b["text"], b.get("plan"))
                    audit(b.get("by", 0), "broadcast_run", "scheduled")
                else:
                    kept.append(b)
            if len(kept) != len(sb):
                d["scheduled_broadcasts"] = kept
                db_save(d)

            # per-bot cron (restart / backup)
            for bid, bdoc in db_load()["bots"].items():
                cron = bdoc.get("cron") or {}
                last = last_per_bot.setdefault(bid, {})
                if cron.get("restart_hours"):
                    iv = int(cron["restart_hours"]) * 3600
                    if now - last.get("restart", 0) >= iv:
                        try:
                            restart_child(bdoc)
                        except Exception:
                            pass
                        last["restart"] = now
                if cron.get("backup_hours"):
                    iv = int(cron["backup_hours"]) * 3600
                    if now - last.get("backup", 0) >= iv:
                        try:
                            res = gh_backup_now()
                            if not res.get("ok"):
                                print(f"[cron] backup failed: {res.get('error')}",
                                      flush=True)
                        except Exception as e:
                            print(f"[cron] backup error: {e}", flush=True)
                            traceback.print_exc()
                        last["backup"] = now

            # ── auto backup — sirf tab jab koi bot 10+ min se online ho ──
            should_backup = False
            for bid, rinfo in list(RUNNING.items()):
                started_ms = rinfo.get("started", 0)
                online_sec = (time.time() * 1000 - started_ms) / 1000
                if online_sec >= 600:  # 10 min = 600 sec
                    should_backup = True
                    break
            if should_backup:
                try:
                    res = gh_backup_now()
                    if res.get("ok"):
                        print("[cron] auto backup ok", flush=True)
                    else:
                        print(f"[cron] auto backup failed: {res.get('error')}", flush=True)
                except Exception as e:
                    print(f"[cron] auto backup error: {e}", flush=True)

        except Exception:
            traceback.print_exc()
        time.sleep(60)


# ═════════════════════════════════════════════════════════════════
# 24. BOOTSTRAP / MAIN
# ═════════════════════════════════════════════════════════════════

def banner() -> None:
    line = "=" * 64
    print(line)
    print(f"   {BRAND_TAG}")
    print(f"   uptime port : {KEEPALIVE_PORT}")
    print(f"   owner id    : {OWNER_ID}")
    print(f"   github keys : {'GitHub' if KEYRING.gh_enabled() else 'local cache'}")
    print(f"   github bkp  : {'on' if gh_enabled() else 'off'}")
    print(f"   announcements: {ANNOUNCE_CHANNEL or '—'}")
    print(line)


def _acquire_singleton_lock() -> Optional[Any]:
    """Best-effort single-instance guard. Prevents two local copies of the
    panel from running on the same machine (which would otherwise both poll
    the same token and deliver every callback twice). Returns the file
    handle to keep alive for the lifetime of the process, or None on
    platforms where fcntl isn't available (e.g. Windows)."""
    try:
        import fcntl
    except ImportError:
        return None
    lock_path = DIRS["data"] / "panel.lock"
    try:
        fh = open(lock_path, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except OSError:
        sys.exit(
            "[x] another panel instance is already running on this machine "
            f"(see {lock_path}). Stop it first, otherwise every button press "
            "will be processed twice."
        )


def main() -> int:
    banner()
    global _LOCK_FH_KEEPALIVE
    _LOCK_FH_KEEPALIVE = _acquire_singleton_lock()
    # restore previously auto-claimed owner (when OWNER_ID env is unset)
    global OWNER_ID, BRAND_TAG, ANNOUNCE_CHANNEL
    stored_owner = int(get_setting("owner_id", 0) or 0)
    if stored_owner > 0:
        # An admin transfer takes precedence; otherwise the env-var
        # owner is the source of truth.
        OWNER_ID = stored_owner if OWNER_ID <= 0 or stored_owner != OWNER_ID else OWNER_ID
        if OWNER_ID <= 0:
            OWNER_ID = stored_owner
    # restore admin-edited brand & announce channel
    bt = get_setting("brand_tag", None)
    if isinstance(bt, str) and bt:
        BRAND_TAG = bt
    ac = get_setting("announce_channel", None)
    if isinstance(ac, str):
        ANNOUNCE_CHANNEL = ac
    gh_load_config()
    GH["autoEnabled"] = bool(get_setting("github_auto_enabled", True))

    # restore from GitHub if storage is empty
    try:
        res = gh_auto_restore_on_boot()
        if res and res.get("ok"):
            print(f"[boot] restored backup ({fmt_bytes(res.get('sizeBytes', 0))})")
    except Exception:
        pass

    # background services
    threading.Thread(target=gh_auto_loop, daemon=True).start()
    threading.Thread(target=gh_uptime_backup_loop, daemon=True,
                     name="gh-uptime-backup").start()
    threading.Thread(target=cron_runner, daemon=True).start()
    threading.Thread(target=_verify_state_janitor, daemon=True,
                     name="verify-janitor").start()
    _start_keepalive()

    # set bot commands
    try:
        bot.set_my_commands([
            types.BotCommand("start",  "open main menu"),
            types.BotCommand("menu",   "main menu"),
            types.BotCommand("help",   "show help"),
            types.BotCommand("id",     "show your user id"),
            types.BotCommand("cancel", "cancel current action"),
        ])
    except Exception:
        pass

    notify_owner(
        f"<b>{G['ok']} {sc('Panel online')}</b>\n"
        f"{bullet('Brand',  BRAND_TAG)}\n"
        f"{bullet('Started', fmt_ts(ts_iso()))}\n"
        f"{bullet('Users',  len(db_load()['users']))}\n"
        f"{bullet('Bots',   len(db_load()['bots']))}"
    )

    # autostart bots that were marked running
    for b in db_load()["bots"].values():
        if b.get("status") == "running":
            try:
                start_child(b)
            except Exception:
                pass

    # ── clear any leftover webhook so polling is the only delivery path ──
    # If a webhook is still registered for this token (from a previous host
    # or a different deployment), Telegram will keep posting updates to it
    # AND deliver them to our polling loop, causing every callback to fire
    # 2-3 times. drop_pending_updates also clears the backlog so we start
    # fresh.
    try:
        bot.remove_webhook()
        try:
            bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        print("[bot] webhook cleared")
    except Exception as e:
        print(f"[bot] webhook clear warning: {e}")

    print("[bot] polling...")
    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
        except KeyboardInterrupt:
            print("\n[bot] stopping...")
            for bid in list(RUNNING.keys()):
                stop_child(bid, manual=False)
            return 0
        except Exception as e:
            print(f"[bot] poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
