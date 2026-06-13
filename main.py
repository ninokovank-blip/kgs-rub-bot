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
# НАСТРОЙКИ
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
NBKR_TIMEOUT_SECONDS = 4
NBKR_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}

# =========================
# БАНКИ ДЛЯ ИСТОРИИ BANKS.KG
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

BTN_RATES = "⚖️ Бакай / A-bank сейчас"
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
# УТИЛИТЫ
# =========================

def now_bishkek() -> datetime:
    return datetime.now(BISHKEK_TZ)


def format_dt_bishkek(dt: Optional[datetime] = None) -> str:
    return (dt or now_bishkek()).strftime("%d.%m.%Y %H:%M:%S")


def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    text = text.replace("\xa0", " ")
    text = text.replace(" ", "")
    text = text.replace(",", ".")

    if not text:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", text)

    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def fmt_rate(value: Optional[float]) -> str:
    return "н/д" if value is None else f"{value:.4f}"


def fmt_pct(value: Optional[float]) -> str:
    return "н/д" if value is None else f"{value:.2f}%"


def fmt_money(value: Optional[float]) -> str:
    return "н/д" if value is None else f"{value:,.2f}".replace(",", " ")


async def send_typing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action="typing",
        )
    except Exception as exc:
        logging.warning("Не удалось отправить typing action: %s", exc)


# =========================
# GOOGLE SHEETS
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
            timeout=8,
        )

        if response.status_code != 200:
            logging.error(
                "Google Sheets HTTP error: %s %s",
                response.status_code,
                response.text[:300],
            )
            return None

        data = response.json()

    except Exception as exc:
        logging.warning("Google Sheets sync error: %s", exc)
        return None

    if not data.get("ok"):
        logging.warning("Google Sheets returned error: %s", data)

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


def sync_rates_log(
    source_type: str,
    bakai_rate: Optional[float],
    abank_rate: Optional[float],
    nbkr_rate: Optional[float],
) -> None:
    bank_rates = []

    if bakai_rate:
        bank_rates.append(("Бакай Банк", bakai_rate))

    if abank_rate:
        bank_rates.append(("АБанк", abank_rate))

    best_bank = ""
    bank_difference_abs = None
    bank_difference_pct = None

    if bank_rates:
        best_bank, best_rate = min(bank_rates, key=lambda x: x[1])

        if len(bank_rates) >= 2:
            rates_only = [x[1] for x in bank_rates]
            bank_difference_abs = max(rates_only) - min(rates_only)
            bank_difference_pct = bank_difference_abs / min(rates_only) * 100

    bakai_spread_abs = None
    bakai_spread_pct = None
    abank_spread_abs = None
    abank_spread_pct = None

    if nbkr_rate:
        if bakai_rate:
            bakai_spread_abs = bakai_rate - nbkr_rate
            bakai_spread_pct = (bakai_rate / nbkr_rate - 1) * 100

        if abank_rate:
            abank_spread_abs = abank_rate - nbkr_rate
            abank_spread_pct = (abank_rate / nbkr_rate - 1) * 100

    post_to_google({
        "event_type": "rates_log",
        "datetime_bishkek": format_dt_bishkek(),
        "source_type": source_type,
        "bakai_rate": bakai_rate or "",
        "aiyl_rate": abank_rate or "",
        "nbkr_rate": nbkr_rate or "",
        "best_bank": best_bank,
        "bank_difference_abs": bank_difference_abs or "",
        "bank_difference_pct": bank_difference_pct or "",
        "bakai_spread_abs": bakai_spread_abs or "",
        "bakai_spread_pct": bakai_spread_pct or "",
        "aiyl_spread_abs": abank_spread_abs or "",
        "aiyl_spread_pct": abank_spread_pct or "",
    })


def get_active_subscribers_from_google() -> Optional[List[int]]:
    data = post_to_google({
        "event_type": "get_active_subscribers",
    })

    if not data or not data.get("ok"):
        return None

    result = []

    for chat_id in data.get("active_chat_ids", []):
        try:
            result.append(int(chat_id))
        except Exception:
            continue

    return sorted(set(result))


# =========================
# ПОДПИСКИ
# =========================

def load_subscribers() -> List[int]:
    if not os.path.exists(SUBSCRIBERS_FILE):
        return []

    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        return sorted({int(x) for x in data})

    except Exception:
        return []


