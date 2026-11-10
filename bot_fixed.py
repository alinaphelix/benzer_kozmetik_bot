# bot.py — Telegram-бот подбора косметических аналогов по INCI
# Python 3.10+, aiogram 3.x
#
# Переменные окружения:
#   BOT_TOKEN   — обязательно
#   APIFY_TOKEN — необязательно; используется как дополнительный источник
#   PORT        — Render задаёт автоматически
#
# Локальная база local_brands.json поддерживает словарь:
# {
#   "cerave moisturizing cream": {
#       "name": "CeraVe Moisturizing Cream",
#       "brand": "CeraVe",
#       "ingredients": "Aqua, Glycerin, ...",
#       "barcode": "..."
#   }
# }

import asyncio
import difflib
import html
import json
import logging
import os
import re
import threading
from collections import Counter
from typing import Any, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import KeyboardButton, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from flask import Flask

# ---------------- CONFIG ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
APIFY_ACTOR = "logiover~open-beauty-facts-scraper"

OBF_SEARCH = "https://world.openbeautyfacts.org/api/v2/search"
OBF_PRODUCT = "https://world.openbeautyfacts.org/api/v2/product/{barcode}.json"
HEADERS = {"User-Agent": "CosmeticIngredientMatcher/2.0 (Telegram bot)"}

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=25, connect=8, sock_read=18)
APIFY_TIMEOUT = aiohttp.ClientTimeout(total=75, connect=10, sock_read=65)
MAX_CANDIDATES = 100
MIN_SIMILARITY = 0.35
MAX_MESSAGE_LENGTH = 3900

