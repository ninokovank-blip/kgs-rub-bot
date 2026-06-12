import os
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

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


def main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("📊 Курсы сейчас"), KeyboardButton("🧮 Калькулятор")],
        [KeyboardButton("❓ Помощь")],
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие или введите команду",
    )


def format_number(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", " ")


def format_rate(value: float) -> str:
    return f"{value:.4f}"


def parse_rate_value(text: str) -> float:
    return float(text.strip().replace(",", "."))


def html_to_text(html: str) -> str:
    """
    Превращает HTML в простой текст.
    Это нужно для сайтов банков, где курс уже есть в отрисованном HTML/тексте страницы.
    """
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def get_nbkr_rub_rate() -> dict:
    """
    Получает официальный курс RUB/KGS из XML НБКР.
    """
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
                        "error": "В XML НБКР найдена валюта RUB, но отсутствует Nominal или Value.",
                    }

                nominal = parse_rate_value(nominal_text)
                value = parse_rate_value(value_text)

                if nominal <= 0:
                    return {
                        "ok": False,
                        "rate": None,
                        "date": xml_date,
                        "error": "В XML НБКР некорректный Nominal для RUB.",
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
            "error": "В XML НБКР не найдена валюта RUB.",
        }

    except requests.RequestException as exc:
        return {
            "ok": False,
            "rate": None,
            "date": None,
            "error": f"Ошибка запроса к НБКР: {exc}",
        }
    except ET.ParseError as exc:
        return {
            "ok": False,
            "rate": None,
            "date": None,
            "error": f"Ошибка разбора XML НБКР: {exc}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "rate": None,
            "date": None,
            "error": f"Неожиданная ошибка при получении курса НБКР: {exc}",
        }


def get_aiyl_cashless_rub_sell_rate() -> dict:
    """
    Получает безналичный курс продажи RUB с официального сайта Айыл Банка / A-bank.
    """
    try:
        response = requests.get(
            AIYL_BANK_URL,
            timeout=20,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; KgsRubBot/1.0; "
                    "+https://github.com/ninokovank-blip/kgs-rub-bot)"
                )
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


def get_bakai_cashless_rub_sell_rate() -> dict:
    """
    Получает безналичный курс продажи RUB с официального сайта Бакай Банка.

    По проверке через document.body.innerText структура блока такая:

    Курсы валют
    Безналичные
    ...
    Покупка Продажа
    87.000 87.500     -> USD
    100.800 101.800   -> EUR
    1.170 1.240       -> RUB
    0.170 0.190       -> KZT

    Нам нужна продажа RUB, то есть третья пара значений, второе число.
    """
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

        if not response.encoding:
            response.encoding = "utf-8"

        text = html_to_text(response.text)

        # Дата и время курса Бакая
        date_time_match = re.search(
            r"Курс указан на\s+(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2})",
            text,
            flags=re.I,
        )
        site_date = date_time_match.group(1) if date_time_match else "дата не найдена"

        # Берём именно блок "Курсы валют" -> "Безналичные" -> до "Все курсы" или "Калькулятор"
        block_match = re.search(
            r"Курсы валют\s+Безналичные.*?Покупка\s+Продажа(?P<body>.*?)(?:Все курсы|Калькулятор)",
            text,
            flags=re.S | re.I,
        )

        if not block_match:
            return {
                "ok": False,
                "rate": None,
                "buy_rate": None,
                "sell_rate": None,
                "date": site_date,
                "error": "Не удалось найти блок 'Курсы валют / Безналичные' на официальном сайте Бакай Банка.",
            }

        rates_block = block_match.group("body")

        # Ищем пары покупка/продажа.
        # На официальном сайте в текстовом виде пары идут подряд:
        # 87.000 87.500
        # 100.800 101.800
        # 1.170 1.240
        # 0.170 0.190
        pairs = re.findall(
            r"([0-9]+(?:[.,][0-9]+)?)\s+([0-9]+(?:[.,][0-9]+)?)",
            rates_block,
        )

        if len(pairs) < 3:
            return {
                "ok": False,
                "rate": None,
                "buy_rate": None,
                "sell_rate": None,
                "date": site_date,
                "error": (
                    "В блоке безналичных курсов Бакай Банка найдено меньше трёх валютных строк. "
                    "Невозможно уверенно определить RUB."
                ),
            }

        # Третья пара — RUB.
        rub_buy_text, rub_sell_text = pairs[2]
        rub_buy = parse_rate_value(rub_buy_text)
        rub_sell = parse_rate_value(rub_sell_text)

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


