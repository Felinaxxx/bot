import os
import time
import datetime
import logging
import asyncio
from typing import Callable, Dict, Any, Awaitable, List

import nest_asyncio
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message, 
    ReplyKeyboardMarkup, 
    KeyboardButton, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery,
    FSInputFile,
    TelegramObject,
)
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest

from googleapiclient.discovery import build
import gspread_asyncio
from google.oauth2.service_account import Credentials
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Разрешаем вложенные циклы событий (для совместимости со Spyder)
nest_asyncio.apply()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# =====================================================================
# НАСТРОЙКИ, ПУТИ И КОНФИГУРАЦИЯ
# =====================================================================

# Пути к файлам относительно директории скрипта (для VDS/Linux)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_ACCOUNT_PATH = os.path.join(BASE_DIR, 'service_account.json')
CALENDAR_IMAGE_PATH = os.path.join(BASE_DIR, 'Calendar.png')
INFOGRAPHICS_IMAGE_PATH = os.path.join(BASE_DIR, 'infographics.jpg')

ALLOWED_CHAT_ID = -1004425538726

ALLOWED_USER_IDS = [
    782887635,
    363660030,
    352461528,
    552950127,
    928748648
]

BOT_TOKEN = "8841757193:AAFPkkSwV1_gQ10CMUPySEtbStZH0-34sho"
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1UWxDUnH-QW-a_grGkbh-iQ_16JLqXQFRsB2GIIkAA9w/edit"
CALENDAR_ID = 'c_441161ddda8bd0b6d26de7e2fff238cb8f1fa9d94fe9732f7595f464806abcb8@group.calendar.google.com'

SCOPES_CALENDAR = ['https://www.googleapis.com/auth/calendar.readonly']
SCOPES_SHEETS = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        link_preview_is_disabled=True
    )
)
dp = Dispatcher()

# Хранилище ID сообщений "Главное меню"
last_main_menu_messages = {}

# =====================================================================
# ГЛОБАЛЬНОЕ ХРАНИЛИЩЕ КЭША
# =====================================================================
CACHE: Dict[str, Any] = {
    "username_map": {},
    "sheet_rows": {
        "presidency": [],
        "student_dep": [],
        "project_dep": [],
        "groups": [],
        "clubs": []
    },
    "calendar_events": {
        "0_7": [],
        "7_14": []
    },
    "duties": []
}

# =====================================================================
# МИДДЛВАРЬ АВТОРИЗАЦИИ
# =====================================================================
class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user = data.get("event_from_user")
        bot_instance = data.get("bot")

        if not user:
            return await handler(event, data)

        # 1. Проверка по списку исключений (White List)
        if user.id in ALLOWED_USER_IDS:
            return await handler(event, data)

        # 2. Проверка участника закрытого чата
        try:
            member = await bot_instance.get_chat_member(chat_id=ALLOWED_CHAT_ID, user_id=user.id)
            if member.status in ["member", "administrator", "creator"]:
                return await handler(event, data)
        except Exception as e:
            logging.error(f"Ошибка проверки статуса в чате: {e}")

        # Отказ в доступе
        if hasattr(event, "message") and event.message:
            await event.message.answer(
                "⛔ <b>Доступ ограничен.</b>\n"
                "Этот бот предназначен только для участников закрытого чата Профкома."
            )
        elif hasattr(event, "callback_query") and event.callback_query:
            await event.callback_query.answer(
                "⛔ Доступ только для участников закрытого чата!", 
                show_alert=True
            )
            
        return None

# =====================================================================
# СЛУЖЕБНЫЕ ФУНКЦИИ GOOGLE API И ОБНОВЛЕНИЯ КЭША
# =====================================================================
def get_credentials():
    return Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES_SHEETS)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_credentials)

def fetch_calendar_events_raw(left_border: int = 0, right_border: int = 7) -> List[dict]:
    """Синхронный запрос к Google Календарю"""
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_PATH, scopes=SCOPES_CALENDAR)
        service = build('calendar', 'v3', credentials=creds)

        now = datetime.datetime.utcnow()
        time_min = (now + datetime.timedelta(days=left_border)).isoformat() + 'Z'
        time_max = (now + datetime.timedelta(days=right_border)).isoformat() + 'Z'

        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        return events_result.get('items', [])
    except Exception as e:
        logging.error(f"Ошибка получения событий Календаря ({left_border}-{right_border}): {e}")
        return []

async def update_all_caches():
    """Фоновая функция: обновляет данные из Таблиц и Календаря в память КЭШа"""
    logging.info("⏳ Запуск обновления КЭШа...")

    try:
        # Устанавливаем жесткий таймаут 60 секунд на всю загрузку КЭШа
        async with asyncio.timeout(60):
            # 1. Обновление Календаря
            events_0_7 = await asyncio.to_thread(fetch_calendar_events_raw, 0, 7)
            events_7_14 = await asyncio.to_thread(fetch_calendar_events_raw, 7, 14)
            CACHE["calendar_events"]["0_7"] = events_0_7
            CACHE["calendar_events"]["7_14"] = events_7_14

            # 2. Обновление Google Таблиц
            client = await agcm.authorize()
            sheet = await client.open_by_url(SPREADSHEET_URL)
            
            # Загрузка юзернеймов (Лист 2)
            worksheet2 = await sheet.get_worksheet(1)
            rows_sheet2 = await worksheet2.get("A1:B50")
            
            username_map = {}
            for row in rows_sheet2:
                if len(row) >= 2 and row[0].strip():
                    fio = row[0].strip()
                    username = row[1].strip().replace("@", "")
                    username_map[fio] = username
            CACHE["username_map"] = username_map

            # Загрузка структурных подразделений (Лист 1)
            worksheet1 = await sheet.get_worksheet(0)
            ranges = {
                "presidency": (3, 14),
                "project_dep": (16, 41),
                "student_dep": (43, 75),
                "groups": (77, 112),
                "clubs": (114, 122)
            }

            for key, (start, end) in ranges.items():
                raw_rows = await worksheet1.get(f"A{start}:Z{end}")
                formatted = []
                for row in raw_rows:
                    if not row:
                        continue
                    row_copy = list(row)
                    if len(row_copy) > 1 and str(row_copy[1]).strip():
                        fio = str(row_copy[1]).strip()
                        uname = username_map.get(fio)
                        row_copy[1] = f"<a href='tg://resolve?domain={uname}'>{fio}</a>" if uname else fio
                    formatted.append(row_copy)
                CACHE["sheet_rows"][key] = formatted

            # 3. Загрузка Дежурств (Лист 3)
            worksheet3 = await sheet.get_worksheet(2)
            duties_rows = await worksheet3.get("A2:D50")
            
            formatted_duties = []
            for row in duties_rows:
                if not row or not str(row[0]).strip():
                    continue

                date_str = str(row[0]).strip()
                try:
                    start_dt = datetime.datetime.strptime(date_str, "%d.%m.%Y")
                    end_dt = start_dt + datetime.timedelta(days=6)
                    week_range = f"{start_dt.strftime('%d.%m.%Y')}–{end_dt.strftime('%d.%m.%Y')}"
                except ValueError:
                    week_range = date_str

                duty_persons = []
                raw_duty_persons = []
                for item in row[1:]:
                    fio = str(item).strip()
                    if not fio:
                        continue
                    raw_duty_persons.append(fio)
                    uname = username_map.get(fio)
                    linked_fio = f"<a href='tg://resolve?domain={uname}'>{fio}</a>" if uname else fio
                    duty_persons.append(linked_fio)

                formatted_duties.append({
                    "week": week_range,
                    "persons": duty_persons,
                    "raw_persons": raw_duty_persons
                })

            CACHE["duties"] = formatted_duties
            logging.info("✅ КЭШ успешно обновлен!")

    except TimeoutError:
        logging.error("❌ Превышено время ожидания ответа от Google API (Таймаут 15 сек).")
    except Exception as e:
        logging.error(f"❌ Ошибка обновления КЭШа: {e}")