def save_subscribers(subscribers: List[int]) -> None:
    try:
        with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as file:
            json.dump(
                sorted(set(subscribers)),
                file,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as exc:
        logging.warning("Не удалось сохранить subscribers.json: %s", exc)


def add_subscriber(chat_id: int) -> None:
    subscribers = load_subscribers()

    if chat_id not in subscribers:
        subscribers.append(chat_id)
        save_subscribers(subscribers)


def remove_subscriber(chat_id: int) -> None:
    save_subscribers([
        x for x in load_subscribers()
        if x != chat_id
    ])


def get_effective_subscribers() -> List[int]:
    google_subscribers = get_active_subscribers_from_google()

    if google_subscribers is not None:
        return google_subscribers

    return load_subscribers()


def is_user_subscribed(chat_id: int) -> bool:
    return chat_id in get_effective_subscribers()


def is_user_subscribed_for_tracking(chat_id: int) -> bool:
    # Для трекинга не ходим в Google, чтобы не тормозить команды.
    return chat_id in load_subscribers()


def track_user(update: Update, action: str) -> None:
    chat = update.effective_chat

    if not chat:
        return

    sync_user_activity(
        update,
        action,
        is_user_subscribed_for_tracking(chat.id),
    )


# =========================
# НБКР
# =========================

def parse_nbkr_report_date(root: ET.Element) -> Optional[datetime]:
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
    if date_obj is None:
        url = "https://www.nbkr.kg/XML/daily.xml"
    else:
        url = f"https://www.nbkr.kg/XML/daily.xml?date_req={date_obj.strftime('%d.%m.%Y')}"

    try:
        response = requests.get(
            url,
            timeout=NBKR_TIMEOUT_SECONDS,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/xml,text/xml,*/*",
            },
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)

    except Exception as exc:
        logging.warning(
            "НБКР не ответил / не разобрался по дате %s: %s",
            date_obj,
            exc,
        )
        return None, "НБКР: не удалось получить данные", None

    actual_date = parse_nbkr_report_date(root)

    for currency in root.findall(".//Currency"):
        if currency.attrib.get("ISOCode") == "RUB":
            nominal = parse_float(currency.findtext("Nominal")) or 1
            value = parse_float(currency.findtext("Value"))

            if value is None:
                return None, "НБКР: курс RUB не найден", actual_date

            rate = value / nominal

            if actual_date:
                return (
                    rate,
                    f"НБКР: получен с официального сайта НБКР, дата курса: {actual_date.strftime('%d.%m.%Y')}",
                    actual_date,
                )

            return rate, "НБКР: получен с официального сайта НБКР", actual_date

    return None, "НБКР: курс RUB не найден", actual_date


def get_nbkr_rate_for_date(date_obj: Optional[datetime] = None) -> Tuple[Optional[float], str]:
    rate, source, _actual_date = get_nbkr_rate_with_actual_date(date_obj)
    return rate, source


def fetch_recent_nbkr_rates_from_investfunds() -> Dict[Any, Dict[str, Any]]:
    """
    Резервный источник для последних дат RUB/KGS.
    Основной источник НБКР остаётся официальный сайт НБКР.
    """
    url = "https://investfunds.ru/indexes/RUB-KGS/"

    try:
        response = requests.get(
            url,
            timeout=8,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,*/*",
            },
        )
        response.raise_for_status()
        html = response.text

    except Exception as exc:
        logging.warning("Investfunds fallback не ответил: %s", exc)
        return {}

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    archive_match = re.search(
        r"Архив значений\s+Дата\s+Значение\s+(.*?)\s+Проверьте корректность ввода дат",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    if not archive_match:
        return {}

    archive_text = archive_match.group(1)

    dates = re.findall(r"\b\d{2}\.\d{2}\.\d{4}\b", archive_text)
    values = re.findall(r"\b\d+[.,]\d{4}\b", archive_text)

    if not dates or not values:
        return {}

    values = values[-len(dates):]

    result = {}

    for date_text, value_text in zip(dates, values):
        try:
            dt = datetime.strptime(date_text, "%d.%m.%Y").replace(tzinfo=BISHKEK_TZ)
        except Exception:
            continue

        rate = parse_float(value_text)

        if not rate:
            continue

        result[dt.date()] = {
            "rate": rate,
            "actual_date": dt.date(),
            "source": "investfunds_fallback",
        }

    return result


def get_nbkr_rates_for_bank_point_dates(
    point_dates: List[Any],
) -> Tuple[Dict[Any, Dict[str, Any]], bool]:
    """
    Корректная логика для истории:
    НБКР загружается только по датам, где реально есть банковские точки курса.

    Если официальный НБКР на историческую дату вернул текущую дату,
    такой курс НЕ используется для прошлого периода.
    """
    result: Dict[Any, Dict[str, Any]] = {}
    is_partial = False

    unique_dates = sorted(set(point_dates))
    investfunds_cache: Optional[Dict[Any, Dict[str, Any]]] = None

    for target_date in unique_dates:
        cache_key = (
            target_date.strftime("%Y-%m-%d")
            if hasattr(target_date, "strftime")
            else str(target_date)
        )

        if cache_key in NBKR_CACHE:
            cached = NBKR_CACHE[cache_key]

            if cached:
                result[target_date] = cached
            else:
                is_partial = True

            continue

        target_dt = datetime.combine(
            target_date,
            datetime.min.time(),
        ).replace(tzinfo=BISHKEK_TZ)

        rate, _source, actual_date = get_nbkr_rate_with_actual_date(target_dt)

        valid_official = False

        if rate and actual_date:
            actual = actual_date.date()

            if actual == target_date:
                valid_official = True
            elif actual < target_date and (target_date - actual).days <= 7:
                # Для выходных/праздников допустим ближайший предыдущий курс НБКР.
                valid_official = True
            else:
                logging.warning(
                    "НБКР вернул неподходящую дату: requested=%s, actual=%s",
                    target_date,
                    actual,
                )

        elif rate and actual_date is None:
            valid_official = True

        if rate and valid_official:
            item = {
                "rate": rate,
                "actual_date": actual_date.date() if actual_date else target_date,
                "source": "nbkr_official",
            }

            NBKR_CACHE[cache_key] = item
            result[target_date] = item

            continue

        if investfunds_cache is None:
            investfunds_cache = fetch_recent_nbkr_rates_from_investfunds()

        fallback_item = investfunds_cache.get(target_date) if investfunds_cache else None

        if fallback_item:
            NBKR_CACHE[cache_key] = fallback_item
            result[target_date] = fallback_item
            is_partial = True
        else:
            NBKR_CACHE[cache_key] = None
            is_partial = True

    return result, is_partial


# =========================
# BANKS.KG ДЛЯ ИСТОРИИ
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
            return datetime.strptime(text[:26], fmt).replace(tzinfo=BISHKEK_TZ)
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
        response = requests.get(
            url,
            headers=headers,
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

    except Exception as exc:
        logging.warning(
            "Ошибка banks.kg для organization_id=%s: %s",
            organization_id,
            exc,
        )
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


# =========================
# ОФИЦИАЛЬНЫЕ САЙТЫ ДЛЯ БЫСТРОГО СРАВНЕНИЯ
# =========================

def get_bakai_rate() -> Tuple[Optional[float], str]:
    """
    Текущий курс Бакай Банка берём с официального сайта.
    Нужен RUB / non_cash / sell — безналичная продажа RUB.
    """
    url = "https://bakai.kg/"

    try:
        response = requests.get(
            url,
            timeout=12,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html,*/*",
            },
        )
        response.raise_for_status()
        html = response.text

    except Exception as exc:
        logging.warning("Ошибка получения курса Бакай с официального сайта: %s", exc)
        return None, "Бакай Банк: не удалось получить данные с официального сайта"

    patterns = [
        r'"RUB".{0,2500}?"non_cash".{0,1200}?"sell"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'"rub".{0,2500}?"non_cash".{0,1200}?"sell"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)"?',
        r'RUB.{0,2500}?non_cash.{0,1200}?sell.{0,80}?([0-9]+(?:[.,][0-9]+)?)',
    ]

    rate = None

    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)

        if match:
            rate = parse_float(match.group(1))

            if rate:
                break

    if rate is None:
        return None, "Бакай Банк: курс RUB / безналичная продажа не найден на официальном сайте"

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

    source = "Бакай Банк: получен с официального сайта, тип курса: RUB / безналичная продажа"

    if date_text:
        source += f", дата обновления на сайте: {date_text}"

    return rate, source


def get_abank_rate() -> Tuple[Optional[float], str]:
    """
    Текущий курс A-bank берём с официального сайта АБанк.
    Нужен RUB / безналичная продажа.
    """
    urls = [
        "https://abank.kg/ru",
        "https://abank.kg/ky",
        "https://www.abank.kg/",
    ]

    last_error = None

    for url in urls:
        try:
            response = requests.get(
                url,
                timeout=12,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,*/*",
                },
            )
            response.raise_for_status()
            html = response.text

        except Exception as exc:
            last_error = exc
            continue

        try:
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text)
            text = text.replace("\xa0", " ")

            rows = []

            for match in re.finditer(
                r"\bRUB\b\s+([0-9]+(?:[.,][0-9]+)?)\s+([0-9]+(?:[.,][0-9]+)?)",
                text,
                re.IGNORECASE,
            ):
                buy_rate = parse_float(match.group(1))
                sell_rate = parse_float(match.group(2))

                if buy_rate is not None and sell_rate is not None:
                    rows.append((buy_rate, sell_rate))

            if not rows:
                rub_positions = [
                    m.start()
                    for m in re.finditer(r"\bRUB\b|Руб", text, re.IGNORECASE)
                ]

                for pos in rub_positions:
                    chunk = text[pos:pos + 250]
                    nums = re.findall(r"\d+[.,]\d+", chunk)
                    parsed_nums = [parse_float(x) for x in nums]
                    parsed_nums = [x for x in parsed_nums if x is not None]

                    if len(parsed_nums) >= 2:
                        rows.append((parsed_nums[0], parsed_nums[1]))

            if not rows:
                continue

            # Обычно структура сайта:
            # 1) наличные;
            # 2) безналичные;
            # 3) НБКР.
            if len(rows) >= 2:
                selected_buy, selected_sell = rows[1]
            else:
                selected_buy, selected_sell = rows[0]

            # Защита от выбора неверного блока около 1.06.
            if selected_sell < 1.10 and len(rows) >= 2:
                selected_buy, selected_sell = max(rows[:2], key=lambda x: x[1])

            date_patterns = re.findall(r"\b\d{2}\.\d{2}\.\d{4}\b", text)

            if len(date_patterns) >= 2:
                date_text = date_patterns[1]
            elif date_patterns:
                date_text = date_patterns[0]
            else:
                date_text = ""

            source = "АБанк: получен с официального сайта, тип курса: безналичная продажа RUB"

            if date_text:
                source += f", дата на сайте: {date_text}"

            return selected_sell, source

        except Exception as exc:
            last_error = exc
            continue

    logging.warning("Ошибка получения/разбора курса A-bank: %s", last_error)

    return None, "АБанк: не удалось получить курс безналичной продажи RUB с официального сайта"


