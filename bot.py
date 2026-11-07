# bot.py — Telegram-бот подбора аналогов косметики по составу
# Python 3.10+, aiogram 3.x

import asyncio
import json
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

# --- ЗАГРУЗКА ЛОКАЛЬНОЙ БАЗЫ ---
LOCAL_DB = {}
try:
    with open('local_brands.json', 'r', encoding='utf-8') as f:
        LOCAL_DB = json.load(f)
    logging.info(f"Локальная база загружена. Брендов: {len(LOCAL_DB)}")
except FileNotFoundError:
    logging.warning("Файл local_brands.json не найден. Бот будет искать только в OBF.")
except json.JSONDecodeError as e:
    logging.error(f"Ошибка в формате local_brands.json: {e}")

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


# ---------- УМНОЕ СРАВНЕНИЕ ----------
def smart_similarity(a_text: str, b_text: str) -> float:
    """
    Вычисляет взвешенное сходство двух составов.
    Игнорирует воду/консерванты и учитывает порядок ингредиентов.
    """
    if not a_text or not b_text:
        return 0.0
        
    # Список того, что есть почти в любом средстве (не считаем за совпадение)
    IGNORE_LIST = {
        "aqua", "water", "eau", "glycerin", "glycerine", "parfum", "fragrance", 
        "phenoxyethanol", "methylparaben", "propylparaben", "ethylparaben", 
        "butylparaben", "sodium benzoate", "potassium sorbate", "citric acid",
        "disodium edta", "tocopherol", "alcohol", "limonene", "linalool"
    }

    def parse_weighted(text: str) -> dict:
        clean = re.sub(r"\(.*?\)", "", text, flags=re.IGNORECASE)
        clean = re.sub(r"(?i)(ingredients?|состав)\s*:?", "", clean)
        parts = [p.strip().lower() for p in re.split(r"[,;\n]", clean) if p.strip()]
        
        result = {}
        total_items = len(parts)
        for i, item in enumerate(parts):
            # Убираем проценты и цифры
            word = re.sub(r"\s*\d+(\.\d+)?\s*%", "", item).strip()
            if len(word) < 3 or word in IGNORE_LIST:
                continue
                
            # Вес: чем ближе к началу, тем выше (от 3.0 до 1.0)
            # Первые 5 ингредиентов получают максимальный вес
            weight = max(1.0, 3.0 - (i / max(1, total_items)) * 2.0)
            result[word] = weight
        return result

    dict_a = parse_weighted(a_text)
    dict_b = parse_weighted(b_text)
    
    if not dict_a or not dict_b:
        return 0.0

    # Считаем сумму весов совпавших ингредиентов
    shared_weight = 0.0
    for word, weight in dict_a.items():
        if word in dict_b:
            # Берем средний вес из двух составов
            shared_weight += (weight + dict_b[word]) / 2

    # Максимально возможный вес (для нормализации)
    max_possible_weight = sum(dict_a.values()) + sum(dict_b.values())
    if max_possible_weight == 0:
        return 0.0
        
    # Умножаем на 2, так как совпадение учитывается с двух сторон
    score = (shared_weight * 2) / max_possible_weight
    return min(1.0, score) # Ограничиваем максимум 100%


