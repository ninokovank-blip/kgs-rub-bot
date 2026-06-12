import os
import re
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


BISHKEK_TZ = ZoneInfo("Asia/Bishkek")
WAITING_FOR_RUB_AMOUNT = 1

NBKR_DAILY_XML_URL = "https://www.nbkr.kg/XML/daily.xml"
AIYL_BANK_URL = "https://abank.kg/ky"
BAKAI_BANK_URL = "https://bakai.kg/"

DEFAULT_BAKAI_FALLBACK = 1.2450
DEFAULT_AIYL_FALLBACK = 1.2300
DEFAULT_NBKR_FALLBACK = 1.2135

SUBSCRIBERS_FILE = "subscribers.json"
USERS_FILE = "users.json"

ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "439636200"))

GOOGLE_APPS_SCRIPT_URL = os.getenv("GOOGLE_APPS_SCRIPT_URL", "")
GOOGLE_APPS_SCRIPT_SECRET = os.getenv("GOOGLE_APPS_SCRIPT_SECRET", "kgs_rub_bot_secret_2026")

NOTIFICATION_HOURS = {7, 9, 11, 13, 15, 17}
SENT_NOTIFICATION_KEYS = set()


def is_admin_chat_id(chat_id: int | None) -> bool:
    return chat_id == ADMIN_CHAT_ID


def main_keyboard(chat_id: int | None = None) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📊 Курсы сейчас"), KeyboardButton("🧮 Калькулятор")],
        [KeyboardButton("🔔 Подписаться на рассылку"), KeyboardButton("🔕 Отписаться")],
    ]

    if is_admin_chat_id(chat_id):
        keyboard.append([KeyboardButton("👥 Пользователи"), KeyboardButton("❓ Помощь")])
    else:
        keyboard.append([KeyboardButton("❓ Помощь")])

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие или введите команду",
    )


def keyboard_for_update(update: Update) -> ReplyKeyboardMarkup:
    chat_id = update.effective_chat.id if update.effective_chat else None
    return main_keyboard(chat_id)


