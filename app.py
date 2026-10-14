import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()

class Form(StatesGroup):
    name = State()
    age = State()

@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.set_state(Form.name)
    await message.answer("Как тебя зовут?")

@dp.message(Form.name)
async def get_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(Form.age)
    await message.answer("Сколько тебе лет?")

@dp.message(Form.age)
async def get_age(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Напиши возраст цифрами, например 25")
        return
    data = await state.get_data()
    await message.answer(f"Приятно познакомиться, {data['name']}! Тебе {message.text} лет.")
    await state.clear()

@dp.message()
async def other(message: types.Message):
    await message.answer("Напиши /start, чтобы заполнить анкету.")

async def main():
    await dp.start_polling(bot)

asyncio.run(main())
