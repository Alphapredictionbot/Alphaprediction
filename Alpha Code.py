#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
===========================================================
🤖 ALPHA AI MARKET BOT (AUTO-TRADE v3.0)
===========================================================

FEATURES:
- Confidence Filter (≥70%) before executing trades
- Optimized for small balances (as low as $7)
- Single trade at a time
- /pause and /resume commands
- Max Daily Loss (10% auto-pause)
- Auto-resume at midnight UTC
- Min quantity protection for altcoins (DOGE, XRP, ADA)
- Manual trade close (/closetrade)
- All API keys moved to .env file for security
"""

import os
import re
import asyncio
import json
import base64
import threading
from concurrent.futures import ThreadPoolExecutor
import io
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

# =========================================================
# AUTO-TRADE: CCXT (Binance)
# =========================================================
try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    print("⚠️ CCXT not installed. Install: pip install ccxt")

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# OPTIONAL PIL
# =========================================================

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠️ Pillow not installed.")

# =========================================================
# ENVIRONMENT - ALL KEYS LOADED FROM .env
# =========================================================

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "https://powerful-snail-161868.upstash.io")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "5910331523").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "AlphaPredictAI_bot").strip().lstrip("@").replace(" ", "")
try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except Exception:
    ADMIN_ID = 0

# =========================================================
# AUTO-TRADE ENV VARS
# =========================================================
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "").strip()
BINANCE_TESTNET = os.getenv("BINANCE_TESTNET", "True").lower() in ("true", "1", "yes")
AUTO_TRADE_SYMBOL = os.getenv("AUTO_TRADE_SYMBOL", "DOGE/USDT").strip()
AUTO_TRADE_RISK_PCT = float(os.getenv("AUTO_TRADE_RISK_PCT", "3.0").strip())
AUTO_TRADE_INTERVAL = int(os.getenv("AUTO_TRADE_INTERVAL", "60").strip())
MIN_CONFIDENCE_TO_TRADE = float(os.getenv("MIN_CONFIDENCE_TO_TRADE", "70.0").strip())
MIN_TRADE_QUANTITY = float(os.getenv("MIN_TRADE_QUANTITY", "1.0").strip())
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "10.0").strip())

# =========================================================
# UPSTASH REDIS (REST)
# =========================================================

if UPSTASH_REDIS_REST_URL and not UPSTASH_REDIS_REST_URL.startswith(("http://", "https://")):
    UPSTASH_REDIS_REST_URL = "https://" + UPSTASH_REDIS_REST_URL
UPSTASH_REDIS_TIMEOUT = int(os.getenv("UPSTASH_REDIS_TIMEOUT", "4").strip() or "4")
UPSTASH_PREFIX = os.getenv("UPSTASH_PREFIX", "alpha_ai_bot").strip() or "alpha_ai_bot"
UPSTASH_ENABLED = bool(UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN)

# =========================================================
# SELF-LEARNING ENGINE
# =========================================================

SELF_LEARNING_FILE = "self_learning.json"
SELF_LEARNING_MAX_RECORDS = 2000
SELF_LEARNING_MIN_SAMPLES = 8
SELF_LEARNING_ENABLED = os.getenv("SELF_LEARNING_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off"
}

def _upstash_key(filename):
    return f"{UPSTASH_PREFIX}:{filename}"

def _upstash_get(filename):
    if not UPSTASH_ENABLED:
        return None
    try:
        r = requests.get(
            f"{UPSTASH_REDIS_REST_URL}/get/{_upstash_key(filename)}",
            headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"},
            timeout=UPSTASH_REDIS_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        value = payload.get("result")
        return None if value is None else json.loads(value)
    except Exception as e:
        print(f"⚠️ Upstash GET failed for {filename}: {e}")
        return None

def _upstash_set(filename, data):
    if not UPSTASH_ENABLED:
        return False
    try:
        value = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        r = requests.post(
            f"{UPSTASH_REDIS_REST_URL}/set/{_upstash_key(filename)}",
            headers={
                "Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}",
                "Content-Type": "text/plain; charset=utf-8",
            },
            data=value.encode("utf-8"),
            timeout=UPSTASH_REDIS_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("result") != "OK":
            raise RuntimeError(payload.get("error", "Unknown Redis error"))
        return True
    except Exception as e:
        print(f"⚠️ Upstash SET failed for {filename}: {e}")
        return False

# =========================================================
# GROQ
# =========================================================

GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "openai/gpt-oss-120b").strip()
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.6-27b").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# =========================================================
# VALIDATION
# =========================================================

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is missing. Please set it in .env file.")

if not GROQ_API_KEY:
    print("⚠️ GROQ_API_KEY is missing. Please set it in .env file.")

# =========================================================
# FILES
# =========================================================

USERS_FILE = "users_data.json"
ALERTS_FILE = "price_alerts.json"
PAYMENTS_FILE = "payments.json"
REFERRALS_FILE = "referrals.json"
FEEDBACK_FILE = "feedback.json"
ACTIVE_TRADE_FILE = "active_trade.json"
PAUSE_FILE = "pause_state.json"
DAILY_STATS_FILE = "daily_stats.json"

file_lock = threading.Lock()

def load_json(filename, default):
    remote = _upstash_get(filename)
    if remote is not None:
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(remote, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return remote
    try:
        if os.path.exists(filename):
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"JSON load error {filename}: {e}")
    return default

def save_json(filename, data):
    with file_lock:
        temp_file = filename + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, filename)
    if UPSTASH_ENABLED:
        _upstash_set(filename, data)

print("☁️ Upstash Redis: ON" if UPSTASH_ENABLED else "☁️ Upstash Redis: OFF (local JSON fallback)")

users_data = load_json(USERS_FILE, {})
alerts_data = load_json(ALERTS_FILE, {})
payments_data = load_json(PAYMENTS_FILE, {})
referral_data = load_json(REFERRALS_FILE, {})
feedback_data = load_json(FEEDBACK_FILE, {})
active_trade = load_json(ACTIVE_TRADE_FILE, {
    "symbol": None,
    "entry_price": 0.0,
    "sl": 0.0,
    "tp1": 0.0,
    "tp2": 0.0,
    "quantity": 0.0,
    "side": None,
    "open_time": None
})

def save_active_trade(data):
    save_json(ACTIVE_TRADE_FILE, data)

# =========================================================
# PAUSE / RESUME & DAILY LOSS LIMIT
# =========================================================
def get_pause_state():
    """Returns (paused: bool, reason: str) where reason is 'manual' or 'daily_loss'."""
    try:
        data = load_json(PAUSE_FILE, {"paused": False, "reason": None})
        return data.get("paused", False), data.get("reason")
    except:
        return False, None

def set_pause_state(paused: bool, reason: str = None):
    """Set pause state. reason: 'manual' or 'daily_loss'."""
    save_json(PAUSE_FILE, {"paused": paused, "reason": reason})

def get_daily_stats():
    """Return today's total PnL in USD and the date."""
    default = {"date": now().strftime("%Y-%m-%d"), "total_pnl": 0.0, "trade_count": 0}
    try:
        data = load_json(DAILY_STATS_FILE, default)
        if data.get("date") != now().strftime("%Y-%m-%d"):
            data = {"date": now().strftime("%Y-%m-%d"), "total_pnl": 0.0, "trade_count": 0}
            save_json(DAILY_STATS_FILE, data)
        return data
    except:
        return default

def update_daily_stats(pnl_amount: float):
    """Add PnL to today's total and save."""
    stats = get_daily_stats()
    stats["total_pnl"] = round(stats["total_pnl"] + pnl_amount, 4)
    stats["trade_count"] = stats.get("trade_count", 0) + 1
    save_json(DAILY_STATS_FILE, stats)
    return stats

def check_daily_loss_limit():
    """Check if daily loss limit is hit. If so, returns True with details."""
    stats = get_daily_stats()
    try:
        if exchange:
            balance = exchange.fetch_balance()
            current_balance = balance['USDT']['free']
        else:
            current_balance = 7.0
    except:
        current_balance = 7.0

    max_loss_amount = current_balance * (MAX_DAILY_LOSS_PCT / 100.0)

    if stats["total_pnl"] <= -max_loss_amount:
        return True, stats["total_pnl"], max_loss_amount
    return False, stats["total_pnl"], max_loss_amount

def now():
    return datetime.now(timezone.utc)

