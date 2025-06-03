import os
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
import asyncio # For the async sleep utility

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# --- Configuration (loaded from .env) ---
INFOBIP_API_BASE_URL = os.getenv("INFOBIP_API_BASE_URL")
INFOBIP_API_KEY = os.getenv("INFOBIP_API_KEY")
INFOBIP_WHATSAPP_SENDER_NUMBER = os.getenv("INFOBIP_WHATSAPP_SENDER_NUMBER")

DIRECT_LINE_BASE_URL = os.getenv("DIRECT_LINE_BASE_URL")
DIRECT_LINE_SECRET = os.getenv("DIRECT_LINE_SECRET")

AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "eastus")

conversations = {}

# --- Utility Function to Send WhatsApp Message via Infobip ---
async def send_whatsapp_message_infobip(to_number, message_text):
    payload = {
        "messages": [
            {
                "from": INFOBIP_WHATSAPP_SENDER_NUMBER,
                "to": to_number,
                "message": {
                    "text": message_text
                }
            }
        ]
    }
    headers = {
        "Authorization": f"App {INFOBIP_API_KEY}",
        "Content-Type": "application/json"
    }
    url = f"{INFOBIP_API_BASE_URL}/whatsapp/1/message/text"
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        print(f"Infobip WhatsApp message sent successfully: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Error sending Infobip WhatsApp message: {e.response.text if e.response else e}")

# --- Voice Message Handler (Adapted for Infobip's media URL) ---
async def handle_voice_message(media_url):
    try:
        # Step 1: Download audio binary from the Infobip-provided URL
        audio_res = requests.get(
            media_url,
            headers={"Authorization": f"App {INFOBIP_API_KEY}"}, # Infobip might require auth for media download
            stream=True # Use stream for efficiency
        )
        audio_res.raise_for_status()

        # Step 2: Send audio to Azure Speech-to-Text
        azure_stt_url = f"https://{AZURE_SPEECH_REGION}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1?language=en-US"
        azure_headers = {
            'Ocp-Apim-Subscription-Key': AZURE_SPEECH_KEY,
            'Content-Type': 'audio/ogg; codecs=opus', # Assuming OGG Opus from WhatsApp/Infobip
            'Transfer-Encoding': 'chunked',
        }

        azure_res = requests.post(azure_stt_url, data=audio_res.iter_content(chunk_size=8192), headers=azure_headers)
        azure_res.raise_for_status()

        transcribed_text = azure_res.json().get('DisplayText')
        if not transcribed_text:
            raise ValueError("No DisplayText found in Azure STT response.")

        print(f"Transcribed: {transcribed_text}")
        return transcribed_text

    except requests.exceptions.RequestException as e:
        print(f"Error in handle_voice_message (requests): {e.response.text if e.response else e}")
        return "Sorry, I had trouble processing your voice message."
    except Exception as e:
        print(f"Error in handle_voice_message: {e}")
        return "Sorry, I had trouble processing your voice message."


## Infobip Webhook Endpoint

# This is the core of your integration, handling incoming WhatsApp messages from Infobip, sending them to your Direct Line bot, and returning the bot's response.

# ```python
@app.route('/infobip-whatsapp-webhook', methods=['POST'])
async def handle_infobip_webhook():
    try:
        infobip_data = request.json
        print(f"Received Infobip webhook payload: {infobip_data}")

        if not infobip_data or 'results' not in infobip_data or not infobip_data['results']:
            print("Invalid Infobip payload: 'results' array missing or empty.")
            return jsonify({"status": "error", "message": "Invalid payload"}), 200

        # Process each message in the results array
        for result in infobip_data['results']:
            from_number = result.get('from')
            message_content = result.get('message')

            if not from_number or not message_content or 'type' not in message_content:
                print("Skipping malformed message in Infobip payload.")
                continue

            message_type = message_content['type']
            user_message_text = None

            if message_type == 'TEXT':
                user_message_text = message_content.get('text')
            elif message_type == 'AUDIO':
                media_url = message_content.get('url') # Infobip provides the URL directly
                if media_url:
                    user_message_text = await handle_voice_message(media_url)
                else:
                    print("Audio message without a direct URL from Infobip.")
                    user_message_text = "Received an audio message, but couldn't get its URL."
            else:
                print(f"Unsupported message type: {message_type}")
                user_message_text = f"Received unsupported message type: {message_type}. Please send a text or voice message."

            if not user_message_text:
                print("No valid user message text extracted from Infobip payload.")
                continue

            print(f"User {from_number} sent: {user_message_text}")

            # --- Integrate with Direct Line/Your Bot ---
            # Start or resume Direct Line conversation
            if from_number not in conversations:
                conv_res = requests.post(f"{DIRECT_LINE_BASE_URL}/conversations",
                    headers={
                        "Authorization": f"Bearer {DIRECT_LINE_SECRET}",
                        "Content-Type": "application/json"
                    })
                conv_res.raise_for_status()
                conv_data = conv_res.json()
                conversations[from_number] = {"conversationId": conv_data["conversationId"]}

            # Send message to bot
            send_message_res = requests.post(f"{DIRECT_LINE_BASE_URL}/conversations/{conversations[from_number]['conversationId']}/activities",
                headers={
                    "Authorization": f"Bearer {DIRECT_LINE_SECRET}",
                    "Content-Type": "application/json"
                },
                json={
                    "type": "message",
                    "from": {"id": "user"},
                    "text": user_message_text
                })
            send_message_res.raise_for_status()

            # Retrieve bot's reply
            reply_data = send_message_res.json()
            full_id = reply_data.get("id")
            watermark = full_id.split("|")[1] if full_id and "|" in full_id else None
            bot_reply_text = None
            retries = 0

            while not bot_reply_text and retries < 10:
                url = f"{DIRECT_LINE_BASE_URL}/conversations/{conversations[from_number]['conversationId']}/activities{f'?watermark={watermark}' if watermark else ''}"
                
                try:
                    response = requests.get(url, headers={"Authorization": f"Bearer {DIRECT_LINE_SECRET}"})
                    response.raise_for_status()
                    data = response.json()
                except requests.exceptions.RequestException as err:
                    print(f"Error fetching activities from Direct Line: {err.response.text if err.response else err}")
                    break

                watermark = data.get("watermark")

                if data.get("activities"):
                    bot_messages = [a for a in data["activities"] if a.get("from", {}).get("id") != "user" and a.get("type") == "message"]
                    if bot_messages:
                        bot_reply_text = " ".join([msg.get("text", "") for msg in bot_messages])
                        
                if not bot_reply_text:
                    await asyncio.sleep(1) # Wait 1 second
                    retries += 1

            bot_reply_text = bot_reply_text or "Sorry, I didn’t get that from the bot."
            print(f"Bot responded: {bot_reply_text}")

            # ✅ Send reply to WhatsApp using Infobip API
            await send_whatsapp_message_infobip(from_number, bot_reply_text)

        return jsonify({"status": "success", "message": "Messages processed"}), 200

    except requests.exceptions.RequestException as e:
        print(f"API Request Error: {e.response.text if e.response else e}")
        return jsonify({"status": "error", "message": f"API request failed: {str(e)}"}), 500
    except Exception as e:
        print(f"An unexpected error occurred in webhook: {e}")
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500

if __name__ == '__main__':
    # This block is for local development only.
    # In production, Gunicorn will handle starting the app.
    print("Starting Flask app for local development...")
    app.run(host='0.0.0.0', port=os.getenv('PORT', 5000), debug=True)