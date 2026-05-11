import asyncio
import os
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = "salon_bookings.sqlite3"
WORKING_HOURS = ["10:00", "11:30", "13:00", "14:30", "16:00", "17:30", "19:00"]
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

DEFAULT_SERVICES = [
    ("manicure", "Маникюр", 90, 1800),
    ("pedicure", "Педикюр", 90, 2200),
    ("haircut_male", "Мужская стрижка", 45, 1200),
    ("haircut_female", "Женская стрижка", 60, 2000),
    ("coloring", "Окрашивание волос", 150, 4500),
    ("styling", "Укладка", 60, 1800),
]
DEFAULT_MASTERS = [
    ("Анна", ["manicure", "pedicure"]),
    ("Мария", ["haircut_female", "coloring", "styling"]),
    ("Игорь", ["haircut_male", "haircut_female"]),
]

router = Router()


class Booking(StatesGroup):
    service = State()
    master = State()
    day = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


class AdminEditService(StatesGroup):
    price = State()
    duration = State()


class AdminManageService(StatesGroup):
    add_title = State()
    rename_title = State()
    add_master_name = State()


class AdminEditMaster(StatesGroup):
    add_name = State()
    edit_name = State()


class AdminEditMasterService(StatesGroup):
    price = State()
    duration = State()


def format_date_iso(iso_date: str) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return d.strftime("%d.%m.%Y")


def format_date_button(d: date) -> str:
    return f"{d.strftime('%d.%m.%Y')}, {WEEKDAYS_RU[d.weekday()]}"


def parse_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def user_reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="✨ Записаться"), KeyboardButton(text="📋 Мои записи")],
        [KeyboardButton(text="🏠 Главное меню")],
    ]
    if is_admin:
        keyboard.insert(0, [KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или напишите сообщение",
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✨ Записаться", callback_data="start_booking")],
            [InlineKeyboardButton(text="📋 Мои записи", callback_data="my_bookings")],
        ]
    )


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS services (
                key TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                duration INTEGER NOT NULL,
                price INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS masters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS master_services (
                master_id INTEGER NOT NULL,
                service_key TEXT NOT NULL,
                price INTEGER,
                duration INTEGER,
                UNIQUE(master_id, service_key)
            )
        """)
        # Миграции для старой версии БД: добавляем индивидуальные цену/длительность мастера, если их еще нет.
        try:
            await db.execute("ALTER TABLE master_services ADD COLUMN price INTEGER")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE master_services ADD COLUMN duration INTEGER")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                service_key TEXT NOT NULL,
                service_title TEXT NOT NULL,
                master_id INTEGER NOT NULL,
                master_name TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                booking_time TEXT NOT NULL,
                client_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS available_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id INTEGER NOT NULL,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(master_id, slot_date, slot_time)
            )
        """)
        for row in DEFAULT_SERVICES:
            await db.execute(
                "INSERT OR IGNORE INTO services (key, title, duration, price) VALUES (?, ?, ?, ?)", row
            )
        cur = await db.execute("SELECT COUNT(*) FROM masters")
        count = (await cur.fetchone())[0]
        if count == 0:
            for name, service_keys in DEFAULT_MASTERS:
                cur = await db.execute("INSERT INTO masters (name) VALUES (?)", (name,))
                master_id = cur.lastrowid
                for service_key in service_keys:
                    await db.execute(
                        "INSERT OR IGNORE INTO master_services (master_id, service_key) VALUES (?, ?)",
                        (master_id, service_key),
                    )
        await db.commit()


async def fetch_services(active_only: bool = True):
    query = "SELECT key, title, duration, price, is_active FROM services"
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY title"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query)
        return await cur.fetchall()


async def get_service(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT key, title, duration, price FROM services WHERE key = ?", (key,))
        return await cur.fetchone()


def make_service_key(title: str) -> str:
    # Простой технический ключ для новой услуги. Название клиент видит отдельно.
    cleaned = re.sub(r"[^a-zA-Zа-яА-Я0-9]+", "_", title.strip().lower()).strip("_")
    return f"svc_{cleaned[:20]}_{int(datetime.now().timestamp())}"


async def get_master_service(service_key: str, master_id: int):
    """Возвращает услугу с учетом индивидуальной цены и длительности конкретного мастера."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT s.key, s.title,
                   COALESCE(ms.duration, s.duration) AS duration,
                   COALESCE(ms.price, s.price) AS price
            FROM services s
            LEFT JOIN master_services ms ON ms.service_key=s.key AND ms.master_id=?
            WHERE s.key=?
            """,
            (master_id, service_key),
        )
        return await cur.fetchone()