def ensure_utc_datetime(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def iso(dt):
    return ensure_utc_datetime(dt).isoformat()

SUBSCRIPTION_DAYS = 30
SUBSCRIPTION_PRICE_ETB = 500

ALLOWED_PHONES = ["0956967050", "0925530098", "0725530098"]

# =========================================================
# LANGUAGE DICTIONARIES
# =========================================================
LANGUAGE_NAMES = {
    "en": "English",
    "am": "Amharic",
    "om": "Afaan Oromoo",
    "ar": "Arabic",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "tr": "Turkish",
    "hi": "Hindi",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
}

LANGUAGE_OUTPUT_INSTRUCTIONS = {
    "en": "English",
    "am": "Amharic (አማርኛ)",
    "om": "Afaan Oromoo (Oromiffa)",
    "ar": "Arabic (العربية)",
    "fr": "French (Français)",
    "es": "Spanish (Español)",
    "pt": "Portuguese (Português)",
    "ru": "Russian (Русский)",
    "tr": "Turkish (Türkçe)",
    "hi": "Hindi (हिन्दी)",
    "zh": "Chinese (中文)",
    "ja": "Japanese (日本語)",
    "ko": "Korean (한국어)",
}

LANGUAGES = {
    "en": {"name": "🇬🇧 English", "welcome": "Welcome!", "choose": "Choose:", "crypto": "🪙 Crypto", "forex": "💱 Forex", "prediction": "🔮 Prediction", "alerts": "🔔 Alerts", "economy": "🌍 Economy", "payment": "💰 Payment", "referral": "🔗 Referral", "feedback": "📝 Feedback", "legal": "⚖️ Legal", "help": "❓ Help", "chart": "📊 Chart", "back": "🔙 Back"},
    "am": {"name": "🇪🇹 አማርኛ", "welcome": "እንኳን ደህና መጡ!", "choose": "ምረጥ:", "crypto": "🪙 ክሪፕቶ", "forex": "💱 ፎሬክስ", "prediction": "🔮 ትንበያ", "alerts": "🔔 ማስጠንቀቂያ", "economy": "🌍 ኢኮኖሚ", "payment": "💰 ክፍያ", "referral": "🔗 ማጋበዣ", "feedback": "📝 አስተያየት", "legal": "⚖️ ህግ", "help": "❓ እርዳታ", "chart": "📊 ገበታ", "back": "🔙 ተመለስ"},
    "om": {"name": "🇪🇹 Oromiffa", "welcome": "Baga dhuftan!", "choose": "Filadhu:", "crypto": "🪙 Crypto", "forex": "💱 Forex", "prediction": "🔮 Raajii", "alerts": "🔔 Akeekkachiisa", "economy": "🌍 Diinagdee", "payment": "💰 Kaffaltii", "referral": "🔗 Affeeruu", "feedback": "📝 Yaada", "legal": "⚖️ Seera", "help": "❓ Gargaarsa", "chart": "📊 Chaartii", "back": "🔙 Duuba"},
    "ar": {"name": "🇦🇪 العربية", "welcome": "مرحباً!", "choose": "اختر:", "crypto": "🪙 عملات رقمية", "forex": "💱 فوركس", "prediction": "🔮 تنبؤ", "alerts": "🔔 تنبيهات", "economy": "🌍 اقتصاد", "payment": "💰 دفع", "referral": "🔗 إحالة", "feedback": "📝 ملاحظات", "legal": "⚖️ قانوني", "help": "❓ مساعدة", "chart": "📊 تحليل", "back": "🔙 رجوع"},
    "fr": {"name": "🇫🇷 Français", "welcome": "Bienvenue!", "choose": "Choisissez:", "crypto": "🪙 Crypto", "forex": "💱 Forex", "prediction": "🔮 Prédiction", "alerts": "🔔 Alertes", "economy": "🌍 Économie", "payment": "💰 Paiement", "referral": "🔗 Parrainage", "feedback": "📝 Commentaire", "legal": "⚖️ Légal", "help": "❓ Aide", "chart": "📊 Graphique", "back": "🔙 Retour"},
    "es": {"name": "🇪🇸 Español", "welcome": "¡Bienvenido!", "choose": "Elige:", "crypto": "🪙 Cripto", "forex": "💱 Forex", "prediction": "🔮 Predicción", "alerts": "🔔 Alertas", "economy": "🌍 Economía", "payment": "💰 Pago", "referral": "🔗 Referidos", "feedback": "📝 Comentarios", "legal": "⚖️ Legal", "help": "❓ Ayuda", "chart": "📊 Gráfico", "back": "🔙 Atrás"},
    "pt": {"name": "🇵🇹 Português", "welcome": "Bem-vindo!", "choose": "Escolha:", "crypto": "🪙 Cripto", "forex": "💱 Forex", "prediction": "🔮 Previsão", "alerts": "🔔 Alertas", "economy": "🌍 Economia", "payment": "💰 Pagamento", "referral": "🔗 Referência", "feedback": "📝 Feedback", "legal": "⚖️ Legal", "help": "❓ Ajuda", "chart": "📊 Análise", "back": "🔙 Voltar"},
    "ru": {"name": "🇷🇺 Русский", "welcome": "Добро пожаловать!", "choose": "Выберите:", "crypto": "🪙 Крипто", "forex": "💱 Форекс", "prediction": "🔮 Прогноз", "alerts": "🔔 Уведомления", "economy": "🌍 Экономика", "payment": "💰 Оплата", "referral": "🔗 Реферал", "feedback": "📝 Отзыв", "legal": "⚖️ Правовое", "help": "❓ Помощь", "chart": "📊 График", "back": "🔙 Назад"},
    "tr": {"name": "🇹🇷 Türkçe", "welcome": "Hoş geldiniz!", "choose": "Seçin:", "crypto": "🪙 Kripto", "forex": "💱 Forex", "prediction": "🔮 Tahmin", "alerts": "🔔 Uyarılar", "economy": "🌍 Ekonomi", "payment": "💰 Ödeme", "referral": "🔗 Referans", "feedback": "📝 Geri Bildirim", "legal": "⚖️ Yasal", "help": "❓ Yardım", "chart": "📊 Grafik", "back": "🔙 Geri"},
    "hi": {"name": "🇮🇳 हिन्दी", "welcome": "स्वागत है!", "choose": "चुनें:", "crypto": "🪙 क्रिप्टो", "forex": "💱 फॉरेक्स", "prediction": "🔮 पूर्वानुमान", "alerts": "🔔 अलर्ट", "economy": "🌍 अर्थव्यवस्था", "payment": "💰 भुगतान", "referral": "🔗 रेफरल", "feedback": "📝 प्रतिक्रिया", "legal": "⚖️ कानूनी", "help": "❓ सहायता", "chart": "📊 चार्ट", "back": "🔙 वापस"},
    "zh": {"name": "🇨🇳 中文", "welcome": "欢迎!", "choose": "选择:", "crypto": "🪙 加密货币", "forex": "💱 外汇", "prediction": "🔮 预测", "alerts": "🔔 提醒", "economy": "🌍 经济", "payment": "💰 支付", "referral": "🔗 推荐", "feedback": "📝 反馈", "legal": "⚖️ 法律", "help": "❓ 帮助", "chart": "📊 图表", "back": "🔙 返回"},
    "ja": {"name": "🇯🇵 日本語", "welcome": "ようこそ!", "choose": "選択:", "crypto": "🪙 暗号資産", "forex": "💱 FX", "prediction": "🔮 予測", "alerts": "🔔 アラート", "economy": "🌍 経済", "payment": "💰 支払い", "referral": "🔗 紹介", "feedback": "📝 フィードバック", "legal": "⚖️ 法的", "help": "❓ ヘルプ", "chart": "📊 チャート", "back": "🔙 戻る"},
    "ko": {"name": "🇰🇷 한국어", "welcome": "환영합니다!", "choose": "선택:", "crypto": "🪙 암호화폐", "forex": "💱 외환", "prediction": "🔮 예측", "alerts": "🔔 알림", "economy": "🌍 경제", "payment": "💰 결제", "referral": "🔗 추천", "feedback": "📝 피드백", "legal": "⚖️ 법률", "help": "❓ 도움말", "chart": "📊 차트", "back": "🔙 뒤로"},
}

CRYPTO = {
    "btc": ("bitcoin", "BTC", "BTCUSDT"),
    "eth": ("ethereum", "ETH", "ETHUSDT"),
    "sol": ("solana", "SOL", "SOLUSDT"),
    "bnb": ("binancecoin", "BNB", "BNBUSDT"),
    "xrp": ("ripple", "XRP", "XRPUSDT"),
    "ada": ("cardano", "ADA", "ADAUSDT"),
    "doge": ("dogecoin", "DOGE", "DOGEUSDT"),
    "dot": ("polkadot", "DOT", "DOTUSDT"),
    "link": ("chainlink", "LINK", "LINKUSDT"),
    "avax": ("avalanche-2", "AVAX", "AVAXUSDT"),
    "uni": ("uniswap", "UNI", "UNIUSDT"),
    "ltc": ("litecoin", "LTC", "LTCUSDT"),
    "atom": ("cosmos", "ATOM", "ATOMUSDT"),
    "near": ("near", "NEAR", "NEARUSDT"),
    "shib": ("shiba-inu", "SHIB", "SHIBUSDT"),
    "pepe": ("pepe", "PEPE", "PEPEUSDT"),
    "bonk": ("bonk", "BONK", "BONKUSDT"),
    "floki": ("floki", "FLOKI", "FLOKIUSDT"),
    "wif": ("dogwifcoin", "WIF", "WIFUSDT"),
    "popcat": ("popcat", "POPCAT", "POPCATUSDT"),
}

FOREX = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF", "USDCAD", "NZDUSD",
    "EURJPY", "EURGBP", "GBPJPY", "EURAUD", "EURCAD", "GBPAUD", "GBPCAD",
    "GBPCHF", "GBPNZD", "AUDJPY", "AUDCAD", "AUDNZD", "NZDJPY", "CADJPY", "CHFJPY",
]

OTHER_MARKETS = ["XAUUSD", "XAGUSD", "NAS100", "US30", "SPX500", "GER30", "UK100"]

TWELVE_SYMBOLS = {
    "EURUSD": "EUR/USD", "GBPUSD": "GBP/USD", "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD", "USDCHF": "USD/CHF", "USDCAD": "USD/CAD",
    "NZDUSD": "NZD/USD", "EURJPY": "EUR/JPY", "EURGBP": "EUR/GBP",
    "GBPJPY": "GBP/JPY", "EURAUD": "EUR/AUD", "EURCAD": "EUR/CAD",
    "GBPAUD": "GBP/AUD", "GBPCAD": "GBP/CAD", "GBPCHF": "GBP/CHF",
    "GBPNZD": "GBP/NZD", "AUDJPY": "AUD/JPY", "AUDCAD": "AUD/CAD",
    "AUDNZD": "AUD/NZD", "NZDJPY": "NZD/JPY", "CADJPY": "CAD/JPY",
    "CHFJPY": "CHF/JPY", "XAUUSD": "XAU/USD", "XAGUSD": "XAG/USD",
    "NAS100": "NDX", "US30": "DJI", "SPX500": "SPX", "GER30": "DAX", "UK100": "FTSE",
}

# =========================================================
# SELF-LEARNING HELPERS
# =========================================================
def _learning_float(value):
    try:
        return float(value)
    except Exception:
        return None

def _learning_price_from_snapshot(snapshot):
    try:
        return _learning_float(snapshot.get("market_data", {}).get("price"))
    except Exception:
        return None

def _learning_extract_field(text, field):
    if not text:
        return None
    pattern = rf"(?im)^\s*{re.escape(field)}\s*:\s*(.+?)\s*$"
    m = re.search(pattern, text)
    return m.group(1).strip() if m else None

def _learning_extract_percent(text, field):
    value = _learning_extract_field(text, field)
    if not value:
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?\s*%", value)
    return float(m.group(0).replace("%", "").strip()) if m else None

def _learning_extract_price(text, field):
    value = _learning_extract_field(text, field)
    if not value:
        return None
    m = re.search(r"(?<![A-Za-z])\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", value)
    return float(m.group(0)) if m else None

def _learning_horizon_seconds(text):
    value = _learning_extract_field(text, "TRADE HORIZON")
    if not value:
        return 4 * 3600
    m = re.search(r"(\d+(?:\.\d+)?)\s*(H|HR|HRS|HOUR|HOURS|D|DAY|DAYS)", value, re.I)
    if not m:
        return 4 * 3600
    n = float(m.group(1))
    unit = m.group(2).lower()
    return int(n * (86400 if unit.startswith("d") else 3600))

def _learning_direction(signal, direction):
    signal = (signal or "").upper()
    direction = (direction or "").upper()
    if signal == "BUY":
        return "UP"
    if signal == "SELL":
        return "DOWN"
    return direction if direction in {"UP", "DOWN", "SIDEWAYS"} else "SIDEWAYS"

def load_learning_data():
    default = {"version": 1, "records": [], "stats": {}, "updated": iso(now())}
    try:
        data = load_json(SELF_LEARNING_FILE, default)
        if not isinstance(data, dict):
            return default
        data.setdefault("version", 1)
        data.setdefault("records", [])
        data.setdefault("stats", {})
        return data
    except Exception as e:
        print(f"Self-learning load error: {e}")
        return default

learning_data = load_learning_data()

def save_learning_data():
    learning_data["updated"] = iso(now())
    if len(learning_data.get("records", [])) > SELF_LEARNING_MAX_RECORDS:
        learning_data["records"] = learning_data["records"][-SELF_LEARNING_MAX_RECORDS:]
    save_json(SELF_LEARNING_FILE, learning_data)

def record_prediction_for_learning(user_id, market, result, snapshot, source="prediction"):
    if not SELF_LEARNING_ENABLED:
        return None
    price = _learning_price_from_snapshot(snapshot)
    if price is None or price <= 0:
        return None
    signal = (_learning_extract_field(result, "SIGNAL") or "HOLD").upper()
    direction = _learning_direction(signal, _learning_extract_field(result, "DIRECTION"))
    confidence = _learning_extract_percent(result, "CONFIDENCE")
    tp1 = _learning_extract_price(result, "TAKE PROFIT 1")
    sl = _learning_extract_price(result, "STOP LOSS")
    expected_move = _learning_extract_percent(result, "EXPECTED MOVE")
    horizon = _learning_horizon_seconds(result)
    record = {
        "id": f"L{int(time.time() * 1000)}",
        "created": iso(now()),
        "user_id": str(user_id),
        "market": str(market).upper(),
        "source": source,
        "entry_price": price,
        "signal": signal,
        "direction": direction,
        "confidence": confidence,
        "tp1": tp1,
        "stop_loss": sl,
        "expected_move_pct": expected_move,
        "horizon_seconds": max(1800, min(horizon, 7 * 86400)),
        "status": "pending",
        "outcome": None,
        "evaluated_at": None,
        "exit_price": None,
        "return_pct": None,
    }
    learning_data.setdefault("records", []).append(record)
    save_learning_data()
    return record["id"]

def _learning_is_correct(record, current_price):
    entry = _learning_float(record.get("entry_price"))
    if not entry or entry <= 0 or not current_price:
        return None
    change = (current_price - entry) / entry * 100
    direction = record.get("direction")
    threshold = max(0.20, min(1.00, abs(_learning_float(record.get("expected_move_pct")) or 0) * 0.25))
    if direction == "UP":
        return change > threshold
    if direction == "DOWN":
        return change < -threshold
    return abs(change) <= threshold

def evaluate_self_learning():
    if not SELF_LEARNING_ENABLED:
        return 0
    changed = 0
    now_dt = now()
    for record in learning_data.get("records", []):
        if record.get("status") != "pending":
            continue
        try:
            created_dt = ensure_utc_datetime(datetime.fromisoformat(record["created"]))
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        horizon = int(record.get("horizon_seconds", 4 * 3600))
        if (now_dt - created_dt).total_seconds() < horizon:
            continue
        market = record.get("market")
        if not market:
            continue
        try:
            current = get_binance_ticker(market)
            current_price = _learning_float(current.get("price")) if isinstance(current, dict) else None
        except Exception:
            current_price = None
        if not current_price:
            continue
        correct = _learning_is_correct(record, current_price)
        if correct is None:
            continue
        record["status"] = "evaluated"
        record["outcome"] = "WIN" if correct else "LOSS"
        record["evaluated_at"] = iso(now())
        record["exit_price"] = current_price
        entry = float(record["entry_price"])
        record["return_pct"] = round((current_price - entry) / entry * 100, 4)
        changed += 1
        key = f"{record.get('market')}|{record.get('signal')}|{record.get('direction')}"
        stats = learning_data.setdefault("stats", {}).setdefault(key, {"samples": 0, "wins": 0, "losses": 0, "avg_confidence": 0.0, "avg_return_pct": 0.0})
        stats["samples"] += 1
        if correct:
            stats["wins"] += 1
        else:
            stats["losses"] += 1
        conf = _learning_float(record.get("confidence"))
        if conf is not None:
            n = stats["samples"]
            stats["avg_confidence"] = round(((stats["avg_confidence"] * (n - 1)) + conf) / n, 2)
        n = stats["samples"]
        ret = float(record.get("return_pct") or 0)
        stats["avg_return_pct"] = round(((stats["avg_return_pct"] * (n - 1)) + ret) / n, 4)
    if changed:
        save_learning_data()
    return changed

