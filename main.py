import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# БАЗОВЫЕ НАСТРОЙКИ
# =========================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "439636200"))

GOOGLE_APPS_SCRIPT_URL = os.getenv("GOOGLE_APPS_SCRIPT_URL", "").strip()
GOOGLE_APPS_SCRIPT_SECRET = os.getenv("GOOGLE_APPS_SCRIPT_SECRET", "").strip()

BISHKEK_TZ = timezone(timedelta(hours=6))

SUBSCRIBERS_FILE = "subscribers.json"

SCHEDULE_HOURS_BISHKEK = {7, 9, 11, 13, 15, 17}
SENT_NOTIFICATION_KEYS = set()

MAX_HISTORY_DAYS = 90

# =========================
# СПРАВОЧНИК БАНКОВ ДЛЯ ИСТОРИИ
# =========================

HISTORY_BANKS = [
    {"id": 1, "name": "Элдик банк"},
    {"id": 2, "name": "Оптима Банк"},
    {"id": 3, "name": "КИКБ"},
    {"id": 4, "name": "Керемет Банк"},
    {"id": 5, "name": "MBANK"},
    {"id": 6, "name": "Демир банк"},
    {"id": 7, "name": "Банк Азии"},
    {"id": 8, "name": "Кыргызкоммерцбанк"},
    {"id": 9, "name": "Банк Компаньон"},
    {"id": 10, "name": 'Банк "Бай Тушум"'},
    {"id": 12, "name": "АБанк"},
    {"id": 13, "name": "O!Bank"},
    {"id": 14, "name": "Бакай Банк"},
    {"id": 15, "name": "Толубай Банк"},
    {"id": 16, "name": "Дос-Кредобанк"},
    {"id": 18, "name": "ФИНКА Банк"},
    {"id": 19, "name": "Капитал Банк"},
    {"id": 20, "name": "Коммерческий Банк КСБ"},
    {"id": 21, "name": "Евразийский Сберегательный Банк"},
    {"id": 22, "name": "ФинансКредитБанк"},
]

# =========================
# КНОПКИ
# =========================

BTN_RATES = "📊 Курсы сейчас"
BTN_CALC = "🧮 Калькулятор"
BTN_HISTORY = "📈 История курсов"
BTN_BANK_HISTORY = "🏦 История по банку"
BTN_SUBSCRIBE = "🔔 Подписаться на рассылку"
BTN_UNSUBSCRIBE = "🔕 Отписаться"
BTN_HELP = "❓ Помощь"
BTN_USERS = "👥 Пользователи"


