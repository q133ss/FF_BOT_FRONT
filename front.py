import os
import re
import asyncio
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
import httpx
import json

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage


PAGE_SIZE = 5
HISTORY_PAGE_SIZE = 5
AUTBOOK_PAGE_SIZE = 5
AUTBOOK_ACCOUNTS_PAGE_SIZE = 5
MOVES_PAGE_SIZE = 5
OVERVIEW_PAGE_SIZE = 10
user_sessions = {} # ЗАМЕНИТЬ НА РЕАЛЬНУЮ БД

# Загружаем переменные окружения
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BACKEND_URL = "http://127.0.0.1:8001"


class WbAuthState(StatesGroup):
    wait_phone = State()
    wait_code = State()


class SlotSearchState(StatesGroup):
    warehouse = State()
    supply_type = State()
    max_coef = State()
    logistics = State()
    period_days = State()
    lead_time = State()
    weekdays = State()
    confirm = State()


class AutoBookState(StatesGroup):
    choose_task = State()
    choose_account = State()
    choose_transit = State()
    choose_draft = State()
    confirm = State()


class SlotTasksState(StatesGroup):
    list = State()


class AutoBookTasksState(StatesGroup):
    list = State()


class AutoBookNewState(StatesGroup):
    choose_account = State()
    choose_draft = State()
    warehouse = State()
    supply_type = State()
    max_coef = State()
    logistics = State()
    period_days = State()
    lead_time = State()
    weekdays = State()
    confirm = State()

# Визард создания задачи перераспределения остатков
class MoveWizardState(StatesGroup):
    choose_account = State()
    choose_article = State()
    choose_from_warehouse = State()
    choose_to_warehouse = State()
    choose_qty = State()
    confirm = State()

def get_warehouse_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()


def get_supply_type_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()


def get_coef_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()


def get_period_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()


def get_lead_time_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()


def get_weekdays_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardRemove()

def normalize_phone(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())

    # Убираем +7, 7, 8
    if digits.startswith("8"):
        digits = digits[1:]
    elif digits.startswith("7"):
        digits = digits[1:]

    # WB принимает только 10 цифр
    if len(digits) != 10:
        return None

    return digits


def get_logistics_coef_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="≤ 120%"), KeyboardButton(text="≤ 140%")],
            [KeyboardButton(text="≤ 160%"), KeyboardButton(text="≤ 180%")],
            [KeyboardButton(text="≤ 200%"), KeyboardButton(text="Не ограничивать")],
            [KeyboardButton(text="Назад"), KeyboardButton(text="Отмена")],
        ],
        resize_keyboard=True,
    )


def build_slot_summary(data: dict) -> str:
    """
    Собирает человекочитаемую сводку параметров задачи поиска слота.
    Ожидает в data поля: warehouse, supply_type, max_coef, period_days, lead_time_days, weekdays,
    max_logistics_coef_percent, search_period_from, search_period_to.
    """
    warehouse = data.get("warehouse")
    supply_type = data.get("supply_type")
    max_coef = data.get("max_coef")
    period_days = data.get("period_days")
    lead_time_days = data.get("lead_time_days")
    weekdays_code = data.get("weekdays")
    search_period_from = data.get("search_period_from")
    search_period_to = data.get("search_period_to")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")

    # Тип поставки
    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    # Дни недели
    ru_days = {
        "mon": "пн",
        "tue": "вт",
        "wed": "ср",
        "thu": "чт",
        "fri": "пт",
        "sat": "сб",
        "sun": "вс",
    }

    if weekdays_code == "daily":
        weekdays_text = "Каждый день"
    elif weekdays_code == "weekdays":
        weekdays_text = "Только будни (пн–пт)"
    elif weekdays_code == "weekends":
        weekdays_text = "Только выходные (сб–вс)"
    elif isinstance(weekdays_code, str) and weekdays_code.startswith("custom:"):
        # custom:mon,sat,sun,thu,tue → "пн, сб, вс, чт, вт"
        raw = weekdays_code.split(":", 1)[1]
        keys = [k for k in raw.split(",") if k]
        weekdays_text = ", ".join(ru_days.get(k, k) for k in keys)
    else:
        weekdays_text = "-" if weekdays_code is None else str(weekdays_code)

    # Период поиска
    def format_period(date_from: str | None, date_to: str | None) -> str:
        try:
            from_dt = datetime.fromisoformat(date_from).date() if date_from else None
            to_dt = datetime.fromisoformat(date_to).date() if date_to else None
        except ValueError:
            from_dt = to_dt = None

        if from_dt and to_dt:
            return f"{from_dt.strftime('%d.%m.%Y')}–{to_dt.strftime('%d.%m.%Y')}"
        if period_days is None:
            return "-"
        return f"{period_days} дней"

    period_text = format_period(search_period_from, search_period_to)

    # Логистика
    if max_logistics_coef_percent is None:
        logistics_text = "Не ограничивать"
    else:
        logistics_text = f"до {max_logistics_coef_percent}%"

    summary_lines = [
        "Проверь параметры задачи:",
        "",
        f"• Склад: {warehouse}",
        f"• Тип поставки: {supply_type_text}",
        f"• Макс. коэффициент: x{max_coef}",
        f"• Логистический коэффициент: {logistics_text}",
        f"• Период поиска: {period_text}",
        f"• Лид-тайм: {lead_time_days} дн.",
        f"• Дни недели: {weekdays_text}",
        "",
        "Создать задачу на поиск слота с такими параметрами?",
    ]
    return "\n".join(summary_lines)


def _format_slot_line(slot_item) -> str:
    """Возвращает человекочитаемую строку со слотами.

    Поддерживает строки (возвращает как есть) и словари, где пытается собрать
    дату, логистику и приёмку. При отсутствии понятных полей — делает dump
    словаря, чтобы не потерять данные.
    """

    if isinstance(slot_item, str):
        return slot_item

    if isinstance(slot_item, dict):
        date_str = (
            slot_item.get("date")
            or slot_item.get("day")
            or slot_item.get("date_from")
            or slot_item.get("date_text")
        )

        logistics_parts = []
        logistics_text = slot_item.get("logistics_text") or slot_item.get("logistics")
        logistics_cost = slot_item.get("logistics_cost") or slot_item.get("logistics_price")
        logistics_percent = (
            slot_item.get("logistics_percent")
            or slot_item.get("logistics_coef_percent")
            or slot_item.get("logistics_coef")
        )

        if logistics_text:
            logistics_parts.append(str(logistics_text))
        else:
            cost_bits = []
            if logistics_cost is not None:
                cost_bits.append(str(logistics_cost))
            if logistics_percent is not None:
                cost_bits.append(f"{logistics_percent}%")
            if cost_bits:
                logistics_parts.append(" / ".join(cost_bits))

        acceptance = slot_item.get("acceptance") or slot_item.get("acceptance_text")
        acceptance = acceptance or slot_item.get("acceptance_price")

        parts = []
        if date_str:
            parts.append(str(date_str))
        if logistics_parts:
            parts.append(f"логистика {' '.join(logistics_parts)}")
        if acceptance is not None:
            parts.append(f"приемка {acceptance}")

        if parts:
            return " • ".join(parts)

        return json.dumps(slot_item, ensure_ascii=False)

    return str(slot_item)


def _extract_slot_lines(search_response: dict | None) -> list[str]:
    """Достаёт список строк слотов из ответа /slots/search."""

    if not isinstance(search_response, dict):
        return []

    candidates = (
        search_response.get("slots"),
        search_response.get("available_slots"),
        search_response.get("slots_found"),
        search_response.get("slots_now"),
        search_response.get("slots_list"),
    )

    slot_items: list = next((c for c in candidates if isinstance(c, list)), [])

    lines: list[str] = []
    for slot_item in slot_items:
        line = _format_slot_line(slot_item)
        if line:
            lines.append(line)

    return lines


def _chunk_text_lines(lines: list[str], limit: int = 4000) -> list[str]:
    """Бьёт список строк на сообщения, учитывая лимит Telegram в 4096 символов."""

    if not lines:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        normalized = line.rstrip("\n")
        extra = len(normalized) + (1 if current else 0)

        if current and current_len + extra > limit:
            chunks.append("\n".join(current))
            current = [normalized]
            current_len = len(normalized)
        else:
            current.append(normalized)
            current_len += extra

    if current:
        chunks.append("\n".join(current))

    return chunks


async def _get_user_id(telegram_id: int) -> int | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/users/get-id",
                params={"telegram_id": telegram_id},
            )
            resp.raise_for_status()
            return resp.json().get("user_id")
    except Exception as e:
        print("Error calling /users/get-id:", e)
        return None


async def _autobook_add_message_id(message_obj: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = data.get("autobook_message_ids") or []
    ids.append(message_obj.message_id)
    await state.update_data(autobook_message_ids=ids)
    await add_ui_message(state, message_obj.message_id)


async def _autobook_clear_messages(message_obj: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = data.get("autobook_message_ids") or []
    for mid in ids:
        try:
            await message_obj.bot.delete_message(chat_id=message_obj.chat.id, message_id=mid)
        except Exception:
            continue
    await state.update_data(autobook_message_ids=[])


async def _drop_ui_message_id(state: FSMContext, mid: int) -> None:
    data = await state.get_data()
    modified = False
    for key in ("ui_message_ids", "autobook_message_ids", "slot_tasks_message_ids"):
        ids = data.get(key)
        if ids and mid in ids:
            data[key] = [i for i in ids if i != mid]
            modified = True
    if modified:
        await state.update_data(**data)


async def _clear_slot_tasks_messages(message_obj: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = data.get("slot_tasks_message_ids") or []
    for mid in ids:
        try:
            await message_obj.bot.delete_message(chat_id=message_obj.chat.id, message_id=mid)
        except Exception:
            continue
    await state.update_data(slot_tasks_message_ids=[])


async def _add_slot_tasks_message_id(message_obj: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = data.get("slot_tasks_message_ids") or []
    ids.append(message_obj.message_id)
    await state.update_data(slot_tasks_message_ids=ids)
    await add_ui_message(state, message_obj.message_id)


async def _clear_autobook_messages(message: Message, state: FSMContext) -> None:
    """
    Удаляет ранее отправленные ботом сообщения раздела 'Мои автоброни'.
    id сообщений храним в FSM под ключом 'autobook_message_ids'.
    """
    data = await state.get_data()
    ids = data.get("autobook_message_ids") or []
    for mid in ids:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=mid)
        except Exception:
            continue
    await state.update_data(autobook_message_ids=[])


async def _add_autobook_message_id(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ids = data.get("autobook_message_ids") or []
    ids.append(msg.message_id)
    await state.update_data(autobook_message_ids=ids)
    await add_ui_message(state, msg.message_id)


async def clear_ui(message: Message, state: FSMContext) -> None:
    """
    Очищает UI-сообщения основных разделов (Мои задачи, Мои автоброни и т.п.).
    Удаляет сообщения бота, id которых хранятся в FSM под известными ключами.
    """
    data = await state.get_data()

    keys = [
        "slot_tasks_message_ids",
        "autobook_message_ids",
        "ui_message_ids",
    ]

    modified = False

    for key in keys:
        ids = data.get(key) or []
        if not ids:
            continue

        for mid in ids:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=mid)
            except Exception:
                continue

        data[key] = []
        modified = True

    if modified:
        await state.update_data(**data)


async def add_ui_message(state: FSMContext, mid: int):
    data = await state.get_data()
    ids = data.get("ui_message_ids", [])
    ids.append(mid)
    await state.update_data(ui_message_ids=ids)


async def delete_ui_message(message: Message, state: FSMContext, mid: int):
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=mid)
        data = await state.get_data()
        ids = data.get("ui_message_ids", [])
        if mid in ids:
            ids = [stored_id for stored_id in ids if stored_id != mid]
            await state.update_data(ui_message_ids=ids)
    except Exception:
        pass


async def send_main_menu(message: Message, state: FSMContext) -> None:
    """
    Отрисовывает главное меню через inline-кнопки и очищает предыдущий UI.
    """
    await clear_all_ui(message, state)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🟢 Поиск слота", callback_data="menu_search")],
            [
                InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks"),
                InlineKeyboardButton(text="🤖 Автобронь", callback_data="menu_autobook"),
                InlineKeyboardButton(text="♻️ Перераспределения", callback_data="menu_moves"),
            ],
            [
                InlineKeyboardButton(text="⚙️ Авторизация WB", callback_data="menu_auth"),
                InlineKeyboardButton(text="📊 Статус WB", callback_data="menu_status"),
            ],
            [
                InlineKeyboardButton(text="🚪 Выйти из WB", callback_data="menu_logout"),
                InlineKeyboardButton(text="❓ Помощь", callback_data="menu_help"),
            ],
        ]
    )

    text = "🏠 Главное меню\n\nВыбери действие ниже:"
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def clear_all_ui(message: Message, state: FSMContext) -> None:
    """
    Глобальная очистка UI: удаляет все сообщения, ID которых бот хранит в FSM.
    """
    data = await state.get_data()
    keys = [
        "ui_message_ids",
        "slot_tasks_message_ids",
        "autobook_message_ids",
    ]
    modified = False
    for key in keys:
        ids = data.get(key) or []
        if not ids:
            continue
        for mid in ids:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=mid)
            except Exception:
                continue
        data[key] = []
        modified = True
    if modified:
        await state.update_data(**data)


async def show_moves_list(message: Message, state: FSMContext, telegram_id: int, page: int = 1) -> None:
    """
    Отображает список задач перераспределения (StockMoveTask) с пагинацией.
    """
    await clear_all_ui(message, state)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/slots/search",
                json={
                    "warehouse": warehouse,
                    "supply_type": {
                        "box": "Короба",
                        "mono": "Монопаллеты",
                        "postal": "Поштучная паллета",
                        "safe": "Суперсейф"
                    }[supply_type],
                    "max_booking_coefficient": str(max_coef),
                    "max_logistics_percent": max_logistics_coef_percent or 9999,
                    "search_period_days": period_days if period_days is not None else 30,
                    "lead_time_days": lead_time_days,
                    "weekdays_only": (weekdays_code == "weekdays"),
                    "telegram_chat_id": telegram_id,
                    "user_id": payload.get("user_id", telegram_id)
                },
            )
            resp.raise_for_status()
    except Exception as e:
        print("Error calling /stock-move/list:", e)
        kb_err = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
        )
        msg_err = await message.answer(
            "Не удалось получить список перераспределений. Попробуй позже.", reply_markup=kb_err
        )
        await add_ui_message(state, msg_err.message_id)
        return

    total = len(tasks)
    total_pages = (total - 1) // MOVES_PAGE_SIZE + 1 if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * MOVES_PAGE_SIZE
    end = start + MOVES_PAGE_SIZE
    page_tasks = tasks[start:end] if total else []

    lines = [
        "♻️ Твои перераспределения",
        f"Страница {page} из {total_pages}",
        "",
        "Нажми на нужный номер, чтобы открыть детали.",
        "",
    ]
    status_emoji = {"active": "🟢", "stopped": "⏸"}

    if page_tasks:
        for idx, t in enumerate(page_tasks, start=1):
            article = t.get("article")
            from_w = t.get("from_warehouse")
            to_w = t.get("to_warehouse")
            qty = t.get("qty")
            status = t.get("status")
            emoji = status_emoji.get(status, "⚪️")
            lines.append(
                f"{emoji} #{idx} — товар {article}, {from_w} → {to_w}, {qty} шт., статус: {status}"
            )
    else:
        lines.append("У тебя пока нет задач перераспределения.")

    text = "\n".join(lines)

    kb_rows = []
    if page_tasks:
        for idx, t in enumerate(page_tasks, start=1):
            task_id = t.get("id")
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"Открыть задачу #{idx}",
                        callback_data=f"moves_open:{task_id}",
                    )
                ]
            )

    nav_buttons = []
    if page > 1:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️", callback_data=f"moves_page:{page-1}")
        )
    if page < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(text="▶️", callback_data=f"moves_page:{page+1}")
        )
    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append(
        [InlineKeyboardButton(text="➕ Создать перераспределение", callback_data="moves_create")]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def show_move_card(message: Message, state: FSMContext, telegram_id: int, task_id: int) -> None:
    await clear_all_ui(message, state)

    task = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/stock-move/list",
                params={"telegram_id": telegram_id},
            )
            resp.raise_for_status()
            tasks = resp.json()
            task = next((t for t in tasks if t.get("id") == task_id), None)
    except Exception as e:
        print("Error calling /stock-move/list for card:", e)
        task = None

    if not task:
        kb_not_found = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ К списку", callback_data="menu_moves")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        msg_nf = await message.answer("Задача не найдена.", reply_markup=kb_not_found)
        await add_ui_message(state, msg_nf.message_id)
        return

    article = task.get("article")
    from_w = task.get("from_warehouse")
    to_w = task.get("to_warehouse")
    qty = task.get("qty")
    status = task.get("status")

    text = (
        f"♻️ Перераспределение #{task_id}\n"
        f"Товар: {article}\n"
        f"Со склада: {from_w}\n"
        f"На склад: {to_w}\n"
        f"Кол-во: {qty} шт.\n"
        f"Статус: {status}"
    )

    kb_rows = []
    if status == "active":
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="⏸ Остановить", callback_data=f"moves_stop:{task_id}"
                )
            ]
        )
    elif status == "stopped":
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Запустить", callback_data=f"moves_start:{task_id}"
                )
            ]
        )

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить (позже)", callback_data="moves_delete_not_implemented"
            )
        ]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="↩️ К списку", callback_data="menu_moves")]
    )
    kb_rows.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def show_move_accounts(message: Message, state: FSMContext) -> None:
    """
    Шаг 1: выбор поставщика.
    """
    await clear_all_ui(message, state)
    data = await state.get_data()
    options = data.get("move_options") or {}
    accounts = options.get("accounts") or []

    text = "Шаг 1 из 6 — поставщик.\n\nВыбери поставщика, по которому будем делать перераспределение:"
    kb_rows = []
    for acc in accounts:
        acc_id = acc.get("id")
        acc_name = acc.get("name")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=acc_name or acc_id, callback_data=f"moves_acc:{acc_id}"
                )
            ]
        )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def show_move_articles(message: Message, state: FSMContext) -> None:
    """
    Шаг 2: выбор товара.
    """
    await clear_all_ui(message, state)
    data = await state.get_data()
    options = data.get("move_options") or {}
    articles = options.get("articles") or []

    text = "Шаг 2 из 6 — товар.\n\nВыбери товар, который нужно перераспределить:"
    kb_rows = []
    for art in articles:
        art_id = art.get("id")
        art_name = art.get("name")
        total_qty = art.get("total_qty")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"{art_name} (остаток {total_qty} шт.)",
                    callback_data=f"moves_art:{art_id}",
                )
            ]
        )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Назад к поставщику", callback_data="moves_back_account")]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def show_move_from_warehouses(message: Message, state: FSMContext) -> None:
    """
    Шаг 3: выбор склада-источника (где есть остаток).
    """
    await clear_all_ui(message, state)
    data = await state.get_data()
    options = data.get("move_options") or {}
    article_id = data.get("article_id")

    articles = options.get("articles") or []
    article = next((a for a in articles if a.get("id") == article_id), None)
    stocks = article.get("stocks") if article else []

    text = "Шаг 3 из 6 — склад-источник.\n\nВыбери склад, с которого будем забирать товар:"
    kb_rows = []
    for st in stocks:
        wh = st.get("warehouse")
        qty = st.get("qty")
        if qty and qty > 0:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{wh} (доступно {qty} шт.)", callback_data=f"moves_from:{wh}"
                    )
                ]
            )
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Назад к товарам", callback_data="moves_back_article")]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def show_move_to_warehouses(message: Message, state: FSMContext) -> None:
    """
    Шаг 4: выбор склада-получателя.
    """
    await clear_all_ui(message, state)
    data = await state.get_data()
    options = data.get("move_options") or {}
    from_warehouse = data.get("from_warehouse")
    warehouses = options.get("warehouses") or []

    text = "Шаг 4 из 6 — склад-получатель.\n\nВыбери склад, на который отправим товар:"
    kb_rows = []
    for wh in warehouses:
        wh_name = wh.get("name")
        if wh_name and wh_name != from_warehouse:
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=wh_name, callback_data=f"moves_to:{wh_name}"
                    )
                ]
            )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад, выбрать другой источник", callback_data="moves_back_from"
            )
        ]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def start_move_wizard(message: Message, state: FSMContext, telegram_id: int) -> None:
    """
    Запуск мастера перераспределения: загружаем options и переходим к выбору поставщика.
    """
    await clear_all_ui(message, state)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BACKEND_URL}/stock-move/options")
            resp.raise_for_status()
            options = resp.json()
    except Exception as e:
        print("Error calling /stock-move/options:", e)
        kb_err = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
        )
        msg_err = await message.answer(
            "Не удалось загрузить данные для перераспределения. Попробуй позже.", reply_markup=kb_err
        )
        await add_ui_message(state, msg_err.message_id)
        return

    await state.clear()
    await state.update_data(telegram_id=telegram_id, move_options=options)
    await state.set_state(MoveWizardState.choose_account)
    await show_move_accounts(message, state)