def collect_current_rates() -> Dict[str, Any]:
    # Для быстрого сравнения Бакай / A-bank принципиально используем официальные сайты банков.
    # banks.kg используется только для исторического рыночного анализа.
    bakai_rate, bakai_source = get_bakai_rate()
    abank_rate, abank_source = get_abank_rate()
    nbkr_rate, nbkr_source = get_nbkr_rate_for_date()

    return {
        "bakai_rate": bakai_rate,
        "bakai_source": bakai_source,
        "aiyl_rate": abank_rate,
        "aiyl_source": abank_source,
        "nbkr_rate": nbkr_rate,
        "nbkr_source": nbkr_source,
    }


def build_rates_message(rates_data: Dict[str, Any]) -> str:
    bakai_rate = rates_data.get("bakai_rate")
    abank_rate = rates_data.get("aiyl_rate")
    nbkr_rate = rates_data.get("nbkr_rate")

    lines = [
        "⚖️ Бакай / A-bank сейчас",
        "Тип курса: безналичная продажа RUB",
        "",
        f"Бакай Банк: {fmt_rate(bakai_rate)}",
        f"АБанк: {fmt_rate(abank_rate)}",
        f"НБКР: {fmt_rate(nbkr_rate)}",
        "",
    ]

    available = []

    if bakai_rate:
        available.append(("Бакай Банк", bakai_rate))

    if abank_rate:
        available.append(("АБанк", abank_rate))

    if available:
        best_bank, best_rate = min(available, key=lambda x: x[1])
        lines.append(f"Лучший курс сейчас: {best_bank} — {fmt_rate(best_rate)}")
        lines.append("")

    if nbkr_rate:
        lines.append("Спред к НБКР:")

        if bakai_rate:
            lines.append(
                f"Бакай Банк: {fmt_pct((bakai_rate / nbkr_rate - 1) * 100)}"
            )

        if abank_rate:
            lines.append(
                f"АБанк: {fmt_pct((abank_rate / nbkr_rate - 1) * 100)}"
            )

        lines.append("")

    lines.append("Источники:")
    lines.append(f"• {rates_data.get('bakai_source')}")
    lines.append(f"• {rates_data.get('aiyl_source')}")
    lines.append(f"• {rates_data.get('nbkr_source')}")
    lines.append("")
    lines.append(
        "Комментарий: это быстрое текущее сравнение двух банков. "
        "Для широкого рынка используйте «📈 История курсов»."
    )

    return "\n".join(lines)