def main_keyboard(chat_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    rows = [
        [BTN_RATES, BTN_CALC],
        [BTN_HISTORY, BTN_BANK_HISTORY],
        [BTN_SUBSCRIBE, BTN_UNSUBSCRIBE],
        [BTN_HELP],
    ]

    if chat_id == ADMIN_CHAT_ID:
        rows.append([BTN_USERS])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# =========================
# ОБЩИЕ УТИЛИТЫ
# =========================

def now_bishkek() -> datetime:
    return datetime.now(BISHKEK_TZ)


def format_dt_bishkek(dt: Optional[datetime] = None) -> str:
    dt = dt or now_bishkek()
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace("\xa0", " ").replace(" ", "").replace(",", ".")

    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def fmt_rate(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    return f"{value:.4f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    return f"{value:.2f}%"


def fmt_money(value: Optional[float]) -> str:
    if value is None:
        return "н/д"
    return f"{value:,.2f}".replace(",", " ")


async def send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
    except Exception as exc:
        logging.exception("Не удалось отправить typing action: %s", exc)


# =========================
# GOOGLE SHEETS СИНХРОНИЗАЦИЯ
# =========================

def post_to_google(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not GOOGLE_APPS_SCRIPT_URL or not GOOGLE_APPS_SCRIPT_SECRET:
        return None

    payload = dict(payload)
    payload["secret_token"] = GOOGLE_APPS_SCRIPT_SECRET

    try:
        response = requests.post(
            GOOGLE_APPS_SCRIPT_URL,
            json=payload,
            timeout=12,
        )
    except Exception as exc:
        logging.exception("Google Sheets sync error: %s", exc)
        return None

    if response.status_code != 200:
        logging.error(
            "Google Sheets sync HTTP error: %s %s",
            response.status_code,
            response.text[:500],
        )
        return None

    try:
        data = response.json()
    except Exception:
        logging.error(
            "Google Sheets sync returned non-json response: %s",
            response.text[:500],
        )
        return None

    if not data.get("ok"):
        logging.error("Google Sheets sync returned error: %s", data)

    return data


def sync_user_activity(update: Update, action: str, is_subscribed: bool = False) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    post_to_google({
        "event_type": "user_activity",
        "chat_id": chat.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "datetime_bishkek": format_dt_bishkek(),
        "last_seen": format_dt_bishkek(),
        "last_action": action,
        "actions_count": 1,
        "is_subscribed": is_subscribed,
    })


def sync_rates_log(
    source_type: str,
    bakai_rate: Optional[float],
    aiyl_rate: Optional[float],
    nbkr_rate: Optional[float],
) -> None:
    best_bank = ""
    bank_difference_abs = None
    bank_difference_pct = None

    bank_rates = []
    if bakai_rate:
        bank_rates.append(("Бакай Банк", bakai_rate))
    if aiyl_rate:
        bank_rates.append(("Айыл Банк / A-bank", aiyl_rate))

    if bank_rates:
        best_bank, best_rate = min(bank_rates, key=lambda x: x[1])
        if len(bank_rates) >= 2:
            rates_only = [x[1] for x in bank_rates]
            bank_difference_abs = max(rates_only) - min(rates_only)
            bank_difference_pct = bank_difference_abs / min(rates_only) * 100

    bakai_spread_abs = None
    bakai_spread_pct = None
    aiyl_spread_abs = None
    aiyl_spread_pct = None

    if nbkr_rate:
        if bakai_rate:
            bakai_spread_abs = bakai_rate - nbkr_rate
            bakai_spread_pct = (bakai_rate / nbkr_rate - 1) * 100
        if aiyl_rate:
            aiyl_spread_abs = aiyl_rate - nbkr_rate
            aiyl_spread_pct = (aiyl_rate / nbkr_rate - 1) * 100

    post_to_google({
        "event_type": "rates_log",
        "datetime_bishkek": format_dt_bishkek(),
        "source_type": source_type,
        "bakai_rate": bakai_rate or "",
        "aiyl_rate": aiyl_rate or "",
        "nbkr_rate": nbkr_rate or "",
        "best_bank": best_bank,
        "bank_difference_abs": bank_difference_abs or "",
        "bank_difference_pct": bank_difference_pct or "",
        "bakai_spread_abs": bakai_spread_abs or "",
        "bakai_spread_pct": bakai_spread_pct or "",
        "aiyl_spread_abs": aiyl_spread_abs or "",
        "aiyl_spread_pct": aiyl_spread_pct or "",
    })


def sync_subscriber_update(update: Update, is_active: bool) -> None:
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    post_to_google({
        "event_type": "subscriber_update",
        "chat_id": chat.id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "datetime_bishkek": format_dt_bishkek(),
        "is_active": is_active,
    })


def get_active_subscribers_from_google() -> Optional[List[int]]:
    response = post_to_google({
        "event_type": "get_active_subscribers",
    })

    if not response or not response.get("ok"):
        return None

    result = []
    for chat_id in response.get("active_chat_ids", []):
        try:
            result.append(int(chat_id))
        except Exception:
            continue

    return result


# =========================
# ПОДПИСКИ
# =========================

def load_subscribers() -> List[int]:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []

    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception:
        return []

    result = []
    for value in data:
        try:
            result.append(int(value))
        except Exception:
            continue

    return sorted(set(result))


def save_subscribers(subscribers: List[int]) -> None:
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as file:
            json.dump(sorted(set(subscribers)), file, ensure_ascii=False, indent=2)
    except Exception as exc:
        logging.exception("Не удалось сохранить subscribers.json: %s", exc)


def add_subscriber(chat_id: int) -> None:
    subscribers = load_subscribers()
    if chat_id not in subscribers:
        subscribers.append(chat_id)
        save_subscribers(subscribers)


def remove_subscriber(chat_id: int) -> None:
    subscribers = load_subscribers()
    subscribers = [item for item in subscribers if item != chat_id]
    save_subscribers(subscribers)


def get_effective_subscribers() -> List[int]:
    google_subscribers = get_active_subscribers_from_google()

    if google_subscribers is not None:
        return sorted(set(google_subscribers))

    return load_subscribers()


def is_user_subscribed(chat_id: int) -> bool:
    return chat_id in get_effective_subscribers()


def track_user(update: Update, action: str) -> None:
    chat = update.effective_chat
    if not chat:
        return

    subscribed = is_user_subscribed(chat.id)
    sync_user_activity(update, action, subscribed)


# =========================
# НБКР
# =========================

def parse_nbkr_report_date(root: ET.Element) -> Optional[datetime]:
    """
    НБКР в XML обычно отдаёт дату отчёта в атрибуте Date.
    Нам важно понимать, за какую дату реально пришёл курс.
    """
    for attr_name in ("Date", "date"):
        raw_date = root.attrib.get(attr_name)
        if raw_date:
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    return datetime.strptime(raw_date, fmt).replace(tzinfo=BISHKEK_TZ)
                except Exception:
                    continue

    return None


def get_nbkr_rate_with_actual_date(
    date_obj: Optional[datetime] = None,
) -> Tuple[Optional[float], str, Optional[datetime]]:
    """
    Возвращает:
    1. курс RUB/KGS по НБКР;
    2. текст источника;
    3. фактическую дату курса, которую вернул НБКР.
    """
    if date_obj is None:
        url = "https://www.nbkr.kg/XML/daily.xml"
    else:
        date_req = date_obj.strftime("%d.%m.%Y")
        url = f"https://www.nbkr.kg/XML/daily.xml?date_req={date_req}"

    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
    except Exception as exc:
        logging.exception("Ошибка получения курса НБКР: %s", exc)
        return None, "НБКР: не удалось получить данные", None

    try:
        root = ET.fromstring(response.content)
    except Exception as exc:
        logging.exception("Ошибка разбора XML НБКР: %s", exc)
        return None, "НБКР: не удалось разобрать данные", None

    actual_date = parse_nbkr_report_date(root)

    for currency in root.findall(".//Currency"):
        if currency.attrib.get("ISOCode") == "RUB":
            nominal = parse_float(currency.findtext("Nominal")) or 1
            value = parse_float(currency.findtext("Value"))

            if value is None:
                return None, "НБКР: курс RUB не найден", actual_date

            rate = value / nominal

            if actual_date:
                source = (
                    "НБКР: получен с официального сайта НБКР, "
                    f"дата курса: {actual_date.strftime('%d.%m.%Y')}"
                )
            else:
                source = "НБКР: получен с официального сайта НБКР"

            return rate, source, actual_date

    return None, "НБКР: курс RUB не найден", actual_date


def get_nbkr_rate_for_date(date_obj: Optional[datetime] = None) -> Tuple[Optional[float], str]:
    """
    Совместимость со старой логикой текущих курсов.
    """
    rate, source, _actual_date = get_nbkr_rate_with_actual_date(date_obj)
    return rate, source


def get_nbkr_rate_for_history_date(
    target_date: datetime,
) -> Tuple[Optional[float], Optional[datetime]]:
    """
    Для истории берём курс НБКР на дату.
    Если на дату нет курса из-за выходного/праздника, берём ближайший предыдущий доступный курс.
    """
    for days_back in range(0, 10):
        request_date = target_date - timedelta(days=days_back)

        rate, _source, actual_date = get_nbkr_rate_with_actual_date(request_date)

        if not rate:
            continue

        if actual_date is None:
            return rate, request_date

        if actual_date.date() <= target_date.date():
            return rate, actual_date

    return None, None


# =========================
# ПАРСЕРЫ ТЕКУЩИХ КУРСОВ
# =========================

def get_bakai_rate() -> Tuple[Optional[float], str]:
    url = "https://bakai.kg/"

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        html = response.text
    except Exception as exc:
        logging.exception("Ошибка получения курса Бакай: %s", exc)
        return None, "Бакай Банк: не удалось получить данные"

    try:
        patterns = [
            r'"RUB".{0,2000}?"non_cash".{0,1000}?"sell"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
            r'"rub".{0,2000}?"non_cash".{0,1000}?"sell"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
            r'RUB.{0,2000}?non_cash.{0,1000}?sell.{0,50}?([0-9]+(?:[.,][0-9]+)?)',
        ]

        rate = None
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            if match:
                rate = parse_float(match.group(1))
                if rate:
                    break

        if rate is None:
            return None, "Бакай Банк: курс RUB / безналичная продажа не найден"

        date_text = ""
        date_match = re.search(
            r'"last_execution"\s*:\s*"([^"]+)"',
            html,
            re.IGNORECASE,
        )
        if date_match:
            raw = date_match.group(1)
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                date_text = parsed.strftime("%d.%m.%Y %H:%M")
            except Exception:
                date_text = raw[:16]

        if date_text:
            source = (
                "Бакай Банк: получен с официального сайта, "
                f"тип курса: RUB / безналичная продажа, дата обновления на сайте: {date_text}"
            )
        else:
            source = (
                "Бакай Банк: получен с официального сайта, "
                "тип курса: RUB / безналичная продажа"
            )

        return rate, source

    except Exception as exc:
        logging.exception("Ошибка разбора курса Бакай: %s", exc)
        return None, "Бакай Банк: ошибка разбора данных"


def get_aiyl_rate() -> Tuple[Optional[float], str]:
    url = "https://abank.kg/ky"

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        html = response.text
    except Exception as exc:
        logging.exception("Ошибка получения курса A-bank: %s", exc)
        return None, "Айыл Банк / A-bank: не удалось получить данные"

    try:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)

        rub_positions = [m.start() for m in re.finditer(r"\bRUB\b|Руб", text, re.IGNORECASE)]

        candidates = []
        for pos in rub_positions:
            chunk = text[pos:pos + 500]
            nums = re.findall(r"\d+[.,]\d+", chunk)
            parsed_nums = [parse_float(x) for x in nums]
            parsed_nums = [x for x in parsed_nums if x is not None]

            if len(parsed_nums) >= 2:
                candidates.append(parsed_nums[1])

        if not candidates:
            return None, "Айыл Банк / A-bank: курс RUB / безналичная продажа не найден"

        rate = candidates[-1]

        date_match = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", text)
        date_text = date_match.group(0) if date_match else ""

        source = (
            "Айыл Банк / A-bank: получен с официального сайта, "
            "тип курса: безналичная продажа RUB"
        )
        if date_text:
            source += f", дата на сайте: {date_text}"

        return rate, source

    except Exception as exc:
        logging.exception("Ошибка разбора курса A-bank: %s", exc)
        return None, "Айыл Банк / A-bank: ошибка разбора данных"


def collect_current_rates() -> Dict[str, Any]:
    bakai_rate, bakai_source = get_bakai_rate()
    aiyl_rate, aiyl_source = get_aiyl_rate()
    nbkr_rate, nbkr_source = get_nbkr_rate_for_date()

    return {
        "bakai_rate": bakai_rate,
        "bakai_source": bakai_source,
        "aiyl_rate": aiyl_rate,
        "aiyl_source": aiyl_source,
        "nbkr_rate": nbkr_rate,
        "nbkr_source": nbkr_source,
    }


def build_rates_message(rates_data: Dict[str, Any]) -> str:
    bakai_rate = rates_data.get("bakai_rate")
    aiyl_rate = rates_data.get("aiyl_rate")
    nbkr_rate = rates_data.get("nbkr_rate")

    lines = []
    lines.append("📊 Курсы RUB / KGS сейчас")
    lines.append("Тип курса: безналичная продажа RUB")
    lines.append("")

    lines.append(f"Бакай Банк: {fmt_rate(bakai_rate)}")
    lines.append(f"Айыл Банк / A-bank: {fmt_rate(aiyl_rate)}")
    lines.append(f"НБКР: {fmt_rate(nbkr_rate)}")
    lines.append("")

    available_banks = []
    if bakai_rate:
        available_banks.append(("Бакай Банк", bakai_rate))
    if aiyl_rate:
        available_banks.append(("Айыл Банк / A-bank", aiyl_rate))

    if available_banks:
        best_bank, best_rate = min(available_banks, key=lambda x: x[1])
        lines.append(f"Лучший курс сейчас: {best_bank} — {fmt_rate(best_rate)}")
        lines.append("")

    if nbkr_rate:
        lines.append("Спред к НБКР:")
        if bakai_rate:
            spread = (bakai_rate / nbkr_rate - 1) * 100
            lines.append(f"Бакай Банк: {fmt_pct(spread)}")
        if aiyl_rate:
            spread = (aiyl_rate / nbkr_rate - 1) * 100
            lines.append(f"Айыл Банк / A-bank: {fmt_pct(spread)}")
        lines.append("")

    lines.append("Источники:")
    lines.append(f"• {rates_data.get('bakai_source')}")
    lines.append(f"• {rates_data.get('aiyl_source')}")
    lines.append(f"• {rates_data.get('nbkr_source')}")

    return "\n".join(lines)


# =========================
# ИСТОРИЯ BANKS.KG
# =========================

def parse_bankskg_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        try:
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric = numeric / 1000
            return datetime.fromtimestamp(numeric, tz=BISHKEK_TZ)
        except Exception:
            return None

    text = str(value).strip()
    if not text:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", text):
        try:
            numeric = float(text)
            if numeric > 10_000_000_000:
                numeric = numeric / 1000
            return datetime.fromtimestamp(numeric, tz=BISHKEK_TZ)
        except Exception:
            pass

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text[:26], fmt)
            return parsed.replace(tzinfo=BISHKEK_TZ)
        except Exception:
            continue

    return None


def normalize_history_points(raw_points: Any) -> List[Dict[str, Any]]:
    result = []

    if not raw_points or not isinstance(raw_points, list):
        return result

    for item in raw_points:
        dt = None
        rate = None

        if isinstance(item, list) and len(item) >= 2:
            dt = parse_bankskg_timestamp(item[0])
            rate = parse_float(item[1])

        elif isinstance(item, dict):
            dt_value = (
                item.get("date")
                or item.get("datetime")
                or item.get("created_at")
                or item.get("time")
                or item.get("timestamp")
                or item.get("x")
            )

            rate_value = (
                item.get("value")
                or item.get("rate")
                or item.get("y")
                or item.get("sell")
                or item.get("buy")
            )

            dt = parse_bankskg_timestamp(dt_value)
            rate = parse_float(rate_value)

        if dt and rate:
            result.append({
                "datetime": dt,
                "date": dt.date(),
                "rate": rate,
            })

    result.sort(key=lambda x: x["datetime"])
    return result


def fetch_bank_history_from_bankskg(organization_id: int) -> Dict[str, Any]:
    url = (
        "https://banks.kg/api/rates/bank-history-cached"
        f"?currency=rub&organization_id={organization_id}&type=cashless"
    )

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://banks.kg/",
    }

    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logging.exception("Ошибка banks.kg для organization_id=%s: %s", organization_id, exc)
        return {"buy": [], "sell": []}

    if isinstance(data, dict):
        if "buy" in data or "sell" in data:
            return {
                "buy": normalize_history_points(data.get("buy")),
                "sell": normalize_history_points(data.get("sell")),
            }

        for key in ("data", "result", "history"):
            nested = data.get(key)
            if isinstance(nested, dict) and ("buy" in nested or "sell" in nested):
                return {
                    "buy": normalize_history_points(nested.get("buy")),
                    "sell": normalize_history_points(nested.get("sell")),
                }

    return {"buy": [], "sell": []}


