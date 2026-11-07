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
OBF_PRODUCT = "https://world.openbeautyfacts.org/api/v2/product/{barcode}.json"
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
    """Ищет продукты. API v2 не поддерживает одновременный поиск по тексту и категории."""
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


async def obf_get_by_barcode(session: aiohttp.ClientSession, barcode: str) -> Optional[dict]:
    """Получает продукт по штрихкоду."""
    url = OBF_PRODUCT.format(barcode=barcode)
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get("status") != 1:
                return None
            return data.get("product")
    except Exception as e:
        logging.error(f"OBF barcode error: {e}")
        return None


# ---------- ПОДБОР АНАЛОГОВ ----------
async def find_analogs(product_name: str, barcode: str = None, top_n: int = 5) -> tuple[list[dict], Optional[dict]]:
    """
    Возвращает кортеж: (список аналогов, исходный продукт).
    """
    async with aiohttp.ClientSession() as session:
        source = None
        
        # Если передан штрихкод, ищем сразу по нему
        if barcode:
            source = await obf_get_by_barcode(session, barcode)
        
        # Если не нашли по штрихкоду или его нет, ищем по названию
        if not source:
            lower_name = product_name.lower() if product_name else ""
            category = None
            
            if any(word in lower_name for word in ["тональный", "foundation", "bb", "cc"]):
                category = "en:foundations"
            elif any(word in lower_name for word in ["тушь", "mascara"]):
                category = "en:mascaras"
            elif any(word in lower_name for word in ["шампунь", "shampoo"]):
                category = "en:shampoos"
            elif any(word in lower_name for word in ["крем", "cream", "moisturizer"]):
                category = "en:face-creams"

            if category:
                candidates = await obf_search(session, category=category, limit=100)
            else:
                candidates = await obf_search(session, search_terms=product_name, limit=20)

            if not candidates:
                return [], None

            # Ищем исходный продукт
            for p in candidates:
                p_name = (p.get("product_name") or "").lower()
                p_brand = (p.get("brands") or "").lower()
                
                if category:
                    words = [w for w in lower_name.split() if len(w) > 2]
                    if any(w in p_name or w in p_brand for w in words) and p.get("ingredients_text"):
                        source = p
                        break
                else:
                    if p.get("ingredients_text"):
                        source = p
                        break

        if not source:
            return [], None

        source_inci = normalize_inci(source.get("ingredients_text", ""))
        if len(source_inci) < 3:
            return [], source # Возвращаем продукт, но без аналогов (состав слишком короткий)

        # Если мы искали по штрихкоду, нам нужно найти других кандидатов для сравнения
        if barcode:
            # Определяем категорию по названию исходного продукта для поиска аналогов
            source_name = (source.get("product_name") or "").lower()
            category = "en:face-creams" # По умолчанию
            if "foundation" in source_name or "тональный" in source_name:
                category = "en:foundations"
            elif "mascara" in source_name or "тушь" in source_name:
                category = "en:mascaras"
            elif "shampoo" in source_name or "шампунь" in source_name:
                category = "en:shampoos"
                
            candidates = await obf_search(session, category=category, limit=100)
        else:
            candidates = await obf_search(session, category=category, limit=100) if category else await obf_search(session, search_terms=product_name, limit=20)

        analogs = []
        for p in candidates:
            if p.get("code") == source.get("code"):
                continue
            other_inci = normalize_inci(p.get("ingredients_text", ""))
            if len(other_inci) < 3:
                continue
            
            score = jaccard(source_inci, other_inci)
            if score >= 0.15:  # порог 15%
                analogs.append({
                    "name": p.get("product_name") or "Без названия",
                    "brand": p.get("brands") or "—",
                    "code": p.get("code"),
                    "score": round(score * 100, 1),
                    "shared": len(source_inci & other_inci),
                    "total": len(source_inci),
                })

        analogs.sort(key=lambda x: x["score"], reverse=True)
        return analogs[:top_n], source