async def fetch_masters_for_service(service_key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT m.id, m.name, COALESCE(ms.price, s.price) AS price, COALESCE(ms.duration, s.duration) AS duration FROM masters m
            JOIN master_services ms ON ms.master_id = m.id
            JOIN services s ON s.key = ms.service_key
            WHERE ms.service_key = ? AND m.is_active = 1
            ORDER BY m.name
            """,
            (service_key,),
        )
        return await cur.fetchall()


async def fetch_active_masters():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM masters WHERE is_active = 1 ORDER BY name")
        return await cur.fetchall()


async def get_master(master_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM masters WHERE id = ?", (master_id,))
        return await cur.fetchone()


async def slot_is_free(master_id: int, booking_date: str, booking_time: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM available_slots WHERE master_id=? AND slot_date=? AND slot_time=? AND is_active=1",
            (master_id, booking_date, booking_time),
        )
        if (await cur.fetchone())[0] == 0:
            return False
        cur = await db.execute(
            "SELECT COUNT(*) FROM bookings WHERE master_id=? AND booking_date=? AND booking_time=? AND status='active'",
            (master_id, booking_date, booking_time),
        )
        return (await cur.fetchone())[0] == 0


async def get_available_times(master_id: int, booking_date: str) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT slot_time FROM available_slots WHERE master_id=? AND slot_date=? AND is_active=1 ORDER BY slot_time",
            (master_id, booking_date),
        )
        rows = await cur.fetchall()
    result = []
    for (slot_time,) in rows:
        if await slot_is_free(master_id, booking_date, slot_time):
            result.append(slot_time)
    return result


async def slot_exists(master_id: int, slot_date: str, slot_time: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM available_slots WHERE master_id=? AND slot_date=? AND slot_time=? AND is_active=1",
            (master_id, slot_date, slot_time),
        )
        return (await cur.fetchone())[0] > 0


async def toggle_admin_slot(master_id: int, slot_date: str, slot_time: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT is_active FROM available_slots WHERE master_id=? AND slot_date=? AND slot_time=?",
            (master_id, slot_date, slot_time),
        )
        row = await cur.fetchone()
        if row is None:
            await db.execute(
                "INSERT INTO available_slots (master_id, slot_date, slot_time, is_active) VALUES (?, ?, ?, 1)",
                (master_id, slot_date, slot_time),
            )
            await db.commit()
            return True
        new_value = 0 if row[0] else 1
        await db.execute(
            "UPDATE available_slots SET is_active=? WHERE master_id=? AND slot_date=? AND slot_time=?",
            (new_value, master_id, slot_date, slot_time),
        )
        await db.commit()
        return bool(new_value)


async def create_booking(data: dict, message: Message) -> int:
    master = await get_master(int(data["master_id"]))
    service = await get_master_service(data["service_key"], int(data["master_id"]))
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO bookings (
                user_id, username, service_key, service_title, master_id, master_name,
                booking_date, booking_time, client_name, phone, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.chat.id,
                message.chat.username,
                service[0], service[1], master[0], master[1],
                data["booking_date"], data["booking_time"], data["client_name"], data["phone"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        await db.commit()
        return cur.lastrowid


def services_keyboard(services) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=title, callback_data=f"service:{key}")]
        for key, title, duration, price, active in services
    ]
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def masters_keyboard(masters) -> InlineKeyboardMarkup:
    rows = []
    for row in masters:
        master_id, name = row[0], row[1]
        if len(row) >= 4:
            rows.append([InlineKeyboardButton(text=f"{name} — {row[2]} ₽, {row[3]} мин", callback_data=f"master:{master_id}")])
        else:
            rows.append([InlineKeyboardButton(text=name, callback_data=f"master:{master_id}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Нет доступных мастеров", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к услугам", callback_data="back:services")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dates_keyboard() -> InlineKeyboardMarkup:
    today = date.today()
    rows = []
    for i in range(1, 8):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(text=format_date_button(d), callback_data=f"date:{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к мастерам", callback_data="back:masters")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def times_keyboard(master_id: int, booking_date: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=t, callback_data=f"time:{t}")] for t in await get_available_times(master_id, booking_date)]
    if not rows:
        rows.append([InlineKeyboardButton(text="Нет свободных окон", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="back:dates")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить запись", callback_data="confirm_booking")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_booking")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])


async def format_booking(data: dict) -> str:
    service = await get_master_service(data["service_key"], int(data["master_id"]))
    master = await get_master(int(data["master_id"]))
    return (
        "<b>Проверьте запись:</b>\n\n"
        f"Услуга: {service[1]}\n"
        f"Мастер: {master[1]}\n"
        f"Дата: {format_date_iso(data['booking_date'])}\n"
        f"Время: {data['booking_time']}\n"
        f"Имя: {data['client_name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Стоимость: {service[3]} ₽\n"
        f"Длительность: {service[2]} мин."
    )


async def show_home(message: Message | CallbackQuery, state: FSMContext, text: str = "Главное меню. Выберите действие:"):
    await state.clear()
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=main_menu())
        await message.answer()
    else:
        await message.answer(text, reply_markup=user_reply_keyboard(message.from_user.id in ADMIN_IDS))
        await message.answer("Быстрые кнопки:", reply_markup=main_menu())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    await show_home(message, state, "Здравствуйте! Я бот салона красоты. Помогу выбрать услугу и записаться на удобное время.")


@router.message(Command("cancel"))
@router.message(F.text == "🏠 Главное меню")
async def main_menu_message(message: Message, state: FSMContext) -> None:
    await show_home(message, state)


@router.message(Command("book"))
@router.message(F.text == "✨ Записаться")
async def book_command(message: Message, state: FSMContext) -> None:
    await state.set_state(Booking.service)
    services = await fetch_services()
    await message.answer("Выберите услугу:", reply_markup=services_keyboard(services))


@router.callback_query(F.data == "start_booking")
async def start_booking(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Booking.service)
    services = await fetch_services()
    await callback.message.edit_text("Выберите услугу:", reply_markup=services_keyboard(services))
    await callback.answer()


@router.callback_query(F.data == "main_menu")
async def show_main_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await show_home(callback, state)


@router.callback_query(F.data.startswith("service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    service_key = callback.data.split(":", 1)[1]
    service = await get_service(service_key)
    masters = await fetch_masters_for_service(service_key)
    await state.update_data(service_key=service_key)
    await state.set_state(Booking.master)
    await callback.message.edit_text(
        f"Вы выбрали: <b>{service[1]}</b>\nТеперь выберите мастера:",
        reply_markup=masters_keyboard(masters),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("master:"))
async def choose_master(callback: CallbackQuery, state: FSMContext) -> None:
    master_id = int(callback.data.split(":", 1)[1])
    master = await get_master(master_id)
    await state.update_data(master_id=master_id)
    await state.set_state(Booking.day)
    await callback.message.edit_text(f"Мастер: <b>{master[1]}</b>\nВыберите дату:", reply_markup=dates_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
    booking_date = callback.data.split(":", 1)[1]
    data = await state.get_data()
    await state.update_data(booking_date=booking_date)
    await state.set_state(Booking.time)
    await callback.message.edit_text(
        f"Дата: <b>{format_date_iso(booking_date)}</b>\nВыберите свободное время:",
        reply_markup=await times_keyboard(int(data["master_id"]), booking_date),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("time:"))
async def choose_time(callback: CallbackQuery, state: FSMContext) -> None:
    booking_time = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if not await slot_is_free(int(data["master_id"]), data["booking_date"], booking_time):
        await callback.answer("Это время уже занято. Выберите другое.", show_alert=True)
        return
    await state.update_data(booking_time=booking_time)
    await state.set_state(Booking.name)
    await callback.message.edit_text("Введите ваше имя.\n\nДля выхода нажмите кнопку «🏠 Главное меню» снизу или отправьте /cancel.")
    await callback.answer()


@router.message(Booking.name)
async def get_name(message: Message, state: FSMContext) -> None:
    client_name = (message.text or "").strip()
    if len(client_name) < 2:
        await message.answer("Пожалуйста, введите имя полностью. Для выхода — 🏠 Главное меню или /cancel.")
        return
    await state.update_data(client_name=client_name)
    await state.set_state(Booking.phone)
    await message.answer("Введите номер телефона в формате +7XXXXXXXXXX. Например: +79991234567")


@router.message(Booking.phone)
async def get_phone(message: Message, state: FSMContext) -> None:
    phone = parse_phone(message.text or "")
    if not phone:
        await message.answer("Некорректный номер. Формат: +7XXXXXXXXXX, например +79991234567. Можно также ввести 89991234567.")
        return
    await state.update_data(phone=phone)
    data = await state.get_data()
    await state.set_state(Booking.confirm)
    await message.answer(await format_booking(data), reply_markup=confirm_keyboard())


@router.callback_query(F.data == "confirm_booking")
async def confirm_booking(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    if not await slot_is_free(int(data["master_id"]), data["booking_date"], data["booking_time"]):
        await callback.answer("К сожалению, это время уже заняли. Начните запись заново.", show_alert=True)
        await state.clear()
        return
    booking_id = await create_booking(data, callback.message)
    await state.clear()
    service = await get_master_service(data["service_key"], int(data["master_id"]))
    master = await get_master(int(data["master_id"]))
    await callback.message.edit_text(
        f"✅ Запись подтверждена!\n\nНомер записи: <b>#{booking_id}</b>\n"
        f"Ждем вас {format_date_iso(data['booking_date'])} в {data['booking_time']}.\n\n"
        f"Можно посмотреть свои записи или записаться еще раз.",
        reply_markup=main_menu(),
    )
    admin_text = (
        f"🔔 Новая запись #{booking_id}\n\n"
        f"Клиент: {data['client_name']}\nТелефон: {data['phone']}\n"
        f"Услуга: {service[1]}\nМастер: {master[1]}\n"
        f"Дата и время: {format_date_iso(data['booking_date'])} {data['booking_time']}\n"
        f"Telegram: @{callback.from_user.username or 'без username'}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text)
        except Exception:
            pass
    await callback.answer()


@router.callback_query(F.data == "cancel_booking")
async def cancel_booking(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Запись отменена.", reply_markup=main_menu())
    await callback.answer()


@router.callback_query(F.data == "my_bookings")
@router.message(Command("my"))
@router.message(F.text == "📋 Мои записи")
async def my_bookings(event: Message | CallbackQuery) -> None:
    user_id = event.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, service_title, master_name, booking_date, booking_time FROM bookings WHERE user_id=? AND status='active' ORDER BY booking_date, booking_time LIMIT 10",
            (user_id,),
        )
        rows = await cur.fetchall()
    text = "У вас пока нет активных записей." if not rows else "<b>Ваши записи:</b>\n\n" + "\n".join(
        f"#{r[0]} — {r[1]}, мастер {r[2]}, {format_date_iso(r[3])} в {r[4]}" for r in rows
    )
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=main_menu())
        await event.answer()
    else:
        await event.answer(text, reply_markup=user_reply_keyboard(event.from_user.id in ADMIN_IDS))


# ----------------------- ADMIN -----------------------

def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Показать свободные окна", callback_data="admin_slots_overview")],
        [InlineKeyboardButton(text="🗓 Управлять свободными окнами", callback_data="admin_slots_manage")],
        [InlineKeyboardButton(text="💰 Услуги: цены и длительность", callback_data="admin_services")],
        [InlineKeyboardButton(text="👩‍💼 Мастера", callback_data="admin_masters")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")],
    ])


async def send_admin_panel(message: Message | CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, client_name, phone, service_title, master_name, booking_date, booking_time FROM bookings WHERE status='active' ORDER BY booking_date, booking_time LIMIT 20"
        )
        rows = await cur.fetchall()
    text = "<b>Админ-панель</b>\n\n"
    if rows:
        text += "<b>Ближайшие записи:</b>\n" + "\n\n".join(
            f"#{r[0]}\n{r[1]}, {r[2]}\n{r[3]} — {r[4]}\n{format_date_iso(r[5])} в {r[6]}" for r in rows
        )
    else:
        text += "Активных записей пока нет."
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=admin_menu_keyboard())
        await message.answer()
    else:
        await message.answer(text, reply_markup=admin_menu_keyboard())


@router.message(F.text == "⚙️ Админ-панель")
@router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("Эта команда доступна только администратору.")
        return
    await send_admin_panel(message)


@router.callback_query(F.data == "admin_slots_overview")
async def admin_slots_overview(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    today = date.today()
    end = today + timedelta(days=14)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT s.slot_date, s.slot_time, m.name, s.master_id
            FROM available_slots s
            JOIN masters m ON m.id = s.master_id
            WHERE s.is_active=1 AND s.slot_date BETWEEN ? AND ?
            AND NOT EXISTS (
                SELECT 1 FROM bookings b
                WHERE b.master_id=s.master_id AND b.booking_date=s.slot_date AND b.booking_time=s.slot_time AND b.status='active'
            )
            ORDER BY s.slot_date, s.slot_time, m.name
            """,
            (today.isoformat(), end.isoformat()),
        )
        rows = await cur.fetchall()
    if not rows:
        text = "Свободных окон на ближайшие 14 дней пока нет."
    else:
        parts = ["<b>Свободные окна на ближайшие 14 дней:</b>"]
        last_date = None
        for slot_date, slot_time, master_name, _ in rows:
            if slot_date != last_date:
                parts.append(f"\n<b>{format_date_iso(slot_date)}</b>")
                last_date = slot_date
            parts.append(f"• {slot_time} — {master_name}")
        text = "\n".join(parts)
    await callback.message.edit_text(text, reply_markup=admin_menu_keyboard())
    await callback.answer()


