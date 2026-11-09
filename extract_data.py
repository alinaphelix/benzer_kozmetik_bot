import json
from bson import decode_file_iter

INPUT_FILE = 'openbeautyfacts-mongodbdump.bson'
OUTPUT_FILE = 'local_brands.json'

local_db = {}
count = 0

print("Читаем файл по кусочкам...")

try:
    with open(INPUT_FILE, 'rb') as f:
        # Читаем файл порциями (даже если он поврежден, мы попробуем поймать ошибку)
        for doc in decode_file_iter(f):
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
            
            # Каждые 5000 продуктов сохраняем, чтобы не потерять данные при ошибке
            if count % 5000 == 0:
                print(f"Найдено: {count}...")
                with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
                    json.dump(local_db, out, ensure_ascii=False)

except Exception as e:
    print(f"Ошибка чтения файла на {count}-м продукте: {e}")
    print("Сохраняю то, что удалось прочитать...")

# Финальное сохранение
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(local_db, f, ensure_ascii=False, indent=2)

print(f"ГОТОВО! Сохранено продуктов: {len(local_db)}")