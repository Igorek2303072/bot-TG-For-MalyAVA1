import os
import logging
import random
import re
import requests
from datetime import datetime, timedelta
from time import mktime
from bs4 import BeautifulSoup
import feedparser
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from openai import OpenAI

# ==========================================
# ⚙️ НАСТРОЙКИ
# ==========================================
# Загружаем переменные из .env файла
load_dotenv()

# Получаем ключи из окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# Добавьте сюда ID администраторов
ADMIN_IDS = [8563698005, 1817790935]
CHANNEL_ID = -1002105198602

# RSS‑ленты источников
RSS_SOURCES = [
    "https://escorenews.com/ru/csgo/rss",
    "https://www.cybersport.ru/rss/tags/cs2",
    "https://www.hltv.org/rss/news",
    "https://esports.ru/feed",
]

PROMPT_TEMPLATE = """Ты — автор новостного канала по Counter-Strike 2 (CS2). На основе новости ниже напиши пост в стиле, как в примерах.
Исходная новость:
Заголовок: {title}
Содержание: {description}
Правила:
- Не врать, опираться строго на исходную новость.
- Пост должен содержать заголовок и 1-3 абзаца.
- Стиль живой, но без кликбейта.
- Объём 300-700 знаков.
- Не писать «Вот пост», «Готово» и т.п. Только сам пост.
- Никаких ссылок на источник.

Примеры:
Из Team Falcons ушёл психолог 💊😭💊
Организация рассталась с психологом Ларсом Роблом.💔💔💔
Робл сообщил, что стороны пришли к соглашению о прекращении отношений. В будущем он планирует выступать только в качестве советника руководства клуба.

Напиши пост сейчас."""

WAITING_MANUAL_REPLY = 1

# Инициализация клиента DeepSeek и логирования
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://open.blackroute.space/v1")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ==========================================
# 📡 СБОР НОВОСТЕЙ (Только свежие, до 3 дней)
# ==========================================
def fetch_latest_news():
    """Возвращает список новостей не старше 3 дней."""
    entries = []
    # Вычисляем дату, которая была 3 дня назад
    three_days_ago = datetime.now() - timedelta(days=3)
    
    for rss_url in RSS_SOURCES:
        try:
            feed = feedparser.parse(rss_url)
            # Берем первые 15 записей
            for e in feed.entries[:15]:
                
                # ПРОВЕРКА ДАТЫ:
                date_parsed = e.get("published_parsed") or e.get("updated_parsed")
                if date_parsed:
                    dt = datetime.fromtimestamp(mktime(date_parsed))
                    if dt < three_days_ago:
                        continue  # Пропускаем, если новость старше 3 дней
                
                title = e.get("title", "").strip()
                link = e.get("link", "")
                desc = e.get("summary", e.get("description", ""))
                clean_desc = BeautifulSoup(desc, "html.parser").get_text() if desc else ""
                
                img_url = None
                if "enclosures" in e and e.enclosures:
                    img_url = e.enclosures[0].get("href")
                if not img_url and "media_content" in e:
                    img_url = e.media_content[0]["url"]
                if not img_url and desc:
                    soup = BeautifulSoup(desc, "html.parser")
                    img_tag = soup.find("img")
                    if img_tag and img_tag.get("src"):
                        img_url = img_tag["src"]
                        
                entries.append({
                    "title": title,
                    "link": link,
                    "description": clean_desc[:300] if clean_desc else title,
                    "image_url": img_url,
                })
        except Exception as e:
            logging.warning(f"Ошибка RSS {rss_url}: {e}")
    return entries

def get_image_from_page(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        og = soup.find("meta", property="og:image")
        if og and og.get("content"): return og["content"]

        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"): return tw["content"]

        img = soup.find("img")
        if img: return img.get("src")
    except Exception as e:
        logging.error(f"Ошибка парсинга картинки: {e}")
    return None

# ==========================================
# 🧠 ГЕНЕРАЦИЯ ПОСТА (Обязательно с картинкой)
# ==========================================
async def generate_post_content():
    """Собирает новости, выбирает одну С КАРТИНКОЙ, генерирует пост."""
    news_list = fetch_latest_news()
    if not news_list:
        return None, None, "Нет свежих новостей за последние 3 дня."

    valid_news = [n for n in news_list if n["description"]]
    if not valid_news:
        valid_news = news_list

    # Перемешиваем список, чтобы бот выбирал новости случайно
    random.shuffle(valid_news)
    
    chosen = None
    # Ищем новость, у которой ТОЧНО есть картинка
    for news in valid_news:
        # Пытаемся взять картинку из RSS или спарсить с сайта
        img_url = news.get("image_url") or get_image_from_page(news["link"])
        
        if img_url:
            news["image_url"] = img_url
            chosen = news
            break  # Останавливаем поиск, новость с картинкой найдена

    if not chosen:
        return None, None, "Свежие новости найдены, но ни у одной не удалось найти картинку."

    prompt = PROMPT_TEMPLATE.format(title=chosen["title"], description=chosen["description"])
    
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": "Ты редактор Telegram. Запрещено: писать 'Вот пост', 'Готово', писать комментарии, объяснять свои действия, использовать Markdown. Ответ должен содержать ТОЛЬКО текст поста."
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.8,
        )
        post_text = response.choices[0].message.content.strip()

        # Очистка от мусора
        patterns = [
            r"^Вот\s+готовый\s+пост[:：]?\s*", r"^Готово[:：]?\s*",
            r"^Конечно[:：]?\s*", r"^Вот\s+вариант[:：]?\s*", r"^Ответ[:：]?\s*",
        ]
        for p in patterns:
            post_text = re.sub(p, "", post_text, flags=re.IGNORECASE)
            
        return post_text.strip(), chosen["image_url"], None
        
    except Exception as e:
        return None, None, f"Ошибка DeepSeek: {e}"