def get_learning_context(market):
    if not SELF_LEARNING_ENABLED:
        return "SELF-LEARNING: disabled."
    evaluate_self_learning()
    market = str(market).upper()
    relevant = []
    for key, stats in learning_data.get("stats", {}).items():
        if not key.startswith(market + "|"):
            continue
        if int(stats.get("samples", 0)) < SELF_LEARNING_MIN_SAMPLES:
            continue
        relevant.append((key, stats))
    if not relevant:
        return "SELF-LEARNING: insufficient evaluated history for this market."
    lines = ["SELF-LEARNING CALIBRATION:"]
    for key, stats in relevant[:6]:
        samples = int(stats.get("samples", 0))
        wins = int(stats.get("wins", 0))
        accuracy = wins / samples * 100 if samples else 0
        lines.append(f"- {key}: samples={samples}, accuracy={accuracy:.1f}%, avg_confidence={stats.get('avg_confidence', 0):.1f}%, avg_return={stats.get('avg_return_pct', 0):.2f}%")
    lines.append("Use this only to calibrate confidence.")
    return "\n".join(lines)

def self_learning_summary():
    evaluate_self_learning()
    records = learning_data.get("records", [])
    evaluated = [r for r in records if r.get("status") == "evaluated"]
    wins = sum(1 for r in evaluated if r.get("outcome") == "WIN")
    losses = sum(1 for r in evaluated if r.get("outcome") == "LOSS")
    accuracy = (wins / len(evaluated) * 100) if evaluated else 0.0
    return f"🧠 SELF-LEARNING\nEnabled: {'YES' if SELF_LEARNING_ENABLED else 'NO'}\nTracked: {len(records)}\nEvaluated: {len(evaluated)}\nWins: {wins}\nLosses: {losses}\nAccuracy: {accuracy:.1f}%"

STRICT_LANGUAGE_POLICY = "\nSTRICT LANGUAGE POLICY: Output MUST be entirely in the user's selected language."

def language_output_guard(text, lang):
    if not text:
        return text
    english_labels = ["SIGNAL:", "CONFIDENCE:", "DIRECTION:", "CURRENT PRICE:", "TRADE HORIZON:", "ENTRY ZONE:", "STOP LOSS:", "TAKE PROFIT", "RISK/REWARD:", "EXPECTED MOVE:", "HOLD DURATION:", "PROFIT CONDITION:", "SUPPORT:", "RESISTANCE:", "TECHNICAL EVIDENCE:", "ORDER BOOK:", "NEWS & MACRO:", "INVALIDATION:", "FINAL VERDICT:"]
    if lang not in ("en", None):
        leaks = sum(1 for label in english_labels if label.lower() in text.lower())
        if leaks:
            print(f"⚠️ Language guard: {leaks} English label(s) detected for {lang}")
    return text

def get_user(user_id):
    uid = str(user_id)
    if uid not in users_data:
        users_data[uid] = {
            "status": "free",
            "created": iso(now()),
            "expiry": None,
            "payment_verified": False,
            "payment_method": None,
            "payment_amount": 0,
            "free_predictions": 5,
            "prediction_date": now().strftime("%Y-%m-%d"),
            "expiry_warnings": [],
            "language": "en",
        }
        save_json(USERS_FILE, users_data)
    return users_data[uid]

def refresh_free_prediction_counter(user_id):
    user = get_user(user_id)
    today = now().strftime("%Y-%m-%d")
    if user.get("prediction_date") != today:
        user["prediction_date"] = today
        user["free_predictions"] = 5
        save_json(USERS_FILE, users_data)

def get_user_language(user_id, context=None):
    user = get_user(user_id)
    if context:
        lang = context.user_data.get("lang")
        if lang in LANGUAGES:
            user["language"] = lang
            save_json(USERS_FILE, users_data)
            return lang
    lang = user.get("language", "en")
    if lang not in LANGUAGES:
        lang = "en"
    return lang

def get_user_status(user_id):
    if str(user_id) == str(ADMIN_ID):
        return "admin"
    user = get_user(user_id)
    if user.get("status") != "paid":
        if user.get("status") == "expired":
            return "expired"
        return "free"
    expiry_string = user.get("expiry")
    if not expiry_string:
        user["status"] = "expired"
        save_json(USERS_FILE, users_data)
        return "expired"
    try:
        expiry = ensure_utc_datetime(datetime.fromisoformat(expiry_string))
    except Exception:
        user["status"] = "expired"
        save_json(USERS_FILE, users_data)
        return "expired"
    if now() >= expiry:
        user["status"] = "expired"
        save_json(USERS_FILE, users_data)
        return "expired"
    return "paid"

def remaining_days(user_id):
    user = get_user(user_id)
    if user.get("status") != "paid":
        return 0
    try:
        expiry = ensure_utc_datetime(datetime.fromisoformat(user["expiry"]))
        seconds = (expiry - now()).total_seconds()
        if seconds <= 0:
            return 0
        return int(seconds / 86400) + 1
    except Exception:
        return 0

def activate_user(user_id, method="admin_verified", amount=SUBSCRIPTION_PRICE_ETB):
    uid = str(user_id)
    old = users_data.get(uid, {})
    users_data[uid] = {
        "status": "paid",
        "created": old.get("created", iso(now())),
        "expiry": iso(now() + timedelta(days=SUBSCRIPTION_DAYS)),
        "payment_verified": True,
        "payment_method": method,
        "payment_amount": amount,
        "payment_date": iso(now()),
        "free_predictions": 5,
        "prediction_date": now().strftime("%Y-%m-%d"),
        "expiry_warnings": [],
        "language": old.get("language", "en"),
    }
    save_json(USERS_FILE, users_data)

def can_use_prediction(user_id):
    if str(user_id) == str(ADMIN_ID):
        return True, "admin"
    status = get_user_status(user_id)
    if status == "paid":
        return True, "paid"
    if status == "expired":
        return False, "expired"
    refresh_free_prediction_counter(user_id)
    user = get_user(user_id)
    remaining = int(user.get("free_predictions", 0))
    if remaining <= 0:
        return False, "limit"
    return True, "free"

def consume_free_prediction(user_id):
    if str(user_id) == str(ADMIN_ID):
        return
    user = get_user(user_id)
    refresh_free_prediction_counter(user_id)
    if user.get("status") == "paid":
        return
    remaining = int(user.get("free_predictions", 0))
    if remaining > 0:
        user["free_predictions"] = remaining - 1
        save_json(USERS_FILE, users_data)

def compress_image(image_bytes, max_dimension=1200, quality=72):
    if not HAS_PIL:
        return image_bytes
    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        width, height = image.size
        largest = max(width, height)
        if largest > max_dimension:
            scale = max_dimension / float(largest)
            image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
        while True:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            result = buffer.getvalue()
            if len(result) <= 3_500_000:
                return result
            if quality > 45:
                quality -= 5
                continue
            return result
    except Exception as e:
        print("Compression error:", e)
        return image_bytes

def http_get(url, params=None, headers=None, timeout=5):
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return None
        return response.json()
    except Exception:
        return None

def get_binance_ticker(symbol):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = http_get(url, params={"symbol": symbol})
    if not data:
        return None
    try:
        return {
            "source": "Binance",
            "symbol": symbol,
            "price": float(data["lastPrice"]),
            "open_24h": float(data["openPrice"]),
            "high_24h": float(data["highPrice"]),
            "low_24h": float(data["lowPrice"]),
            "volume_24h": float(data["volume"]),
            "quote_volume_24h": float(data["quoteVolume"]),
            "change_24h_pct": float(data["priceChangePercent"]),
            "trades_24h": int(data["count"]),
            "timestamp": iso(now()),
        }
    except Exception:
        return None

def get_binance_orderbook(symbol):
    url = "https://api.binance.com/api/v3/depth"
    data = http_get(url, params={"symbol": symbol, "limit": 20})
    if not data:
        return None
    try:
        bids = [[float(x[0]), float(x[1])] for x in data.get("bids", [])]
        asks = [[float(x[0]), float(x[1])] for x in data.get("asks", [])]
        bid_volume = sum(x[1] for x in bids)
        ask_volume = sum(x[1] for x in asks)
        imbalance = 0.0
        if bid_volume + ask_volume > 0:
            imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        return {
            "source": "Binance",
            "bid_volume_top20": bid_volume,
            "ask_volume_top20": ask_volume,
            "orderbook_imbalance": round(imbalance, 4),
            "best_bid": bids[0][0] if bids else None,
            "best_ask": asks[0][0] if asks else None,
        }
    except Exception:
        return None