# ---------- ХЕНДЛЕРЫ ----------
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="❓ Помощь"))
    builder.add(KeyboardButton(text="ℹ️ О боте"))
    builder.adjust(2)
    
    inline_builder = InlineKeyboardBuilder()
    inline_builder.add(InlineKeyboardButton(text="🧴 Пример: CeraVe", callback_data="example_cerave"))
    
    await message.answer(
        "Пришлите название косметического средства (крем, шампунь, тональный крем и т.п.) "
        "или его <b>штрихкод</b> (8-14 цифр).\n"
        "Я найду его состав и подберу аналоги с похожими ингредиентами.",
        reply_markup=builder.as_markup(resize_keyboard=True),
        parse_mode="HTML"
    )
    await message.answer(
        "Или нажмите на кнопку ниже, чтобы попробовать пример:",
        reply_markup=inline_builder.as_markup()
    )


@dp.message(F.text == "❓ Помощь")
async def help_handler(message: types.Message):
    await message.answer("Напиши мне название косметики или её штрихкод, и я поищу аналоги по составу.")

@dp.message(F.text == "ℹ️ О боте")
async def about_handler(message: types.Message):
    await message.answer("Я бот, который ищет аналоги косметики по базе Open Beauty Facts.")

@dp.callback_query(F.data == "example_cerave")
async def example_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("🔍 Ищу состав для «CeraVe Moisturizing Cream»...")
    await callback.message.answer("Это пример. Напишите название своего средства текстом или пришлите штрихкод, чтобы я подобрал аналоги.")


@dp.message(F.text)
async def handle_product(message: types.Message):
    text = message.text.strip()
    
    # Проверяем, не штрихкод ли это (только цифры, длина 8-14)
    barcode = None
    if text.isdigit() and 8 <= len(text) <= 14:
        barcode = text
        name = "продукт"
        await message.answer(f"🔍 Ищу состав по штрихкоду {barcode}...")
    else:
        name = text
        if len(name) < 2:
            await message.answer("Название слишком короткое. Пришлите полное название средства.")
            return
        await message.answer(f"🔍 Ищу состав для «{name}»...")

    try:
        analogs, source = await find_analogs(product_name=name, barcode=barcode)
    except Exception as e:
        logging.exception("Ошибка подбора")
        await message.answer("⚠️ Не удалось выполнить поиск. Попробуйте позже.")
        return

    if not source:
        await message.answer(
            "❌ Не удалось найти продукт в открытой базе Open Beauty Facts.\n\n"
            "Попробуйте указать бренд + название (например, «CeraVe Moisturizing Cream») или проверьте штрихкод."
        )
        return

    # Формируем информацию об исходном продукте
    source_name = source.get("product_name") or "Без названия"
    source_brand = source.get("brands") or "—"
    source_inci_text = source.get("ingredients_text") or "Состав не указан"
    
    # Ограничиваем длину состава, чтобы не превысить лимит сообщения Telegram (4096 символов)
    if len(source_inci_text) > 1000:
        source_inci_text = source_inci_text[:1000] + "..."

    response = f"🧴 <b>Найден продукт:</b>\n"
    response += f"<b>{source_name}</b> ({source_brand})\n"
    response += f"📜 <b>Состав:</b>\n<i>{source_inci_text}</i>\n\n"

    if not analogs:
        response += (
            "❌ Аналоги не найдены. Возможные причины:\n"
            "• в базе нет других средств с похожим составом;\n"
            "• состав продукта слишком короткий для сравнения."
        )
    else:
        response += "🧪 <b>Аналоги по составу:</b>\n"
        for i, a in enumerate(analogs, 1):
            response += (
                f"{i}. <b>{a['name']}</b> ({a['brand']})\n"
                f"   Совпадение: <b>{a['score']}%</b> "
                f"({a['shared']} из {a['total']} ингредиентов)\n"
                f"   Штрихкод: <code>{a['code']}</code>\n"
            )
        response += "\n⚠️ Сравнение по Jaccard-сходству. Это ориентир, а не гарантия идентичности формулы."

    # Отправляем ответ с защитой от ошибок
    try:
        # Разбиваем сообщение, если оно слишком длинное
        if len(response) > 4000:
            for x in range(0, len(response), 4000):
                await message.answer(response[x:x+4000], parse_mode="HTML")
        else:
            await message.answer(response, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка при отправке ответа: {e}")
        await message.answer("Произошла ошибка при формировании ответа. Попробуйте другое название.")


# ---------- ЗАПУСК ----------
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())