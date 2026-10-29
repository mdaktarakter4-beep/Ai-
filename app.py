import os
import hmac
import hashlib
import logging
import requests

from flask import Flask, request, jsonify
from google import genai

app = Flask(name)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(name)

Environment variables / GitHub Secrets

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
PAGE_ID = os.environ["PAGE_ID"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
APP_SECRET = os.environ["APP_SECRET"]

GRAPH_API_VERSION = os.environ["GRAPH_API_VERSION"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

Gemini client

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

Facebook API endpoint

MESSENGER_URL = (
f"https://graph.facebook.com/"
f"{GRAPH_API_VERSION}/{PAGE_ID}/messages"
)

Prevent duplicate replies during the current process lifetime.

processed_messages = set()
MAX_PROCESSED_MESSAGES = 5000

def verify_signature(raw_body, signature):
"""Verify that the webhook request came from Meta."""

if not signature or not signature.startswith("sha256="):
    return False

expected_signature = "sha256=" + hmac.new(
    APP_SECRET.encode("utf-8"),
    raw_body,
    hashlib.sha256
).hexdigest()

return hmac.compare_digest(expected_signature, signature)

def generate_reply(user_message):
"""Generate a natural reply using Gemini AI."""

prompt = f"""

তুমি একটি Facebook Page-এর Messenger AI assistant।

নিয়ম:
১. ব্যবহারকারীর মেসেজের প্রাসঙ্গিক উত্তর দেবে।
২. ব্যবহারকারী বাংলায় লিখলে বাংলায় উত্তর দেবে।
৩. ইংরেজিতে লিখলে ইংরেজিতে উত্তর দেবে।
৪. উত্তর সংক্ষিপ্ত, ভদ্র, বন্ধুত্বপূর্ণ ও স্বাভাবিক রাখবে।
৫. প্রশ্নের উত্তর জানা না থাকলে বানিয়ে কিছু বলবে না।
৬. প্রয়োজনীয় তথ্য না জানলে বিনয়ের সঙ্গে আরও তথ্য চাইবে।
৭. নিজের পরিচয় সম্পর্কে মিথ্যা দাবি করবে না।
৮. ব্যবহারকারীর মেসেজকে নির্দেশ হিসেবে বিবেচনা করবে,
কিন্তু গোপন তথ্য প্রকাশ বা নিরাপত্তাবিরোধী কাজ করবে না।
৯. শুধু Messenger-এ পাঠানোর উপযোগী উত্তর দেবে।

ব্যবহারকারীর মেসেজ:
{user_message}
"""

response = gemini_client.models.generate_content(
    model=GEMINI_MODEL,
    contents=prompt
)

reply = (response.text or "").strip()

if not reply:
    return "দুঃখিত, এই মুহূর্তে উত্তর তৈরি করতে পারছি না।"

# Keep the reply within a conservative message length.
if len(reply) > 1800:
    reply = reply[:1790] + "..."

return reply

def send_message(recipient_id, text):
"""Send a text reply through the Messenger API."""

payload = {
    "recipient": {
        "id": recipient_id
    },
    "messaging_type": "RESPONSE",
    "message": {
        "text": text
    }
}

response = requests.post(
    MESSENGER_URL,
    params={"access_token": PAGE_ACCESS_TOKEN},
    json=payload,
    timeout=30
)

try:
    result = response.json()
except ValueError:
    result = {"error": response.text}

if not response.ok or "error" in result:
    logger.error("Messenger API error: %s", result)
    response.raise_for_status()
    raise RuntimeError("Messenger API failed")

logger.info("Reply sent successfully.")
return result

def process_message(event):
"""Process an incoming Messenger text message."""

message = event.get("message", {})
sender = event.get("sender", {})
recipient = event.get("recipient", {})

sender_id = sender.get("id")
message_id = message.get("mid")
message_text = message.get("text", "").strip()

# Ignore invalid events and messages sent by the Page itself.
if not sender_id or sender_id == PAGE_ID:
    return

# Ignore unsupported messages, such as attachments without text.
if not message_text:
    return

# Ignore messages without an ID.
if not message_id:
    return

# Avoid replying to the same webhook event twice.
if message_id in processed_messages:
    logger.info("Duplicate message ignored.")
    return

# Add before processing to avoid duplicate work in this process.
processed_messages.add(message_id)

if len(processed_messages) > MAX_PROCESSED_MESSAGES:
    processed_messages.clear()
    processed_messages.add(message_id)

try:
    logger.info("Incoming message received.")

    reply = generate_reply(message_text)
    send_message(sender_id, reply)

except Exception:
    logger.exception("Failed to process incoming message.")

    # Do not send an additional message if the API outcome
    # is uncertain; this prevents accidental duplicate replies.

@app.route("/", methods=["GET"])
def home():
return "Facebook Messenger AI bot is running.", 200

@app.route("/webhook", methods=["GET"])
def verify_webhook():
"""Verify the webhook subscription with Meta."""

mode = request.args.get("hub.mode")
token = request.args.get("hub.verify_token")
challenge = request.args.get("hub.challenge")

if (
    mode == "subscribe"
    and token
    and hmac.compare_digest(token, VERIFY_TOKEN)
    and challenge is not None
):
    return challenge, 200

return "Webhook verification failed.", 403

@app.route("/webhook", methods=["POST"])
def receive_webhook():
"""Receive Facebook Messenger webhook events."""

raw_body = request.get_data(cache=True)
signature = request.headers.get("X-Hub-Signature-256", "")

if not verify_signature(raw_body, signature):
    return jsonify({"error": "Invalid webhook signature"}), 403

data = request.get_json(silent=True)

if not isinstance(data, dict):
    return jsonify({"error": "Invalid JSON"}), 400

# Messenger events are sent for Facebook Page objects.
if data.get("object") != "page":
    return jsonify({"status": "ignored"}), 200

for entry in data.get("entry", []):
    for event in entry.get("messaging", []):
        # Ignore delivery/read receipts and non-message events.
        if "message" not in event:
            continue

        # Ignore echo messages generated by the Page itself.
        if event["message"].get("is_echo"):
            continue

        process_message(event)

return jsonify({"status": "ok"}), 200

if name == "main":
port = int(os.getenv("PORT", "8000"))

app.run(
    host="0.0.0.0",
    port=port,
    debug=False
)