def get_binance_klines(symbol, interval="1h", limit=100):
    url = "https://api.binance.com/api/v3/klines"
    data = http_get(url, params={"symbol": symbol, "interval": interval, "limit": limit})
    if not data:
        return []
    result = []
    try:
        for row in data:
            result.append({
                "open_time": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
    except Exception:
        return []
    return result

def get_coingecko_market_data(coin_id):
    url = "https://api.coingecko.com/api/v3/coins/markets"
    data = http_get(url, params={"vs_currency": "usd", "ids": coin_id, "price_change_percentage": "1h,24h,7d,30d"})
    if not data or not isinstance(data, list) or not data:
        return None
    item = data[0]
    return {
        "source": "CoinGecko",
        "symbol": item.get("symbol", "").upper(),
        "price": item.get("current_price"),
        "market_cap": item.get("market_cap"),
        "market_cap_rank": item.get("market_cap_rank"),
        "volume_24h": item.get("total_volume"),
        "high_24h": item.get("high_24h"),
        "low_24h": item.get("low_24h"),
        "change_24h_pct": item.get("price_change_percentage_24h"),
        "change_7d_pct": item.get("price_change_percentage_7d_in_currency"),
        "change_30d_pct": item.get("price_change_percentage_30d_in_currency"),
        "timestamp": iso(now()),
    }

def calculate_sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period

def calculate_ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(closes):
    if len(closes) < 35:
        return None
    ema12 = calculate_ema(closes, 12)
    ema26 = calculate_ema(closes, 26)
    if ema12 is None or ema26 is None:
        return None
    return {"ema12": ema12, "ema26": ema26, "macd": ema12 - ema26}

def technical_snapshot(candles):
    if not candles:
        return {}
    closes = [x["close"] for x in candles]
    volumes = [x["volume"] for x in candles]
    current = closes[-1]
    sma20 = calculate_sma(closes, 20)
    sma50 = calculate_sma(closes, 50)
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)
    macd = calculate_macd(closes)
    volume20 = calculate_sma(volumes, 20)
    volume_ratio = None
    if volume20 and volume20 > 0:
        volume_ratio = volumes[-1] / volume20
    trend = "NEUTRAL"
    if ema20 and ema50:
        if current > ema20 > ema50:
            trend = "BULLISH"
        elif current < ema20 < ema50:
            trend = "BEARISH"
    return {
        "last_close": current,
        "sma20": sma20,
        "sma50": sma50,
        "ema20": ema20,
        "ema50": ema50,
        "rsi14": rsi,
        "macd": macd,
        "volume_ratio": volume_ratio,
        "trend": trend,
    }

def get_twelve_data(symbol, interval="1h"):
    if not TWELVE_DATA_API_KEY:
        return None
    twelve_symbol = TWELVE_SYMBOLS.get(symbol, symbol)
    url = "https://api.twelvedata.com/time_series"
    data = http_get(url, params={"symbol": twelve_symbol, "interval": interval, "outputsize": 100, "apikey": TWELVE_DATA_API_KEY})
    if not data:
        return None
    values = data.get("values")
    if not values:
        return None
    candles = []
    for item in reversed(values):
        try:
            candles.append({
                "datetime": item.get("datetime"),
                "open": float(item["open"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "close": float(item["close"]),
                "volume": float(item.get("volume", 0)),
            })
        except Exception:
            continue
    if not candles:
        return None
    latest = candles[-1]
    return {
        "source": "Twelve Data",
        "symbol": symbol,
        "price": latest["close"],
        "high": latest["high"],
        "low": latest["low"],
        "candles": candles,
        "technical": technical_snapshot(candles),
        "timestamp": iso(now()),
    }

def get_finnhub_news(symbol):
    if not FINNHUB_API_KEY:
        return []
    clean_symbol = symbol
    if symbol in CRYPTO:
        clean_symbol = CRYPTO[symbol][1]
    url = "https://finnhub.io/api/v1/news"
    category = "general"
    if clean_symbol in FOREX or clean_symbol in OTHER_MARKETS:
        category = "general"
    data = http_get(url, params={"category": category, "token": FINNHUB_API_KEY})
    if not data:
        return []
    result = []
    for item in data[:10]:
        result.append({
            "source": item.get("source"),
            "headline": item.get("headline"),
            "summary": item.get("summary"),
            "url": item.get("url"),
            "datetime": item.get("datetime"),
        })
    return result

FRED_SERIES = {
    "DFF": "Federal Funds Effective Rate",
    "CPIAUCSL": "US CPI",
    "UNRATE": "US Unemployment Rate",
    "DGS10": "US 10-Year Treasury Yield",
    "DTWEXBGS": "US Dollar Broad Index",
}

_FRED_CACHE = {}
_FRED_CACHE_TTL = 15 * 60
_FRED_CACHE_LOCK = threading.Lock()

def get_fred_series(series_id, limit=5):
    if not FRED_API_KEY:
        return []
    cache_key = f"{series_id}:{limit}"
    now_ts = time.time()
    with _FRED_CACHE_LOCK:
        cached = _FRED_CACHE.get(cache_key)
        if cached and now_ts - cached["time"] < _FRED_CACHE_TTL:
            return cached["data"]
    url = "https://api.stlouisfed.org/fred/series/observations"
    data = http_get(url, params={"series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json", "sort_order": "desc", "limit": limit}, timeout=2.0)
    result = []
    if data:
        for item in data.get("observations", []):
            result.append({"date": item.get("date"), "value": item.get("value")})
    if result:
        with _FRED_CACHE_LOCK:
            _FRED_CACHE[cache_key] = {"time": time.time(), "data": result}
        return result
    with _FRED_CACHE_LOCK:
        stale = _FRED_CACHE.get(cache_key)
        if stale and stale.get("data"):
            return stale["data"]
    return []

def _fred_latest_value(macro, series_name):
    item = macro.get(series_name) or {}
    observations = item.get("observations") or []
    for obs in observations:
        value = obs.get("value")
        if value not in (None, "", "."):
            try:
                return float(value)
            except Exception:
                return None
    return None

def macro_data_is_usable(snapshot):
    macro = snapshot.get("macro") or {}
    required = {
        "Federal Funds Effective Rate": "DFF",
        "US CPI": "CPIAUCSL",
        "US Unemployment Rate": "UNRATE",
        "US 10-Year Treasury Yield": "DGS10",
        "US Dollar Broad Index": "DTWEXBGS",
    }
    checks = {name: _fred_latest_value(macro, name) is not None for name in required}
    passed = sum(checks.values())
    macro["readiness"] = {
        "checks": checks,
        "passed": passed,
        "required": 5,
        "score": passed,
        "gate": "PASS" if passed == 5 else "BLOCKED",
    }
    return passed == 5

def get_macro_snapshot():
    snapshot = {}
    def fetch(item):
        series_id, name = item
        observations = get_fred_series(series_id, limit=3)
        return name, {"series_id": series_id, "observations": observations, "available": bool(observations)}
    with ThreadPoolExecutor(max_workers=min(5, len(FRED_SERIES))) as executor:
        for name, value in executor.map(fetch, FRED_SERIES.items()):
            snapshot[name] = value
    return snapshot

def normalize_market_symbol(market):
    market = market.strip().upper()
    for key, item in CRYPTO.items():
        coin_id, symbol, binance_symbol = item
        if market in (key.upper(), symbol.upper(), binance_symbol.upper()):
            return {"asset_class": "crypto", "key": key, "symbol": symbol, "binance_symbol": binance_symbol, "coin_id": coin_id}
    if market in FOREX:
        return {"asset_class": "forex", "symbol": market}
    if market in OTHER_MARKETS:
        return {"asset_class": "commodity" if market in ("XAUUSD", "XAGUSD") else "index", "symbol": market}
    return {"asset_class": "unknown", "symbol": market}

def five_of_five_gate(snapshot):
    snapshot = snapshot or {}
    dq = snapshot.get("data_quality") or {}
    pillars = {
        "live_market": bool(snapshot.get("market_data")) or bool(dq.get("live_market")),
        "technical": bool(snapshot.get("technical_data")) or bool(dq.get("technical")),
        "orderbook": bool(snapshot.get("orderbook")) or bool(dq.get("orderbook")),
        "news": bool(snapshot.get("news")) or bool(dq.get("news")),
        "macro": bool(dq.get("macro_5of5")) or macro_data_is_usable(snapshot),
    }
    score = sum(1 for ok in pillars.values() if ok)
    missing = [name for name, ok in pillars.items() if not ok]
    return {
        "score": score,
        "max_score": 5,
        "ready": score >= 1,
        "full": score == 5,
        "status": "FULL_5_OF_5" if score == 5 else f"PARTIAL_{score}_OF_5",
        "pillars": pillars,
        "missing": missing,
        "instruction": "Use all available evidence."
    }

def build_market_snapshot(market, include_news=True, include_macro=True):
    info = normalize_market_symbol(market)
    asset_class = info["asset_class"]
    snapshot = {
        "requested_market": market.upper(),
        "asset_class": asset_class,
        "timestamp": iso(now()),
        "data_sources": [],
        "market_data": {},
        "technical_data": {},
        "orderbook": {},
        "news": [],
        "macro": {},
        "data_quality": {},
    }
    if asset_class == "crypto":
        symbol = info["symbol"]
        binance_symbol = info["binance_symbol"]
        ticker = get_binance_ticker(binance_symbol)
        if ticker:
            snapshot["market_data"] = ticker
            snapshot["data_sources"].append("Binance")
            orderbook = get_binance_orderbook(binance_symbol)
            if orderbook:
                snapshot["orderbook"] = orderbook
                if "Binance" not in snapshot["data_sources"]:
                    snapshot["data_sources"].append("Binance")
            candles = get_binance_klines(binance_symbol, "1h", 100)
            snapshot["technical_data"] = technical_snapshot(candles)
        else:
            cg = get_coingecko_market_data(info["coin_id"])
            if cg:
                snapshot["market_data"] = cg
                snapshot["data_sources"].append("CoinGecko")
                candles = []
                ohlc_url = f"https://api.coingecko.com/api/v3/coins/{info['coin_id']}/ohlc"
                ohlc = http_get(ohlc_url, params={"vs_currency": "usd", "days": 7})
                if ohlc:
                    for row in ohlc:
                        try:
                            candles.append({"open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": 0})
                        except Exception:
                            continue
                    snapshot["technical_data"] = technical_snapshot(candles)
    elif asset_class in ("forex", "commodity", "index"):
        td = get_twelve_data(info["symbol"], "1h")
        if td:
            snapshot["market_data"] = {
                "source": td["source"],
                "symbol": td["symbol"],
                "price": td["price"],
                "high": td["high"],
                "low": td["low"],
                "timestamp": td["timestamp"],
            }
            snapshot["technical_data"] = td.get("technical", {})
            snapshot["data_sources"].append("Twelve Data")
    if include_news:
        news = get_finnhub_news(market)
        snapshot["news"] = news
        if news:
            snapshot["data_sources"].append("Finnhub")
    if include_macro:
        macro = get_macro_snapshot()
        snapshot["macro"] = macro
        if macro_data_is_usable(snapshot):
            snapshot["data_sources"].append("FRED (5/5 MACRO)")
        else:
            snapshot["data_sources"].append("FRED_UNAVAILABLE")
    snapshot["data_quality"] = {
        "live_market": bool(snapshot["market_data"]),
        "technical": bool(snapshot["technical_data"]),
        "orderbook": bool(snapshot["orderbook"]),
        "news": bool(snapshot["news"]),
        "macro": bool(snapshot["macro"]),
        "sources": list(dict.fromkeys(snapshot["data_sources"])),
    }
    dq = snapshot["data_quality"]
    dq["live_market_required"] = bool(snapshot.get("market_data"))
    dq["technical_required"] = bool(snapshot.get("technical_data"))
    dq["macro_5of5"] = macro_data_is_usable(snapshot) if include_macro else False
    dq["macro_10of10"] = dq["macro_5of5"]
    gate = five_of_five_gate(snapshot)
    dq["expert_gate"] = f"{gate['score']}/5"
    dq["prediction_ready"] = gate["ready"]
    dq["full_evidence"] = gate["full"]
    return snapshot

def groq_request(messages, model, max_tokens=1500, temperature=0.2):
    if not GROQ_API_KEY:
        return None, "GROQ_API_KEY is missing."
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    if model == GROQ_VISION_MODEL:
        max_tokens = min(int(max_tokens), 450)
    payload = {"model": model, "messages": messages, "temperature": temperature, "max_completion_tokens": max_tokens}
    if model == GROQ_VISION_MODEL:
        payload["reasoning_effort"] = "none"
        payload["reasoning_format"] = "hidden"
    try:
        response = requests.post(GROQ_URL, headers=headers, json=payload, timeout=120)
        if response.status_code == 429 and model == GROQ_VISION_MODEL and max_tokens > 350:
            retry_payload = dict(payload)
            retry_payload["max_completion_tokens"] = 300
            try:
                response = requests.post(GROQ_URL, headers=headers, json=retry_payload, timeout=120)
            except Exception:
                pass
        if response.status_code != 200:
            try:
                error_message = str(response.json())[:1500]
            except Exception:
                error_message = response.text[:1500]
            print("Groq error:", response.status_code, error_message)
            return None, f"Groq API error {response.status_code}: {error_message}"
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return None, "No response from Groq."
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    if text:
                        parts.append(text)
            content = "\n".join(parts)
        return str(content).strip(), None
    except requests.exceptions.Timeout:
        return None, "Groq request timed out."
    except requests.exceptions.RequestException as e:
        return None, f"Network error: {str(e)[:400]}"
    except Exception as e:
        return None, f"Groq error: {str(e)[:400]}"

def groq_text_analysis(prompt, language="English"):
    messages = [
        {"role": "system", "content": f"You are Alpha AI Market Intelligence Assistant.\n\nMANDATORY OUTPUT LANGUAGE: {language}.\nSTRICT LANGUAGE POLICY: Translate every natural-language output element into {language}; never switch languages.\nUse ONLY the supplied structured market data for live facts.\nNever invent current prices, news, statistics or events.\nPredictions are scenarios, not guarantees."},
        {"role": "user", "content": prompt}
    ]
    result, error = groq_request(messages, GROQ_TEXT_MODEL, max_tokens=1000, temperature=0.15)
    if error:
        print(error)
        return None
    return result

def create_prediction_prompt(snapshot, language, chart_description=None):
    compact_snapshot = snapshot.copy()
    compact_snapshot["news"] = snapshot.get("news", [])[:6]
    macro = snapshot.get("macro", {})
    compact_snapshot["macro"] = macro
    gate = five_of_five_gate(snapshot)
    compact_snapshot["expert_evidence_gate"] = gate
    data_json = json.dumps(compact_snapshot, ensure_ascii=False, indent=2)
    chart_section = ""
    if chart_description:
        chart_section = f"\nCHART VISUAL OBSERVATION:\n{chart_description}\n\nIMPORTANT:\nThe chart observation is visual evidence.\nCompare it with the structured market data.\nIf they conflict, explicitly mention the conflict."
    return f"""
You are Alpha AI's MARKET-AWARE PREDICTION ENGINE.

QUALITY STANDARD:
Return a professional, decision-ready trading report. Be specific, evidence-based and concise.
Never pad the answer with generic education or repeated disclaimers.
Use the latest supplied data first; distinguish observed data from forecast.
If a required value is missing, write Unavailable rather than guessing.

OUTPUT FORMAT (follow exactly and keep it compact):
1) SIGNAL: BUY / SELL / HOLD
2) DIRECTION: UP / DOWN / SIDEWAYS
3) CONFIDENCE: 0-100%
4) CURRENT PRICE: value + source/time if available
5) TRADE HORIZON: 1H / 4H / 24H / 7D
6) ENTRY ZONE: exact range when justified
7) STOP LOSS: level + brief reason
8) TAKE PROFIT 1: level + expected move % when calculable
9) TAKE PROFIT 2: level + expected move % when calculable
10) RISK/REWARD: ratio if calculable
11) HOLD PLAN: ONLY if SIGNAL=HOLD — estimated waiting/reassessment time + what price/action confirms the move; never guarantee profit
12) MARKET REGIME: Bullish / Bearish / Sideways / High Volatility / Mixed
13) TECHNICAL EVIDENCE: RSI, MACD, EMA/MA, structure, volume — only material evidence
14) ORDER BOOK: buyers vs sellers when available
15) NEWS & MACRO: only material factors
16) BULL CASE: trigger + target
17) BEAR CASE: trigger + target
18) INVALIDATION: exact level/condition
19) FINAL VERDICT: one clear actionable sentence

For HOLD, explicitly answer: "How long should I wait?" using an estimated time window based on the timeframe and market structure. Also state the expected move/profit zone if calculable, but label it as an estimate, not a promise.

You are NOT a generic chart analyzer.

Your prediction must combine:
1. LIVE MARKET DATA
2. PRICE CHANGE
3. OHLC DATA
4. TECHNICAL INDICATORS
5. ORDER BOOK when available
6. NEWS when available
7. MACROECONOMIC DATA when available
8. CHART VISUAL INFORMATION when available

================================================
ASSET
================================================
{snapshot.get("requested_market")}

ASSET CLASS:
{snapshot.get("asset_class")}

DATA SOURCES:
{snapshot.get("data_sources")}

================================================
STRUCTURED MARKET DATA
================================================
{data_json}

{chart_section}

================================================
MANDATORY OUTPUT LANGUAGE
================================================
{language}

Write the entire response in {language}.
LANGUAGE LOCK: Every heading, label, explanation, warning and sentence MUST be in {language}.
Do not mix English except ticker symbols, numbers, standard trading abbreviations (BUY/SELL/HOLD, RSI, MACD, EMA) and unavoidable source names.
If the requested language is Amharic, write naturally in አማርኛ. If Afaan Oromoo, write naturally in Afaan Oromoo.

================================================
PREDICTION TASK
================================================
Determine the most likely market scenario.
Do NOT say simply: "analysis looks bullish."
You must provide an actual prediction.

Required:
1. MARKET REGIME
2. CURRENT BIAS
3. SHORT-TERM DIRECTION
4. PREDICTION HORIZON
5. ENTRY ZONE
6. STOP LOSS ZONE
7. TAKE PROFIT 1
8. TAKE PROFIT 2
9. BULLISH SCENARIO
10. BEARISH SCENARIO
11. INVALIDATION LEVEL
12. KEY SUPPORT
13. KEY RESISTANCE
14. MOMENTUM
15. VOLUME CONDITION
16. ORDER BOOK PRESSURE
17. NEWS IMPACT
18. MACRO IMPACT
19. RISK LEVEL
20. CONFIDENCE
21. FINAL PREDICTION

================================================
IMPORTANT RULES
================================================
- Do NOT guarantee profit.
- Do NOT claim certainty.
- Do NOT invent missing data.
- Do NOT invent news.
- Do NOT invent exact prices.
- If live data is unavailable, clearly say so.
- If news is unavailable, say "News data unavailable."
- FRED macro data is a preferred evidence pillar; use it whenever available, but do not block prediction when it is temporarily unavailable.
- When available, use all five FRED series: DFF, CPIAUCSL, UNRATE, DGS10 and DTWEXBGS.
- When available, incorporate Fed policy/rates, inflation, employment, Treasury yields and USD strength.
- The five dimensions are derived only from the supplied five FRED series; they are not separate external datasets.
- Do not invent or silently omit available macro evidence.
- Do not invent macro values. If a FRED series is missing, mark it unavailable and continue with useful evidence.
- Explain relevant macro impact using only supplied FRED observations and derived context.
- BUY/SELL/HOLD is a scenario-based signal.
- Entry/SL/TP must be derived from available market evidence.
- Do not use fabricated support/resistance.
- Clearly distinguish observed facts from prediction.
- RAW STRUCTURED MARKET DATA HAS PRIORITY over any visual estimate. Never contradict supplied price, MA/EMA, RSI, MACD, volume or order-book values.
- Before writing a comparison such as price vs MA/EMA/support/resistance, calculate the relationship from the supplied numbers.
- If chart-visible data conflicts with structured live data, explicitly label the conflict instead of silently choosing one.

The final answer should be concise and suitable for Telegram.
This is educational market analysis, not financial advice.
"""

def market_prediction(market, language, chart_description=None):
    snapshot = build_market_snapshot(market, include_news=True, include_macro=True)
    dq = snapshot.get("data_quality", {})
    gate = five_of_five_gate(snapshot)
    snapshot["expert_gate"] = gate
    if gate["score"] < 1:
        return (None, "No meaningful market evidence is currently available. Please retry.")
    prompt = create_prediction_prompt(snapshot, language, chart_description)
    learning_context = get_learning_context(market)
    prompt = prompt + "\n\n================================================\n" + learning_context
    result = groq_text_analysis(prompt, language)
    return result, snapshot

def groq_chart_analysis(image_bytes, market="Unknown", language="English"):
    try:
        compressed = compress_image(image_bytes)
        encoded = base64.b64encode(compressed).decode("utf-8")
        data_url = "data:image/jpeg;base64," + encoded
        snapshot = build_market_snapshot(market, include_news=True, include_macro=True)
        dq = snapshot.get("data_quality", {})
        gate = five_of_five_gate(snapshot)
        snapshot["expert_gate"] = gate
        if gate["score"] < 1:
            return "❌ No meaningful market evidence is currently available. Please retry."
        snapshot_json = json.dumps(snapshot, ensure_ascii=False, indent=2)
        prompt = f"""
You are Alpha AI's professional financial chart vision analyst.

This is NOT chart-only analysis.

The chart must be interpreted together with the supplied live market snapshot.
Use FRED macro data whenever available; if unavailable, continue with the other available evidence and state that it is unavailable.

MARKET:
{market}

OUTPUT LANGUAGE:
{language}
STRICT LANGUAGE LOCK: Write the complete final analysis ONLY in {language}. Translate every heading, label, warning and natural-language sentence into {language}. Never fall back to English.

================================================
LIVE STRUCTURED MARKET DATA
================================================
{snapshot_json}

================================================
CHART TASK
================================================
Read the chart image carefully.
Identify only information actually visible in the image.
Then compare the chart with the supplied market data.
Analyze:
1. Asset / market
2. Timeframe
3. Visible current price
4. Trend
5. Market structure
6. Support
7. Resistance
8. Candlestick structure
9. EMA / MA
10. RSI
11. MACD
12. Volume
13. Breakout / breakdown
14. Momentum
15. Entry
16. Stop Loss
17. Take Profit 1
18. Take Profit 2
19. Invalidation
20. Risk
21. Confidence

Then combine:
CHART EVIDENCE + LIVE MARKET DATA + ORDER BOOK + NEWS + MACRO DATA
and produce a MARKET-AWARE PREDICTION.

================================================
MANDATORY OUTPUT — EXACT PROFESSIONAL TRADE CARD
================================================
{language}

IMPORTANT: Do NOT use the old numbered chart-analysis format.
Return ONLY the following compact trade card, in EXACTLY this order.
Keep each field to one short line. Do not add extra numbered sections.

SIGNAL: BUY / SELL / HOLD
CONFIDENCE: 0-100%
DIRECTION: UP / DOWN / SIDEWAYS
CURRENT PRICE: value + source/time if available
TRADE HORIZON: 1H / 4H / 24H / 7D
ENTRY ZONE: exact range when justified
STOP LOSS: level + brief reason
TAKE PROFIT 1: level + expected move %
TAKE PROFIT 2: level + expected move %
RISK/REWARD: ratio when calculable
EXPECTED MOVE: estimated % range, never guaranteed
HOLD DURATION: ONLY for HOLD — estimated waiting/reassessment window
PROFIT CONDITION: ONLY for HOLD — price/technical condition that would support the expected move
SUPPORT: key level(s)
RESISTANCE: key level(s)
TECHNICAL EVIDENCE: concise RSI/MACD/EMA/structure/volume evidence
ORDER BOOK: buyers vs sellers when available
NEWS & MACRO: only material factors
INVALIDATION: exact price/condition
FINAL VERDICT: one clear sentence

For HOLD, HOLD DURATION must answer "How long should I wait?" and PROFIT CONDITION must explain what must happen before the expected move is considered valid.
For BUY/SELL, HOLD DURATION and PROFIT CONDITION must be omitted.
Confidence must be a calibrated estimate from the combined evidence, not a guarantee.
Never invent unavailable values. Use "Unavailable" or "Not visible" when necessary.
Never guarantee profit.

FINAL FORMAT GUARD:
The first line of the answer MUST begin with "SIGNAL:".
The second line MUST begin with "CONFIDENCE:".
Do not prepend a title, introduction, explanation, emoji banner, or numbered heading.
Do not output the old chart-analysis list.

================================================
RULES
================================================
Never invent visible chart values.
Never invent market data.
Never invent news.
Never guarantee profit.
If an indicator is not visible, say "Not visible".
If live market data is unavailable, say "Live market data unavailable".
If news is unavailable, say "News data unavailable".
Macro data should be used whenever available. If unavailable, continue with available evidence and never fabricate values.
The prediction must be based on evidence.
This is educational market analysis, not financial advice.
"""
        messages = [
            {"role": "system", "content": f"You are Alpha AI's multimodal financial market prediction engine.\n\nRequired language: {language}.\n\nUse both image evidence and structured data."},
            {"role": "user", "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": data_url}}]}
        ]
        result, error = groq_request(messages, GROQ_VISION_MODEL, max_tokens=450, temperature=0.1)
        if error:
            return "❌ Chart AI failed.\n\nThe vision service is temporarily unavailable or rate-limited. Please try again shortly."
        if not result:
            return "❌ Groq returned an empty analysis."
        return result
    except Exception as e:
        print("Chart analysis exception:", e)
        return f"❌ Chart analysis failed.\n\n{str(e)[:500]}"

