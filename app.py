import os
from collections import defaultdict
from datetime import date

from flask import Flask, request, jsonify
from flask_cors import CORS
from openai import OpenAI

app = Flask(__name__)
CORS(app)  # фронт на GitHub Pages сможет дёргать этот бэкенд

# --- Модель: провайдер-агностик через OpenAI-совместимый эндпоинт ---
# Меняешь провайдера = меняешь 3 переменные окружения, код не трогаешь.
#   Gemini:   LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/   LLM_MODEL=gemini-2.5-flash-lite
#   DeepSeek: LLM_BASE_URL=https://api.deepseek.com                                    LLM_MODEL=deepseek-chat
client = OpenAI(
    api_key=os.environ.get("LLM_API_KEY"),
    base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
)
MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

# --- Анти-абьюз: грубый лимит по IP, чтобы один человек не сжёг бюджет ---
ip_usage = defaultdict(lambda: {"day": date.today().isoformat(), "count": 0})
DAILY_IP_CAP = 30  # бэкстоп; основной триал-счётчик живёт на фронте


def over_ip_cap(ip):
    u = ip_usage[ip]
    today = date.today().isoformat()
    if u["day"] != today:
        u["day"], u["count"] = today, 0
    if u["count"] >= DAILY_IP_CAP:
        return True
    u["count"] += 1
    return False


# --- Промпт = ТВОЙ МОАТ. Качество живёт здесь, а не в коде. ---
SYSTEM_PROMPT = """You are a world-class Etsy SEO and conversion copywriter.
A seller gives you a product. You produce a complete, ready-to-paste Etsy listing
optimized to BOTH rank in Etsy search AND convert browsers into buyers.

Rules:
- TITLE: max 140 characters. Front-load the 2-3 highest-intent buyer search phrases.
  Lead with what the item IS plus key attributes (material, style, occasion, recipient).
  Natural and readable, never keyword-stuffed gibberish.
- DESCRIPTION: the first 1-2 lines are the search snippet, so they must hook the buyer
  AND contain the main keywords. Then benefit-driven, scannable copy: what it is, who
  it's for, why they'll love it, key details (size, material, personalization), and a
  soft call to action. Short paragraphs or bullets. Warm, human, specific, not corporate.
- TAGS: exactly 13 tags. Each <= 20 characters. Multi-word long-tail phrases that buyers
  actually type. Cover variations, occasions, recipients, styles, and use-cases. Do not
  just repeat the title phrase 13 times.
- Use only realistic claims based on what the seller told you. Never invent materials,
  sizes, or specs that were not given or clearly implied.

Output in EXACTLY this format and nothing else:

TITLE:
<title>

DESCRIPTION:
<description>

TAGS:
<tag1, tag2, tag3, tag4, tag5, tag6, tag7, tag8, tag9, tag10, tag11, tag12, tag13>
"""


@app.route("/")
def home():
    return "Etsy Listing Generator API is running. Use POST /generate."


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
        f"Product: {product}\n"
        f"Seed keywords: {keywords or '(none given)'}\n"
        f"Extra details: {details or '(none given)'}"
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.7,
            max_tokens=900,
        )
        return jsonify({"result": resp.choices[0].message.content})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": "llm_failed", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))  # Railway задаёт PORT сам
    app.run(host="0.0.0.0", port=port)
