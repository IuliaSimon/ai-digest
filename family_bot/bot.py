"""Семейный Telegram-бот: карточки детей, расписание, памятки и напоминания об оплате.

Все данные о детях лежат в JSON-файле (DATA_FILE) на сервере, а не в репозитории.
Настройки — только через переменные окружения:
  BOT_TOKEN    — токен бота от @BotFather (обязательно)
  ALLOWED_IDS  — Telegram ID через запятую, кому бот отвечает и кому шлёт напоминания
  DATA_FILE    — путь к файлу с данными (по умолчанию family_bot/data.json)
  TZ_NAME      — часовой пояс (по умолчанию Europe/Samara)
  REMIND_DAYS  — числа месяца для напоминаний об оплате (по умолчанию 1,5,8,9,10)
  REMIND_HOUR  — час отправки напоминаний (по умолчанию 10)
"""

import datetime as dt
import json
import logging
import os
import shutil
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("family_bot")

DATA_FILE = Path(os.environ.get("DATA_FILE", Path(__file__).with_name("data.json")))
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Samara"))
REMIND_DAYS = {int(d) for d in os.environ.get("REMIND_DAYS", "1,5,8,9,10").split(",") if d.strip()}
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "10"))
ALLOWED_IDS = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

# Поля карточки, которые бот использует для напоминаний и оплаты.
F_FIO = "ФИО"
F_BIRTH = "Дата рождения"
F_FOOD = "Лицевой счёт (питание)"
F_PAID = "Лицевой счёт (платные услуги)"
F_PAID_SUM = "Платные услуги, ₽ в месяц"
F_FOOD_SUM = "Питание, ₽ в месяц"

PAY_FOOD = "питание"
PAY_PAID = "платные"
PAY_TITLES = {PAY_FOOD: "Питание", PAY_PAID: "Платные услуги"}

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь",
          "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

EMPTY_DATA = {"дети": {}, "звонки": {"будни": [], "суббота": []}, "памятки": {}, "оплачено": {}}

HELP = (
    "Что я умею:\n"
    "• Нажмите имя ребёнка, чтобы увидеть его карточку.\n"
    "• «Оплата» — что оплачено в этом месяце, счета и суммы.\n"
    "• «Сейчас» — какой урок идёт у детей прямо сейчас и какой следующий.\n"
    "• «Расписание» — уроки на сегодня, «расписание завтра» — на завтра, «Неделя» — вся неделя.\n"
    "• «Звонки», «Памятка питание», «Памятка платные».\n\n"
    "Изменить или добавить поле:\n"
    "  изменить кирилл Дата рождения = 12.03.2015\n"
    "Удалить поле:\n"
    "  удалить кирилл Дата рождения\n"
    "Добавить ребёнка:\n"
    "  добавить ребёнка Маша\n\n"
    "Резервная копия: «выгрузить» — пришлю файл с данными. "
    "Чтобы загрузить данные, просто отправьте мне этот .json-файл."
)


# ---------- хранение ----------

def load_data() -> dict:
    if not DATA_FILE.exists():
        return json.loads(json.dumps(EMPTY_DATA))
    with DATA_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    for key, value in EMPTY_DATA.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    return data


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


def validate_data(data) -> str | None:
    """Возвращает текст ошибки или None, если структура подходит."""
    if not isinstance(data, dict):
        return "в файле должен быть JSON-объект"
    children = data.get("дети")
    if not isinstance(children, dict):
        return "нет раздела «дети»"
    for key, child in children.items():
        if not isinstance(child, dict) or not isinstance(child.get("поля", {}), dict):
            return f"у ребёнка «{key}» неправильная карточка"
    return None


# ---------- поиск ----------

def norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def find_child(data: dict, name: str) -> str | None:
    name = norm(name)
    for key, child in data["дети"].items():
        variants = {norm(key), norm(child.get("имя", ""))}
        if name in variants:
            return key
    return None