# =========================
# ИСТОРИЧЕСКИЙ АНАЛИЗ
# =========================

def parse_history_period(text: str) -> Tuple[Optional[datetime], Optional[datetime], Optional[str]]:
    normalized = text.strip().lower()
    normalized = normalized.replace("—", "-").replace("–", "-")

    today = now_bishkek().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )

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
            dt = datetime.strptime(dates[0], "%d.%m.%Y").replace(tzinfo=BISHKEK_TZ)
            return dt, dt, None
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
        "a-bank": "АБанк",
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
    lines = [
        "Выберите банк для анализа.",
        "",
        "Можно написать номер или название банка.",
        "",
    ]

    for bank in HISTORY_BANKS:
        lines.append(f"{bank['id']} — {bank['name']}")

    lines.extend([
        "",
        "Примеры:",
        "5",
        "MBANK",
        "Бакай",
        "АБанк",
        "КИКБ",
    ])

    return "\n".join(lines)


def filter_sell_points_for_period(
    sell_points: List[Dict[str, Any]],
    start: datetime,
    end: datetime,
) -> List[Dict[str, Any]]:
    return [
        point for point in sell_points
        if start.date() <= point["date"] <= end.date()
    ]


def collect_dates_from_filtered_points(
    filtered_points_by_bank: Dict[int, List[Dict[str, Any]]],
) -> List[Any]:
    dates = set()

    for points in filtered_points_by_bank.values():
        for point in points:
            dates.add(point["date"])

    return sorted(dates)


def calculate_bank_history_summary(
    bank_name: str,
    organization_id: int,
    sell_points: List[Dict[str, Any]],
    nbkr_rates_by_date: Dict[Any, Dict[str, Any]],
    start: datetime,
    end: datetime,
) -> Optional[Dict[str, Any]]:
    filtered = filter_sell_points_for_period(
        sell_points,
        start,
        end,
    )

    if not filtered:
        return None

    rates = [point["rate"] for point in filtered]
    spreads = []
    nbkr_rates_used = []
    nbkr_dates_used = set()
    negative_spread_points = 0
    points_without_nbkr = 0

    for point in filtered:
        nbkr_item = nbkr_rates_by_date.get(point["date"])

        if not nbkr_item or not nbkr_item.get("rate"):
            points_without_nbkr += 1
            continue

        nbkr_rate = nbkr_item["rate"]
        nbkr_rates_used.append(nbkr_rate)
        nbkr_dates_used.add(point["date"])

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
        "points_without_nbkr": points_without_nbkr,
        "nbkr_dates_count": len(nbkr_dates_used),
        "bank_dates_count": len(set(point["date"] for point in filtered)),
        "rate_points_count": len(filtered),
    }