if not BOT_TOKEN:
    raise RuntimeError("Задайте BOT_TOKEN в переменных окружения.")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("cosmetic_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ---------------- LOCAL DATABASE ----------------

def load_local_db() -> dict[str, Any]:
    path = os.getenv("LOCAL_DB_PATH", "local_brands.json")
    try:
        with open(path, "r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            logger.warning("Локальная база должна быть JSON-объектом.")
            return {}
        logger.info("Локальная база загружена: %s записей", len(data))
        return data
    except FileNotFoundError:
        logger.info("Файл %s не найден; локальная база отключена.", path)
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("Не удалось загрузить локальную базу: %s", exc)
    return {}

LOCAL_DB = load_local_db()

# ---------------- RENDER HEALTH CHECK ----------------

app = Flask(__name__)

@app.route("/")
def health_check():
    return "Bot is running", 200

@app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------- INCI NORMALIZATION ----------------

# Только безопасные общеупотребительные варианты написания.
# Не объединяем ингредиенты с разными химическими функциями.
INGREDIENT_ALIASES = {
    "aqua": "water",
    "eau": "water",
    "water": "water",
    "glycerine": "glycerin",
    "tocopheryl acetate": "tocopheryl acetate",
    "vitamin e": "tocopherol",
}

# Вспомогательные компоненты не удаляются из состава, но получают
# меньший вес при оценке. Это не означает, что они не важны для формулы.
LOW_WEIGHT_INGREDIENTS = {
    "water", "glycerin", "parfum", "fragrance",
    "phenoxyethanol", "sodium benzoate", "potassium sorbate",
    "citric acid", "disodium edta", "limonene", "linalool",
    "benzyl alcohol", "ethylhexylglycerin",
}

def normalize_ingredient(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"\s*\d+(?:[.,]\d+)?\s*%", "", value)
    value = re.sub(r"^[\s•·\-\d.)]+", "", value)
    value = re.sub(r"\s+", " ", value)
    value = value.strip(" .:")
    return INGREDIENT_ALIASES.get(value, value)

def parse_inci(text: str) -> list[str]:
    """Возвращает упорядоченный список уникальных INCI-названий."""
    if not text:
        return []

    clean = html.unescape(text)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\([^)]*\)", " ", clean)
    clean = re.sub(
        r"(?i)\b(?:ingredients?|ingrédients|состав)\s*:?",
        "",
        clean,
    )
    parts = re.split(r"[,;\n\r]+", clean)

    result: list[str] = []
    seen: set[str] = set()
    for part in parts:
        ingredient = normalize_ingredient(part)
        if len(ingredient) < 3 or ingredient.isdigit():
            continue
        if ingredient not in seen:
            seen.add(ingredient)
            result.append(ingredient)
    return result

def ingredient_weight(ingredient: str, position: int, total: int) -> float:
    # INCI обычно перечисляет компоненты по убыванию концентрации,
    # но после порога 1% порядок может быть произвольным.
    if total <= 1:
        rank_weight = 2.0
    else:
        rank_weight = 2.5 - 1.5 * (position / (total - 1))
    if ingredient in LOW_WEIGHT_INGREDIENTS:
        rank_weight *= 0.45
    return max(0.35, rank_weight)

def smart_similarity(a_text: str, b_text: str) -> float:
    """
    Взвешенный коэффициент Жаккара (0..1).
    Совпадения в начале INCI имеют больший вес; общие вспомогательные
    компоненты не доминируют в результате.
    """
    a = parse_inci(a_text)
    b = parse_inci(b_text)
    if not a or not b:
        return 0.0

    weights_a = {
        item: ingredient_weight(item, index, len(a))
        for index, item in enumerate(a)
    }
    weights_b = {
        item: ingredient_weight(item, index, len(b))
        for index, item in enumerate(b)
    }

    all_items = set(weights_a) | set(weights_b)
    intersection = sum(
        min(weights_a[item], weights_b[item])
        for item in set(weights_a) & set(weights_b)
    )
    union = sum(
        max(weights_a.get(item, 0.0), weights_b.get(item, 0.0))
        for item in all_items
    )
    if union <= 0:
        return 0.0

    weighted_jaccard = intersection / union

    # Дополнительная небольшая поправка на близость длины составов:
    # длинный состав с несколькими общими компонентами не должен
    # получать чрезмерно высокий балл.
    length_ratio = min(len(a), len(b)) / max(len(a), len(b))
    return max(0.0, min(1.0, weighted_jaccard * (0.9 + 0.1 * length_ratio)))


# ---------------- PRODUCT CATEGORY ----------------

CATEGORY_RULES = [
    (("тональный", "foundation", "bb cream", "cc cream", "bb-крем", "cc-крем"),
     "en:foundations"),
    (("тушь", "mascara"), "en:mascaras"),
    (("шампунь", "shampoo"), "en:shampoos"),
    (("кондиционер", "conditioner", "бальзам для волос"), "en:hair-conditioners"),
    (("крем для лица", "face cream", "moisturizer", "moisturiser"), "en:face-creams"),
    (("солнцезащит", "sunscreen", "sun cream", "spf"), "en:sunscreens"),
    (("помада", "lipstick"), "en:lipsticks"),
    (("гель для душа", "shower gel"), "en:shower-gels"),
]

def detect_category(text: str, product: Optional[dict] = None) -> Optional[str]:
    haystack = (text or "").lower()
    if product:
        haystack += " " + " ".join(
            str(product.get(key) or "")
            for key in ("product_name", "categories", "categories_tags")
        ).lower()
    for phrases, category in CATEGORY_RULES:
        if any(phrase in haystack for phrase in phrases):
            return category
    return None


# ---------------- OPEN BEAUTY FACTS ----------------

async def obf_search(
    session: aiohttp.ClientSession,
    search_terms: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    params = {
        "page_size": min(max(limit, 1), 100),
        "fields": "code,product_name,brands,ingredients_text,categories,categories_tags",
    }
    if category:
        params["categories_tags"] = category
    if search_terms:
        params["search_terms"] = search_terms
    if not category and not search_terms:
        return []

    try:
        async with session.get(
            OBF_SEARCH, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
        ) as response:
            if response.status != 200:
                logger.warning("Open Beauty Facts search: HTTP %s", response.status)
                return []
            payload = await response.json(content_type=None)
            products = payload.get("products", [])
            return products if isinstance(products, list) else []
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Ошибка Open Beauty Facts search: %s", exc)
        return []

async def obf_get_by_barcode(
    session: aiohttp.ClientSession, barcode: str
) -> Optional[dict]:
    try:
        async with session.get(
            OBF_PRODUCT.format(barcode=barcode),
            headers=HEADERS,
            timeout=REQUEST_TIMEOUT,
        ) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)
            if payload.get("status") != 1:
                return None
            product = payload.get("product")
            return product if isinstance(product, dict) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Ошибка поиска по штрихкоду: %s", exc)
        return None


# ---------------- SOURCE PRODUCT MATCHING ----------------

STOPWORDS = {
    "для", "the", "and", "with", "cream", "крем", "шампунь",
    "shampoo", "маска", "mask", "лосьон", "lotion", "гель", "gel",
}

def name_tokens(value: str) -> set[str]:
    tokens = re.findall(r"[a-zа-яё0-9]+", (value or "").lower())
    return {token for token in tokens if len(token) > 1 and token not in STOPWORDS}

def product_name_score(query: str, product: dict) -> float:
    query_tokens = name_tokens(query)
    product_tokens = name_tokens(
        f"{product.get('product_name') or ''} {product.get('brands') or ''}"
    )
    if not query_tokens or not product_tokens:
        return 0.0
    return len(query_tokens & product_tokens) / len(query_tokens)

def choose_source_product(query: str, products: list[dict]) -> Optional[dict]:
    eligible = [
        p for p in products
        if isinstance(p, dict) and len((p.get("ingredients_text") or "").strip()) >= 20
    ]
    if not eligible:
        return None
    ranked = sorted(
        eligible,
        key=lambda p: (
            product_name_score(query, p),
            min(len(p.get("ingredients_text") or ""), 1500),
        ),
        reverse=True,
    )
    best = ranked[0]
    # Не выбираем случайный товар только потому, что API вернул его первым.
    if product_name_score(query, best) < 0.25:
        return None
    return best

def find_local_product(query: str) -> Optional[dict]:
    query_tokens = name_tokens(query)
    if not query_tokens:
        return None

    best_item = None
    best_score = 0.0
    for key, value in LOCAL_DB.items():
        if not isinstance(value, dict):
            continue
        score = len(query_tokens & name_tokens(str(key))) / len(query_tokens)
        if score > best_score and value.get("ingredients"):
            best_score = score
            best_item = value

    if best_item and best_score >= 0.5:
        return {
            "product_name": best_item.get("name", "Без названия"),
            "brands": best_item.get("brand", "—"),
            "ingredients_text": best_item.get("ingredients", ""),
            "code": best_item.get("barcode") or "local_db",
        }
    return None


# ---------------- OPTIONAL APIFY FALLBACK ----------------

async def search_via_apify(
    session: aiohttp.ClientSession, product_name: str
) -> Optional[str]:
    if not APIFY_TOKEN:
        return None

    category = detect_category(product_name) or "shampoos"
    tokens = name_tokens(product_name)
    brand = next(iter(sorted(tokens, key=len, reverse=True)), product_name)

    url = (
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR}"
        "/run-sync-get-dataset-items"
    )
    body = {
        "mode": "search",
        "categories": [category.removeprefix("en:")],
        "brands": [brand],
        "maxResults": 5,
    }
    try:
        async with session.post(
            url,
            params={"token": APIFY_TOKEN},
            json=body,
            headers=HEADERS,
            timeout=APIFY_TIMEOUT,
        ) as response:
            if response.status != 200:
                logger.warning("Apify вернул HTTP %s", response.status)
                return None
            data = await response.json(content_type=None)
            if not isinstance(data, list):
                return None
            for item in data:
                if not isinstance(item, dict):
                    continue
                ingredients = item.get("ingredientsText") or item.get("ingredients_text")
                if isinstance(ingredients, str) and len(ingredients.strip()) >= 20:
                    return ingredients
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning("Ошибка Apify: %s", exc)
    return None


