import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from . import business, config, extras, jobs, owner
from .db import db


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await db.open()
    bot = Bot(config.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_routers(business.router, extras.router, owner.router)

    await bot.set_my_commands([
        BotCommand(command="settings", description="Режим ответов и настройки"),
        BotCommand(command="tasks", description="Открытые задачи"),
        BotCommand(command="plan", description="План на сегодня"),
        BotCommand(command="summary", description="Сводка переписок"),
        BotCommand(command="ask", description="Найти в переписке"),
        BotCommand(command="faq", description="Факты для ответов"),
        BotCommand(command="away", description="Режим «нет на связи»"),
        BotCommand(command="contacts", description="Важные и «не трогать»"),
        BotCommand(command="persona", description="Как отвечать от вашего имени"),
        BotCommand(command="help", description="Все команды"),
    ])
    jobs_task = asyncio.create_task(jobs.run(bot))
    try:
        # Business-обновления не приходят по умолчанию — запрашиваем их явно.
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        jobs_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
