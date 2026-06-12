import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


BISHKEK_TZ = ZoneInfo("Asia/Bishkek")
DEFAULT_AMOUNT_KGS = 1_000_000


def format_number(value: float, digits: int = 2) -> str:
    """
    Красиво форматирует числа:
    1234567.89 -> 1 234 567.89
    """
    return f"{value:,.{digits}f}".replace(",", " ")


def format_rate(value: float) -> str:
    """
    Формат курса с 4 знаками после запятой.
    """
    return f"{value:.4f}"


def get_test_rates() -> dict:
    """
    Временная функция.
    Пока возвращает тестовые курсы, чтобы проверить формулы.
    Позже заменим на получение данных с сайтов банков и НБКР.
    """
    return {
        "bakai": 1.2450,
        "aiyl": 1.2300,
        "nbkr": 1.2135,
        "source_status": "Тестовые данные, не для финансового решения",
    }


def calculate_comparison(amount_kgs: float, rates: dict) -> dict:
    """
    Считает сравнение курсов.

    Логика:
    компания покупает RUB за KGS.
    Чем ниже курс продажи RUB, тем выгоднее банк.
    """
    bakai_rate = rates["bakai"]
    aiyl_rate = rates["aiyl"]
    nbkr_rate = rates["nbkr"]

    rub_bakai = amount_kgs / bakai_rate
    rub_aiyl = amount_kgs / aiyl_rate

    if bakai_rate < aiyl_rate:
        best_bank = "Бакай Банк"
        best_rate = bakai_rate
        worst_rate = aiyl_rate
        effect_rub = rub_bakai - rub_aiyl
    elif aiyl_rate < bakai_rate:
        best_bank = "Айыл Банк / A-bank"
        best_rate = aiyl_rate
        worst_rate = bakai_rate
        effect_rub = rub_aiyl - rub_bakai
    else:
        best_bank = "Курсы равны"
        best_rate = bakai_rate
        worst_rate = aiyl_rate
        effect_rub = 0

    absolute_difference = abs(bakai_rate - aiyl_rate)

    if best_rate > 0:
        percent_difference = ((worst_rate / best_rate) - 1) * 100
    else:
        percent_difference = 0

    bakai_spread_abs = bakai_rate - nbkr_rate
    aiyl_spread_abs = aiyl_rate - nbkr_rate

    bakai_spread_pct = ((bakai_rate / nbkr_rate) - 1) * 100
    aiyl_spread_pct = ((aiyl_rate / nbkr_rate) - 1) * 100

    return {
        "amount_kgs": amount_kgs,
        "bakai_rate": bakai_rate,
        "aiyl_rate": aiyl_rate,
        "nbkr_rate": nbkr_rate,
        "rub_bakai": rub_bakai,
        "rub_aiyl": rub_aiyl,
        "best_bank": best_bank,
        "absolute_difference": absolute_difference,
        "percent_difference": percent_difference,
        "effect_rub": effect_rub,
        "bakai_spread_abs": bakai_spread_abs,
        "bakai_spread_pct": bakai_spread_pct,
        "aiyl_spread_abs": aiyl_spread_abs,
        "aiyl_spread_pct": aiyl_spread_pct,
    }