# ---------------- ANALOG SEARCH ----------------

async def find_analogs(
    product_name: str,
    barcode: Optional[str] = None,
    top_n: int = 5,
) -> tuple[list[dict], Optional[dict]]:
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        source = None

        if barcode:
            source = await obf_get_by_barcode(session, barcode)
            # Поиск по штрихкоду использует реальные данные найденного товара.
            if not source:
                return [], None
        else:
            source = find_local_product(product_name)

            if not source:
                search_results = await obf_search(
                    session, search_terms=product_name, limit=50
                )
                source = choose_source_product(product_name, search_results)

            if not source:
                apify_ingredients = await search_via_apify(session, product_name)
                if apify_ingredients:
                    source = {
                        "product_name": product_name,
                        "brands": "Источник: Apify",
                        "ingredients_text": apify_ingredients,
                        "code": "apify_search",
                    }

        if not source:
            return [], None

        source_inci = source.get("ingredients_text") or ""
        if len(parse_inci(source_inci)) < 3:
            return [], source

        actual_name = source.get("product_name") or product_name
        category = detect_category(product_name, source)

        # Сначала ищем внутри категории. Если категория неизвестна,
        # используем поисковые термины, а не произвольную категорию кремов.
        if category:
            candidates = await obf_search(
                session, category=category, limit=MAX_CANDIDATES
            )
        else:
            candidates = await obf_search(
                session, search_terms=actual_name, limit=MAX_CANDIDATES
            )

        # Для неизвестной категории дополнительно пробуем поиск по названию.
        if not candidates and category:
            candidates = await obf_search(
                session, search_terms=actual_name, limit=MAX_CANDIDATES
            )

        source_code = str(source.get("code") or "")
        source_ingredients = parse_inci(source_inci)
        analogs: list[dict] = []

        for product in candidates:
            if not isinstance(product, dict):
                continue
            candidate_code = str(product.get("code") or "")
            if source_code and candidate_code and candidate_code == source_code:
                continue

            candidate_inci_text = product.get("ingredients_text") or ""
            candidate_ingredients = parse_inci(candidate_inci_text)
            if len(candidate_ingredients) < 3:
                continue

            score = smart_similarity(source_inci, candidate_inci_text)
            if score < MIN_SIMILARITY:
                continue

            shared = sorted(set(source_ingredients) & set(candidate_ingredients))
            analogs.append({
                "name": product.get("product_name") or "Без названия",
                "brand": product.get("brands") or "—",
                "code": candidate_code or "не указан",
                "score": round(score * 100, 1),
                "shared": len(shared),
                "total": len(source_ingredients),
                "shared_ingredients": shared[:8],
            })

        # Дедупликация по штрихкоду, если он есть, иначе по имени и бренду.
        unique: dict[str, dict] = {}
        for item in analogs:
            key = item["code"] if item["code"] != "не указан" else (
                item["name"].lower() + "|" + item["brand"].lower()
            )
            if key not in unique or item["score"] > unique[key]["score"]:
                unique[key] = item

        ranked = sorted(
            unique.values(),
            key=lambda item: (item["score"], item["shared"]),
            reverse=True,
        )
        logger.info(
            "Подбор завершён: source=%s, category=%s, candidates=%s, analogs=%s",
            actual_name, category, len(candidates), len(ranked),
        )
        return ranked[:top_n], source