async def show_move_qty(message: Message, state: FSMContext) -> None:
    """
    Шаг 4: выбор количества для перераспределения.
    """
    await clear_all_ui(message, state)

    text = "Шаг 4 из 4 — количество.\n\nВыбери, сколько единиц товара перераспределить:"
    qty_options = [10, 50, 100, 200]
    kb_rows = [[InlineKeyboardButton(text=f"{q} шт.", callback_data=f"moves_qty:{q}")] for q in qty_options]
    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Назад, выбрать другой склад", callback_data="moves_back_to")]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def show_move_confirm(message: Message, state: FSMContext) -> None:
    """
    Шаг 5: подтверждение создания задачи перераспределения.
    """
    await clear_all_ui(message, state)

    data = await state.get_data()
    options = data.get("move_options") or {}
    accounts = {acc.get("id"): acc.get("name") for acc in options.get("accounts", [])}
    articles_map = {art.get("id"): art for art in options.get("articles", [])}
    article_id = data.get("article_id")
    from_warehouse = data.get("from_warehouse")
    to_warehouse = data.get("to_warehouse")
    qty = data.get("qty")
    account_id = data.get("account_id")

    article = articles_map.get(article_id) or {}
    article_name = article.get("name", article_id)
    barcode = article.get("barcode")
    account_name = accounts.get(account_id, account_id)
    barcode_line = f"Штрихкод: {barcode}\n" if barcode else ""

    text = (
        "Проверь параметры задачи перераспределения:\n\n"
        f"Поставщик: {account_name}\n"
        f"Товар: {article_name}\n"
        f"{barcode_line}"
        f"Со склада: {from_warehouse}\n"
        f"На склад: {to_warehouse}\n"
        f"Количество: {qty} шт.\n\n"
        "Создать задачу с такими параметрами?"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать задачу", callback_data="moves_confirm")],
            [InlineKeyboardButton(text="⬅️ Назад, изменить количество", callback_data="moves_back_qty")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)

async def _send_autobook_page(message: Message, state: FSMContext, page: int = 0) -> None:
    data = await state.get_data()
    tasks = data.get("autobook_tasks") or []

    if not tasks:
        msg = await message.answer(
            "У тебя пока нет задач автобронирования.",
            reply_markup=get_main_menu_keyboard(),
        )
        await _add_autobook_message_id(msg, state)
        return

    total = len(tasks)
    total_pages = (total - 1) // AUTBOOK_PAGE_SIZE + 1
    page = max(0, min(page, total_pages - 1))

    start = page * AUTBOOK_PAGE_SIZE
    end = start + AUTBOOK_PAGE_SIZE
    page_tasks = tasks[start:end]

    lines = [
        "🤖 Твои автобронирования",
        f"Страница {page+1} из {total_pages}",
        "",
        "Нажми на нужный номер, чтобы открыть детали.",
        "",
    ]
    status_emoji = {
        "active": "🟢",
        "stopped": "⏸",
    }
    for idx, t in enumerate(page_tasks, start=1):
        task_id = t.get("id")
        slot_task_id = t.get("slot_search_task_id")
        status = t.get("status")
        emoji = status_emoji.get(status, "⚪️")
        lines.append(f"{emoji} #{idx} — задача поиска #{slot_task_id}, статус: {status}")

    text = "\n".join(lines)

    kb_rows = []
    for t in page_tasks:
        task_id = t.get("id")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"Открыть автобронь #{task_id}",
                    callback_data=f"autobook_open:{task_id}",
                )
            ]
        )

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"autobook_page:{page-1}",
            )
        )
    if page < total_pages - 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="▶️ Далее",
                callback_data=f"autobook_page:{page+1}",
            )
        )
    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Главное меню",
                callback_data="autobook_main_menu",
            )
        ]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _clear_autobook_messages(message, state)
    msg = await message.answer(text, reply_markup=kb)
    await _add_autobook_message_id(msg, state)
    await state.update_data(autobook_page=page)


async def _render_autobook_card(message: Message, state: FSMContext, autobook_id: int) -> None:
    data = await state.get_data()
    tasks = data.get("autobook_tasks") or []
    task = next((t for t in tasks if t.get("id") == autobook_id), None)
    if not task:
        msg = await message.answer(
            "Задача автобронирования не найдена.", reply_markup=get_main_menu_keyboard()
        )
        await _add_autobook_message_id(msg, state)
        return

    warehouse = task.get("warehouse")
    supply_type = task.get("supply_type")
    max_coef = task.get("max_coef")
    status = task.get("status")
    slot_task_id = task.get("slot_search_task_id")

    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    status_emoji = {
        "active": "🟢",
        "stopped": "⏸",
        "completed": "⚪️",
        "error": "🔴",
    }.get(status, "⚙️")

    text = (
        f"{status_emoji} Автобронирование #{autobook_id}\n\n"
        f"По задаче поиска #{slot_task_id}\n"
        f"Склад: {warehouse}\n"
        f"Тип поставки: {supply_type_text}\n"
        f"Макс. коэффициент: x{max_coef}\n"
        f"Статус: {status}"
    )

    kb_rows = []

    if status == "active":
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="⏸ Остановить",
                    callback_data=f"autobook_stop:{autobook_id}",
                )
            ]
        )
    elif status == "stopped":
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="▶️ Запустить",
                    callback_data=f"autobook_start:{autobook_id}",
                )
            ]
        )

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=f"autobook_delete:{autobook_id}",
            )
        ]
    )

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад к списку",
                callback_data="autobook_back_to_list",
            )
        ]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _clear_autobook_messages(message, state)
    msg = await message.answer(text, reply_markup=kb)
    await _add_autobook_message_id(msg, state)


async def _send_slot_tasks_page(message: Message, state: FSMContext, page: int = 0) -> None:
    data = await state.get_data()
    tasks = data.get("slot_tasks") or []

    if not tasks:
        msg = await message.answer(
            "У тебя пока нет задач на поиск слотов.",
            reply_markup=get_main_menu_keyboard(),
        )
        await _add_slot_tasks_message_id(msg, state)
        return

    total = len(tasks)
    total_pages = (total - 1) // PAGE_SIZE + 1
    page = max(0, min(page, total_pages - 1))

    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_tasks = tasks[start:end]

    lines = [f"📋 Твои задачи (страница {page+1} из {total_pages}):\n"]

    def fmt_date(value: str) -> str:
        if not value:
            return "-"
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y")
        except Exception:
            return str(value)

    for t in page_tasks:
        task_id = t.get("id")
        warehouse = t.get("warehouse")
        supply_type = t.get("supply_type")
        max_coef = t.get("max_coef")
        max_logistics = t.get("max_logistics_coef_percent")
        date_from = fmt_date(t.get("date_from"))
        date_to = fmt_date(t.get("date_to"))
        status = t.get("status")

        supply_type_text = {
            "box": "Короба",
            "mono": "Монопаллеты",
            "postal": "Поштучная паллета",
            "safe": "Суперсейф",
        }.get(supply_type, str(supply_type))

        status_emoji = {
            "active": "🟢",
            "cancelled": "🔴",
            "completed": "⚪️",
        }.get(status, "⚙️")

        if max_logistics is None:
            logistics_line = ""
        else:
            logistics_line = f", логистика: ≤{max_logistics}%"

        lines.append(
            f"{status_emoji} #{task_id} — {warehouse}, {supply_type_text}, x{max_coef}{logistics_line}\n"
            f"   Период: {date_from} → {date_to}\n"
        )

    text = "\n".join(lines)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"slot_tasks_page:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️ Далее", callback_data=f"slot_tasks_page:{page+1}"))

    kb_rows = []
    for t in page_tasks:
        task_id = t.get("id")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"Открыть задачу #{task_id}", callback_data=f"slot_task_open:{task_id}"
                )
            ]
        )

    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="slot_tasks_main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _clear_slot_tasks_messages(message, state)
    msg = await message.answer(text, reply_markup=kb)
    await _add_slot_tasks_message_id(msg, state)
    await state.update_data(slot_tasks_page=page)


async def _render_slot_task_card(message: Message, state: FSMContext, task_id: int) -> None:
    """
    Рендерит одну карточку задачи поиска слотов по её id.
    Использует список slot_tasks из FSM и показывает актуальный статус и кнопки.
    """
    data = await state.get_data()
    tasks = data.get("slot_tasks") or []
    task = next((t for t in tasks if t.get("id") == task_id), None)
    if not task:
        await message.answer("Задача не найдена.", reply_markup=get_main_menu_keyboard())
        return

    warehouse = task.get("warehouse")
    supply_type = task.get("supply_type")
    max_coef = task.get("max_coef")
    max_logistics = task.get("max_logistics_coef_percent")
    date_from = task.get("date_from")
    date_to = task.get("date_to")
    lead_time_days = task.get("lead_time_days")
    weekdays = task.get("weekdays")
    status = task.get("status")

    def fmt_date(value: str) -> str:
        if not value:
            return "-"
        try:
            return datetime.fromisoformat(value).strftime("%d.%m.%Y")
        except Exception:
            return str(value)

    date_from_f = fmt_date(date_from)
    date_to_f = fmt_date(date_to)

    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    weekdays_text = {
        "daily": "Ежедневно",
        "weekdays": "Только будни",
        "weekends": "Только выходные",
    }.get(weekdays, str(weekdays))

    status_emoji = {
        "active": "🟢",
        "cancelled": "🔴",
        "completed": "⚪️",
    }.get(status, "⚙️")

    if max_logistics is None:
        logistics_line = ""
    else:
        logistics_line = f"\nЛогистический коэффициент: до {max_logistics}%"

    text = (
        f"{status_emoji} Задача #{task_id}\n\n"
        f"Склад: {warehouse}\n"
        f"Тип поставки: {supply_type_text}\n"
        f"Макс. коэффициент приёмки: x{max_coef}"
        f"{logistics_line}\n"
        f"Период: {date_from_f} → {date_to_f}\n"
        f"Лид-тайм: {lead_time_days} дн.\n"
        f"Дни недели: {weekdays_text}\n"
        f"Статус: {status}"
    )

    kb_rows = []

    action_buttons = []
    if status == "active":
        action_buttons.append(
            InlineKeyboardButton(
                text="🤖 Автобронировать",
                callback_data=f"autobook_from_search:{task_id}",
            )
        )
        action_buttons.append(
            InlineKeyboardButton(
                text="❌ Отменить",
                callback_data=f"slot_cancel:{task_id}",
            )
        )
    elif status == "cancelled":
        action_buttons.append(
            InlineKeyboardButton(
                text="🔁 Запустить заново",
                callback_data=f"slot_restart:{task_id}",
            )
        )

    action_buttons.append(
        InlineKeyboardButton(
            text="🗑 Удалить",
            callback_data=f"slot_delete:{task_id}",
        )
    )

    if action_buttons:
        kb_rows.append(action_buttons)

    kb_rows.append(
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_slot_tasks")]
    )
    kb_rows.append(
        [
            InlineKeyboardButton(
                text="🤖 Настроить автоброни", callback_data=f"slot_auto_{task_id}"
            )
        ]
    )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb_rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад к списку",
                callback_data="slot_tasks_back_to_list",
            )
        ]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _clear_slot_tasks_messages(message, state)
    msg = await message.answer(text, reply_markup=kb)
    await _add_slot_tasks_message_id(msg, state)


async def cmd_start(message: Message, state: FSMContext) -> None:
    """
    /start:
    1) регистрируем пользователя в нашем backend
    2) шлём приветствие
    """
    await clear_all_ui(message, state)
    async with httpx.AsyncClient() as client:
        try:
            await client.post(
                f"{BACKEND_URL}/users/register",
                json={
                    "telegram_id": message.from_user.id,
                    "username": message.from_user.username,
                },
                timeout=5.0,
            )
        except Exception as e:
            # На этом спринте можно просто залогировать, но не падать
            print(f"Error calling /users/register: {e}")

    await send_main_menu(message, state)


async def wb_auth_phone_step(message: Message, state: FSMContext) -> None:
    phone_raw = message.text.strip()
    telegram_id = message.from_user.id

    await clear_all_ui(message, state)

    kb_main = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
    )

    normalized = normalize_phone(phone_raw)
    if not normalized:
        msg_err = await message.answer(
            "Номер должен быть российским формата:\n"
            "8951…, +7951…, 7951…, или просто 951…\n\n"
            "Итог: должен получиться номер из 10 цифр.",
            reply_markup=kb_main,
        )
        await add_ui_message(state, msg_err.message_id)
        return

    # --- отправляем запрос ---
    try:
        waiting_msg = await message.answer(
            "Вводим номер, подождите..",
            reply_markup=ReplyKeyboardRemove(),
        )
        await add_ui_message(state, waiting_msg.message_id)

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{BACKEND_URL}/auth/start",
                json={
                    "telegram_id": telegram_id,
                    "username": message.from_user.username,
                    "phone": normalized
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as e:
        print("Error calling /auth/start:", e)
        msg = await message.answer("Сервер не отвечает. Попробуй позже.", reply_markup=kb_main)
        await add_ui_message(state, msg.message_id)
        return

    # --- пользователь уже авторизован ---
    if payload.get("status") == "already_authorized":
        msg = await message.answer(
            "Ты уже авторизован в кабинете WB ✅",
            reply_markup=kb_main,
        )
        await add_ui_message(state, msg.message_id)
        await state.clear()
        return

    # --- не получили session_id ---
    session_id = payload.get("session_id")
    if not session_id:
        msg = await message.answer(
            "WB не принял номер или вернул неверный ответ. Попробуй снова.",
            reply_markup=kb_main,
        )
        await add_ui_message(state, msg.message_id)
        return

    # сохраняем
    await state.update_data(phone=normalized, session_id=session_id)
    await state.set_state(WbAuthState.wait_code)

    # удаляем сообщение ожидания
    await delete_ui_message(message, state, waiting_msg.message_id)

    msg = await message.answer(
        "Отлично! Введи код из СМС.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await add_ui_message(state, msg.message_id)


async def wb_auth_code_step(message: Message, state: FSMContext) -> None:
    code = message.text.strip()

    await clear_all_ui(message, state)

    kb_main = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
    )

    if not code.isdigit():
        msg_err = await message.answer("Код должен содержать только цифры.", reply_markup=kb_main)
        await add_ui_message(state, msg_err.message_id)
        return

    data = await state.get_data()
    session_id = data.get("session_id")
    telegram_id = message.from_user.id

    if not session_id:
        msg_err = await message.answer("Не найдена сессия авторизации. Начни заново.", reply_markup=kb_main)
        await add_ui_message(state, msg_err.message_id)
        await state.clear()
        return

    waiting_msg = None
    try:
        waiting_msg = await message.answer(
            "Вводим код, подождите..",
            reply_markup=ReplyKeyboardRemove(),
        )
        await add_ui_message(state, waiting_msg.message_id)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{BACKEND_URL}/auth/code",
                json={"session_id": session_id, "code": code},
            )
            resp.raise_for_status()
            payload = resp.json()
            user_sessions[telegram_id] = session_id
    except Exception as e:
        if waiting_msg:
            await delete_ui_message(message, state, waiting_msg.message_id)
        print("Error calling /auth/code:", e)
        msg_err = await message.answer("Ошибка подтверждения кода. Попробуй снова.", reply_markup=kb_main)
        await add_ui_message(state, msg_err.message_id)
        return

    if waiting_msg:
        await delete_ui_message(message, state, waiting_msg.message_id)

    if payload.get("status") != "authorized":
        msg_err = await message.answer("Код неверный. Попробуй снова.", reply_markup=kb_main)
        await add_ui_message(state, msg_err.message_id)
        return

    await state.clear()

    # msg = await message.answer(
    #     "Готово! Ты успешно авторизован в WB ✅",
    #     reply_markup=kb_main,
    # )
    # await add_ui_message(state, msg.message_id)
    if payload.get("status") in ("authorized", "ok"):
        # сохраняем session id навсегда
        user_sessions[telegram_id] = session_id

        await state.clear()
        msg = await message.answer("Готово! Ты успешно авторизован в WB ✅", reply_markup=kb_main)
        await add_ui_message(state, msg.message_id)
        return