# ---------- OPEN BEAUTY FACTS ----------
async def obf_search(session: aiohttp.ClientSession, search_terms: str = None, category: str = None, limit: int = 50) -> list[dict]:
    params = {"page_size": limit, "fields": "code,product_name,brands,ingredients_text,categories"}
    if category:
        params["categories_tags"] = category
    elif search_terms:
        params["search_terms"] = search_terms
    else:
        return []
    try:
        async with session.get(OBF_SEARCH, params=params, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("products", []) or []
    except Exception as e:
        logging.error(f"OBF search error: {e}")
        return []

async def obf_get_by_barcode(session: aiohttp.ClientSession, barcode: str) -> Optional[dict]:
    url = OBF_PRODUCT.format(barcode=barcode)
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            if data.get("status") != 1: return None
            return data.get("product")
    except Exception as e:
        logging.error(f"OBF barcode error: {e}")
        return None


# ---------- ПОДБОР АНАЛОГОВ ----------
async def find_analogs(product_name: str, barcode: str = None, top_n: int = 5) -> tuple[list[dict], Optional[dict]]:
    lower_name = product_name.lower() if product_name else ""
    source = None
    
    # 1. Сначала ищем в локальной базе
    if lower_name in LOCAL_DB:
        local_data = LOCAL_DB[lower_name]
        source = {
            "product_name": local_data.get("name", "Без названия"),
            "brands": local_data.get("brand", "—"),
            "ingredients_text": local_data.get("ingredients", ""),
            "code": "local_db"
        }
        logging.info(f"Продукт найден в локальной базе: {lower_name}")

    async with aiohttp.ClientSession() as session:
        # 2. Если в локальной базе нет, ищем в OBF
        if not source:
            if barcode:
                source = await obf_get_by_barcode(session, barcode)
            
            if not source:
                category = None
                if any(w in lower_name for w in ["тональный", "foundation", "bb", "cc"]): category = "en:foundations"
                elif any(w in lower_name for w in ["тушь", "mascara"]): category = "en:mascaras"
                elif any(w in lower_name for w in ["шампунь", "shampoo"]): category = "en:shampoos"
                elif any(w in lower_name for w in ["крем", "cream", "moisturizer"]): category = "en:face-creams"

                candidates = await obf_search(session, category=category, limit=100) if category else await obf_search(session, search_terms=product_name, limit=20)
                if candidates:
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

        source_inci_text = source.get("ingredients_text", "")
        
        # 3. Ищем аналоги для сравнения в OBF
        source_name = (source.get("product_name") or "").lower()
        category = "en:face-creams"
        if "foundation" in source_name or "тональный" in source_name: category = "en:foundations"
        elif "mascara" in source_name or "тушь" in source_name: category = "en:mascaras"
        elif "shampoo" in source_name or "шампунь" in source_name: category = "en:shampoos"
        
        candidates = await obf_search(session, category=category, limit=100)

        analogs = []
        for p in candidates:
            if p.get("code") == source.get("code"): continue
            
            other_inci_text = p.get("ingredients_text", "")
            if not other_inci_text: continue
            
            # Используем новую умную функцию
            score = smart_similarity(source_inci_text, other_inci_text)
            
            # Показываем только если совпадение реально высокое (больше 30%)
            if score >= 0.30:
                # Считаем примерное количество общих ингредиентов для отображения
                shared_count = len(normalize_inci(source_inci_text) & normalize_inci(other_inci_text))
                total_count = len(normalize_inci(source_inci_text))
                
                analogs.append({
                    "name": p.get("product_name") or "Без названия",
                    "brand": p.get("brands") or "—",
                    "code": p.get("code"),
                    "score": round(score * 100, 1),
                    "shared": shared_count,
                    "total": total_count,
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
        "Пришлите название косметического средства или его <b>штрихкод</b> (8-14 цифр).\n"
        "Я найду его состав и подберу аналоги с похожими ингредиентами.",
        reply_markup=builder.as_markup(resize_keyboard=True),
        parse_mode="HTML"
    )
    await message.answer("Или нажмите на кнопку ниже, чтобы попробовать пример:", reply_markup=inline_builder.as_markup())

@dp.message(F.text == "❓ Помощь")
async def help_handler(message: types.Message):
    await message.answer("Напиши мне название косметики или её штрихкод, и я поищу аналоги по составу.")

@dp.message(F.text == "ℹ️ О боте")
async def about_handler(message: types.Message):
    await message.answer("Я бот, который ищет аналоги косметики по базе Open Beauty Facts и локальной базе.")

@dp.callback_query(F.data == "example_cerave")
async def example_handler(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("🔍 Ищу состав для «CeraVe Moisturizing Cream»...")
    await callback.message.answer("Это пример. Напишите название своего средства текстом или пришлите штрихкод, чтобы я подобрал аналоги.")


@dp.message(F.text)
async def handle_product(message: types.Message):
    text = message.text.strip()
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
            "❌ Не удалось найти продукт в базе Open Beauty Facts и в локальной базе.\n\n"
            "Попробуйте указать бренд + название или проверьте штрихкод."
        )
        return

    source_name = source.get("product_name") or "Без названия"
    source_brand = source.get("brands") or "—"
    source_inci_text = source.get("ingredients_text") or "Состав не указан"
    
    if len(source_inci_text) > 1000:
        source_inci_text = source_inci_text[:1000] + "..."

    response = f"🧴 <b>Найден продукт:</b>\n<b>{source_name}</b> ({source_brand})\n"
    response += f"📜 <b>Состав:</b>\n<i>{source_inci_text}</i>\n\n"

    if not analogs:
        response += "❌ Аналоги не найдены. Возможно, в базе нет средств с похожим составом."
    else:
        response += "🧪 <b>Аналоги по составу:</b>\n"
        for i, a in enumerate(analogs, 1):
            response += (
                f"{i}. <b>{a['name']}</b> ({a['brand']})\n"
                f"   Совпадение: <b>{a['score']}%</b> ({a['shared']} из {a['total']})\n"
                f"   Штрихкод: <code>{a['code']}</code>\n"
            )
        response += "\n⚠️ Сравнение по взвешенному сходству (умный алгоритм). Это ориентир."

    try:
        if len(response) > 4000:
            for x in range(0, len(response), 4000):
                await message.answer(response[x:x+4000], parse_mode="HTML")
        else:
            await message.answer(response, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка при отправке: {e}")
        await message.answer("Произошла ошибка при формировании ответа.")


# ---------- ЗАПУСК ----------
async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())