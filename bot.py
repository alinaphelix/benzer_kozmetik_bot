# bot.py — Telegram-бот подбора аналогов косметики по составу
# Python 3.10+, aiogram 3.x

import asyncio
import logging
import os
import re
import threading
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from flask import Flask

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Задайте BOT_TOKEN в переменных окружения")

# Open Beauty Facts API
OBF_SEARCH = "https://world.openbeautyfacts.org/api/v2/search"
HEADERS = {"User-Agent": "CosmeticAnalogBot/1.0 (contact: your@email)"}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


# ---------- ПАРСИНГ INCI ----------
def normalize_inci(text: str) -> set[str]:
    """Превращает строку состава в множество нормализованных INCI-имён."""
    if not text:
        return set()
    clean = re.sub(r"\(.*?\)", "", text, flags=re.IGNORECASE)
    clean = re.sub(r"(?i)(ingredients?|состав)\s*:?", "", clean)
    parts = re.split(r"[,;\n]", clean)
    result = set()
    for p in parts:
        w = p.strip().lower()
        w = re.sub(r"\s*\d+(\.\d+)?\s*%", "", w).strip()
        if len(w) > 2 and not w.isdigit():
            result.add(w)
    return result


def jaccard(a: set[str], b: set[str]) -> float:
    """Сходство Жаккара: |A∩B| / |A∪B|."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------- OPEN BEAUTY FACTS ----------
async def obf_search(session: aiohttp.ClientSession, search_terms: str = None, category: str = None, limit: int = 50) -> list[dict]:
    """
    Ищет продукты. API v2 не поддерживает одновременный поиск по тексту и категории.
    Поэтому мы передаем либо одно, либо другое.
    """
    params = {
        "page_size": limit,
        "fields": "code,product_name,brands,ingredients_text,categories",
    }
    
    if category:
        params["categories_tags"] = category
    elif search_terms:
        params["search_terms"] = search_terms
    else:
        return []
        
    try:
        async with session.get(OBF_SEARCH, params=params, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200:
                logging.warning(f"OBF API status {resp.status}")
                return []
            data = await resp.json()
            return data.get("products", []) or []
    except Exception as e:
        logging.error(f"OBF search error: {e}")
        return []


# ---------- ПОДБОР АНАЛОГОВ ----------
async def find_analogs(product_name: str, top_n: int = 5) -> list[dict]:
    lower_name = product_name.lower()
    category = None
    
    # Определяем категорию по ключевым словам
    if any(word in lower_name for word in ["тональный", "foundation", "bb", "cc"]):
        category = "en:foundations"
    elif any(word in lower_name for word in ["тушь", "mascara"]):
        category = "en:mascaras"
    elif any(word in lower_name for word in ["шампунь", "shampoo"]):
        category = "en:shampoos"
    elif any(word in lower_name for word in ["крем", "cream", "moisturizer"]):
        category = "en:face-creams"
    
    async with aiohttp.ClientSession() as session:
        if category:
            candidates = await obf_search(session, category=category, limit=100)
        else:
            candidates = await obf_search(session, search_terms=product_name, limit=20)

        if not candidates:
            return []

        source = None
        for p in candidates:
            p_name = (p.get("product_name") or "").lower()
            p_brand = (p.get("brands") or "").lower()
            
            # Проверяем, что в названии или бренде есть слова из запроса
            if category:
                words = [w for w in lower_name.split() if len(w) > 2]
                if any(w in p_name or w in p_brand for w in words) and p.get("ingredients_text"):
                    source = p
                    break
            else:
                if p.get("ingredients_text"):
                    source = p
                    break
        
        # Если не нашли исходный продукт, возвращаем пустой список (НЕ берем шампунь!)
        if not source:
            return []

        source_inci = normalize_inci(source["ingredients_text"])
        if len(source_inci) < 3:
            return []

        analogs = []
        for p in candidates:
            if p.get("code") == source.get("code"):
                continue
            other_inci = normalize_inci(p.get("ingredients_text", ""))
            if len(other_inci) < 3:
                continue
            
            score = jaccard(source_inci, other_inci)
            if score >= 0.15:
                analogs.append({
                    "name": p.get("product_name") or "Без названия",
                    "brand": p.get("brands") or "—",
                    "code": p.get("code"),
                    "score": round(score * 100, 1),
                    "shared": len(source_inci & other_inci),
                    "total": len(source_inci),
                })

        analogs.sort(key=lambda x: x["score"], reverse=True)
        return analogs[:top_n]


# ---------- ХЕНДЛЕРЫ ----------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    # --- Reply-кнопки (под полем ввода) ---
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="❓ Помощь"))
    builder.add(KeyboardButton(text="ℹ️ О боте"))
    builder.adjust(2) # Расположить в 2 колонки
    
    # --- Inline-кнопка (под сообщением) ---
    inline_builder = InlineKeyboardBuilder()
    inline_builder.add(InlineKeyboardButton(text="🧴 Пример: CeraVe", callback_data="example_cerave"))
    
    await message.answer(
        "Пришлите название косметического средства (крем, шампунь, тональный крем и т.п.).\n"
        "Я найду его состав и подберу аналоги с похожими ингредиентами.",
        reply_markup=builder.as_markup(resize_keyboard=True),
    )
    await message.answer(
        "Или нажмите на кнопку ниже, чтобы попробовать пример:",
        reply_markup=inline_builder.as_markup()
    )


@dp.message(F.text == "❓ Помощь")
async def help_handler(message: types.Message):
    await message.answer("Просто напиши мне название косметики, и я поищу аналоги по составу.")

@dp.message(F.text == "ℹ️ О боте")
async def about_handler(message: types.Message):
    await message.answer("Я бот, который ищет аналоги косметики по базе Open Beauty Facts.")

@dp.callback_query(F.data == "example_cerave")
async def example_handler(callback: types.CallbackQuery):
    await callback.answer() # Убирает "часики" на кнопке
    await callback.message.answer("🔍 Ищу состав для «CeraVe Moisturizing Cream»...")
    # Имитация поиска для примера
    await callback.message.answer("Это пример. Напишите название своего средства текстом, чтобы я подобрал аналоги.")


@dp.message(F.text)
async def handle_product(message: types.Message):
    name = message.text.strip()
    if len(name) < 2:
        await message.answer("Название слишком короткое. Пришлите полное название средства.")
        return

    await message.answer(f"🔍 Ищу состав для «{name}»...")

    try:
        analogs = await find_analogs(name)
    except Exception as e:
        logging.exception("Ошибка подбора")
        await message.answer("⚠️ Не удалось выполнить поиск. Попробуйте позже.")
        return

    if not analogs:
        await message.answer(
            "❌ Не удалось найти аналоги. Возможные причины:\n"
            "• продукт отсутствует в открытой базе Open Beauty Facts;\n"
            "• у найденных записей нет INCI-состава;\n"
            "• состав слишком короткий для сравнения.\n\n"
            "Попробуйте указать бренд + название (например, «CeraVe Moisturizing Cream»)."
        )
        return

    lines = [f"🧪 <b>Аналоги для «{name}»</b> (по составу):\n"]
    for i, a in enumerate(analogs, 1):
        lines.append(
            f"{i}. <b>{a['name']}</b> ({a['brand']})\n"
            f"   Совпадение: <b>{a['score']}%</b> "
            f"({a['shared']} из {a['total']} ингредиентов)\n"
            f"   Штрихкод: <code>{a['code']}</code>"
        )
    lines.append("\n⚠️ Сравнение по Jaccard-сходству. Это ориентир, а не гарантия идентичности формулы.")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ---------- ЗАПУСК ----------
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())