def build_top_lines(
    items: List[Dict[str, Any]],
    key: str,
    reverse: bool = False,
    value_formatter=fmt_rate,
    limit: int = 5,
) -> List[str]:
    valid = [
        item for item in items
        if item.get(key) is not None
    ]

    sorted_items = sorted(
        valid,
        key=lambda x: x[key],
        reverse=reverse,
    )

    return [
        f"{index}. {item['bank_name']} — {value_formatter(item[key])}"
        for index, item in enumerate(sorted_items[:limit], start=1)
    ]


def get_market_summaries(
    start: datetime,
    end: datetime,
) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    summaries = []
    no_data_banks = []
    filtered_points_by_bank: Dict[int, List[Dict[str, Any]]] = {}
    raw_points_by_bank: Dict[int, List[Dict[str, Any]]] = {}

    for bank in HISTORY_BANKS:
        history = fetch_bank_history_from_bankskg(bank["id"])
        sell_points = history.get("sell", [])
        filtered_points = filter_sell_points_for_period(
            sell_points,
            start,
            end,
        )

        raw_points_by_bank[bank["id"]] = sell_points
        filtered_points_by_bank[bank["id"]] = filtered_points

    # Важно: НБКР берём только по датам, где реально есть банковские курсы.
    point_dates = collect_dates_from_filtered_points(filtered_points_by_bank)
    nbkr_rates_by_date, nbkr_partial = get_nbkr_rates_for_bank_point_dates(point_dates)

    for bank in HISTORY_BANKS:
        summary = calculate_bank_history_summary(
            bank_name=bank["name"],
            organization_id=bank["id"],
            sell_points=raw_points_by_bank.get(bank["id"], []),
            nbkr_rates_by_date=nbkr_rates_by_date,
            start=start,
            end=end,
        )

        if summary:
            summaries.append(summary)
        else:
            no_data_banks.append(bank["name"])

    return summaries, no_data_banks, nbkr_partial


def build_history_message(
    start: datetime,
    end: datetime,
    summaries: List[Dict[str, Any]],
    no_data_banks: List[str],
    nbkr_partial: bool,
) -> str:
    period_text = (
        format_date(start)
        if start.date() == end.date()
        else f"{format_date(start)}–{format_date(end)}"
    )

    lines = [
        f"📈 История RUB / KGS за {period_text}",
        "Тип курса: безналичная продажа RUB",
        "",
    ]

    if not summaries:
        return "\n".join(
            lines + [
                "За выбранный период данные по банкам не найдены.",
            ]
        )

    lines.append("Лучший средний курс:")
    lines.extend(
        build_top_lines(
            summaries,
            "avg_sell_rate",
            reverse=False,
            value_formatter=fmt_rate,
            limit=5,
        )
    )
    lines.append("")

    lines.append("Минимальный курс за период:")
    lines.extend(
        build_top_lines(
            summaries,
            "min_sell_rate",
            reverse=False,
            value_formatter=fmt_rate,
            limit=5,
        )
    )
    lines.append("")

    lines.append("Максимальный курс за период:")
    lines.extend(
        build_top_lines(
            summaries,
            "max_sell_rate",
            reverse=True,
            value_formatter=fmt_rate,
            limit=5,
        )
    )
    lines.append("")

    nbkr_values = [
        item.get("avg_nbkr_rate")
        for item in summaries
        if item.get("avg_nbkr_rate") is not None
    ]

    if nbkr_values:
        lines.append("НБКР по датам банковских курсов:")
        lines.append(f"средний: {fmt_rate(mean(nbkr_values))}")
        lines.append(f"минимум: {fmt_rate(min(nbkr_values))}")
        lines.append(f"максимум: {fmt_rate(max(nbkr_values))}")

        if nbkr_partial:
            lines.append(
                "часть дат НБКР не загрузилась, "
                "спред рассчитан только по доступным датам"
            )

        lines.append("")

    else:
        lines.extend([
            "НБКР по датам банковских курсов: н/д",
            "Спред к НБКР: н/д",
            "",
        ])

    lines.append("Средний спред к НБКР:")
    spread_lines = build_top_lines(
        summaries,
        "avg_spread_pct",
        reverse=False,
        value_formatter=fmt_pct,
        limit=5,
    )
    lines.extend(spread_lines if spread_lines else ["н/д"])
    lines.append("")

    sorted_by_avg = sorted(
        summaries,
        key=lambda x: x["avg_sell_rate"],
    )

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
        lines.extend([
            f"• {bank}"
            for bank in no_data_banks
        ])
        lines.append("")

    best_avg = min(summaries, key=lambda x: x["avg_sell_rate"])
    best_min = min(summaries, key=lambda x: x["min_sell_rate"])
    max_rate = max(summaries, key=lambda x: x["max_sell_rate"])

    spread_candidates = [
        x for x in summaries
        if x.get("avg_spread_pct") is not None
    ]

    best_spread = (
        min(spread_candidates, key=lambda x: x["avg_spread_pct"])
        if spread_candidates
        else None
    )

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

    elif nbkr_partial:
        comment += (
            " Спред к НБКР рассчитан частично или не рассчитан "
            "из-за недоступности НБКР."
        )

    comment += " Данные приведены для анализа рынка."
    lines.append(comment)

    message = "\n".join(lines)

    if len(message) <= 3900:
        return message

    short = [
        f"📈 История RUB / KGS за {period_text}",
        "Тип курса: безналичная продажа RUB",
        "",
        "Лучший средний курс:",
    ]

    short.extend(
        build_top_lines(
            summaries,
            "avg_sell_rate",
            reverse=False,
            value_formatter=fmt_rate,
            limit=5,
        )
    )
    short.append("")

    short.append("Минимальный курс за период:")
    short.extend(
        build_top_lines(
            summaries,
            "min_sell_rate",
            reverse=False,
            value_formatter=fmt_rate,
            limit=5,
        )
    )
    short.append("")

    short.append("Максимальный курс за период:")
    short.extend(
        build_top_lines(
            summaries,
            "max_sell_rate",
            reverse=True,
            value_formatter=fmt_rate,
            limit=5,
        )
    )
    short.append("")

    if nbkr_values:
        short.extend([
            "НБКР по датам банковских курсов:",
            f"средний: {fmt_rate(mean(nbkr_values))}",
            f"минимум: {fmt_rate(min(nbkr_values))}",
            f"максимум: {fmt_rate(max(nbkr_values))}",
            "",
        ])
    else:
        short.extend([
            "НБКР по датам банковских курсов: н/д",
            "",
        ])

    short.append("Средний спред к НБКР:")
    short.extend(spread_lines if spread_lines else ["н/д"])
    short.append("")
    short.append(comment)

    return "\n".join(short)


