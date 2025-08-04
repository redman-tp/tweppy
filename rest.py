# rest.py

import os, requests, base64
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI")

# Quick MongoDB connection (non-blocking)
try:
    client = MongoClient(
        MONGO_URI,
        serverSelectionTimeoutMS=2000,  # Very short timeout
        connectTimeoutMS=2000,
        socketTimeoutMS=2000
    )
    # Quick ping test
    client.admin.command('ping')
except Exception as e:
    print(f"MongoDB connection error in rest.py: {str(e)[:50]}...")
    # Fallback to basic connection
    client = MongoClient(MONGO_URI)

db = client["twitter"]
collection = db["tokens"]

# Twitter credentials
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

def get_tokens():
    return collection.find_one({"_id": "twitter_token"})

def save_tokens(access_token, refresh_token):
    collection.update_one(
        {"_id": "twitter_token"},
        {
            "$set": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "updated_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )

def is_expired(updated_at):
    now = datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        print("⚠️ updated_at is naive. Making it aware.")
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    else:
        print("✅ updated_at is already timezone-aware.")
    print(f"Now: {now}, Updated At: {updated_at}")
    return now - updated_at > timedelta(minutes=115)

def refresh_token_if_needed():
    tokens = get_tokens()
    if not tokens:
        raise Exception("No token found in DB.")

    if not is_expired(tokens["updated_at"]):
        return tokens["access_token"]

    # If expired, refresh
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI
    }

    response = requests.post("https://api.twitter.com/2/oauth2/token", headers=headers, data=data)

    if response.status_code != 200:
        raise Exception(f"Failed to refresh token: {response.text}")

    result = response.json()
    save_tokens(result["access_token"], result["refresh_token"])
    return result["access_token"]

# Only call this in your main script when needed
def get_valid_token():
    return refresh_token_if_needed()
