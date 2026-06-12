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

# Сумма по умолчанию: сколько RUB нужно купить.
# Если написать просто /compare или /buy, бот посчитает на эту сумму.
DEFAULT_TARGET_RUB = 250_000_000


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
    Временная тестовая функция.

    Пока возвращает тестовые курсы, чтобы проверить формулы и формат сообщения.

    Важно:
    это курс продажи RUB за KGS.
    То есть сколько сомов стоит 1 российский рубль.

    Позже эту функцию заменим на реальные источники:
    - Бакай Банк;
    - Айыл Банк / A-bank;
    - НБКР.
    """
    return {
        "bakai": 1.2450,
        "aiyl": 1.2300,
        "nbkr": 1.2135,
        "source_status": "Тестовые данные, не для финансового решения",
    }


def calculate_purchase_cost(target_rub: float, rates: dict) -> dict:
    """
    Считает, сколько KGS потребуется для покупки заданной суммы RUB.

    Бизнес-логика:
    компания покупает RUB за KGS.
    Чем ниже курс продажи RUB, тем выгоднее банк.

    Формула:
    стоимость в KGS = сумма RUB × курс продажи RUB
    """
    bakai_rate = rates["bakai"]
    aiyl_rate = rates["aiyl"]
    nbkr_rate = rates["nbkr"]

    # Стоимость покупки заданной суммы RUB через каждый банк
    cost_bakai_kgs = target_rub * bakai_rate
    cost_aiyl_kgs = target_rub * aiyl_rate

    # Ориентир по официальному курсу НБКР
    cost_nbkr_kgs = target_rub * nbkr_rate

    # Отклонение от НБКР на сумму сделки
    bakai_vs_nbkr_kgs = cost_bakai_kgs - cost_nbkr_kgs
    aiyl_vs_nbkr_kgs = cost_aiyl_kgs - cost_nbkr_kgs

    # Определяем лучший банк.
    # Для покупки RUB выгоднее тот банк, у которого курс продажи RUB ниже.
    if bakai_rate < aiyl_rate:
        best_bank = "Бакай Банк"
        best_rate = bakai_rate
        worst_rate = aiyl_rate
        saving_kgs = cost_aiyl_kgs - cost_bakai_kgs
    elif aiyl_rate < bakai_rate:
        best_bank = "Айыл Банк / A-bank"
        best_rate = aiyl_rate
        worst_rate = bakai_rate
        saving_kgs = cost_bakai_kgs - cost_aiyl_kgs
    else:
        best_bank = "Курсы равны"
        best_rate = bakai_rate
        worst_rate = aiyl_rate
        saving_kgs = 0

    # Абсолютная разница между курсами банков
    absolute_difference = abs(bakai_rate - aiyl_rate)

    # Процентная разница между банками.
    # Показывает, насколько худший курс дороже лучшего.
    if best_rate > 0:
        percent_difference = ((worst_rate / best_rate) - 1) * 100
    else:
        percent_difference = 0

    # Спред каждого банка к НБКР за 1 RUB
    bakai_spread_abs = bakai_rate - nbkr_rate
    aiyl_spread_abs = aiyl_rate - nbkr_rate

    # Спред каждого банка к НБКР в процентах
    bakai_spread_pct = ((bakai_rate / nbkr_rate) - 1) * 100
    aiyl_spread_pct = ((aiyl_rate / nbkr_rate) - 1) * 100

    # Дополнительные флаги для будущей проверки аномалий
    bakai_below_nbkr = bakai_rate < nbkr_rate
    aiyl_below_nbkr = aiyl_rate < nbkr_rate

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
        "best_bank": best_bank,
        "absolute_difference": absolute_difference,
        "percent_difference": percent_difference,
        "saving_kgs": saving_kgs,
        "bakai_spread_abs": bakai_spread_abs,
        "bakai_spread_pct": bakai_spread_pct,
        "aiyl_spread_abs": aiyl_spread_abs,
        "aiyl_spread_pct": aiyl_spread_pct,
        "bakai_below_nbkr": bakai_below_nbkr,
        "aiyl_below_nbkr": aiyl_below_nbkr,
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

    message = (
        "Курсы RUB / KGS, безналичная продажа\n"
        f"Дата и время: {now} Бишкек\n\n"
        f"Бакай Банк: {format_rate(result['bakai_rate'])}\n"
        f"Айыл Банк / A-bank: {format_rate(result['aiyl_rate'])}\n"
        f"НБКР: {format_rate(result['nbkr_rate'])}\n\n"
        f"Цель: купить {format_number(result['target_rub'], 0)} RUB\n\n"
        f"Выгоднее для покупки RUB: {result['best_bank']}\n\n"
        "Стоимость покупки:\n"
        f"Через Бакай Банк потребуется: {format_number(result['cost_bakai_kgs'], 2)} KGS\n"
        f"Через Айыл Банк / A-bank потребуется: {format_number(result['cost_aiyl_kgs'], 2)} KGS\n"
        f"По курсу НБКР ориентир: {format_number(result['cost_nbkr_kgs'], 2)} KGS\n\n"
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

    return message


def parse_target_rub_from_command(context: ContextTypes.DEFAULT_TYPE) -> float:
    """
    Позволяет написать:
    /compare 250000000
    /buy 250000000

    Смысл суммы:
    сколько RUB нужно купить.
    """
    if not context.args:
        return DEFAULT_TARGET_RUB

    # Берём только первый аргумент после команды.
    # Например в /buy 250000000 это будет 250000000.
    raw_amount = context.args[0]

    # Убираем пробелы и приводим запятую к точке.
    raw_amount = raw_amount.replace(" ", "").replace(",", ".")

    try:
        amount = float(raw_amount)
        if amount <= 0:
            return DEFAULT_TARGET_RUB
        return amount
    except ValueError:
        return DEFAULT_TARGET_RUB


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "Здравствуйте!\n\n"
        "Я бот для мониторинга курсов RUB / KGS.\n\n"
        "Главная логика:\n"
        "вы вводите, сколько RUB нужно купить, "
        "а я считаю, сколько KGS потребуется через каждый банк.\n\n"
        "Доступные команды:\n"
        "/rates — показать текущие курсы\n"
        "/compare — сравнить покупку 250 000 000 RUB\n"
        "/compare 100000000 — сравнить покупку 100 000 000 RUB\n"
        "/buy 250000000 — рассчитать покупку 250 000 000 RUB\n"
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
        "/compare — сравнить покупку 250 000 000 RUB\n"
        "/compare 100000000 — сравнить покупку 100 000 000 RUB\n"
        "/buy 250000000 — то же самое, более понятная команда\n\n"
        "Важно:\n"
        "Сумма в командах /compare и /buy — это сумма RUB, которую нужно купить.\n\n"
        "Пример:\n"
        "/buy 250000000\n"
        "означает: мне нужно купить 250 млн RUB, посчитай, сколько KGS потребуется."
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
        "Логика:\n"
        "это курс продажи RUB, то есть сколько KGS стоит 1 RUB.\n"
        "Чем ниже курс, тем выгоднее покупка RUB.\n\n"
        f"Статус данных: {rates_data['source_status']}"
    )

    await update.message.reply_text(message)


async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target_rub = parse_target_rub_from_command(context)
    rates_data = get_test_rates()
    result = calculate_purchase_cost(target_rub, rates_data)
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
    app.add_handler(CommandHandler("buy", compare))

    app.run_polling()


if __name__ == "__main__":
    main()