def format_price(price):
    if price is None:
        return "N/A"
    try:
        price = float(price)
        if price >= 1000:
            return f"{price:,.2f}"
        if price >= 1:
            return f"{price:,.4f}"
        if price >= 0.01:
            return f"{price:.6f}"
        return f"{price:.10f}"
    except Exception:
        return "N/A"

def market_snapshot_text(snapshot):
    market = snapshot.get("requested_market", "Unknown")
    data = snapshot.get("market_data", {})
    tech = snapshot.get("technical_data", {})
    quality = snapshot.get("data_quality", {})
    lines = [
        f"📊 {market}",
        "",
        "🌐 DATA SOURCES:",
        ", ".join(snapshot.get("data_sources", [])) or "None",
        "",
        "💰 PRICE:",
        format_price(data.get("price")),
    ]
    if data.get("change_24h_pct") is not None:
        lines.append(f"📈 24H: {data['change_24h_pct']:.2f}%")
    if tech.get("trend"):
        lines.append(f"📊 TREND: {tech['trend']}")
    if tech.get("rsi14") is not None:
        lines.append(f"RSI: {tech['rsi14']:.2f}")
    if tech.get("ema20") is not None:
        lines.append(f"EMA20: {format_price(tech['ema20'])}")
    if tech.get("ema50") is not None:
        lines.append(f"EMA50: {format_price(tech['ema50'])}")
    lines.extend([
        "",
        "DATA QUALITY:",
        f"Live: {'YES' if quality.get('live_market') else 'NO'}",
        f"Technical: {'YES' if quality.get('technical') else 'NO'}",
        f"News: {'YES' if quality.get('news') else 'NO'}",
        f"Macro: {'YES' if quality.get('macro') else 'NO'}",
    ])
    return "\n".join(lines)

def language_menu():
    languages = [
        ("🇬🇧 English", "en"), ("🇪🇹 አማርኛ", "am"), ("🇪🇹 Oromiffa", "om"),
        ("🇦🇪 العربية", "ar"), ("🇫🇷 Français", "fr"), ("🇪🇸 Español", "es"),
        ("🇵🇹 Português", "pt"), ("🇷🇺 Русский", "ru"), ("🇹🇷 Türkçe", "tr"),
        ("🇮🇳 हिन्दी", "hi"), ("🇨🇳 中文", "zh"), ("🇯🇵 日本語", "ja"), ("🇰🇷 한국어", "ko")
    ]
    rows = []
    row = []
    for label, code in languages:
        row.append(InlineKeyboardButton(label, callback_data=f"lang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def main_menu(lang):
    t = LANGUAGES.get(lang, LANGUAGES["en"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t["crypto"], callback_data=f"menu:crypto:{lang}"), InlineKeyboardButton(t["forex"], callback_data=f"menu:forex:{lang}")],
        [InlineKeyboardButton(t["prediction"], callback_data=f"menu:prediction:{lang}"), InlineKeyboardButton(t["alerts"], callback_data=f"menu:alerts:{lang}")],
        [InlineKeyboardButton(t["economy"], callback_data=f"menu:economy:{lang}"), InlineKeyboardButton(t["payment"], callback_data=f"menu:payment:{lang}")],
        [InlineKeyboardButton(t["referral"], callback_data=f"menu:referral:{lang}"), InlineKeyboardButton(t["feedback"], callback_data=f"menu:feedback:{lang}")],
        [InlineKeyboardButton(t["legal"], callback_data=f"menu:legal:{lang}"), InlineKeyboardButton(t["help"], callback_data=f"menu:help:{lang}")]
    ])

