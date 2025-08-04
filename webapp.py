#!/usr/bin/env python3
"""
Twitter Bot Web Frontend
Simple interface to manage tweet queue
"""
import os
import re
import requests
import base64
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from dotenv import load_dotenv
from rest import get_valid_token, save_tokens
from datetime import datetime, timezone
import threading
import time
import random

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Twitter credentials
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

# Twitter API endpoints
AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"

# Tweet queue management
from pymongo import MongoClient

MONGO_URI = os.getenv("MONGO_URI")

# Global variables for fallback storage
IN_MEMORY_QUEUE = []
IN_MEMORY_STATS = {"posted_count": 0}
USE_MONGODB = False

# Try multiple MongoDB connection approaches
connection_attempts = [
    # Attempt 1: Basic connection without SSL
    lambda: MongoClient(MONGO_URI.replace('ssl=true', 'ssl=false') if 'ssl=' in MONGO_URI else MONGO_URI + '&ssl=false'),
    # Attempt 2: Connection with minimal SSL
    lambda: MongoClient(MONGO_URI, tls=False, ssl=False),
    # Attempt 3: Original connection string as-is
    lambda: MongoClient(MONGO_URI),
]

client = None
for i, attempt in enumerate(connection_attempts):
    try:
        print(f"Attempting MongoDB connection method {i+1}...")
        test_client = attempt()
        test_client.admin.command('ping')
        client = test_client
        USE_MONGODB = True
        print(f"✅ MongoDB connection successful with method {i+1}")
        break
    except Exception as e:
        print(f"❌ Connection method {i+1} failed: {e}")
        continue

if USE_MONGODB and client:
    db = client["twitter"]
    queue_collection = db["tweet_queue"]
    stats_collection = db["bot_stats"]
    print("✅ Using MongoDB for data storage")
else:
    print("⚠️ MongoDB unavailable - using in-memory storage (data will not persist)")
    queue_collection = None
    stats_collection = None

def parse_tweets_from_input(input_text):
    """Parse tweets from input text, splitting by quotes"""
    # Find all text within quotes (both single and double)
    tweets = re.findall(r'"([^"]*)"', input_text)
    tweets.extend(re.findall(r"'([^']*)'", input_text))
    
    # Clean up tweets - remove empty ones and strip whitespace
    tweets = [tweet.strip() for tweet in tweets if tweet.strip()]
    return tweets

def add_tweets_to_queue(tweets):
    """Add tweets to the queue (MongoDB or in-memory)"""
    if not tweets:
        return 0
    
    if USE_MONGODB and queue_collection:
        tweet_docs = []
        for tweet in tweets:
            tweet_docs.append({
                "text": tweet,
                "created_at": datetime.now(timezone.utc),
                "posted": False
            })
        
        result = queue_collection.insert_many(tweet_docs)
        return len(result.inserted_ids)
    else:
        # Use in-memory storage
        for tweet in tweets:
            IN_MEMORY_QUEUE.append({
                "_id": len(IN_MEMORY_QUEUE),
                "text": tweet,
                "created_at": datetime.now(timezone.utc),
                "posted": False
            })
        return len(tweets)

def get_next_tweet():
    """Get the next unposted tweet from queue"""
    if USE_MONGODB and queue_collection:
        return queue_collection.find_one({"posted": False}, sort=[("created_at", 1)])
    else:
        # Use in-memory storage
        for tweet in IN_MEMORY_QUEUE:
            if not tweet.get("posted", False):
                return tweet
        return None

def mark_tweet_posted(tweet_id):
    """Mark a tweet as posted and remove it from queue"""
    if USE_MONGODB and queue_collection and stats_collection:
        # Remove from queue
        queue_collection.delete_one({"_id": tweet_id})
        
        # Increment posted counter
        stats_collection.update_one(
            {"_id": "posted_count"},
            {"$inc": {"count": 1}},
            upsert=True
        )
    else:
        # Use in-memory storage
        global IN_MEMORY_QUEUE, IN_MEMORY_STATS
        IN_MEMORY_QUEUE = [t for t in IN_MEMORY_QUEUE if t["_id"] != tweet_id]
        IN_MEMORY_STATS["posted_count"] += 1