# ==========================================
# 🎛 ИНТЕРФЕЙС И КНОПКИ
# ==========================================
async def send_approval_menu(message, context, text, image_url=None):
    """Отправляет готовый пост с кнопками управления"""
    keyboard = [
        [InlineKeyboardButton("✅ Опубликовать в канал", callback_data="approve")],
        [InlineKeyboardButton("🔄 Сгенерировать другой", callback_data="cancel_post")]
    ]
    markup = InlineKeyboardMarkup(keyboard)
    
    context.user_data["pending_post"] = text
    context.user_data["pending_image_url"] = image_url

    if image_url:
        await message.reply_photo(photo=image_url, caption=f"📝 <b>Предпросмотр:</b>\n\n{text}", parse_mode="HTML", reply_markup=markup)
    else:
        # Это запасной вариант, если вдруг картинка "сломалась" при отправке
        await message.reply_text(f"📝 <b>Предпросмотр:</b>\n\n{text}", parse_mode="HTML", reply_markup=markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        return await query.answer("⛔ У вас нет доступа.", show_alert=True)
    
    await query.answer()

    text = context.user_data.get("pending_post", "")
    img_url = context.user_data.get("pending_image_url")

    if query.data == "generate_new_post":
        await query.message.reply_text("📡 Ищу свежие новости и пишу пост...")
        text, img_url, err = await generate_post_content()
        if err:
            return await query.message.reply_text(f"❌ Ошибка: {err}")
        await send_approval_menu(query.message, context, text, img_url)

    elif query.data == "approve":
        try:
            if img_url:
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=img_url, caption=text)
                await query.edit_message_caption(caption=f"✅ <b>Пост опубликован в канал!</b>\n\n{text}", parse_mode="HTML", reply_markup=None)
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
                await query.edit_message_text(f"✅ <b>Пост опубликован в канал!</b>\n\n{text}", parse_mode="HTML", reply_markup=None)
        except Exception as e:
            await query.message.reply_text(f"❌ Ошибка отправки в канал: {e}")
            
    elif query.data == "cancel_post":
        if img_url:
            await query.edit_message_caption(caption=f"<i>Отменено. Генерирую новый вариант...</i>", parse_mode="HTML", reply_markup=None)
        else:
            await query.edit_message_text(text=f"<i>Отменено. Генерирую новый вариант...</i>", parse_mode="HTML", reply_markup=None)
        
        text, img_url, err = await generate_post_content()
        if err:
            return await query.message.reply_text(f"❌ Ошибка: {err}")
        await send_approval_menu(query.message, context, text, img_url)

# ==========================================
# 🤖 БАЗОВЫЕ КОМАНДЫ И РУЧНОЙ РЕЖИМ
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("⛔ Доступ запрещён.")
        
    keyboard = [
        [InlineKeyboardButton("📝 Создать авто-пост", callback_data="generate_new_post")]
    ]
    await update.message.reply_text(
        "🤖 <b>Панель управления CS2 News Bot</b>\n\n"
        "Нажми на кнопку ниже, чтобы бот нашел новость и написал пост, "
        "или используй /manual для ручной публикации.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def auto_generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    msg = await update.message.reply_text("📡 Собираю свежие новости...")
    text, img_url, err = await generate_post_content()
    await msg.delete()
    
    if err:
        return await update.message.reply_text(f"❌ Ошибка: {err}")
    await send_approval_menu(update.message, context, text, img_url)

async def manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text("📝 <b>Ручной режим:</b> отправь текст поста с прикрепленной картинкой.\nДля отмены введи /cancel", parse_mode="HTML")
    return WAITING_MANUAL_REPLY

async def manual_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.copy(chat_id=CHANNEL_ID)
    await update.message.reply_text("✅ Успешно отправлено в канал.")
    return ConversationHandler.END

async def cancel_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("❌ Ручной режим отменён.")
    return ConversationHandler.END

# ==========================================
# 🚀 ЗАПУСК БОТА
# ==========================================
def main():
    if not BOT_TOKEN or not DEEPSEEK_API_KEY:
        print("❌ ОШИБКА: Не найдены ключи в файле .env. Убедитесь, что файл существует и ключи прописаны.")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    
    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("prompt", auto_generate_command))
    
    # Обработчик кнопок
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # Ручной режим
    manual_conv = ConversationHandler(
        entry_points=[CommandHandler("manual", manual_start)],
        states={
            WAITING_MANUAL_REPLY: [MessageHandler(filters.TEXT | filters.PHOTO | filters.VIDEO & ~filters.COMMAND, manual_forward)],
        },
        fallbacks=[CommandHandler("cancel", cancel_manual)],
    )
    app.add_handler(manual_conv)
    
    print("✅ Бот успешно запущен и готов к работе!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