def crypto_menu(lang):
    rows = []
    row = []
    for key, item in CRYPTO.items():
        symbol = item[1]
        row.append(InlineKeyboardButton(symbol, callback_data=f"coin:{key}:{lang}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(LANGUAGES[lang]["back"], callback_data=f"main:{lang}")])
    return InlineKeyboardMarkup(rows)

def forex_menu(lang):
    rows = []
    row = []
    for symbol in (FOREX + OTHER_MARKETS):
        row.append(InlineKeyboardButton(symbol, callback_data=f"market:{symbol}:{lang}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(LANGUAGES[lang]["back"], callback_data=f"main:{lang}")])
    return InlineKeyboardMarkup(rows)

def prediction_menu(lang):
    t = LANGUAGES[lang]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Send Chart", callback_data=f"chartmode:{lang}")],
        [InlineKeyboardButton("🔮 Predict by Symbol", callback_data=f"predictsymbol:{lang}")],
        [InlineKeyboardButton(t["crypto"], callback_data=f"menu:crypto:{lang}"), InlineKeyboardButton(t["forex"], callback_data=f"menu:forex:{lang}")],
        [InlineKeyboardButton(t["back"], callback_data=f"main:{lang}")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user.get("language"):
        context.user_data["lang"] = user["language"]
    context.user_data["photo_mode"] = "chart"
    if context.args:
        argument = context.args[0]
        if argument.startswith("ref_"):
            inviter = argument.replace("ref_", "")
            if inviter != str(user_id):
                add_referral(inviter, user_id)
    await update.message.reply_text("🌍 Choose your language\nቋንቋ ምረጥ\nAfaan filadhu", reply_markup=language_menu())

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split(":")
    action = data[0]
    if action == "lang":
        lang = data[1]
        user_id = query.from_user.id
        context.user_data["lang"] = lang
        context.user_data["photo_mode"] = "chart"
        user = get_user(user_id)
        user["language"] = lang
        save_json(USERS_FILE, users_data)
        t = LANGUAGES[lang]
        await query.edit_message_text(f"{t['welcome']}\n\n{t['choose']}", reply_markup=main_menu(lang))
        return
    if action == "main":
        lang = data[1]
        context.user_data["lang"] = lang
        await query.edit_message_text(LANGUAGES[lang]["choose"], reply_markup=main_menu(lang))
        return
    if action == "menu":
        section = data[1]; lang = data[2]
        if section == "crypto":
            await query.edit_message_text("🪙 Select cryptocurrency:", reply_markup=crypto_menu(lang)); return
        if section == "forex":
            await query.edit_message_text("💱 Select market:", reply_markup=forex_menu(lang)); return
        if section == "prediction":
            await query.edit_message_text("🔮 MARKET-AWARE PREDICTION\n\nAlpha AI combines:\n💰 Live data\n📊 Technicals\n📰 News\n🌍 Macro\n📸 Chart\n\nSend chart or /predict BTC", reply_markup=prediction_menu(lang)); return
        if section == "alerts": await show_alerts(query, query.from_user.id, lang); return
        if section == "payment": await show_payment(query, query.from_user.id, lang); return
        if section == "economy": await economy_menu(query, lang); return
        if section == "referral": await referral_menu(query, query.from_user.id, lang); return
        if section == "feedback":
            await query.edit_message_text("📝 FEEDBACK\n/complaint message\n/suggest message\n/bug message\n/myfeedback", reply_markup=main_menu(lang)); return
        if section == "legal":
            await query.edit_message_text("⚖️ LEGAL\nAlpha AI provides analysis only.\nNot financial advice.", reply_markup=main_menu(lang)); return
        if section == "help":
            await query.edit_message_text("❓ HELP\n/predict BTC\n/alert BTC 120000 above\n/alerts\n/payment\n/impact Bitcoin", reply_markup=main_menu(lang)); return
    if action == "chartmode":
        lang = data[1]
        context.user_data["lang"] = lang
        context.user_data["photo_mode"] = "chart"
        await query.edit_message_text("📊 CHART MODE ACTIVE\nSend chart screenshot.", reply_markup=main_menu(lang)); return
    if action == "paymode":
        lang = data[1]
        context.user_data["photo_mode"] = "payment"
        await query.edit_message_text("💰 PAYMENT MODE\nSend screenshot with phone number.", reply_markup=main_menu(lang)); return
    if action == "coin":
        key, lang = data[1], data[2]
        symbol = CRYPTO[key][1]
        context.user_data["selected_market"] = symbol
        context.user_data["photo_mode"] = "chart"
        snapshot = build_market_snapshot(symbol, False, False)
        text = market_snapshot_text(snapshot) + "\n\n📸 Send chart for prediction."
        await query.edit_message_text(text, reply_markup=main_menu(lang)); return
    if action == "market":
        symbol, lang = data[1], data[2]
        context.user_data["selected_market"] = symbol
        context.user_data["photo_mode"] = "chart"
        snapshot = build_market_snapshot(symbol, False, False)
        text = market_snapshot_text(snapshot) + "\n\n📸 Send chart for prediction."
        await query.edit_message_text(text, reply_markup=main_menu(lang)); return
    if action == "predictsymbol":
        lang = data[1]
        await query.edit_message_text("🔮 Use /predict BTC", reply_markup=main_menu(lang)); return

def payment_text(user_id):
    status = get_user_status(user_id)
    if status in ("paid", "admin"):
        status_text = "👑 ADMIN" if status == "admin" else f"🟢 PREMIUM\n📅 {remaining_days(user_id)} days left"
    elif status == "expired":
        status_text = "🔴 EXPIRED"
    else:
        user = get_user(user_id)
        refresh_free_prediction_counter(user_id)
        status_text = f"🟢 FREE\n🔮 {user.get('free_predictions', 0)} left"
    return f"💰 PAYMENT\n{status_text}\n\n📱 Telebirr: 0956967050\n📱 M-Pesa: 0725530098\n💵 Price: {SUBSCRIPTION_PRICE_ETB} ETB / {SUBSCRIPTION_DAYS} days"

async def show_payment(query, user_id, lang):
    await query.edit_message_text(payment_text(user_id), reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Send Screenshot", callback_data=f"paymode:{lang}")],
        [InlineKeyboardButton(LANGUAGES[lang]["back"], callback_data=f"main:{lang}")]
    ]))

async def payment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["photo_mode"] = "payment"
    await update.message.reply_text(payment_text(update.effective_user.id))

async def payment_screenshot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    caption = (update.message.caption or "").strip()
    phone_matches = re.findall(r"\b(?:09|07)\d{8}\b", caption)
    valid_phone = None
    for phone in phone_matches:
        if phone in ALLOWED_PHONES:
            valid_phone = phone
            break
    if not valid_phone:
        await update.message.reply_text("⚠️ Phone number not found in caption.")
        return
    payment_id = "PAY" + str(int(time.time() * 1000))
    payments_data[payment_id] = {"user_id": str(user_id), "phone": valid_phone, "amount_expected": SUBSCRIPTION_PRICE_ETB, "status": "pending", "date": iso(now())}
    save_json(PAYMENTS_FILE, payments_data)
    if ADMIN_ID:
        try:
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id, caption=f"💳 NEW PAYMENT\n🆔 {payment_id}\n👤 {user_id}\n📱 {valid_phone}\n/approve {payment_id}\n/reject {payment_id}")
        except Exception as e:
            print("Admin error:", e)
    await update.message.reply_text(f"📸 Received. Waiting for admin. ID: {payment_id}")

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: await update.message.reply_text("Usage: /approve PAYMENT_ID"); return
    payment_id = context.args[0]
    payment = payments_data.get(payment_id)
    if not payment: await update.message.reply_text("❌ Not found."); return
    if payment.get("status") == "approved": await update.message.reply_text("⚠️ Already approved."); return
    user_id = payment["user_id"]
    activate_user(user_id, "admin_verified", SUBSCRIPTION_PRICE_ETB)
    payment["status"] = "approved"
    payment["approved_date"] = iso(now())
    save_json(PAYMENTS_FILE, payments_data)
    await update.message.reply_text(f"✅ Approved.\n👤 {user_id}")
    try:
        await context.bot.send_message(chat_id=int(user_id), text="✅ PAYMENT CONFIRMED!\nPremium active.")
    except Exception: pass

async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if not context.args: await update.message.reply_text("Usage: /reject PAYMENT_ID"); return
    payment_id = context.args[0]
    payment = payments_data.get(payment_id)
    if not payment: await update.message.reply_text("❌ Not found."); return
    payment["status"] = "rejected"
    payment["rejected_date"] = iso(now())
    save_json(PAYMENTS_FILE, payments_data)
    await update.message.reply_text("❌ Rejected.")

async def chart_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    allowed, reason = can_use_prediction(user_id)
    if not allowed:
        if reason == "expired": await update.message.reply_text("⛔ Expired. Use /payment.")
        else: await update.message.reply_text("⚠️ Limit reached. /payment")
        return
    lang = get_user_language(user_id, context)
    language = LANGUAGE_OUTPUT_INSTRUCTIONS.get(lang, "English")
    selected_market = context.user_data.get("selected_market", "Unknown")
    processing = await update.message.reply_text("🤖 Analyzing...")
    try:
        photo = update.message.photo[-1]
        telegram_file = await photo.get_file()
        raw_bytes = await telegram_file.download_as_bytearray()
        image_bytes = compress_image(bytes(raw_bytes))
        analysis = await asyncio.to_thread(groq_chart_analysis, image_bytes, selected_market, language)
        if not analysis or analysis.startswith("❌"):
            await processing.edit_text(analysis or "❌ Failed.")
            return
        consume_free_prediction(user_id)
        status = get_user_status(user_id)
        if status in ("paid", "admin"):
            footer = "\n\n💎 PREMIUM\n⚠️ Not financial advice."
        else:
            user = get_user(user_id)
            footer = f"\n\n🔮 Free remaining: {user.get('free_predictions', 0)}\n⚠️ Not financial advice."
        final_text = "📊 PREDICTION\n\n" + analysis + footer
        if len(final_text) > 4000: final_text = final_text[:3900] + "\n\n[Truncated]"
        await processing.edit_text(final_text)
    except Exception as e:
        print(e)
        await processing.edit_text(f"❌ Error: {str(e)[:500]}")

async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("photo_mode", "chart")
    if mode == "payment":
        await payment_screenshot_handler(update, context)
        context.user_data["photo_mode"] = "chart"
        return
    await chart_photo_handler(update, context)

# =========================================================
# EXTRACT CONFIDENCE FROM AI RESPONSE
# =========================================================
def extract_confidence_from_ai(text):
    """Extract CONFIDENCE percentage from AI response."""
    if not text:
        return 0.0
    match = re.search(r"CONFIDENCE:\s*(\d+(?:\.\d+)?)\s*%", text, re.I)
    if match:
        try:
            return float(match.group(1))
        except:
            return 0.0
    return 0.0

def parse_signal_from_ai(text):
    if not text:
        return "HOLD", None, None, None, None
    signal = "HOLD"
    entry = None
    sl = None
    tp1 = None
    tp2 = None
    match = re.search(r"SIGNAL:\s*(BUY|SELL|HOLD)", text, re.I)
    if match:
        signal = match.group(1).upper()
    match = re.search(r"ENTRY ZONE:\s*\$?([\d,]+\.?\d*)\s*[-–]\s*\$?([\d,]+\.?\d*)", text)
    if match:
        try:
            entry = (float(match.group(1).replace(',', '')), float(match.group(2).replace(',', '')))
        except: pass
    match = re.search(r"STOP LOSS:\s*\$?([\d,]+\.?\d*)", text)
    if match:
        try:
            sl = float(match.group(1).replace(',', ''))
        except: pass
    match = re.search(r"TAKE PROFIT 1:\s*\$?([\d,]+\.?\d*)", text)
    if match:
        try:
            tp1 = float(match.group(1).replace(',', ''))
        except: pass
    match = re.search(r"TAKE PROFIT 2:\s*\$?([\d,]+\.?\d*)", text)
    if match:
        try:
            tp2 = float(match.group(1).replace(',', ''))
        except: pass
    return signal, entry, sl, tp1, tp2

# =========================================================
# BINANCE EXCHANGE INIT
# =========================================================
exchange = None
if CCXT_AVAILABLE and BINANCE_API_KEY and BINANCE_SECRET_KEY:
    try:
        exchange = ccxt.binance({
            'apiKey': BINANCE_API_KEY,
            'secret': BINANCE_SECRET_KEY,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
        if BINANCE_TESTNET:
            exchange.set_sandbox_mode(True)
            print("🔁 Binance Testnet mode ENABLED.")
        else:
            print("🔒 Binance LIVE mode (REAL MONEY).")
    except Exception as e:
        print(f"⚠️ Binance init failed: {e}")
        exchange = None
else:
    if not CCXT_AVAILABLE:
        print("⚠️ CCXT not installed.")
    elif not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        print("⚠️ Binance API keys missing.")

# =========================================================
# CLOSE POSITION
# =========================================================
async def close_position(context, reason, manual_close=False):
    global active_trade
    if not active_trade.get("symbol"):
        if manual_close:
            await context.bot.send_message(chat_id=ADMIN_ID, text="❌ No active trade to close.")
        return

    symbol = active_trade["symbol"]
    side = "sell" if active_trade["side"] == "buy" else "buy"
    quantity = active_trade["quantity"]
    old_entry = active_trade["entry_price"]

    try:
        if not exchange:
            await context.bot.send_message(chat_id=ADMIN_ID, text="❌ Exchange not initialized.")
            return

        order = exchange.create_market_order(
            symbol=symbol,
            side=side,
            amount=quantity
        )

        # Get current price
        symbol_binance = symbol.replace("/", "")
        ticker = get_binance_ticker(symbol_binance)
        current_price = ticker["price"] if ticker else old_entry

        # Calculate PnL
        pnl_amount = 0.0
        if active_trade["side"] == "buy":
            pnl_amount = (current_price - old_entry) * quantity
        else:
            pnl_amount = (old_entry - current_price) * quantity

        # Update daily stats
        update_daily_stats(pnl_amount)

        # Check daily loss limit
        hit, total_pnl, max_loss = check_daily_loss_limit()
        if hit:
            set_pause_state(True, "daily_loss")
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🚨 DAILY LOSS LIMIT HIT!\n"
                     f"📉 Total loss: ${total_pnl:.2f}\n"
                     f"⛔ Max allowed: ${max_loss:.2f}\n\n"
                     f"⏸️ Bot paused. Will auto-resume at midnight UTC."
            )

        # Clear active trade
        active_trade = {
            "symbol": None,
            "entry_price": 0.0,
            "sl": 0.0,
            "tp1": 0.0,
            "tp2": 0.0,
            "quantity": 0.0,
            "side": None,
            "open_time": None
        }
        save_active_trade(active_trade)

        # Send notification
        pnl_pct = (pnl_amount / (old_entry * quantity)) * 100 if old_entry * quantity > 0 else 0
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🔒 Position Closed{' (MANUAL)' if manual_close else ''}!\n"
                 f"━━━━━━━━━━━━━━━━\n"
                 f"📊 Symbol: {symbol}\n"
                 f"💰 Entry: ${old_entry:.4f}\n"
                 f"💰 Exit: ${current_price:.4f}\n"
                 f"📈 PnL: ${pnl_amount:.2f} ({pnl_pct:.2f}%)\n"
                 f"🆔 Order ID: {order.get('id')}\n"
                 f"📋 Reason: {reason}\n"
                 f"━━━━━━━━━━━━━━━━"
        )

    except Exception as e:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"❌ Failed to close position: {str(e)[:500]}"
        )

# =========================================================
# MANUAL CLOSE TRADE COMMAND
# =========================================================
async def close_trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ ፈቃድ የለዎትም።")
        return

    if not active_trade.get("symbol"):
        await update.message.reply_text("❌ ምንም ክፍት ንግድ (Active Trade) የለም።")
        return

    await update.message.reply_text("🔒 ንግዱን በእጅ እየዘጋሁ ነው...")
    await close_position(context, "Manual close via /closetrade", manual_close=True)

# =========================================================
# PAUSE / RESUME COMMANDS
# =========================================================
async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ ፈቃድ የለዎትም።")
        return
    paused, reason = get_pause_state()
    if paused:
        await update.message.reply_text(f"⏸️ ቦቱ ቀድሞውኑ ቆሟል (ምክንያት: {reason})።")
        return
    set_pause_state(True, "manual")
    await update.message.reply_text(
        "⏸️ ቦቱ በእጅ ተቆሟል!\n\n"
        "✅ ነባር ንግዶች መከታተል ይቀጥላሉ (SL/TP)።\n"
        "❌ አዲስ ንግድ አይከፈትም።\n\n"
        "▶️ /resume በመጠቀም እንደገና ያስነሱት።\n"
        "⚠️ በእጅ ያቆሙት በራሱ አይነሳም!"
    )

async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ ፈቃድ የለዎትም።")
        return
    paused, reason = get_pause_state()
    if not paused:
        await update.message.reply_text("▶️ ቦቱ ቀድሞውኑ እየሰራ ነው።")
        return
    set_pause_state(False, None)
    await update.message.reply_text(
        "▶️ ቦቱ እንደገና ተነስቷል!\n\n"
        "🔄 አሁን አዲስ ንግድ ይከፍታል።"
    )

# =========================================================
# AUTO-TRADE JOB
# =========================================================
async def auto_trade_job(context):
    global active_trade

    # 0. Reset daily stats if new day
    get_daily_stats()

    # 1. Check pause state
    paused, reason = get_pause_state()

    # 2. Get current price first
    symbol_binance = AUTO_TRADE_SYMBOL.replace("/", "")
    ticker = get_binance_ticker(symbol_binance)
    if not ticker:
        return
    current_price = ticker["price"]

    # 3. Check active trade monitoring (always runs even if paused)
    if active_trade.get("symbol"):
        # Check Stop Loss
        if active_trade["sl"] and active_trade["sl"] > 0:
            if (active_trade["side"] == "buy" and current_price <= active_trade["sl"]) or \
               (active_trade["side"] == "sell" and current_price >= active_trade["sl"]):
                await close_position(context, "STOP LOSS HIT")
                return

        # Check Take Profit 1
        if active_trade["tp1"] and active_trade["tp1"] > 0:
            if (active_trade["side"] == "buy" and current_price >= active_trade["tp1"]) or \
               (active_trade["side"] == "sell" and current_price <= active_trade["tp1"]):
                await close_position(context, f"TAKE PROFIT 1 HIT @ ${active_trade['tp1']}")
                return

        # If paused, don't open new trades
        if paused:
            return

        return  # Still has active trade, don't open new one

    # If paused and no active trade, do nothing
    if paused:
        return

    # 4. No active trade - get new signal
    market_symbol = AUTO_TRADE_SYMBOL.split("/")[0]
    result, snapshot = market_prediction(market_symbol, "English")
    if not result:
        return

    # 5. Extract Confidence
    confidence = extract_confidence_from_ai(result)
    if confidence < MIN_CONFIDENCE_TO_TRADE:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⏸️ SKIPPED: Confidence {confidence:.1f}% < {MIN_CONFIDENCE_TO_TRADE}%"
        )
        return

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"✅ HIGH CONFIDENCE ({confidence:.1f}%) - Executing..."
    )

    signal, entry, sl, tp1, tp2 = parse_signal_from_ai(result)
    if signal == "HOLD":
        return

    # 6. Risk Management
    try:
        balance = exchange.fetch_balance()
        free_usdt = balance['USDT']['free']
    except Exception:
        free_usdt = 7.0

    risk_pct = AUTO_TRADE_RISK_PCT / 100.0
    risk_amount = free_usdt * risk_pct

    entry_price = current_price
    if not sl:
        sl = entry_price * 0.97 if signal == "BUY" else entry_price * 1.03

    risk_per_unit = abs(entry_price - sl)
    if risk_per_unit == 0:
        quantity = MIN_TRADE_QUANTITY
    else:
        quantity = risk_amount / risk_per_unit

    # Round and ensure minimum quantity
    quantity = round(quantity, 6)
    if quantity < MIN_TRADE_QUANTITY:
        quantity = MIN_TRADE_QUANTITY

    # 7. Execute Order
    try:
        order = exchange.create_market_order(
            symbol=AUTO_TRADE_SYMBOL,
            side=signal.lower(),
            amount=quantity
        )

        side = signal.lower()
        active_trade = {
            "symbol": AUTO_TRADE_SYMBOL,
            "entry_price": entry_price,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "quantity": quantity,
            "side": side,
            "open_time": iso(now())
        }
        save_active_trade(active_trade)

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"✅ {signal} ORDER EXECUTED!\n"
                 f"Symbol: {AUTO_TRADE_SYMBOL}\n"
                 f"Price: ${entry_price:.4f}\n"
                 f"Qty: {quantity}\n"
                 f"SL: ${sl:.4f}\n"
                 f"TP1: ${tp1 if tp1 else 'N/A'}\n"
                 f"Confidence: {confidence:.1f}%"
        )
    except Exception as e:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"❌ Order execution failed: {e}"
        )