async def send_formatted_rows_from_cache(callback: CallbackQuery, cache_key: str, title: str):
    """Форматирует и мгновенно выводит данные подразделений из КЭШа"""
    rows = CACHE["sheet_rows"].get(cache_key, [])
    
    if not rows:
        await callback.message.edit_text(
            f"⚠️ В разделе «{title}» пока нет данных или КЭШ обновляется.",
            reply_markup=back_to_categories_keyboard_departments
        )
        await callback.answer()
        return

    text = f"<b>{title}:</b>\n\n"
    
    for row in rows:
        clean_row = [str(cell).strip() for cell in row if str(cell).strip()]
        if not clean_row:
            continue
            
        first_cell = clean_row[0]
        is_header_row = first_cell.isupper() and len(first_cell) > 2 and not first_cell.startswith("<A")

        if is_header_row:
            header_title = f"<b>{first_cell}</b>"
            if len(clean_row) > 1:
                rest_of_row = " — ".join(clean_row[1:])
                text += f"\n🟣 {header_title} — {rest_of_row}\n"  
            else:
                text += f"\n 🟣 {header_title}:\n"
        else:
            row_text = " — ".join(clean_row)
            text += f"• {row_text}\n"

    await callback.message.edit_text(text, reply_markup=back_to_categories_keyboard_departments)
    await callback.answer()

async def delete_main_menu_notification(bot_inst: Bot, user_id: int):
    """Удаляет предыдущее уведомление главное меню"""
    if user_id in last_main_menu_messages:
        try:
            await bot_inst.delete_message(chat_id=user_id, message_id=last_main_menu_messages[user_id])
        except Exception:
            pass
        finally:
            del last_main_menu_messages[user_id]

# =====================================================================
# СЕКЦИЯ КЛАВИАТУР
# =====================================================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Памятки"), KeyboardButton(text="Инфраструктура ПК")],
        [KeyboardButton(text="Календарь"), KeyboardButton(text="Архив")],
        [KeyboardButton(text="Обратная связь")]
    ],
    resize_keyboard=True
)

memo_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="ТЗ", callback_data="specs"), InlineKeyboardButton(text="Ивент", callback_data="event")],
        [InlineKeyboardButton(text="Работа с посетителями", callback_data="visitors")],
        [InlineKeyboardButton(text="Техника Профкома", callback_data="technology")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main")]
    ]
)

struct_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Состав", callback_data="people")],
        [InlineKeyboardButton(text="Отделы", callback_data="departments")],
        [InlineKeyboardButton(text="Инвентарь", callback_data="inventory")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main")]
    ]
)

departments_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="РГ председателя", callback_data="presidency")],
        [InlineKeyboardButton(text="Студенческий отдел", callback_data="student_dep"), InlineKeyboardButton(text="Проектный отдел", callback_data="project_dep")],
        [InlineKeyboardButton(text="Группы", callback_data="groups"), InlineKeyboardButton(text="Клубы", callback_data="clubs")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_struct")]
    ]
)

archive_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Отчеты", callback_data="reports")],
        [InlineKeyboardButton(text="Истории проектов", callback_data="history")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main")]
    ]
)

feedback_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main")]
    ]
)

specs_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Дизайн", callback_data="design"), InlineKeyboardButton(text="Медиа", callback_data="media")],
        [InlineKeyboardButton(text="Техники", callback_data="techs"), InlineKeyboardButton(text="Спонсоры", callback_data="sponsors")],
        [InlineKeyboardButton(text="Бумаги", callback_data="papers"), InlineKeyboardButton(text="Закупки", callback_data="procurement")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_memo")]
    ]
)

visitors_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Выплаты", callback_data="payments"), InlineKeyboardButton(text="Пропуск на машину", callback_data="car_pass")],
        [InlineKeyboardButton(text="Вступление в Профсоюз", callback_data="enter_union"), InlineKeyboardButton(text="Выход из Профсоюза", callback_data="exit_union")],
        [InlineKeyboardButton(text="Прокат", callback_data="rent"), InlineKeyboardButton(text="Профсоюзный билет", callback_data="membership")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_memo")]
    ]
)

payments_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="ГАС и ПГАС", callback_data="sas"), InlineKeyboardButton(text="ГСС и ПГСС", callback_data="sss")],
        [InlineKeyboardButton(text="БДНС и дотации", callback_data="nsd"), InlineKeyboardButton(text="Матпомощь", callback_data="mat_help")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_visitors")]
    ]
)

calendar_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Ближайшие 7 дней", callback_data="this_week"), InlineKeyboardButton(text="Следующие 7 дней", callback_data="next_week")],
        [InlineKeyboardButton(text="Дежурства", callback_data="duties")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main")]
    ]
)

tech_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Бесплатный принтер", callback_data="free_printer")],
        [InlineKeyboardButton(text="Печать на МФУ", callback_data="mfd")],
        [InlineKeyboardButton(text="Печать на принтере Профкома", callback_data="epson")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_memo")]
    ]
)

inv_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Стеллаж ПК", callback_data="shelf")],
        [InlineKeyboardButton(text="Подсобка у СФА", callback_data="north")],
        [InlineKeyboardButton(text="Подсобка в пристройке", callback_data="south")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_struct")]
    ]
)

event_help_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Памятка по организации", callback_data="organiser")],
        [InlineKeyboardButton(text="Профорги и активисты", callback_data="active")],
        [InlineKeyboardButton(text="Рабочая табличка", callback_data="table")],
        [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_memo")]
    ]
)

back_to_categories_keyboard_memo = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_memo")]]
)

back_to_categories_keyboard_struct = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_struct")]]
)

back_to_categories_keyboard_specs = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_specs")]]
)

back_to_categories_keyboard_visitors = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_visitors")]]
)

back_to_categories_keyboard_payments = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_payments")]]
)

back_to_categories_keyboard_departments = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_departments")]]
)

back_to_categories_keyboard_calendar = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_calendar")]]
)

back_to_categories_keyboard_tech = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_tech")]]
)

back_to_categories_keyboard_inv = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_inv")]]
)

back_to_categories_keyboard_event = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_event")]]
)

back_to_categories_keyboard_archive = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_categories_archive")]]
)
   
# =====================================================================
# ОБРАБОТКА КОМАНД И РЕПЛАЙ-КНОПОК
# =====================================================================

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}! 👋\n"
        "Я бот-помощник. Помогу разобраться с процессами в Профкоме.\n"
        "Выбери нужный раздел на панели ниже 👇",
        reply_markup=main_keyboard
    )

@dp.message(Command("update"))
async def cmd_update_cache(message: Message):
    """Ручное обновление КЭШа администраторами"""
    if message.from_user.id not in ALLOWED_USER_IDS:
        await message.answer("⛔ У вас нет прав для выполнения этой команды.")
        return

    status_msg = await message.answer("🔄 Обновляю КЭШ из Google Таблиц и Календаря...")
    await update_all_caches()
    await status_msg.edit_text("✅ <b>КЭШ успешно обновлен!</b>\nВсе данные актуализированы.")

@dp.message(F.text == "Памятки")
async def process_memo(message: Message):
    await delete_main_menu_notification(bot, message.from_user.id)
    await message.answer("Памятки это очень круто!", reply_markup=memo_help_keyboard)

@dp.message(F.text == "Инфраструктура ПК")
async def process_structure(message: Message):
    await delete_main_menu_notification(bot, message.from_user.id)
    await message.answer("Прочитайте информацию об инфраструктуре", reply_markup=struct_help_keyboard)

@dp.message(F.text == "Календарь")
async def process_calendar(message: Message):
    await delete_main_menu_notification(bot, message.from_user.id)
    
    memo_text = (
        "<b>Календарь Профкома</b>\n\n"
        "🟣 <b>Добавление или перенос мероприятия.</b>\n"
        "— согласовать дату с курирующим заместителем;\n"
        "— написать ответственному за календарь <a href='tg://resolve?domain=Zuzechka'>Азизу</a>;\n\n"
        "🟠 <b>Добавление календаря в Google-аккаунт.</b>\n"
        "— войдите на ваш аккаунт physics.msu.ru\n"
        "— перейдите <a href='https://calendar.google.com/calendar/u/0/r?cid=c_k6v8sf03tunshtf2qd6hil5960@group.calendar.google.com'>по этой ссылке</a>;\n"
        "— подтвердите добавление календаря.\n\n"
        "<b>По всем вопросам можно обращаться к ответственному за календарь.</b>"
    )

    if os.path.exists(CALENDAR_IMAGE_PATH):
        photo = FSInputFile(CALENDAR_IMAGE_PATH)
        await message.answer_photo(photo=photo, caption=memo_text, reply_markup=calendar_help_keyboard)
    else:
        await message.answer(text=memo_text, reply_markup=calendar_help_keyboard)