def get_queue_stats():
    """Get queue statistics"""
    if USE_MONGODB and queue_collection and stats_collection:
        unposted = queue_collection.count_documents({})
        
        # Get posted count from stats collection
        posted_doc = stats_collection.find_one({"_id": "posted_count"})
        posted = posted_doc["count"] if posted_doc else 0
        
        total = unposted + posted
        return {"total": total, "unposted": unposted, "posted": posted}
    else:
        # Use in-memory storage
        unposted = len([t for t in IN_MEMORY_QUEUE if not t.get("posted", False)])
        posted = IN_MEMORY_STATS["posted_count"]
        total = unposted + posted
        return {"total": total, "unposted": unposted, "posted": posted}

def post_tweet(caption):
    """Post tweet using Twitter API v2"""
    try:
        token = get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {"text": caption}
        
        response = requests.post("https://api.twitter.com/2/tweets", headers=headers, json=payload)
        
        if response.status_code == 201:
            return True, "Tweet posted successfully"
        else:
            return False, f"Failed to post tweet: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"Error posting tweet: {str(e)}"

# Web routes
@app.route('/')
def index():
    """Main dashboard"""
    stats = get_queue_stats()
    
    if USE_MONGODB and queue_collection:
        recent_tweets = list(queue_collection.find({}, sort=[("created_at", -1)]).limit(10))
    else:
        # Use in-memory storage
        recent_tweets = sorted(IN_MEMORY_QUEUE, key=lambda x: x["created_at"], reverse=True)[:10]
    
    return render_template('index.html', stats=stats, recent_tweets=recent_tweets, use_mongodb=USE_MONGODB)

@app.route('/add_tweets', methods=['POST'])
def add_tweets():
    """Add tweets to queue from form input"""
    input_text = request.form.get('tweets_input', '').strip()
    
    if not input_text:
        flash('Please enter some tweets in quotes!', 'error')
        return redirect(url_for('index'))
    
    tweets = parse_tweets_from_input(input_text)
    
    if not tweets:
        flash('No tweets found! Make sure to put each tweet in quotes like "Hello world"', 'error')
        return redirect(url_for('index'))
    
    added_count = add_tweets_to_queue(tweets)
    flash(f'Successfully added {added_count} tweets to the queue!', 'success')
    
    return redirect(url_for('index'))

@app.route('/post_next', methods=['POST'])
def post_next():
    """Manually post the next tweet in queue"""
    next_tweet = get_next_tweet()
    
    if not next_tweet:
        flash('No tweets in queue to post!', 'error')
        return redirect(url_for('index'))
    
    success, message = post_tweet(next_tweet['text'])
    
    if success:
        mark_tweet_posted(next_tweet['_id'])
        flash(f'Posted: "{next_tweet["text"]}"', 'success')
    else:
        flash(f'Failed to post tweet: {message}', 'error')
    
    return redirect(url_for('index'))

@app.route('/clear_queue', methods=['POST'])
def clear_queue():
    """Clear all tweets from queue"""
    result = queue_collection.delete_many({})
    flash(f'Cleared {result.deleted_count} tweets from queue', 'info')
    return redirect(url_for('index'))

@app.route('/api/stats')
def api_stats():
    """API endpoint for queue stats"""
    return jsonify(get_queue_stats())

# Auto-posting background thread
posting_active = False

def auto_post_worker():
    """Background worker to automatically post tweets from queue"""
    global posting_active
    
    while posting_active:
        try:
            next_tweet = get_next_tweet()
            
            if next_tweet:
                print(f"Auto-posting: {next_tweet['text']}")
                success, message = post_tweet(next_tweet['text'])
                
                if success:
                    mark_tweet_posted(next_tweet['_id'])
                    print(f"✅ Posted and removed: {next_tweet['text']}")
                else:
                    print(f"❌ Failed to post: {message}")
            else:
                print("No tweets in queue, waiting...")
            
            # Wait 2 hours before next post
            time.sleep(60 * 60 * 2)
            
        except Exception as e:
            print(f"Error in auto-post worker: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

@app.route('/start_auto_posting', methods=['POST'])
def start_auto_posting():
    """Start automatic posting"""
    global posting_active
    
    if not posting_active:
        posting_active = True
        thread = threading.Thread(target=auto_post_worker, daemon=True)
        thread.start()
        flash('Auto-posting started! Will post every 2 hours.', 'success')
    else:
        flash('Auto-posting is already running!', 'info')
    
    return redirect(url_for('index'))

@app.route('/stop_auto_posting', methods=['POST'])
def stop_auto_posting():
    """Stop automatic posting"""
    global posting_active
    posting_active = False
    flash('Auto-posting stopped.', 'info')
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