# =========================================================
# MIDNIGHT RESET JOB
# =========================================================
async def midnight_reset_job(context):
    """Called at midnight UTC. Resets daily stats and auto-resumes if paused due to daily loss."""
    # 1. Reset daily stats
    get_daily_stats()  # This resets automatically if date changed

    # 2. Check if bot is paused due to daily loss
    paused, reason = get_pause_state()
    if paused and reason == "daily_loss":
        # Auto-resume
        set_pause_state(False, None)
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text="🌅 አዲስ ቀን ጀምሯል!\n\n"
                 "✅ የቀን ኪሳራ ገደብ ተሻሽሏል።\n"
                 "▶️ ቦቱ በራሱ ተነስቷል! አሁን አዲስ ንግድ ይከፍታል።\n\n"
                 "⚠️ በእጅ ማቆም ከፈለጉ /pause ይጠቀሙ።"
        )
    elif paused and reason == "manual":
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text="🌅 አዲስ ቀን ጀምሯል!\n\n"
                 "⏸️ ቦቱ በእጅ ስለቆሙ አልተነሳም።\n"
                 "▶️ ለማስነሳት /resume ይላኩ።"
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text="🌅 አዲስ ቀን ጀምሯል!\n\n"
                 "📊 የቀን ኪሳራ መረጃ ተሻሽሏል።\n"
                 "📈 ቦቱ እንደበፊቱ እየሰራ ነው።"
        )

# =========================================================
# PREDICT COMMAND
# =========================================================
async def predict_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    allowed, reason = can_use_prediction(user_id)
    if not allowed:
        if reason == "expired": await update.message.reply_text("⛔ Expired.")
        else: await update.message.reply_text("⚠️ Limit reached.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /predict BTC")
        return
    market = context.args[0].upper()
    normalized = normalize_market_symbol(market)
    if normalized.get("asset_class") == "unknown":
        await update.message.reply_text("❌ Unsupported.")
        return
    market = normalized["symbol"]
    lang = get_user_language(user_id, context)
    language = LANGUAGE_OUTPUT_INSTRUCTIONS.get(lang, "English")
    processing = await update.message.reply_text("🤖 Generating...")
    try:
        result, snapshot = await asyncio.to_thread(market_prediction, market, language)
        if not result:
            await processing.edit_text("❌ Failed.")
            return
        consume_free_prediction(user_id)
        quality = snapshot.get("data_quality", {})
        sources = snapshot.get("data_sources", [])
        footer = localized_prediction_footer(lang, quality, sources)
        titles = {"am": "🔮 ALPHA AI የገበያ ትንበያ", "en": "🔮 ALPHA AI MARKET PREDICTION", "om": "🔮 ALPHA AI TILMAAMA"}
        title = titles.get(lang, "🔮 PREDICTION")
        final_text = title + "\n\n" + result + footer
        if len(final_text) > 4000: final_text = final_text[:3900] + "\n\n[Truncated]"
        await processing.edit_text(final_text)
    except Exception as e:
        await processing.edit_text(f"❌ Error: {str(e)[:600]}")

async def learning_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    await update.message.reply_text(self_learning_summary())

# =========================================================
# ALERT COMMANDS
# =========================================================
def add_alert(user_id, symbol, target, direction):
    alert_id = "A" + str(int(time.time() * 1000))
    alerts_data[alert_id] = {"user_id": str(user_id), "symbol": symbol.upper(), "target": float(target), "direction": direction.lower(), "active": True, "created": iso(now())}
    save_json(ALERTS_FILE, alerts_data)
    return alert_id

def remove_alert(alert_id, user_id):
    if alert_id not in alerts_data: return False
    alert = alerts_data[alert_id]
    if alert["user_id"] != str(user_id): return False
    alert["active"] = False
    save_json(ALERTS_FILE, alerts_data)
    return True