def parse_history_period(text: str) -> Tuple[Optional[datetime], Optional[datetime], Optional[str]]:
    normalized = text.strip().lower()
    normalized = normalized.replace("—", "-").replace("–", "-")

    today = now_bishkek().replace(hour=0, minute=0, second=0, microsecond=0)

    match = re.search(r"последн\w*\s+(\d+)\s+д", normalized)
    if match:
        days = int(match.group(1))
        if days <= 0:
            return None, None, "Период должен быть больше 0 дней."
        if days > MAX_HISTORY_DAYS:
            return None, None, f"Пока максимальный период для анализа — {MAX_HISTORY_DAYS} дней."
        start = today - timedelta(days=days - 1)
        end = today
        return start, end, None

    dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", normalized)

    if len(dates) == 1:
        try:
            start = datetime.strptime(dates[0], "%d.%m.%Y").replace(tzinfo=BISHKEK_TZ)
            return start, start, None
        except Exception:
            return None, None, "Не удалось распознать дату."

    if len(dates) >= 2:
        try:
            start = datetime.strptime(dates[0], "%d.%m.%Y").replace(tzinfo=BISHKEK_TZ)
            end = datetime.strptime(dates[1], "%d.%m.%Y").replace(tzinfo=BISHKEK_TZ)
        except Exception:
            return None, None, "Не удалось распознать период."

        if end < start:
            start, end = end, start

        days_count = (end.date() - start.date()).days + 1

        if days_count > MAX_HISTORY_DAYS:
            return None, None, f"Пока максимальный период для анализа — {MAX_HISTORY_DAYS} дней."

        return start, end, None

    return None, None, (
        "Не удалось распознать период.\n\n"
        "Введите, например:\n"
        "12.06.2026\n"
        "01.06.2026-12.06.2026\n"
        "последние 7 дней"
    )