def get_history_analysis(start: datetime, end: datetime) -> str:
    summaries, no_data_banks, nbkr_partial = get_market_summaries(start, end)

    return build_history_message(
        start,
        end,
        summaries,
        no_data_banks,
        nbkr_partial,
    )


def build_single_bank_history_message(
    selected_bank: Dict[str, Any],
    start: datetime,
    end: datetime,
) -> str:
    market_summaries, _no_data_banks, nbkr_partial = get_market_summaries(
        start,
        end,
    )

    selected_summary = next(
        (
            item for item in market_summaries
            if item["organization_id"] == selected_bank["id"]
        ),
        None,
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

    def get_rank(
        items: List[Dict[str, Any]],
        key: str,
        bank_name: str,
        reverse: bool = False,
    ) -> Optional[int]:
        valid = [
            item for item in items
            if item.get(key) is not None
        ]

        sorted_items = sorted(
            valid,
            key=lambda x: x[key],
            reverse=reverse,
        )

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

    lines = [
        f"🏦 {selected_bank['name']}",
        f"История RUB / KGS за {period_text}",
        "Тип курса: безналичная продажа RUB",
        "",
        "Показатели банка:",
        f"средний курс: {fmt_rate(selected_summary['avg_sell_rate'])}",
        f"минимум: {fmt_rate(selected_summary['min_sell_rate'])}",
        f"максимум: {fmt_rate(selected_summary['max_sell_rate'])}",
        f"последний: {fmt_rate(selected_summary['last_sell_rate'])}",
        f"изменений курса: {selected_summary['rate_points_count']}",
        "",
        "НБКР по датам банковских курсов:",
        f"средний: {fmt_rate(selected_summary['avg_nbkr_rate'])}",
        f"минимум: {fmt_rate(selected_summary['min_nbkr_rate'])}",
        f"максимум: {fmt_rate(selected_summary['max_nbkr_rate'])}",
        (
            f"дат банка: {selected_summary.get('bank_dates_count', 0)} / "
            f"дат НБКР использовано: {selected_summary.get('nbkr_dates_count', 0)}"
        ),
    ]

    if selected_summary.get("points_without_nbkr", 0) > 0:
        lines.append(
            f"точек банка без НБКР: {selected_summary['points_without_nbkr']}"
        )

    if nbkr_partial:
        lines.append(
            "часть дат НБКР не загрузилась, "
            "спред рассчитан только по доступным датам"
        )

    lines.append("")

    lines.extend([
        "Спред к НБКР:",
        f"средний: {fmt_pct(selected_summary['avg_spread_pct'])}",
        f"минимальный: {fmt_pct(selected_summary['min_spread_pct'])}",
        f"максимальный: {fmt_pct(selected_summary['max_spread_pct'])}",
    ])

    if selected_summary.get("negative_spread_points", 0) > 0:
        lines.append(
            f"аномалий ниже НБКР: {selected_summary['negative_spread_points']}"
        )

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
        comment += (
            " Есть точки ниже НБКР, их нужно проверить как возможную "
            "аномалию источника или сопоставления дат."
        )

    if nbkr_partial:
        comment += (
            " НБКР был доступен не по всем датам, "
            "поэтому спред мог быть рассчитан частично."
        )

    comment += " Данные приведены для анализа рынка."
    lines.append(comment)

    return "\n".join(lines)


# =========================
# КОМАНДЫ
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()
    track_user(update, "start")

    chat_id = update.effective_chat.id if update.effective_chat else None

    await update.message.reply_text(
        "Привет! Я бот для мониторинга RUB / KGS.\n\n"
        "Я показываю быстрое сравнение Бакай / A-bank, считаю спред к НБКР, "
        "помогаю рассчитать сумму для конвертации и анализирую историю курсов по банкам.\n\n"
        "Выберите действие кнопкой ниже.",
        reply_markup=main_keyboard(chat_id),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()
    track_user(update, "help")

    chat_id = update.effective_chat.id if update.effective_chat else None

    await update.message.reply_text(
        "❓ Помощь\n\n"
        "⚖️ Бакай / A-bank сейчас\n"
        "Показывает быстрое текущее сравнение безналичной продажи RUB "
        "по официальным сайтам Бакай Банка и A-bank, а также спред к НБКР.\n\n"
        "🧮 Калькулятор\n"
        "Введите сумму RUB, которую нужно купить. Бот рассчитает, сколько KGS "
        "потребуется по текущим курсам Бакай / A-bank.\n\n"
        "📈 История курсов\n"
        "Показывает историю безналичной продажи RUB по банкам за выбранную дату "
        "или период. Банковская история берётся с banks.kg.\n\n"
        "🏦 История по банку\n"
        "Позволяет выбрать конкретный банк и посмотреть его исторический курс. "
        "НБКР для спреда берётся по датам, где у банка реально были значения курса.\n\n"
        "Примеры периода:\n"
        "12.06.2026\n"
        "01.06.2026-12.06.2026\n"
        "последние 7 дней\n"
        "последние 30 дней\n\n"
        "🔔 Рассылка приходит по рабочим дням по времени Бишкека: "
        "07:00, 09:00, 11:00, 13:00, 15:00, 17:00.\n\n"
        "Важно: исторические данные используются для анализа рынка. "
        "Если НБКР временно не отвечает, спред может быть рассчитан частично.",
        reply_markup=main_keyboard(chat_id),
    )


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()
    track_user(update, "rates")

    rates_data = collect_current_rates()

    sync_rates_log(
        "manual",
        rates_data.get("bakai_rate"),
        rates_data.get("aiyl_rate"),
        rates_data.get("nbkr_rate"),
    )

    await update.message.reply_text(
        build_rates_message(rates_data),
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()
    context.user_data["mode"] = "calc"
    track_user(update, "calc_start")

    await update.message.reply_text(
        "Введите сумму RUB, которую нужно купить.\n\n"
        "Например:\n"
        "1000000",
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def calc_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    amount = parse_float(update.message.text or "")

    if not amount or amount <= 0:
        await update.message.reply_text(
            "Не удалось распознать сумму. Введите число, например: 1000000",
            reply_markup=main_keyboard(
                update.effective_chat.id
                if update.effective_chat
                else None
            ),
        )
        return

    track_user(update, "calc_amount")

    rates_data = collect_current_rates()

    lines = [
        "🧮 Расчёт покупки RUB",
        f"Сумма: {fmt_money(amount)} RUB",
        "",
    ]

    bank_rates = []

    if rates_data.get("bakai_rate"):
        bank_rates.append(("Бакай Банк", rates_data["bakai_rate"]))

    if rates_data.get("aiyl_rate"):
        bank_rates.append(("АБанк", rates_data["aiyl_rate"]))

    if not bank_rates:
        lines.append("Не удалось получить доступные курсы банков.")
    else:
        for bank_name, rate in bank_rates:
            lines.extend([
                bank_name + ":",
                f"курс: {fmt_rate(rate)}",
                f"потребуется: {fmt_money(amount * rate)} KGS",
                "",
            ])

        best_bank, best_rate = min(bank_rates, key=lambda x: x[1])
        lines.append(f"Лучший вариант: {best_bank} — {fmt_rate(best_rate)}")

    if rates_data.get("nbkr_rate"):
        lines.extend([
            "",
            f"Ориентир НБКР: {fmt_rate(rates_data.get('nbkr_rate'))}",
        ])

    context.user_data.clear()

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()
    context.user_data["mode"] = "history"
    track_user(update, "history_start")

    await update.message.reply_text(
        "Введите дату или период для анализа.\n\n"
        "Примеры:\n"
        "12.06.2026\n"
        "01.06.2026-12.06.2026\n"
        "последние 7 дней\n"
        "последние 30 дней\n\n"
        f"Максимальный период: {MAX_HISTORY_DAYS} дней.",
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def history_period_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    start_period, end_period, error = parse_history_period(update.message.text or "")

    if error:
        await update.message.reply_text(
            error,
            reply_markup=main_keyboard(
                update.effective_chat.id
                if update.effective_chat
                else None
            ),
        )
        return

    track_user(update, "history")

    await update.message.reply_text(
        "Собираю исторические данные по банкам и считаю спред к НБКР. "
        "Это может занять немного времени.",
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )

    await send_typing(update, context)

    try:
        message = get_history_analysis(start_period, end_period)
    except Exception as exc:
        logging.exception("Ошибка исторического анализа: %s", exc)
        message = (
            "Не удалось выполнить исторический анализ. Возможные причины: "
            "banks.kg временно не отвечает или изменился формат данных."
        )

    context.user_data.clear()

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def bank_history_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()
    context.user_data["mode"] = "bank_select"
    track_user(update, "bank_history_start")

    await update.message.reply_text(
        build_bank_selection_message(),
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def bank_selected_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    bank = find_history_bank(update.message.text or "")

    if not bank:
        await update.message.reply_text(
            "Не удалось найти банк.\n\n"
            "Напишите номер или название банка, например:\n"
            "5\n"
            "MBANK\n"
            "Бакай\n"
            "АБанк\n"
            "КИКБ",
            reply_markup=main_keyboard(
                update.effective_chat.id
                if update.effective_chat
                else None
            ),
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
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def bank_period_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)

    selected_bank = context.user_data.get("selected_bank")

    if not selected_bank:
        context.user_data["mode"] = "bank_select"

        await update.message.reply_text(
            "Сначала выберите банк.",
            reply_markup=main_keyboard(
                update.effective_chat.id
                if update.effective_chat
                else None
            ),
        )
        return

    start_period, end_period, error = parse_history_period(update.message.text or "")

    if error:
        await update.message.reply_text(
            error,
            reply_markup=main_keyboard(
                update.effective_chat.id
                if update.effective_chat
                else None
            ),
        )
        return

    track_user(update, "bank_history")

    await update.message.reply_text(
        f"Собираю историю по банку {selected_bank['name']} и считаю позицию среди рынка. "
        "Это может занять немного времени.",
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )

    await send_typing(update, context)

    try:
        message = build_single_bank_history_message(
            selected_bank,
            start_period,
            end_period,
        )
    except Exception as exc:
        logging.exception("Ошибка анализа истории по банку: %s", exc)
        message = (
            "Не удалось выполнить анализ по банку. Возможные причины: "
            "banks.kg временно не отвечает или изменился формат данных."
        )

    context.user_data.clear()

    await update.message.reply_text(
        message,
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_typing(update, context)
    context.user_data.clear()

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
    context.user_data.clear()

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
    context.user_data.clear()

    chat = update.effective_chat

    if not chat:
        return

    if chat.id != ADMIN_CHAT_ID:
        await update.message.reply_text(
            "Не понял команду.\n\n"
            "Выберите действие кнопкой ниже или напишите /help.",
            reply_markup=main_keyboard(chat.id),
        )
        return

    track_user(update, "users")

    subscribers = get_effective_subscribers()

    await update.message.reply_text(
        "👥 Пользователи\n\n"
        f"Подписчиков на рассылку: {len(subscribers)}\n\n"
        "Ваш статус:\n"
        f"chat_id: {chat.id}\n"
        f"рассылка: {'да' if chat.id in subscribers else 'нет'}",
        reply_markup=main_keyboard(chat.id),
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    track_user(update, "unknown_command")

    await update.message.reply_text(
        "Не понял команду.\n\n"
        "Выберите действие кнопкой ниже или напишите /help.",
        reply_markup=main_keyboard(
            update.effective_chat.id
            if update.effective_chat
            else None
        ),
    )


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text or ""

    if text == BTN_RATES:
        await rates(update, context)

    elif text == BTN_CALC:
        await calc_start(update, context)

    elif text == BTN_HISTORY:
        await history_start(update, context)

    elif text == BTN_BANK_HISTORY:
        await bank_history_start(update, context)

    elif text == BTN_SUBSCRIBE:
        await subscribe(update, context)

    elif text == BTN_UNSUBSCRIBE:
        await unsubscribe(update, context)

    elif text == BTN_HELP:
        await help_command(update, context)

    elif text == BTN_USERS:
        await show_users_to_admin(update, context)

    elif context.user_data.get("mode") == "calc":
        await calc_amount_received(update, context)

    elif context.user_data.get("mode") == "history":
        await history_period_received(update, context)

    elif context.user_data.get("mode") == "bank_select":
        await bank_selected_received(update, context)

    elif context.user_data.get("mode") == "bank_period":
        await bank_period_received(update, context)

    else:
        await update.message.reply_text(
            "Не понял сообщение.\n\n"
            "Выберите действие кнопкой ниже или напишите /help.",
            reply_markup=main_keyboard(
                update.effective_chat.id
                if update.effective_chat
                else None
            ),
        )


# =========================
# РАССЫЛКА
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
        "scheduled",
        rates_data.get("bakai_rate"),
        rates_data.get("aiyl_rate"),
        rates_data.get("nbkr_rate"),
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
            logging.warning(
                "Не удалось отправить рассылку chat_id=%s: %s",
                chat_id,
                exc,
            )


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
    application.add_handler(CommandHandler("users", show_users_to_admin))
    application.add_handler(CommandHandler("buy", rates))
    application.add_handler(CommandHandler("compare", rates))
    application.add_handler(CommandHandler("compare_now", rates))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            unknown_command,
        )
    )

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