@dp.message(F.text == "Архив")
async def process_archive(message: Message):
    await delete_main_menu_notification(bot, message.from_user.id)
    await message.answer("Придумайте наполнение:", reply_markup=archive_help_keyboard)

@dp.message(F.text == "Обратная связь")
async def process_feedback(message: Message):
    await delete_main_menu_notification(bot, message.from_user.id)
    await message.answer("Пишите @donbasster", reply_markup=feedback_help_keyboard)

@dp.message(F.text == "⬅️ Главное меню")
async def back_to_global_main(message: Message):
    await message.answer(
        "Вы вернулись в главное меню. Выберите интересующий раздел:",
        reply_markup=main_keyboard
    )

# =====================================================================
# ОБРАБОТКА ИНЛАЙН-КНОПОК
# =====================================================================

@dp.callback_query(F.data == "reports")
async def inline_reports_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Отчеты о деятельности Профкома</b>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Ежегодные отчеты.</b>\n"
        "— Отчетные материалы и презентации о работе Профкома за прошлые годы доступны по "
        "<a href='https://docs.google.com/spreadsheets/d/1UWxDUnH-QW-a_grGkbh-iQ_16JLqXQFRsB2GIIkAA9w/edit'>ссылке</a>;\n\n"
        "🟠 <b>Финансовые отчеты.</b>\n"
        "— По вопросам финансовой отчетности и смет обращайтесь к председателю Профкома.\n\n"
        "<b>По всем вопросам можно обращаться к председателю.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_archive)
    await callback.answer()

@dp.callback_query(F.data == "history")
async def inline_history_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Истории проектов</b>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>База знаний проектов.</b>\n"
        "— Здесь хранятся архивы прошлых мероприятий, передачи дел и кейсы реализации проектов;\n"
        "— Ознакомиться с архивом проектов можно в "
        "<a href='https://docs.google.com/spreadsheets/d/1UWxDUnH-QW-a_grGkbh-iQ_16JLqXQFRsB2GIIkAA9w/edit'>общей базе</a>;\n\n"
        "🟠 <b>Передача опыта.</b>\n"
        "— Если вы планируете запуск нового проекта, рекомендуется изучить отчеты прошлых организаторов.\n\n"
        "<b>По всем вопросам можно обращаться к руководителю Проектного отдела.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_archive)
    await callback.answer()

@dp.callback_query(F.data == "specs")
async def inline_specs(callback: CallbackQuery):
    await callback.message.edit_text(
        "🟣 <b>Технические задания</b>\n\n"
        "— Технические задания позволяют привлечь группы Профкома к своему проекту;\n"
        "— ТЗ должны отправляться своевременно, для отправки ТЗ используйте меню ниже.",
        reply_markup=specs_help_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "design")
async def inline_design_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Работа дизайн-группы</b>\n\n"
        "ТЗ отправляются через <a href='https://forms.yandex.ru/cloud/64ea063169387244cffb83cb/'>форму</a>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Печатная продукция и цифровое оформление.</b>\n"
        "— ТЗ через форму за 10 дней;\n"
        "— ответственная <a href='tg://resolve?domain=Afanaseva_Anya'>Ася</a>;\n"
        "— имейте в виду, что производство в типографии занимает еще 2-3 дня.\n"
        "— продукцию из типографии на Сухаревской надо забрать самостоятельно.\n\n"
        "🟠 <b>Афиши и стенды.</b>\n"
        "— развешивание афиш требует согласования, этим занимается Ася;\n"
        "— как только афиши согласованы, Ася напишет вам и вы сможете повесить их;\n"
        "— на факультете афиши А5 вешаются возле лифтов, перед этим снимите старые афиши Профкома, место для афиш А3 на стендах Профкома;\n"
        "— в ГЗ вешаются афищи размера А3 на пробковых досках возле лифтов;\n"
        "— в ДСЛ вешаются афиши размера А5 на правом стенде в холлах лифтов 11-16 этажей и одна афиша размера А3 на первом этаже справа от лифтов.\n\n"
        "<b>По всем вопросам можно обращаться к председателю группы дизайна.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_specs)
    await callback.answer()

@dp.callback_query(F.data == "media")
async def inline_media_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Работа медиа-группы</b>\n\n"
        "ТЗ отправляются через <a href='https://forms.yandex.ru/cloud/63f0d41b90fa7b6335a39338/'>форму</a>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Группа Профкома ВК.</b>\n"
        "— ТЗ через форму за 72 часа;\n"
        "— ответственная <a href='tg://resolve?domain=willec'>Ева</a>;\n"
        "— если нужна история, необходимо написать непосредственно Еве за 1-2 дня до даты публикации истории (учитывайте, что история провисит 12/24/48 часов).\n\n"
        "🟠 <b>Телеграм-канал Профкома.</b>\n"
        "— ответственная <a href='tg://resolve?domain=willec'>Ева</a>;\n"
        "— чтобы сделать пост, необходимо написать Маше за несколько дней до публикации поста;\n"
        "— чтобы выложить кружок с мероприятия, необходимо договориться с Машей и самостоятельно прислать ей кружок в день мероприятия (снять может фотограф, видеограф или организатор);\n"
        "— чтобы ваше мероприятие попало в дайджест, необходимо сообщить о нём Маше до вечера субботы.\n\n"
        "🟣 <b>Фото</b>\n"
        "— ТЗ через форму за 1-2 недели до мероприятия;\n"
        "— ответственный <a href='tg://resolve?domain=lexxegek'>Саша</a>.\n\n"
        "🟠 <b>Видео</b>\n"
        "— ТЗ на вертикальный видеоотчёт с мероприятия за 2 недели до мероприятия;\n"
        "— ТЗ на проморолик мероприятия за 2 недели до публикации проморолика;\n"
        "— ответственная <a href='tg://resolve?domain=willec'>Ева</a>;\n"
        "— если вы самостоятельно снимаете клипы или промо, перед публикацией на ресурсах Профкома их нужно прислать на проверку Еве.\n\n"
        "🟣 <b>Техника медиа</b>\n"
        "— ответственный <a href='tg://resolve?domain=lexxegek'>Саша</a>;\n"
        "— чтобы взять нашу технику, напишите Саше, а потом аккуратно верните на место.\n\n"
        "<b>По вопросам направлений можно обращаться к ответственным.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_specs)
    await callback.answer()

@dp.callback_query(F.data == "techs")
async def inline_techs_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Работа группы техников</b>\n\n"
        "ТЗ отправляются через <a href='https://forms.yandex.ru/cloud/65ce100473cee7000f3ca174/'>форму</a>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Техника на мероприятие.</b>\n"
        "— ТЗ через форму за 2 недели;\n"
        "— ответственная <a href='tg://resolve?domain=Nastasya_fed'>Настя</a>;\n\n"
        "🟠 <b>Время застройки.</b>\n"
        "— Для подготовки площадки к мероприятию понадобится время, учитывайте это при бронировании аудитории:\n"
        "— демонстрация материалов – 20 минут;\n"
        "— аудиосопровождение – 40-60 минут;\n"
        "— трансляция – 1 час.\n"
        "— музыкальное мероприятие – 3 часа.\n\n"
        "🟣 <b>Медиафайлы</b>\n"
        "— ТЗ через форму за 3 дня до мероприятия;\n\n"
        "<b>По всем вопросам можно обращаться к председателю группы техники.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_specs)
    await callback.answer()

@dp.callback_query(F.data == "sponsors")
async def inline_sponsors_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Работа группы по социальному партнерству</b>\n\n"
        "ТЗ отправляются через <a href='tg://resolve?domain=profcomff'>Ахмеда</a>;\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Мерч на мероприятие.</b>\n"
        "— напишите Ахмеду за несколько недель до мероприятия\n"
        "— в сообщении уточните что, для кого и сколько нужно;\n"
        "— партнеры мероприятия могут попросить вас что-либо сделать, держите контакт с группой медиа и ведущим на мероприятии.\n\n"
        "<b>По всем вопросам можно обращаться к председателю группы по социальному партнерству.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_specs)
    await callback.answer()