def build_compare_message(result: dict, source_status: str) -> str:
    """
    Собирает текст Telegram-сообщения.
    """
    now = datetime.now(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M")

    if result["best_bank"] == "Курсы равны":
        comment = "Курсы банков равны. Существенной разницы для выбора банка нет."
    else:
        comment = (
            f"Выгоднее покупать RUB через {result['best_bank']}, "
            "потому что курс продажи RUB ниже."
        )

    message = (
        "Курсы RUB / KGS, безналичная продажа\n"
        f"Дата и время: {now} Бишкек\n\n"
        f"Бакай Банк: {format_rate(result['bakai_rate'])}\n"
        f"Айыл Банк / A-bank: {format_rate(result['aiyl_rate'])}\n"
        f"НБКР: {format_rate(result['nbkr_rate'])}\n\n"
        f"Выгоднее для покупки RUB: {result['best_bank']}\n\n"
        "Разница между банками:\n"
        f"{format_rate(result['absolute_difference'])} KGS за 1 RUB / "
        f"{format_number(result['percent_difference'], 2)}%\n\n"
        "Спред к НБКР:\n"
        f"Бакай Банк: {format_rate(result['bakai_spread_abs'])} KGS / "
        f"{format_number(result['bakai_spread_pct'], 2)}%\n"
        f"Айыл Банк / A-bank: {format_rate(result['aiyl_spread_abs'])} KGS / "
        f"{format_number(result['aiyl_spread_pct'], 2)}%\n\n"
        f"При сумме {format_number(result['amount_kgs'], 0)} KGS:\n"
        f"Через Бакай Банк можно купить: {format_number(result['rub_bakai'], 2)} RUB\n"
        f"Через Айыл Банк / A-bank можно купить: {format_number(result['rub_aiyl'], 2)} RUB\n"
        f"Эффект: {format_number(result['effect_rub'], 2)} RUB\n\n"
        "Комментарий:\n"
        f"{comment}\n\n"
        f"Статус данных: {source_status}"
    )

    return message


def parse_amount_from_command(context: ContextTypes.DEFAULT_TYPE) -> float:
    """
    Позволяет написать:
    /compare 10000000
    """
    if not context.args:
        return DEFAULT_AMOUNT_KGS

    raw_amount = context.args[0]
    raw_amount = raw_amount.replace(" ", "").replace(",", ".")

    try:
        amount = float(raw_amount)
        if amount <= 0:
            return DEFAULT_AMOUNT_KGS
        return amount
    except ValueError:
        return DEFAULT_AMOUNT_KGS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "Здравствуйте!\n\n"
        "Я бот для мониторинга курсов RUB / KGS.\n\n"
        "Доступные команды:\n"
        "/rates — показать текущие курсы\n"
        "/compare — сравнить курсы на 1 000 000 KGS\n"
        "/compare 10000000 — сравнить курсы на 10 000 000 KGS\n"
        "/help — помощь\n\n"
        "Сейчас используются тестовые данные. "
        "На следующих этапах подключим реальные источники банков и НБКР."
    )

    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "Доступные команды:\n\n"
        "/start — запуск бота\n"
        "/rates — показать текущие тестовые курсы\n"
        "/compare — сравнить курсы на сумму по умолчанию\n"
        "/compare 10000000 — сравнить курсы на указанную сумму KGS\n\n"
        "Важно: пока это тестовый режим. "
        "Реальные курсы подключим после проверки формул."
    )

    await update.message.reply_text(message)


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rates_data = get_test_rates()
    now = datetime.now(BISHKEK_TZ).strftime("%d.%m.%Y %H:%M")

    message = (
        "Текущие курсы RUB / KGS\n"
        f"Дата и время: {now} Бишкек\n\n"
        f"Бакай Банк: {format_rate(rates_data['bakai'])}\n"
        f"Айыл Банк / A-bank: {format_rate(rates_data['aiyl'])}\n"
        f"НБКР: {format_rate(rates_data['nbkr'])}\n\n"
        f"Статус данных: {rates_data['source_status']}"
    )

    await update.message.reply_text(message)


async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    amount_kgs = parse_amount_from_command(context)
    rates_data = get_test_rates()
    result = calculate_comparison(amount_kgs, rates_data)
    message = build_compare_message(result, rates_data["source_status"])

    await update.message.reply_text(message)


def main() -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not bot_token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Добавьте токен Telegram-бота в переменные Railway."
        )

    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("rates", rates))
    app.add_handler(CommandHandler("compare", compare))
    app.add_handler(CommandHandler("compare_now", compare))

    app.run_polling()


if __name__ == "__main__":
    main()
