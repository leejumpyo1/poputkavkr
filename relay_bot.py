"""
Бот-релей для чата "Попутка".
Пересылает сообщения между пассажиром и водителем, привязанные к конкретной поездке,
без раскрытия их личных аккаунтов друг другу.

Установка:
    pip install aiogram

Запуск:
    export BOT_TOKEN="токен_от_BotFather"
    python relay_bot.py

Как это работает:
1. Водитель публикует поездку в Mini App -> бэкенд Mini App сохраняет ride_id и driver_tg_id
   (в этом файле для простоты используется SQLite, в проде — та же БД, что и у Mini App).
2. Пассажир жмёт "Написать водителю" -> Mini App открывает
       https://t.me/<BOT_USERNAME>?start=ride_<ride_id>
   через Telegram.WebApp.openTelegramLink() (см. пояснение в чате).
3. Бот видит /start ride_42, находит водителя этой поездки, создаёт связку
   (ride_id, passenger_id, driver_id) и с этого момента пересылает сообщения
   в обе стороны, пока чат не закрыт командой /end.
"""

import asyncio
import logging
import os
import sqlite3
import json
import urllib.request
from contextlib import closing

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Тот же URL, что вписан в poputka-app.html как BACKEND_URL
BACKEND_URL = os.environ.get(
    "BACKEND_URL",
    "https://script.google.com/macros/s/AKfycbzRGzgWzuorn3glsEnCYi1XeSqDjIBTo-kxAhlmjfAdeyXk86z8AQChZoWG2BkibB6V/exec"
)
DB_PATH = "poputka_chats.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------------------------------------------------------------- storage --

def db_init() -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ride_chats (
                ride_id      TEXT NOT NULL,
                passenger_id INTEGER NOT NULL,
                driver_id    INTEGER NOT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (ride_id, passenger_id)
            )
        """)
        con.commit()


def get_driver_for_ride(ride_id: str) -> int | None:
    """Спрашивает у Google Apps Script backend, кто водитель этой поездки."""
    try:
        with urllib.request.urlopen(BACKEND_URL, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logging.warning("Не удалось получить данные с бэкенда: %s", e)
        return None

    for ride in data.get("rides", []):
        if str(ride.get("id")) == str(ride_id):
            driver_id = ride.get("driverTgId")
            return int(driver_id) if driver_id else None
    return None


def open_chat(ride_id: int, passenger_id: int, driver_id: int) -> None:
    with closing(sqlite3.connect(DB_PATH)) as con:
        con.execute(
            "INSERT INTO ride_chats (ride_id, passenger_id, driver_id, active) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(ride_id, passenger_id) DO UPDATE SET active=1",
            (ride_id, passenger_id, driver_id),
        )
        con.commit()


def close_chat(passenger_id: int | None = None, driver_id: int | None = None) -> int:
    """Закрывает все активные чаты для данного пассажира или водителя. Возвращает число закрытых."""
    with closing(sqlite3.connect(DB_PATH)) as con:
        if passenger_id is not None:
            cur = con.execute(
                "UPDATE ride_chats SET active=0 WHERE passenger_id=? AND active=1", (passenger_id,)
            )
        else:
            cur = con.execute(
                "UPDATE ride_chats SET active=0 WHERE driver_id=? AND active=1", (driver_id,)
            )
        con.commit()
        return cur.rowcount


def find_active_peer(user_id: int) -> tuple[str, int, int] | None:
    """
    Ищет активный чат, где user_id участвует как пассажир или водитель.
    Возвращает (role, peer_id, ride_id) или None.
    """
    with closing(sqlite3.connect(DB_PATH)) as con:
        row = con.execute(
            "SELECT driver_id, ride_id FROM ride_chats WHERE passenger_id=? AND active=1",
            (user_id,),
        ).fetchone()
        if row:
            return ("passenger", row[0], row[1])

        row = con.execute(
            "SELECT passenger_id, ride_id FROM ride_chats WHERE driver_id=? AND active=1 "
            "ORDER BY ride_id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row:
            return ("driver", row[0], row[1])
    return None


# ---------------------------------------------------------------- handlers --

@dp.message(CommandStart(deep_link=True))
async def handle_start_with_ride(message: Message, command: CommandObject):
    """Обрабатывает t.me/<bot>?start=ride_42 — открывает чат с водителем этой поездки."""
    payload = command.args or ""
    if not payload.startswith("ride_"):
        await message.answer("Открой чат из мини-приложения кнопкой «Написать водителю».")
        return

    ride_id = payload.removeprefix("ride_")
    driver_id = get_driver_for_ride(ride_id)

    if driver_id is None:
        await message.answer("Не нашёл такую поездку. Возможно, она уже завершена.")
        return

    if driver_id == message.from_user.id:
        await message.answer("Это ваша собственная поездка — ждите сообщений от пассажиров здесь.")
        return

    open_chat(ride_id, passenger_id=message.from_user.id, driver_id=driver_id)
    await message.answer(
        "Чат с водителем открыт. Пишите сюда — сообщения будут переданы, "
        "ваш аккаунт водителю не раскрывается.\nЗавершить чат — /end"
    )
    await bot.send_message(
        driver_id,
        f"Пассажир интересуется вашей поездкой #{ride_id}. Отвечайте прямо здесь."
    )


@dp.message(CommandStart())
async def handle_plain_start(message: Message):
    await message.answer(
        "Привет! Это бот чата приложения «Попутка».\n"
        "Чтобы написать водителю, открой поездку в мини-приложении и нажми «Написать водителю»."
    )


@dp.message(Command("end"))
async def handle_end(message: Message):
    closed_as_passenger = close_chat(passenger_id=message.from_user.id)
    closed_as_driver = close_chat(driver_id=message.from_user.id)
    if closed_as_passenger or closed_as_driver:
        await message.answer("Чат закрыт.")
    else:
        await message.answer("Активных чатов не найдено.")


@dp.message(Command("myid"))
async def handle_myid(message: Message):
    """Вспомогательная команда для теста: узнать свой tg_id, чтобы вручную привязать как driver_id."""
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@dp.message(F.text | F.photo | F.voice | F.video | F.document | F.sticker)
async def relay_message(message: Message):
    """Пересылает любое сообщение активному собеседнику по этой поездке."""
    peer = find_active_peer(message.from_user.id)
    if peer is None:
        await message.answer(
            "У вас нет активного чата. Откройте поездку в мини-приложении, "
            "чтобы написать водителю."
        )
        return

    _role, peer_id, ride_id = peer
    await bot.forward_message(chat_id=peer_id, from_chat_id=message.chat.id, message_id=message.message_id)


async def main():
    db_init()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
