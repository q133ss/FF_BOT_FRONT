import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv
import httpx

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
AUTBOOK_PAGE_SIZE = 5
MOVES_PAGE_SIZE = 5
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
    Ожидает в data поля: warehouse, supply_type, max_coef, period_days, lead_time_days, weekdays.
    """
    warehouse = data.get("warehouse")
    supply_type = data.get("supply_type")
    max_coef = data.get("max_coef")
    period_days = data.get("period_days")
    lead_time_days = data.get("lead_time_days")
    weekdays_code = data.get("weekdays")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")
    max_logistics_coef_percent = data.get("max_logistics_coef_percent")

    supply_type_text = {
        "box": "Короба",
        "mono": "Монопаллеты",
        "postal": "Почтовая паллета",
        "safe": "Суперсейф",
    }.get(supply_type, str(supply_type))

    weekdays_text = {
        "daily": "Каждый день",
        "weekdays": "Только будни",
        "weekends": "Только выходные",
    }.get(weekdays_code, str(weekdays_code))

    period_text = "Не ограничивать" if period_days is None else f"{period_days} дней"
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
        "postal": "Почтовая паллета",
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
            "postal": "Почтовая паллета",
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
        "postal": "Почтовая паллета",
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
        async with httpx.AsyncClient(timeout=15) as client:
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

    msg = await message.answer("Отлично! Введи код из СМС.", reply_markup=kb_main)
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

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{BACKEND_URL}/auth/code",
                json={"session_id": session_id, "code": code},
            )
            resp.raise_for_status()
            payload = resp.json()
            user_sessions[telegram_id] = session_id
    except Exception as e:
        print("Error calling /auth/code:", e)
        msg_err = await message.answer("Ошибка подтверждения кода. Попробуй снова.", reply_markup=kb_main)
        await add_ui_message(state, msg_err.message_id)
        return

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



async def _do_wb_status(message: Message, state: FSMContext, telegram_id: int) -> None:
    authorized = False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{BACKEND_URL}/auth/status")
            resp.raise_for_status()
            payload = resp.json()
            authorized = payload.get("authorized")
    except Exception as e:
        print("Error calling /auth/status:", e)
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
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{BACKEND_URL}/wb/auth/logout",
                json={"telegram_id": telegram_id},
                timeout=5.0,
            )
            if resp.status_code == 404:
                msg = await message.answer("Ты и так не авторизован в WB.")
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


async def cmd_create_search(message: Message, state: FSMContext) -> None:
    await clear_all_ui(message, state)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Коледино", callback_data="slot_wh:Коледино")],
            [InlineKeyboardButton(text="Тула", callback_data="slot_wh:Тула")],
            [InlineKeyboardButton(text="Электросталь", callback_data="slot_wh:Электросталь")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_main")],
        ]
    )
    msg = await message.answer(
        "Шаг 1 из 7 — выбор склада.\n\nВыбери склад, для которого будем искать слоты:",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)
    await state.set_state(SlotSearchState.warehouse)


async def handle_main_menu_create_search(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await _do_main_menu_create_search(message, state, telegram_id)


async def _do_main_menu_create_search(message: Message, state: FSMContext, telegram_id: int) -> None:
    await clear_all_ui(message, state)
    await cmd_create_search(message, state)


async def _do_main_menu_my_searches(message: Message, state: FSMContext, telegram_id: int) -> None:
    await clear_all_ui(message, state)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{BACKEND_URL}/slot-search/list",
                params={"telegram_id": telegram_id},
            )
            resp.raise_for_status()
            resp_data = resp.json()
            tasks = resp_data.get("requests", [])
    except Exception as e:
        print("Error calling /slot-search/list:", e)
        msg_err = await message.answer("Не удалось получить список задач. Попробуй позже.")
        await add_ui_message(state, msg_err.message_id)
        return

    await state.update_data(slot_tasks=tasks, slot_tasks_page=0, slot_tasks_message_ids=[])

    await _clear_slot_tasks_messages(message, state)
    await _send_slot_tasks_page(message, state, page=0)
    await state.set_state(SlotTasksState.list)


async def handle_main_menu_my_searches(message: Message, state: FSMContext) -> None:
    telegram_id = message.from_user.id
    await _do_main_menu_my_searches(message, state, telegram_id)


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
    await _do_main_menu_my_searches(callback.message, state, callback.from_user.id)


async def menu_autobook_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _do_main_menu_autobook_list(callback.message, state, callback.from_user.id)


async def menu_auth_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await start_wb_auth_flow(callback.message, state, callback.from_user.id)


async def menu_status_callback(callback: CallbackQuery, state: FSMContext) -> None:
    telegram_id = callback.from_user.id
    await callback.answer()
    await clear_all_ui(callback.message, state)

    authorized = False
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
        print("Error /wb/auth/status:", e)
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
        "postal": "Почтовая паллета",
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
        "postal": "Почтовая паллета",
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
    await _do_main_menu_my_searches(callback.message, state, callback.from_user.id)


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
                    InlineKeyboardButton(text="✉️ Почтовая паллета", callback_data="slot_supply:postal"),
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
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="x1", callback_data="slot_coef:1"),
                    InlineKeyboardButton(text="x2", callback_data="slot_coef:2"),
                    InlineKeyboardButton(text="x3", callback_data="slot_coef:3"),
                ],
                [
                    InlineKeyboardButton(text="x4", callback_data="slot_coef:4"),
                    InlineKeyboardButton(text="x5", callback_data="slot_coef:5"),
                    InlineKeyboardButton(text="x10", callback_data="slot_coef:10"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:warehouse")],
            ]
        )
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
                    InlineKeyboardButton(text="≤ 120%", callback_data="slot_log:120"),
                    InlineKeyboardButton(text="≤ 140%", callback_data="slot_log:140"),
                ],
                [
                    InlineKeyboardButton(text="≤ 160%", callback_data="slot_log:160"),
                    InlineKeyboardButton(text="≤ 180%", callback_data="slot_log:180"),
                ],
                [
                    InlineKeyboardButton(text="≤ 200%", callback_data="slot_log:200"),
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
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="3 дня", callback_data="slot_period:3"),
                    InlineKeyboardButton(text="7 дней", callback_data="slot_period:7"),
                ],
                [
                    InlineKeyboardButton(text="10 дней", callback_data="slot_period:10"),
                    InlineKeyboardButton(text="30 дней", callback_data="slot_period:30"),
                ],
                [
                    InlineKeyboardButton(text="Не ограничивать", callback_data="slot_period:none"),
                ],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:logistics")],
            ]
        )
        msg = await callback.message.answer(
            "Шаг 5 из 7 — период поиска.\n\nНа сколько дней вперёд искать слоты?",
            reply_markup=kb,
        )
        await add_ui_message(state, msg.message_id)
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

    # --- Проверяем авторизацию WB ---
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
    # 1) ИЗВЛЕКАЕМ WAREHOUSE ИЗ CALLBACK — С ЗАЩИТОЙ ОТ ДВОЙНЫХ ВЫЗОВОВ
    # ================================================================
    data_cb = callback.data or ""
    parts = data_cb.split(":", 1)
    warehouse = parts[1] if len(parts) == 2 and parts[1].strip() else None

    # Получаем старый state
    data = await state.get_data()

    # Если callback содержит склад → обновляем
    if warehouse:
        await state.update_data(warehouse=warehouse)
    else:
        # Если callback пришёл пустой (второй вызов) → восстанавливаем из state
        warehouse = data.get("warehouse")

    # Если даже state не помог — ошибка
    if not warehouse:
        await callback.message.answer("Ошибка: склад не выбран. Попробуй снова.")
        return

    print("WAREHOUSE SAVED:", warehouse)

    # ================================================================
    # 2) ПОКАЗЫВАЕМ ШАГ «ВЫБОР ТИПА ПОСТАВКИ»
    # ================================================================
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Короба", callback_data="slot_supply:box"),
                InlineKeyboardButton(text="🟫 Монопаллеты", callback_data="slot_supply:mono"),
            ],
            [
                InlineKeyboardButton(text="✉️ Почтовая паллета", callback_data="slot_supply:postal"),
                InlineKeyboardButton(text="🛡 Суперсейф", callback_data="slot_supply:safe"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_main")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 2 из 7 — тип поставки.\n\nВыбери один из вариантов:",
        reply_markup=kb,
    )

    await add_ui_message(state, msg.message_id)
    await state.set_state(SlotSearchState.supply_type)



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

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="x1", callback_data="slot_coef:1"),
                InlineKeyboardButton(text="x2", callback_data="slot_coef:2"),
                InlineKeyboardButton(text="x3", callback_data="slot_coef:3"),
            ],
            [
                InlineKeyboardButton(text="x4", callback_data="slot_coef:4"),
                InlineKeyboardButton(text="x5", callback_data="slot_coef:5"),
                InlineKeyboardButton(text="x10", callback_data="slot_coef:10"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:warehouse")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 3 из 7 — максимальный коэффициент.\n\nВыбери максимальный коэффициент бронирования:",
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
                InlineKeyboardButton(text="≤ 120%", callback_data="slot_log:120"),
                InlineKeyboardButton(text="≤ 140%", callback_data="slot_log:140"),
            ],
            [
                InlineKeyboardButton(text="≤ 160%", callback_data="slot_log:160"),
                InlineKeyboardButton(text="≤ 180%", callback_data="slot_log:180"),
            ],
            [
                InlineKeyboardButton(text="≤ 200%", callback_data="slot_log:200"),
                InlineKeyboardButton(text="Не ограничивать", callback_data="slot_log:none"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:supply")],
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

    await state.update_data(max_logistics_coef_percent=max_logistics_coef_percent)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 дня", callback_data="slot_period:3"),
                InlineKeyboardButton(text="7 дней", callback_data="slot_period:7"),
            ],
            [
                InlineKeyboardButton(text="10 дней", callback_data="slot_period:10"),
                InlineKeyboardButton(text="30 дней", callback_data="slot_period:30"),
            ],
            [
                InlineKeyboardButton(text="Не ограничивать", callback_data="slot_period:none"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:coef")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 5 из 7 — период поиска.\n\nНа сколько дней вперёд искать слоты?",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)
    await state.set_state(SlotSearchState.period_days)


async def on_slot_period(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, raw = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    mapping = {
        "3": 3,
        "7": 7,
        "10": 10,
        "30": 30,
        "none": None,
    }

    period_days = mapping.get(raw)
    if raw not in mapping:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(period_days=period_days)

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
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:logistics")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 6 из 7 — запас по времени.\n\nЗа сколько дней нужно начинать поиск перед датой слота?",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(SlotSearchState.lead_time)


async def on_slot_lead(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, raw = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    mapping = {
        "1": 1,
        "2": 2,
        "3": 3,
        "5": 5,
    }
    lead_time_days = mapping.get(raw)
    if raw not in mapping:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(lead_time_days=lead_time_days)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Каждый день", callback_data="slot_week:daily")],
            [
                InlineKeyboardButton(text="Только будни", callback_data="slot_week:weekdays"),
                InlineKeyboardButton(text="Только выходные", callback_data="slot_week:weekends"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:period")],
        ]
    )

    msg = await callback.message.answer(
        "Шаг 7 из 7 — дни недели.\n\nВ какие дни можно сдавать поставку?",
        reply_markup=kb,
    )
    await add_ui_message(state, msg.message_id)

    await state.set_state(SlotSearchState.weekdays)


async def on_slot_week(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await clear_all_ui(callback.message, state)

    data_cb = callback.data or ""
    try:
        _, code = data_cb.split(":", 1)
    except Exception:
        await send_main_menu(callback.message, state)
        return

    mapping = {
        "daily": "daily",
        "weekdays": "weekdays",
        "weekends": "weekends",
    }
    weekdays = mapping.get(code)
    if weekdays is None:
        await send_main_menu(callback.message, state)
        return

    await state.update_data(weekdays=weekdays)

    data = await state.get_data()
    summary = build_slot_summary(data)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать задачу", callback_data="slot_confirm:create")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="slot_back:lead")],
        ]
    )

    msg = await callback.message.answer(summary, reply_markup=kb)
    await add_ui_message(state, msg.message_id)

    await state.set_state(SlotSearchState.confirm)


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

    # 3) Формируем запрос /slots/search
    payload = {
        "warehouse": warehouse,
        "supply_type": supply_type_backend,
        "max_booking_coefficient": str(max_coef),
        "max_logistics_percent": max_logistics_coef_percent or 9999,
        "search_period_days": period_days if period_days is not None else 30,
        "lead_time_days": lead_time_days,
        "weekdays_only": (weekdays_code == "weekdays"),
        "telegram_chat_id": telegram_id,
        "user_id": user_id,
    }

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

    # 5) Переход в список задач
    await state.clear()
    await _do_main_menu_my_searches(callback.message, state, telegram_id)


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
    dp.callback_query.register(on_slot_cancel_callback, F.data.startswith("slot_cancel:"))
    dp.callback_query.register(on_slot_restart_callback, F.data.startswith("slot_restart:"))
    dp.callback_query.register(on_slot_delete, F.data.startswith("slot_delete:"))
    dp.callback_query.register(on_slot_warehouse, F.data.startswith("slot_wh:"))
    dp.callback_query.register(on_slot_supply, F.data.startswith("slot_supply:"))
    dp.callback_query.register(on_slot_coef, F.data.startswith("slot_coef:"))
    dp.callback_query.register(on_slot_logistics, F.data.startswith("slot_log:"))
    dp.callback_query.register(on_slot_period, F.data.startswith("slot_period:"))
    dp.callback_query.register(on_slot_lead, F.data.startswith("slot_lead:"))
    dp.callback_query.register(on_slot_week, F.data.startswith("slot_week:"))
    dp.callback_query.register(on_slot_confirm, F.data == "slot_confirm:create")
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
    dp.callback_query.register(menu_autobook_callback, F.data == "menu_autobook")
    dp.callback_query.register(menu_auth_callback, F.data == "menu_auth")
    dp.callback_query.register(menu_status_callback, F.data == "menu_status")
    dp.callback_query.register(menu_logout_callback, F.data == "menu_logout")
    dp.callback_query.register(menu_help_callback, F.data == "menu_help")
    dp.callback_query.register(menu_main_callback, F.data == "menu_main")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