def normalize_bank_search_text(text: str) -> str:
    return (
        text.lower()
        .replace("ё", "е")
        .replace('"', "")
        .replace("«", "")
        .replace("»", "")
        .replace("!", "")
        .strip()
    )


def find_history_bank(text: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_bank_search_text(text)

    if normalized.isdigit():
        bank_id = int(normalized)
        for bank in HISTORY_BANKS:
            if bank["id"] == bank_id:
                return bank

    aliases = {
        "мбанк": "MBANK",
        "mbank": "MBANK",
        "м банк": "MBANK",
        "обанк": "O!Bank",
        "o bank": "O!Bank",
        "obank": "O!Bank",
        "о банк": "O!Bank",
        "абанк": "АБанк",
        "a bank": "АБанк",
        "abank": "АБанк",
        "а банк": "АБанк",
        "бакай": "Бакай Банк",
        "оптима": "Оптима Банк",
        "кикб": "КИКБ",
        "бки": "КИКБ",
        "kicb": "КИКБ",
        "элдик": "Элдик банк",
        "керемет": "Керемет Банк",
        "демир": "Демир банк",
        "банк азии": "Банк Азии",
        "компаньон": "Банк Компаньон",
        "бай тушум": 'Банк "Бай Тушум"',
        "толубай": "Толубай Банк",
        "дос кредо": "Дос-Кредобанк",
        "дос-кредо": "Дос-Кредобанк",
        "финка": "ФИНКА Банк",
        "капитал": "Капитал Банк",
        "ксб": "Коммерческий Банк КСБ",
        "евразийский": "Евразийский Сберегательный Банк",
        "финанскредит": "ФинансКредитБанк",
        "финанс кредит": "ФинансКредитБанк",
    }

    for alias, bank_name in aliases.items():
        if alias in normalized:
            for bank in HISTORY_BANKS:
                if bank["name"] == bank_name:
                    return bank

    for bank in HISTORY_BANKS:
        bank_name_normalized = normalize_bank_search_text(bank["name"])
        if normalized in bank_name_normalized or bank_name_normalized in normalized:
            return bank

    return None


def build_bank_selection_message() -> str:
    lines = []
    lines.append("Выберите банк для анализа.")
    lines.append("")
    lines.append("Можно написать номер или название банка.")
    lines.append("")

    for bank in HISTORY_BANKS:
        lines.append(f"{bank['id']} — {bank['name']}")

    lines.append("")
    lines.append("Примеры:")
    lines.append("5")
    lines.append("MBANK")
    lines.append("Бакай")
    lines.append("КИКБ")

    return "\n".join(lines)


def get_nbkr_history_rates(start: datetime, end: datetime) -> Dict[Any, Dict[str, Any]]:
    """
    Возвращает словарь:
    {
        date: {
            "rate": 1.0732,
            "actual_date": date(...)
        }
    }
    """
    result = {}
    current = start

    while current.date() <= end.date():
        rate, actual_date = get_nbkr_rate_for_history_date(current)

        if rate:
            result[current.date()] = {
                "rate": rate,
                "actual_date": actual_date.date() if actual_date else current.date(),
            }

        current += timedelta(days=1)

    return result


def calculate_bank_history_summary(
    bank_name: str,
    organization_id: int,
    sell_points: List[Dict[str, Any]],
    nbkr_rates_by_date: Dict[Any, Dict[str, Any]],
    start: datetime,
    end: datetime,
) -> Optional[Dict[str, Any]]:
    filtered = [
        point for point in sell_points
        if start.date() <= point["date"] <= end.date()
    ]

    if not filtered:
        return None

    rates = [point["rate"] for point in filtered]
    spreads = []
    nbkr_rates_used = []
    negative_spread_points = 0

    for point in filtered:
        nbkr_item = nbkr_rates_by_date.get(point["date"])

        if not nbkr_item:
            continue

        nbkr_rate = nbkr_item.get("rate")

        if not nbkr_rate:
            continue

        nbkr_rates_used.append(nbkr_rate)

        spread_pct = (point["rate"] / nbkr_rate - 1) * 100
        spreads.append(spread_pct)

        if spread_pct < 0:
            negative_spread_points += 1

    last_point = max(filtered, key=lambda x: x["datetime"])

    return {
        "bank_name": bank_name,
        "organization_id": organization_id,
        "avg_sell_rate": mean(rates),
        "min_sell_rate": min(rates),
        "max_sell_rate": max(rates),
        "last_sell_rate": last_point["rate"],
        "avg_nbkr_rate": mean(nbkr_rates_used) if nbkr_rates_used else None,
        "min_nbkr_rate": min(nbkr_rates_used) if nbkr_rates_used else None,
        "max_nbkr_rate": max(nbkr_rates_used) if nbkr_rates_used else None,
        "avg_spread_pct": mean(spreads) if spreads else None,
        "min_spread_pct": min(spreads) if spreads else None,
        "max_spread_pct": max(spreads) if spreads else None,
        "negative_spread_points": negative_spread_points,
        "rate_points_count": len(filtered),
    }


def build_top_lines(
    items: List[Dict[str, Any]],
    key: str,
    reverse: bool = False,
    value_formatter=fmt_rate,
    limit: int = 5,
) -> List[str]:
    valid = [item for item in items if item.get(key) is not None]
    sorted_items = sorted(valid, key=lambda x: x[key], reverse=reverse)

    lines = []
    for index, item in enumerate(sorted_items[:limit], start=1):
        lines.append(f"{index}. {item['bank_name']} — {value_formatter(item[key])}")

    return lines


def build_history_message(
    start: datetime,
    end: datetime,
    summaries: List[Dict[str, Any]],
    no_data_banks: List[str],
) -> str:
    period_text = (
        format_date(start)
        if start.date() == end.date()
        else f"{format_date(start)}–{format_date(end)}"
    )

    lines = []
    lines.append(f"📈 История RUB / KGS за {period_text}")
    lines.append("Тип курса: безналичная продажа RUB")
    lines.append("")

    if not summaries:
        lines.append("За выбранный период данные по банкам не найдены.")
        return "\n".join(lines)

    lines.append("Лучший средний курс:")
    lines.extend(build_top_lines(summaries, "avg_sell_rate", reverse=False, value_formatter=fmt_rate, limit=5))
    lines.append("")

    lines.append("Минимальный курс за период:")
    lines.extend(build_top_lines(summaries, "min_sell_rate", reverse=False, value_formatter=fmt_rate, limit=5))
    lines.append("")

    lines.append("Максимальный курс за период:")
    lines.extend(build_top_lines(summaries, "max_sell_rate", reverse=True, value_formatter=fmt_rate, limit=5))
    lines.append("")

    nbkr_values = [
        item.get("avg_nbkr_rate")
        for item in summaries
        if item.get("avg_nbkr_rate") is not None
    ]

    if nbkr_values:
        lines.append("НБКР за период:")
        lines.append(f"средний: {fmt_rate(mean(nbkr_values))}")
        lines.append(f"минимум: {fmt_rate(min(nbkr_values))}")
        lines.append(f"максимум: {fmt_rate(max(nbkr_values))}")
        lines.append("")

    lines.append("Средний спред к НБКР:")
    spread_lines = build_top_lines(summaries, "avg_spread_pct", reverse=False, value_formatter=fmt_pct, limit=5)
    if spread_lines:
        lines.extend(spread_lines)
    else:
        lines.append("н/д")
    lines.append("")

    sorted_by_avg = sorted(summaries, key=lambda x: x["avg_sell_rate"])

    lines.append("Детализация по банкам:")
    for index, item in enumerate(sorted_by_avg, start=1):
        lines.append(f"{index}. {item['bank_name']}")
        lines.append(
            f"средний: {fmt_rate(item['avg_sell_rate'])} / "
            f"минимум: {fmt_rate(item['min_sell_rate'])} / "
            f"максимум: {fmt_rate(item['max_sell_rate'])}"
        )
        lines.append(
            f"последний: {fmt_rate(item['last_sell_rate'])} / "
            f"средний спред к НБКР: {fmt_pct(item['avg_spread_pct'])} / "
            f"изменений: {item['rate_points_count']}"
        )

        if item.get("negative_spread_points", 0) > 0:
            lines.append(f"аномалий ниже НБКР: {item['negative_spread_points']}")

        lines.append("")

    if no_data_banks:
        lines.append("Нет данных за период:")
        for bank in no_data_banks:
            lines.append(f"• {bank}")
        lines.append("")

    best_avg = min(summaries, key=lambda x: x["avg_sell_rate"])
    best_min = min(summaries, key=lambda x: x["min_sell_rate"])
    max_rate = max(summaries, key=lambda x: x["max_sell_rate"])

    spread_candidates = [x for x in summaries if x.get("avg_spread_pct") is not None]
    best_spread = min(spread_candidates, key=lambda x: x["avg_spread_pct"]) if spread_candidates else None

    comment = (
        f"Комментарий:\n"
        f"За выбранный период лучший средний курс был у {best_avg['bank_name']} — "
        f"{fmt_rate(best_avg['avg_sell_rate'])}. "
        f"Минимальный курс зафиксирован у {best_min['bank_name']} — "
        f"{fmt_rate(best_min['min_sell_rate'])}, "
        f"максимальный — у {max_rate['bank_name']} — "
        f"{fmt_rate(max_rate['max_sell_rate'])}."
    )

    if best_spread:
        comment += (
            f" Наименьший средний спред к НБКР был у {best_spread['bank_name']} — "
            f"{fmt_pct(best_spread['avg_spread_pct'])}."
        )

    comment += " Данные приведены для анализа рынка."
    lines.append(comment)

    message = "\n".join(lines)

    if len(message) <= 3900:
        return message

    lines_short = []
    lines_short.append(f"📈 История RUB / KGS за {period_text}")
    lines_short.append("Тип курса: безналичная продажа RUB")
    lines_short.append("")

    lines_short.append("Лучший средний курс:")
    lines_short.extend(build_top_lines(summaries, "avg_sell_rate", reverse=False, value_formatter=fmt_rate, limit=5))
    lines_short.append("")

    lines_short.append("Минимальный курс за период:")
    lines_short.extend(build_top_lines(summaries, "min_sell_rate", reverse=False, value_formatter=fmt_rate, limit=5))
    lines_short.append("")

    lines_short.append("Максимальный курс за период:")
    lines_short.extend(build_top_lines(summaries, "max_sell_rate", reverse=True, value_formatter=fmt_rate, limit=5))
    lines_short.append("")

    if nbkr_values:
        lines_short.append("НБКР за период:")
        lines_short.append(f"средний: {fmt_rate(mean(nbkr_values))}")
        lines_short.append(f"минимум: {fmt_rate(min(nbkr_values))}")
        lines_short.append(f"максимум: {fmt_rate(max(nbkr_values))}")
        lines_short.append("")

    lines_short.append("Средний спред к НБКР:")
    lines_short.extend(spread_lines if spread_lines else ["н/д"])
    lines_short.append("")

    lines_short.append("Краткая детализация по банкам:")
    for index, item in enumerate(sorted_by_avg[:10], start=1):
        extra = ""
        if item.get("negative_spread_points", 0) > 0:
            extra = f", аномалий ниже НБКР: {item['negative_spread_points']}"

        lines_short.append(
            f"{index}. {item['bank_name']}: "
            f"средний {fmt_rate(item['avg_sell_rate'])}, "
            f"min {fmt_rate(item['min_sell_rate'])}, "
            f"max {fmt_rate(item['max_sell_rate'])}, "
            f"спред {fmt_pct(item['avg_spread_pct'])}"
            f"{extra}"
        )

    if no_data_banks:
        lines_short.append("")
        lines_short.append("Нет данных за период:")
        lines_short.append(", ".join(no_data_banks[:10]))
        if len(no_data_banks) > 10:
            lines_short.append(f"и ещё {len(no_data_banks) - 10}")

    lines_short.append("")
    lines_short.append(comment)

    return "\n".join(lines_short)


def get_history_analysis(start: datetime, end: datetime) -> str:
    nbkr_rates_by_date = get_nbkr_history_rates(start, end)

    summaries = []
    no_data_banks = []

    for bank in HISTORY_BANKS:
        history = fetch_bank_history_from_bankskg(bank["id"])
        sell_points = history.get("sell", [])

        summary = calculate_bank_history_summary(
            bank_name=bank["name"],
            organization_id=bank["id"],
            sell_points=sell_points,
            nbkr_rates_by_date=nbkr_rates_by_date,
            start=start,
            end=end,
        )

        if summary:
            summaries.append(summary)
        else:
            no_data_banks.append(bank["name"])

    return build_history_message(start, end, summaries, no_data_banks)


def build_single_bank_history_message(
    selected_bank: Dict[str, Any],
    start: datetime,
    end: datetime,
) -> str:
    nbkr_rates_by_date = get_nbkr_history_rates(start, end)

    selected_history = fetch_bank_history_from_bankskg(selected_bank["id"])
    selected_summary = calculate_bank_history_summary(
        bank_name=selected_bank["name"],
        organization_id=selected_bank["id"],
        sell_points=selected_history.get("sell", []),
        nbkr_rates_by_date=nbkr_rates_by_date,
        start=start,
        end=end,
    )

    period_text = (
        format_date(start)
        if start.date() == end.date()
        else f"{format_date(start)}–{format_date(end)}"
    )

    if not selected_summary:
        return (
            f"🏦 {selected_bank['name']}\n"
            f"История RUB / KGS за {period_text}\n"
            "Тип курса: безналичная продажа RUB\n\n"
            "За выбранный период данные по этому банку не найдены."
        )

    market_summaries = []

    for bank in HISTORY_BANKS:
        history = fetch_bank_history_from_bankskg(bank["id"])
        summary = calculate_bank_history_summary(
            bank_name=bank["name"],
            organization_id=bank["id"],
            sell_points=history.get("sell", []),
            nbkr_rates_by_date=nbkr_rates_by_date,
            start=start,
            end=end,
        )
        if summary:
            market_summaries.append(summary)

    def get_rank(
        items: List[Dict[str, Any]],
        key: str,
        bank_name: str,
        reverse: bool = False,
    ) -> Optional[int]:
        valid = [item for item in items if item.get(key) is not None]
        sorted_items = sorted(valid, key=lambda x: x[key], reverse=reverse)

        for index, item in enumerate(sorted_items, start=1):
            if item["bank_name"] == bank_name:
                return index

        return None

    total_banks = len(market_summaries)

    rank_avg = get_rank(
        market_summaries,
        "avg_sell_rate",
        selected_bank["name"],
        reverse=False,
    )

    rank_min = get_rank(
        market_summaries,
        "min_sell_rate",
        selected_bank["name"],
        reverse=False,
    )

    rank_spread = get_rank(
        market_summaries,
        "avg_spread_pct",
        selected_bank["name"],
        reverse=False,
    )

    lines = []
    lines.append(f"🏦 {selected_bank['name']}")
    lines.append(f"История RUB / KGS за {period_text}")
    lines.append("Тип курса: безналичная продажа RUB")
    lines.append("")

    lines.append("Показатели банка:")
    lines.append(f"средний курс: {fmt_rate(selected_summary['avg_sell_rate'])}")
    lines.append(f"минимум: {fmt_rate(selected_summary['min_sell_rate'])}")
    lines.append(f"максимум: {fmt_rate(selected_summary['max_sell_rate'])}")
    lines.append(f"последний: {fmt_rate(selected_summary['last_sell_rate'])}")
    lines.append(f"изменений курса: {selected_summary['rate_points_count']}")
    lines.append("")

    lines.append("НБКР за период:")
    lines.append(f"средний: {fmt_rate(selected_summary['avg_nbkr_rate'])}")
    lines.append(f"минимум: {fmt_rate(selected_summary['min_nbkr_rate'])}")
    lines.append(f"максимум: {fmt_rate(selected_summary['max_nbkr_rate'])}")
    lines.append("")

    lines.append("Спред к НБКР:")
    lines.append(f"средний: {fmt_pct(selected_summary['avg_spread_pct'])}")
    lines.append(f"минимальный: {fmt_pct(selected_summary['min_spread_pct'])}")
    lines.append(f"максимальный: {fmt_pct(selected_summary['max_spread_pct'])}")

    if selected_summary.get("negative_spread_points", 0) > 0:
        lines.append(f"аномалий ниже НБКР: {selected_summary['negative_spread_points']}")

    lines.append("")

    if total_banks:
        lines.append("Позиция среди банков:")
        if rank_avg:
            lines.append(f"по среднему курсу: {rank_avg} из {total_banks}")
        if rank_min:
            lines.append(f"по минимальному курсу: {rank_min} из {total_banks}")
        if rank_spread:
            lines.append(f"по среднему спреду к НБКР: {rank_spread} из {total_banks}")
        lines.append("")

    comment = (
        f"Комментарий:\n"
        f"За выбранный период средний курс {selected_bank['name']} составил "
        f"{fmt_rate(selected_summary['avg_sell_rate'])}, "
        f"средний спред к НБКР — {fmt_pct(selected_summary['avg_spread_pct'])}."
    )

    if rank_avg and total_banks:
        comment += f" По среднему курсу банк занял {rank_avg} место из {total_banks}."

    if selected_summary.get("negative_spread_points", 0) > 0:
        comment += " Есть точки ниже НБКР, их нужно проверить как возможную аномалию источника или сопоставления дат."

    comment += " Данные приведены для анализа рынка."

    lines.append(comment)

    return "\n".join(lines)


# =========================
# КОМАНДЫ И СЦЕНАРИИ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)
    track_user(update, "start")

    chat_id = update.effective_chat.id if update.effective_chat else None

    text = (
        "Привет! Я бот для мониторинга RUB / KGS.\n\n"
        "Я показываю текущие курсы, считаю спред к НБКР, помогаю рассчитать сумму "
        "для конвертации и анализирую историю курсов по банкам.\n\n"
        "Выберите действие кнопкой ниже."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(chat_id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)
    track_user(update, "help")

    chat_id = update.effective_chat.id if update.effective_chat else None

    text = (
        "❓ Помощь\n\n"
        "Что умеет бот:\n\n"
        "📊 Курсы сейчас\n"
        "Показывает текущие курсы RUB / KGS по банкам и спред к НБКР.\n\n"
        "🧮 Калькулятор\n"
        "Можно ввести сумму RUB, которую нужно купить. Бот рассчитает, сколько KGS потребуется "
        "по доступным курсам.\n\n"
        "📈 История курсов\n"
        "Показывает историю безналичной продажи RUB по банкам за выбранную дату или период. "
        "Бот считает лучший средний курс, минимальный и максимальный курс за период, "
        "а также средний спред к НБКР в процентах.\n\n"
        "🏦 История по банку\n"
        "Позволяет выбрать конкретный банк и посмотреть его исторический курс за дату или период. "
        "Бот показывает средний, минимальный, максимальный и последний курс, "
        "курс НБКР за период, спред к НБКР, а также место банка среди остальных банков.\n\n"
        "Примеры периода для истории:\n"
        "12.06.2026\n"
        "01.06.2026-12.06.2026\n"
        "последние 7 дней\n"
        "последние 30 дней\n\n"
        "🔔 Подписаться на рассылку\n"
        "Бот будет присылать курсы по рабочим дням по времени Бишкека: "
        "07:00, 09:00, 11:00, 13:00, 15:00, 17:00.\n\n"
        "Важно: исторические данные используются для анализа рынка. "
        "Фактическая возможность конвертации зависит от банка, лимитов, ликвидности и доступности курса."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(chat_id),
    )


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)
    track_user(update, "rates")

    rates_data = collect_current_rates()
    sync_rates_log(
        source_type="manual",
        bakai_rate=rates_data.get("bakai_rate"),
        aiyl_rate=rates_data.get("aiyl_rate"),
        nbkr_rate=rates_data.get("nbkr_rate"),
    )

    chat_id = update.effective_chat.id if update.effective_chat else None

    await update.message.reply_text(
        build_rates_message(rates_data),
        reply_markup=main_keyboard(chat_id),
    )


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data["mode"] = "calc"
    context.user_data.pop("selected_bank", None)
    track_user(update, "calc_start")

    await update.message.reply_text(
        "Введите сумму RUB, которую нужно купить.\n\nНапример:\n1000000",
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def calc_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    text = update.message.text or ""
    amount = parse_float(text)

    if not amount or amount <= 0:
        await update.message.reply_text(
            "Не удалось распознать сумму. Введите число, например: 1000000",
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    track_user(update, "calc_amount")

    rates_data = collect_current_rates()

    lines = []
    lines.append("🧮 Расчёт покупки RUB")
    lines.append(f"Сумма: {fmt_money(amount)} RUB")
    lines.append("")

    bank_rates = []
    if rates_data.get("bakai_rate"):
        bank_rates.append(("Бакай Банк", rates_data["bakai_rate"]))
    if rates_data.get("aiyl_rate"):
        bank_rates.append(("Айыл Банк / A-bank", rates_data["aiyl_rate"]))

    if not bank_rates:
        lines.append("Не удалось получить доступные курсы банков.")
    else:
        for bank_name, rate in bank_rates:
            kgs_needed = amount * rate
            lines.append(f"{bank_name}:")
            lines.append(f"курс: {fmt_rate(rate)}")
            lines.append(f"потребуется: {fmt_money(kgs_needed)} KGS")
            lines.append("")

        best_bank, best_rate = min(bank_rates, key=lambda x: x[1])
        lines.append(f"Лучший вариант: {best_bank} — {fmt_rate(best_rate)}")

    nbkr_rate = rates_data.get("nbkr_rate")
    if nbkr_rate:
        lines.append("")
        lines.append(f"Ориентир НБКР: {fmt_rate(nbkr_rate)}")

    context.user_data.pop("mode", None)

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data["mode"] = "history"
    context.user_data.pop("selected_bank", None)
    track_user(update, "history_start")

    text = (
        "Введите дату или период для анализа.\n\n"
        "Примеры:\n"
        "12.06.2026\n"
        "01.06.2026-12.06.2026\n"
        "последние 7 дней\n"
        "последние 30 дней\n\n"
        f"Максимальный период: {MAX_HISTORY_DAYS} дней."
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def history_period_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    text = update.message.text or ""
    start_period, end_period, error = parse_history_period(text)

    if error:
        await update.message.reply_text(
            error,
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    if not start_period or not end_period:
        await update.message.reply_text(
            "Не удалось распознать период.",
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    track_user(update, "history")

    await update.message.reply_text(
        "Собираю исторические данные по банкам и считаю спред к НБКР. Это может занять немного времени.",
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )

    await send_typing(update, context)

    try:
        message = get_history_analysis(start_period, end_period)
    except Exception as exc:
        logging.exception("Ошибка исторического анализа: %s", exc)
        message = (
            "Не удалось выполнить исторический анализ.\n\n"
            "Возможные причины: banks.kg временно не отвечает, изменился формат данных "
            "или выбран слишком большой период."
        )

    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def bank_history_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data["mode"] = "bank_select"
    context.user_data.pop("selected_bank", None)
    track_user(update, "bank_history_start")

    await update.message.reply_text(
        build_bank_selection_message(),
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def bank_selected_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    text = update.message.text or ""
    bank = find_history_bank(text)

    if not bank:
        await update.message.reply_text(
            "Не удалось найти банк.\n\n"
            "Напишите номер или название банка, например:\n"
            "5\n"
            "MBANK\n"
            "Бакай\n"
            "КИКБ",
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    context.user_data["selected_bank"] = bank
    context.user_data["mode"] = "bank_period"

    await update.message.reply_text(
        f"Выбран банк: {bank['name']}.\n\n"
        "Теперь введите дату или период для анализа.\n\n"
        "Примеры:\n"
        "12.06.2026\n"
        "01.06.2026-12.06.2026\n"
        "последние 7 дней\n"
        "последние 30 дней",
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def bank_period_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    selected_bank = context.user_data.get("selected_bank")
    if not selected_bank:
        context.user_data["mode"] = "bank_select"
        await update.message.reply_text(
            "Сначала выберите банк.",
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    text = update.message.text or ""
    start_period, end_period, error = parse_history_period(text)

    if error:
        await update.message.reply_text(
            error,
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    if not start_period or not end_period:
        await update.message.reply_text(
            "Не удалось распознать период.",
            reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
        )
        return

    track_user(update, "bank_history")

    await update.message.reply_text(
        f"Собираю историю по банку {selected_bank['name']} и считаю позицию среди рынка. "
        "Это может занять немного времени.",
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )

    await send_typing(update, context)

    try:
        message = build_single_bank_history_message(
            selected_bank=selected_bank,
            start=start_period,
            end=end_period,
        )
    except Exception as exc:
        logging.exception("Ошибка анализа истории по банку: %s", exc)
        message = (
            "Не удалось выполнить анализ по банку.\n\n"
            "Возможные причины: banks.kg временно не отвечает, изменился формат данных "
            "или выбран слишком большой период."
        )

    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)

    chat = update.effective_chat
    if not chat:
        return

    already_subscribed = is_user_subscribed(chat.id)

    add_subscriber(chat.id)
    sync_subscriber_update(update, True)
    track_user(update, "subscribe")

    if already_subscribed:
        text = "Вы уже подписаны на рассылку курсов."
    else:
        text = (
            "Готово, вы подписаны на рассылку курсов.\n\n"
            "Бот будет присылать курсы по рабочим дням по времени Бишкека: "
            "07:00, 09:00, 11:00, 13:00, 15:00, 17:00."
        )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(chat.id),
    )


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)

    chat = update.effective_chat
    if not chat:
        return

    remove_subscriber(chat.id)
    sync_subscriber_update(update, False)
    track_user(update, "unsubscribe")

    await update.message.reply_text(
        "Готово, вы отписаны от рассылки.",
        reply_markup=main_keyboard(chat.id),
    )


async def show_users_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)

    chat = update.effective_chat
    if not chat:
        return

    if chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text(
            "Не понял команду.\n\nВыберите действие кнопкой ниже или напишите /help.",
            reply_markup=main_keyboard(chat.id),
        )
        return

    track_user(update, "users")

    subscribers = get_effective_subscribers()

    text = (
        "👥 Пользователи\n\n"
        f"Подписчиков на рассылку: {len(subscribers)}\n\n"
        "Ваш статус:\n"
        f"chat_id: {chat.id}\n"
        f"рассылка: {'да' if chat.id in subscribers else 'нет'}"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_keyboard(chat.id),
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_users_to_admin(update, context)


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await rates(update, context)


async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await rates(update, context)


async def compare_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await rates(update, context)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    context.user_data.pop("selected_bank", None)
    track_user(update, "unknown_command")

    await update.message.reply_text(
        "Не понял команду.\n\nВыберите действие кнопкой ниже или напишите /help.",
        reply_markup=main_keyboard(update.effective_chat.id if update.effective_chat else None),
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text or ""
    chat_id = update.effective_chat.id if update.effective_chat else None

    if text == BTN_RATES:
        await rates(update, context)
        return

    if text == BTN_CALC:
        await calc_start(update, context)
        return

    if text == BTN_HISTORY:
        await history_start(update, context)
        return

    if text == BTN_BANK_HISTORY:
        await bank_history_start(update, context)
        return

    if text == BTN_SUBSCRIBE:
        await subscribe(update, context)
        return

    if text == BTN_UNSUBSCRIBE:
        await unsubscribe(update, context)
        return

    if text == BTN_HELP:
        await help_command(update, context)
        return

    if text == BTN_USERS:
        await show_users_to_admin(update, context)
        return

    mode = context.user_data.get("mode")

    if mode == "calc":
        await calc_amount_received(update, context)
        return

    if mode == "history":
        await history_period_received(update, context)
        return

    if mode == "bank_select":
        await bank_selected_received(update, context)
        return

    if mode == "bank_period":
        await bank_period_received(update, context)
        return

    await update.message.reply_text(
        "Не понял сообщение.\n\nВыберите действие кнопкой ниже или напишите /help.",
        reply_markup=main_keyboard(chat_id),
    )


# =========================
# АВТОМАТИЧЕСКАЯ РАССЫЛКА
# =========================

async def scheduled_rates_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    current = now_bishkek()

    if current.weekday() >= 5:
        return

    if current.hour not in SCHEDULE_HOURS_BISHKEK:
        return

    if current.minute > 4:
        return

    send_key = f"{current.strftime('%Y-%m-%d')}-{current.hour}"

    if send_key in SENT_NOTIFICATION_KEYS:
        return

    SENT_NOTIFICATION_KEYS.add(send_key)

    subscribers = get_effective_subscribers()
    if not subscribers:
        return

    rates_data = collect_current_rates()
    sync_rates_log(
        source_type="scheduled",
        bakai_rate=rates_data.get("bakai_rate"),
        aiyl_rate=rates_data.get("aiyl_rate"),
        nbkr_rate=rates_data.get("nbkr_rate"),
    )

    message = "🔔 Плановая рассылка курсов\n\n" + build_rates_message(rates_data)

    for chat_id in subscribers:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=main_keyboard(chat_id),
            )
        except Exception as exc:
            logging.exception("Не удалось отправить рассылку chat_id=%s: %s", chat_id, exc)


# =========================
# ЗАПУСК
# =========================

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rates", rates))
    application.add_handler(CommandHandler("calc", calc_start))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("users", users_command))

    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("compare", compare))
    application.add_handler(CommandHandler("compare_now", compare_now))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    if application.job_queue:
        application.job_queue.run_repeating(
            scheduled_rates_job,
            interval=60,
            first=10,
        )
    else:
        logging.warning(
            "Job queue недоступен. Проверь requirements.txt: "
            "python-telegram-bot[job-queue]==21.6"
        )

    logging.info("Бот запущен")
    application.run_polling()


if __name__ == "__main__":
    main()