def format_number(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", " ")


def format_rate(value: float) -> str:
    return f"{value:.4f}"


def parse_rate_value(text: str) -> float:
    return float(str(text).strip().replace(",", "."))


def now_bishkek_str() -> str:
    return datetime.now(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M:%S")


def now_bishkek_short() -> str:
    return datetime.now(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M")


def html_to_text(html: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def format_source_datetime(value: str | None) -> str:
    if not value or value == "дата не найдена":
        return "дата не найдена"

    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return value


def sync_to_google_sheets(payload: dict) -> dict | None:
    """
    Отправляет событие в Google Apps Script.

    Если Google Sheets временно недоступен, бот продолжает работать.
    Возвращает ответ Google Apps Script или None при ошибке.
    """
    if not GOOGLE_APPS_SCRIPT_URL:
        return None

    try:
        payload_with_secret = {
            **payload,
            "secret_token": GOOGLE_APPS_SCRIPT_SECRET,
        }

        response = requests.post(
            GOOGLE_APPS_SCRIPT_URL,
            json=payload_with_secret,
            timeout=8,
        )

        if response.status_code >= 400:
            logging.warning(
                "Google Sheets sync HTTP error: status=%s, body=%s",
                response.status_code,
                response.text[:500],
            )
            return None

        try:
            result = response.json()
        except Exception:
            logging.warning("Google Sheets sync returned non-json response: %s", response.text[:500])
            return None

        if not result.get("ok"):
            logging.warning("Google Sheets sync error: %s", result)

        return result

    except Exception as exc:
        logging.exception("Ошибка синхронизации с Google Sheets: %s", exc)
        return None


def load_json_file(path: str, default_value):
    if not os.path.exists(path):
        return default_value

    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        logging.exception("Ошибка чтения файла %s: %s", path, exc)
        return default_value


def save_json_file(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except Exception as exc:
        logging.exception("Ошибка сохранения файла %s: %s", path, exc)


def load_subscribers() -> set[int]:
    data = load_json_file(SUBSCRIBERS_FILE, {"chat_ids": []})
    chat_ids = data.get("chat_ids", [])
    return {int(chat_id) for chat_id in chat_ids}


def save_subscribers(subscribers: set[int]) -> None:
    data = {
        "chat_ids": sorted(list(subscribers)),
        "updated_at": datetime.now(BISHKEK_TZ).isoformat(),
    }
    save_json_file(SUBSCRIBERS_FILE, data)


def add_subscriber(chat_id: int) -> bool:
    subscribers = load_subscribers()

    if chat_id in subscribers:
        return False

    subscribers.add(chat_id)
    save_subscribers(subscribers)
    return True


def remove_subscriber(chat_id: int) -> bool:
    subscribers = load_subscribers()

    if chat_id not in subscribers:
        return False

    subscribers.remove(chat_id)
    save_subscribers(subscribers)
    return True


def load_users() -> dict:
    return load_json_file(USERS_FILE, {"users": {}})


def save_users(data: dict) -> None:
    save_json_file(USERS_FILE, data)


def get_active_subscribers_from_google() -> set[int] | None:
    """
    Получает активных подписчиков из Google Sheets.

    Возвращает:
    - set(chat_id), если Google Sheets ответил успешно;
    - None, если Google Sheets недоступен или вернул ошибку.

    Пустой set означает, что активных подписчиков реально нет.
    None означает техническую ошибку, тогда используем резервный subscribers.json.
    """
    result = sync_to_google_sheets(
        {
            "event_type": "get_active_subscribers",
        }
    )

    if not result or not result.get("ok"):
        return None

    raw_chat_ids = result.get("active_chat_ids", [])

    active_subscribers = set()

    for chat_id in raw_chat_ids:
        try:
            active_subscribers.add(int(chat_id))
        except Exception:
            continue

    return active_subscribers


def get_effective_subscribers() -> set[int]:
    """
    Основной источник подписчиков — Google Sheets.
    Резервный источник — локальный subscribers.json.
    """
    google_subscribers = get_active_subscribers_from_google()

    if google_subscribers is not None:
        return google_subscribers

    return load_subscribers()


def is_user_subscribed(chat_id: int) -> bool:
    return chat_id in get_effective_subscribers()


def track_user(update: Update, action: str) -> None:
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    now = now_bishkek_str()
    data = load_users()
    users = data.setdefault("users", {})

    key = str(chat_id)
    existing = users.get(key, {})

    actions_count = int(existing.get("actions_count", 0)) + 1
    first_seen = existing.get("first_seen", now)
    is_subscribed = is_user_subscribed(chat_id)

    users[key] = {
        "chat_id": chat_id,
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "first_seen": first_seen,
        "last_seen": now,
        "actions_count": actions_count,
        "last_action": action,
    }

    data["updated_at"] = datetime.now(BISHKEK_TZ).isoformat()
    save_users(data)

    sync_to_google_sheets(
        {
            "event_type": "user_activity",
            "datetime_bishkek": now,
            "chat_id": chat_id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "first_seen": first_seen,
            "last_seen": now,
            "actions_count": actions_count,
            "last_action": action,
            "is_subscribed": is_subscribed,
        }
    )


def sync_subscriber_update(update: Update, is_active: bool) -> None:
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user = update.effective_user

    sync_to_google_sheets(
        {
            "event_type": "subscriber_update",
            "datetime_bishkek": now_bishkek_str(),
            "chat_id": chat_id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "is_active": is_active,
        }
    )


def is_admin(update: Update) -> bool:
    if not update.effective_chat:
        return False

    return update.effective_chat.id == ADMIN_CHAT_ID


def get_nbkr_rub_rate() -> dict:
    try:
        response = requests.get(NBKR_DAILY_XML_URL, timeout=15)
        response.raise_for_status()

        if not response.encoding:
            response.encoding = "windows-1251"

        root = ET.fromstring(response.text)
        xml_date = root.attrib.get("Date", "дата не указана")

        for currency in root.findall("Currency"):
            if currency.attrib.get("ISOCode") == "RUB":
                nominal_text = currency.findtext("Nominal")
                value_text = currency.findtext("Value")

                if not nominal_text or not value_text:
                    return {
                        "ok": False,
                        "rate": None,
                        "date": xml_date,
                        "error": "На сайте НБКР найдена валюта RUB, но отсутствует Nominal или Value.",
                    }

                nominal = parse_rate_value(nominal_text)
                value = parse_rate_value(value_text)

                if nominal <= 0:
                    return {
                        "ok": False,
                        "rate": None,
                        "date": xml_date,
                        "error": "На сайте НБКР некорректный Nominal для RUB.",
                    }

                return {
                    "ok": True,
                    "rate": value / nominal,
                    "date": xml_date,
                    "error": None,
                }

        return {
            "ok": False,
            "rate": None,
            "date": xml_date,
            "error": "На сайте НБКР не найдена валюта RUB.",
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "rate": None,
            "date": None,
            "error": f"Ошибка запроса к сайту НБКР: {exc}",
        }
    except ET.ParseError as exc:
        return {
            "ok": False,
            "rate": None,
            "date": None,
            "error": f"Ошибка разбора данных НБКР: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "rate": None,
            "date": None,
            "error": f"Неожиданная ошибка при получении курса НБКР: {exc}",
        }


def get_bakai_cashless_rub_sell_rate() -> dict:
    try:
        response = requests.get(
            BAKAI_BANK_URL,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; KgsRubBot/1.0; "
                    "+https://github.com/ninokovank-blip/kgs-rub-bot)"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        response.raise_for_status()

        html = response.text

        normalized = html.replace('\\"', '"')
        normalized = normalized.replace("\\/", "/")

        date_match = re.search(
            r'"last_execution"\s*:\s*"([^"]+)"',
            normalized,
            flags=re.S,
        )
        site_date = date_match.group(1) if date_match else "дата не найдена"

        pattern = (
            r'"RUB"\s*:\s*\{.*?'
            r'"non_cash"\s*:\s*\{'
            r'\s*"buy"\s*:\s*([0-9]+(?:\.[0-9]+)?|null)\s*,'
            r'\s*"sell"\s*:\s*([0-9]+(?:\.[0-9]+)?|null)'
        )

        match = re.search(pattern, normalized, flags=re.S)

        if not match:
            return {
                "ok": False,
                "rate": None,
                "buy_rate": None,
                "sell_rate": None,
                "date": site_date,
                "error": (
                    "Не удалось найти RUB -> non_cash -> sell "
                    "в данных официального сайта Бакай Банка."
                ),
            }

        buy_text = match.group(1)
        sell_text = match.group(2)

        if buy_text == "null" or sell_text == "null":
            return {
                "ok": False,
                "rate": None,
                "buy_rate": None,
                "sell_rate": None,
                "date": site_date,
                "error": "В данных Бакай Банка RUB/non_cash содержит null вместо курса.",
            }

        rub_buy = parse_rate_value(buy_text)
        rub_sell = parse_rate_value(sell_text)

        if rub_sell <= 0:
            return {
                "ok": False,
                "rate": None,
                "buy_rate": rub_buy,
                "sell_rate": rub_sell,
                "date": site_date,
                "error": "Найденный курс продажи RUB Бакай Банка некорректен.",
            }

        return {
            "ok": True,
            "rate": rub_sell,
            "buy_rate": rub_buy,
            "sell_rate": rub_sell,
            "date": site_date,
            "error": None,
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "rate": None,
            "buy_rate": None,
            "sell_rate": None,
            "date": None,
            "error": f"Ошибка запроса к официальному сайту Бакай Банка: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "rate": None,
            "buy_rate": None,
            "sell_rate": None,
            "date": None,
            "error": f"Неожиданная ошибка при получении курса Бакай Банка: {exc}",
        }


def get_aiyl_cashless_rub_sell_rate() -> dict:
    try:
        response = requests.get(
            AIYL_BANK_URL,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; KgsRubBot/1.0; "
                    "+https://github.com/ninokovank-blip/kgs-rub-bot)"
                ),
                "Accept-Language": "ky-KG,ky;q=0.9,ru-RU;q=0.8,ru;q=0.7,en-US;q=0.6,en;q=0.5",
            },
        )
        response.raise_for_status()

        if not response.encoding:
            response.encoding = "utf-8"

        text = html_to_text(response.text)

        date_match = re.search(r"\b\d{2}\.\d{2}\.\d{4}\b", text)
        site_date = date_match.group(0) if date_match else "дата не найдена"

        pattern = (
            r"Валюта\s+Сатып\s+алуу\s+Сатуу"
            r".*?RUB\s+([0-9]+(?:[.,][0-9]+)?)\s+([0-9]+(?:[.,][0-9]+)?)"
        )

        matches = re.findall(pattern, text, flags=re.S | re.I)

        if len(matches) < 2:
            return {
                "ok": False,
                "rate": None,
                "buy_rate": None,
                "sell_rate": None,
                "date": site_date,
                "error": (
                    "Не удалось уверенно найти второй банковский блок курсов "
                    "Айыл Банка для безналичных операций."
                ),
            }

        cashless_buy_text, cashless_sell_text = matches[1]
        cashless_buy = parse_rate_value(cashless_buy_text)
        cashless_sell = parse_rate_value(cashless_sell_text)

        if cashless_sell <= 0:
            return {
                "ok": False,
                "rate": None,
                "buy_rate": cashless_buy,
                "sell_rate": cashless_sell,
                "date": site_date,
                "error": "Найденный курс продажи Айыл Банка некорректен.",
            }

        return {
            "ok": True,
            "rate": cashless_sell,
            "buy_rate": cashless_buy,
            "sell_rate": cashless_sell,
            "date": site_date,
            "error": None,
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "rate": None,
            "buy_rate": None,
            "sell_rate": None,
            "date": None,
            "error": f"Ошибка запроса к сайту Айыл Банка: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "rate": None,
            "buy_rate": None,
            "sell_rate": None,
            "date": None,
            "error": f"Неожиданная ошибка при получении курса Айыл Банка: {exc}",
        }


async def get_rates_async() -> dict:
    nbkr_data = get_nbkr_rub_rate()
    aiyl_data = get_aiyl_cashless_rub_sell_rate()
    bakai_data = get_bakai_cashless_rub_sell_rate()

    if nbkr_data["ok"]:
        nbkr_rate = nbkr_data["rate"]
        nbkr_status = f"получен с официального сайта НБКР, дата курса: {nbkr_data['date']}"
    else:
        nbkr_rate = DEFAULT_NBKR_FALLBACK
        nbkr_status = f"не получен, используется резервный тестовый курс. Причина: {nbkr_data['error']}"

    if aiyl_data["ok"]:
        aiyl_rate = aiyl_data["rate"]
        aiyl_status = (
            f"получен с официального сайта, тип курса: безналичная продажа RUB, "
            f"дата на сайте: {aiyl_data['date']}"
        )
    else:
        aiyl_rate = DEFAULT_AIYL_FALLBACK
        aiyl_status = (
            "не получен, используется резервный тестовый курс. "
            f"Причина: {aiyl_data['error']}"
        )

    if bakai_data["ok"]:
        bakai_rate = bakai_data["rate"]
        bakai_date = format_source_datetime(bakai_data["date"])
        bakai_status = (
            f"получен с официального сайта, тип курса: RUB / безналичная продажа, "
            f"дата обновления на сайте: {bakai_date}"
        )
    else:
        bakai_rate = DEFAULT_BAKAI_FALLBACK
        bakai_status = (
            "не получен, используется резервный тестовый курс. "
            f"Причина: {bakai_data['error']}"
        )

    return {
        "bakai": bakai_rate,
        "aiyl": aiyl_rate,
        "nbkr": nbkr_rate,
        "source_status": (
            "Статус источников:\n"
            f"• Бакай Банк: {bakai_status}\n"
            f"• Айыл Банк / A-bank: {aiyl_status}\n"
            f"• НБКР: {nbkr_status}"
        ),
    }


def parse_amount_text(text: str) -> float | None:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = cleaned.replace("RUB", "")
    cleaned = cleaned.replace("rub", "")
    cleaned = cleaned.replace("РУБ", "")
    cleaned = cleaned.replace("руб", "")
    cleaned = cleaned.replace("₽", "")
    cleaned = cleaned.replace(" ", "")

    if cleaned.count(",") > 0 and cleaned.count(".") == 0:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(".") > 1 and cleaned.count(",") == 0:
        cleaned = cleaned.replace(".", "")
    elif cleaned.count(",") > 0 and cleaned.count(".") > 0:
        cleaned = cleaned.replace(",", "")

    try:
        amount = float(cleaned)
    except ValueError:
        return None

    if amount <= 0:
        return None

    return amount


def get_best_bank_by_rate(rates: dict) -> dict:
    bakai_rate = rates["bakai"]
    aiyl_rate = rates["aiyl"]

    if bakai_rate < aiyl_rate:
        best_bank = "Бакай Банк"
        best_rate = bakai_rate
        worst_bank = "Айыл Банк / A-bank"
        worst_rate = aiyl_rate
    elif aiyl_rate < bakai_rate:
        best_bank = "Айыл Банк / A-bank"
        best_rate = aiyl_rate
        worst_bank = "Бакай Банк"
        worst_rate = bakai_rate
    else:
        best_bank = "Курсы равны"
        best_rate = bakai_rate
        worst_bank = "Курсы равны"
        worst_rate = aiyl_rate

    absolute_difference = abs(bakai_rate - aiyl_rate)

    if best_rate > 0 and best_bank != "Курсы равны":
        percent_difference = ((worst_rate / best_rate) - 1) * 100
    else:
        percent_difference = 0

    return {
        "best_bank": best_bank,
        "best_rate": best_rate,
        "worst_bank": worst_bank,
        "worst_rate": worst_rate,
        "absolute_difference": absolute_difference,
        "percent_difference": percent_difference,
    }


def calculate_purchase_cost(target_rub: float, rates: dict) -> dict:
    bakai_rate = rates["bakai"]
    aiyl_rate = rates["aiyl"]
    nbkr_rate = rates["nbkr"]

    cost_bakai_kgs = target_rub * bakai_rate
    cost_aiyl_kgs = target_rub * aiyl_rate
    cost_nbkr_kgs = target_rub * nbkr_rate

    bakai_vs_nbkr_kgs = cost_bakai_kgs - cost_nbkr_kgs
    aiyl_vs_nbkr_kgs = cost_aiyl_kgs - cost_nbkr_kgs

    best_info = get_best_bank_by_rate(rates)

    if best_info["best_bank"] == "Бакай Банк":
        saving_kgs = cost_aiyl_kgs - cost_bakai_kgs
    elif best_info["best_bank"] == "Айыл Банк / A-bank":
        saving_kgs = cost_bakai_kgs - cost_aiyl_kgs
    else:
        saving_kgs = 0

    bakai_spread_abs = bakai_rate - nbkr_rate
    aiyl_spread_abs = aiyl_rate - nbkr_rate

    bakai_spread_pct = ((bakai_rate / nbkr_rate) - 1) * 100
    aiyl_spread_pct = ((aiyl_rate / nbkr_rate) - 1) * 100

    return {
        "target_rub": target_rub,
        "bakai_rate": bakai_rate,
        "aiyl_rate": aiyl_rate,
        "nbkr_rate": nbkr_rate,
        "cost_bakai_kgs": cost_bakai_kgs,
        "cost_aiyl_kgs": cost_aiyl_kgs,
        "cost_nbkr_kgs": cost_nbkr_kgs,
        "bakai_vs_nbkr_kgs": bakai_vs_nbkr_kgs,
        "aiyl_vs_nbkr_kgs": aiyl_vs_nbkr_kgs,
        "best_bank": best_info["best_bank"],
        "absolute_difference": best_info["absolute_difference"],
        "percent_difference": best_info["percent_difference"],
        "saving_kgs": saving_kgs,
        "bakai_spread_abs": bakai_spread_abs,
        "bakai_spread_pct": bakai_spread_pct,
        "aiyl_spread_abs": aiyl_spread_abs,
        "aiyl_spread_pct": aiyl_spread_pct,
        "bakai_below_nbkr": bakai_rate < nbkr_rate,
        "aiyl_below_nbkr": aiyl_rate < nbkr_rate,
    }


def sync_rates_log_to_google(source_type: str, rates_data: dict) -> None:
    best_info = get_best_bank_by_rate(rates_data)

    bakai_spread_abs = rates_data["bakai"] - rates_data["nbkr"]
    aiyl_spread_abs = rates_data["aiyl"] - rates_data["nbkr"]

    bakai_spread_pct = ((rates_data["bakai"] / rates_data["nbkr"]) - 1) * 100
    aiyl_spread_pct = ((rates_data["aiyl"] / rates_data["nbkr"]) - 1) * 100

    sync_to_google_sheets(
        {
            "event_type": "rates_log",
            "datetime_bishkek": now_bishkek_str(),
            "source_type": source_type,
            "bakai_rate": rates_data["bakai"],
            "aiyl_rate": rates_data["aiyl"],
            "nbkr_rate": rates_data["nbkr"],
            "best_bank": best_info["best_bank"],
            "bank_difference_abs": best_info["absolute_difference"],
            "bank_difference_pct": best_info["percent_difference"],
            "bakai_spread_abs": bakai_spread_abs,
            "bakai_spread_pct": bakai_spread_pct,
            "aiyl_spread_abs": aiyl_spread_abs,
            "aiyl_spread_pct": aiyl_spread_pct,
        }
    )


def build_calculator_message(result: dict, source_status: str) -> str:
    now = now_bishkek_short()

    if result["best_bank"] == "Курсы равны":
        comment = "Курсы банков равны. Существенной разницы для выбора банка нет."
    else:
        comment = (
            f"Выгоднее покупать RUB через {result['best_bank']}, "
            "потому что курс продажи RUB ниже и для покупки нужной суммы потребуется меньше KGS."
        )

    anomaly_notes = []

    if result["bakai_below_nbkr"]:
        anomaly_notes.append(
            "Бакай Банк ниже курса НБКР — требуется ручная проверка типа курса и источника."
        )

    if result["aiyl_below_nbkr"]:
        anomaly_notes.append(
            "Айыл Банк / A-bank ниже курса НБКР — требуется ручная проверка типа курса и источника."
        )

    if anomaly_notes:
        anomaly_text = "\n\nПроверка аномалий:\n" + "\n".join(f"• {note}" for note in anomaly_notes)
    else:
        anomaly_text = "\n\nПроверка аномалий:\nАномалий по сравнению с НБКР не выявлено."

    return (
        "Калькулятор покупки RUB за KGS\n"
        f"Дата и время: {now} Бишкек\n\n"
        "Тип курса: безналичная продажа RUB\n\n"
        "Курсы RUB / KGS:\n"
        f"Бакай Банк: {format_rate(result['bakai_rate'])}\n"
        f"Айыл Банк / A-bank: {format_rate(result['aiyl_rate'])}\n"
        f"НБКР: {format_rate(result['nbkr_rate'])}\n\n"
        f"Цель: купить {format_number(result['target_rub'], 0)} RUB\n\n"
        f"Выгоднее: {result['best_bank']}\n\n"
        "Сколько KGS потребуется:\n"
        f"Бакай Банк: {format_number(result['cost_bakai_kgs'], 2)} KGS\n"
        f"Айыл Банк / A-bank: {format_number(result['cost_aiyl_kgs'], 2)} KGS\n"
        f"Ориентир по НБКР: {format_number(result['cost_nbkr_kgs'], 2)} KGS\n\n"
        "Разница между банками:\n"
        f"{format_rate(result['absolute_difference'])} KGS за 1 RUB / "
        f"{format_number(result['percent_difference'], 2)}%\n\n"
        "Экономия при выборе лучшего банка:\n"
        f"{format_number(result['saving_kgs'], 2)} KGS\n\n"
        "Спред к НБКР за 1 RUB:\n"
        f"Бакай Банк: {format_rate(result['bakai_spread_abs'])} KGS / "
        f"{format_number(result['bakai_spread_pct'], 2)}%\n"
        f"Айыл Банк / A-bank: {format_rate(result['aiyl_spread_abs'])} KGS / "
        f"{format_number(result['aiyl_spread_pct'], 2)}%\n\n"
        f"Отклонение от НБКР на сумму {format_number(result['target_rub'], 0)} RUB:\n"
        f"Бакай Банк: {format_number(result['bakai_vs_nbkr_kgs'], 2)} KGS\n"
        f"Айыл Банк / A-bank: {format_number(result['aiyl_vs_nbkr_kgs'], 2)} KGS"
        f"{anomaly_text}\n\n"
        "Комментарий:\n"
        f"{comment}\n\n"
        f"{source_status}"
    )


def build_notification_message(rates_data: dict) -> str:
    best_info = get_best_bank_by_rate(rates_data)
    now = now_bishkek_short()

    bakai_spread_abs = rates_data["bakai"] - rates_data["nbkr"]
    aiyl_spread_abs = rates_data["aiyl"] - rates_data["nbkr"]

    bakai_spread_pct = ((rates_data["bakai"] / rates_data["nbkr"]) - 1) * 100
    aiyl_spread_pct = ((rates_data["aiyl"] / rates_data["nbkr"]) - 1) * 100

    if best_info["best_bank"] == "Курсы равны":
        conclusion = "Курсы банков равны."
    else:
        conclusion = (
            f"Выгоднее сейчас: {best_info['best_bank']} "
            "за счёт более низкого курса продажи RUB."
        )

    return (
        "Плановое уведомление по курсам RUB / KGS\n"
        f"Дата и время: {now} Бишкек\n\n"
        "Тип курса: безналичная продажа RUB\n\n"
        f"Бакай Банк: {format_rate(rates_data['bakai'])}\n"
        f"Айыл Банк / A-bank: {format_rate(rates_data['aiyl'])}\n"
        f"НБКР: {format_rate(rates_data['nbkr'])}\n\n"
        f"{conclusion}\n\n"
        "Разница между банками:\n"
        f"{format_rate(best_info['absolute_difference'])} KGS за 1 RUB / "
        f"{format_number(best_info['percent_difference'], 2)}%\n\n"
        "Спред к НБКР:\n"
        f"• Бакай Банк: {format_rate(bakai_spread_abs)} KGS / {format_number(bakai_spread_pct, 2)}%\n"
        f"• Айыл Банк / A-bank: {format_rate(aiyl_spread_abs)} KGS / {format_number(aiyl_spread_pct, 2)}%\n\n"
        "Чтобы открыть калькулятор, нажмите 🧮 Калькулятор.\n"
        "Чтобы отключить рассылку, нажмите 🔕 Отписаться."
    )


def build_users_report() -> str:
    data = load_users()
    users = data.get("users", {})
    subscribers = get_effective_subscribers()

    if not users:
        return "Пока нет зафиксированных пользователей."

    sorted_users = sorted(
        users.values(),
        key=lambda item: item.get("last_seen", ""),
        reverse=True,
    )

    lines = [
        "Пользователи бота",
        f"Всего пользователей: {len(sorted_users)}",
        f"Подписчиков на рассылку: {len(subscribers)}",
        "",
    ]

    for index, user in enumerate(sorted_users[:30], start=1):
        chat_id = int(user.get("chat_id", 0))
        username = user.get("username") or ""
        first_name = user.get("first_name") or ""
        last_name = user.get("last_name") or ""
        full_name = f"{first_name} {last_name}".strip() or "имя не указано"

        username_text = f"@{username}" if username else "username не указан"
        subscribed_text = "да" if chat_id in subscribers else "нет"

        lines.extend(
            [
                f"{index}. {full_name} / {username_text}",
                f"chat_id: {chat_id}",
                f"первый запуск: {user.get('first_seen', 'н/д')}",
                f"последняя активность: {user.get('last_seen', 'н/д')}",
                f"действий: {user.get('actions_count', 0)}",
                f"последнее действие: {user.get('last_action', 'н/д')}",
                f"рассылка: {subscribed_text}",
                "",
            ]
        )

    if len(sorted_users) > 30:
        lines.append(f"Показаны последние 30 пользователей из {len(sorted_users)}.")

    return "\n".join(lines)


def parse_target_rub_from_command(context: ContextTypes.DEFAULT_TYPE) -> float | None:
    if not context.args:
        return None

    raw_amount = " ".join(context.args)
    return parse_amount_text(raw_amount)


async def show_users_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    report = build_users_report()

    if len(report) <= 4000:
        await update.message.reply_text(report, reply_markup=keyboard_for_update(update))
        return

    chunks = [report[i:i + 4000] for i in range(0, len(report), 4000)]

    for chunk in chunks:
        await update.message.reply_text(chunk, reply_markup=keyboard_for_update(update))


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        track_user(update, "unknown_command_users")

        await update.message.reply_text(
            "Не понял команду.\n\n"
            "Выберите действие кнопкой ниже или напишите /help.",
            reply_markup=keyboard_for_update(update),
        )
        return

    track_user(update, "users")
    await show_users_to_admin(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update, "start")

    message = (
        "Здравствуйте!\n\n"
        "Я бот для мониторинга курсов RUB / KGS.\n\n"
        "Что я показываю:\n"
        "• текущие курсы Бакай Банка, Айыл Банка / A-bank и НБКР;\n"
        "• какой банк выгоднее для покупки RUB за KGS;\n"
        "• спред каждого банка к курсу НБКР;\n"
        "• расчёт потребности в KGS для покупки заданной суммы RUB;\n"
        "• плановые уведомления по курсам в будние дни.\n\n"
        "Главная логика:\n"
        "вы вводите, сколько RUB нужно купить, а я считаю, сколько KGS потребуется через каждый банк.\n\n"
        "Доступные действия:\n"
        "📊 Курсы сейчас — показать актуальные курсы и лучший банк на текущий момент\n"
        "🧮 Калькулятор — рассчитать, сколько KGS потребуется для покупки нужной суммы RUB\n"
        "🔔 Подписаться на рассылку — получать курсы в будние дни с 07:00 до 17:00 по Бишкеку\n"
        "🔕 Отписаться — отключить автоматические уведомления\n"
        "❓ Помощь — посмотреть описание команд и логики расчёта\n\n"
        "Команды:\n"
        "/start — перезапустить бота и показать главное меню\n"
        "/rates — показать текущие курсы\n"
        "/calc — открыть калькулятор покупки RUB\n"
        "/subscribe — подписаться на рассылку\n"
        "/unsubscribe — отписаться от рассылки\n"
        "/help — помощь"
    )

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update, "help")

    message = (
        "Помощь по боту\n\n"
        "Основные кнопки:\n\n"
        "📊 Курсы сейчас\n"
        "Показывает текущие курсы RUB / KGS по Бакай Банку, Айыл Банку / A-bank и НБКР. "
        "Также бот сразу определяет, где сейчас выгоднее покупать RUB за KGS.\n\n"
        "🧮 Калькулятор\n"
        "Используется, когда нужно купить конкретную сумму RUB. "
        "Вы вводите сумму RUB, а бот считает, сколько KGS потребуется через каждый банк.\n\n"
        "🔔 Подписаться на рассылку\n"
        "Включает автоматические уведомления по курсам. "
        "Рассылка приходит в будние дни по Бишкекскому времени: 07:00, 09:00, 11:00, 13:00, 15:00 и 17:00.\n\n"
        "🔕 Отписаться\n"
        "Отключает автоматические уведомления по курсам.\n\n"
        "❓ Помощь\n"
        "Показывает описание кнопок, команд и логики расчёта.\n\n"
        "Команды:\n"
        "/start — открыть главное меню\n"
        "/rates — показать текущие курсы и лучший банк сейчас\n"
        "/calc — открыть калькулятор покупки RUB\n"
        "/subscribe — подписаться на рассылку\n"
        "/unsubscribe — отписаться от рассылки\n"
        "/help — показать эту справку\n\n"
        "Как пользоваться калькулятором:\n"
        "1. Нажмите 🧮 Калькулятор или напишите /calc\n"
        "2. Введите сумму RUB, которую нужно купить\n"
        "3. Например: 1000000\n\n"
        "Важно:\n"
        "для покупки RUB за KGS выгоднее тот банк, у которого ниже курс продажи RUB."
    )

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update, "rates")

    rates_data = await get_rates_async()
    sync_rates_log_to_google("manual_rates", rates_data)

    best_info = get_best_bank_by_rate(rates_data)
    now = now_bishkek_short()

    if best_info["best_bank"] == "Курсы равны":
        conclusion = "Курсы банков равны. Существенной разницы сейчас нет."
    else:
        conclusion = (
            f"Сейчас выгоднее покупать RUB через {best_info['best_bank']}, "
            "потому что курс продажи RUB ниже."
        )

    bakai_spread_abs = rates_data["bakai"] - rates_data["nbkr"]
    aiyl_spread_abs = rates_data["aiyl"] - rates_data["nbkr"]

    bakai_spread_pct = ((rates_data["bakai"] / rates_data["nbkr"]) - 1) * 100
    aiyl_spread_pct = ((rates_data["aiyl"] / rates_data["nbkr"]) - 1) * 100

    message = (
        "Текущие курсы RUB / KGS\n"
        "Тип курса: безналичная продажа RUB\n"
        f"Дата и время: {now} Бишкек\n\n"
        f"Бакай Банк: {format_rate(rates_data['bakai'])}\n"
        f"Айыл Банк / A-bank: {format_rate(rates_data['aiyl'])}\n"
        f"НБКР: {format_rate(rates_data['nbkr'])}\n\n"
        f"Лучший банк сейчас: {best_info['best_bank']}\n\n"
        "Разница между банками:\n"
        f"{format_rate(best_info['absolute_difference'])} KGS за 1 RUB / "
        f"{format_number(best_info['percent_difference'], 2)}%\n\n"
        "Спред к НБКР за 1 RUB:\n"
        f"Бакай Банк: {format_rate(bakai_spread_abs)} KGS / "
        f"{format_number(bakai_spread_pct, 2)}%\n"
        f"Айыл Банк / A-bank: {format_rate(aiyl_spread_abs)} KGS / "
        f"{format_number(aiyl_spread_pct, 2)}%\n\n"
        "Комментарий:\n"
        f"{conclusion}\n\n"
        f"{rates_data['source_status']}"
    )

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_user(update, "calc_start")

    target_rub = parse_target_rub_from_command(context)

    if target_rub is not None:
        rates_data = await get_rates_async()
        sync_rates_log_to_google("calculator_command", rates_data)

        result = calculate_purchase_cost(target_rub, rates_data)
        message = build_calculator_message(result, rates_data["source_status"])
        await update.message.reply_text(message, reply_markup=keyboard_for_update(update))
        return ConversationHandler.END

    await update.message.reply_text(
        "Калькулятор покупки RUB за KGS\n\n"
        "Введите сумму RUB, которую нужно купить.\n\n"
        "Пример:\n"
        "1000000\n\n"
        "Для отмены напишите /cancel.",
        reply_markup=keyboard_for_update(update),
    )

    return WAITING_FOR_RUB_AMOUNT


async def calc_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_user(update, "calc_amount_received")

    target_rub = parse_amount_text(update.message.text)

    if target_rub is None:
        await update.message.reply_text(
            "Не удалось распознать сумму.\n\n"
            "Введите только сумму RUB числом, например:\n"
            "1000000\n\n"
            "Для отмены напишите /cancel.",
            reply_markup=keyboard_for_update(update),
        )
        return WAITING_FOR_RUB_AMOUNT

    rates_data = await get_rates_async()
    sync_rates_log_to_google("calculator", rates_data)

    result = calculate_purchase_cost(target_rub, rates_data)
    message = build_calculator_message(result, rates_data["source_status"])

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))
    return ConversationHandler.END


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    already_subscribed = is_user_subscribed(chat_id)

    add_subscriber(chat_id)
    sync_subscriber_update(update, True)
    track_user(update, "subscribe")

    if not already_subscribed:
        message = (
            "Готово, вы подписаны на рассылку курсов.\n\n"
            "Уведомления будут приходить в будние дни по Бишкекскому времени:\n"
            "07:00, 09:00, 11:00, 13:00, 15:00 и 17:00.\n\n"
            "Чтобы отключить рассылку, нажмите 🔕 Отписаться или напишите /unsubscribe."
        )
    else:
        message = (
            "Вы уже подписаны на рассылку курсов.\n\n"
            "Уведомления приходят в будние дни по Бишкекскому времени:\n"
            "07:00, 09:00, 11:00, 13:00, 15:00 и 17:00."
        )

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))


