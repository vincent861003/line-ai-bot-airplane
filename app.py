import os
import threading
import time
import requests
import psycopg2
from datetime import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, PushMessageRequest,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai

app = Flask(__name__)

# ── 環境變數 ──────────────────────────────────────────────
LINE_TOKEN  = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_SECRET = os.environ["LINE_CHANNEL_SECRET"]
GEMINI_KEY  = os.environ["GEMINI_API_KEY"]
TP_TOKEN    = os.environ["TRAVELPAYOUTS_TOKEN"]   # Travelpayouts API token
DATABASE_URL = os.environ["DATABASE_URL"]          # Railway PostgreSQL

# ── 初始化 ────────────────────────────────────────────────
configuration = Configuration(access_token=LINE_TOKEN)
handler       = WebhookHandler(LINE_SECRET)
gemini_client = genai.Client(api_key=GEMINI_KEY)

# ── 資料庫 ────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS routes (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    last_price INTEGER,
                    updated_at TIMESTAMP,
                    UNIQUE(user_id, origin, destination)
                )
            """)
        conn.commit()

# ── Travelpayouts 查價 ────────────────────────────────────
def fetch_price(origin: str, destination: str) -> int | None:
    """查詢指定航線本月最低票價（美元）。回傳整數或 None。"""
    month = datetime.utcnow().strftime("%Y-%m")
    url = (
        f"https://api.travelpayouts.com/v1/prices/cheap"
        f"?origin={origin}&destination={destination}"
        f"&depart_date={month}&currency=usd&token={TP_TOKEN}"
    )
    try:
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if not data.get("success") or not data.get("data"):
            return None
        prices = [v["price"] for v in data["data"].get(destination, {}).values()]
        return min(prices) if prices else None
    except Exception as e:
        app.logger.error("fetch_price error: %s", e)
        return None

# ── 排程：每天查價一次 ────────────────────────────────────
def price_check_loop():
    time.sleep(30)  # 等服務完全啟動
    while True:
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, user_id, origin, destination, last_price FROM routes")
                    rows = cur.fetchall()
            for row_id, user_id, origin, destination, last_price in rows:
                new_price = fetch_price(origin, destination)
                if new_price is None:
                    continue
                # 降價才通知
                if last_price is not None and new_price < last_price:
                    diff = last_price - new_price
                    msg = (
                        f"✈️ 降價通知！\n"
                        f"{origin} → {destination}\n"
                        f"原價：${last_price} USD\n"
                        f"現價：${new_price} USD\n"
                        f"降了 ${diff} USD！\n"
                        f"🔗 查票：https://www.aviasales.com/search/{origin}{destination}"
                    )
                    with ApiClient(configuration) as api_client:
                        MessagingApi(api_client).push_message(
                            PushMessageRequest(
                                to=user_id,
                                messages=[TextMessage(text=msg)]
                            )
                        )
                # 更新資料庫
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE routes SET last_price=%s, updated_at=%s WHERE id=%s",
                            (new_price, datetime.utcnow(), row_id)
                        )
                    conn.commit()
        except Exception as e:
            app.logger.error("price_check_loop error: %s", e)
        time.sleep(86400)  # 24 小時

# ── 指令處理 ──────────────────────────────────────────────
def handle_command(user_id: str, text: str) -> str:
    parts = text.strip().split()
    cmd = parts[0] if parts else ""

    # 追蹤 TPE TYO
    if cmd == "追蹤" and len(parts) == 3:
        origin, dest = parts[1].upper(), parts[2].upper()
        price = fetch_price(origin, dest)
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO routes (user_id, origin, destination, last_price, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, origin, destination) DO UPDATE
                    SET last_price=EXCLUDED.last_price, updated_at=EXCLUDED.updated_at
                """, (user_id, origin, dest, price, datetime.utcnow()))
            conn.commit()
        price_str = f"${price} USD" if price else "暫無資料"
        return f"✅ 已開始追蹤 {origin} → {dest}\n目前最低票價：{price_str}\n降價時會通知你！"

    # 清單
    if cmd == "清單":
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT origin, destination, last_price, updated_at FROM routes WHERE user_id=%s",
                    (user_id,)
                )
                rows = cur.fetchall()
        if not rows:
            return "目前沒有追蹤任何航線。\n\n輸入「追蹤 出發地 目的地」開始追蹤，例如：追蹤 TPE TYO"
        lines = ["📋 你的追蹤清單："]
        for origin, dest, price, updated in rows:
            price_str = f"${price} USD" if price else "暫無資料"
            lines.append(f"• {origin} → {dest}：{price_str}")
        return "\n".join(lines)

    # 取消 TPE TYO
    if cmd == "取消" and len(parts) == 3:
        origin, dest = parts[1].upper(), parts[2].upper()
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM routes WHERE user_id=%s AND origin=%s AND destination=%s",
                    (user_id, origin, dest)
                )
            conn.commit()
        return f"🗑️ 已取消追蹤 {origin} → {dest}"

    # 說明
    if cmd == "說明" or cmd == "help":
        return (
            "✈️ 機票追蹤機器人使用說明\n\n"
            "追蹤 出發地 目的地\n"
            "  例：追蹤 TPE TYO\n\n"
            "清單 — 查看追蹤中的航線\n\n"
            "取消 出發地 目的地\n"
            "  例：取消 TPE TYO\n\n"
            "其他問題直接問我，我是 AI 助理！\n\n"
            "常用機場代碼：\n"
            "TPE 台北、TYO 東京、NRT 成田\n"
            "KIX 大阪、HKG 香港、BKK 曼谷\n"
            "SIN 新加坡、ICN 首爾、LAX 洛杉磯"
        )

    return None  # 不是指令，交給 Gemini

# ── Gemini 對話 ───────────────────────────────────────────
def ask_gemini(text: str) -> str:
    try:
        resp = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=text,
            config={
                "system_instruction": (
                    "你是一個親切的旅遊助理，專門協助查詢機票資訊。"
                    "請一律使用繁體中文（台灣用語）回覆，不要使用簡體字。"
                    "如果使用者想追蹤機票價格，請告訴他輸入「追蹤 出發地IATA 目的地IATA」，"
                    "例如「追蹤 TPE TYO」。輸入「說明」可以看完整功能介紹。"
                )
            },
        )
        return resp.text
    except Exception as e:
        app.logger.error("Gemini error: %s", e)
        return "抱歉，我現在無法回應，請稍後再試。"

# ── LINE Webhook ──────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "LINE bot is running.", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        app.logger.error("Webhook error: %s", e)
        abort(500)
    return "OK", 200

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id  = event.source.user_id
    user_text = event.message.text.strip()

    reply = handle_command(user_id, user_text)
    if reply is None:
        reply = ask_gemini(user_text)

    if len(reply) > 4900:
        reply = reply[:4900] + "…（內容過長已截斷）"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )

# ── 啟動 ──────────────────────────────────────────────────
init_db()
threading.Thread(target=price_check_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
