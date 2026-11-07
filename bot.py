# bot.py — Telegram-бот подбора аналогов косметики по составу
# Python 3.10+, aiogram 3.x

import asyncio
import logging
import os
import re
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Задайте BOT_TOKEN в переменных окружения")

# Open Beauty Facts API (бесплатный, без ключа) [citation:11][citation:19]
OBF_SEARCH = "https://world.openbeautyfacts.org/api/v2/search"
OBF_PRODUCT = "https://world.openbeautyfacts.org/api/v2/product/{barcode}.json"
HEADERS = {"User-Agent": "CosmeticAnalogBot/1.0 (contact: your@email)"}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- ПАРСИНГ INCI ----------
def normalize_inci(text: str) -> set[str]:
    """Превращает строку состава в множество нормализованных INCI-имён."""
    if not text:
        return set()
    # Убираем служебные пометки и текст в скобках-уточнениях
    clean = re.sub(r"\(.*?\)", "", text, flags=re.IGNORECASE)
    clean = re.sub(r"(?i)(ingredients?|состав)\s*:?", "", clean)
    # Разделители: запятая, точка с запятой, перенос строки
    parts = re.split(r"[,;\n]", clean)
    result = set()
    for p in parts:
        w = p.strip().lower()
        # Убираем концентрации вида "5%" и пустые/слишком короткие токены
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
# Обновленная функция поиска
# 1. Меняем функцию поиска, чтобы она могла искать по категории
async def obf_search_by_name(session: aiohttp.ClientSession, name: str, category: str = None, limit: int = 50) -> list[dict]:
    """Ищет продукты. Если передана категория, ищет только в ней."""
    params = {
        "page_size": limit,
        "fields": "code,product_name,brands,ingredients_text,categories",
    }
    
    if category:
        # Ищем ТОЛЬКО по категории (без search_terms, чтобы фильтр сработал)
        params["categories_tags"] = category
    else:
        # Если категория не определена, ищем по названию
        params["search_terms"] = name
        
    async with session.get(OBF_SEARCH, params=params, headers=HEADERS, timeout=15) as resp:
        if resp.status != 200:
            return []
        data = await resp.json()
    return data.get("products", []) or []

# 2. Обновляем логику подбора
async def find_analogs(product_name: str, top_n: int = 5) -> list[dict]:
    lower_name = product_name.lower()
    category = None
    
    # Определяем категорию (используем теги, которые есть в базе)
    if any(word in lower_name for word in ["тональный", "foundation", "bb", "cc"]):
        category = "en:foundations"  # Или "en:face-makeup"
    elif any(word in lower_name for word in ["тушь", "mascara"]):
        category = "en:mascaras"
    elif any(word in lower_name for word in ["шампунь", "shampoo"]):
        category = "en:shampoos"
    elif any(word in lower_name for word in ["крем", "cream", "moisturizer"]):
        category = "en:face-creams"
    else:
        # Если не угадали, ищем как раньше (по названию)
        category = None

    async with aiohttp.ClientSession() as session:
        # Если категория определена, ищем в ней. Если нет — по названию.
        if category:
            candidates = await obf_search_by_name(session, product_name, category=category)
        else:
            candidates = await obf_search_by_name(session, product_name, category=None)

        if not candidates:
            return []

        # ... (дальше ваш код сравнения Jaccard без изменений) ...
# Обновленная функция подбора
async def find_analogs(product_name: str, top_n: int = 5) -> list[dict]:
    # Пытаемся угадать категорию по ключевым словам в названии
    lower_name = product_name.lower()
    category = None
    
    if "тональный" in lower_name or "foundation" in lower_name or "крем" in lower_name:
        category = "makeup" # Или более точная категория: "foundations"
    elif "шампунь" in lower_name or "shampoo" in lower_name:
        category = "shampoos"
    elif "тушь" in lower_name or "mascara" in lower_name:
        category = "mascaras"
        
    async with aiohttp.ClientSession() as session:
        candidates = await obf_search_by_name(session, product_name, category)
        
        # Дальше ваш код без изменений...
        if not candidates:
            return []

        source = next((p for p in candidates if p.get("ingredients_text")), None)
        if not source:
            return []

        source_inci = normalize_inci(source["ingredients_text"])
        if len(source_inci) < 3:
            return []

        analogs = []
        for p in candidates:
            # ... остальной код сравнения (Jaccard) ...

# ---------- ПОДБОР АНАЛОГОВ ----------
async def find_analogs(product_name: str, top_n: int = 5) -> list[dict]:
    """
    1. Ищет исходный продукт по названию.
    2. Берёт его INCI-состав.
    3. Сравнивает с другими продуктами из выдачи по Jaccard.
    Возвращает список аналогов, отсортированных по убыванию сходства.
    """
    async with aiohttp.ClientSession() as session:
        candidates = await obf_search_by_name(session, product_name)
        if not candidates:
            return []

        # Исходный продукт — первый, у которого есть состав
        source = next((p for p in candidates if p.get("ingredients_text")), None)
        if not source:
            return []

        source_inci = normalize_inci(source["ingredients_text"])
        if len(source_inci) < 3:  # слишком короткий/битый состав
            return []

        analogs = []
        for p in candidates:
            if p.get("code") == source.get("code"):
                continue
            other_inci = normalize_inci(p.get("ingredients_text", ""))
            if len(other_inci) < 3:
                continue
            score = jaccard(source_inci, other_inci)
            if score >= 0.15:  # порог: хотя бы 15% общих ингредиентов
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
    await message.answer(
        "Пришлите название косметического средства (крем, шампунь, тональный крем и т.п.).\n"
        "Я найду его состав и подберу аналоги с похожими ингредиентами."
    )


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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())