def find_field(fields: dict, name: str) -> str | None:
    for key in fields:
        if norm(key) == norm(name):
            return key
    return None


# ---------- тексты ----------

def parse_date(value: str) -> dt.date | None:
    try:
        return dt.datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except (ValueError, AttributeError):
        return None


def age_on(birth: dt.date, day: dt.date) -> int:
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def next_birthday(birth: dt.date, today: dt.date) -> dt.date:
    for year in (today.year, today.year + 1):
        try:
            candidate = birth.replace(year=year)
        except ValueError:  # 29 февраля в невисокосный год
            candidate = dt.date(year, 3, 1)
        if candidate >= today:
            return candidate
    raise AssertionError("unreachable")


def child_card(data: dict, key: str, today: dt.date) -> str:
    child = data["дети"][key]
    lines = [f"👤 {child.get('имя', key)}"]
    for field, value in child.get("поля", {}).items():
        lines.append(f"{field}: {value or '—'}")
    birth = parse_date(child.get("поля", {}).get(F_BIRTH, ""))
    if birth:
        nb = next_birthday(birth, today)
        days = (nb - today).days
        when = "сегодня! 🎉" if days == 0 else f"через {days} дн. ({nb:%d.%m})"
        lines.append(f"\nСейчас {age_on(birth, today)} лет, день рождения {when}")
    if child.get("расписание"):
        lines.append("\nРасписание: кнопка «Расписание»")
    return "\n".join(lines)


def paid_this_month(data: dict, today: dt.date) -> set:
    return set(data["оплачено"].get(today.strftime("%Y-%m"), []))


def payment_text(data: dict, today: dt.date, only_unpaid: bool = False) -> str:
    done = paid_this_month(data, today)
    month = MONTHS[today.month - 1]
    lines = [f"💳 Оплата за {month} {today.year} (срок — до 10 числа)"]
    for kind, account_field, sum_field in (
        (PAY_FOOD, F_FOOD, F_FOOD_SUM),
        (PAY_PAID, F_PAID, F_PAID_SUM),
    ):
        if only_unpaid and kind in done:
            continue
        mark = "✅ оплачено" if kind in done else "❗ не оплачено"
        lines.append(f"\n{PAY_TITLES[kind]}: {mark}")
        for key, child in data["дети"].items():
            fields = child.get("поля", {})
            account = fields.get(account_field)
            if not account:
                continue
            amount = fields.get(sum_field)
            amount_text = f", {amount} ₽" if amount else ""
            lines.append(f"• {child.get('имя', key)}: л/с {account}{amount_text}")
            if kind == PAY_PAID:
                fio = fields.get(F_FIO, child.get("имя", key)).split()
                short = " ".join(fio[:2])
                lines.append(
                    f"  назначение: Оплата за дополнительные образовательные услуги {short}, {month}"
                )
    lines.append("\nКак платить — кнопки «Памятка питание» и «Памятка платные».")
    return "\n".join(lines)


def payment_buttons(data: dict, today: dt.date) -> InlineKeyboardMarkup:
    done = paid_this_month(data, today)
    row = []
    for kind in (PAY_FOOD, PAY_PAID):
        if kind in done:
            row.append(InlineKeyboardButton(f"↩️ {PAY_TITLES[kind]}: не оплачено", callback_data=f"unpay:{kind}"))
        else:
            row.append(InlineKeyboardButton(f"✅ {PAY_TITLES[kind]} оплачено", callback_data=f"pay:{kind}"))
    return InlineKeyboardMarkup([[b] for b in row])