@dp.callback_query(F.data == "papers")
async def inline_papers_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Работа с бумагами</b>\n\n"
        "ТЗ отправляются через <a href='https://docs.google.com/forms/d/e/1FAIpQLSc3kqBhVKZKrIjRZlgal7ir8SHHx4LCkoi-XMWPFNoW4WyXzw/viewform'>форму</a>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Бумаги на меропрития.</b>\n"
        "— ТЗ через форму за 2 недели <b>до анонса мероприятия в соцсетях</b>;\n"
        "— ответственная <a href='tg://resolve?domain=pniazdhdoeyc'>Саша</a>;\n\n" 
        "🟠 <b>Функционал бумаг.</b>\n"
        "— проведение мероприятия на факультете (кроме кабинета Профкома);\n"
        "— проход людей без пропуска МГУ;\n"
        "— разрешение на въезд во внутренинй двор;\n"
        "— внос и вынос инвентаря.\n\n"
        "🟣 <b>Важные уточнения</b>\n"
        "— подписанные оригиналы бумаг нужно отнести на охрану самостоятельно;\n"
        "— оригинал бумаги на въезд на задний двор сдается на дальнюю охрану (шлагбаум с другой стороны факультета);\n"
        "— проход на факультет возможен с 8 до 20 часов, в субботу с 8 до 18.\n\n"
        "<b>По всем вопросам можно обращаться к ответственному за бумаги.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_specs)
    await callback.answer()

@dp.callback_query(F.data == "procurement")
async def inline_procurements_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Наличные закупки</b>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>Составление сметы.</b>\n"
        "— смету нужно составить за неделю и согласовать с курирующим заместителем;\n"
        "— согласованную смету надо одобрить у председателя;\n" 
        "— после одобрения продукцию в смете следует закупить.\n\n" 
        "🟠 <b>Отчет по закупкам.</b>\n"
        "— после покупки надо отправить председателю одним сообщением чек и информацию:\n"
        "— проект, что куплено;\n"
        "— отдел Профкома;\n"
        "— сумма;\n"
        "— получатель перевода (фамилия, номер телефона, предпочтительный банк);\n\n"       
        "<b>По всем вопросам можно обращаться к председателю Профкома.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_specs)
    await callback.answer()

@dp.callback_query(F.data == "visitors")
async def inline_visitors(callback: CallbackQuery):
    await callback.message.edit_text(
        "🟣 <b>Работа с посетителями</b>\n\n"
        "🟠 <b>Ниже представлены инструкции для обслуживания посетителей кабинета</b>",
        reply_markup=visitors_help_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "people")
async def inline_people(callback: CallbackQuery):
    memo_text = (
        "🟣  <b>Председатель Профкома – <a href='tg://resolve?domain=sima_samchenko'>Серафима</a></b>\n\n"
        "🟠  <b>Подробную информацию о структуре можно посмотреть в <a href='https://docs.google.com/spreadsheets/d/1_OuaLM9iRSNDzhRGEVOcFv_ctReLG6kh0zxnpFDeDJk/edit?gid=485158768#gid=485158768'>таблице</a></b>"
    )
    try:
        await callback.message.delete()
    except Exception:
        pass

    if os.path.exists(INFOGRAPHICS_IMAGE_PATH):
        photo = FSInputFile(INFOGRAPHICS_IMAGE_PATH)
        await callback.message.answer_photo(
            photo=photo,
            caption=memo_text,
            reply_markup=back_to_categories_keyboard_struct
        )
    else:
        await callback.message.answer(
            text=memo_text,
            reply_markup=back_to_categories_keyboard_struct
        )
    await callback.answer()

@dp.callback_query(F.data == "departments")
async def inline_departments(callback: CallbackQuery):
    memo_text = (
        "<b>Отделы Профкома</b>\n\n"
        "<b>Профком состоит из РГ председателя и четырех отделов</b>\n\n"
        "🟠 <b>РГ председателя.</b>\n"
        "— орган управления Профкомом, состоящий из председателя и его заместителей-руководителей отделов. Занимается развитием внутренней структуры Профкома и выстраиванием взаимоотношений с администрацией факультета и другими студенческими организациями;\n\n"
        "🟣 <b>Проектный отдел.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=Zuzechka'>Азиз</a>;\n"
        "— отдел занимается подготовкой и проведением ивентовых мероприятий;\n\n" 
        "🟠 <b>Студенческий отдел.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=AlentevDV'>Денис</a>;\n"
        "— отдел занимается реализацией студенческих сервисов и правовых программ;\n\n"
        "🟣 <b>Клубы.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=AlekseyPhys'>Леша</a>;\n"
        "— клубы – это самостоятельные сообщества со своей иерархией, порядком передачи проектов, распределением задач и регулярным проведением мероприятий;\n\n"
        "🟠 <b>Группы.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=pniazdhdoeyc'>Саша</a>;\n"     
        "— группы – это Отделение Профкома, обеспечивающая поддержку мероприятий. Зачастую требует от своих членов определенных hard-навыков для работы;\n\n"
        "<b>Полноценную информацию о деятельности каждого из отделов можно посмотреть в <a href='https://docs.google.com/spreadsheets/d/1UWxDUnH-QW-a_grGkbh-iQ_16JLqXQFRsB2GIIkAA9w/edit?gid=0#gid=0'>Тройных списках</a> и в меню ниже</b>\n"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=departments_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "payments")
async def inline_payments_memo(callback: CallbackQuery):
    memo_text = (
        "🟣 <b>Студентам Физического факультета полагаются разные выплата, среди них:</b>\n\n"
        "— ГАС и ПГАС;\n"
        "— ГСС и ПГСС;\n"
        "— БДНС и дотации контрактникам;\n"
        "— материальная поддержка;\n\n"
        "<b>Для подробной информации по выплатам пользуйтесь меню ниже</b>\n"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=payments_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "car_pass")
async def inline_car_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Оформление автомобильных пропусков</b>\n"
        "— ответственный <a href='tg://resolve?domain=Ahrimaaan'>Андрей</a>;\n\n"
        "🟣 <b>Краткая памятка:</b>\n\n"
        "— заявление на автомобильный пропуск можно заполнить через приложение «Твой ФФ» или виджет в группе Профкома;\n"
        "— бланки лежат на стеллаже возле стола АС;\n"
        "— заполненные согласия нужно положить рядом с бланками на стеллаже АС;\n"
        "— пропуск будет готов в течение месяца, до этого можно пользоваться <a href='https://pass.msu.ru/'>одноразовыми пропусками</a>;\n"
        "— Профком Физфака не делает пропуски для КПП у второма ГУМа.\n\n"
        "<b>По всем вопросам можно обращаться к ответственному.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_visitors)
    await callback.answer()

@dp.callback_query(F.data == "enter_union")
async def inline_enter_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Вступление в Профсоюз</b>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>АС в кабинете:</b>\n"
        "— отправьте человека к Александре Сергеевне;\n\n"
        "🟠 <b>АС не в кабинете:</b>\n"
        "— попросите человека зарегистрироваться в <a>lk.msuprof.com</a>;\n"
        "— далее он должен подтвердить регистрацию через почту;\n"
        "— распечатайте его белую анкету и дайте ему подписать ее;\n"
        "— подписанную анкету положите в папку для вступивших на стеллаже АС;\n"
        "— в крайних случаях можно выдать оранжевую анкету.\n"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_visitors)
    await callback.answer()

@dp.callback_query(F.data == "exit_union")
async def inline_exit_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Выход из Профсоюза</b>\n\n"
        "<b>Краткая памятка:</b>\n\n"
        "🟣 <b>АС в кабинете:</b>\n"
        "— отправьте человека к Александре Сергеевне;\n\n"
        "🟠 <b>АС не в кабинете:</b>\n"
        "— попросите его подождать АС, она работает пн-пт, 11:00-16:00;\n"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_visitors)
    await callback.answer()

