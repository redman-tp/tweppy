import requests
import logging
import os
from rest import get_valid_token
import random
import time
from datetime import datetime, timezone

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Captions to be used as tweets
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

# Post tweet via v2
def post_tweet(caption):
    logger.info("Posting tweet with v2 API...")
    try:
        token = get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "text": caption
        }
        res = requests.post("https://api.twitter.com/2/tweets", headers=headers, json=payload)
        if res.status_code == 201:
            logger.info("Tweet posted successfully.")
        else:
            logger.error(f"Failed to post tweet: {res.status_code} - {res.text}")
    except Exception as e:
        logger.error(f"Token or tweet error: {e}")

# Full post flow
def post_random_text():
    try:
        caption = random.choice(CAPTIONS)
        post_tweet(caption)
    except Exception as e:
        logger.error(f"Error during posting: {e}")

def main():
    # Post immediately after starting
    logger.info("Posting first tweet on startup...")
    post_random_text()

    # Schedule to post every 2 hours
    while True:
        # Sleep for 2 hours before the next post
        time.sleep(60 * 60 * 2)
        logger.info(f"Scheduled post — {datetime.now(timezone.utc)}")
        post_random_text()

if __name__ == "__main__":
    main()
