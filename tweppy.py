import requests
import tweepy
import logging
import os
from rest import get_valid_token
import random
import time
from datetime import datetime, timezone

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Twitter V1.1 credentials
V1_CONSUMER_KEY = "RfsHYTncI0jGXtGoieKFWrcG8"
V1_CONSUMER_SECRET = "Fbv8gsmqbK59HP5FThVHLf9CiCn3uJ0dPyKqnesCxPg3JmALJD"
V1_ACCESS_TOKEN = "1880287416914964486-jdSezCKfXdWl3HLldaC6Y21Rz8IAQn"
V1_ACCESS_SECRET = "qqLUvu7tComDXzUaR1DLiDvOyp3MrQBJm48e6KJyoz5iY"

# Twitter V2 Bearer + OAuth2.0 access token
V2_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAAOtU2AEAAAAAEAa0zCTAsNPub8qDeG1uQXggT8U%3DkRMYY9BDvmHM8h1HuS3QvU6HfkeC3wRXhJVkiEhzOJyvYFJSwc"
V2_ACCESS_TOKEN = "NHFxME5yeFV0MERjUVpZcE12UkZsaWlYZjJPRkxQdUlJcGJJMmJVM0dOTE15OjE3NDg1MTI1OTM3MDY6MTowOmF0OjE"

# Waifu API
WAIFU_API_URL = "https://api.waifu.pics/nsfw/waifu"
IMAGE_PATH = "waifu.jpg"

# Captions
CAPTIONS = [
    "chundai ❤", 
    "good morning ☀️", 
    "watta 😍", 
    "just wow ✨", 
    "mood rn 💫", 
    "for you 🫵", 
    "don't scroll 🚫", 
    "hello sunshine 🌸"
]

# Upload image via v1.1
def upload_media_v1(api):
    logger.info("Uploading media with v1.1 API...")
    media = api.media_upload(IMAGE_PATH)
    logger.info("Media uploaded. Media ID: %s", media.media_id_string)
    return media.media_id_string

# Post tweet via v2
def post_tweet_v2(media_id, caption):
    logger.info("Posting tweet with v2 API...")
    try:
        token = get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "text": caption,
            "media": {
                "media_ids": [media_id]
            }
        }
        res = requests.post("https://api.twitter.com/2/tweets", headers=headers, json=payload)
        if res.status_code == 201 or res.status_code == 200:
            logger.info("Tweet posted successfully.")
        else:
            logger.error(f"Failed to post tweet: {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(f"Token or tweet error: {e}")


# Download image
def fetch_and_save_image():
    logger.info("Fetching image from waifu.pics...")
    res = requests.get(WAIFU_API_URL)
    res.raise_for_status()
    image_url = res.json()["url"]
    img_data = requests.get(image_url).content
    with open(IMAGE_PATH, "wb") as handler:
        handler.write(img_data)
    logger.info("Image downloaded.")

# Full post flow
def post_random_waifu(api_v1):
    try:
        fetch_and_save_image()
        media_id = upload_media_v1(api_v1)
        caption = random.choice(CAPTIONS)
        post_tweet_v2(media_id, caption)
    except Exception as e:
        logger.error(f"Error during posting: {e}")
    finally:
        if os.path.exists(IMAGE_PATH):
            os.remove(IMAGE_PATH)

# Main setup and scheduling
def main():
    # Authenticate with v1.1
    auth = tweepy.OAuth1UserHandler(
        V1_CONSUMER_KEY, V1_CONSUMER_SECRET,
        V1_ACCESS_TOKEN, V1_ACCESS_SECRET
    )
    api_v1 = tweepy.API(auth)
    
    # Post immediately after starting
    logger.info("Posting first tweet on startup...")
    post_random_waifu(api_v1)

    # Schedule 8 times a day (every 3 hours)
    while True:
        for i in range(8):
            logger.info(f"Scheduled post {i+1}/8 — {datetime.now(timezone.utc)}")
            post_random_waifu(api_v1)
            time.sleep(60 * 60 * 3)  # sleep for 3 hours

if __name__ == "__main__":
    main()
