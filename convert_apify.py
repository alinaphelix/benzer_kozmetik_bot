import json
import re

INPUT_FILE = 'apify_raw_data.json'
OUTPUT_FILE = 'local_brands.json'

print("Читаю данные Apify...")

try:
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
except FileNotFoundError:
    print(f"ОШИБКА: Файл {INPUT_FILE} не найден! Переименуйте скачанный файл в {INPUT_FILE}")
    exit()
except json.JSONDecodeError:
    print(f"ОШИБКА: Файл {INPUT_FILE} поврежден.")
    exit()

local_db = {}
count = 0
skipped = 0

for item in data:
    # Достаем данные из полей Apify
    name = item.get('productName') or ''
    brand = item.get('brands') or ''
    ingredients = item.get('ingredientsText') or ''
    barcode = item.get('code') or ''

    # Если нет названия или состава, пропускаем (это мусор)
    if not name.strip() or not ingredients.strip():
        skipped += 1
        continue

    # Очищаем состав от переносов строк
    ingredients = ingredients.replace('\n', ' ').replace('\r', ' ').strip()
    ingredients = re.sub(r'\s+', ' ', ingredients)

    # Формируем ключ для бота (название + бренд)
    key = f"{name} {brand}".strip().lower()
    
    local_db[key] = {
        "brand": brand.strip() or "—",
        "name": name.strip(),
        "ingredients": ingredients,
        "barcode": str(barcode).strip()
    }
    count += 1

# Сохраняем
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(local_db, f, ensure_ascii=False, indent=2)

print(f"ГОТОВО! Добавлено продуктов: {count}")
print(f"Пропущено (без состава или названия): {skipped}")
print(f"Файл {OUTPUT_FILE} обновлен.")