async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    was_subscribed = is_user_subscribed(chat_id)

    remove_subscriber(chat_id)
    sync_subscriber_update(update, False)
    track_user(update, "unsubscribe")

    if was_subscribed:
        message = "Готово, рассылка отключена."
    else:
        message = "Вы не были подписаны на рассылку."

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_user(update, "cancel")

    await update.message.reply_text("Калькулятор закрыт.", reply_markup=keyboard_for_update(update))
    return ConversationHandler.END


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update, "buy")

    target_rub = parse_target_rub_from_command(context)

    if target_rub is None:
        await update.message.reply_text(
            "Для расчёта используйте калькулятор:\n"
            "/calc",
            reply_markup=keyboard_for_update(update),
        )
        return

    rates_data = await get_rates_async()
    sync_rates_log_to_google("buy_command", rates_data)

    result = calculate_purchase_cost(target_rub, rates_data)
    message = build_calculator_message(result, rates_data["source_status"])

    await update.message.reply_text(message, reply_markup=keyboard_for_update(update))


async def scheduled_rates_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(BISHKEK_TZ)

    if now.weekday() >= 5:
        return

    if now.hour not in NOTIFICATION_HOURS:
        return

    if now.minute > 4:
        return

    notification_key = now.strftime("%Y-%m-%d-%H")

    if notification_key in SENT_NOTIFICATION_KEYS:
        return

    SENT_NOTIFICATION_KEYS.add(notification_key)

    subscribers = get_effective_subscribers()

    if not subscribers:
        return

    rates_data = await get_rates_async()
    sync_rates_log_to_google("scheduled_notification", rates_data)

    message = build_notification_message(rates_data)

    for chat_id in subscribers:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=main_keyboard(chat_id),
            )
        except Exception as exc:
            logging.exception("Ошибка отправки уведомления chat_id=%s: %s", chat_id, exc)