def schedule_text(data: dict, day: dt.date, label: str) -> str:
    weekday = day.weekday()
    bells = data["звонки"].get("суббота" if weekday == 5 else "будни", [])
    parts = []
    for key, child in data["дети"].items():
        lessons = child.get("расписание", {})
        if not lessons:
            continue
        today_lessons = lessons.get(WEEKDAYS[weekday], [])
        header = f"📚 {child.get('имя', key)} — {label}, {WEEKDAY_NAMES[weekday].lower()}"
        if not today_lessons:
            parts.append(f"{header}\nУроков нет 🎈")
            continue
        rows = [header]
        for i, lesson in enumerate(today_lessons):
            if not lesson:
                continue
            time = f" ({bells[i]})" if i < len(bells) else ""
            rows.append(f"{i + 1}. {lesson}{time}")
        parts.append("\n".join(rows))
    if not parts:
        return "Расписание пока не заполнено."
    return "\n\n".join(parts)


def parse_bell(bell: str) -> tuple[dt.time, dt.time] | None:
    try:
        start, end = bell.replace("—", "–").replace("-", "–").split("–")
        return (dt.datetime.strptime(start.strip().replace(".", ":"), "%H:%M").time(),
                dt.datetime.strptime(end.strip().replace(".", ":"), "%H:%M").time())
    except ValueError:
        return None


def now_text(data: dict, moment: dt.datetime) -> str:
    """Какой урок идёт у каждого ребёнка прямо сейчас."""
    weekday = moment.weekday()
    now = moment.time()
    bells = data["звонки"].get("суббота" if weekday == 5 else "будни", [])
    parts = []
    for key, child in data["дети"].items():
        name = child.get("имя", key)
        schedule = child.get("расписание")
        if not schedule:
            parts.append(f"👤 {name}: расписание не заполнено")
            continue
        lessons = [(i, lesson, parse_bell(bells[i]))
                   for i, lesson in enumerate(schedule.get(WEEKDAYS[weekday], []))
                   if lesson and i < len(bells) and parse_bell(bells[i])]
        if not lessons:
            parts.append(f"👤 {name}: сегодня уроков нет 🎈")
            continue
        first_start = lessons[0][2][0]
        last_end = lessons[-1][2][1]
        if now < first_start:
            i, lesson, (start, _) = lessons[0]
            parts.append(f"👤 {name}: уроки ещё не начались\nПервый — {i + 1}. {lesson} в {start:%H:%M}")
        elif now >= last_end:
            parts.append(f"👤 {name}: уроки закончились в {last_end:%H:%M} 🏠")
        else:
            for n, (i, lesson, (start, end)) in enumerate(lessons):
                nxt = lessons[n + 1] if n + 1 < len(lessons) else None
                if start <= now < end:
                    line = f"👤 {name}: сейчас {i + 1} урок — {lesson}, до {end:%H:%M}"
                    if nxt:
                        line += f"\nДальше: {nxt[0] + 1}. {nxt[1]} в {nxt[2][0]:%H:%M}"
                    else:
                        line += "\nЭто последний урок"
                    parts.append(line)
                    break
                if nxt and end <= now < nxt[2][0]:
                    parts.append(f"👤 {name}: перемена\nДальше: {nxt[0] + 1}. {nxt[1]} в {nxt[2][0]:%H:%M}")
                    break
            parts[-1] += f"\nУроки до {last_end:%H:%M}"
    return f"🕐 {moment:%H:%M}, {WEEKDAY_NAMES[weekday].lower()}\n\n" + "\n\n".join(parts)


def week_text(data: dict) -> str:
    parts = []
    for key, child in data["дети"].items():
        schedule = child.get("расписание")
        if not schedule:
            continue
        rows = [f"📚 {child.get('имя', key)} — вся неделя"]
        for w, short in enumerate(WEEKDAYS[:6]):
            lessons = schedule.get(short, [])
            rows.append(f"\n{WEEKDAY_NAMES[w]}:")
            rows += [f"{i + 1}. {lesson}" for i, lesson in enumerate(lessons) if lesson] or ["уроков нет"]
        parts.append("\n".join(rows))
    return "\n\n".join(parts) or "Расписание пока не заполнено."