async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text("Usage: /alert BTC 120000 above")
        return
    symbol = context.args[0].upper()
    normalized = normalize_market_symbol(symbol)
    if normalized.get("asset_class") != "crypto":
        await update.message.reply_text("❌ Crypto only.")
        return
    symbol = normalized["symbol"]
    try:
        target = float(context.args[1])
    except:
        await update.message.reply_text("❌ Invalid price.")
        return
    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("❌ Use above/below.")
        return
    alert_id = add_alert(update.effective_user.id, symbol, target, direction)
    await update.message.reply_text(f"✅ Alert set.\nID: {alert_id}")

async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    mine = [(aid, data) for aid, data in alerts_data.items() if data.get("user_id") == user_id and data.get("active", True)]
    if not mine:
        await update.message.reply_text("No active alerts.")
        return
    text = "🔔 YOUR ALERTS\n\n"
    for aid, data in mine[-20:]:
        text += f"ID: {aid}\nSymbol: {data['symbol']} Target: ${format_price(data['target'])} {data['direction']}\n\n"
    await update.message.reply_text(text + "\n/cancelalert ID")

async def cancel_alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancelalert ID")
        return
    if remove_alert(context.args[0], update.effective_user.id):
        await update.message.reply_text("✅ Cancelled.")
    else:
        await update.message.reply_text("❌ Not found.")

async def check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    active_alerts = [(aid, data) for aid, data in alerts_data.items() if data.get("active", True)]
    if not active_alerts: return
    symbol_to_id = {item[1]: item[2] for item in CRYPTO.values()}
    for _, alert in active_alerts:
        symbol = alert["symbol"]
        if symbol not in symbol_to_id: continue
        ticker = await asyncio.to_thread(get_binance_ticker, symbol_to_id[symbol])
        if not ticker: continue
        price = ticker["price"]
        target = float(alert["target"])
        direction = alert["direction"]
        if (direction == "above" and price >= target) or (direction == "below" and price <= target):
            alert["active"] = False
            try:
                await context.bot.send_message(chat_id=int(alert["user_id"]), text=f"🔔 Alert triggered!\n{alert['symbol']} at ${format_price(price)}")
            except: pass
    save_json(ALERTS_FILE, alerts_data)

# =========================================================
# ECONOMY COMMANDS
# =========================================================
async def economy_menu(query, lang):
    await query.edit_message_text("🌍 GLOBAL ECONOMY\nUse /impact Bitcoin", reply_markup=main_menu(lang))

async def economy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌍 Use /impact Bitcoin")

async def impact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = " ".join(context.args) if context.args else "Bitcoin"
    user_id = update.effective_user.id
    lang = get_user_language(user_id, context)
    language = LANGUAGE_OUTPUT_INSTRUCTIONS.get(lang, "English")
    processing = await update.message.reply_text("🌍 Analyzing impact...")
    snapshot = await asyncio.to_thread(build_market_snapshot, asset, True, True)
    prompt = f"Analyze impact for {asset}. Use data: {json.dumps(snapshot, ensure_ascii=False, indent=2)}. Overall impact BULLISH/BEARISH/MIXED. Language: {language}."
    result = await asyncio.to_thread(groq_text_analysis, prompt, language)
    text = "🌍 IMPACT\n\n" + (result or "Failed.") + "\n\n⚠️ Not advice."
    await processing.edit_text(text[:4000])

# =========================================================
# REFERRAL FUNCTIONS
# =========================================================
def add_referral(inviter_id, new_user_id):
    inviter_id, new_user_id = str(inviter_id), str(new_user_id)
    if inviter_id == new_user_id: return False
    if inviter_id not in referral_data: referral_data[inviter_id] = {"invited": [], "total": 0}
    if new_user_id in referral_data[inviter_id]["invited"]: return False
    referral_data[inviter_id]["invited"].append(new_user_id)
    referral_data[inviter_id]["total"] += 1
    inviter_user = get_user(int(inviter_id))
    inviter_user["free_predictions"] = inviter_user.get("free_predictions", 5) + 3
    save_json(USERS_FILE, users_data)
    save_json(REFERRALS_FILE, referral_data)
    return True

async def referral_menu(query, user_id, lang):
    uid = str(user_id)
    data = referral_data.get(uid, {"invited": [], "total": 0})
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{user_id}"
    await query.edit_message_text(f"🔗 REFERRALS\nInvited: {data['total']}\nLink: {link}", reply_markup=main_menu(lang))

# =========================================================
# FEEDBACK FUNCTIONS
# =========================================================
def save_feedback(user_id, feedback_type, message):
    fid = "FB" + str(int(time.time() * 1000))
    feedback_data[fid] = {"user_id": str(user_id), "type": feedback_type, "message": message, "date": iso(now()), "status": "pending"}
    save_json(FEEDBACK_FILE, feedback_data)
    return fid

async def complaint_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: /complaint message"); return
    fid = save_feedback(update.effective_user.id, "complaint", " ".join(context.args))
    await update.message.reply_text(f"✅ Received. ID: {fid}")

async def suggest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: /suggest message"); return
    fid = save_feedback(update.effective_user.id, "suggestion", " ".join(context.args))
    await update.message.reply_text(f"✅ Received. ID: {fid}")

async def bug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: await update.message.reply_text("Usage: /bug message"); return
    fid = save_feedback(update.effective_user.id, "bug", " ".join(context.args))
    await update.message.reply_text(f"🐛 Received. ID: {fid}")

async def myfeedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    mine = [(fid, data) for fid, data in feedback_data.items() if data.get("user_id") == uid]
    if not mine: await update.message.reply_text("No feedback."); return
    text = "📝 YOUR FEEDBACK\n"
    for fid, data in mine[-10:]:
        text += f"ID: {fid}\n{data['type']}: {data['message'][:100]}\n\n"
    await update.message.reply_text(text)

# =========================================================
# TEXT & HELP HANDLERS
# =========================================================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text in ALLOWED_PHONES:
        context.user_data["photo_mode"] = "payment"
        await update.message.reply_text("💰 Payment mode. Send screenshot.")
        return
    if text.lower() in ["hello", "hi", "ሰላም"]:
        await update.message.reply_text("👋 Use /start")
        return
    await update.message.reply_text("🤖 Use /start or /help")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ HELP\n\n"
        "🔮 /predict BTC - Get AI prediction\n"
        "📸 Send chart - Analyze with AI\n"
        "🔔 /alert BTC 120000 above - Set price alert\n"
        "📊 /alerts - View your alerts\n"
        "💰 /payment - Subscription info\n"
        "🌍 /impact Bitcoin - Macro impact\n"
        "⏸️ /pause - Pause auto-trading\n"
        "▶️ /resume - Resume auto-trading\n"
        "🔒 /closetrade - Close active trade manually\n"
        "📝 /complaint message - Send feedback"
    )

def localized_prediction_footer(lang, quality, sources):
    labels = {
        "en": ("DATA QUALITY", "Live", "Tech", "News", "Macro", "Sources", "⚠️ Not advice."),
        "am": ("የመረጃ ጥራት", "ገበያ", "ቴክ", "ዜና", "ማክሮ", "ምንጮች", "⚠️ ምክር አይደለም።"),
        "om": ("QULQULLINA", "Gabaa", "Teek", "Oduu", "Makroo", "Madda", "⚠️ Gorsa miti.")
    }
    d = labels.get(lang, labels["en"])
    return (f"\n\n━━━━━━━━━━━━━━━━\n📡 {d[0]}\n{d[1]}: {'✅' if quality.get('live_market') else '❌'}\n{d[2]}: {'✅' if quality.get('technical') else '❌'}\n{d[3]}: {'✅' if quality.get('news') else '❌'}\n{d[4]}: {'✅' if quality.get('macro') else '❌'}\n\n📚 {d[5]}: {', '.join(sources) if sources else 'None'}\n\n{d[6]}")

# =========================================================
# SUBSCRIPTION EXPIRY CHECK
# =========================================================
async def check_subscription_expiry(context: ContextTypes.DEFAULT_TYPE):
    changed = False
    for user_id, user in list(users_data.items()):
        if user.get("status") != "paid": continue
        try:
            expiry = ensure_utc_datetime(datetime.fromisoformat(user["expiry"]))
        except: continue
        seconds_left = (expiry - now()).total_seconds()
        days_left = int(seconds_left // 86400) if seconds_left > 0 else 0
        warnings = user.setdefault("expiry_warnings", [])
        for warning_day in (7, 3, 1):
            if days_left <= warning_day and warning_day not in warnings and seconds_left > 0:
                try:
                    await context.bot.send_message(chat_id=int(user_id), text=f"⚠️ Expires in {warning_day} days.")
                    warnings.append(warning_day)
                    changed = True
                except: pass
        if seconds_left <= 0:
            user["status"] = "expired"
            warnings.append("expired")
            changed = True
    if changed:
        save_json(USERS_FILE, users_data)

# =========================================================
# APP SETUP
# =========================================================
app = Application.builder().token(TELEGRAM_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("predict", predict_command))
app.add_handler(CommandHandler("learning", learning_command))
app.add_handler(CommandHandler("payment", payment_command))
app.add_handler(CommandHandler("alert", alert_command))
app.add_handler(CommandHandler("alerts", alerts_command))
app.add_handler(CommandHandler("cancelalert", cancel_alert_command))
app.add_handler(CommandHandler("economy", economy_command))
app.add_handler(CommandHandler("impact", impact_command))
app.add_handler(CommandHandler("approve", approve_command))
app.add_handler(CommandHandler("reject", reject_command))
app.add_handler(CommandHandler("complaint", complaint_command))
app.add_handler(CommandHandler("suggest", suggest_command))
app.add_handler(CommandHandler("bug", bug_command))
app.add_handler(CommandHandler("myfeedback", myfeedback_command))
app.add_handler(CommandHandler("help", help_command))
# New commands
app.add_handler(CommandHandler("pause", pause_command))
app.add_handler(CommandHandler("resume", resume_command))
app.add_handler(CommandHandler("closetrade", close_trade_command))
app.add_handler(CallbackQueryHandler(callback_handler))
app.add_handler(MessageHandler(filters.PHOTO, photo_router))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

# =========================================================
# BACKGROUND JOBS
# =========================================================
if app.job_queue:
    # Price alerts every 30 seconds
    app.job_queue.run_repeating(check_price_alerts, interval=30, first=10, job_kwargs={"coalesce": True, "max_instances": 1})

    # Self-learning every 5 minutes
    async def _self_learning_job(context):
        await asyncio.to_thread(evaluate_self_learning)
    app.job_queue.run_repeating(_self_learning_job, interval=300, first=60, job_kwargs={"coalesce": True, "max_instances": 1})

    # Subscription expiry every hour
    app.job_queue.run_repeating(check_subscription_expiry, interval=3600, first=30, job_kwargs={"coalesce": True, "max_instances": 1})

    # Midnight reset at 00:00 UTC daily
    from datetime import time as dt_time
    app.job_queue.run_daily(
        midnight_reset_job,
        time=dt_time(hour=0, minute=0, second=0, tzinfo=timezone.utc),
        job_kwargs={"coalesce": True, "max_instances": 1}
    )
    print("🌅 Midnight reset scheduled at 00:00 UTC")

    # Auto-trade
    if exchange:
        app.job_queue.run_repeating(auto_trade_job, interval=AUTO_TRADE_INTERVAL, first=15, job_kwargs={"coalesce": True, "max_instances": 1})
        print(f"🔄 Auto-Trade: {AUTO_TRADE_SYMBOL} every {AUTO_TRADE_INTERVAL}s")
    else:
        print("⏸️ Auto-Trade disabled.")
else:
    print("⚠️ JobQueue unavailable.")

# =========================================================
# STARTUP INFO
# =========================================================
print("=" * 65)
print("🤖 ALPHA AI (AUTO-TRADE v3.0)")
print("=" * 65)
print(f"📊 Symbol: {AUTO_TRADE_SYMBOL}")
print(f"🎯 Min Confidence: {MIN_CONFIDENCE_TO_TRADE}%")
print(f"💰 Risk: {AUTO_TRADE_RISK_PCT}% per trade")
print(f"🔁 Interval: {AUTO_TRADE_INTERVAL}s")
print(f"📦 Min Qty: {MIN_TRADE_QUANTITY}")
print(f"🛡️ Max Daily Loss: {MAX_DAILY_LOSS_PCT}%")
print("=" * 65)
print("📌 Commands:")
print("  /pause     - Pause auto-trading")
print("  /resume    - Resume auto-trading")
print("  /closetrade - Close active trade manually")
print("  /predict BTC - Get AI prediction")
print("=" * 65)

# =========================================================
# RUN
# =========================================================
app.run_polling(drop_pending_updates=True)