# ---------------- TELEGRAM HELPERS ----------------

def split_html_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Разбивает готовый HTML по строкам, не разрезая отдельные строки."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        if len(line) > limit:
            # Все динамические поля экранированы; длинная строка режется
            # только как аварийный fallback.
            for start in range(0, len(line), limit):
                piece = line[start:start + limit]
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(piece)
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks or [""]

async def send_html(message: types.Message, text: str):
    for chunk in split_html_message(text):
        await message.answer(chunk, parse_mode="HTML")

async def run_product_search(message: types.Message, query: str):
    query = query.strip()
    barcode = query if query.isdigit() and 8 <= len(query) <= 14 else None

    if barcode:
        await message.answer(f"🔍 Ищу продукт по штрихкоду {html.escape(barcode)}…")
        search_name = ""
    else:
        if len(query) < 2:
            await message.answer("Название слишком короткое. Пришлите название или штрихкод.")
            return
        if len(query) > 180:
            await message.answer("Слишком длинный запрос. Сократите название продукта.")
            return
        await message.answer(f"🔍 Ищу состав для «{html.escape(query)}»…")
        search_name = query

    try:
        analogs, source = await find_analogs(
            product_name=search_name,
            barcode=barcode,
        )
    except Exception:
        logger.exception("Непредвиденная ошибка подбора")
        await message.answer("⚠️ Не удалось выполнить поиск. Попробуйте немного позже.")
        return

    if not source:
        await message.answer(
            "❌ Не удалось найти продукт.\n\n"
            "Проверьте штрихкод или укажите бренд и полное название средства."
        )
        return

    source_name = html.escape(str(source.get("product_name") or "Без названия"))
    source_brand = html.escape(str(source.get("brands") or "—"))
    source_inci = str(source.get("ingredients_text") or "Состав не указан")
    source_inci = html.escape(source_inci[:900] + ("…" if len(source_inci) > 900 else ""))

    response = (
        f"🧴 <b>Найден продукт:</b>\n"
        f"<b>{source_name}</b> ({source_brand})\n\n"
        f"📜 <b>Состав:</b>\n<i>{source_inci}</i>\n\n"
    )

    if not source.get("ingredients_text"):
        response += (
            "⚠️ Для этого продукта в источнике нет полного состава INCI, "
            "поэтому надёжно подобрать аналоги нельзя."
        )
    elif not analogs:
        response += (
            "❌ В доступной базе не найдено достаточно похожих составов.\n"
            "Это не доказывает, что аналогов не существует: база может быть неполной."
        )
    else:
        response += "🧪 <b>Наиболее похожие составы:</b>\n"
        for index, item in enumerate(analogs, 1):
            name = html.escape(str(item["name"]))
            brand = html.escape(str(item["brand"]))
            code = html.escape(str(item["code"]))
            shared = ", ".join(item["shared_ingredients"])
            shared = html.escape(shared) if shared else "—"
            response += (
                f"\n{index}. <b>{name}</b> ({brand})\n"
                f"   Сходство INCI: <b>{item['score']}%</b>\n"
                f"   Общих ингредиентов: {item['shared']} из {item['total']}\n"
                f"   Совпадения: {shared}\n"
                f"   Штрихкод: <code>{code}</code>\n"
            )
        response += (
            "\n<i>Процент отражает сходство списка и порядка ингредиентов, "
            "а не идентичность формул, эффективности или переносимости.</i>"
        )

    try:
        await send_html(message, response)
    except Exception:
        logger.exception("Не удалось отправить ответ Telegram")
        await message.answer("Не удалось отправить результат. Попробуйте ещё раз.")


