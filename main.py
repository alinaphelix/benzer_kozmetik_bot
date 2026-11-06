import logging
import re
import json
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from googlesearch import search
import requests
from bs4 import BeautifulSoup

# --- НАСТРОЙКИ ---
API_TOKEN = '9278451675:AAHnp8QFBLpz6md4J55FXqdvaTvBNO9foDc'
# Загрузка вашей локальной базы аналогов (пример структуры)
# try:
#     with open('analogs_db.json', 'r', encoding='utf-8') as f:
#         ANALOGS_DB = json.load(f)
# except FileNotFoundError:
#     ANALOGS_DB = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)
# Загрузка базы аналогов из файла
try:
    with open('analogs.json', 'r', encoding='utf-8') as f:
        ANALOGS_DB = json.load(f)
    print("База аналогов успешно загружена.")
except FileNotFoundError:
    print("ОШИБКА: Файл analogs.json не найден. Положите его в папку с ботом.")
    ANALOGS_DB = {}
except json.JSONDecodeError:
    print("ОШИБКА: Файл analogs.json поврежден или имеет неверный формат.")
    ANALOGS_DB = {}

# Сайты, на которых мы будем искать составы
TARGET_DOMAINS = [
    'cosmobase.ru', 'incidecoder.com', 'cosdna.com', 
    'laroche-posay.ru', 'vichyconsult.ru', 'bioderma.com'
]

def normalize_inci(text: str) -> list:
    """Очищает текст и превращает его в список ингредиентов INCI."""
    # Удаляем всё, что не похоже на ингредиенты
    clean_text = re.sub(r'$$.*?$$|$.*?$|Ingredients:|Состав:', '', text, flags=re.IGNORECASE)
    # Разделяем по запятым, скобкам и переносам строк
    items = re.split(r',|;|\n|\|', clean_text)
    # Финальная очистка каждого слова
    result = []
    for item in items:
        word = item.strip().title()  # Приводим к стандарту INCI (первая буква заглавная)
        # Убираем концентрации (например, "Glycerin 5%") и мусор
        word = re.sub(r'\s*\d+.*', '', word)
        if word and len(word) > 2 and not word.isdigit():
            result.append(word)
    return list(dict.fromkeys(result)) # Убираем дубликаты, сохраняя порядок

async def find_inci_by_name(product_name: str) -> list | None:
    """Ищет состав по названию через Google и парсинг."""
    query = f'"{product_name}" ingredients site:{" OR site:".join(TARGET_DOMAINS)}'
    logging.info(f"Searching Google for: {query}")
    
    try:
        urls = search(query, num_results=3, lang='ru')
    except Exception as e:
        logging.error(f"Google search failed: {e}")
        return None

    for url in urls:
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, headers=headers, timeout=10)
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Попытка 1: Найти тег по ключевому слову
            inci_text = soup.find(string=re.compile(r'ingredients', re.I))
            if inci_text:
                parent = inci_text.find_parent()
                if parent:
                    return normalize_inci(parent.get_text())

            # Попытка 2: Поиск в метаданных или специфических блоках
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = script.string
                    if 'ingredients' in data.lower():
                        # Здесь нужна более сложная логика извлечения JSON, для примера упрощено
                        return normalize_inci(data)
                except Exception:
                    continue
        except Exception as e:
            logging.error(f"Failed to parse {url}: {e}")
            continue
            
    return None

def find_analogs_by_name(product_name: str) -> list:
    """
    Ищет аналоги по точному названию в нашей базе.
    """
    # Убираем лишние пробелы по краям и приводим к нижнему регистру для надежности
    clean_name = product_name.strip().lower()
    
    # Проходим по каждому средству в нашей базе
    for db_name, data in ANALOGS_DB.items():
        # Сравниваем базу с тем, что ввел пользователь
        if clean_name == db_name.lower():
            return data.get("analogs", [])
            
    # Если ничего не нашлось
    return []
@dp.message_handler()
async def handle_product(message: types.Message):
    # Бот просто пришлет назад то, что вы ему написали, и покажет содержимое всей базы
    await message.answer(f"Вы написали: {message.text}")
    await message.answer(f"Содержимое базы данных: {ANALOGS_DB}")

@dp.message_handler()
async def handle_product(message: types.Message):
    product_name = message.text.strip()
    await message.answer(f"🔍 Ищу состав для: {product_name}...")

    inci = await find_inci_by_name(product_name)
    
    if not inci:
        await message.answer("❌ Не удалось найти состав по названию. Попробуйте прислать сам список ингредиентов (INCI) с упаковки.")
        return

    # Формируем красивый ответ
    inci_text = "\n".join([f"{i+1}. {item}" for i, item in enumerate(inci[:15])]) # Показываем первые 15
    if len(inci) > 15:
        inci_text += f"\n... и еще {len(inci) - 15} компонентов."

    analogs = find_analogs_by_name(product_name)
    
    response = f"✅ Состав для *{product_name}*\n\n{inci_text}\n\n"
    
    if analogs:
        response += f"🧪 *Похожие по формуле средства:*\n" + "\n".join(analogs)
    else:
        response += "🧪 Точных аналогов в базе не найдено. Поиск идет по совпадению базы и активов."

    await message.answer(response)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())