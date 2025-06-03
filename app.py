import os
from flask import Flask, request, jsonify
import requests
import asyncio # For asynchronous operations like sleep
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# --- Flask Application Initialization ---
app = Flask(__name__)

# --- Configuration (HARDCODED as per request) ---
# WARNING: Hardcoding sensitive information like API keys and secrets directly
# into your source code is a significant security risk and is NOT recommended
# for production environments. Use environment variables or a secrets management
# service for better security practices.

INFOBIP_API_BASE_URL = "https://g9ln56.api.infobip.com"
INFOBIP_API_KEY = "c82c0a67289498dd25b2395b4fab7a65-13749d80-dbfa-4899-bb11-0229ce8ca0a6"
INFOBIP_WHATSAPP_SENDER_NUMBER = "447860099299"

DIRECT_LINE_BASE_URL = "https://directline.botframework.com/v3/directline"
DIRECT_LINE_SECRET = "4bEHl4WbbsPZnu4Tq3APzAfGbKMVBM2uUEDw2dXyzZ4MDTZSPc03JQQJ99BEAC77bzfAArohAAABAZBS0118.CebHBnxyeBs63IDEs2dowO7Acu8IALe5lc19M3zLy8s26aLWNuhpJQQJ99BEAC77bzfAArohAAABAZBS3Qhc"

AZURE_SPEECH_KEY = "40K84vS2b0E637v9J0qtz4MEpA7bsjaoRBg9DjQY9A3wjcptJ9o1JQQJ99BCACYeBjFXJ3w3AAAYACOG2sOr"
AZURE_SPEECH_REGION = "eastus"

DB_NAME = "SmartcardApp"
MONGO_DB_URI = "mongodb+srv://darshanmagdum:tzj7SxsKHeZoqc14@whatsappbot-cluster.2wgzguz.mongodb.net/SmartcardApp?retryWrites=true&w=majority&appName=WhatsappBOT-CLUSTER"

# Store conversations (in-memory, consider persistent storage like Redis for production)
conversations = {}

# --- MongoDB Connection ---
mongo_client = None
try:
    mongo_client = MongoClient(MONGO_DB_URI)
    mongo_client.admin.command('ping') # The ping command is cheap and does not require auth.
    print("✅ MongoDB connected successfully!")
    db = mongo_client.get_database(DB_NAME) # Get the specified database
    users_collection = db.users # Get the 'users' collection

    # Ensure unique index on smartcardNumber (similar to mongoose unique: true)
    if 'smartcardNumber_1' not in users_collection.index_information():
        users_collection.create_index("smartcardNumber", unique=True)
        print("Created unique index for smartcardNumber.")

except ConnectionFailure as e:
    print(f"❌ MongoDB connection error: {e}")
    mongo_client = None # Set to None to indicate connection failure
except Exception as e:
    print(f"❌ An unexpected error occurred during MongoDB connection: {e}")
    mongo_client = None


# In-memory set to track verified users (for current request session,
# for persistence across restarts/scaling, integrate into User model/DB)
verified_users_session = set()

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
        response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
        print(f"Infobip WhatsApp message sent successfully: {response.json()}")
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error sending Infobip WhatsApp message: {e.response.text if e.response else e}")
        raise # Re-raise to be caught by the main webhook handler

# --- Voice Message Handler (Adapted for Infobip's media URL) ---
async def handle_voice_message(media_url):
    try:
        # Step 1: Download audio binary from the Infobip-provided URL
        # Infobip might require authentication for media downloads, check your Infobip docs.
        audio_res = requests.get(
            media_url,
            headers={"Authorization": f"App {INFOBIP_API_KEY}"}, # Assuming Infobip requires this
            stream=True # Use stream for efficiency
        )
        audio_res.raise_for_status()

        # Step 2: Send audio to Azure Speech-to-Text
        azure_stt_url = f"https://{AZURE_SPEECH_REGION}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1?language=en-US"
        azure_headers = {
            'Ocp-Apim-Subscription-Key': AZURE_SPEECH_KEY,
            'Content-Type': 'audio/ogg; codecs=opus', # WhatsApp audio is typically OGG Opus
            'Transfer-Encoding': 'chunked', # Indicate that the body is sent in chunks
        }

        # Send audio in chunks
        audio_data_chunks = []
        for chunk in audio_res.iter_content(chunk_size=8192):
            audio_data_chunks.append(chunk)

        azure_res = requests.post(azure_stt_url, data=b''.join(audio_data_chunks), headers=azure_headers)
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

# --- Infobip Webhook Endpoint ---
# This endpoint will receive incoming messages from Infobip
@app.route("/infobip-whatsapp-webhook", methods=["POST"])
async def handle_infobip_webhook():
    try:
        infobip_data = request.json
        print(f"Received Infobip webhook payload: {infobip_data}")

        if not infobip_data or 'results' not in infobip_data or not infobip_data['results']:
            print("Invalid Infobip payload: 'results' array missing or empty.")
            return jsonify({"status": "error", "message": "Invalid payload"}), 200

        for result in infobip_data['results']:
            from_number = result.get('from') # The sender's WhatsApp number (e.g., '919876543210')
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

            # --- Integrate with Direct Line/Your Copilot Bot ---
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
                print(f"New conversation started for {from_number}: {conv_data['conversationId']}")

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

            # Retrieve bot's reply by polling
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
                    # Filter for bot messages (not from 'user' and type 'message')
                    bot_messages = [a for a in data["activities"] if a.get("from", {}).get("id") != "user" and a.get("type") == "message"]
                    if bot_messages:
                        # Concatenate text from all bot messages
                        bot_reply_text = " ".join([msg.get("text", "") for msg in bot_messages if msg.get("text")])
                        
                if not bot_reply_text:
                    await asyncio.sleep(1) # Wait 1 second before retrying
                    retries += 1

            bot_reply_text = bot_reply_text or "Sorry, I didn’t get that from the bot."
            print(f"Bot responded: {bot_reply_text}")

            # ✅ Send reply to WhatsApp using Infobip API
            # Ensure the 'from_number' is in E.164 format (e.g., +919876543210)
            # Infobip's webhook usually sends numbers without '+', so prepend it.
            formatted_to_number = from_number
            if not formatted_to_number.startswith('+'):
                formatted_to_number = '+' + formatted_to_number
            await send_whatsapp_message_infobip(formatted_to_number, bot_reply_text)

        return jsonify({"status": "success", "message": "Messages processed"}), 200

    except requests.exceptions.RequestException as e:
        print(f"API Request Error in webhook: {e.response.text if e.response else e}")
        return jsonify({"status": "error", "message": f"API request failed: {str(e)}"}), 500
    except Exception as e:
        print(f"An unexpected error occurred in webhook: {e}")
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500