async def text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip().lower()

    if text in [
        "📊 курсы сейчас",
        "курсы",
        "курс",
        "курс сейчас",
        "текущие курсы",
        "какой банк выгоднее",
        "сравни курс",
        "сравни курс сейчас",
    ]:
        await rates(update, context)
        return

    if text in [
        "🧮 калькулятор",
        "калькулятор",
        "рассчитать",
        "расчет",
        "расчёт",
        "купить rub",
        "купить рубли",
    ]:
        await calc_start(update, context)
        return

    if text in [
        "🔔 подписаться на рассылку",
        "подписаться",
        "подписка",
        "включить рассылку",
    ]:
        await subscribe(update, context)
        return

    if text in [
        "🔕 отписаться",
        "отписаться",
        "отключить рассылку",
        "убрать рассылку",
    ]:
        await unsubscribe(update, context)
        return

    if text in [
        "👥 пользователи",
        "пользователи",
    ]:
        if is_admin(update):
            track_user(update, "users_button")
            await show_users_to_admin(update, context)
            return

        track_user(update, "unknown_text_users_button")

        await update.message.reply_text(
            "Не понял команду.\n\n"
            "Выберите действие кнопкой ниже или напишите /help.",
            reply_markup=keyboard_for_update(update),
        )
        return

    if text in [
        "❓ помощь",
        "помощь",
        "help",
    ]:
        await help_command(update, context)
        return

    track_user(update, "unknown_text")

    await update.message.reply_text(
        "Не понял команду.\n\n"
        "Выберите действие кнопкой ниже или напишите /help.",
        reply_markup=keyboard_for_update(update),
    )


def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not bot_token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Добавьте токен Telegram-бота в переменные Railway."
        )

    app = Application.builder().token(bot_token).build()

    calc_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("calc", calc_start),
            MessageHandler(filters.Regex("^🧮 Калькулятор$"), calc_start),
        ],
        states={
            WAITING_FOR_RUB_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, calc_amount_received)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rates", rates))
    app.add_handler(CommandHandler("subscribe", subscribe))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(calc_conversation)

    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("compare", buy))
    app.add_handler(CommandHandler("compare_now", buy))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_buttons))

    app.job_queue.run_repeating(
        scheduled_rates_job,
        interval=60,
        first=10,
        name="scheduled_rates_notifications",
    )

    app.run_polling()


if __name__ == "__main__":
    main()
