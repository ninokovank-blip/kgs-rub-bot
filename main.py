import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# Включаем базовое логирование.
# Это поможет видеть ошибки в Railway.
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /start.
    Проверяем, что бот живой и отвечает.
    """
    message = (
        "Здравствуйте!\n\n"
        "Я бот для мониторинга курсов RUB / KGS.\n\n"
        "Скоро я смогу:\n"
        "• показывать курсы Бакай Банка, Айыл Банка и НБКР;\n"
        "• сравнивать, где выгоднее покупать RUB за KGS;\n"
        "• считать спред к НБКР;\n"
        "• считать эффект на заданную сумму;\n"
        "• отправлять автоматические уведомления.\n\n"
        "Пока доступна тестовая команда:\n"
        "/start"
    )

    await update.message.reply_text(message)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /help.
    """
    message = (
        "Доступные команды:\n\n"
        "/start — запуск бота\n"
        "/help — помощь\n\n"
        "Команды с курсами добавим на следующем этапе."
    )

    await update.message.reply_text(message)


def main() -> None:
    """
    Главная функция запуска Telegram-бота.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not bot_token:
        raise RuntimeError(
            "Не найден TELEGRAM_BOT_TOKEN. "
            "Добавьте токен Telegram-бота в переменные Railway."
        )

    app = Application.builder().token(bot_token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))

    # polling — самый простой способ для MVP.
    # Бот сам постоянно спрашивает Telegram, есть ли новые сообщения.
    app.run_polling()


if __name__ == "__main__":
    main()
