# rest.py

import os, requests, base64
from pymongo import MongoClient
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI")

# MongoDB setup
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

# Global cache for user ID
CACHED_USER_ID = None

def get_valid_token(return_full_response=False):
    """Get a valid access token and user ID, refreshing if necessary."""
    global CACHED_USER_ID
    token_info = get_tokens()
    if not token_info:
        print("No token found in DB.")
        return None

    if not is_expired(token_info["updated_at"]):
        # Fetch and cache user_id if not already cached
        if not CACHED_USER_ID:
            headers = {"Authorization": f"Bearer {token_info['access_token']}"}
            try:
                response = requests.get("https://api.twitter.com/2/users/me", headers=headers)
                if response.status_code == 200:
                    user_id = response.json().get('data', {}).get('id')
                    if user_id:
                        CACHED_USER_ID = user_id
                        print(f"User ID cached: {user_id}")
                else:
                    print(f"Failed to fetch user ID: {response.status_code} - {response.text}")
            except Exception as e:
                print(f"An exception occurred while fetching user ID: {e}")

        # Attach user_id to the token info
        if CACHED_USER_ID:
            token_info['user_id'] = CACHED_USER_ID

        if return_full_response:
            return token_info
        return token_info.get('access_token')

    # If expired, refresh
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()

    headers = {
        "Authorization": f"Basic {b64_auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "refresh_token",
        "refresh_token": token_info["refresh_token"],
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI
    }

    response = requests.post("https://api.twitter.com/2/oauth2/token", headers=headers, data=data)

    if response.status_code != 200:
        raise Exception(f"Failed to refresh token: {response.text}")

    result = response.json()
    save_tokens(result["access_token"], result["refresh_token"])
    
    # After refreshing, get the full document again to return it
    refreshed_tokens = get_tokens()
    return refreshed_tokens if return_full_response else refreshed_tokens["access_token"]