async def _fetch_wb_auth_status(telegram_id: int) -> bool | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BACKEND_URL}/wb/auth/status", params={"telegram_id": telegram_id}
            )
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("authorized")
    except Exception as e:
        print("Error calling /wb/auth/status:", e)
        return None


async def _do_wb_status(message: Message, state: FSMContext, telegram_id: int) -> None:
    authorized = await _fetch_wb_auth_status(telegram_id)
    if authorized is None:
        msg = await message.answer("Не удалось получить статус WB. Попробуй позже.")
        await add_ui_message(state, msg.message_id)
        return

    text = "Статус WB: авторизован ✅" if authorized else "Статус WB: не авторизован ❌"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def cmd_wb_status(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)
    await _do_wb_status(message, state, message.from_user.id)


async def _do_wb_logout(message: Message, state: FSMContext, telegram_id: int) -> None:
    authorized = await _fetch_wb_auth_status(telegram_id)
    if authorized is False:
        msg = await message.answer(
            "Ты не авторизован в WB. Перейди в меню → Авторизация WB"
        )
        await add_ui_message(state, msg.message_id)
        return
    if authorized is None:
        msg = await message.answer("Не удалось получить статус WB. Попробуй позже.")
        await add_ui_message(state, msg.message_id)
        return

    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{BACKEND_URL}/logout",
                json={"telegram_id": telegram_id},
                timeout=5.0,
            )
            if resp.status_code == 404:
                msg = await message.answer("Ты и так не авторизован в WB.")
                await add_ui_message(state, msg.message_id)
                return
            if resp.status_code == 422:
                detail = resp.json().get("detail")
                detail_text = "Неверные данные запроса." if detail is None else str(detail)
                msg = await message.answer(
                    f"Не удалось выполнить выход из WB: {detail_text}"
                )
                await add_ui_message(state, msg.message_id)
                return
            resp.raise_for_status()
        except Exception:
            msg = await message.answer("Не удалось выполнить выход из WB, попробуй позже.")
            await add_ui_message(state, msg.message_id)
            return

    msg = await message.answer(
        "Ты вышел из кабинета WB. При необходимости можешь заново авторизоваться через меню «Авторизация WB».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
        ),
    )
    await add_ui_message(state, msg.message_id)


async def cmd_wb_logout(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)
    await _do_wb_logout(message, state, message.from_user.id)


async def on_warehouse_page(callback: CallbackQuery, state: FSMContext):
    await callback.answer()

    _, page_str = callback.data.split(":")
    page = int(page_str)

    # Загружаем новую страницу
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/warehouses",
                params={"page": page, "limit": 10}
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error /warehouses:", e)
        return

    # Берём старый wh_map (может содержать данные прошлых страниц)
    fs = await state.get_data()
    old_map = fs.get("wh_map", {})

    # Создаем map для новой страницы
    new_map = {w["id"]: w["name"] for w in data["items"]}

    # Объединяем, НЕ перезаписывая прежние данные
    combined_map = {**old_map, **new_map}

    # Сохраняем ВСЁ
    await state.update_data(
        wh_items=data["items"],
        wh_page=data["page"],
        wh_pages=data["pages"],
        wh_map=combined_map,
    )

    await clear_all_ui(callback.message, state)
    await _render_warehouse_page(callback.message, state)

async def _render_warehouse_page(message: Message, state: FSMContext):
    data = await state.get_data()
    items = data.get("wh_items", [])
    page = data.get("wh_page", 0)
    pages = data.get("wh_pages", 1)

    rows = []
    for w in items:
        rows.append([
            InlineKeyboardButton(
                text=w["name"],
                callback_data=f"slot_wh_id:{w['id']}"
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"wh_page:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"wh_page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    msg = await message.answer(
        "Шаг 1 из 7 — выбор склада.\n\nВыбери склад:",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

async def cmd_create_search(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)

    # грузим первую страницу складов
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BACKEND_URL}/warehouses", params={"page": 0, "limit": 10})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error GET /warehouses:", e)
        msg = await message.answer("Не удалось загрузить список складов.")
        await add_ui_message(state, msg.message_id)
        return

    await state.update_data(
        wh_items=data["items"],
        wh_page=data["page"],
        wh_pages=data["pages"],
        wh_map={w["id"]: w["name"] for w in data["items"]}
    )

    await _render_warehouse_page(message, state)
    await state.set_state(SlotSearchState.warehouse)


async def handle_main_menu_create_search(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await _do_main_menu_create_search(message, state, telegram_id)


async def _do_main_menu_create_search(message: Message, state: FSMContext, telegram_id: int) -> None:
    await clear_all_ui(message, state)
    await cmd_create_search(message, state)


async def _show_tasks_menu(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Задачи по поиску", callback_data="tasks_history_search")],
            [InlineKeyboardButton(text="Задачи по автоброни", callback_data="tasks_history_autobook")],
            [InlineKeyboardButton(text="Назад", callback_data="menu_main")],
        ]
    )

    msg = await message.answer("📋 Мои задачи\n\nВыбери нужный раздел:", reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def _render_tasks_history(
    message: Message, state: FSMContext, telegram_id: int, req_type: str, page: int = 1
) -> None:
    await clear_all_ui(message, state)

    user_id = await _get_user_id(telegram_id)
    if not user_id:
        kb_err = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        msg_err = await message.answer(
            "Не удалось определить пользователя. Попробуй позже.", reply_markup=kb_err
        )
        await add_ui_message(state, msg_err.message_id)
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/requests/history",
                params={
                    "user_id": user_id,
                    "req_type": req_type,
                    "page": page,
                    "page_size": HISTORY_PAGE_SIZE,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error calling /requests/history:", e)
        kb_err = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="↩️ Назад", callback_data="menu_tasks")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        msg_err = await message.answer(
            "Не удалось получить список задач. Попробуй позже.", reply_markup=kb_err
        )
        await add_ui_message(state, msg_err.message_id)
        return

    items = data.get("items") or []
    total = data.get("total", len(items))
    page_num = data.get("page") or page or 1
    page_size = data.get("page_size") or HISTORY_PAGE_SIZE

    total_pages = (total - 1) // page_size + 1 if total else 1
    page_num = max(1, min(page_num, total_pages))

    titles = {
        "slot_search": "Задачи по поиску",
        "auto_booking": "Задачи по автоброни",
    }

    await state.update_data(
        **{
            f"tasks_history_{req_type}": {
                "items": items,
                "page": page_num,
                "total_pages": total_pages,
            }
        }
    )

    lines = [
        f"📋 {titles.get(req_type, 'Задачи')}".strip(),
        f"Страница {page_num} из {total_pages}",
    ]

    kb_rows = []
    if items:
        if req_type == "slot_search":
            for item in items:
                item_id = item.get("id")
                warehouse = item.get("warehouse") or "-"
                supply_type = item.get("supply_type") or "-"
                status = item.get("status") or "-"
                found = item.get("found", 0)
                button_text = f"#{item_id} • {warehouse}, {supply_type} — {status}, найдено: {found}"
                kb_rows.append(
                    [
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=f"tasks_history_slot_search_open:{item_id}",
                        )
                    ]
                )
        else:
            for item in items:
                item_id = item.get("id")
                seller = item.get("seller_name") or "-"
                draft_id = item.get("draft_id") or "-"
                created_at = item.get("created_at") or "-"
                button_text = f"#{item_id} • {seller} — черновик {draft_id}, создано: {created_at}"
                kb_rows.append(
                    [
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=f"tasks_history_auto_booking_open:{item_id}",
                        )
                    ]
                )
    else:
        lines.append("Пока нет задач этого типа.")

    text = "\n".join(lines).rstrip()

    nav_buttons = []

    if page_num > 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️", callback_data=f"tasks_history_{req_type}_page:{page_num-1}"
            )
        )
    if page_num < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(
                text="▶️", callback_data=f"tasks_history_{req_type}_page:{page_num+1}"
            )
        )

    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append([InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def _render_slot_history_detail(
    message: Message, state: FSMContext, request_id: int
) -> None:
    await clear_all_ui(message, state)

    data = await state.get_data()
    history = data.get("tasks_history_slot_search") or {}
    items = history.get("items") or []
    item = next((i for i in items if i.get("id") == request_id), None)

    if not item:
        msg = await message.answer(
            "Не удалось найти задачу. Попробуй обновить список.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
                ]
            ),
        )
        await add_ui_message(state, msg.message_id)
        return

    warehouse = item.get("warehouse") or "-"
    supply_type = item.get("supply_type") or "-"
    status = item.get("status") or "-"
    found = item.get("found", 0)
    period = item.get("period") or {}
    period_from = period.get("from") or "-"
    period_to = period.get("to") or "-"
    lead_time = item.get("lead_time_days")
    weekdays = item.get("weekdays") or "-"
    max_coef = item.get("max_coef")
    max_logistics = item.get("max_logistics_coef_percent")

    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    weekdays_text = {
        "daily": "Ежедневно",
        "weekdays": "Только будни",
        "weekends": "Только выходные",
    }.get(weekdays, str(weekdays))

    lines = [
        f"🔎 Задача поиска #{request_id}",
        f"Склад: {warehouse}",
        f"Тип поставки: {supply_type_text}",
        f"Статус: {status}",
        f"Найдено слотов: {found}",
        f"Период: {period_from} → {period_to}",
    ]

    if lead_time is not None:
        lines.append(f"Лид-тайм: {lead_time} дн.")
    if weekdays_text:
        lines.append(f"Дни недели: {weekdays_text}")
    if max_coef is not None:
        lines.append(f"Коэффициент приёмки: x{max_coef}")
    if max_logistics is not None:
        lines.append(f"Логистика: до {max_logistics}%")

    kb_rows = []

    if status == "pending":
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text="⛔️ Отменить поиск",
                    callback_data=f"tasks_history_slot_search_cancel:{request_id}",
                )
            ]
        )

    kb_rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tasks_history_slot_search_page:{history.get('page', 1)}")]
    )
    kb_rows.append([InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    msg = await message.answer("\n".join(lines), reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def _render_autobook_history_detail(
    message: Message, state: FSMContext, request_id: int
) -> None:
    await clear_all_ui(message, state)

    data = await state.get_data()
    history = data.get("tasks_history_auto_booking") or {}
    items = history.get("items") or []
    item = next((i for i in items if i.get("id") == request_id), None)

    if not item:
        msg = await message.answer(
            "Не удалось найти задачу автобронирования.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
                ]
            ),
        )
        await add_ui_message(state, msg.message_id)
        return

    seller = item.get("seller_name") or "-"
    draft_id = item.get("draft_id") or "-"
    created_at = item.get("created_at") or "-"
    status = item.get("status") or "-"

    lines = [
        f"🤖 Задача автоброни #{request_id}",
        f"Продавец: {seller}",
        f"Черновик: {draft_id}",
        f"Статус: {status}",
        f"Создано: {created_at}",
    ]

    kb_rows = [
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"tasks_history_auto_booking_page:{history.get('page', 1)}")],
        [InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
    ]

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    msg = await message.answer("\n".join(lines), reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def _do_main_menu_my_searches(message: Message, state: FSMContext, telegram_id: int) -> None:
    await _render_tasks_history(message, state, telegram_id, "slot_search", page=1)


async def handle_main_menu_my_searches(message: Message, state: FSMContext) -> None:
    await _show_tasks_menu(message, state)


async def _do_main_menu_autobook_list(message: Message, state: FSMContext, telegram_id: int) -> None:
    await clear_all_ui(message, state)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/autobook/list",
                params={"telegram_id": telegram_id},
            )
            resp.raise_for_status()
            tasks = resp.json()
    except Exception as e:
        print("Error calling /autobook/list:", e)
        msg_err = await message.answer("Не удалось получить список задач автобронирования. Попробуй позже.")
        await add_ui_message(state, msg_err.message_id)
        return

    if not tasks:
        msg = await message.answer(
            "У тебя пока нет задач автобронирования.\n\n"
            "Создай их в разделе «📋 Мои задачи», нажав «Автобронирование» под нужной задачей.",
        )
        await _add_autobook_message_id(msg, state)
        await add_ui_message(state, msg.message_id)
        return

    await state.update_data(autobook_tasks=tasks, autobook_page=0, autobook_message_ids=[])

    await _send_autobook_page(message, state, page=0)


async def handle_main_menu_autobook_list(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await _do_main_menu_autobook_list(message, state, telegram_id)


async def open_autobook_menu(message: Message, state: FSMContext) -> None:
    await handle_main_menu_autobook_list(message, state)


async def handle_main_menu_help(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)
    text = (
        "Вот что я умею:\n\n"
        "🟢 Поиск слота — создать новую задачу на поиск выгодных слотов.\n"
        "📋 Мои задачи — посмотреть активные и завершённые задачи, отменить или запустить заново.\n"
        "⚙️ Авторизация WB — привязать твой аккаунт Wildberries к боту.\n"
        "📊 Статус WB — проверить, авторизован ли ты сейчас в кабинете WB.\n"
        "🚪 Выйти из WB — разлогиниться из кабинета WB.\n"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )
    msg = await message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def start_wb_auth_flow(message: Message, state: FSMContext, telegram_id: int) -> None:
    """
    Запуск inline-масштаба авторизации WB: очищает UI, сбрасывает состояние и просит телефон.
    """
    await clear_all_ui(message, state)
    await state.clear()
    await state.set_state(WbAuthState.wait_phone)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )
    msg = await message.answer(
        "Давай авторизуемся в кабинете WB.\n\n"
        "Введи номер телефона в формате 7XXXXXXXXXX.",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)


async def _do_main_menu_status(message: Message, state: FSMContext, telegram_id: int) -> None:
    await clear_all_ui(message, state)
    await _do_wb_status(message, state, telegram_id)


async def _do_main_menu_logout(message: Message, state: FSMContext, telegram_id: int) -> None:
    await clear_all_ui(message, state)
    await _do_wb_logout(message, state, telegram_id)


async def handle_main_menu_auth(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    authorized = await _fetch_wb_auth_status(telegram_id)

    if authorized:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚪 Выйти из WB", callback_data="menu_logout")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        msg = await message.answer(
            "Ты уже авторизован в WB ✅\nЧтобы войти под другим аккаунтом, сначала выйди.",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        return

    if authorized is None:
        msg = await message.answer("Не удалось получить статус WB. Попробуй позже.")
        await add_ui_message(state, msg.message_id)
        return

    await start_wb_auth_flow(message, state, telegram_id)


async def handle_main_menu_status(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await _do_main_menu_status(message, state, telegram_id)


async def handle_main_menu_logout(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await _do_main_menu_logout(message, state, telegram_id)


async def menu_search_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _do_main_menu_create_search(callback.message, state, callback.from_user.id)


async def menu_tasks_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_tasks_menu(callback.message, state)


async def tasks_history_search_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _render_tasks_history(callback.message, state, callback.from_user.id, "slot_search", page=1)


async def tasks_history_autobook_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _render_tasks_history(callback.message, state, callback.from_user.id, "auto_booking", page=1)


async def tasks_history_page_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        prefix, page_str = data_cb.split(":", 1)
        page = int(page_str)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    handled = False
    if prefix.startswith("tasks_history_slot_search_page"):
        await _render_tasks_history(callback.message, state, callback.from_user.id, "slot_search", page=page)
        handled = True
    elif prefix.startswith("tasks_history_auto_booking_page"):
        await _render_tasks_history(callback.message, state, callback.from_user.id, "auto_booking", page=page)
        handled = True

    if handled:
        await callback.answer()
    else:
        await callback.answer("Неизвестный тип задач.", show_alert=True)


async def tasks_history_slot_search_open_callback(
    callback: CallbackQuery, state: FSMContext
) -> None:
    data_cb = callback.data or ""
    try:
        _, request_id_str = data_cb.split(":", 1)
        request_id = int(request_id_str)
    except Exception:
        await callback.answer("Некорректная задача.", show_alert=True)
        return

    await callback.answer()
    await _render_slot_history_detail(callback.message, state, request_id)


async def tasks_history_autobook_open_callback(
    callback: CallbackQuery, state: FSMContext
) -> None:
    data_cb = callback.data or ""
    try:
        _, request_id_str = data_cb.split(":", 1)
        request_id = int(request_id_str)
    except Exception:
        await callback.answer("Некорректная задача.", show_alert=True)
        return

    await callback.answer()
    await _render_autobook_history_detail(callback.message, state, request_id)


async def tasks_history_slot_search_cancel_callback(
    callback: CallbackQuery, state: FSMContext
) -> None:
    data_cb = callback.data or ""
    try:
        _, request_id_str = data_cb.split(":", 1)
        request_id = int(request_id_str)
    except Exception:
        await callback.answer("Некорректная задача.", show_alert=True)
        return

    await callback.answer()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{BACKEND_URL}/slots/search/{request_id}/cancel")
            resp.raise_for_status()
    except Exception as e:
        print(f"Error calling /slots/search/{request_id}/cancel:", e)
        msg_err = await callback.message.answer(
            "Не удалось отменить поиск. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📋 Мои задачи", callback_data="menu_tasks")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
                ]
            ),
        )
        await add_ui_message(state, msg_err.message_id)
        return

    data = await state.get_data()
    history = data.get("tasks_history_slot_search") or {}
    items = history.get("items") or []
    for i in items:
        if i.get("id") == request_id:
            i["status"] = "cancelled"
            break

    await state.update_data(
        **{"tasks_history_slot_search": {**history, "items": items}}
    )

    await _render_slot_history_detail(callback.message, state, request_id)