@dp.callback_query(F.data == "rent")
async def inline_rent_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Сервисы проката</b>\n"
        "— ответственный <a href='tg://resolve?domain=Karamazysocrat'>Рифат</a>;\n\n"
        "🟣 <b>Краткая памятка:</b>\n\n"
        "— перейдите в приложении «Твой ФФ» на <a href='https://app.profcomff.com/apps/65'>Прокат в Профкоме</a> и нажмите пункт «Админка»;\n"
        "— примите заявку и выдайте прокат;\n"
        "— по возвращении завершите прокат в окне «текущие»;\n"
        "— если прокат вернули с нарушением (вечером или сломан), завершите со страйком.\n\n"
        "<b>По всем вопросам можно обращаться к ответственному.</b>"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_visitors)
    await callback.answer()

@dp.callback_query(F.data == "membership")
async def inline_membership_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Членство в профсоюзе</b>\n\n"
        "🟣 <b>Номер профбилета (карты «Zachet»)</b>\n"
        "— номер профбилета отследить по фамилии и имени в <a href='https://docs.google.com/spreadsheets/d/1cwgF3tgtSxCzjI5MuVkQx0eC2GBsvHFQKLm34gHN05c/edit?gid=26054723#gid=26054723'>базе студентов</a>;\n\n"
        "🟠 <b>Получение карты «Zachet»</b>\n"
        "— для оформления карты нужно прикрепить своё фото в lk.msuprof.com;\n"
        "— карты «Zachet» выдаются профоргу на всю группу, коробка с ними лежит на столе;\n"
        "— попросите профорга отметиться в ведомости о получении карт;\n"
    )
    await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_visitors)
    await callback.answer()

@dp.callback_query(F.data == "sas")
async def inline_sas_memo(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>ГАС и ПГАС</b>\n\n"
        "🟣 <b>ГАС – государственная академическая стипендия</b>\n"
        "Это основная стипендия всех студентов, выплачивается автоматически. Её размер зависит от успеваемости: в случае получения тройки или пересдачи по любому предмету выплата стипендии прекращается:\n"
        "— 3326 рублей, если за последнюю сессию только «хорошо» или при поступлении на факультет;\n"
        "— 3825 рублей, если за последнюю сессию «хорошо» и «отлично»;\n"
        "— 4159 рублей, если за последнюю сессию только «отлично»;\n\n"
        "🟠 <b>ПГАС – повышенная государственная академическая стипендия</b>\n"
        "Эту стипендию получает 10% от всех получающих ГАС. Назначается по результатам конкурса студентам за успехи в одном или нескольких видах деятельности:\n"
        "— учебная;\n"
        "— научно-исследовательская;\n"
        "— спортивная;\n"
        "— культурно-творческая;\n"
        "— общественная;\n\n"
        "Конкурс на ПГАС проходит 3 раза в год: для всех в сентябре и феврале, а для выпускников ещё и в мае. Размер ПГАС определяется в начале каждого семестра, составляет около 13 000 – 15 000 рублей.\n",
        reply_markup=back_to_categories_keyboard_payments
    )
    await callback.answer()

@dp.callback_query(F.data == "sss")
async def inline_sss_memo(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>ГCС и ПГCС</b>\n\n"
        "🟣 <b>ГCС – государственная социальная стипендия</b>\n"
        "Составляет 4990 рублей. Получать ГСС могут студенты, подходящие под определённые категории:\n"
        "— дети-сироты и дети, оставшиеся без попечения родителей;\n"
        "— студенты, потерявшие в период обучения обоих или единственного родителя;\n"
        "— инвалиды I или II группы, инвалиды с детства (в том числе дети-инвалиды);\n"
        "— лица, подвергшиеся воздействию радиации (Чернобыль, Семипалатинск и другие радиационные катастрофы);\n"
        "— инвалиды вследствие военной травмы или заболевания, полученного во время службы;\n"
        "— ветераны боевых действий;\n"
        "— граждане, проходившие военную службу по контракту не менее трёх лет и уволенные по определённым основаниям (окончание контракта, состояние здоровья, организационно-штатные мероприятия, семейные обстоятельства и др.);\n"
        "— студенты, получившие государственную социальную помощь (например, из малоимущей семьи);\n\n"
        "🟠 <b>Подача заявления на ГСС</b>\n"
        "Члены профсоюза могут заполнить заявление в приложении «Твой ФФ»:\n"
        "— заполненное заявление и подтверждающие документы нужно будет принести АС;\n"
        "— ГСС назначается со дня предоставления оригинала справки (в нашем случае совпадает с днем подачи заявления в приложении);\n\n"
        "🟣 <b>ПГСС – повышенная государственная социальная стипендия</b>\n"
        "Размер ПГСС определяется в начале каждого семестра, составляет около 13 000 – 15 000 рублей. Выплачивается только во 2, 3 и 4 семестрах обучения при выполнении следующих условий:\n"
        "— отсутствие оценок «удоволетворительно» за последнюю сессию в том числе по итогам пересдач;\n"
        "— получение ГСС либо возраст до 20 лет при наличии только одного родителя, являющегося инвалидом I группы;\n\n"
        "ПГСС назначается автоматически, никаких заявлений подавать не нужно.\n",
        reply_markup=back_to_categories_keyboard_payments
    )
    await callback.answer()

@dp.callback_query(F.data == "nsd")
async def inline_nsd_memo(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>БДНС и дотация контрактинкам</b>\n\n"
        "🟣 <b>БДНС – база данных нуждающихся студентов</b>\n"
        "Студенты бюджетной формы получения, относящиеся к одной из следующих категорий, могут получать выплату в размере 1200 рублей в месяц:\n"
        "— студенты-сироты;\n"
        "— студенты-инвалиды;\n"
        "— студенты из многодетных семей;\n"
        "— студенты-участники военных действий;\n"
        "— студенты, подвергшиеся воздействию радиации (Чернобыль, Семипалатинск и другие радиационные катастрофы);\n"
        "— студенты, имеющие родителей-инвалидов и/или родителей-пенсионеров;\n"
        "— студенты, являющиеся членами неполных семей;\n"
        "— студенты, находящиеся на диспансерном учёте с хроническими заболеваниями;\n"
        "— студенты, имеющие детей;\n"
        "— студенты, являющиеся членами студенческих семей и проживающие в общежитии, либо не получающие ГАС;\n"
        "— студенты, являющиеся членами малообеспеченных семей и проживающие в общежитии, либо не получающие ГАС;\n"
        "— студенты, имеющие родителей в разводе либо не получающие ГАС и проживающие в общежитии;\n"
        "— студенты, проживающие в Курской, Белгородской, Брянской областях;\n"
        "— студенты, зарегистрированные в новых регионах, получившие гражданство упрощённо, студенты-беженцы;\n"
        "— студенты, проживающие в общежитии (для любого региона, выплачивается при наличии средств в зависимости от приоритета региона).\n\n"
        "🟠 <b>Подача заявления на БДНС</b>\n"
        "Члены профсоюза могут заполнить заявление на БДНС в приложении «Твой ФФ»:\n"
        "— заполненное заявление затем нужно будет принести АС;\n\n"
        "🟣 <b>Дотация контрактникам</b>\n"
        "Студенты контрактной формы обучения–члены Профсоюза могут получать выплату в размере 1200 рублей в месяц при вхождении в одну из следующих категорий:\n"
        "— студенты-сироты;\n"
        "— студенты-инвалиды;\n"
        "— студенты из многодетных семей;\n"
        "— студенты, подвергшиеся воздействию радиации (Чернобыль, Семипалатинск и другие радиационные катастрофы);\n"
        "— студенты, имеющие детей;\n"
        "— студенты, находящиеся на диспансерном учёте с хроническими заболеваниями;\n"
        "— студенты, являющиеся членами неполных семей;\n"
        "— студенты, зарегистрированные в новых регионах, получившие гражданство упрощённо, студенты-беженцы;\n"
        "— студенты, проживающие в Курской, Белгородской, Брянской областях;\n\n"
        "🟠 <b>Подача заявления на дотацию контрактинкам</b>\n"
        "Члены профсоюза могут заполнить заявление на дотацию контрактинкам в приложении «Твой ФФ»:\n"
        "— заполненное заявление затем нужно будет принести АС;\n",
        reply_markup=back_to_categories_keyboard_payments
    )
    await callback.answer()

