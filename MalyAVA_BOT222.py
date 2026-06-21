import logging
import random
import re
import requests
from bs4 import BeautifulSoup
import feedparser
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

# ⚙️ НАСТРОЙКИ
BOT_TOKEN = "8834716334:AAFcBGeNVnCfV7UmDP7sILG5F8lOgPVUGv8"
# Добавьте сюда ID других администраторов через запятую, например: [8563698005, 123456789]
ADMIN_IDS = [8563698005, 1817790935]
CHANNEL_ID = -1002105198602
DEEPSEEK_API_KEY = "sk-Fz7JdiceYTWiEtQEcyeVZA"

# RSS‑ленты источников
RSS_SOURCES = [
    "https://escorenews.com/ru/csgo/rss",
    "https://www.cybersport.ru/rss/tags/cs2",
    "https://www.hltv.org/rss/news",
    "https://esports.ru/feed",
]

PROMPT_TEMPLATE = """Ты — автор новостного канала по Counter-Strike 2 (CS2). На основе новости ниже напиши пост в стиле, как в примерах.

**Исходная новость:**
Заголовок: {title}
Содержание: {description}

**Правила:**
- Не врать, опираться строго на исходную новость.
- Пост должен содержать заголовок и 1-3 абзаца.
- Стиль живой, но без кликбейта.
- Объём 300-700 знаков.
- Не писать «Вот пост», «Готово» и т.п. Только сам пост.
- Никаких ссылок на источник.

Примеры:
---
Из Team Falcons ушёл психолог
💊😭💊
Организация рассталась с психологом Ларсом Роблом.💔💔💔
Робл сообщил, что стороны пришли к соглашению о прекращении отношений. В будущем он планирует выступать только в качестве советника руководства клуба и не намерен активно участвовать в работе с командами и игроками. Он также признался, что не поддерживает контакт со всеми составами Falcons с конца 2025 года.
---
Датамайнер: в CS2 появится анимация осмотра оружия от третьего лица
Как сообщил в социальных сетях Максим Полетаев, известный как Gabe Follower, в CS2 может появиться возможность осматривать оружие от третьего лица.
Судя по всему, разработчики Counter-Strike 2 добавят анимации осмотра оружия от третьего лица в будущем обновлении AnimGraph2.
---
Напиши пост сейчас."""

WAITING_MANUAL_REPLY = 1

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://open.blackroute.space/v1")
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # Уменьшаем спам от Telegram-библиотеки


# ------------------------------------------------------------
#  Сбор новостей из RSS
def fetch_latest_news():
    """Возвращает список словарей: title, link, description, image_url"""
    entries = []
    for rss_url in RSS_SOURCES:
        try:
            feed = feedparser.parse(rss_url)
            for e in feed.entries[:5]:  # берём 5 самых свежих из каждого источника
                title = e.get("title", "").strip()
                link = e.get("link", "")
                desc = e.get("summary", e.get("description", ""))
                clean_desc = BeautifulSoup(desc, "lxml").get_text() if desc else ""
                img_url = None
                if "enclosures" in e and e.enclosures:
                    img_url = e.enclosures[0].get("href")
                if not img_url and "media_content" in e:
                    img_url = e.media_content[0]["url"]
                if not img_url and desc:
                    soup = BeautifulSoup(desc, "lxml")
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
            logging.warning(f"RSS {rss_url} failed: {e}")
    return entries


def get_image_from_page(url):
    try:
        headers = {
            "User-Agent": "Mozilla/5.0"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # og:image
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]

        # twitter:image
        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            return tw["content"]

        # первая картинка статьи
        img = soup.find("img")
        if img:
            return img.get("src")
    except Exception as e:
        logging.error(e)
    return None


# ------------------------------------------------------------
#  Выбор случайной новости и получение готового контента
async def fetch_news_and_generate(update, context):
    """Основная логика: собрать новости, выбрать одну, сгенерировать пост."""
    news_list = fetch_latest_news()
    if not news_list:
        return None, None

    valid = [n for n in news_list if n["description"]]
    if not valid:
        valid = news_list

    chosen = random.choice(valid)

    # Всегда пытаемся получить картинку со страницы, используя фолбэк
    chosen["image_url"] = get_image_from_page(chosen["link"]) or chosen["image_url"]

    # Генерируем пост через DeepSeek с усиленным system prompt
    prompt = PROMPT_TEMPLATE.format(
        title=chosen["title"],
        description=chosen["description"]
    )
    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {
                    "role": "system",
                    "content": """Ты редактор Telegram.

Запрещено:
- писать "Вот пост"
- писать "Готово"
- писать комментарии
- объяснять свои действия
- использовать Markdown

Ответ должен содержать ТОЛЬКО текст поста."""
                },
                {"role": "user", "content": prompt}
            ],
            max_tokens=800,
            temperature=0.8,
        )
        post_text = response.choices[0].message.content.strip()

        # Очистка от возможных комментариев в начале ответа
        patterns = [
            r"^Вот\s+готовый\s+пост[:：]?\s*",
            r"^Готово[:：]?\s*",
            r"^Конечно[:：]?\s*",
            r"^Вот\s+вариант[:：]?\s*",
            r"^Ответ[:：]?\s*",
        ]
        for p in patterns:
            post_text = re.sub(p, "", post_text, flags=re.IGNORECASE)
        post_text = post_text.strip()
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка DeepSeek: {e}")
        return None, None

    return post_text, chosen["image_url"]