def admin_masters_select_keyboard(prefix="admin_master_slots") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


async def admin_slots_masters_keyboard() -> InlineKeyboardMarkup:
    masters = await fetch_active_masters()
    rows = [[InlineKeyboardButton(text=name, callback_data=f"admin_master_slots:{mid}")] for mid, name in masters]
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_home")
async def admin_home(callback: CallbackQuery) -> None:
    if callback.from_user.id in ADMIN_IDS:
        await send_admin_panel(callback)


@router.callback_query(F.data == "admin_slots_manage")
async def admin_slots_manage(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    await callback.message.edit_text("Выберите мастера для настройки окон:", reply_markup=await admin_slots_masters_keyboard())
    await callback.answer()


def admin_dates_keyboard(master_id: int) -> InlineKeyboardMarkup:
    today = date.today()
    rows = []
    for i in range(0, 14):
        d = today + timedelta(days=i)
        rows.append([InlineKeyboardButton(text=format_date_button(d), callback_data=f"admin_date:{master_id}:{d.isoformat()}")])
    rows.append([InlineKeyboardButton(text="⬅️ К мастерам", callback_data="admin_slots_manage")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin_master_slots:"))
async def admin_choose_master_slots(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    master_id = int(callback.data.split(":", 1)[1])
    master = await get_master(master_id)
    await callback.message.edit_text(f"Мастер: <b>{master[1]}</b>\nВыберите дату:", reply_markup=admin_dates_keyboard(master_id))
    await callback.answer()


async def admin_times_keyboard(master_id: int, slot_date: str) -> InlineKeyboardMarkup:
    rows = []
    for t in WORKING_HOURS:
        mark = "✅" if await slot_exists(master_id, slot_date, t) else "➕"
        rows.append([InlineKeyboardButton(text=f"{mark} {t}", callback_data=f"admin_toggle:{master_id}:{slot_date}:{t}")])
    rows.append([InlineKeyboardButton(text="⬅️ К датам", callback_data=f"admin_master_slots:{master_id}")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin_date:"))
async def admin_choose_date(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id, slot_date = callback.data.split(":", 2)
    master = await get_master(int(master_id))
    await callback.message.edit_text(
        f"Настройка окон: <b>{master[1]}</b>, {format_date_iso(slot_date)}\n\n✅ — показывается клиентам\n➕ — добавить окно",
        reply_markup=await admin_times_keyboard(int(master_id), slot_date),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_toggle:"))
async def admin_toggle_slot(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id, slot_date, slot_time = callback.data.split(":", 3)
    enabled = await toggle_admin_slot(int(master_id), slot_date, slot_time)
    await callback.message.edit_reply_markup(reply_markup=await admin_times_keyboard(int(master_id), slot_date))
    await callback.answer("Окно добавлено" if enabled else "Окно скрыто")


async def admin_services_keyboard() -> InlineKeyboardMarkup:
    services = await fetch_services(active_only=False)
    rows = []
    for key, title, duration, price, active in services:
        status = "" if active else " 🚫"
        rows.append([InlineKeyboardButton(text=f"{title}{status}", callback_data=f"admin_service:{key}")])
    rows.append([InlineKeyboardButton(text="➕ Добавить услугу", callback_data="service_add")])
    rows.append([InlineKeyboardButton(text="✏️ Редактировать/удалять услуги", callback_data="service_manage_list")])
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_home")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_services")
async def admin_services(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    await callback.message.edit_text(
        "<b>Услуги: цены и длительность</b>\n\n"
        "Выберите услугу. Затем выберите мастера, который выполняет эту услугу.\n"
        "Цена и длительность редактируются отдельно для каждого мастера.",
        reply_markup=await admin_services_keyboard(),
    )
    await callback.answer()


async def admin_service_masters_keyboard(service_key: str) -> InlineKeyboardMarkup:
    masters = await fetch_masters_for_service(service_key)
    rows = []
    for master_id, name, price, duration in masters:
        rows.append([InlineKeyboardButton(text=f"{name} — {price} ₽, {duration} мин", callback_data=f"admin_ms:{master_id}:{service_key}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="Пока нет исполнителей", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="🧩 Назначить/убрать мастеров", callback_data=f"service_assign:{service_key}")])
    rows.append([InlineKeyboardButton(text="⬅️ К услугам", callback_data="admin_services")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin_service:"))
async def admin_service_detail(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    key = callback.data.split(":", 1)[1]
    s = await get_service(key)
    await callback.message.edit_text(
        f"<b>{s[1]}</b>\n\n"
        "Выберите мастера, для которого нужно настроить стоимость и длительность.\n"
        "В списке показаны только мастера, у которых эта услуга включена.",
        reply_markup=await admin_service_masters_keyboard(key),
    )
    await callback.answer()


def admin_master_service_edit_keyboard(master_id: int, service_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Редактировать цену именно у этого мастера", callback_data=f"edit_ms_price:{master_id}:{service_key}")],
        [InlineKeyboardButton(text="⏱ Редактировать длительность именно у этого мастера", callback_data=f"edit_ms_duration:{master_id}:{service_key}")],
        [InlineKeyboardButton(text="⬅️ К выбору мастера", callback_data=f"admin_service:{service_key}")],
        [InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")],
    ])


@router.callback_query(F.data.startswith("admin_ms:"))
async def admin_master_service_detail(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id_raw, service_key = callback.data.split(":", 2)
    master_id = int(master_id_raw)
    service = await get_master_service(service_key, master_id)
    master = await get_master(master_id)
    await callback.message.edit_text(
        f"<b>{service[1]}</b>\n"
        f"Мастер: <b>{master[1]}</b>\n\n"
        f"Стоимость именно у этого мастера: <b>{service[3]} ₽</b>\n"
        f"Длительность именно у этого мастера: <b>{service[2]} мин.</b>\n\n"
        "Так можно разделить начинающих и профи: разные цены и разное время процедуры.",
        reply_markup=admin_master_service_edit_keyboard(master_id, service_key),
    )
    await callback.answer()


async def service_assign_keyboard(service_key: str) -> InlineKeyboardMarkup:
    masters = await fetch_active_masters()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT master_id FROM master_services WHERE service_key=?", (service_key,))
        assigned = {row[0] for row in await cur.fetchall()}
    rows = []
    for mid, name in masters:
        mark = "✅" if mid in assigned else "➕"
        rows.append([InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"service_toggle_master:{service_key}:{mid}")])
    rows.append([InlineKeyboardButton(text="➕ Создать нового мастера", callback_data=f"service_create_master:{service_key}")])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=f"admin_service:{service_key}")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("service_assign:"))
async def service_assign(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    service_key = callback.data.split(":", 1)[1]
    service = await get_service(service_key)
    await callback.message.edit_text(
        f"<b>{service[1]}</b>\n\nВыберите мастеров, которые выполняют эту услугу.\n"
        "Если нужного мастера нет — нажмите «Создать нового мастера».",
        reply_markup=await service_assign_keyboard(service_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("service_toggle_master:"))
async def service_toggle_master(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, service_key, master_id_raw = callback.data.split(":", 2)
    master_id = int(master_id_raw)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM master_services WHERE master_id=? AND service_key=?", (master_id, service_key))
        exists = (await cur.fetchone())[0] > 0
        if exists:
            await db.execute("DELETE FROM master_services WHERE master_id=? AND service_key=?", (master_id, service_key))
        else:
            cur2 = await db.execute("SELECT price, duration FROM services WHERE key=?", (service_key,))
            price, duration = await cur2.fetchone()
            await db.execute(
                "INSERT OR IGNORE INTO master_services (master_id, service_key, price, duration) VALUES (?, ?, ?, ?)",
                (master_id, service_key, price, duration),
            )
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=await service_assign_keyboard(service_key))
    await callback.answer("Мастер убран из услуги" if exists else "Мастер назначен на услугу")


@router.callback_query(F.data == "service_add")
async def service_add(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    await state.set_state(AdminManageService.add_title)
    await callback.message.edit_text(
        "Введите название новой услуги. Например: Брови, Макияж, Барберская стрижка.\n\n"
        "После создания можно сразу назначить мастеров, цену и длительность.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")]])
    )
    await callback.answer()


@router.message(AdminManageService.add_title)
async def save_new_service(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() in ("⚙️ Админ-панель", "/admin", "🏠 Главное меню"):
        await admin_panel(message, state); return
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Введите понятное название услуги.")
        return
    key = make_service_key(title)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO services (key, title, duration, price, is_active) VALUES (?, ?, 60, 0, 1)", (key, title))
        await db.commit()
    await state.clear()
    await message.answer(
        f"Услуга <b>{title}</b> добавлена. Теперь назначьте мастеров, которые ее выполняют.",
        reply_markup=await service_assign_keyboard(key),
    )


@router.callback_query(F.data.startswith("service_create_master:"))
async def service_create_master(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    service_key = callback.data.split(":", 1)[1]
    await state.update_data(service_key=service_key)
    await state.set_state(AdminManageService.add_master_name)
    await callback.message.edit_text(
        "Введите имя нового мастера. После создания он сразу будет назначен на эту услугу.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")]])
    )
    await callback.answer()


@router.message(AdminManageService.add_master_name)
async def save_service_new_master(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() in ("⚙️ Админ-панель", "/admin", "🏠 Главное меню"):
        await admin_panel(message, state); return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Введите имя мастера полностью.")
        return
    data = await state.get_data()
    service_key = data["service_key"]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO masters (name) VALUES (?)", (name,))
        master_id = cur.lastrowid
        cur2 = await db.execute("SELECT price, duration FROM services WHERE key=?", (service_key,))
        price, duration = await cur2.fetchone()
        await db.execute("INSERT INTO master_services (master_id, service_key, price, duration) VALUES (?, ?, ?, ?)", (master_id, service_key, price, duration))
        await db.commit()
    await state.clear()
    await message.answer(
        f"Мастер <b>{name}</b> создан и назначен на услугу.",
        reply_markup=await service_assign_keyboard(service_key),
    )


async def service_manage_keyboard() -> InlineKeyboardMarkup:
    services = await fetch_services(active_only=False)
    rows = [[InlineKeyboardButton(text=f"{title}{'' if active else ' 🚫'}", callback_data=f"service_manage:{key}")] for key, title, duration, price, active in services]
    rows.append([InlineKeyboardButton(text="➕ Добавить услугу", callback_data="service_add")])
    rows.append([InlineKeyboardButton(text="⬅️ К услугам", callback_data="admin_services")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "service_manage_list")
async def service_manage_list(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    await callback.message.edit_text("Выберите услугу для редактирования или удаления:", reply_markup=await service_manage_keyboard())
    await callback.answer()


def service_manage_detail_keyboard(service_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Переименовать услугу", callback_data=f"service_rename:{service_key}")],
        [InlineKeyboardButton(text="🧩 Назначить мастеров", callback_data=f"service_assign:{service_key}")],
        [InlineKeyboardButton(text="🗑 Удалить/скрыть услугу", callback_data=f"service_delete:{service_key}")],
        [InlineKeyboardButton(text="⬅️ К списку услуг", callback_data="service_manage_list")],
        [InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")],
    ])


@router.callback_query(F.data.startswith("service_manage:"))
async def service_manage_detail(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    service_key = callback.data.split(":", 1)[1]
    service = await get_service(service_key)
    await callback.message.edit_text(f"Услуга: <b>{service[1]}</b>", reply_markup=service_manage_detail_keyboard(service_key))
    await callback.answer()


@router.callback_query(F.data.startswith("service_rename:"))
async def service_rename(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    service_key = callback.data.split(":", 1)[1]
    await state.update_data(service_key=service_key)
    await state.set_state(AdminManageService.rename_title)
    await callback.message.edit_text(
        "Введите новое название услуги:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")]])
    )
    await callback.answer()


@router.message(AdminManageService.rename_title)
async def save_service_rename(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() in ("⚙️ Админ-панель", "/admin", "🏠 Главное меню"):
        await admin_panel(message, state); return
    title = (message.text or "").strip()
    if len(title) < 2:
        await message.answer("Введите понятное название услуги.")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE services SET title=? WHERE key=?", (title, data["service_key"]))
        await db.commit()
    await state.clear()
    await message.answer("Название услуги обновлено.", reply_markup=await service_manage_keyboard())


@router.callback_query(F.data.startswith("service_delete:"))
async def service_delete(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    service_key = callback.data.split(":", 1)[1]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE services SET is_active=0 WHERE key=?", (service_key,))
        await db.execute("DELETE FROM master_services WHERE service_key=?", (service_key,))
        await db.commit()
    await callback.message.edit_text("Услуга скрыта из клиентского меню и убрана у мастеров.", reply_markup=await service_manage_keyboard())
    await callback.answer()


async def admin_masters_keyboard() -> InlineKeyboardMarkup:
    masters = await fetch_active_masters()
    rows = [[InlineKeyboardButton(text=name, callback_data=f"admin_master_edit:{mid}")] for mid, name in masters]
    rows.append([InlineKeyboardButton(text="➕ Добавить мастера", callback_data="add_master")])
    rows.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "admin_masters")
async def admin_masters(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    await callback.message.edit_text("Мастера салона:", reply_markup=await admin_masters_keyboard())
    await callback.answer()


def master_edit_keyboard(master_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить имя", callback_data=f"rename_master:{master_id}")],
        [InlineKeyboardButton(text="🧩 Услуги мастера", callback_data=f"master_services:{master_id}")],
        [InlineKeyboardButton(text="🗑 Удалить мастера", callback_data=f"delete_master:{master_id}")],
        [InlineKeyboardButton(text="⬅️ К мастерам", callback_data="admin_masters")],
        [InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")],
    ])


@router.callback_query(F.data.startswith("admin_master_edit:"))
async def admin_master_detail(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    master_id = int(callback.data.split(":", 1)[1])
    master = await get_master(master_id)
    await callback.message.edit_text(f"Мастер: <b>{master[1]}</b>", reply_markup=master_edit_keyboard(master_id))
    await callback.answer()


@router.callback_query(F.data == "add_master")
async def add_master(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    await state.set_state(AdminEditMaster.add_name)
    await callback.message.edit_text("Введите имя нового мастера. После добавления выберите для него услуги.")
    await callback.answer()


@router.message(AdminEditMaster.add_name)
async def save_new_master(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() == "⚙️ Админ-панель" or (message.text or "").strip() == "/admin":
        await admin_panel(message, state); return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Введите имя мастера полностью.")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO masters (name) VALUES (?)", (name,))
        await db.commit()
        master_id = cur.lastrowid
    await state.clear()
    await message.answer(f"Мастер {name} добавлен. Теперь выберите услуги мастера.", reply_markup=await master_services_keyboard(master_id))


@router.callback_query(F.data.startswith("rename_master:"))
async def rename_master(callback: CallbackQuery, state: FSMContext) -> None:
    master_id = int(callback.data.split(":", 1)[1])
    await state.update_data(master_id=master_id)
    await state.set_state(AdminEditMaster.edit_name)
    await callback.message.edit_text("Введите новое имя мастера:")
    await callback.answer()


@router.message(AdminEditMaster.edit_name)
async def save_master_name(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() == "⚙️ Админ-панель" or (message.text or "").strip() == "/admin":
        await admin_panel(message, state); return
    name = (message.text or "").strip()
    if len(name) < 2:
        await message.answer("Введите имя мастера полностью.")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE masters SET name=? WHERE id=?", (name, int(data["master_id"])))
        await db.commit()
    await state.clear()
    await message.answer("Имя мастера обновлено.", reply_markup=await admin_masters_keyboard())


@router.callback_query(F.data.startswith("delete_master:"))
async def delete_master(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    master_id = int(callback.data.split(":", 1)[1])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE masters SET is_active=0 WHERE id=?", (master_id,))
        await db.commit()
    await callback.message.edit_text("Мастер удален из активных.", reply_markup=await admin_masters_keyboard())
    await callback.answer()


async def master_services_keyboard(master_id: int) -> InlineKeyboardMarkup:
    services = await fetch_services(active_only=False)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT service_key, price, duration FROM master_services WHERE master_id=?", (master_id,))
        assigned_rows = await cur.fetchall()
    assigned = {row[0]: {"price": row[1], "duration": row[2]} for row in assigned_rows}
    rows = []
    for key, title, duration, price, active in services:
        if key in assigned:
            p = assigned[key]["price"] if assigned[key]["price"] is not None else price
            d = assigned[key]["duration"] if assigned[key]["duration"] is not None else duration
            rows.append([InlineKeyboardButton(text=f"✅ {title} — {p} ₽, {d} мин", callback_data=f"master_service_detail:{master_id}:{key}")])
        else:
            rows.append([InlineKeyboardButton(text=f"➕ {title}", callback_data=f"toggle_ms:{master_id}:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ К мастеру", callback_data=f"admin_master_edit:{master_id}")])
    rows.append([InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def master_service_detail_keyboard(master_id: int, service_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Редактировать цену именно у этого мастера", callback_data=f"edit_ms_price:{master_id}:{service_key}")],
        [InlineKeyboardButton(text="⏱ Редактировать длительность именно у этого мастера", callback_data=f"edit_ms_duration:{master_id}:{service_key}")],
        [InlineKeyboardButton(text="❌ Убрать услугу у мастера", callback_data=f"toggle_ms:{master_id}:{service_key}")],
        [InlineKeyboardButton(text="⬅️ К услугам мастера", callback_data=f"master_services:{master_id}")],
        [InlineKeyboardButton(text="🏠 Админ-панель", callback_data="admin_home")],
    ])


@router.callback_query(F.data.startswith("master_services:"))
async def master_services(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    master_id = int(callback.data.split(":", 1)[1])
    await callback.message.edit_text("Выберите услуги, которые делает мастер:", reply_markup=await master_services_keyboard(master_id))
    await callback.answer()


@router.callback_query(F.data.startswith("master_service_detail:"))
async def master_service_detail(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id, service_key = callback.data.split(":", 2)
    master_id = int(master_id)
    master = await get_master(master_id)
    service = await get_master_service(service_key, master_id)
    await callback.message.edit_text(
        f"<b>{master[1]}</b> — {service[1]}\n\nЦена для этого мастера: {service[3]} ₽\nДлительность: {service[2]} мин.\n\nЭто удобно для профи и начинающих мастеров: у каждого может быть своя цена и свое время выполнения.",
        reply_markup=await master_service_detail_keyboard(master_id, service_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_ms_price:"))
async def edit_master_service_price(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id, service_key = callback.data.split(":", 2)
    await state.update_data(master_id=int(master_id), service_key=service_key)
    await state.set_state(AdminEditMasterService.price)
    await callback.message.edit_text("Введите цену этой услуги именно для выбранного мастера. Например: 1500\n\nДля выхода нажмите «⚙️ Админ-панель» внизу или отправьте /admin.")
    await callback.answer()


@router.message(AdminEditMasterService.price)
async def save_master_service_price(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() == "⚙️ Админ-панель":
        await admin_panel(message, state); return
    if not (message.text or "").strip().isdigit():
        await message.answer("Введите цену числом, например 1500.")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE master_services SET price=? WHERE master_id=? AND service_key=?", (int(message.text), int(data["master_id"]), data["service_key"]))
        await db.commit()
    await state.clear()
    master_id = int(data["master_id"])
    service_key = data["service_key"]
    service = await get_master_service(service_key, master_id)
    master = await get_master(master_id)
    await message.answer(
        f"Цена для мастера обновлена.\n\n<b>{service[1]}</b>\nМастер: <b>{master[1]}</b>\nСтоимость именно у этого мастера: <b>{service[3]} ₽</b>\nДлительность именно у этого мастера: <b>{service[2]} мин.</b>",
        reply_markup=admin_master_service_edit_keyboard(master_id, service_key),
    )


@router.callback_query(F.data.startswith("edit_ms_duration:"))
async def edit_master_service_duration(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id, service_key = callback.data.split(":", 2)
    await state.update_data(master_id=int(master_id), service_key=service_key)
    await state.set_state(AdminEditMasterService.duration)
    await callback.message.edit_text("Введите длительность этой услуги именно для выбранного мастера в минутах. Например: 120\n\nДля выхода нажмите «⚙️ Админ-панель» внизу или отправьте /admin.")
    await callback.answer()


@router.message(AdminEditMasterService.duration)
async def save_master_service_duration(message: Message, state: FSMContext) -> None:
    if message.from_user.id not in ADMIN_IDS:
        return
    if (message.text or "").strip() == "⚙️ Админ-панель":
        await admin_panel(message, state); return
    if not (message.text or "").strip().isdigit():
        await message.answer("Введите длительность числом, например 120.")
        return
    data = await state.get_data()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE master_services SET duration=? WHERE master_id=? AND service_key=?", (int(message.text), int(data["master_id"]), data["service_key"]))
        await db.commit()
    await state.clear()
    master_id = int(data["master_id"])
    service_key = data["service_key"]
    service = await get_master_service(service_key, master_id)
    master = await get_master(master_id)
    await message.answer(
        f"Длительность для мастера обновлена.\n\n<b>{service[1]}</b>\nМастер: <b>{master[1]}</b>\nСтоимость именно у этого мастера: <b>{service[3]} ₽</b>\nДлительность именно у этого мастера: <b>{service[2]} мин.</b>",
        reply_markup=admin_master_service_edit_keyboard(master_id, service_key),
    )


@router.callback_query(F.data.startswith("toggle_ms:"))
async def toggle_master_service(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True); return
    _, master_id, service_key = callback.data.split(":", 2)
    master_id = int(master_id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM master_services WHERE master_id=? AND service_key=?", (master_id, service_key))
        exists = (await cur.fetchone())[0] > 0
        if exists:
            await db.execute("DELETE FROM master_services WHERE master_id=? AND service_key=?", (master_id, service_key))
        else:
            cur2 = await db.execute("SELECT price, duration FROM services WHERE key=?", (service_key,))
            base_price, base_duration = await cur2.fetchone()
            await db.execute("INSERT OR IGNORE INTO master_services (master_id, service_key, price, duration) VALUES (?, ?, ?, ?)", (master_id, service_key, base_price, base_duration))
        await db.commit()
    await callback.message.edit_reply_markup(reply_markup=await master_services_keyboard(master_id))
    await callback.answer("Услуга удалена" if exists else "Услуга добавлена")


@router.callback_query(F.data.startswith("back:"))
async def go_back(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if target == "services":
        await state.set_state(Booking.service)
        await callback.message.edit_text("Выберите услугу:", reply_markup=services_keyboard(await fetch_services()))
    elif target == "masters":
        await state.set_state(Booking.master)
        await callback.message.edit_text("Выберите мастера:", reply_markup=masters_keyboard(await fetch_masters_for_service(data["service_key"])))
    elif target == "dates":
        await state.set_state(Booking.day)
        await callback.message.edit_text("Выберите дату:", reply_markup=dates_keyboard())
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в .env")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