@dp.callback_query(F.data == "mat_help")
async def inline_mat_help_memo(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>Материальная помощь и материальная поддержка</b>\n\n"
        "🟣 <b>Материальная поддержка</b>\n"
        "Выплачивается студентам бюджетной формы обучения:\n"
        "— 18000 рублей, в случае свадьбы;\n"
        "— 18000 рублей, в случае рождения ребенка;\n"
        "— 18000 рублей, в случае смерти близкого родтсвенника;\n"
        "— Иная сумма, в случае смерти обучающегося;\n"
        "— Иная сумма, в случае участия в соревнованиях за сборную МГУ;\n"
        "— Иная сумма, в случае прохождения дорогостоящего лечения;\n"
        "— Иная сумма, в случае тяжелого материального положения;\n\n"
        "🟠 <b>Материальная помощь</b>\n"
        "Выплачивается студентам-членам Профсоюза при:\n"
        "— прохождении дорогостоящего лечения;\n"
        "— тяжелом материальном положении;\n"
        "— операциях по коррекции зрения;\n"
        "— участии в соревнованиях за сборную МГУ;\n"
        "— иных ситуациях;\n\n"        
        "🟣 <b>Подача заявления на материальную помощь</b>\n"
        "Люди могут заполнить заявление на материальную помощь и материальную поддержку в приложении «Твой ФФ»:\n"
        "— заявление будет рассмотренно на заседании Стипендиальной комиссии или заседании членов Профкома.\n\n",
        reply_markup=back_to_categories_keyboard_payments
    )
    await callback.answer()

@dp.callback_query(F.data == "technology")
async def inline_tech_help_memo(callback: CallbackQuery):
    await callback.message.edit_text("🟣 <b>Техника Профкома:</b>\n\n", reply_markup=tech_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "free_printer")
async def inline_free_printer_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Бесплатный принтер Kyocera P3260dn:</b>\n"
        "— ответственный <a href='tg://resolve?domain=ratatouilleshiroi'>Саша</a>;\n\n"
        "🟣 <b>Краткая памятка:</b>\n"
        "— на данный момент наиболее стабильно печать работает через приложение «Твой ФФ»;\n"
        "— при возникновении ошибки ID, попросите айтишников поменять токен;\n"
        "— при наличии конкретных проблем, скачайте прикрепленный файл.\n\n"
        "<b>По всем вопросам и качеству печати можно обращаться к ответственному.</b>"
    )
    
    printer_file_path = os.path.join(BASE_DIR, "printer.pdf")
    
    if os.path.exists(printer_file_path):
        document = FSInputFile(printer_file_path)
        
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer_document(
            document=document,
            caption=memo_text,
            reply_markup=back_to_categories_keyboard_tech
        )
    else:
        await callback.message.edit_text(
            text=memo_text + "\n\n⚠️ <i>Файл инструкции (printer.pdf) не найден.</i>",
            reply_markup=back_to_categories_keyboard_tech
        )

    await callback.answer()

@dp.callback_query(F.data == "mfd")
async def inline_msd_memo(callback: CallbackQuery):
       memo_text = (
           "<b>МФУ Ricoh Aficio MP C2051:</b>\n"
           "— ответственный <a href='tg://resolve?domain=donbasster'>Карим</a>;\n\n"
           "🟣 <b>Краткая памятка:</b>\n"
           "— подключитесь к Wi-Fi Профкома;\n"
           "— проходите по <a href='https://support.ricoh.com/bb/html/dr_ut_e/re1/model/mpc21/mpc21.htm'>ссылке</a>, в разделе «утилиты» скачиваем файл;\n"
           "— запустите скачанный установщик;\n"
           "— в настройках в разделе «устройства» выберите подраздел «принтеры» и добавьте новый;\n"
           "— выберите «Aficio MP C2051» и загрузите драйверы.\n\n"
           "🟠 <b>Неформатная печать:</b>\n"
           "— ответственная <a href='tg://resolve?domain=Nastasya_fed'>Настя</a>;\n"
           "— за 7 дней до готовности афиш напишите ответственному формат, количество и дедлайн афиш;\n"
           "— не позднее, чем за 2 до готовности афиш пришлите ответственному файлы на печать;\n\n"
           "<b>По всем вопросам можно обращаться к ответственным.</b>"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_tech)
       await callback.answer() 

@dp.callback_query(F.data == "epson")
async def inline_epson_memo(callback: CallbackQuery):
       memo_text = (
           "<b>Принтер Epson L1210:</b>\n"
           "— ответственный <a href='tg://resolve?domain=donbasster'>Карим</a>;\n\n"
           "🟣 <b>Краткая памятка:</b>\n"
           "— подключите принтер к компьютеру через кабель;\n"
           "— проходите по <a href='https://www.epson.eu/en_EU/support/sc/epson-l1210/s/s2091'>ссылке</a>, скачайте «Epson Product setup»;\n"
           "— запустите скачанный установщик;\n"
           "— следуйте инструкциям;\n"
           "— выберите Epson L1210 в настройках печати.\n\n"
           "<b>По всем вопросам можно обращаться к ответственному.</b>"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_tech)
       await callback.answer() 

@dp.callback_query(F.data == "duties")
async def inline_duties(callback: CallbackQuery):
    duties_data = CACHE.get("duties", [])

    text = (
        "<b>Дежурства:</b>\n\n"
        "🟣 <b>Краткая памятка:</b>\n"
        "Дежурные назначаются на неделю, в течение этой недели следят за порядком в кабинете. В конце недели дежурные проводят уборку кабинета:"
        "— убрать лишнее со столов;\n"
        "— протереть везде пыль;\n"
        "— помыть посуду, очистить кофеварку, выбросить мусор на кухне;\n"
        "— выбросить испорченную еду из холодильника;\n"
        "— обновить буклеты в парусе возле учебной части;\n"
        "— найти владельцев лишних вещей на подоконнике или на полу в синем чате;\n"
        "— обновить лист с дежурными;\n"
        "— отправить фото чистого кабинета и и передать дежурство следующей тройке в синем чате;\n\n"
        "<b>График дежурств:</b>\n")

    if not duties_data:
        text += "⚠️ <i>Данные о дежурствах временно недоступны.</i>"
    else:
        today = datetime.date.today()

        for entry in duties_data:
            week_str = entry["week"]
            persons = entry["persons"]
            persons_str = ", ".join(persons) if persons else "<i>Дежурные не указаны</i>"

            is_current_week = False
            try:
                dates = week_str.replace("—", "–").split("–")
                if len(dates) == 2:
                    start_date = datetime.datetime.strptime(dates[0].strip(), "%d.%m.%Y").date()
                    end_date = datetime.datetime.strptime(dates[1].strip(), "%d.%m.%Y").date()
                    
                    if start_date <= today <= end_date:
                        is_current_week = True
            except Exception as e:
                logging.warning(f"Ошибка парсинга даты недели '{week_str}': {e}")

            if is_current_week:
                text += (
                    f"🟠 <b><u>{week_str} (ТЕКУЩАЯ НЕДЕЛЯ)</u></b>\n"
                    f"└ <b>{persons_str}</b>\n\n"
                )
            else:
                text += (
                    f"🟣  <b>{week_str}</b>:\n"
                    f"└ {persons_str}\n\n"
                )

    try:
        await callback.message.edit_text(
            text=text, 
            reply_markup=back_to_categories_keyboard_calendar
        )
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer(
            text=text, 
            reply_markup=back_to_categories_keyboard_calendar
        )

    await callback.answer()

@dp.callback_query(F.data == "inventory")
async def inline_inventory_memo(callback: CallbackQuery):
       memo_text = (
           "<b>Инвентарь Профкома:</b>\n"
           "— ответственный <a href='tg://resolve?domain=ratatouilleshiroi'>Саша</a>;\n\n"
           "🟣 <b>Краткая памятка:</b>\n"
           "Большая часть инвентаря Профкома находится в следующих местах:\n"
           "— стеллаж в кабинете;\n"
           "— подсобка возле СФА;\n"
           "— подсобка в пристройке факультета.\n\n"
           "Воспользуйтесь меню, чтобы узнать содержание каждого места.\n\n"
           "<b>По всем вопросам можно обращаться к ответственным.</b>"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=inv_help_keyboard)
       await callback.answer()
       