async def menu_autobook_new_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await clear_all_ui(callback.message, state)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список автобронирований", callback_data="autobook_menu:list")],
            [InlineKeyboardButton(text="➕ Создать автобронь", callback_data="autobook_menu:create")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )

    msg = await callback.message.answer("🚀 Автобронь\n\nВыбери действие:", reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def autobook_menu_list_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _do_main_menu_autobook_list(callback.message, state, callback.from_user.id)


async def autobook_menu_create_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    telegram_id = callback.from_user.id
    wait_msg = await callback.message.answer("Загружаем ваши аккаунты, подождите..")
    await add_ui_message(state, wait_msg.message_id)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp_user = await client.get(
                f"{BACKEND_URL}/users/get-id",
                params={"telegram_id": telegram_id},
            )
            resp_user.raise_for_status()
            user_id = resp_user.json().get("user_id")
            if user_id is None:
                raise ValueError("user_id is missing in /users/get-id response")
    except Exception as e:
        print("Error calling /users/get-id:", e)
        await wait_msg.edit_text(
            "Не удалось получить данные пользователя. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
            ),
        )
        return

    await _autobook_render_accounts(wait_msg, state, user_id)


async def _autobook_render_accounts(
    message_obj: Message, state: FSMContext, user_id: int, page: int = 1
) -> None:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/wb/accounts",
                params={
                    "user_id": user_id,
                    "page": page,
                    "per_page": AUTBOOK_ACCOUNTS_PAGE_SIZE,
                },
            )
            resp.raise_for_status()
            accounts_resp = resp.json() or {}
    except Exception as e:
        print("Error calling /wb/accounts:", e)
        await message_obj.edit_text(
            "Не удалось загрузить аккаунты. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
            ),
        )
        return

    try:
        page_num = int(accounts_resp.get("page", page))
    except Exception:
        page_num = page

    try:
        per_page = int(accounts_resp.get("per_page", AUTBOOK_ACCOUNTS_PAGE_SIZE))
    except Exception:
        per_page = AUTBOOK_ACCOUNTS_PAGE_SIZE

    try:
        total = int(accounts_resp.get("total", 0))
    except Exception:
        total = 0

    total_pages = (total - 1) // per_page + 1 if total else 1
    page_num = max(1, min(page_num, total_pages))

    accounts = accounts_resp.get("items") or []

    await state.update_data(
        autobook_accounts=accounts,
        autobook_user_id=user_id,
        autobook_accounts_page=page_num,
        autobook_accounts_pagination={
            "page": page_num,
            "per_page": per_page,
            "total": total,
            "pages": total_pages,
        },
    )

    if not accounts:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="autobook_new_refresh")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        await message_obj.edit_text(
            "Не найдено аккаунтов. Обнови список и попробуй снова.", reply_markup=kb
        )
        await state.set_state(AutoBookNewState.choose_account)
        return

    text_lines = [
        "Атобронировние",
        f"Страница {page_num} из {total_pages}",
        "",
        "Выберите аккаунт:\n",
    ]
    kb_rows = []
    for acc in accounts:
        acc_id = acc.get("id")
        acc_name = acc.get("name") or str(acc_id)
        text_lines.append(f"• {acc_name}")
        kb_rows.append(
            [InlineKeyboardButton(text=acc_name, callback_data=f"autobook_new_account:{acc_id}")]
        )

    nav_buttons = []
    if page_num > 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"autobook_accounts_page:{page_num - 1}"
            )
        )
    if page_num < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Вперед ▶️", callback_data=f"autobook_accounts_page:{page_num + 1}"
            )
        )
    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="autobook_new_refresh")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await message_obj.edit_text("\n".join(text_lines), reply_markup=kb)
    except Exception:
        prev_mid = message_obj.message_id
        new_msg = await message_obj.answer("\n".join(text_lines), reply_markup=kb)
        await add_ui_message(state, new_msg.message_id)
        try:
            await message_obj.bot.delete_message(chat_id=message_obj.chat.id, message_id=prev_mid)
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass

    await state.set_state(AutoBookNewState.choose_account)


async def on_autobook_new_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    user_id = data.get("autobook_user_id")
    page = data.get("autobook_accounts_page", 1)

    try:
        page = int(page)
    except Exception:
        page = 1

    if user_id is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp_user = await client.get(
                    f"{BACKEND_URL}/users/get-id",
                    params={"telegram_id": callback.from_user.id},
                )
                resp_user.raise_for_status()
                user_id = resp_user.json().get("user_id")
        except Exception as e:
            print("Error calling /users/get-id on refresh:", e)
            await callback.answer("Не удалось обновить аккаунты.", show_alert=True)
            return

    await callback.answer()
    await callback.message.edit_text("Обновляем список аккаунтов...")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp_sync = await client.post(
                f"{BACKEND_URL}/wb/accounts/sync",
                params={"user_id": user_id},
                headers={"accept": "application/json"},
                data="",
            )
            resp_sync.raise_for_status()
    except Exception as e:
        print("Error calling /wb/accounts/sync on refresh:", e)
        await callback.message.edit_text(
            "Не удалось обновить аккаунты. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
            ),
        )
        return

    await _autobook_render_accounts(callback.message, state, user_id, page=page)


async def on_autobook_accounts_page(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, page_raw = data_cb.split(":", 1)
        page = int(page_raw)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    data = await state.get_data()
    user_id = data.get("autobook_user_id")

    if user_id is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp_user = await client.get(
                    f"{BACKEND_URL}/users/get-id",
                    params={"telegram_id": callback.from_user.id},
                )
                resp_user.raise_for_status()
                user_id = resp_user.json().get("user_id")
        except Exception:
            await callback.answer("Не удалось получить пользователя.", show_alert=True)
            return

    await callback.answer()

    try:
        await callback.message.edit_text("Загружаем список аккаунтов, подождите..")
    except Exception:
        prev_mid = callback.message.message_id
        loading_msg = await callback.message.answer(
            "Загружаем список аккаунтов, подождите.."
        )
        await add_ui_message(state, loading_msg.message_id)
        callback.message = loading_msg
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_mid
            )
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass

    await _autobook_render_accounts(callback.message, state, user_id, page=page)


async def _autobook_send_drafts(message_obj: Message, state: FSMContext) -> None:
    data = await state.get_data()
    drafts = data.get("autobook_drafts") or []
    pagination = data.get("autobook_drafts_pagination") or {}
    try:
        page_num = int(pagination.get("page", 1))
    except Exception:
        page_num = 1
    try:
        total_pages = int(pagination.get("pages", 1))
    except Exception:
        total_pages = 1

    if not drafts:
        await message_obj.edit_text(
            "Не найдено черновиков для автобронирования.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
            ),
        )
        await state.clear()
        return

    lines = [
        "Выберите черновик" + (f" (стр. {page_num} из {total_pages})" if total_pages else "")
    ]
    kb_rows = []
    for draft in drafts:
        draft_id = draft.get("id")
        created = draft.get("created_at")
        barcode_qty = draft.get("barcode_quantity")
        good_qty = draft.get("good_quantity")
        author = draft.get("author")
        lines.append(
            f"• #{draft_id} от {created} — товаров: {good_qty}, баркодов: {barcode_qty}, автор: {author}"
        )
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{draft_id} — {created} ({good_qty} шт.)",
                    callback_data=f"autobook_new_draft:{draft_id}",
                )
            ]
        )

    nav_buttons = []
    if page_num > 1:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️ Назад", callback_data=f"autobook_drafts_page:{page_num - 1}"
            )
        )
    if total_pages and page_num < total_pages:
        nav_buttons.append(
            InlineKeyboardButton(
                text="Вперед ▶️", callback_data=f"autobook_drafts_page:{page_num + 1}"
            )
        )
    if nav_buttons:
        kb_rows.append(nav_buttons)

    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await message_obj.edit_text("\n".join(lines), reply_markup=kb)
    except Exception:
        prev_mid = message_obj.message_id
        new_msg = await message_obj.answer("\n".join(lines), reply_markup=kb)
        await add_ui_message(state, new_msg.message_id)
        try:
            await message_obj.bot.delete_message(chat_id=message_obj.chat.id, message_id=prev_mid)
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass
    await state.set_state(AutoBookNewState.choose_draft)


async def _autobook_render_warehouse_page(message_obj: Message, state: FSMContext) -> None:
    data = await state.get_data()
    items = data.get("autobook_wh_items") or []
    page = data.get("autobook_wh_page", 0)
    pages = data.get("autobook_wh_pages", 1)

    rows = []
    for w in items:
        rows.append([
            InlineKeyboardButton(
                text=w.get("name"),
                callback_data=f"autobook_wh_id:{w.get('id')}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"autobook_wh_page:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"autobook_wh_page:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    msg = await message_obj.answer(
        "Шаг 1 из 7 — выбор склада.\n\nВыбери склад:", reply_markup=kb
    )
    await add_ui_message(state, msg.message_id)
    await state.set_state(AutoBookNewState.warehouse)


async def _autobook_load_warehouses(message_obj: Message, state: FSMContext) -> None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{BACKEND_URL}/warehouses", params={"page": 0, "limit": 10})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error GET /warehouses for autobook:", e)
        msg = await message_obj.answer("Не удалось загрузить список складов.")
        await add_ui_message(state, msg.message_id)
        return

    await state.update_data(
        autobook_wh_items=data.get("items"),
        autobook_wh_page=data.get("page"),
        autobook_wh_pages=data.get("pages"),
        autobook_wh_map={w.get("id"): w.get("name") for w in data.get("items", [])},
    )

    await _autobook_render_warehouse_page(message_obj, state)


async def _fetch_overview_page(user_id: int, account_id: int, page: int) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{BACKEND_URL}/wb/overview",
            params={
                "user_id": user_id,
                "seller_account_id": account_id,
                "page": page,
                "per_page": OVERVIEW_PAGE_SIZE,
            },
        )
        resp.raise_for_status()
        return resp.json() or {}


