import os
import secrets
from collections import defaultdict
from datetime import date

import psycopg2
from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__)
CORS(app)

client = OpenAI(
    api_key=os.environ.get("LLM_API_KEY"),
    base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

DATABASE_URL = os.environ.get("DATABASE_URL")
# Секрет, который знает только бот — чтобы левый человек не мог сам себе сгенерить ключ
BOT_SECRET = os.environ.get("BOT_SECRET", "")


def db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    if not DATABASE_URL:
        return
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS license_keys (
                    key TEXT PRIMARY KEY,
                    telegram_user_id BIGINT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    redeemed_at TIMESTAMP
                )
            """)
        conn.commit()


# --- Анти-абьюз: лимит по IP на бесплатные генерации ---
ip_usage = defaultdict(lambda: {"day": date.today().isoformat(), "count": 0})
DAILY_IP_CAP = 30


def over_ip_cap(ip):
    u = ip_usage[ip]
    today = date.today().isoformat()
    if u["day"] != today:
        u["day"], u["count"] = today, 0
    if u["count"] >= DAILY_IP_CAP:
        return True
    u["count"] += 1
    return False


SYSTEM_PROMPT = """Ты — эксперт по SEO и продающим текстам для маркетплейсов Wildberries и Ozon.
Продавец присылает товар. Ты возвращаешь готовую оптимизированную карточку: название под WB,
название под Ozon, продающее SEO-описание и список поисковых ключей.

Правила:
- НАЗВАНИЕ WB: до 60 символов. В начало — 3-4 самых частотных поисковых ключа. Сначала ТИП товара,
  затем ключевые атрибуты (назначение, для кого, материал, цвет). Читаемо, без мусора и спецсимволов.
- НАЗВАНИЕ OZON: до 200 символов. Схема: тип - бренд (если есть) - модель - важные характеристики
  (цвет, материал, размер, назначение). Ключей больше, чем в WB, но без переспама.
- ОПИСАНИЕ: 1000-1500 символов. Первое предложение — с главным ключом. Дальше продающий,
  человекочитаемый текст: что это, для кого, выгоды, характеристики, применение, уход. Короткие абзацы.
  Ключевые слова вплетай ЕСТЕСТВЕННО (3-5 вхождений), без переспама и без перечисления через запятую.
  Без спецсимволов.
- КЛЮЧЕВЫЕ СЛОВА: 10-15 поисковых фраз, которые реально вводят покупатели — длиннохвостые, разные
  формулировки, синонимы, назначение, для кого. Через запятую.
- Используй ТОЛЬКО реальные факты из того, что дал продавец. Не выдумывай материалы, размеры и
  характеристики, которых не было во вводе.

Верни СТРОГО в этом формате, без лишнего текста до или после:

НАЗВАНИЕ WB:
<название>

НАЗВАНИЕ OZON:
<название>

ОПИСАНИЕ:
<описание>

КЛЮЧЕВЫЕ СЛОВА:
<ключ1, ключ2, ключ3, ...>
"""


@app.route("/")
def home():
    return "Card Generator API is running. Use POST /generate."


@app.route("/health")
def health():
    return "ok"


@app.route("/generate", methods=["POST"])
def generate():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    if over_ip_cap(ip):
        return jsonify({"error": "daily_limit"}), 429

    data = request.get_json(silent=True) or {}
    product = (data.get("product") or "").strip()
    keywords = (data.get("keywords") or "").strip()
    details = (data.get("details") or "").strip()
    if not product:
        return jsonify({"error": "no_product"}), 400

    user_msg = (
        f"Товар: {product}\n"
        f"Ключевые слова (если есть): {keywords or '—'}\n"
        f"Доп. детали: {details or '—'}"
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=1500,
        )
        return jsonify({"result": resp.choices[0].message.content})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "llm_failed", "detail": str(e)}), 500


# --- ЛИЦЕНЗИОННЫЕ КЛЮЧИ ---

@app.route("/admin/create_key", methods=["POST"])
def create_key():
    """Вызывается ТОЛЬКО ботом после успешной оплаты Stars. Требует BOT_SECRET."""
    data = request.get_json(silent=True) or {}
    if not BOT_SECRET or data.get("secret") != BOT_SECRET:
        return jsonify({"error": "forbidden"}), 403

    telegram_user_id = data.get("telegram_user_id")
    new_key = "CARD-" + secrets.token_hex(4).upper() + "-" + secrets.token_hex(4).upper()

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO license_keys (key, telegram_user_id) VALUES (%s, %s)",
                    (new_key, telegram_user_id),
                )
            conn.commit()
        return jsonify({"key": new_key})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "db_failed", "detail": str(e)}), 500


@app.route("/redeem", methods=["POST"])
def redeem():
    """Покупатель вводит ключ на сайте. Если валиден — фронт снимает лимит навсегда."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip().upper()
    if not key:
        return jsonify({"valid": False, "error": "no_key"}), 400

    try:
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT key FROM license_keys WHERE key = %s", (key,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"valid": False, "error": "not_found"}), 404
                cur.execute(
                    "UPDATE license_keys SET redeemed_at = NOW() WHERE key = %s AND redeemed_at IS NULL",
                    (key,),
                )
            conn.commit()
        return jsonify({"valid": True})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"valid": False, "error": "db_failed", "detail": str(e)}), 500


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