def get_rates() -> dict:
    """
    Единая функция получения курсов.

    Сейчас:
    - Бакай Банк: официальный сайт;
    - Айыл Банк: официальный сайт;
    - НБКР: официальный XML.
    """
    nbkr_data = get_nbkr_rub_rate()
    aiyl_data = get_aiyl_cashless_rub_sell_rate()
    bakai_data = get_bakai_cashless_rub_sell_rate()

    if nbkr_data["ok"]:
        nbkr_rate = nbkr_data["rate"]
        nbkr_status = f"НБКР получен из официального XML, дата курса: {nbkr_data['date']}"
    else:
        nbkr_rate = 1.2135
        nbkr_status = f"НБКР не получен, используется тестовый fallback. Причина: {nbkr_data['error']}"

    if aiyl_data["ok"]:
        aiyl_rate = aiyl_data["rate"]
        aiyl_status = (
            f"Айыл Банк получен с официального сайта, "
            f"безналичная продажа RUB, дата сайта: {aiyl_data['date']}"
        )
    else:
        aiyl_rate = 1.2300
        aiyl_status = (
            "Айыл Банк не получен, используется тестовый fallback. "
            f"Причина: {aiyl_data['error']}"
        )

    if bakai_data["ok"]:
        bakai_rate = bakai_data["rate"]
        bakai_status = (
            f"Бакай Банк получен с официального сайта, "
            f"безналичная продажа RUB, дата сайта: {bakai_data['date']}"
        )
    else:
        bakai_rate = 1.2450
        bakai_status = (
            "Бакай Банк не получен, используется тестовый fallback. "
            f"Причина: {bakai_data['error']}"
        )

    return {
        "bakai": bakai_rate,
        "aiyl": aiyl_rate,
        "nbkr": nbkr_rate,
        "source_status": (
            f"{bakai_status}; "
            f"{aiyl_status}; "
            f"{nbkr_status}. "
            "Перед финансовым решением рекомендуется ручная сверка курса на сайтах банков."
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


def build_calculator_message(result: dict, source_status: str) -> str:
    now = datetime.now(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M")

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
        "Курсы RUB / KGS, безналичная продажа:\n"
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
        f"Статус данных: {source_status}"
    )


def parse_target_rub_from_command(context: ContextTypes.DEFAULT_TYPE) -> float | None:
    if not context.args:
        return None

    raw_amount = " ".join(context.args)
    return parse_amount_text(raw_amount)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "Здравствуйте!\n\n"
        "Я бот для мониторинга курсов RUB / KGS.\n\n"
        "Главная логика:\n"
        "вы вводите, сколько RUB нужно купить, "
        "а я считаю, сколько KGS потребуется через каждый банк.\n\n"
        "Выберите действие кнопкой ниже или используйте команды:\n"
        "/rates — показать текущие курсы и лучший банк сейчас\n"
        "/calc — открыть калькулятор покупки RUB\n"
        "/help — помощь\n\n"
        "Курсы банков подтягиваются с официальных сайтов. "
        "НБКР подтягивается из официального XML."
    )

    await update.message.reply_text(message, reply_markup=main_keyboard())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "Помощь\n\n"
        "Основные действия:\n"
        "📊 Курсы сейчас — показать текущие курсы и лучший банк\n"
        "🧮 Калькулятор — ввести сумму RUB и рассчитать потребность в KGS\n\n"
        "Как пользоваться калькулятором:\n"
        "1. Нажмите кнопку 🧮 Калькулятор или напишите /calc\n"
        "2. Бот попросит ввести сумму RUB\n"
        "3. Введите сумму, например: 250000000\n\n"
        "Важно:\n"
        "вводимая сумма — это сумма RUB, которую нужно купить."
    )

    await update.message.reply_text(message, reply_markup=main_keyboard())


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rates_data = get_rates()
    best_info = get_best_bank_by_rate(rates_data)
    now = datetime.now(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M")

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
        f"Статус данных: {rates_data['source_status']}"
    )

    await update.message.reply_text(message, reply_markup=main_keyboard())


async def calc_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_rub = parse_target_rub_from_command(context)

    if target_rub is not None:
        rates_data = get_rates()
        result = calculate_purchase_cost(target_rub, rates_data)
        message = build_calculator_message(result, rates_data["source_status"])
        await update.message.reply_text(message, reply_markup=main_keyboard())
        return ConversationHandler.END

    await update.message.reply_text(
        "Калькулятор покупки RUB за KGS\n\n"
        "Введите сумму RUB, которую нужно купить.\n\n"
        "Пример:\n"
        "250000000\n\n"
        "Для отмены напишите /cancel.",
    )

    return WAITING_FOR_RUB_AMOUNT


async def calc_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    target_rub = parse_amount_text(update.message.text)

    if target_rub is None:
        await update.message.reply_text(
            "Не удалось распознать сумму.\n\n"
            "Введите только сумму RUB числом, например:\n"
            "250000000\n\n"
            "Для отмены напишите /cancel."
        )
        return WAITING_FOR_RUB_AMOUNT

    rates_data = get_rates()
    result = calculate_purchase_cost(target_rub, rates_data)
    message = build_calculator_message(result, rates_data["source_status"])

    await update.message.reply_text(message, reply_markup=main_keyboard())
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Калькулятор закрыт.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_rub = parse_target_rub_from_command(context)

    if target_rub is None:
        await update.message.reply_text(
            "Для расчёта используйте калькулятор:\n"
            "/calc",
            reply_markup=main_keyboard(),
        )
        return

    rates_data = get_rates()
    result = calculate_purchase_cost(target_rub, rates_data)
    message = build_calculator_message(result, rates_data["source_status"])

    await update.message.reply_text(message, reply_markup=main_keyboard())


async def text_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if text == "📊 Курсы сейчас":
        await rates(update, context)
        return

    if text == "🧮 Калькулятор":
        await calc_start(update, context)
        return

    if text == "❓ Помощь":
        await help_command(update, context)
        return

    await update.message.reply_text(
        "Не понял команду.\n\n"
        "Выберите действие кнопкой ниже или напишите /help.",
        reply_markup=main_keyboard(),
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
    app.add_handler(calc_conversation)

    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("compare", buy))
    app.add_handler(CommandHandler("compare_now", buy))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_buttons))

    app.run_polling()


if __name__ == "__main__":
    main()