async def on_autobook_wh_page(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, page_raw = data_cb.split(":", 1)
        page = int(page_raw)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/warehouses", params={"page": page, "limit": 10}
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error paging /warehouses for autobook:", e)
        await callback.answer("Не удалось обновить список складов.", show_alert=True)
        return

    await state.update_data(
        autobook_wh_items=data.get("items"),
        autobook_wh_page=data.get("page", page),
        autobook_wh_pages=data.get("pages", 1),
        autobook_wh_map={w.get("id"): w.get("name") for w in data.get("items", [])},
    )

    await callback.answer()
    await _autobook_render_warehouse_page(callback.message, state)


async def on_autobook_warehouse(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/wb/auth/status",
                params={"telegram_id": telegram_id},
            )
            resp.raise_for_status()
            payload = resp.json()
            authorized = bool(payload.get("authorized"))
    except Exception as e:
        print("Error checking WB auth for autobook:", e)
        await callback.message.answer("Не удалось проверить авторизацию WB. Попробуй позже.")
        await callback.answer()
        return

    if not authorized:
        await callback.message.answer(
            "Ты не авторизован в WB ❌\nПерейди в меню → Авторизация WB"
        )
        await callback.answer()
        return

    await callback.answer()
    await clear_all_ui(callback.message, state)

    try:
        _, wh_id_str = callback.data.split(":", 1)
        wh_id = int(wh_id_str)
    except Exception:
        await callback.message.answer("Ошибка: неверный ID склада.")
        return

    data = await state.get_data()
    name_map = data.get("autobook_wh_map", {})

    warehouse_name = name_map.get(wh_id)
    if not warehouse_name:
        await callback.message.answer("Ошибка: склад не найден. Попробуй снова.")
        return

    await state.update_data(warehouse=warehouse_name)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Короба", callback_data="autobook_supply:box"),
                InlineKeyboardButton(text="🟫 Монопаллеты", callback_data="autobook_supply:mono"),
            ],
            [
                InlineKeyboardButton(text="✉️ Поштучная паллета", callback_data="autobook_supply:postal"),
                InlineKeyboardButton(text="🛡 Суперсейф", callback_data="autobook_supply:safe"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:warehouse")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 2 из 7 — тип поставки.\n\nВыбери один из вариантов:",
        reply_markup=kb,
    )

    await add_ui_message(state, msg.message_id)
    await state.set_state(AutoBookNewState.supply_type)


async def on_autobook_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    target = (callback.data or "").split(":", 1)[-1]

    if target == "warehouse":
        await clear_all_ui(callback.message, state)
        await _autobook_render_warehouse_page(callback.message, state)
        return

    if target == "supply":
        await clear_all_ui(callback.message, state)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📦 Короба", callback_data="autobook_supply:box"),
                    InlineKeyboardButton(text="🟫 Монопаллеты", callback_data="autobook_supply:mono"),
                ],
                [
                    InlineKeyboardButton(text="✉️ Поштучная паллета", callback_data="autobook_supply:postal"),
                    InlineKeyboardButton(text="🛡 Суперсейф", callback_data="autobook_supply:safe"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:warehouse")],
            ]
        )
        msg = await callback.message.answer(
            "Шаг 2 из 7 — тип поставки.\n\nВыбери один из вариантов:",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(AutoBookNewState.supply_type)
        return

    if target == "coef":
        await clear_all_ui(callback.message, state)
        kb = build_coef_keyboard(0, 20, per_row=4, prefix="autobook_coef")
        msg = await callback.message.answer(
            "Шаг 3 из 7 — максимальный коэффициент.\n\nВыбери максимальный коэффициент бронирования:",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(AutoBookNewState.max_coef)
        return

    if target == "logistics":
        await clear_all_ui(callback.message, state)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="> 100%", callback_data="autobook_log:100"),
                    InlineKeyboardButton(text="≤ 120%", callback_data="autobook_log:120"),
                ],
                [
                    InlineKeyboardButton(text="≤ 140%", callback_data="autobook_log:140"),
                    InlineKeyboardButton(text="≤ 160%", callback_data="autobook_log:160"),
                ],
                [
                    InlineKeyboardButton(text="≤ 180%", callback_data="autobook_log:180"),
                    InlineKeyboardButton(text="Не ограничивать", callback_data="autobook_log:none"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:coef")],
            ]
        )
        msg = await callback.message.answer(
            "Шаг 4 из 7 — логистика.\n\n"
            "Wildberries показывает для разных складов логистический коэффициент в процентах.\n"
            "Выбери максимальный коэффициент логистики, который тебя устраивает:",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(AutoBookNewState.logistics)
        return

    if target == "period":
        await clear_all_ui(callback.message, state)
        await _autobook_show_period_step(callback.message, state)
        return

    if target == "lead":
        await clear_all_ui(callback.message, state)
        await _autobook_show_lead_time_step(callback.message, state)


async def on_autobook_supply(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, supply_type = data_cb.split(":", 1)
    except Exception:
        supply_type = None

    if not supply_type:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(supply_type=supply_type)

    kb = build_coef_keyboard(0, 20, per_row=4, prefix="autobook_coef")

    msg = await callback.message.answer(
        "Шаг 3 из 7 — максимальный коэффициент.\n\n"
        "Выбери максимальный коэффициент бронирования:",
        reply_markup=kb,
    )

    await add_ui_message(state, msg.message_id)
    await state.set_state(AutoBookNewState.max_coef)


async def on_autobook_coef(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    try:
        _, coef_str = callback.data.split(":", 1)
        max_coef = int(coef_str)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(max_coef=max_coef)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="> 100%", callback_data="autobook_log:100"),
                InlineKeyboardButton(text="≤ 120%", callback_data="autobook_log:120"),
            ],
            [
                InlineKeyboardButton(text="≤ 140%", callback_data="autobook_log:140"),
                InlineKeyboardButton(text="≤ 160%", callback_data="autobook_log:160"),
            ],
            [
                InlineKeyboardButton(text="≤ 180%", callback_data="autobook_log:180"),
                InlineKeyboardButton(text="Не ограничивать", callback_data="autobook_log:none"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:coef")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 4 из 7 — логистика.\n\n"
        "Wildberries показывает для разных складов логистический коэффициент в процентах.\n"
        "Выбери максимальный коэффициент логистики, который тебя устраивает:",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)
    await state.set_state(AutoBookNewState.logistics)


async def _autobook_show_period_step(message_obj: Message, state: FSMContext) -> None:
    kb = build_period_keyboard(prefix="autobook_period")
    msg = await message_obj.answer(
        "Шаг 5 из 7 — период поиска.\n\nНа сколько дней вперёд искать слоты?",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)
    await state.update_data(awaiting_manual_period=False)
    await state.set_state(AutoBookNewState.period_days)


async def on_autobook_logistics(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_cb = callback.data or ""
    _, code = data_cb.split(":", 1)
    mapping = {
        "100": 100,
        "120": 120,
        "140": 140,
        "160": 160,
        "180": 180,
        "none": None,
    }
    max_logistics_coef_percent = mapping.get(code)

    await state.update_data(max_logistics_coef_percent=max_logistics_coef_percent)

    await clear_all_ui(callback.message, state)
    await _autobook_show_period_step(callback.message, state)


async def on_autobook_period(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_cb = callback.data or ""
    try:
        _, raw = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    if raw == "manual":
        text = (
            "Отправьте сообщение с периодом ДД.ММ.ГГГГ-ДД.ММ.ГГГГ без пробелов\n"
            "Дефис \"-\" обязателен\n"
            "Пример: 01.01.2025-31.01.2025"
        )
        msg = await callback.message.answer(text)
        await add_ui_message(state, msg.message_id)
        await state.update_data(awaiting_manual_period=True)
        await state.set_state(AutoBookNewState.period_days)
        return

    mapping = {
        "3": 3,
        "7": 7,
        "10": 10,
        "30": 30,
    }

    period_days = mapping.get(raw)
    if period_days is None:
        await send_main_menu(callback.message, state)
        return

    today = date.today()
    search_period_from = today.isoformat()
    search_period_to = (today + timedelta(days=period_days)).isoformat()

    await state.update_data(
        period_days=period_days,
        search_period_from=search_period_from,
        search_period_to=search_period_to,
        awaiting_manual_period=False,
    )

    await _autobook_show_lead_time_step(callback.message, state)


async def on_autobook_period_manual_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("awaiting_manual_period"):
        await message.answer("Пожалуйста, выбери период с помощью кнопок выше.")
        return

    raw = (message.text or "").strip()
    pattern = r"^(\d{2})\.(\d{2})\.(\d{4})-(\d{2})\.(\d{2})\.(\d{4})$"
    match = re.fullmatch(pattern, raw)
    if not match:
        msg = await message.answer(
            "Неверный формат. Введите даты как ДД.ММ.ГГГГ-ДД.ММ.ГГГГ без пробелов."
        )
        await add_ui_message(state, msg.message_id)
        return

    try:
        from_dt = datetime.strptime(".".join(match.group(1, 2, 3)), "%d.%m.%Y").date()
        to_dt = datetime.strptime(".".join(match.group(4, 5, 6)), "%d.%m.%Y").date()
    except ValueError:
        msg = await message.answer("Не удалось распознать даты. Проверь формат и повтори попытку.")
        await add_ui_message(state, msg.message_id)
        return

    if from_dt > to_dt:
        msg = await message.answer("Дата начала должна быть не позже даты окончания периода.")
        await add_ui_message(state, msg.message_id)
        return

    period_days = (to_dt - from_dt).days + 1

    await state.update_data(
        period_days=period_days,
        search_period_from=from_dt.isoformat(),
        search_period_to=to_dt.isoformat(),
        awaiting_manual_period=False,
    )

    await _autobook_show_lead_time_step(message, state)


async def _autobook_show_lead_time_step(message: Message, state: FSMContext) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 день", callback_data="autobook_lead:1"
                ),
                InlineKeyboardButton(
                    text="2 дня", callback_data="autobook_lead:2"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="3 дня", callback_data="autobook_lead:3"
                ),
                InlineKeyboardButton(
                    text="5 дней", callback_data="autobook_lead:5"
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:period")],
        ]
    )

    msg = await message.answer(
        "Шаг 6 из 7 — Лид тайм поставки.\n\n"
        "Укажите срок необходимый вам для подготовки отгрузки (лид- тайм):\n"
        "Дата сдвигается ежедневно\n"
        "Этот период - запас времени, который необходим вам, чтобы успеть сдать поставку\n"
        "Поможет избежать поиска поставок, которые вы не сможете отгрузить\n"
        "При выборе 0 дней, бот будет искать поставки день в день\n"
        "WВ примет у вас поставку с тем же коэффициентом, если вы привезёте её в течении 24 часов после запланированной даты\n"
        "Как правило самые низкие коэффициенты появляются за 0-2 дня до даты приемки, т.к. селлеры начинают массово отменять поставки, которые бронировали заранее.\n",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(AutoBookNewState.lead_time)


async def on_autobook_lead(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    _, raw = callback.data.split(":", 1)
    mapping = {"1": 1, "2": 2, "3": 3, "5": 5}
    lead_time_days = mapping.get(raw)
    if lead_time_days is None:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(lead_time_days=lead_time_days)

    selected = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    await state.update_data(selected_days=selected)

    kb = build_weekday_keyboard(selected, prefix="autobook_day", back_callback="autobook_back:lead")

    msg = await callback.message.answer(
        "Шаг 7 из 7 — дни недели.\n\n"
        "Выбери, в какие дни можно сдавать поставку:",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(AutoBookNewState.weekdays)


def build_autobook_manual_summary(data: dict) -> str:
    account = data.get("autobook_account") or {}
    draft = data.get("autobook_draft") or {}
    account_name = account.get("name") or account.get("id")
    draft_id = draft.get("id")
    draft_created = draft.get("created_at")
    draft_goods = draft.get("good_quantity")
    draft_barcodes = draft.get("barcode_quantity")

    slot_summary = build_slot_summary(data)

    lines = [
        "🚀 Автобронирование",
        "",
        f"Продавец: {account_name}",
        f"Черновик #{draft_id} — от {draft_created}, товаров: {draft_goods}, баркодов: {draft_barcodes}",
        "",
        slot_summary,
        "",
        "На следующем этапе я подготовлю поставки для каждого склада в вашем личном кабинете WB к бронированию",
        "Пожалуйста, не удаляйте их - так я сэкономлю ~0.5 секунды на бронирование при появлении слота",
        "После успешного бронировании лишние поставки будут удалены",
    ]

    return "\n".join(lines)


async def on_autobook_week(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_cb = callback.data or ""
    _, code = data_cb.split(":", 1)

    data = await state.get_data()
    selected = set(data.get("selected_days", []))

    if code == "done":
        if selected == {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            weekdays = "daily"
        elif selected == {"mon", "tue", "wed", "thu", "fri"}:
            weekdays = "weekdays"
        elif selected == {"sat", "sun"}:
            weekdays = "weekends"
        else:
            weekdays = "custom:" + ",".join(sorted(selected))

        await state.update_data(weekdays=weekdays)

        payload_source = await state.get_data()
        search_period_from = payload_source.get("search_period_from") or date.today().isoformat()
        search_period_to = payload_source.get("search_period_to")
        if search_period_to is None:
            offset = payload_source.get("period_days") or 0
            search_period_to = (date.today() + timedelta(days=offset)).isoformat()

        supply_type_backend = {
            "box": "Короба",
            "mono": "Монопаллеты",
            "postal": "Поштучная паллета",
            "safe": "Суперсейф",
        }.get(payload_source.get("supply_type")) or payload_source.get("supply_type")

        payload = {
            "draft_id": (payload_source.get("autobook_draft") or {}).get("id"),
            "lead_time_days": payload_source.get("lead_time_days"),
            "period_from": search_period_from,
            "period_to": search_period_to,
            "seller_name": (payload_source.get("autobook_account") or {}).get("name"),
            "supply_type": supply_type_backend,
            "telegram_chat_id": callback.from_user.id,
            "user_id": payload_source.get("autobook_user_id") or (payload_source.get("autobook_account") or {}).get("user_id"),
            "warehouse": payload_source.get("warehouse"),
            "weekdays": weekdays,
        }

        await state.update_data(autobook_new_payload=payload)

        summary = build_autobook_manual_summary(await state.get_data())
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Продолжить", callback_data="autobook_new_confirm")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:lead")],
            ]
        )

        msg = await callback.message.answer(summary, reply_markup=kb)
        await add_ui_message(state, msg.message_id)
        await state.set_state(AutoBookNewState.confirm)
        return

    if code in selected:
        selected.remove(code)
    else:
        selected.add(code)

    await state.update_data(selected_days=selected)

    kb = build_weekday_keyboard(selected, prefix="autobook_day", back_callback="autobook_back:lead")

    await callback.message.edit_reply_markup(reply_markup=kb)


async def on_autobook_new_account(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, account_id = data_cb.split(":", 1)
        account_id = int(account_id)
    except Exception:
        await callback.answer("Некорректный продавец.", show_alert=True)
        return

    data = await state.get_data()
    accounts = data.get("autobook_accounts") or []
    user_id = data.get("autobook_user_id")
    selected = next((a for a in accounts if str(a.get("id")) == str(account_id)), None)

    if not selected:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    if user_id is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp_user = await client.get(
                    f"{BACKEND_URL}/users/get-id",
                    params={"telegram_id": callback.from_user.id},
                )
                resp_user.raise_for_status()
                user_id = resp_user.json().get("user_id")
        except Exception:
            await callback.answer("Не удалось обновить пользователя.", show_alert=True)
            return

    try:
        await callback.message.edit_text("Загружаем ваши черновики, подождите..")
    except Exception:
        prev_mid = callback.message.message_id
        loading_msg = await callback.message.answer("Загружаем ваши черновики, подождите..")
        await add_ui_message(state, loading_msg.message_id)
        callback.message = loading_msg
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_mid
            )
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass

    try:
        overview = await _fetch_overview_page(
            user_id=user_id,
            account_id=selected.get("id"),
            page=1,
        )
    except Exception as e:
        print("Error calling /wb/overview:", e)
        await callback.message.edit_text(
            "Не удалось загрузить данные для аккаунта. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
            ),
        )
        return

    drafts = overview.get("drafts") or []
    new_accounts = overview.get("accounts") or accounts
    pagination = overview.get("pagination") or {}
    selected = next(
        (a for a in new_accounts if str(a.get("id")) == str(account_id)), selected
    )

    await state.update_data(
        autobook_account=selected,
        autobook_drafts=drafts,
        autobook_accounts=new_accounts,
        autobook_user_id=user_id,
        autobook_drafts_page=pagination.get("page", 1),
        autobook_drafts_pagination=pagination,
    )
    await callback.answer()
    await _autobook_send_drafts(callback.message, state)


async def on_autobook_drafts_page(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, page_raw = data_cb.split(":", 1)
        page = int(page_raw)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    data = await state.get_data()
    account = data.get("autobook_account") or {}
    user_id = data.get("autobook_user_id")
    account_id = account.get("id")

    if user_id is None or account_id is None:
        await callback.answer("Не хватает данных для обновления.", show_alert=True)
        return

    try:
        await callback.message.edit_text("Загружаем список складов, подождите..")
    except Exception:
        prev_mid = callback.message.message_id
        loading_msg = await callback.message.answer("Загружаем список складов, подождите..")
        await add_ui_message(state, loading_msg.message_id)
        callback.message = loading_msg
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_mid
            )
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass

    try:
        overview = await _fetch_overview_page(
            user_id=user_id, account_id=account_id, page=page
        )
    except Exception as e:
        print("Error calling /wb/overview:", e)
        await callback.message.edit_text(
            "Не удалось загрузить данные для аккаунта. Попробуй позже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
            ),
        )
        return

    drafts = overview.get("drafts") or []
    pagination = overview.get("pagination") or {}
    accounts = overview.get("accounts") or data.get("autobook_accounts") or []
    selected = next(
        (a for a in accounts if str(a.get("id")) == str(account_id)), account
    )

    await state.update_data(
        autobook_drafts=drafts,
        autobook_drafts_page=pagination.get("page", page),
        autobook_drafts_pagination=pagination,
        autobook_accounts=accounts,
        autobook_account=selected,
    )

    await callback.answer()
    await _autobook_send_drafts(callback.message, state)


async def on_autobook_new_draft(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, draft_id = data_cb.split(":", 1)
        draft_id_int = int(draft_id)
    except Exception:
        await callback.answer("Некорректный черновик.", show_alert=True)
        return

    data = await state.get_data()
    drafts = data.get("autobook_drafts") or []
    selected = next((d for d in drafts if d.get("id") == draft_id_int), None)
    if not selected:
        await callback.answer("Черновик не найден.", show_alert=True)
        return

    await state.update_data(
        autobook_draft=selected,
        awaiting_manual_period=False,
        selected_days=set(),
        autobook_new_payload=None,
    )
    await callback.answer()

    try:
        await callback.message.edit_text("Загружаем список складов, подождите..")
    except Exception:
        prev_mid = callback.message.message_id
        loading_msg = await callback.message.answer("Загружаем список складов, подождите..")
        await add_ui_message(state, loading_msg.message_id)
        callback.message = loading_msg
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_mid
            )
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass

    await _autobook_load_warehouses(callback.message, state)


async def on_autobook_new_request(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, req_id = data_cb.split(":", 1)
        req_id_int = int(req_id)
    except Exception:
        await callback.answer("Некорректный поиск.", show_alert=True)
        return

    data = await state.get_data()
    requests_list = data.get("autobook_requests") or []
    selected = next((r for r in requests_list if r.get("id") == req_id_int), None)
    account = data.get("autobook_account") or {}
    draft = data.get("autobook_draft") or {}

    if not selected:
        await callback.answer("Поиск не найден.", show_alert=True)
        return

    warehouse = selected.get("warehouse")
    supply_type = selected.get("supply_type")
    max_coef = selected.get("max_booking_coefficient")
    logistics_percent = selected.get("max_logistics_percent")
    lead_time = selected.get("lead_time_days")
    period = selected.get("period") or {}
    period_text = f"{period.get('from')} – {period.get('to')}"
    supply_map = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
        "Монопаллеты": "Монопаллеты",
    }
    supply_text = supply_map.get(supply_type, str(supply_type))

    account_name = account.get("name") or account.get("id")
    draft_id = draft.get("id")
    draft_created = draft.get("created_at")
    draft_goods = draft.get("good_quantity")
    draft_barcodes = draft.get("barcode_quantity")

    summary_lines = [
        "🚀 Автобронирование",
        "",
        f"Продавец: {account_name}",
        f"Черновик #{draft_id} — от {draft_created}, товаров: {draft_goods}, баркодов: {draft_barcodes}",
        "",
        "Поиск:",
        f"• Склад: {warehouse}",
        f"• Тип поставки: {supply_text}",
        f"• Коэффициент: {max_coef}",
        f"• Логистика: {logistics_percent}%",
        f"• Лид-тайм: {lead_time} дн.",
        f"• Даты: {period_text}",
        "",
        "На следующем этапе я подготовлю поставки для каждого склада в вашем личном кабинете WB к бронированию",
        "Пожалуйста, не удаляйте их - так я сэкономлю ~0.5 секунды на бронирование при появлении слота",
        "После успешного бронировании лишние поставки будут удалены",
    ]

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить", callback_data="autobook_new_confirm")],
            [InlineKeyboardButton(text="Отмена", callback_data="autobook_new_cancel")],
        ]
    )

    user_id = data.get("autobook_user_id") or selected.get("user_id")

    await state.update_data(
        autobook_request=selected,
        autobook_new_payload={
            "user_id": user_id,
            "seller_name": account_name,
            "draft_id": draft_id,
            "slot_request_id": req_id_int,
        },
    )

    try:
        await callback.message.edit_text("\n".join(summary_lines), reply_markup=kb)
    except Exception:
        prev_mid = callback.message.message_id
        new_msg = await callback.message.answer("\n".join(summary_lines), reply_markup=kb)
        await add_ui_message(state, new_msg.message_id)
        try:
            await callback.message.bot.delete_message(
                chat_id=callback.message.chat.id, message_id=prev_mid
            )
            await _drop_ui_message_id(state, prev_mid)
        except Exception:
            pass
    await callback.answer()
    await state.set_state(AutoBookNewState.confirm)


async def _send_autobook_confirm_error(message_obj, state: FSMContext) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Повторить", callback_data="autobook_new_retry")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )
    msg = await message_obj.answer("Не удалось создать автобронь. Попробовать снова?", reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def on_autobook_new_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    payload = data.get("autobook_new_payload")

    required_fields = [
        "draft_id",
        "lead_time_days",
        "period_from",
        "period_to",
        "seller_name",
        "supply_type",
        "telegram_chat_id",
        "user_id",
        "warehouse",
        "weekdays",
    ]

    if not payload or any(payload.get(f) in (None, "") for f in required_fields):
        await _send_autobook_confirm_error(callback.message, state)
        return

    try:
        await callback.message.bot.delete_message(
            chat_id=callback.message.chat.id, message_id=callback.message.message_id
        )
        await _drop_ui_message_id(state, callback.message.message_id)
    except Exception:
        pass

    status_msg = await callback.message.answer("Автобронирование в процессе, ждите!")
    await add_ui_message(state, status_msg.message_id)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{BACKEND_URL}/wb/autobooking", json=payload)
            resp.raise_for_status()
    except Exception as e:
        print("Error calling /wb/autobooking:", e)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Повторить", callback_data="autobook_new_retry")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        await status_msg.edit_text(
            "Не удалось создать автобронь. Попробовать снова?", reply_markup=kb
        )
        return

    await state.clear()
    await status_msg.edit_text(
        "Автобронирование в процессе, ждите!", reply_markup=get_main_menu_keyboard()
    )


async def on_autobook_new_retry(callback: CallbackQuery, state: FSMContext) -> None:
    await on_autobook_new_confirm(callback, state)


async def on_autobook_new_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await send_main_menu(callback.message, state)


async def menu_auth_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    telegram_id = callback.from_user.id
    authorized = await _fetch_wb_auth_status(telegram_id)

    if authorized:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚪 Выйти из WB", callback_data="menu_logout")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
            ]
        )
        msg = await callback.message.answer(
            "Ты уже авторизован в WB ✅\nЧтобы войти под другим аккаунтом, сначала выйди.",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        return

    if authorized is None:
        msg = await callback.message.answer("Не удалось получить статус WB. Попробуй позже.")
        await add_ui_message(state, msg.message_id)
        return

    await start_wb_auth_flow(callback.message, state, callback.from_user.id)


async def menu_status_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    telegram_id = callback.from_user.id
    authorized = await _fetch_wb_auth_status(telegram_id)
    if authorized is None:
        text = "Не удалось получить статус авторизации WB. Попробуй позже."
    else:
        text = "Статус WB: авторизован ✅" if authorized else "Статус WB: не авторизован ❌"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]]
    )
    msg = await callback.message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def menu_moves_callback(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    await callback.answer()
    await show_moves_list(callback.message, state, telegram_id, page=1)


async def moves_page_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, page_str = data_cb.split(":", 1)
        page = int(page_str)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    await callback.answer()
    await show_moves_list(callback.message, state, callback.from_user.id, page=page)


async def moves_open_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, task_id_str = data_cb.split(":", 1)
        task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return
    await callback.answer()
    await show_move_card(callback.message, state, callback.from_user.id, task_id)


async def moves_stop_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, task_id_str = data_cb.split(":", 1)
        task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return
    await callback.answer()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/stock-move/cancel",
                json={"telegram_id": callback.from_user.id, "task_id": task_id},
            )
            resp.raise_for_status()
    except Exception as e:
        print("Error /stock-move/cancel:", e)
    await show_move_card(callback.message, state, callback.from_user.id, task_id)


async def moves_start_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, task_id_str = data_cb.split(":", 1)
        task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return
    await callback.answer()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/stock-move/restart",
                json={"telegram_id": callback.from_user.id, "task_id": task_id},
            )
            resp.raise_for_status()
    except Exception as e:
        print("Error /stock-move/restart:", e)
    await show_move_card(callback.message, state, callback.from_user.id, task_id)