# ------------------------------------------------------------
#  Кнопки одобрения
async def send_approval(update, context, text, image_url=None):
    keyboard = [
        [
            InlineKeyboardButton("✅ Отправить в канал", callback_data="approve"),
            InlineKeyboardButton("❌ Отменить", callback_data="cancel_post"),
        ]
    ]
    if image_url:
        await update.message.reply_photo(
            photo=image_url,
            caption=f"📝 <b>Готовый пост:</b>\n\n{text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            f"📝 <b>Готовый пост (без картинки):</b>\n\n{text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    context.user_data["pending_post"] = text
    context.user_data["pending_image_url"] = image_url


# ------------------------------------------------------------
#  Команда /prompt – теперь собирает новости
async def auto_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return await update.message.reply_text("⛔ Доступ запрещён")

    msg = await update.message.reply_text("📡 Собираю свежие новости с источников...")
    text, img_url = await fetch_news_and_generate(update, context)
    await msg.delete()

    if not text:
        await update.message.reply_text("❌ Не удалось получить новости. Попробуй позже.")
        return

    await send_approval(update, context, text, img_url)


# ------------------------------------------------------------
#  Обработка кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        return await query.answer("⛔ Доступ запрещён", show_alert=True)
    await query.answer()

    text = context.user_data.get("pending_post", "")
    img_url = context.user_data.get("pending_image_url")
    
    if query.data == "approve":
        try:
            if img_url:
                await context.bot.send_photo(chat_id=CHANNEL_ID, photo=img_url, caption=text)
                await query.edit_message_caption(caption=f"✅ Пост опубликован!\n\n{text}", parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=text)
                await query.edit_message_text(f"✅ Пост опубликован!\n\n{text}", parse_mode="HTML")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка отправки: {e}")
            
    elif query.data == "cancel_post":
        # Сообщаем об отмене и автоматически генерируем новый пост
        if img_url:
            await query.edit_message_caption(caption=f"🔄 Генерирую новый пост...", parse_mode="HTML")
        else:
            await query.edit_message_text(f"🔄 Генерирую новый пост...", parse_mode="HTML")
        
        # Генерируем новый пост
        text, img_url = await fetch_news_and_generate(update, context)
        
        if not text:
            await query.message.reply_text("❌ Не удалось получить новости. Попробуй позже.")
            return
        
        # Отправляем новый пост с кнопками
        keyboard = [
            [
                InlineKeyboardButton("✅ Отправить в канал", callback_data="approve"),
                InlineKeyboardButton("❌ Отменить", callback_data="cancel_post"),
            ]
        ]
        
        if img_url:
            await query.message.reply_photo(
                photo=img_url,
                caption=f"📝 <b>Новый пост:</b>\n\n{text}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await query.message.reply_text(
                f"📝 <b>Новый пост (без картинки):</b>\n\n{text}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        # Обновляем данные в context
        context.user_data["pending_post"] = text
        context.user_data["pending_image_url"] = img_url


# ------------------------------------------------------------
#  Ручной режим и остальное без изменений
async def manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.reply_text(f"📝 <b>Ручной режим:</b> отправь текст поста.\n(Ты должен приложить картинку сам)", parse_mode="HTML")
    return WAITING_MANUAL_REPLY

async def manual_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return ConversationHandler.END
    await update.message.copy(chat_id=CHANNEL_ID)
    await update.message.reply_text("✅ Отправлено в канал.")
    return ConversationHandler.END

async def cancel_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("❌ Ручной режим отменён.")
    return ConversationHandler.END

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>CS2 News Bot</b>\n\n"
        "/prompt — авто-пост из свежих новостей\n"
        "/manual — ручной ввод\n"
        "/cancel — выход из ручного режима",
        parse_mode="HTML"
    )


# ------------------------------------------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("prompt", auto_generate))
    app.add_handler(CallbackQueryHandler(button_handler))
    manual_conv = ConversationHandler(
        entry_points=[CommandHandler("manual", manual_start)],
        states={
            WAITING_MANUAL_REPLY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_forward),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_manual)],
    )
    app.add_handler(manual_conv)
    app.add_handler(CommandHandler("start", start))
    print("✅ Бот с RSS-новостями запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()