import os
import re
import asyncio
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters, CommandHandler
import openai
import logging

# --- Загрузка конфигурации ---
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai.api_key = OPENAI_API_KEY

# --- Логи ---
logging.basicConfig(level=logging.INFO)

# --- Разрешённые чаты и треды ---
ALLOWED_CONTEXTS = {
    -1002079167705: [7340],
    -1002387655137: [9],
    -1002423500927: [4],
    -1002178818697: [4],
}

# --- Таблица цен ---
PRICE_MAP = {
    r"\+|^-": 10,
    r"мк(\.|$|\s)|МК|Мк|мк": 15,
    r"мк доп синяя": 23,
    r"мк доп красная": 31,
    r"мк доп оранжевая": 40,
    r"мк доп салатовая": 48,
    r"мк доп коричневая": 57,
    r"мк доп светло-серая": 65,
    r"мк доп розовая": 74,
    r"мк доп темно-серая": 82,
    r"мк доп голубая": 91
}

# --- Хранилище доходов: user_id -> date -> total ---
user_income_storage = defaultdict(lambda: defaultdict(int))

def add_income(user_id: int, amount: int) -> int:
    today = datetime.now().date()
    user_income_storage[user_id][today] += amount
    return user_income_storage[user_id][today]

def get_income_today(user_id: int) -> int:
    today = datetime.now().date()
    return user_income_storage[user_id].get(today, 0)

def extract_amount(text: str) -> int:
    text = text.lower()
    for pattern, amount in sorted(PRICE_MAP.items(), key=lambda x: -x[1]):
        if re.search(pattern, text):
            return amount
    return 0

# --- Проверка номера ---
def is_valid_phone(phone: str) -> bool:
    phone = re.sub(r'\D', '', phone)
    return phone.startswith(("375", "8029", "8044", "8033", "8025")) and len(phone) >= 9

# --- Prompt для ИИ ---
PROMPT_TEMPLATE = """
Ты помощник службы доставки. Из текста заявки извлеки:

1. Временной интервал доставки (или фразу: "в ближайшее время", "как можно скорее")
2. Адрес с номером дома
3. Номер телефона
4. Комментарий заказчика (если есть)

Формат ответа:
[временной интервал]
[адрес]
[номер телефона]
Комментарий заказчика: [если есть]

Вот заявка:
{text}
"""

def extract_fields(text: str) -> dict:
    lines = text.strip().split('\n')
    fields = {"interval": "", "address": "", "phone": "", "comment": ""}
    for line in lines:
        if re.search(r'\d{1,2}:\d{2}', line) or 'ближайшее' in line.lower() or 'как можно скорее' in line.lower():
            fields["interval"] = line.strip()
        elif 'Комментарий заказчика:' in line:
            fields["comment"] = line.split('Комментарий заказчика:')[-1].strip()
        elif re.search(r'\+?\d{7,}', line):
            fields["phone"] = re.sub(r'[^\d+]', '', line.strip())
        else:
            fields["address"] = line.strip()
    return fields

# --- Обработка сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    chat_id = message.chat_id
    thread_id = message.message_thread_id or 0

    if chat_id not in ALLOWED_CONTEXTS or thread_id not in ALLOWED_CONTEXTS[chat_id]:
        return

    user_text = message.text

    if re.search(r"(^|\s)[\+\-]", user_text):
        amount = extract_amount(user_text)
        if amount:
            user_id = message.from_user.id
            total_today = add_income(user_id, amount)
            try:
                await context.bot.send_message(chat_id=user_id, text=f"+{amount} BYN.\nДоход: {total_today} BYN")
            except:
                pass  # пользователь не начал чат с ботом
            return

    prompt = PROMPT_TEMPLATE.replace("{text}", user_text)

    try:
        response = openai.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        parsed = response.choices[0].message.content.strip()
        data = extract_fields(parsed)

        # Проверки
        missing = []
        if not data["address"] or not re.search(r'\d+', data["address"]):
            missing.append("адрес с номером дома")
        if not data["interval"]:
            missing.append("временной интервал")
        if not data["phone"]:
            missing.append("номер телефона")
        elif not is_valid_phone(data["phone"]):
            await message.reply_text(
                "🚫 Заказ не принят в работу! Причина: номер получателя не соответствует региону осуществления деятельности службы доставки. "
                "Пожалуйста, пришлите корректный номер телефона в формате +375XXXXXXXXX или своими силами осуществите связь водителя с получателем."
            )
            return

        if missing:
            await message.reply_text(
                f"🚫 Заказ не принят в работу! Причина: Не хватает данных для осуществления доставки. "
                f"Пожалуйста, уточните данные и пришлите заявку повторно.\n\nНе хватает: {', '.join(missing)}"
            )
            return

        # Если всё хорошо
        formatted = f"""{data['interval']}
{data['address']}
{data['phone']}
Комментарий заказчика: {data['comment'] or '—'}"""
        await message.reply_text(f"✅ Заказ принят в работу:\n{formatted}")

    except Exception as e:
        logging.error(e)
        await message.reply_text("⚠️ Ошибка при обработке заявки. Попробуйте ещё раз.")

# --- Команда /доход ---
async def handle_dohod(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    total = get_income_today(user_id)
    await update.message.reply_text(f"Ваш доход за сегодня: {total} BYN")

# --- Запуск бота ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("доход", handle_dohod))
    print("🚀 Бот запущен...")
    app.run_polling()
