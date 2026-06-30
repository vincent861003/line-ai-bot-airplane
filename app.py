import os
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google import genai

app = Flask(__name__)

# 從環境變數讀取金鑰，不要寫死在程式碼裡
CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("請在 Railway 環境變數設定 LINE_CHANNEL_ACCESS_TOKEN 和 LINE_CHANNEL_SECRET")

if not GEMINI_API_KEY:
    raise RuntimeError("請在 Railway 環境變數設定 GEMINI_API_KEY")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


def ask_gemini(user_text: str) -> str:
    """把使用者的訊息送給 Gemini，回傳生成的文字。"""
    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=user_text,
        )
        return response.text
    except Exception as e:
        app.logger.error("Gemini API error: %s", e)
        return "抱歉，我現在無法回應，請稍後再試一次。"


@app.route("/", methods=["GET"])
def home():
    return "LINE bot is running.", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    app.logger.info("Request body: %s", body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature. Channel secret 可能設錯了。")
        abort(400)
    except Exception as e:
        app.logger.error("Webhook handling error: %s", e)
        abort(500)

    return "OK", 200


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text
    reply_text = ask_gemini(user_text)

    # LINE 單則訊息上限 5000 字，保險起見截斷
    if len(reply_text) > 4900:
        reply_text = reply_text[:4900] + "…（內容過長已截斷）"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
    