async def moves_delete_placeholder(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Удаление пока не реализовано.", show_alert=True)


async def moves_create_callback(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    await callback.answer()
    await start_move_wizard(callback.message, state, telegram_id)


async def moves_choose_qty(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    try:
        _, qty_str = (callback.data or "").split(":", 1)
        qty = int(qty_str)
    except Exception:
        await callback.answer("Некорректное количество.", show_alert=True)
        return
    data = await state.get_data()
    article_id = data.get("article_id")
    from_warehouse = data.get("from_warehouse")
    options = data.get("move_options") or {}
    available_qty = None
    for art in options.get("articles", []):
        if art.get("id") == article_id:
            for st in art.get("stocks", []):
                if st.get("warehouse") == from_warehouse:
                    available_qty = st.get("qty")
                    break
            break
    if available_qty is not None and qty > available_qty:
        msg_err = await callback.message.answer(
            f"Недостаточно остатка на складе. Доступно: {available_qty} шт.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="moves_back_qty")]]
            ),
        )
        await add_ui_message(state, msg_err.message_id)
        return
    await state.update_data(qty=qty)
    await state.set_state(MoveWizardState.confirm)
    await show_move_confirm(callback.message, state)


async def moves_confirm_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    telegram_id = data.get("telegram_id") or callback.from_user.id
    article_id = data.get("article_id")
    from_warehouse = data.get("from_warehouse")
    to_warehouse = data.get("to_warehouse")
    qty = data.get("qty")
    account_id = data.get("account_id")

    if not all([article_id, from_warehouse, to_warehouse, qty, account_id]):
        await clear_all_ui(callback.message, state)
        msg = await callback.message.answer("Не удалось создать задачу: не все поля заполнены.")
        await add_ui_message(state, msg.message_id)
        await state.clear()
        return

    await clear_all_ui(callback.message, state)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/stock-move/create",
                json={
                    "telegram_id": telegram_id,
                    "article": article_id,
                    "from_warehouse": from_warehouse,
                    "to_warehouse": to_warehouse,
                    "qty": qty,
                },
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        print("Error /stock-move/create:", e)
        msg = await callback.message.answer("Не удалось создать задачу перераспределения. Попробуй позже.")
        await add_ui_message(state, msg.message_id)
        await state.clear()
        return

    task_id = result.get("id")
    msg_done = await callback.message.answer(f"Задача перераспределения #{task_id} создана.")
    await add_ui_message(state, msg_done.message_id)

    await state.clear()
    await show_moves_list(callback.message, state, callback.from_user.id, page=1)


async def moves_back_qty(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(MoveWizardState.choose_qty)
    await show_move_qty(callback.message, state)


async def moves_back_to(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(MoveWizardState.choose_to_warehouse)
    await show_move_to_warehouses(callback.message, state)


async def moves_back_from(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(MoveWizardState.choose_from_warehouse)
    await show_move_from_warehouses(callback.message, state)


async def moves_back_articles(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(MoveWizardState.choose_article)
    await show_move_articles(callback.message, state)


async def moves_choose_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    account_id = callback.data.split(":", 1)[1]
    await state.update_data(account_id=account_id)
    await state.set_state(MoveWizardState.choose_article)
    await show_move_articles(callback.message, state)


async def moves_choose_article(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    article_id = callback.data.split(":", 1)[1]
    await state.update_data(article_id=article_id)
    await state.set_state(MoveWizardState.choose_from_warehouse)
    await show_move_from_warehouses(callback.message, state)


async def moves_back_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(MoveWizardState.choose_account)
    await show_move_accounts(callback.message, state)


async def moves_back_article(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(MoveWizardState.choose_article)
    await show_move_articles(callback.message, state)


async def moves_choose_from(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    from_wh = callback.data.split(":", 1)[1]
    await state.update_data(from_warehouse=from_wh)
    await state.set_state(MoveWizardState.choose_to_warehouse)
    await show_move_to_warehouses(callback.message, state)


async def moves_choose_to(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    to_wh = callback.data.split(":", 1)[1]
    await state.update_data(to_warehouse=to_wh)
    await state.set_state(MoveWizardState.choose_qty)
    await show_move_qty(callback.message, state)
async def menu_logout_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _do_main_menu_logout(callback.message, state, callback.from_user.id)


async def menu_help_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await handle_main_menu_help(callback.message, state)


async def menu_main_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await send_main_menu(callback.message, state)


async def wb_auth_command_handler(message: Message, state: FSMContext) -> None:
    """
    Команда /wb_auth запускает новый мастер авторизации WB.
    """
    telegram_id = message.from_user.id
    await start_wb_auth_flow(message, state, telegram_id)


async def cmd_cancel_search(message: Message, command: CommandObject, state: FSMContext) -> None:
    """
    Отмена задачи поиска слота по ID. Использование: /cancel_search 1
    """
    args = command.args
    if not args:
        await message.answer("Укажи ID задачи, которую нужно отменить.\n\nПример: /cancel_search 1")
        return

    task_id_str = args.strip()
    if not task_id_str.isdigit():
        await message.answer("ID задачи должен быть числом.\nПример: /cancel_search 1")
        return

    task_id = int(task_id_str)
    telegram_id = message.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/slot-search/cancel",
                json={"telegram_id": telegram_id, "task_id": task_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error calling /slot-search/cancel:", e)
        await message.answer("Не удалось отменить задачу. Проверь ID и попробуй ещё раз.")
        return

    status = data.get("status")
    await message.answer(f"Задача #{task_id} переведена в статус: {status}.")


async def cmd_restart_search(message: Message, command: CommandObject, state: FSMContext) -> None:
    """
    Перезапуск задачи поиска слота по ID. Использование: /restart_search 1
    """
    args = command.args
    if not args:
        await message.answer("Укажи ID задачи, которую нужно запустить заново.\n\nПример: /restart_search 1")
        return

    task_id_str = args.strip()
    if not task_id_str.isdigit():
        await message.answer("ID задачи должен быть числом.\nПример: /restart_search 1")
        return

    task_id = int(task_id_str)
    telegram_id = message.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/slot-search/restart",
                json={"telegram_id": telegram_id, "task_id": task_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error calling /slot-search/restart:", e)
        await message.answer("Не удалось запустить задачу заново. Проверь ID и попробуй ещё раз.")
        return

    status = data.get("status")
    await message.answer(f"Задача #{task_id} теперь в статусе: {status}.")


async def on_slot_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    try:
        _, task_id_str = data.split(":", 1)
        task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный формат ID.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/slot-search/cancel",
                json={"telegram_id": telegram_id, "task_id": task_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error calling /slot-search/cancel (callback):", e)
        await callback.answer("Не удалось отменить задачу. Попробуй позже.", show_alert=True)
        return

    data_state = await state.get_data()
    tasks = data_state.get("slot_tasks") or []
    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = "cancelled"
            break
    await state.update_data(slot_tasks=tasks)
    await _render_slot_task_card(callback.message, state, task_id)
    await callback.answer("Задача отменена.", show_alert=False)


async def on_slot_restart_callback(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    try:
        _, task_id_str = data.split(":", 1)
        task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный формат ID.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/slot-search/restart",
                json={"telegram_id": telegram_id, "task_id": task_id},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        print("Error calling /slot-search/restart (callback):", e)
        await callback.answer("Не удалось запустить задачу заново. Попробуй позже.", show_alert=True)
        return

    data_state = await state.get_data()
    tasks = data_state.get("slot_tasks") or []
    for t in tasks:
        if t.get("id") == task_id:
            t["status"] = "active"
            break
    await state.update_data(slot_tasks=tasks)
    await _render_slot_task_card(callback.message, state, task_id)
    await callback.answer("Задача запущена.", show_alert=False)


async def on_slot_delete(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, id_str = data_cb.split(":", 1)
        task_id = int(id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/slot-search/delete",
                json={
                    "telegram_id": telegram_id,
                    "slot_search_task_id": task_id,
                },
            )
            resp.raise_for_status()
    except Exception as e:
        print("Error calling /slot-search/delete:", e)
        await callback.answer("Не удалось удалить задачу.", show_alert=True)
        return

    data_state = await state.get_data()
    tasks = data_state.get("slot_tasks") or []
    tasks = [t for t in tasks if t.get("id") != task_id]
    await state.update_data(slot_tasks=tasks)

    await callback.answer("Задача удалена.", show_alert=False)
    await _send_slot_tasks_page(callback.message, state, page=0)


async def on_autobook_task_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    try:
        _, task_id_str = data.split(":", 1)
        task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный формат задачи.", show_alert=True)
        return

    await state.update_data(slot_search_task_id=task_id)

    await callback.answer(f"Выбрана задача #{task_id}.", show_alert=False)
    await callback.message.answer(
        f"Задача #{task_id} выбрана для настройки автоброни.\n"
        f"Следующий шаг — логистика и выбор черновика (добавим на следующих шагах).",
        reply_markup=get_main_menu_keyboard(),
    )

    await state.clear()


async def on_autobook_from_search(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    try:
        _, task_id_str = data.split(":", 1)
        slot_search_task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    await clear_all_ui(callback.message, state)
    await state.update_data(autobook_message_ids=[], slot_search_task_id=slot_search_task_id)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/autobook/options",
                json={"telegram_id": telegram_id, "slot_search_task_id": slot_search_task_id},
            )
            resp.raise_for_status()
            options = resp.json()
    except Exception as e:
        print("Error calling /autobook/options:", e)
        await callback.answer("Не удалось получить данные для автобронирования.", show_alert=True)
        return

    slot_task = options.get("slot_task") or {}
    accounts = options.get("accounts") or []
    drafts = options.get("drafts") or []
    transit_warehouses = options.get("transit_warehouses") or []

    await state.update_data(
        slot_task=slot_task,
        accounts=accounts,
        drafts=drafts,
        transit_warehouses=transit_warehouses,
    )

    if not accounts:
        await callback.message.answer(
            "Нет доступных кабинетов WB для этой задачи.\nПопробуй позже.",
            reply_markup=get_main_menu_keyboard(),
        )
        await callback.answer()
        await state.clear()
        return

    warehouse = slot_task.get("warehouse")
    supply_type = slot_task.get("supply_type")
    max_coef = slot_task.get("max_coef")
    lead_time_days = slot_task.get("lead_time_days")
    date_from = slot_task.get("date_from")
    date_to = slot_task.get("date_to")
    weekdays = slot_task.get("weekdays")

    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    weekdays_text = {
        "daily": "Ежедневно",
        "weekdays": "Только будни",
        "weekends": "Только выходные",
    }.get(weekdays, str(weekdays))

    text = (
        "🚀 Автобронирование\n\n"
        f"Склад: {warehouse}\n"
        f"Тип поставки: {supply_type_text}\n"
        f"Коэффициент: ≤x{max_coef}\n"
        f"Лид-тайм (мин. кол-во дней до слота): {lead_time_days}\n"
        f"Поиск слота на даты: {date_from}–{date_to}\n"
        f"Дни недели: {weekdays_text}\n\n"
        "На следующем этапе я подключусь к вашему кабинету на WB, чтобы запросить поставщиков и список черновиков.\n\n"
        "Нажмите «Далее»."
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Выбрать кабинет", callback_data="autobook_show_accounts")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )

    await _autobook_clear_messages(callback.message, state)
    new_msg = await callback.message.answer(text, reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)
    await callback.answer()
    await state.set_state(AutoBookState.choose_account)


async def on_autobook_show_accounts(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)
    data = await state.get_data()
    accounts = data.get("accounts") or []
    if not accounts:
        await state.clear()
        await send_main_menu(callback.message, state)
        return

    text_lines = ["🚀 Автобронирование\n\nВыберите продавца:\n"]
    kb_rows = []
    for acc in accounts:
        acc_id = acc.get("id")
        acc_name = acc.get("name")
        text_lines.append(f"• {acc_name}")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=acc_name or acc_id,
                    callback_data=f"autobook_choose_account:{acc_id}",
                )
            ]
        )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await _autobook_clear_messages(callback.message, state)
    new_msg = await callback.message.answer("\n".join(text_lines), reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)


async def on_autobook_start(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    try:
        _, task_id_str = data.split(":", 1)
        autobook_task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/autobook/start",
                json={"telegram_id": telegram_id, "autobook_task_id": autobook_task_id},
            )
            resp.raise_for_status()
            data_json = resp.json()
    except Exception as e:
        print("Error calling /autobook/start:", e)
        await callback.answer("Не удалось запустить автобронирование.", show_alert=True)
        return

    status = data_json.get("status")
    data_state = await state.get_data()
    tasks = data_state.get("autobook_tasks") or []
    for item in tasks:
        if item.get("id") == autobook_task_id:
            item["status"] = status
            break
    await state.update_data(autobook_tasks=tasks)
    await _render_autobook_card(callback.message, state, autobook_task_id)
    await callback.answer("Запущено", show_alert=False)


async def on_autobook_stop(callback: CallbackQuery, state: FSMContext) -> None:
    data = callback.data or ""
    try:
        _, task_id_str = data.split(":", 1)
        autobook_task_id = int(task_id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/autobook/stop",
                json={"telegram_id": telegram_id, "autobook_task_id": autobook_task_id},
            )
            resp.raise_for_status()
            data_json = resp.json()
    except Exception as e:
        print("Error calling /autobook/stop:", e)
        await callback.answer("Не удалось остановить автобронирование.", show_alert=True)
        return

    status = data_json.get("status")
    data_state = await state.get_data()
    tasks = data_state.get("autobook_tasks") or []
    for item in tasks:
        if item.get("id") == autobook_task_id:
            item["status"] = status
            break
    await state.update_data(autobook_tasks=tasks)
    await _render_autobook_card(callback.message, state, autobook_task_id)
    await callback.answer("Остановлено", show_alert=False)


async def on_autobook_open(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, id_str = data_cb.split(":", 1)
        autobook_id = int(id_str)
    except Exception:
        await callback.answer("Некорректный ID.", show_alert=True)
        return

    await _render_autobook_card(callback.message, state, autobook_id)
    await callback.answer()


async def on_autobook_back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _send_autobook_page(callback.message, state, page=0)


async def on_autobook_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _clear_autobook_messages(callback.message, state)
    await state.clear()
    await send_main_menu(callback.message, state)


async def on_autobook_page(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, page_str = data_cb.split(":", 1)
        page = int(page_str)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    await callback.answer()
    await _send_autobook_page(callback.message, state, page=page)


async def on_autobook_delete(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, id_str = data_cb.split(":", 1)
        autobook_id = int(id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/autobook/delete",
                json={
                    "telegram_id": telegram_id,
                    "autobook_task_id": autobook_id,
                },
            )
            resp.raise_for_status()
    except Exception as e:
        print("Error calling /autobook/delete:", e)
        await callback.answer("Не удалось удалить задачу автобронирования.", show_alert=True)
        return

    data_state = await state.get_data()
    tasks = data_state.get("autobook_tasks") or []
    tasks = [t for t in tasks if t.get("id") != autobook_id]
    await state.update_data(autobook_tasks=tasks)

    await callback.answer("Автобронирование удалено.", show_alert=False)
    await _send_autobook_page(callback.message, state, page=0)

async def on_autobook_show_accounts(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)
    data = await state.get_data()
    accounts = data.get("accounts") or []
    if not accounts:
        await state.clear()
        await send_main_menu(callback.message, state)
        return

    text_lines = ["🚀 Автобронирование\n\nВыберите продавца:\n"]
    kb_rows = []
    for acc in accounts:
        acc_id = acc.get("id")
        acc_name = acc.get("name")
        text_lines.append(f"• {acc_name}")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=acc_name or acc_id,
                    callback_data=f"autobook_choose_account:{acc_id}",
                )
            ]
        )
    kb_rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await _autobook_clear_messages(callback.message, state)
    new_msg = await callback.message.answer("\n".join(text_lines), reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)


async def autobook_choose_account_step(message: Message, state: FSMContext) -> None:
    text = message.text.strip()

    await clear_all_ui(message, state)

    if text == "<< Личный кабинет":
        await _autobook_clear_messages(message, state)
        await state.clear()
        await message.answer("Возвращаю в главное меню.", reply_markup=get_main_menu_keyboard())
        return

    if text != "Далее":
        await message.answer(
            "Нажми «Далее», чтобы выбрать кабинет WB, или «<< Личный кабинет», чтобы выйти."
        )
        return

    data = await state.get_data()
    accounts = data.get("accounts") or []

    if not accounts:
        await state.clear()
        await message.answer(
            "Нет доступных кабинетов WB для этой задачи.",
            reply_markup=get_main_menu_keyboard(),
        )
        return

    text_lines = ["🚀 Автобронирование\n\nВыберите продавца:\n"]
    kb_rows = []
    for acc in accounts:
        acc_id = acc.get("id")
        acc_name = acc.get("name")
        text_lines.append(f"• {acc_name}")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=acc_name or acc_id,
                    callback_data=f"autobook_choose_account:{acc_id}",
                )
            ]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _autobook_clear_messages(message, state)
    new_msg = await message.answer("\n".join(text_lines), reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)


async def autobook_choose_transit_step(message: Message, state: FSMContext) -> None:
    text = message.text.strip()

    await clear_all_ui(message, state)

    if text == "<< Личный кабинет":
        await _autobook_clear_messages(message, state)
        await state.clear()
        await message.answer("Возвращаю в главное меню.", reply_markup=get_main_menu_keyboard())
        return

    mapping = {
        "Без транзитного склада ➡": "none",
        "СЦ Гродно": "sc_grodno",
    }

    if text not in mapping:
        await message.answer("Пожалуйста, выбери один из вариантов транзитного склада.")
        return

    transit_id = mapping[text]
    await state.update_data(transit_warehouse_id=transit_id)

    data = await state.get_data()
    drafts = data.get("drafts") or []

    if not drafts:
        await _autobook_clear_messages(message, state)
        await state.clear()
        await message.answer("Не найдено черновиков для автобронирования.", reply_markup=get_main_menu_keyboard())
        return

    text_lines = ["Выберите черновик из списка:\n"]
    kb_rows = []
    for d in drafts:
        draft_id = d.get("id")
        name = d.get("name")
        text_lines.append(f"• {name} (id: {draft_id})")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=name or f"Черновик {draft_id}",
                    callback_data=f"autobook_choose_draft:{draft_id}",
                )
            ]
        )

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _autobook_clear_messages(message, state)
    new_msg = await message.answer("\n".join(text_lines), reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)
    await state.set_state(AutoBookState.choose_draft)


async def on_autobook_choose_draft(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, draft_id = data_cb.split(":", 1)
    except Exception:
        await callback.answer("Некорректный черновик.", show_alert=True)
        return

    await clear_all_ui(callback.message, state)
    await state.update_data(draft_id=draft_id)

    data = await state.get_data()
    slot_search_task_id = data.get("slot_search_task_id")
    slot_task = data.get("slot_task") or {}
    account_id = data.get("account_id")
    transit_id = data.get("transit_warehouse_id")
    if not slot_search_task_id:
        await callback.answer("Не найдена выбранная задача поиска.", show_alert=True)
        await state.clear()
        return

    warehouse = slot_task.get("warehouse")
    supply_type = slot_task.get("supply_type")
    max_coef = slot_task.get("max_coef")
    lead_time_days = slot_task.get("lead_time_days")
    date_from = slot_task.get("date_from")
    date_to = slot_task.get("date_to")
    weekdays = slot_task.get("weekdays")

    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    weekdays_text = {
        "daily": "Ежедневно",
        "weekdays": "Только будни",
        "weekends": "Только выходные",
    }.get(weekdays, str(weekdays))

    summary_lines = [
        "🚀 Ваше задание на автобронирование\n",
        f"Задача поиска #{slot_search_task_id}",
        f"Склад: {warehouse}",
        f"Тип поставки: {supply_type_text}",
        f"Коэффициент приёмки: ≤x{max_coef}" if max_coef is not None else "",
        f"Лид-тайм: {lead_time_days} дн." if lead_time_days is not None else "",
        f"Период: {date_from}–{date_to}",
        f"Дни недели: {weekdays_text}",
        f"Кабинет: {account_id}" if account_id else "",
        f"Транзитный склад: {transit_id}" if transit_id else "",
        "",
        f"Черновик: {draft_id}",
        "",
        "Нажми «✅ Добавить автобронирование», чтобы бот начал отслеживать слоты.",
    ]

    text = "\n".join([line for line in summary_lines if line])

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Добавить автобронирование", callback_data="autobook_confirm")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_show_accounts")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )

    await _autobook_clear_messages(callback.message, state)
    new_msg = await callback.message.answer(text, reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)
    await callback.answer()

    await state.set_state(AutoBookState.confirm)


async def autobook_confirm_step(message: Message, state: FSMContext) -> None:
    return


async def on_autobook_transit(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)
    data_cb = callback.data or ""
    try:
        _, transit_id = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(transit_warehouse_id=transit_id)

    data = await state.get_data()
    drafts = data.get("drafts") or []

    if not drafts:
        await state.clear()
        await send_main_menu(callback.message, state)
        return

    text_lines = ["Выберите черновик из списка:\n"]
    kb_rows = []
    for d in drafts:
        draft_id = d.get("id")
        name = d.get("name")
        text_lines.append(f"• {name} (id: {draft_id})")
        kb_rows.append(
            [
                InlineKeyboardButton(
                    text=name or f"Черновик {draft_id}",
                    callback_data=f"autobook_choose_draft:{draft_id}",
                )
            ]
        )

    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_show_accounts")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _autobook_clear_messages(callback.message, state)
    new_msg = await callback.message.answer("\n".join(text_lines), reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)
    await state.set_state(AutoBookState.choose_draft)


async def on_autobook_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)
    data = await state.get_data()
    slot_task_id = data.get("slot_search_task_id")
    telegram_id = callback.from_user.id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/autobook/create",
                json={
                    "telegram_id": telegram_id,
                    "slot_search_task_id": slot_task_id,
                    "logistics_accept_mode": "any",
                },
            )
            resp.raise_for_status()
    except Exception as e:
        print("Error calling /autobook/create in confirm step:", e)
        msg_err = await callback.message.answer(
            "Не удалось создать задачу автобронирования. Попробуй позже."
        )
        await add_ui_message(state, msg_err.message_id)
        await state.clear()
        return

    await clear_all_ui(callback.message, state)
    await state.clear()
    await _do_main_menu_autobook_list(callback.message, state, callback.from_user.id)


async def on_slot_tasks_page(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, page_str = data_cb.split(":", 1)
        page = int(page_str)
    except Exception:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    await callback.answer()
    await _send_slot_tasks_page(callback.message, state, page=page)


async def on_slot_tasks_main_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _clear_slot_tasks_messages(callback.message, state)
    await state.clear()
    await send_main_menu(callback.message, state)


async def on_slot_task_open(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        _, id_str = data_cb.split(":", 1)
        task_id = int(id_str)
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return
    await callback.answer()
    await _render_slot_task_card(callback.message, state, task_id)


async def on_slot_tasks_back_to_list(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data = await state.get_data()
    page = data.get("slot_tasks_page", 0)
    await _send_slot_tasks_page(callback.message, state, page=page)


async def on_slot_auto(callback: CallbackQuery, state: FSMContext) -> None:
    data_cb = callback.data or ""
    try:
        task_id = int(data_cb.split("_", 2)[-1])
    except Exception:
        await callback.answer("Некорректный ID задачи.", show_alert=True)
        return

    # Переиспользуем существующий поток автоброни
    fake_data = f"autobook_from_search:{task_id}"
    callback.data = fake_data  # перенаправляем на текущий хендлер
    await on_autobook_from_search(callback, state)


async def on_menu_slot_tasks(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _show_tasks_menu(callback.message, state)


async def on_slot_back(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    data_cb = callback.data or ""
    target = data_cb.split(":", 1)[-1]
    if target == "warehouse":
        await cmd_create_search(callback.message, state)
    elif target == "supply":
        await clear_all_ui(callback.message, state)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📦 Короба", callback_data="slot_supply:box"),
                    InlineKeyboardButton(text="🟫 Монопаллеты", callback_data="slot_supply:mono"),
                ],
                [
                    InlineKeyboardButton(text="✉️ Поштучная паллета", callback_data="slot_supply:postal"),
                    InlineKeyboardButton(text="🛡 Суперсейф", callback_data="slot_supply:safe"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:warehouse")],
            ]
        )
        msg = await callback.message.answer(
            "Шаг 2 из 7 — тип поставки.\n\nВыбери один из вариантов:",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(SlotSearchState.supply_type)
    elif target == "coef":
        await clear_all_ui(callback.message, state)
        kb = build_coef_keyboard(0, 20, per_row=4)
        msg = await callback.message.answer(
            "Шаг 3 из 7 — максимальный коэффициент.\n\nВыбери максимальный коэффициент бронирования:",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(SlotSearchState.max_coef)
    elif target == "logistics":
        await clear_all_ui(callback.message, state)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="> 100%", callback_data="slot_log:100"),
                    InlineKeyboardButton(text="≤ 120%", callback_data="slot_log:120"),
                ],
                [
                    InlineKeyboardButton(text="≤ 140%", callback_data="slot_log:140"),
                    InlineKeyboardButton(text="≤ 160%", callback_data="slot_log:160"),
                ],
                [
                    InlineKeyboardButton(text="≤ 180%", callback_data="slot_log:180"),
                    InlineKeyboardButton(text="Не ограничивать", callback_data="slot_log:none"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:coef")],
            ]
        )
        msg = await callback.message.answer(
            "Шаг 4 из 7 — логистика.\n\n"
            "Wildberries показывает для разных складов логистический коэффициент в процентах.\n"
            "Выбери максимальный коэффициент логистики, который тебя устраивает:",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(SlotSearchState.logistics)
    elif target == "period":
        await clear_all_ui(callback.message, state)
        kb = build_period_keyboard()
        msg = await callback.message.answer(
            "Шаг 5 из 7 — период поиска.\n\nНа сколько дней вперёд искать слоты?",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.update_data(awaiting_manual_period=False)
        await state.set_state(SlotSearchState.period_days)
    elif target == "lead":
        await clear_all_ui(callback.message, state)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="1 день", callback_data="slot_lead:1"),
                    InlineKeyboardButton(text="2 дня", callback_data="slot_lead:2"),
                ],
                [
                    InlineKeyboardButton(text="3 дня", callback_data="slot_lead:3"),
                    InlineKeyboardButton(text="5 дней", callback_data="slot_lead:5"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:period")],
            ]
        )
        msg = await callback.message.answer(
            "Шаг 6 из 7 — запас по времени.\n\nЗа сколько дней нужно начинать поиск перед датой слота?",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
        await state.set_state(SlotSearchState.lead_time)


async def on_autobook_choose_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)
    data_cb = callback.data or ""
    try:
        _, account_id = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(account_id=account_id)
    data = await state.get_data()
    transit_warehouses = data.get("transit_warehouses") or []

    if not transit_warehouses:
        drafts = data.get("drafts") or []
        if not drafts:
            await state.clear()
            await send_main_menu(callback.message, state)
            return

        text_lines = ["Выберите черновик из списка:\n"]
        kb_rows = []
        for d in drafts:
            draft_id = d.get("id")
            name = d.get("name")
            text_lines.append(f"• {name} (id: {draft_id})")
            kb_rows.append(
                [
                    InlineKeyboardButton(
                        text=name or f"Черновик {draft_id}",
                        callback_data=f"autobook_choose_draft:{draft_id}",
                    )
                ]
            )

        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

        await _autobook_clear_messages(callback.message, state)
        new_msg = await callback.message.answer("\n".join(text_lines), reply_markup=kb)
        await _autobook_add_message_id(new_msg, state)
        await add_ui_message(state, new_msg.message_id)
        await state.set_state(AutoBookState.choose_draft)
        return

    lines = ["🚀 Автобронирование\n\nВыберите транзитный склад:\n"]
    kb_rows = []
    for tw in transit_warehouses:
        tw_id = tw.get("id")
        name = tw.get("name")
        lines.append(f"• {name}")
        kb_rows.append(
            [InlineKeyboardButton(text=name or tw_id, callback_data=f"autobook_transit:{tw_id}")]
        )

    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_show_accounts")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    await _autobook_clear_messages(callback.message, state)
    new_msg = await callback.message.answer("\n".join(lines), reply_markup=kb)
    await _autobook_add_message_id(new_msg, state)
    await add_ui_message(state, new_msg.message_id)
    await state.set_state(AutoBookState.choose_transit)


async def on_slot_warehouse(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id

    # --- проверяем авторизацию WB ---
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/wb/auth/status",
                params={"telegram_id": telegram_id},
            )
            resp.raise_for_status()
            payload = resp.json()
            authorized = bool(payload.get("authorized"))
    except Exception as e:
        print("Error checking WB auth:", e)
        await callback.message.answer("Не удалось проверить авторизацию WB. Попробуй позже.")
        await callback.answer()
        return

    if not authorized:
        await callback.message.answer(
            "Ты не авторизован в WB ❌\nПерейди в меню → Авторизация WB"
        )
        await callback.answer()
        return

    await callback.answer()
    await clear_all_ui(callback.message, state)

    # ================================================================
    # 1) ПАРСИМ CALLBACK slot_wh_id:<id>
    # ================================================================
    try:
        _, wh_id_str = callback.data.split(":", 1)
        wh_id = int(wh_id_str)
    except Exception:
        await callback.message.answer("Ошибка: неверный ID склада.")
        return

    # ================================================================
    # 2) ДОСТАЁМ ИМЯ СКЛАДА ИЗ FSM
    # ================================================================
    data = await state.get_data()
    name_map = data.get("wh_map", {})  # словарь {id: name}

    warehouse_name = name_map.get(wh_id)
    if not warehouse_name:
        await callback.message.answer("Ошибка: склад не найден. Попробуй снова.")
        return

    # сохраняем склад
    await state.update_data(warehouse=warehouse_name)

    print("WAREHOUSE SAVED:", warehouse_name)

    # ================================================================
    # 3) ПОКАЗЫВАЕМ ШАГ «ТИП ПОСТАВКИ»
    # ================================================================
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Короба", callback_data="slot_supply:box"),
                InlineKeyboardButton(text="🟫 Монопаллеты", callback_data="slot_supply:mono"),
            ],
            [
                InlineKeyboardButton(text="✉️ Поштучная паллета", callback_data="slot_supply:postal"),
                InlineKeyboardButton(text="🛡 Суперсейф", callback_data="slot_supply:safe"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:warehouse")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 2 из 7 — тип поставки.\n\nВыбери один из вариантов:",
        reply_markup=kb,
    )

    await add_ui_message(state, msg.message_id)
    await state.set_state(SlotSearchState.supply_type)



def build_coef_keyboard(
    start: int = 0,
    end: int = 20,
    per_row: int = 4,
    prefix: str = "slot_coef",
) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"x{i}",
            callback_data=f"{prefix}:{i}",
        )
        for i in range(start, end + 1)
    ]

    keyboard = [
        buttons[i:i + per_row]
        for i in range(0, len(buttons), per_row)
    ]

    keyboard.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:supply")]
        if prefix == "slot_coef"
        else [InlineKeyboardButton(text="⬅️ Назад", callback_data="autobook_back:supply")]
    )

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def build_period_keyboard(prefix: str = "slot_period") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 дня", callback_data=f"{prefix}:3"),
                InlineKeyboardButton(text="7 дней", callback_data=f"{prefix}:7"),
            ],
            [
                InlineKeyboardButton(text="10 дней", callback_data=f"{prefix}:10"),
                InlineKeyboardButton(text="30 дней", callback_data=f"{prefix}:30"),
            ],
            [
                InlineKeyboardButton(text="Ввести даты вручную", callback_data=f"{prefix}:manual"),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=(
                        "slot_back:logistics" if prefix == "slot_period" else "autobook_back:logistics"
                    ),
                )
            ],
        ]
    )


async def on_slot_supply(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, supply_type = data_cb.split(":", 1)
    except Exception:
        supply_type = None

    if not supply_type:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(supply_type=supply_type)

    kb = build_coef_keyboard(0, 20, per_row=4)

    msg = await callback.message.answer(
        "Шаг 3 из 7 — максимальный коэффициент.\n\n"
        "Выбери максимальный коэффициент бронирования:",
        reply_markup=kb,
    )

    await add_ui_message(state, msg.message_id)
    await state.set_state(SlotSearchState.max_coef)


async def on_slot_coef(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, coef_str = data_cb.split(":", 1)
        max_coef = int(coef_str)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(max_coef=max_coef)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="> 100%", callback_data="slot_log:100"),
                InlineKeyboardButton(text="≤ 120%", callback_data="slot_log:120"),
            ],
            [
                InlineKeyboardButton(text="≤ 140%", callback_data="slot_log:140"),
                InlineKeyboardButton(text="≤ 160%", callback_data="slot_log:160"),
            ],
            [
                InlineKeyboardButton(text="≤ 180%", callback_data="slot_log:180"),
                InlineKeyboardButton(text="Не ограничивать", callback_data="slot_log:none"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:coef")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 4 из 7 — логистика.\n\n"
        "Wildberries показывает для разных складов логистический коэффициент в процентах.\n"
        "Выбери максимальный коэффициент логистики, который тебя устраивает:",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)
    await state.set_state(SlotSearchState.logistics)


async def on_slot_logistics(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, raw = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    if raw == "none":
        max_logistics_coef_percent = None
    else:
        try:
            max_logistics_coef_percent = int(raw)
        except Exception:
            await send_main_menu(callback.message, state)
            return

    # сохраняем лимит логистики
    await state.update_data(max_logistics_coef_percent=max_logistics_coef_percent)
    await state.update_data(awaiting_manual_period=False)

    kb = build_period_keyboard()

    msg = await callback.message.answer(
        "Шаг 5 из 7 — период поиска.\n\nНа сколько дней вперёд искать слоты?",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(SlotSearchState.period_days)


async def _show_lead_time_step(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1 день",
                    callback_data="slot_lead:1"
                ),
                InlineKeyboardButton(
                    text="2 дня",
                    callback_data="slot_lead:2"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="3 дня",
                    callback_data="slot_lead:3"
                ),
                InlineKeyboardButton(
                    text="5 дней",
                    callback_data="slot_lead:5"
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:period")],
        ]
    )

    msg = await message.answer(
        "Шаг 6 из 7 — Лид тайм поставки.\n\n"
        "Укажите срок необходимый вам для подготовки отгрузки (лид- тайм):\n"
        "Дата сдвигается ежедневно\n"
        "Этот период - запас времени, который необходим вам, чтобы успеть сдать поставку\n"
        "Поможет избежать поиска поставок, которые вы не сможете отгрузить\n"
        "При выборе 0 дней, бот будет искать поставки день в день\n"
        "WВ примет у вас поставку с тем же коэффициентом, если вы привезёте её в течении 24 часов после запланированной даты\n"
        "Как правило самые низкие коэффициенты появляются за 0-2 дня до даты приемки, т.к. селлеры начинают массово отменять поставки, которые бронировали заранее.\n",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(SlotSearchState.lead_time)


async def on_slot_period(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_cb = callback.data or ""
    try:
        _, raw = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    if raw == "manual":
        text = (
            "Отправьте сообщение с периодом ДД.ММ.ГГГГ-ДД.ММ.ГГГГ без пробелов\n"
            "Дефис \"-\" обязателен\n"
            "Пример: 01.01.2025-31.01.2025"
        )
        msg = await callback.message.answer(text)
        await add_ui_message(state, msg.message_id)
        await state.update_data(awaiting_manual_period=True)
        await state.set_state(SlotSearchState.period_days)
        return

    mapping = {
        "3": 3,
        "7": 7,
        "10": 10,
        "30": 30,
    }

    period_days = mapping.get(raw)
    if period_days is None:
        await send_main_menu(callback.message, state)
        return

    today = date.today()
    search_period_from = today.isoformat()
    search_period_to = (today + timedelta(days=period_days)).isoformat()

    await state.update_data(
        period_days=period_days,
        search_period_from=search_period_from,
        search_period_to=search_period_to,
        awaiting_manual_period=False,
    )

    await _show_lead_time_step(callback.message, state)


async def on_slot_period_manual_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("awaiting_manual_period"):
        await message.answer("Пожалуйста, выбери период с помощью кнопок выше.")
        return

    raw = (message.text or "").strip()
    pattern = r"^(\d{2})\.(\d{2})\.(\d{4})-(\d{2})\.(\d{2})\.(\d{4})$"
    match = re.fullmatch(pattern, raw)
    if not match:
        msg = await message.answer(
            "Неверный формат. Введите даты как ДД.ММ.ГГГГ-ДД.ММ.ГГГГ без пробелов."
        )
        await add_ui_message(state, msg.message_id)
        return

    try:
        from_dt = datetime.strptime(".".join(match.group(1, 2, 3)), "%d.%m.%Y").date()
        to_dt = datetime.strptime(".".join(match.group(4, 5, 6)), "%d.%m.%Y").date()
    except ValueError:
        msg = await message.answer("Не удалось распознать даты. Проверь формат и повтори попытку.")
        await add_ui_message(state, msg.message_id)
        return

    if from_dt > to_dt:
        msg = await message.answer("Дата начала должна быть не позже даты окончания периода.")
        await add_ui_message(state, msg.message_id)
        return

    period_days = (to_dt - from_dt).days + 1

    await state.update_data(
        period_days=period_days,
        search_period_from=from_dt.isoformat(),
        search_period_to=to_dt.isoformat(),
        awaiting_manual_period=False,
    )

    await _show_lead_time_step(message, state)


def build_weekday_keyboard(
    selected: set[str], prefix: str = "slot_day", back_callback: str = "slot_back:lead"
) -> InlineKeyboardMarkup:
    names = [
        ("mon", "Пн"),
        ("tue", "Вт"),
        ("wed", "Ср"),
        ("thu", "Чт"),
        ("fri", "Пт"),
        ("sat", "Сб"),
        ("sun", "Вс"),
    ]

    buttons = []
    for key, label in names:
        mark = "✅" if key in selected else "⬜️"
        buttons.append(
            InlineKeyboardButton(text=f"{label} {mark}", callback_data=f"{prefix}:{key}")
        )

    rows = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])

    rows.append([InlineKeyboardButton(text="➡️ Готово", callback_data=f"{prefix}:done")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def on_slot_lead(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    # --- читаем выбранный lead_time
    _, raw = callback.data.split(":", 1)
    mapping = {"1": 1, "2": 2, "3": 3, "5": 5}
    lead_time_days = mapping.get(raw)
    if lead_time_days is None:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(lead_time_days=lead_time_days)

    # --- создаём данные: все дни включены по умолчанию
    selected = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
    await state.update_data(selected_days=selected)

    kb = build_weekday_keyboard(selected)

    msg = await callback.message.answer(
        "Шаг 7 из 7 — дни недели.\n\n"
        "Выбери, в какие дни можно сдавать поставку:",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(SlotSearchState.weekdays)


async def on_slot_week(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()

    data_cb = callback.data or ""
    _, code = data_cb.split(":", 1)

    data = await state.get_data()
    selected = set(data.get("selected_days", []))

    # ----------------------------
    # Пользователь нажал "Готово"
    # ----------------------------
    if code == "done":
        if selected == {"mon","tue","wed","thu","fri","sat","sun"}:
            weekdays = "daily"
        elif selected == {"mon","tue","wed","thu","fri"}:
            weekdays = "weekdays"
        elif selected == {"sat","sun"}:
            weekdays = "weekends"
        else:
            weekdays = "custom:" + ",".join(sorted(selected))

        await state.update_data(weekdays=weekdays)

        summary = build_slot_summary(await state.get_data())
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Создать задачу", callback_data="slot_confirm:create")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:lead")],
            ]
        )

        msg = await callback.message.answer(summary, reply_markup=kb)
        await add_ui_message(state, msg.message_id)
        await state.set_state(SlotSearchState.confirm)
        return

    # ----------------------------
    # Тоггл дня
    # ----------------------------
    if code in selected:
        selected.remove(code)
    else:
        selected.add(code)

    await state.update_data(selected_days=selected)

    # Перерисовка клавиатуры (очищать UI НЕЛЬЗЯ!)
    kb = build_weekday_keyboard(selected)

    await callback.message.edit_reply_markup(reply_markup=kb)


async def on_slot_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    if data_cb != "slot_confirm:create":
        await send_main_menu(callback.message, state)
        return

    data = await state.get_data()
    telegram_id = callback.from_user.id

    # 1) Получаем user_id через backend
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/users/get-id",
                params={"telegram_id": telegram_id}
            )
            resp.raise_for_status()
            user_id = resp.json().get("user_id")
    except Exception as e:
        print("Error calling /users/get-id:", e)
        await callback.message.answer("Ошибка получения user_id. Попробуй позже.")
        return

    # 2) Подготовка данных задачи
    warehouse = data.get("warehouse")
    supply_type = data.get("supply_type")
    max_coef = data.get("max_coef")
    period_days = data.get("period_days")
    lead_time_days = data.get("lead_time_days")
    weekdays_code = data.get("weekdays")
    search_period_from = data.get("search_period_from")
    search_period_to = data.get("search_period_to")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")

    # → supply_type преобразуем в формат backend (русские названия)
    supply_type_backend = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Поштучная паллета",
        "safe": "Суперсейф",
    }.get(supply_type)

    if warehouse is None:
        await callback.message.answer("Ошибка: склад не выбран.")
        return

    if search_period_from is None:
        search_period_from = date.today().isoformat()

    if search_period_to is None:
        offset = period_days if period_days is not None else 0
        search_period_to = (date.today() + timedelta(days=offset)).isoformat()

    # 3) Формируем запрос /slots/search
    payload = {
        "warehouse": warehouse,
        "supply_type": supply_type_backend,
        "max_booking_coefficient": str(max_coef),
        "max_logistics_percent": max_logistics_coef_percent or 9999,
        "search_period_from": search_period_from,
        "search_period_to": search_period_to,
        "lead_time_days": lead_time_days,
        "weekdays_only": False,
        "telegram_chat_id": telegram_id,
        "user_id": user_id,
    }

    print("\n===== SLOT SEARCH PAYLOAD =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("================================\n")

    # 4) Отправка запроса
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{BACKEND_URL}/slots/search", json=payload)
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        print("Error calling /slots/search:", e)
        await callback.message.answer("Ошибка создания задачи на поиск слота.")
        return

    # 4.1) Отправляем пользователю полный список слотов (если есть)
    slot_lines = _extract_slot_lines(result)
    supply_type_text = supply_type_backend or str(supply_type)
    logistics_text = (
        f"до {max_logistics_coef_percent}%" if max_logistics_coef_percent is not None else "Не ограничивать"
    )

    header_lines = [
        "Поиск слота запущен ✅",
        f"Склад: {warehouse}",
        f"Тип поставки: {supply_type_text}",
        f"Макс. коэффициент бронирования: {max_coef}",
        f"Логистика: {logistics_text}",
        f"Окно: {search_period_from} → {search_period_to}",
    ]

    if slot_lines:
        header_lines.extend(["", f"🎯 Найдено слотов уже сейчас: {len(slot_lines)}", ""])
        messages_to_send = _chunk_text_lines(header_lines + slot_lines)
    else:
        messages_to_send = ["\n".join(header_lines)]

    for text in messages_to_send:
        try:
            await callback.message.answer(text)
        except Exception as e:
            print("Error sending slot list to user:", e)

    # 5) Переход в список задач
    await state.clear()
    await _do_main_menu_my_searches(callback.message, state, telegram_id)


async def on_autobook_load(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await clear_all_ui(callback.message, state)

    telegram_id = callback.from_user.id

    try:
        _, tid_str = callback.data.split(":")
        request_id = int(tid_str)
    except:
        await callback.message.answer("Некорректный ID задачи.")
        return

    # Получаем user_id через backend
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{BACKEND_URL}/users/get-id",
                params={"telegram_id": telegram_id}
            )
            r.raise_for_status()
            user_id = r.json().get("user_id")
    except Exception:
        await callback.message.answer("Не удалось получить user_id.")
        return

    # Показываем сообщение о загрузке
    loading_msg = await callback.message.answer("⏳ Выполняю автобронирование… Подожди немного.")
    await add_ui_message(state, loading_msg.message_id)

    # Выполняем POST /supplies/load
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/supplies/load",
                params={"user_id": user_id, "request_id": request_id, "debug": False}
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        print("Error /supplies/load:", e)
        msg = await callback.message.answer("Ошибка при создании поставки.")
        await add_ui_message(state, msg.message_id)
        return

    # Готовим ответ
    text = (
        "✔️ Автобронирование выполнено!\n\n"
        f"Склад: {result.get('warehouse')}\n"
        f"Тип поставки: {result.get('supply_type')}\n"
        f"Файл: {result.get('file_saved')}\n"
        f"Выбранная дата: {result.get('chosen_date')}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")]
        ]
    )

    msg = await callback.message.answer(text, reply_markup=kb)
    await add_ui_message(state, msg.message_id)


async def main() -> None:
    """
    Точка входа для бота.
    """
    if not BOT_TOKEN:
        raise RuntimeError(f"BOT_TOKEN is not set or empty. Current value: {BOT_TOKEN!r}")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрация хендлеров
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(wb_auth_command_handler, Command("wb_auth"))
    dp.message.register(cmd_wb_status, Command("wb_status"))
    dp.message.register(cmd_wb_logout, Command("wb_logout"))
    dp.message.register(cmd_create_search, Command("create_search"))
    dp.message.register(cmd_cancel_search, Command("cancel_search"))
    dp.message.register(cmd_restart_search, Command("restart_search"))

    dp.message.register(wb_auth_phone_step, WbAuthState.wait_phone)
    dp.message.register(wb_auth_code_step, WbAuthState.wait_code)
    dp.message.register(on_slot_period_manual_input, SlotSearchState.period_days)
    dp.message.register(on_autobook_period_manual_input, AutoBookNewState.period_days)
    dp.callback_query.register(on_slot_cancel_callback, F.data.startswith("slot_cancel:"))
    dp.callback_query.register(on_slot_restart_callback, F.data.startswith("slot_restart:"))
    dp.callback_query.register(on_slot_delete, F.data.startswith("slot_delete:"))
    dp.callback_query.register(on_slot_warehouse, F.data.startswith("slot_wh:"))
    dp.callback_query.register(on_slot_warehouse, F.data.startswith("slot_wh_id:"))
    dp.callback_query.register(on_slot_supply, F.data.startswith("slot_supply:"))
    dp.callback_query.register(on_slot_coef, F.data.startswith("slot_coef:"))
    dp.callback_query.register(on_slot_logistics, F.data.startswith("slot_log:"))
    dp.callback_query.register(on_slot_period, F.data.startswith("slot_period:"))
    dp.callback_query.register(on_slot_lead, F.data.startswith("slot_lead:"))
    dp.callback_query.register(on_slot_week, F.data.startswith("slot_week:"))
    dp.callback_query.register(on_slot_confirm, F.data == "slot_confirm:create")
    dp.callback_query.register(on_slot_week, F.data.startswith("slot_day:"))
    dp.callback_query.register(on_slot_tasks_page, F.data.startswith("slot_tasks_page:"))
    dp.callback_query.register(on_slot_tasks_main_menu, F.data == "slot_tasks_main_menu")
    dp.callback_query.register(on_slot_task_open, F.data.startswith("slot_task_open:"))
    dp.callback_query.register(on_slot_tasks_back_to_list, F.data == "slot_tasks_back_to_list")
    dp.callback_query.register(on_slot_back, F.data.startswith("slot_back:"))
    dp.callback_query.register(menu_moves_callback, F.data == "menu_moves")
    dp.callback_query.register(moves_page_callback, F.data.startswith("moves_page:"))
    dp.callback_query.register(moves_open_callback, F.data.startswith("moves_open:"))
    dp.callback_query.register(moves_stop_callback, F.data.startswith("moves_stop:"))
    dp.callback_query.register(moves_start_callback, F.data.startswith("moves_start:"))
    dp.callback_query.register(moves_delete_placeholder, F.data == "moves_delete_not_implemented")
    dp.callback_query.register(moves_create_callback, F.data == "moves_create")
    dp.callback_query.register(moves_choose_qty, F.data.startswith("moves_qty:"))
    dp.callback_query.register(moves_confirm_callback, F.data == "moves_confirm")
    dp.callback_query.register(moves_back_qty, F.data == "moves_back_qty")
    dp.callback_query.register(moves_back_to, F.data == "moves_back_to")
    dp.callback_query.register(moves_back_from, F.data == "moves_back_from")
    dp.callback_query.register(moves_back_articles, F.data == "moves_back_articles")
    dp.callback_query.register(moves_choose_account, F.data.startswith("moves_acc:"))
    dp.callback_query.register(moves_choose_article, F.data.startswith("moves_art:"))
    dp.callback_query.register(moves_back_account, F.data == "moves_back_account")
    dp.callback_query.register(moves_back_article, F.data == "moves_back_article")
    dp.callback_query.register(moves_choose_from, F.data.startswith("moves_from:"))
    dp.callback_query.register(moves_choose_to, F.data.startswith("moves_to:"))
    dp.callback_query.register(on_autobook_task_chosen, F.data.startswith("autobook_task:"))
    dp.callback_query.register(on_autobook_wh_page, F.data.startswith("autobook_wh_page:"))
    dp.callback_query.register(on_autobook_warehouse, F.data.startswith("autobook_wh_id:"))
    dp.callback_query.register(on_autobook_supply, F.data.startswith("autobook_supply:"))
    dp.callback_query.register(on_autobook_coef, F.data.startswith("autobook_coef:"))
    dp.callback_query.register(on_autobook_logistics, F.data.startswith("autobook_log:"))
    dp.callback_query.register(on_autobook_period, F.data.startswith("autobook_period:"))
    dp.callback_query.register(on_autobook_lead, F.data.startswith("autobook_lead:"))
    dp.callback_query.register(on_autobook_week, F.data.startswith("autobook_day:"))
    dp.callback_query.register(on_autobook_back, F.data.startswith("autobook_back:"))
    dp.callback_query.register(on_autobook_from_search, F.data.startswith("autobook_from_search:"))
    dp.callback_query.register(on_autobook_choose_account, F.data.startswith("autobook_choose_account:"))
    dp.callback_query.register(on_autobook_choose_draft, F.data.startswith("autobook_choose_draft:"))
    dp.callback_query.register(on_autobook_start, F.data.startswith("autobook_start:"))
    dp.callback_query.register(on_autobook_stop, F.data.startswith("autobook_stop:"))
    dp.callback_query.register(on_autobook_open, F.data.startswith("autobook_open:"))
    dp.callback_query.register(on_autobook_back_to_list, F.data == "autobook_back_to_list")
    dp.callback_query.register(on_autobook_main_menu, F.data == "autobook_main_menu")
    dp.callback_query.register(on_autobook_page, F.data.startswith("autobook_page:"))
    dp.callback_query.register(on_autobook_delete, F.data.startswith("autobook_delete:"))
    dp.callback_query.register(on_autobook_show_accounts, F.data == "autobook_show_accounts")
    dp.callback_query.register(on_autobook_transit, F.data.startswith("autobook_transit:"))
    dp.callback_query.register(on_autobook_confirm, F.data == "autobook_confirm")
    dp.callback_query.register(on_slot_cancel_callback, F.data.startswith("slot_cancel:"))
    dp.callback_query.register(on_slot_restart_callback, F.data.startswith("slot_restart:"))
    dp.callback_query.register(on_slot_delete, F.data.startswith("slot_delete:"))
    dp.callback_query.register(on_autobook_task_chosen, F.data.startswith("autobook_task:"))
    dp.callback_query.register(on_autobook_from_search, F.data.startswith("autobook_from_search:"))
    dp.message.register(autobook_choose_account_step, AutoBookState.choose_account)
    dp.callback_query.register(on_autobook_choose_account, F.data.startswith("autobook_choose_account:"))
    dp.callback_query.register(on_autobook_choose_draft, F.data.startswith("autobook_choose_draft:"))
    dp.callback_query.register(on_autobook_start, F.data.startswith("autobook_start:"))
    dp.callback_query.register(on_autobook_stop, F.data.startswith("autobook_stop:"))
    dp.callback_query.register(on_autobook_open, F.data.startswith("autobook_open:"))
    dp.callback_query.register(on_autobook_back_to_list, F.data == "autobook_back_to_list")
    dp.callback_query.register(on_autobook_main_menu, F.data == "autobook_main_menu")
    dp.callback_query.register(on_autobook_page, F.data.startswith("autobook_page:"))
    dp.callback_query.register(on_autobook_delete, F.data.startswith("autobook_delete:"))
    dp.callback_query.register(on_autobook_show_accounts, F.data == "autobook_show_accounts")
    dp.callback_query.register(on_autobook_transit, F.data.startswith("autobook_transit:"))
    dp.callback_query.register(on_autobook_confirm, F.data == "autobook_confirm")
    dp.callback_query.register(on_slot_tasks_page, F.data.startswith("slot_tasks_page:"))
    dp.callback_query.register(on_slot_tasks_main_menu, F.data == "slot_tasks_main_menu")
    dp.callback_query.register(on_slot_task_open, F.data.startswith("slot_task_open:"))
    dp.callback_query.register(on_slot_tasks_back_to_list, F.data == "slot_tasks_back_to_list")
    dp.callback_query.register(on_slot_auto, F.data.startswith("slot_auto_"))
    dp.callback_query.register(on_menu_slot_tasks, F.data == "menu_slot_tasks")
    dp.callback_query.register(on_slot_back, F.data.startswith("slot_back:"))
    dp.callback_query.register(menu_search_callback, F.data == "menu_search")
    dp.callback_query.register(menu_tasks_callback, F.data == "menu_tasks")
    dp.callback_query.register(tasks_history_search_callback, F.data == "tasks_history_search")
    dp.callback_query.register(tasks_history_autobook_callback, F.data == "tasks_history_autobook")
    dp.callback_query.register(
        tasks_history_slot_search_open_callback,
        F.data.startswith("tasks_history_slot_search_open:"),
    )
    dp.callback_query.register(
        tasks_history_slot_search_cancel_callback,
        F.data.startswith("tasks_history_slot_search_cancel:"),
    )
    dp.callback_query.register(
        tasks_history_autobook_open_callback,
        F.data.startswith("tasks_history_auto_booking_open:"),
    )
    dp.callback_query.register(
        tasks_history_page_callback, F.data.startswith("tasks_history_slot_search_page:")
    )
    dp.callback_query.register(
        tasks_history_page_callback, F.data.startswith("tasks_history_auto_booking_page:")
    )
    dp.callback_query.register(menu_autobook_new_callback, F.data == "menu_autobook")
    dp.callback_query.register(autobook_menu_list_callback, F.data == "autobook_menu:list")
    dp.callback_query.register(autobook_menu_create_callback, F.data == "autobook_menu:create")
    dp.callback_query.register(on_autobook_accounts_page, F.data.startswith("autobook_accounts_page:"))
    dp.callback_query.register(on_autobook_new_refresh, F.data == "autobook_new_refresh")
    dp.callback_query.register(on_autobook_new_account, F.data.startswith("autobook_new_account:"))
    dp.callback_query.register(on_autobook_drafts_page, F.data.startswith("autobook_drafts_page:"))
    dp.callback_query.register(on_autobook_new_draft, F.data.startswith("autobook_new_draft:"))
    dp.callback_query.register(on_autobook_new_confirm, F.data == "autobook_new_confirm")
    dp.callback_query.register(on_autobook_new_cancel, F.data == "autobook_new_cancel")
    dp.callback_query.register(on_autobook_new_retry, F.data == "autobook_new_retry")
    dp.callback_query.register(menu_auth_callback, F.data == "menu_auth")
    dp.callback_query.register(menu_status_callback, F.data == "menu_status")
    dp.callback_query.register(menu_logout_callback, F.data == "menu_logout")
    dp.callback_query.register(menu_help_callback, F.data == "menu_help")
    dp.callback_query.register(menu_main_callback, F.data == "menu_main")
    dp.callback_query.register(on_warehouse_page, F.data.startswith("wh_page:"))
    dp.callback_query.register(on_autobook_load, F.data.startswith("autobook_load:"))


    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