@dp.callback_query(F.data == "shelf")
async def inline_shelf_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Стеллаж Профкома:</b>\n"
        "🟣 <b>Навигация по карте:</b>\n"
        "— стеллаж представляет собой 6 столбцов по 5 полок в каждом;\n"
        "— каждый стеллаж выделен под конкретный инвентарь, просьба не захламлять лишним.\n"
    )
    
    picture_file_path = os.path.join(BASE_DIR, "shelf.png")
    
    if os.path.exists(picture_file_path):
        photo = FSInputFile(picture_file_path)
        
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer_photo(
            photo=photo,
            caption=memo_text,
            reply_markup=back_to_categories_keyboard_inv
        )
    else:
        await callback.message.edit_text(
            text=memo_text + "\n\n⚠️ <i>Файл инструкции (printer.pdf) не найден.</i>",
            reply_markup=back_to_categories_keyboard_inv
        )

    await callback.answer()
    
@dp.callback_query(F.data == "north")
async def inline_north_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Северная подсобка:</b>\n"
        "🟣 <b>Навигация по карте:</b>\n"
        "— подсобка делится напополам с ОКДФ;\n"
        "— нашей зоной являются два стеллажа и зона у входа.\n"
    )
    
    picture_file_path = os.path.join(BASE_DIR, "north.png")
    
    if os.path.exists(picture_file_path):
        photo = FSInputFile(picture_file_path)
        
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer_photo(
            photo=photo,
            caption=memo_text,
            reply_markup=back_to_categories_keyboard_inv
        )
    else:
        await callback.message.edit_text(
            text=memo_text + "\n\n⚠️ <i>Файл инструкции (printer.pdf) не найден.</i>",
            reply_markup=back_to_categories_keyboard_inv
        )

    await callback.answer()
    
@dp.callback_query(F.data == "south")
async def inline_south_memo(callback: CallbackQuery):
    memo_text = (
        "<b>Южная подсобка:</b>\n"
        "🟣 <b>Навигация по карте:</b>\n"
        "— подсобка делится напополам с ОКДФ;\n"
        "— нашей зоной являются восемь стеллажей, обозначенные (А-Ж).\n"
    )
    
    picture_file_path = os.path.join(BASE_DIR, "south.png")
    
    if os.path.exists(picture_file_path):
        photo = FSInputFile(picture_file_path)
        
        try:
            await callback.message.delete()
        except Exception:
            pass

        await callback.message.answer_photo(
            photo=photo,
            caption=memo_text,
            reply_markup=back_to_categories_keyboard_inv
        )
    else:
        await callback.message.edit_text(
            text=memo_text + "\n\n⚠️ <i>Файл инструкции (printer.pdf) не найден.</i>",
            reply_markup=back_to_categories_keyboard_inv
        )

    await callback.answer()


@dp.callback_query(F.data == "event")
async def inline_active_memo(callback: CallbackQuery):
       memo_text = (
           "🟣  <b>Помощь по мероприятию:</b>\n"
           "— воспользуйтесь меню ниже.\n"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=event_help_keyboard)
       await callback.answer() 
       
@dp.callback_query(F.data == "active")
async def inline_active_memo(callback: CallbackQuery):
       memo_text = (
           "<b>Работа с активом:</b>\n"
           "— ответственная <a href='tg://resolve?domain=sleppydragon'>Лена</a>;\n\n"
           "🟣 <b>Информирование студентов:</b>\n"
           "— скиньте ответственному публикацию с информацией о дате, месте и программе мероприятия;\n"
           "— крупные мероприятия могут публиковаться в чатах курсов;\n"
           "— не просите своих юных активистов писать в чаты.\n\n"
           "🟠 <b>Поиск актива:</b>\n"
           "— не позднее, чем за три дня напишите ответственному, указав число человек, место, время и задачу;\n"
           "— после назначения активиста поддерживайте с ним связь самостоятельно.\n\n"
           "🟣 <b>Рейтинг групп:</b>\n"
           "— после выполнения задачи активистов, попросите куратора заполнить информацию о нем в табличке рейтинга групп.\n"
           "<b>По всем вопросам можно обращаться к ответственному.</b>"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_event)
       await callback.answer() 
       
@dp.callback_query(F.data == "organiser")
async def inline_organiser_memo(callback: CallbackQuery):
       memo_text = (
           "<b>Организация мероприятий:</b>\n\n"
           "🟣 <b>Краткая памятка:</b>\n"
           "— согласуйте мероприятие с курирующим заместителем;\n"
           "— соберите команду проекта;\n"
           "— разошлите необходимые ТЗ;\n"
           "— проведите мероприятие;\n"
           "— обсудите прошедшее мероприятие с членами команды.\n\n"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_event)
       await callback.answer() 
       
@dp.callback_query(F.data == "table")
async def inline_table_memo(callback: CallbackQuery):
       memo_text = (
           "🟣 <b><a href='https://docs.google.com/spreadsheets/d/1ZW8DEY8_HYRx5dzxob6g16DOkmQJsqUZP7kx-RaFHP4/edit?usp=sharing'>Универсальная рабочая табличка:</a></b>\n"
           "— для работы над своим мероприятием создайте копию и назовите ее под свое мероприятие;\n"
           "— следуйте инструкциям на титульном листе таблицы.\n"
       )
       await callback.message.edit_text(text=memo_text, reply_markup=back_to_categories_keyboard_event)
       await callback.answer() 
# --- НАВИГАЦИЯ НАЗАД ---

