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
import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from flask import Flask

# ---------- НАСТРОЙКИ ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
APIFY_TOKEN = os.getenv("APIFY_TOKEN") # ВАЖНО: Добавьте этот токен в Render!
if not BOT_TOKEN:
    raise RuntimeError("Задайте BOT_TOKEN в переменных окружения")

# ID скрапера Logiover на Apify
APIFY_ACTOR = "logiover~open-beauty-facts-scraper"

OBF_SEARCH = "https://world.openbeautyfacts.org/api/v2/search"
OBF_PRODUCT = "https://world.openbeautyfacts.org/api/v2/product/{barcode}.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- ЗАГРУЗКА ЛОКАЛЬНОЙ БАЗЫ ---
LOCAL_DB = {}
try:
    with open('local_brands.json', 'r', encoding='utf-8') as f:
        LOCAL_DB = json.load(f)
    logging.info(f"Локальная база загружена. Брендов: {len(LOCAL_DB)}")
except Exception as e:
    logging.warning(f"Не удалось загрузить local_brands.json: {e}")

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
    if not text: return set()
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

def smart_similarity(a_text: str, b_text: str) -> float:
    if not a_text or not b_text: return 0.0
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
            word = re.sub(r"\s*\d+(\.\d+)?\s*%", "", item).strip()
            if len(word) < 3 or word in IGNORE_LIST: continue
            weight = max(1.0, 3.0 - (i / max(1, total_items)) * 2.0)
            result[word] = weight
        return result
    dict_a = parse_weighted(a_text)
    dict_b = parse_weighted(b_text)
    if not dict_a or not dict_b: return 0.0
    shared_weight = 0.0
    for word, weight in dict_a.items():
        if word in dict_b:
            shared_weight += (weight + dict_b[word]) / 2
    max_possible_weight = sum(dict_a.values()) + sum(dict_b.values())
    if max_possible_weight == 0: return 0.0
    return min(1.0, (shared_weight * 2) / max_possible_weight)


# ---------- ПОИСК ЧЕРЕЗ APIFY ----------
def search_via_apify(product_name: str) -> Optional[str]:
    if not APIFY_TOKEN:
        logging.warning("APIFY_TOKEN не задан, поиск через Apify пропущен.")
        return None
        
    logging.info(f"Запуск Apify для: {product_name}")
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?token={APIFY_TOKEN}"
    
    # Настройки для скрапера Logiover (поиск по названию бренда/продукта)
    body = {
        "searchQuery": product_name,
        "maxResults": 5
    }
    
    try:
        response = requests.post(url, json=body, timeout=60)
        if response.status_code != 200:
            logging.error(f"Ошибка Apify: {response.text}")
            return None
            
        data = response.json()
        if not data or not isinstance(data, list):
            return None
            
        for item in data:
            # Скрапер Logiover возвращает состав в поле ingredientsText или ingredients
            ingredients = item.get("ingredientsText") or item.get("ingredients")
            if ingredients and len(ingredients) > 20:
                return ingredients
                
    except Exception as e:
        logging.error(f"Ошибка запроса к Apify: {e}")
        
    return None


# ---------- OPEN BEAUTY FACTS ----------
async def obf_search(session: aiohttp.ClientSession, search_terms: str = None, category: str = None, limit: int = 50) -> list[dict]:
    params = {"page_size": limit, "fields": "code,product_name,brands,ingredients_text,categories"}
    if category: params["categories_tags"] = category
    elif search_terms: params["search_terms"] = search_terms
    else: return []
    try:
        async with session.get(OBF_SEARCH, params=params, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200: return []
            data = await resp.json()
            return data.get("products", []) or []
    except Exception: return []

async def obf_get_by_barcode(session: aiohttp.ClientSession, barcode: str) -> Optional[dict]:
    url = OBF_PRODUCT.format(barcode=barcode)
    try:
        async with session.get(url, headers=HEADERS, timeout=15) as resp:
            if resp.status != 200: return None
            data = await resp.json()
            if data.get("status") != 1: return None
            return data.get("product")
    except Exception: return None


# ---------- ПОДБОР АНАЛОГОВ ----------
async def find_analogs(product_name: str, barcode: str = None, top_n: int = 5) -> tuple[list[dict], Optional[dict]]:
    lower_name = product_name.lower() if product_name else ""
    source = None
    source_inci_text = ""
    category = None
    
    if any(w in lower_name for w in ["тональный", "foundation", "bb", "cc"]): category = "en:foundations"
    elif any(w in lower_name for w in ["тушь", "mascara"]): category = "en:mascaras"
    elif any(w in lower_name for w in ["шампунь", "shampoo"]): category = "en:shampoos"
    elif any(w in lower_name for w in ["крем", "cream", "moisturizer"]): category = "en:face-creams"

    # 1. Локальная база
    if LOCAL_DB:
        words = [w for w in lower_name.split() if len(w) > 2]
        for key, local_data in LOCAL_DB.items():
            if all(w in key for w in words):
                source = {
                    "product_name": local_data.get("name", "Без названия"),
                    "brands": local_data.get("brand", "—"),
                    "ingredients_text": local_data.get("ingredients", ""),
                    "code": local_data.get("barcode", "local_db")
                }
                source_inci_text = source["ingredients_text"]
                break

    # 2. Open Beauty Facts
    if not source:
        async with aiohttp.ClientSession() as session:
            if barcode: source = await obf_get_by_barcode(session, barcode)
            if not source:
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

    # 3. Apify (Скрапер Logiover)
    if not source:
        logging.info("Поиск через Apify...")
        found_ingredients = search_via_apify(product_name)
        if found_ingredients:
            source = {
                "product_name": product_name,
                "brands": "Найдено через Apify",
                "ingredients_text": found_ingredients,
                "code": "apify_search"
            }
            source_inci_text = found_ingredients

    if not source: return [], None
    if not source_inci_text: source_inci_text = source.get("ingredients_text", "")
    if not source_inci_text or len(source_inci_text) < 20: return [], source

    # 4. Поиск аналогов
    async with aiohttp.ClientSession() as session:
        if not category: category = "en:face-creams"
        candidates = await obf_search(session, category=category, limit=100)
        analogs = []
        for p in candidates:
            if p.get("code") == source.get("code"): continue
            other_inci_text = p.get("ingredients_text", "")
            if not other_inci_text: continue
            
            score = smart_similarity(source_inci_text, other_inci_text)
            if score >= 0.30:
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

# ... (остальной код с хендлерами и запуском остается без изменений) ...