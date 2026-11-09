import json
import os
from bson import decode_file_iter  # <-- ИСПРАВЛЕННЫЙ ИМПОРТ

# ПРАВИЛЬНОЕ ИМЯ ВАШЕГО ФАЙЛА
INPUT_FILE = 'openbeautyfacts-mongodbdump.bson' 
OUTPUT_FILE = 'local_brands.json'

if not os.path.exists(INPUT_FILE):
    print(f"ОШИБКА: Файл {INPUT_FILE} не найден в этой папке!")
    exit()

print("Начинаю потоковую обработку файла (940 МБ). Это займет время...")

local_db = {}
count = 0

# Открываем файл и читаем его по одному документу
with open(INPUT_FILE, 'rb') as f:
    for doc in decode_file_iter(f):
        # Фильтр по категориям (чтобы база не была слишком большой)
        categories = doc.get('categories', '').lower()
        
        is_needed = False
        if any(cat in categories for cat in ['face cream', 'foundation', 'shampoo', 'mascara', 'крем', 'тональный']):
            is_needed = True
            
        if not is_needed:
            continue

        # Нам нужны только продукты с составом
        if 'ingredients_text' not in doc or not doc['ingredients_text']:
            continue
            
        name = doc.get('product_name_ru') or doc.get('product_name') or ''
        brand = doc.get('brands') or ''
        barcode = doc.get('code') or ''
        
        if not name.strip():
            continue
            
        key = f"{name} {brand}".strip().lower()
        
        local_db[key] = {
            "brand": brand.strip(),
            "name": name.strip(),
            "ingredients": doc['ingredients_text'].strip(),
            "barcode": barcode
        }
        count += 1
        
        if count % 1000 == 0:
            print(f"Найдено подходящих продуктов: {count}...")

# Сохраняем результат
print(f"Всего найдено: {len(local_db)} продуктов. Сохраняю в файл...")

with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(local_db, f, ensure_ascii=False, indent=2)

print(f"ГОТОВО! Создан файл: {OUTPUT_FILE}")
print(f"Размер базы: {len(local_db)} продуктов.")