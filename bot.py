import logging
import sqlite3
import datetime
import os
import ssl
import certifi
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- ЗАГЛУШКА ДЛЯ RENDER ---
# Render требует открытый порт, иначе он думает, что бот упал
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# Запускаем заглушку в фоне
Thread(target=run_health_server, daemon=True).start()
# ---------------------------

# --- НАСТРОЙКИ ---
# В облаке токен берется из переменных окружения, локально - из кавычек
TOKEN = os.environ.get("BOT_TOKEN", "8947219918:AAHYm1JXN9zle1D4bVaOELyMjVc4rAGNNbI")

ADMIN_ID = 704799343       # Ваш ID (безлимитный доступ)
PRICE_STARS = 3            # Цена в звездах
CALCS_PER_PAYMENT = 5      # Сколько расчетов дает 1 оплата
FREE_DAYS_LIMIT = 7        # Сколько дней ждать до следующего бесплатного расчета

# --- ИНИЦИАЛИЗАЦИЯ БД ---
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users
                      (user_id INTEGER PRIMARY KEY, last_free_date TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS paid_balance
                      (user_id INTEGER PRIMARY KEY, calculations_left INTEGER)''')
    conn.commit()
    conn.close()

def get_user_data(user_id):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT last_free_date FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    last_free = row[0] if row else None

    cursor.execute('SELECT calculations_left FROM paid_balance WHERE user_id = ?', (user_id,))
    row_paid = cursor.fetchone()
    paid_left = row_paid[0] if row_paid else 0
    conn.close()
    return last_free, paid_left

def update_free_date(user_id, date_str):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO users (user_id, last_free_date) VALUES (?, ?)', (user_id, date_str))
    conn.commit()
    conn.close()

def update_paid_balance(user_id, amount):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO paid_balance (user_id, calculations_left) VALUES (?, 0)', (user_id,))
    cursor.execute('UPDATE paid_balance SET calculations_left = calculations_left + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()

init_db()

# --- ФУНКЦИЯ РАСЧЕТА ---
def calculate_date(start_date_str, total_days):
    try:
        start_date = datetime.datetime.strptime(start_date_str, "%d.%m.%Y").date()
        current_date = start_date
        days_counted = 0
        while days_counted < total_days:
            current_date += datetime.timedelta(days=1)
            if current_date.weekday() != 6: # 6 = воскресенье
                days_counted += 1
        return current_date.strftime("%d.%m.%Y")
    except ValueError:
        return None

# --- МАШИНА СОСТОЯНИЙ ---
class CalcStates(StatesGroup):
    waiting_for_date = State()
    waiting_for_days = State()

# --- БОТ ---
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я бот-калькулятор для расчета последнего дня оплаты по договору. Расчет ведется согласно условий договора (ежедневные оплат кроме воскресений).\n"
        f"🎁 **Вам доступен 1 бесплатный расчет в {FREE_DAYS_LIMIT} дней.**\n"
        f"💎 Дополнительные расчеты: {PRICE_STARS} звезды за {CALCS_PER_PAYMENT} штук.\n\n"
        "Введите дату заключения договора в формате ДД.ММ.ГГГГ (например, 09.10.2024):"
    )
    await state.set_state(CalcStates.waiting_for_date)

@dp.message(CalcStates.waiting_for_date, F.text)
async def process_date(message: Message, state: FSMContext):
    try:
        datetime.datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(start_date=message.text)
        await message.answer("Теперь введите количество дней (например, 730):")
        await state.set_state(CalcStates.waiting_for_days)
    except ValueError:
        await message.answer("❌ Неверный формат даты. Пожалуйста, введите в формате ДД.ММ.ГГГГ (например, 09.10.2024):")

@dp.message(CalcStates.waiting_for_days, F.text)
async def process_days(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Пожалуйста, введите целое число дней (например, 730):")
        return

    total_days = int(message.text)
    user_data = await state.get_data()
    start_date = user_data.get('start_date')
    user_id = message.from_user.id

    today = datetime.date.today()

    # --- ПРОВЕРКА ЛИМИТОВ ---
    if user_id == ADMIN_ID:
        # Админ (вы) — безлимит
        pass
    else:
        last_free_str, paid_left = get_user_data(user_id)
        is_free_available = False

        if last_free_str:
            last_free_date = datetime.datetime.strptime(last_free_str, "%Y-%m-%d").date()
            if (today - last_free_date).days >= FREE_DAYS_LIMIT:
                is_free_available = True
        else:
            is_free_available = True

        if is_free_available:
            update_free_date(user_id, today.strftime("%Y-%m-%d"))
        else:
            if paid_left > 0:
                update_paid_balance(user_id, -1)
            else:
                builder = InlineKeyboardBuilder()
                builder.button(text=f"Купить {CALCS_PER_PAYMENT} расчетов за {PRICE_STARS} ⭐", callback_data="buy_calcs")
                await message.answer(
                    f"❌ Бесплатный расчет доступен раз в {FREE_DAYS_LIMIT} дней.\n"
                    f"Купите пакет расчетов, чтобы продолжить:",
                    reply_markup=builder.as_markup()
                )
                await state.clear()
                return

    # --- ВЫПОЛНЕНИЕ РАСЧЕТА ---
    result = calculate_date(start_date, total_days)
    if result:
        await message.answer(
            f"✅ **Результат:**\n"
            f"Дата начала: {start_date}\n"
            f"Количество дней: {total_days}\n\n"
            f"Последний день оплаты: **{result}**\n\n"
            f"Для нового расчета нажмите /start"
        )
    else:
        await message.answer("❌ Ошибка при расчете. Проверьте введенные данные.")

    await state.clear()

# --- ОПЛАТА (TELEGRAM STARS) ---
@dp.callback_query(F.data == "buy_calcs")
async def send_invoice(callback: types.CallbackQuery):
    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Пакет расчетов",
        description=f"{CALCS_PER_PAYMENT} дополнительных расчетов дат",
        payload="extra_calcs",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Пакет расчетов", amount=PRICE_STARS)],
    )

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    user_id = message.from_user.id
    update_paid_balance(user_id, CALCS_PER_PAYMENT)
    await message.answer(
        f"✅ Оплата получена! Вам начислено {CALCS_PER_PAYMENT} расчетов.\n"
        f"Нажмите /start, чтобы продолжить."
    )

# --- ЗАПУСК ---
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    dp.run_polling(bot)