def bells_text(data: dict) -> str:
    lines = ["🔔 Звонки (пн–пт):"]
    lines += [f"{i}. {t}" for i, t in enumerate(data["звонки"].get("будни", []), 1)]
    lines.append("\n🔔 Звонки (суббота):")
    lines += [f"{i}. {t}" for i, t in enumerate(data["звонки"].get("суббота", []), 1)]
    return "\n".join(lines)


def birthday_reminders(data: dict, today: dt.date) -> list[str]:
    result = []
    for key, child in data["дети"].items():
        birth = parse_date(child.get("поля", {}).get(F_BIRTH, ""))
        if not birth:
            continue
        nb = next_birthday(birth, today)
        days = (nb - today).days
        name = child.get("имя", key)
        if days == 0:
            result.append(f"🎉 Сегодня день рождения: {name}, {age_on(birth, today)} лет!")
        elif days == 7:
            result.append(f"🎁 Через неделю ({nb:%d.%m}) день рождения: {name}, исполнится {age_on(birth, nb)}.")
    return result


# ---------- доступ и клавиатура ----------

def keyboard(data: dict) -> ReplyKeyboardMarkup:
    names = [child.get("имя", key) for key, child in data["дети"].items()]
    rows = [names] if names else []
    rows += [["Сейчас", "Расписание", "Неделя"], ["Оплата", "Звонки", "Помощь"],
             ["Памятка питание", "Памятка платные"]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


async def allowed(update: Update) -> bool:
    user = update.effective_user
    if user and user.id in ALLOWED_IDS:
        return True
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Нет доступа. Ваш Telegram ID: {user.id if user else '?'}\n"
            "Его нужно добавить в настройку ALLOWED_IDS."
        )
    return False


def today() -> dt.date:
    return dt.datetime.now(TZ).date()


# ---------- обработчики ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    await update.message.reply_text("Привет! " + HELP, reply_markup=keyboard(load_data()))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    text = update.message.text.strip()
    low = norm(text)
    data = load_data()
    day = today()
    reply = update.message.reply_text

    if low in ("помощь", "help"):
        await reply(HELP, reply_markup=keyboard(data))
    elif low == "оплата":
        await reply(payment_text(data, day), reply_markup=payment_buttons(data, day))
    elif low in ("расписание", "расписание сегодня"):
        await reply(schedule_text(data, day, "сегодня"))
    elif low == "расписание завтра":
        await reply(schedule_text(data, day + dt.timedelta(days=1), "завтра"))
    elif low == "сейчас":
        await reply(now_text(data, dt.datetime.now(TZ)))
    elif low == "неделя":
        await reply(week_text(data))
    elif low == "звонки":
        await reply(bells_text(data))
    elif low in ("памятка питание", "памятка платные"):
        kind = PAY_FOOD if low.endswith("питание") else PAY_PAID
        await reply(data["памятки"].get(kind) or "Памятка пока не заполнена.",
                    disable_web_page_preview=True)
    elif low == "выгрузить":
        if not DATA_FILE.exists():
            await reply("Данных пока нет.")
        else:
            with DATA_FILE.open("rb") as f:
                await update.message.reply_document(f, filename="data.json",
                                                    caption=f"Резервная копия на {day:%d.%m.%Y}")
    elif low.startswith("изменить "):
        await reply(edit_field(data, text[len("изменить "):]), reply_markup=keyboard(data))
    elif low.startswith("удалить "):
        await reply(delete_field(data, text[len("удалить "):]))
    elif low.startswith(("добавить ребенка ", "добавить ребёнка ")):
        name = text.split(maxsplit=2)[2].strip()
        if find_child(data, name):
            await reply("Такой ребёнок уже есть.")
        else:
            data["дети"][norm(name)] = {"имя": name, "поля": {}}
            save_data(data)
            await reply(f"Добавил: {name}", reply_markup=keyboard(data))
    elif key := find_child(data, text):
        await reply(child_card(data, key, day))
    else:
        await reply("Не понял 🙂 Нажмите «Помощь».", reply_markup=keyboard(data))


