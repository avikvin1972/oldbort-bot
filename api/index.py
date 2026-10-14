"""
Telegram-бот «анкета» (имя -> возраст) на вебхуках для Vercel.

КАК ЭТО РАБОТАЕТ (коротко):
1. Telegram получает сообщение пользователя и отправляет его (POST-запросом)
   на адрес нашего сайта на Vercel.
2. Vercel запускает эту функцию. Она читает сообщение, обрабатывает его
   и отправляет ответ пользователю через Telegram API.
3. Функция завершается. В памяти ничего не остаётся, поэтому «на каком шаге
   анкеты находится человек» мы храним во внешней базе Redis (Upstash).

ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (задаются в настройках проекта на Vercel, НЕ в коде):
    BOT_TOKEN       - токен бота от @BotFather
    REDIS_URL       - строка подключения Redis вида rediss://default:ПАРОЛЬ@адрес:6379
    WEBHOOK_SECRET  - любой секретный набор латинских букв/цифр (до 256 символов);
                      Telegram будет присылать его в заголовке, так мы отсекаем
                      чужие запросы
"""

import os
import asyncio
import json
import traceback
from http.server import BaseHTTPRequestHandler

from aiogram import Bot, Dispatcher, Router, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage


# ---------------------------------------------------------------------------
# 1. Состояния анкеты (те же, что были в app.py)
# ---------------------------------------------------------------------------
class Form(StatesGroup):
    name = State()  # ждём имя
    age = State()   # ждём возраст


# ---------------------------------------------------------------------------
# 2. Обработчики сообщений (логика почти не изменилась)
# ---------------------------------------------------------------------------
async def start(message: types.Message, state: FSMContext):
    """Команда /start: начинаем анкету с вопроса про имя."""
    await state.set_state(Form.name)
    await message.answer("Как тебя зовут?")


async def get_name(message: types.Message, state: FSMContext):
    """Получили имя: запоминаем его и спрашиваем возраст."""
    if not message.text:
        # Если прислали стикер или фото вместо текста - просим текст.
        await message.answer("Напиши имя текстом.")
        return
    await state.update_data(name=message.text)
    await state.set_state(Form.age)
    await message.answer("Сколько тебе лет?")


async def get_age(message: types.Message, state: FSMContext):
    """Получили возраст: проверяем, что это цифры, и завершаем анкету."""
    if not message.text or not message.text.isdigit():
        await message.answer("Напиши возраст цифрами, например 25")
        return
    data = await state.get_data()
    await message.answer(f"Приятно познакомиться, {data['name']}! Тебе {message.text} лет.")
    await state.clear()


async def other(message: types.Message):
    """Любое другое сообщение вне анкеты."""
    await message.answer("Напиши /start, чтобы заполнить анкету.")


def build_dispatcher(storage: RedisStorage) -> Dispatcher:
    """
    Собираем диспетчер заново для каждого запроса.

    Почему не один раз при запуске? На Vercel каждое сообщение может
    обрабатываться в новой копии программы, а соединение с Redis нельзя
    переиспользовать между запусками. Поэтому проще строить всё заново:
    это быстро (миллисекунды).

    Порядок регистрации ВАЖЕН: aiogram проверяет обработчики сверху вниз
    и берёт первый подходящий. Универсальный "other" должен быть последним.
    """
    router = Router()
    router.message.register(start, CommandStart())
    router.message.register(get_name, Form.name)
    router.message.register(get_age, Form.age)
    router.message.register(other)

    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    return dp


# ---------------------------------------------------------------------------
# 3. Обработка одного обновления (update) от Telegram
# ---------------------------------------------------------------------------
async def process_update(update_data: dict) -> None:
    """Принимает разобранный JSON от Telegram и обрабатывает его."""
    bot = Bot(token=os.environ["BOT_TOKEN"])
    # state_ttl / data_ttl: недозаполненные анкеты сами удалятся из Redis
    # через сутки (86400 секунд), чтобы база не копила мусор.
    storage = RedisStorage.from_url(
        os.environ["REDIS_URL"], state_ttl=86400, data_ttl=86400
    )
    try:
        dp = build_dispatcher(storage)
        update = types.Update.model_validate(update_data, context={"bot": bot})
        await dp.feed_update(bot, update)
    finally:
        # Обязательно закрываем соединения, иначе функция может «зависнуть».
        await storage.close()
        await bot.session.close()


# ---------------------------------------------------------------------------
# 4. Точка входа для Vercel
#    Vercel ищет в api/index.py класс с именем "handler".
# ---------------------------------------------------------------------------
class handler(BaseHTTPRequestHandler):

    def _reply(self, code: int, text: str) -> None:
        """Вспомогательная функция: отправить короткий текстовый ответ."""
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Открыв адрес в браузере, можно убедиться, что бот «жив»."""
        self._reply(200, "Bot is running")

    def do_POST(self):
        """Сюда Telegram присылает новые сообщения."""
        # --- Проверка секрета: пускаем только запросы от Telegram ---
        expected = os.environ.get("WEBHOOK_SECRET")
        if not expected:
            # Без секрета работать небезопасно - лучше остановиться.
            self._reply(500, "WEBHOOK_SECRET is not set")
            return
        received = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if received != expected:
            self._reply(403, "Forbidden")
            return

        # --- Читаем тело запроса (JSON с сообщением) ---
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            update_data = json.loads(raw)
            # Запускаем нашу асинхронную обработку и ждём её завершения.
            asyncio.run(process_update(update_data))
        except Exception:
            # Ошибку пишем в журнал Vercel (вкладка Logs), но всё равно
            # отвечаем 200: если ответить ошибкой, Telegram будет
            # бесконечно повторять то же сообщение.
            traceback.print_exc()

        self._reply(200, "ok")