# ---------------- HANDLERS ----------------

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    keyboard = ReplyKeyboardBuilder()
    keyboard.add(KeyboardButton(text="❓ Помощь"))
    keyboard.add(KeyboardButton(text="ℹ️ О боте"))
    keyboard.adjust(2)

    inline = InlineKeyboardBuilder()
    inline.add(InlineKeyboardButton(
        text="🧴 Пример: CeraVe",
        callback_data="example_cerave",
    ))

    await message.answer(
        "Пришлите название косметического средства или штрихкод (8–14 цифр).\n"
        "Я попробую найти состав INCI и подобрать продукты с похожими составами.",
        reply_markup=keyboard.as_markup(resize_keyboard=True),
    )
    await message.answer(
        "Или нажмите кнопку, чтобы проверить пример:",
        reply_markup=inline.as_markup(),
    )

@dp.message(F.text == "❓ Помощь")
async def help_handler(message: types.Message):
    await message.answer(
        "Отправьте бренд и название косметики либо штрихкод.\n"
        "Чем точнее название, тем меньше риск выбрать не тот продукт."
    )

@dp.message(F.text == "ℹ️ О боте")
async def about_handler(message: types.Message):
    await message.answer(
        "Я ищу косметические продукты и сравниваю их списки ингредиентов INCI. "
        "Сходство состава не гарантирует одинаковую эффективность или взаимозаменяемость."
    )

@dp.callback_query(F.data == "example_cerave")
async def example_handler(callback: types.CallbackQuery):
    await callback.answer()
    if callback.message:
        await run_product_search(callback.message, "CeraVe Moisturizing Cream")

@dp.message(F.text)
async def handle_product(message: types.Message):
    await run_product_search(message, message.text or "")

@dp.message()
async def unsupported_handler(message: types.Message):
    await message.answer("Пожалуйста, отправьте название продукта или штрихкод текстом.")


# ---------------- STARTUP ----------------

async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    logger.info("Telegram-бот запускается.")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