def edit_field(data: dict, rest: str) -> str:
    """rest: «кирилл Дата рождения = 12.03.2015»."""
    if "=" not in rest:
        return "Формат: изменить кирилл Название поля = значение"
    left, value = rest.split("=", 1)
    parts = left.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Формат: изменить кирилл Название поля = значение"
    key = find_child(data, parts[0])
    if not key:
        return f"Не нашёл ребёнка «{parts[0]}»."
    fields = data["дети"][key].setdefault("поля", {})
    field = find_field(fields, parts[1]) or parts[1].strip()
    value = value.strip()
    if norm(field) == norm(F_BIRTH) and not parse_date(value):
        return "Дату рождения напишите так: 12.03.2015"
    old = fields.get(field)
    fields[field] = value
    save_data(data)
    name = data["дети"][key].get("имя", key)
    if old is None:
        return f"Добавил {name}: {field} = {value}"
    return f"Изменил {name}: {field}\nбыло: {old or '—'}\nстало: {value}"


def delete_field(data: dict, rest: str) -> str:
    parts = rest.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Формат: удалить кирилл Название поля"
    key = find_child(data, parts[0])
    if not key:
        return f"Не нашёл ребёнка «{parts[0]}»."
    fields = data["дети"][key].get("поля", {})
    field = find_field(fields, parts[1])
    if not field:
        return f"Нет поля «{parts[1]}»."
    old = fields.pop(field)
    save_data(data)
    return f"Удалил {field} (было: {old or '—'})"


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await allowed(update):
        return
    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".json"):
        await update.message.reply_text("Я принимаю только файл данных .json")
        return
    file = await doc.get_file()
    raw = await file.download_as_bytearray()
    try:
        data = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        await update.message.reply_text("Файл не читается как JSON, данные не менял.")
        return
    error = validate_data(data)
    if error:
        await update.message.reply_text(f"Файл не подошёл: {error}. Данные не менял.")
        return
    if DATA_FILE.exists():
        shutil.copy(DATA_FILE, DATA_FILE.with_suffix(".bak.json"))
    for key, value in EMPTY_DATA.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    save_data(data)
    names = ", ".join(c.get("имя", k) for k, c in data["дети"].items()) or "нет"
    await update.message.reply_text(f"Данные загружены. Дети: {names}", reply_markup=keyboard(data))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not update.effective_user or update.effective_user.id not in ALLOWED_IDS:
        await query.answer("Нет доступа")
        return
    action, kind = query.data.split(":", 1)
    data = load_data()
    day = today()
    month = day.strftime("%Y-%m")
    done = set(data["оплачено"].get(month, []))
    if action == "pay":
        done.add(kind)
    else:
        done.discard(kind)
    data["оплачено"][month] = sorted(done)
    save_data(data)
    await query.answer("Отметил")
    await query.edit_message_text(payment_text(data, day), reply_markup=payment_buttons(data, day))


# ---------- ежедневная проверка ----------

async def daily_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    day = today()
    messages = birthday_reminders(data, day)
    need_pay = day.day in REMIND_DAYS and {PAY_FOOD, PAY_PAID} - paid_this_month(data, day)
    for chat_id in ALLOWED_IDS:
        try:
            for text in messages:
                await context.bot.send_message(chat_id, text)
            if need_pay:
                await context.bot.send_message(
                    chat_id,
                    "⏰ Напоминание!\n\n" + payment_text(data, day, only_unpaid=True),
                    reply_markup=payment_buttons(data, day),
                )
        except Exception:  # один недоступный чат не должен ломать остальные
            log.exception("Не удалось отправить напоминание в %s", chat_id)


def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN")
    if not ALLOWED_IDS:
        log.warning("ALLOWED_IDS пуст: бот будет только сообщать людям их Telegram ID")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(pay|unpay):"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_daily(daily_check, time=dt.time(REMIND_HOUR, 0, tzinfo=TZ))
    log.info("Бот запущен, данные: %s", DATA_FILE)
    app.run_polling()


if __name__ == "__main__":
    main()