# --- Smartcard verification APIs ---

# Step 1: Verify Smartcard Number
@app.route('/verify-smartcard', methods=['POST'])
async def verify_smartcard():
    if not mongo_client:
        return jsonify({"message": "Database not connected", "validation": False}), 500
    
    data = request.json
    smartcard_number = data.get('smartcardNumber')
    
    if not smartcard_number:
        return jsonify({"message": "Smartcard number is required.", "validation": False}), 400

    user = users_collection.find_one({"smartcardNumber": smartcard_number})

    if user:
        return jsonify({
            "message": 'Smartcard verified. Please enter your mobile number.',
            "validation": True
        })
    else:
        return jsonify({
            "message": 'Smartcard is invalid.',
            "validation": False
        })

# Step 2: Verify Mobile Number
@app.route('/verify-mobile', methods=['POST'])
async def verify_mobile():
    if not mongo_client:
        return jsonify({"message": "Database not connected", "validation": False}), 500

    data = request.json
    smartcard_number = data.get('smartcardNumber')
    mobile_number = data.get('mobileNumber')

    if not smartcard_number or not mobile_number:
        return jsonify({"message": "Smartcard number and mobile number are required.", "validation": False}), 400

    user = users_collection.find_one({"smartcardNumber": smartcard_number})

    if user and user.get('mobile') == mobile_number:
        # In a real application, you might set a session token or update a 'verified' status in DB
        verified_users_session.add(smartcard_number) # For in-memory session tracking
        return jsonify({
            "message": 'Verification successful.',
            "name": user.get('name'),
            "smartcardNumber": smartcard_number,
            "mobileNumber": mobile_number,
            "validation": True
        })
    else:
        return jsonify({
            "message": 'The provided mobile number does not match our records.',
            "validation": False
        })

# Usecase 1: Add Movie
@app.route('/add-movie', methods=['POST'])
async def add_movie():
    if not mongo_client:
        return jsonify({"message": "Database not connected", "validation": False}), 500

    data = request.json
    smartcard_number = data.get('smartcardNumber')
    movie_name = data.get('movieName')

    if not smartcard_number or not movie_name:
        return jsonify({"message": "Smartcard number and movie name are required.", "validation": False}), 400

    user = users_collection.find_one({"smartcardNumber": smartcard_number})

    if not user:
        return jsonify({
            "message": 'User not found in the system.',
            "validation": False
        })

    # Update movies array
    users_collection.update_one(
        {"smartcardNumber": smartcard_number},
        {"$addToSet": {"movies": movie_name}} # $addToSet prevents duplicate movie names
    )
    
    return jsonify({
        "message": f"Movie '{movie_name}' added successfully.",
        "movieName": movie_name,
        "validation": True
    })

# Usecase 2: Add Top-Up Balance
@app.route('/add-balance', methods=['POST'])
async def add_balance():
    if not mongo_client:
        return jsonify({"message": "Database not connected", "validation": False}), 500

    data = request.json
    smartcard_number = data.get('smartcardNumber')
    amount = data.get('amount')

    if not smartcard_number or not isinstance(amount, (int, float)):
        return jsonify({"message": "Smartcard number and valid amount are required.", "validation": False}), 400

    user = users_collection.find_one({"smartcardNumber": smartcard_number})

    if not user:
        return jsonify({
            "message": 'User not found in the system.',
            "validation": False
        })

    # Increment balance
    users_collection.update_one(
        {"smartcardNumber": smartcard_number},
        {"$inc": {"balance": amount}}
    )
    
    # Fetch updated user to return latest balance
    updated_user = users_collection.find_one({"smartcardNumber": smartcard_number})

    return jsonify({
        "message": f"₹{amount} added successfully.",
        "totalBalance": updated_user.get('balance', 0),
        "validation": True
    })

# Usecase 3: Get Balance and Movies
@app.route('/get-balance', methods=['GET'])
async def get_balance():
    if not mongo_client:
        return jsonify({"message": "Database not connected", "validation": False}), 500

    smartcard_number = request.args.get('smartcardNumber')

    if not smartcard_number:
        return jsonify({"message": "Smartcard number is required.", "validation": False}), 400

    user = users_collection.find_one({"smartcardNumber": smartcard_number})

    if not user:
        return jsonify({
            "message": 'User not found in the system.',
            "validation": False
        })

    return jsonify({
        "balance": user.get('balance', 0),
        "movies": user.get('movies', []),
        "validation": True
    })

# --- Start Server ---
if __name__ == '__main__':
    PORT = int(os.getenv('PORT', 3000)) # Use PORT from environment if available, else 3000
    # In production, use a WSGI server like Gunicorn
    print(f"Server running on port {PORT}")
    app.run(host='0.0.0.0', port=PORT, debug=True) # debug=True for development only