@dp.callback_query(F.data == "back_to_categories_archive")
async def inline_back_to_categories_archive(callback: CallbackQuery):
    await callback.message.edit_text("Придумайте наполнение:", reply_markup=archive_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_memo")
async def inline_back_to_categories_memo(callback: CallbackQuery):
    await callback.message.edit_text("Памятки это круто!", reply_markup=memo_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_struct")
async def inline_back_to_categories_struct(callback: CallbackQuery):
    try:
        await callback.message.edit_text("Структура ПК разнообразна", reply_markup=struct_help_keyboard)
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer("Структура ПК разнообразна", reply_markup=struct_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_specs")
async def inline_back_to_categories_specs(callback: CallbackQuery):
    await callback.message.edit_text("Технические задания", reply_markup=specs_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_event")
async def inline_back_to_categories_event(callback: CallbackQuery):
    await callback.message.edit_text(           
        "🟣  <b>Помощь по мероприятию:</b>\n"
        "— воспользуйтесь меню ниже.\n", 
        reply_markup=event_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_tech")
async def inline_back_to_categories_technology(callback: CallbackQuery):
    memo_text = ("🟣 <b>Техника Профкома:</b>\n\n")
    
    try:
        await callback.message.edit_text(
            text=memo_text,
            reply_markup=tech_help_keyboard
        )
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer(
            text=memo_text,
            reply_markup=tech_help_keyboard
        )

    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_inv")
async def inline_back_to_categories_inv(callback: CallbackQuery):
    memo_text = (           
               "<b>Инвентарь Профкома:</b>\n"
               "— ответственный <a href='tg://resolve?domain=ratatouilleshiroi'>Саша</a>;\n\n"
               "🟣 <b>Краткая памятка:</b>\n"
               "Большая часть инвентаря Профкома находится в следующих местах:\n"
               "— стеллаж в кабинете;\n"
               "— подсобка возле СФА;\n"
               "— подсобка в пристройке факультета.\n\n"
               "Воспользуйтесь меню, чтобы узнать содержание каждого места.\n\n"
               "<b>По всем вопросам можно обращаться к ответственным.</b>"
               )
    
    try:
        await callback.message.edit_text(
            text=memo_text,
            reply_markup=inv_help_keyboard
        )
    except TelegramBadRequest:
        await callback.message.delete()
        await callback.message.answer(
            text=memo_text,
            reply_markup=inv_help_keyboard
        )

    await callback.answer()
    
@dp.callback_query(F.data == "back_to_categories_visitors")
async def inline_back_to_categories_visitors(callback: CallbackQuery):
    await callback.message.edit_text(
        "🟣 <b>Работа с посетителями</b>\n\n"
        "🟠 <b>Ниже представлены инструкции для обслуживания посетителей кабинета</b>",
        reply_markup=visitors_help_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_payments")
async def inline_back_to_categories_payments(callback: CallbackQuery):
    await callback.message.edit_text(
        "🟣 <b>Студентам Физического факультета полагаются разные выплата, среди них:</b>\n\n"
        "— ГАС и ПГАС;\n"
        "— ГСС и ПГСС;\n"
        "— БДНС и дотации контрактникам;\n"
        "— материальная поддержка;\n\n"
        "<b>Для подробной информации по выплатам пользуйтесь меню ниже</b>\n",
        reply_markup=payments_help_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_departments")
async def inline_back_to_categories_departments(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>Отделы Профкома</b>\n\n"
        "<b>Профком состоит из РГ председателя и четырех отделов</b>\n\n"
        "🟠 <b>РГ председателя.</b>\n"
        "— орган управления Профкомом, состоящий из председателя и его заместителей-руководителей отделов. Занимается развитием внутренней структуры Профкома и выстраиванием взаимоотношений с администрацией факультета и другими студенческими организациями;\n\n"
        "🟣 <b>Проектный отдел.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=Zuzechka'>Азиз</a>;\n"
        "— отдел занимается подготовкой и проведением ивентовых мероприятий;\n\n" 
        "🟠 <b>Студенческий отдел.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=AlentevDV'>Денис</a>;\n"
        "— отдел занимается реализацией студенческих сервисов и правовых программ;\n\n"
        "🟣 <b>Клубы.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=AlekseyPhys'>Леша</a>;\n"
        "— клубы – это самостоятельные сообщества со своей иерархией, порядком передачи проектов, распределением задач и регулярным проведением мероприятий;\n\n"
        "🟠 <b>Группы.</b>\n"
        "— руководитель – <a href='tg://resolve?domain=pniazdhdoeyc'>Саша</a>;\n"     
        "— группы – это Отделение Профкома, обеспечивающая поддержку мероприятий. Зачастую требует от своих членов определенных hard-навыков для работы;\n\n"
        "<b>Полноценную информацию о деятельности каждого из отделов можно посмотреть в <a href='https://docs.google.com/spreadsheets/d/1UWxDUnH-QW-a_grGkbh-iQ_16JLqXQFRsB2GIIkAA9w/edit?gid=0#gid=0'>Тройных списках</a> и в меню ниже</b>\n", 
        reply_markup=departments_help_keyboard
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_categories_calendar")
async def inline_back_to_categories_calendar(callback: CallbackQuery):
    await delete_main_menu_notification(bot, callback.from_user.id)
    
    memo_text = (
        "<b>Календарь Профкома</b>\n\n"
        "🟣 <b>Добавление или перенос мероприятия.</b>\n"
        "— согласовать дату с курирующим заместителем;\n"
        "— написать ответственному за календарь <a href='tg://resolve?domain=Zuzechka'>Азизу</a>;\n\n"
        "🟠 <b>Добавление календаря в Google-аккаунт.</b>\n"
        "— войдите на ваш аккаунт physics.msu.ru\n"
        "— перейдите <a href='https://calendar.google.com/calendar/u/0/r?cid=c_k6v8sf03tunshtf2qd6hil5960@group.calendar.google.com'>по этой ссылке</a>;\n"
        "— подтвердите добавление календаря.\n\n"
        "<b>По всем вопросам можно обращаться к ответственному за календарь.</b>"
    )
    
    try:
        await callback.message.delete()
    except Exception:
        pass

    if os.path.exists(CALENDAR_IMAGE_PATH):
        photo = FSInputFile(CALENDAR_IMAGE_PATH)
        await callback.message.answer_photo(photo=photo, caption=memo_text, reply_markup=calendar_help_keyboard)
    else:
        await callback.message.answer(text=memo_text, reply_markup=calendar_help_keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_main")
async def inline_back_to_main(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    if user_id in last_main_menu_messages:
        try:
            await bot.delete_message(chat_id=user_id, message_id=last_main_menu_messages[user_id])
        except Exception:
            pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    sent_msg = await callback.message.answer(
        "Вы вернулись в главное меню. Используйте кнопки внизу экрана 👇",
        reply_markup=main_keyboard
    )
    last_main_menu_messages[user_id] = sent_msg.message_id
    await callback.answer()

# =====================================================================
# ФУНКЦИОНАЛ ВЫДАЧИ ТАБЛИЦ ИЗ КЭША
# =====================================================================

@dp.callback_query(F.data == "presidency")
async def show_presidency(callback: CallbackQuery):
    await send_formatted_rows_from_cache(callback, "presidency", "🟣 Состав РГ председателя")
    
@dp.callback_query(F.data == "student_dep")
async def show_students(callback: CallbackQuery):
    await send_formatted_rows_from_cache(callback, "student_dep", "🟣 Состав Студенческого отдела")

@dp.callback_query(F.data == "project_dep")
async def show_project(callback: CallbackQuery):
    await send_formatted_rows_from_cache(callback, "project_dep", "🟣 Состав Проектного отдела")
    
@dp.callback_query(F.data == "groups")
async def show_groups(callback: CallbackQuery):
    await send_formatted_rows_from_cache(callback, "groups", "🟣 Состав Групп")
    
@dp.callback_query(F.data == "clubs")
async def show_clubs(callback: CallbackQuery):
    await send_formatted_rows_from_cache(callback, "clubs", "🟣 Состав Клубов")

# =====================================================================
# ФУНКЦИОНАЛ ВЫДАЧИ КАЛЕНДАРЯ ИЗ КЭША
# =====================================================================

@dp.callback_query(F.data == "this_week")
async def show_this_week(callback: CallbackQuery):
    events = CACHE["calendar_events"].get("0_7", [])

    if not events:
        await callback.answer("📅 На этой неделе нет запланированных мероприятий!", show_alert=True)
        return

    text = "<b>📅 Мероприятия на ближайшие 7 дней:</b>\n\n"

    for event in events:
        summary = event.get('summary', 'Без названия')
        start = event['start'].get('dateTime', event['start'].get('date'))
        
        if 'T' in start:
            dt = datetime.datetime.fromisoformat(start)
            date_str = dt.strftime("%d.%m (%a) в %H:%M")
        else:
            dt = datetime.datetime.strptime(start, "%Y-%m-%d")
            date_str = dt.strftime("%d.%m (%a) — Весь день")

        text += f"▫️ <b>{date_str}</b>: {summary}\n"

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        text=text, 
        reply_markup=back_to_categories_keyboard_calendar
    )
    await callback.answer()

@dp.callback_query(F.data == "next_week")
async def show_next_week(callback: CallbackQuery):
    events = CACHE["calendar_events"].get("7_14", [])

    if not events:
        await callback.answer("📅 На следующей неделе нет запланированных мероприятий!", show_alert=True)
        return

    text = "<b>📅 Мероприятия на следующую неделю:</b>\n\n"

    for event in events:
        summary = event.get('summary', 'Без названия')
        start = event['start'].get('dateTime', event['start'].get('date'))
        
        if 'T' in start:
            dt = datetime.datetime.fromisoformat(start)
            date_str = dt.strftime("%d.%m (%a) в %H:%M")
        else:
            dt = datetime.datetime.strptime(start, "%Y-%m-%d")
            date_str = dt.strftime("%d.%m (%a) — Весь день")

        text += f"▫️ <b>{date_str}</b>: {summary}\n"

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        text=text, 
        reply_markup=back_to_categories_keyboard_calendar
    )
    await callback.answer()

# =====================================================================
# ТОЧКА ВХОДА И ЗАПУСК
# =====================================================================

async def main():
    # Регистрация Middlewares
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    
    # 1. Запуск планировщика
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(update_all_caches, 'cron', hour=0, minute=0)
    scheduler.start()

    # 2. Очистка прошлых зависших запросов Telegram
    await bot.delete_webhook(drop_pending_updates=True)

    # 3. Запуск загрузки КЭШа В ФОНЕ (не блокируя бота)
    asyncio.create_task(update_all_caches())

    logging.info("🚀 Бот запущен и готов к приему команд!")
    
    # 4. Запуск приема сообщений
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as e:
        if str(e) == "asyncio.run() cannot be called from a running event loop":
            loop = asyncio.get_event_loop()
            loop.create_task(main())
        else:
            raise e