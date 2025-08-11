#!/usr/bin/env python3
"""
Twitter Bot Web Frontend
Simple interface to manage tweet queue
"""
import os
import re
import requests
import base64
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from dotenv import load_dotenv
from rest import get_valid_token, save_tokens
from datetime import datetime, timezone, timedelta
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
IN_MEMORY_HISTORY = []
IN_MEMORY_STATS = {"posted_count": 0}
IN_MEMORY_SCHEDULED = []
USE_MONGODB = False

# Try MongoDB connection with SSL error handling
try:
    print("🔄 Attempting MongoDB connection...")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # Test the connection
    client.admin.command('ping')
    db = client["twitter"]
    queue_collection = db["tweet_queue"]
    history_collection = db["post_history"]
    stats_collection = db["bot_stats"]
    scheduled_collection = db["scheduled_posts"]
    USE_MONGODB = True
    print("✅ MongoDB connection successful!")
except Exception as e:
    print(f"❌ MongoDB connection failed: {str(e)[:100]}...")
    print("⚠️  Using in-memory storage - data will not persist after restart")
    client = None
    queue_collection = None
    stats_collection = None
    scheduled_collection = None

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
    
    if USE_MONGODB and queue_collection is not None:
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
    if USE_MONGODB and queue_collection is not None:
        return queue_collection.find_one({"posted": False}, sort=[("created_at", 1)])
    else:
        # Use in-memory storage
        for tweet in IN_MEMORY_QUEUE:
            if not tweet.get("posted", False):
                return tweet
        return None

def mark_tweet_posted(tweet_id, tweet_text):
    """Move a tweet from queue to history and mark as posted."""
    if USE_MONGODB and queue_collection is not None and history_collection is not None:
        # Add to history with a timestamp
        history_collection.insert_one({
            "text": tweet_text,
            "posted_at": datetime.now(timezone.utc)
        })
        # Remove from queue
        queue_collection.delete_one({"_id": tweet_id})
        # Increment posted counter in stats
        if stats_collection is not None:
            stats_collection.update_one(
                {"_id": "posted_count"},
                {"$inc": {"count": 1}},
                upsert=True
            )
    else:
        # Use in-memory storage
        global IN_MEMORY_QUEUE, IN_MEMORY_HISTORY
        IN_MEMORY_HISTORY.append({
            "text": tweet_text,
            "posted_at": datetime.now(timezone.utc)
        })
        IN_MEMORY_QUEUE = [t for t in IN_MEMORY_QUEUE if t["_id"] != tweet_id]
        IN_MEMORY_STATS["posted_count"] = IN_MEMORY_STATS.get("posted_count", 0) + 1

def get_queue_stats():
    """Get queue statistics"""
    if USE_MONGODB and queue_collection is not None and stats_collection is not None:
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

def schedule_tweets(tweets, scheduled_at):
    """Create scheduled posts for the given tweets at the specified datetime (UTC)."""
    if not tweets:
        return 0
    if USE_MONGODB and scheduled_collection is not None:
        docs = [{
            "text": t,
            "scheduled_at": scheduled_at,
            "posted": False,
            "created_at": datetime.now(timezone.utc)
        } for t in tweets]
        res = scheduled_collection.insert_many(docs)
        return len(res.inserted_ids)
    else:
        start_id = len(IN_MEMORY_SCHEDULED)
        for idx, t in enumerate(tweets):
            IN_MEMORY_SCHEDULED.append({
                "_id": start_id + idx,
                "text": t,
                "scheduled_at": scheduled_at,
                "posted": False,
                "created_at": datetime.now(timezone.utc)
            })
        return len(tweets)

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
    stats = get_queue_stats()
    
    # Get all unposted tweets for display
    if USE_MONGODB and queue_collection is not None and history_collection is not None:
        tweets = list(queue_collection.find().sort("created_at", 1))
        history = list(history_collection.find().sort("posted_at", -1).limit(50))
        # Scheduled (unposted only)
        scheduled = list(scheduled_collection.find({"posted": False}).sort("scheduled_at", 1)) if 'scheduled_collection' in globals() and scheduled_collection is not None else []
    else:
        tweets = [t for t in IN_MEMORY_QUEUE if not t.get("posted", False)]
        history = sorted(IN_MEMORY_HISTORY, key=lambda x: x['posted_at'], reverse=True)[:50]
        scheduled = [s for s in IN_MEMORY_SCHEDULED if not s.get('posted')]

    # Compute WAT display strings
    WAT = timezone(timedelta(hours=1))
    for post in history:
        try:
            post_dt = post.get('posted_at')
            if post_dt and post_dt.tzinfo is not None:
                post['posted_wat_str'] = post_dt.astimezone(WAT).strftime('%b %d, %H:%M WAT')
        except Exception:
            post['posted_wat_str'] = ''

    for item in scheduled:
        try:
            sched_dt = item.get('scheduled_at')
            if sched_dt and sched_dt.tzinfo is not None:
                item['scheduled_wat_str'] = sched_dt.astimezone(WAT).strftime('%b %d, %H:%M WAT')
        except Exception:
            item['scheduled_wat_str'] = ''

    return render_template('index.html', 
                         stats=stats, 
                         tweets=tweets,
                         history=history,
                         scheduled=scheduled,
                         scheduled_count=len(scheduled),
                         use_mongodb=USE_MONGODB)

@app.route('/keepalive')
def keepalive():
    """An endpoint to keep the server alive on free hosting services."""
    return jsonify({"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/add_tweets', methods=['POST'])
def add_tweets():
    # TODO: Add authentication check when OAuth is implemented
    # if not session.get('oauth_token'):
    #     return redirect(url_for('index'))
    action = request.form.get('action')
    tweets_input = request.form.get('tweets_input', '')
    tweet_texts = parse_tweets_from_input(tweets_input)

    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in double quotes.', 'error')
        return redirect(url_for('index'))

    if action == 'post':
        # Post the first tweet immediately
        tweet_to_post = tweet_texts.pop(0)
        success, message = post_tweet(tweet_to_post)
        if success:
            mark_tweet_posted(None, tweet_to_post)  # None for ID as it wasn't in DB
            flash('Tweet posted successfully!', 'success')
        else:
            flash(f'Error posting tweet: {message}', 'error')
            # Add it back to the list to be queued if posting fails
            tweet_texts.insert(0, tweet_to_post)

    # Add remaining tweets (or all if action was 'queue') to the queue
    if action == 'queue':
        if tweet_texts:
            added_count = add_tweets_to_queue(tweet_texts)
            flash(f'{added_count} tweet(s) added to the queue!', 'success')
    elif tweet_texts:  # This handles the case where action is 'post' and there are remaining tweets
        added_count = add_tweets_to_queue(tweet_texts)
        flash(f'First tweet posted, {added_count} remaining tweet(s) added to queue.', 'success')
    else:
        # This case is for when action is 'post' and there was only one tweet.
        # The success message is already flashed inside the 'post' block.
        pass

    return redirect(url_for('index'))
@app.route('/scheduled/delete/<sid>', methods=['POST'])
def delete_scheduled(sid):
    """Cancel a scheduled tweet (delete it)."""
    if USE_MONGODB and 'scheduled_collection' in globals() and scheduled_collection is not None:
        try:
            from bson.objectid import ObjectId
            result = scheduled_collection.delete_one({'_id': ObjectId(sid)})
            if result.deleted_count:
                flash('Scheduled tweet canceled.', 'info')
            else:
                flash('Scheduled tweet not found.', 'error')
        except Exception as e:
            flash(f'Error canceling scheduled tweet: {e}', 'error')
    else:
        global IN_MEMORY_SCHEDULED
        before = len(IN_MEMORY_SCHEDULED)
        IN_MEMORY_SCHEDULED = [it for it in IN_MEMORY_SCHEDULED if str(it.get('_id')) != sid]
        if len(IN_MEMORY_SCHEDULED) < before:
            flash('Scheduled tweet canceled.', 'info')
        else:
            flash('Scheduled tweet not found.', 'error')
    return redirect(url_for('index'))

@app.route('/scheduled/update/<sid>', methods=['POST'])
def update_scheduled(sid):
    """Edit the text and/or scheduled time of a scheduled tweet."""
    new_text = request.form.get('scheduled_text', '').strip()
    new_at_str = request.form.get('scheduled_at', '').strip()
    new_at_utc = request.form.get('scheduled_at_utc', '').strip()

    if not new_text:
        flash('Tweet text cannot be empty.', 'error')
        return redirect(url_for('index'))

    try:
        if new_at_utc:
            parsed = datetime.fromisoformat(new_at_utc.replace('Z', '+00:00'))
            new_dt = parsed.astimezone(timezone.utc)
        elif new_at_str:
            # Fallback; treat as UTC naive
            new_dt = datetime.fromisoformat(new_at_str).replace(tzinfo=timezone.utc)
        else:
            new_dt = None
    except Exception:
        flash('Invalid date/time.', 'error')
        return redirect(url_for('index'))

    if USE_MONGODB and 'scheduled_collection' in globals() and scheduled_collection is not None:
        try:
            from bson.objectid import ObjectId
            update_doc = {'text': new_text}
            if new_dt is not None:
                update_doc['scheduled_at'] = new_dt
            result = scheduled_collection.update_one({'_id': ObjectId(sid)}, {'$set': update_doc})
            if result.matched_count:
                flash('Scheduled tweet updated.', 'success')
            else:
                flash('Scheduled tweet not found.', 'error')
        except Exception as e:
            flash(f'Error updating scheduled tweet: {e}', 'error')
    else:
        for item in IN_MEMORY_SCHEDULED:
            if str(item.get('_id')) == sid:
                item['text'] = new_text
                if new_dt is not None:
                    item['scheduled_at'] = new_dt
                flash('Scheduled tweet updated.', 'success')
                break
        else:
            flash('Scheduled tweet not found.', 'error')
    return redirect(url_for('index'))
    action = request.form.get('action')
    tweets_input = request.form.get('tweets_input', '')
    tweet_texts = parse_tweets_from_input(tweets_input)

    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in double quotes.', 'error')
        return redirect(url_for('index'))

    if action == 'post':
        # Post the first tweet immediately
        tweet_to_post = tweet_texts.pop(0)
        success, message = post_tweet(tweet_to_post)
        if success:
            mark_tweet_posted(None, tweet_to_post)  # None for ID as it wasn't in DB
            flash('Tweet posted successfully!', 'success')
        else:
            flash(f'Error posting tweet: {message}', 'error')
            # Add it back to the list to be queued if posting fails
            tweet_texts.insert(0, tweet_to_post)

    # Add remaining tweets (or all if action was 'queue') to the queue
    if action == 'queue':
        if tweet_texts:
            added_count = add_tweets_to_queue(tweet_texts)
            flash(f'{added_count} tweet(s) added to the queue!', 'success')
    elif tweet_texts:  # This handles the case where action is 'post' and there are remaining tweets
        added_count = add_tweets_to_queue(tweet_texts)
        flash(f'First tweet posted, {added_count} remaining tweet(s) added to queue.', 'success')
    else:
        # This case is for when action is 'post' and there was only one tweet.
        # The success message is already flashed inside the 'post' block.
        pass

    return redirect(url_for('index'))

@app.route('/schedule', methods=['POST'])
def schedule():
    """Schedule a single tweet at a specified datetime (UTC)."""
    scheduled_text = request.form.get('scheduled_text', '').strip()
    scheduled_at_str = request.form.get('scheduled_at', '').strip()
    scheduled_at_utc = request.form.get('scheduled_at_utc', '').strip()

    if not scheduled_text:
        flash('Please enter the tweet text to schedule.', 'error')
        return redirect(url_for('index'))

    if not scheduled_at_str:
        flash('Please select a schedule date/time.', 'error')
        return redirect(url_for('index'))

    try:
        if scheduled_at_utc:
            # Expect ISO 8601 with Z or offset, parse to aware dt
            # Normalize to UTC
            parsed = datetime.fromisoformat(scheduled_at_utc.replace('Z', '+00:00'))
            scheduled_dt = parsed.astimezone(timezone.utc)
        else:
            # Fallback: interpret datetime-local (no tz) as server local; not ideal
            scheduled_naive = datetime.fromisoformat(scheduled_at_str)
            scheduled_dt = scheduled_naive.replace(tzinfo=timezone.utc)
    except Exception:
        flash('Invalid date/time format.', 'error')
        return redirect(url_for('index'))

    count = schedule_tweets([scheduled_text], scheduled_dt)
    flash('Scheduled 1 tweet.', 'success')

    # Ensure scheduler is running
    start_scheduler_if_needed()
    return redirect(url_for('index'))

    action = request.form.get('action')
    tweets_input = request.form.get('tweets_input', '')
    tweet_texts = parse_tweets_from_input(tweets_input)

    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in double quotes.', 'error')
        return redirect(url_for('index'))

    if action == 'post':
        # Post the first tweet immediately
        tweet_to_post = tweet_texts.pop(0)
        success, message = post_tweet(tweet_to_post)
        if success:
            mark_tweet_posted(None, tweet_to_post)  # None for ID as it wasn't in DB
            flash('Tweet posted successfully!', 'success')
        else:
            flash(f'Error posting tweet: {message}', 'error')
            # Add it back to the list to be queued if posting fails
            tweet_texts.insert(0, tweet_to_post)

    # Add remaining tweets (or all if action was 'queue') to the queue
    if action == 'queue':
        if tweet_texts:
            added_count = add_tweets_to_queue(tweet_texts)
            flash(f'{added_count} tweet(s) added to the queue!', 'success')
    elif tweet_texts: # This handles the case where action is 'post' and there are remaining tweets
        added_count = add_tweets_to_queue(tweet_texts)
        flash(f'First tweet posted, {added_count} remaining tweet(s) added to queue.', 'success')
    else:
        # This case is for when action is 'post' and there was only one tweet.
        # The success message is already flashed inside the 'post' block.
        pass

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
        mark_tweet_posted(next_tweet['_id'], next_tweet['text'])
        flash(f'Posted: "{next_tweet["text"]}"', 'success')
    else:
        flash(f'Failed to post tweet: {message}', 'error')
    
    return redirect(url_for('index'))

@app.route('/clear_queue', methods=['POST'])
def clear_queue():
    """Clear all tweets from queue"""
    if USE_MONGODB and queue_collection is not None:
        result = queue_collection.delete_many({})
        flash(f'Cleared {result.deleted_count} tweets from queue', 'info')
    else:
        global IN_MEMORY_QUEUE
        cleared_count = len(IN_MEMORY_QUEUE)
        IN_MEMORY_QUEUE = []
        flash(f'Cleared {cleared_count} tweets from queue', 'info')
    return redirect(url_for('index'))

@app.route('/api/stats')
def api_stats():
    """API endpoint for queue stats"""
    return jsonify(get_queue_stats())

@app.route('/delete_tweet/<tweet_id>', methods=['POST'])
def delete_tweet(tweet_id):
    """Delete a single tweet from the queue."""
    if USE_MONGODB and queue_collection is not None:
        from bson.objectid import ObjectId
        result = queue_collection.delete_one({'_id': ObjectId(tweet_id)})
        if result.deleted_count == 0:
            flash('Tweet not found or already deleted.', 'error')
        else:
            flash('Tweet successfully deleted.', 'success')
    else:
        global IN_MEMORY_QUEUE
        original_len = len(IN_MEMORY_QUEUE)
        IN_MEMORY_QUEUE = [t for t in IN_MEMORY_QUEUE if str(t['_id']) != tweet_id]
        if len(IN_MEMORY_QUEUE) < original_len:
            flash('Tweet successfully deleted.', 'success')
        else:
            flash('Tweet not found or already deleted.', 'error')
    return redirect(url_for('index'))

@app.route('/update_tweet/<tweet_id>', methods=['POST'])
def update_tweet(tweet_id):
    """Update the text of a single tweet in the queue."""
    new_text = request.form.get('new_text', '').strip()
    if not new_text:
        flash('Tweet text cannot be empty.', 'error')
        return redirect(url_for('index'))

    if USE_MONGODB and queue_collection is not None:
        from bson.objectid import ObjectId
        result = queue_collection.update_one(
            {'_id': ObjectId(tweet_id)},
            {'$set': {'text': new_text}}
        )
        if result.matched_count == 0:
            flash('Tweet not found.', 'error')
        else:
            flash('Tweet updated successfully.', 'success')
    else:
        global IN_MEMORY_QUEUE
        for tweet in IN_MEMORY_QUEUE:
            if str(tweet['_id']) == tweet_id:
                tweet['text'] = new_text
                flash('Tweet updated successfully.', 'success')
                return redirect(url_for('index'))
        flash('Tweet not found.', 'error')
    return redirect(url_for('index'))

@app.route('/post_tweet/<tweet_id>', methods=['POST'])
def post_specific_tweet(tweet_id):
    # TODO: Add authentication check when OAuth is implemented
    # if not session.get('oauth_token'):
    #     return redirect(url_for('index'))

    tweet_to_post = None
    if USE_MONGODB and queue_collection is not None:
        from bson.objectid import ObjectId
        obj_id = ObjectId(tweet_id)
        tweet_to_post = queue_collection.find_one({'_id': obj_id})
    else:
        for tweet in IN_MEMORY_QUEUE:
            if str(tweet.get('_id')) == tweet_id:
                tweet_to_post = tweet
                break

    if not tweet_to_post:
        flash('Tweet not found in queue.', 'error')
        return redirect(url_for('index'))

    success, message = post_tweet(tweet_to_post['text'])

    if success:
        # Pass the correct ID type depending on storage
        if USE_MONGODB and queue_collection is not None:
            mark_tweet_posted(obj_id, tweet_to_post['text'])
        else:
            mark_tweet_posted(tweet_id, tweet_to_post['text'])
        flash('Tweet posted successfully!', 'success')
    else:
        flash(f'Failed to post tweet: {message}', 'error')

    return redirect(url_for('index'))

@app.route('/clear_history', methods=['POST'])
def clear_history():
    """Clear all tweets from the post history."""
    if USE_MONGODB and history_collection is not None:
        result = history_collection.delete_many({})
        flash(f'Cleared {result.deleted_count} tweets from history.', 'info')
    else:
        global IN_MEMORY_HISTORY
        cleared_count = len(IN_MEMORY_HISTORY)
        IN_MEMORY_HISTORY = []
        flash(f'Cleared {cleared_count} tweets from history.', 'info')
    return redirect(url_for('index'))


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
                    mark_tweet_posted(next_tweet['_id'], next_tweet['text'])
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

# Scheduler background thread for scheduled posts
scheduler_active = False
scheduler_thread = None

def scheduler_worker():
    global scheduler_active
    while scheduler_active:
        try:
            now = datetime.now(timezone.utc)
            if USE_MONGODB and 'scheduled_collection' in globals() and scheduled_collection is not None:
                due = list(scheduled_collection.find({
                    "posted": False,
                    "scheduled_at": {"$lte": now}
                }).sort("scheduled_at", 1))
                for doc in due:
                    success, message = post_tweet(doc['text'])
                    if success:
                        scheduled_collection.update_one({"_id": doc["_id"]}, {"$set": {"posted": True}})
                        if history_collection is not None:
                            history_collection.insert_one({"text": doc['text'], "posted_at": now})
                        if stats_collection is not None:
                            stats_collection.update_one({"_id": "posted_count"}, {"$inc": {"count": 1}}, upsert=True)
                    else:
                        print(f"❌ Failed scheduled post: {message}")
            else:
                for item in IN_MEMORY_SCHEDULED:
                    if not item.get('posted') and item['scheduled_at'] <= now:
                        success, message = post_tweet(item['text'])
                        if success:
                            item['posted'] = True
                            IN_MEMORY_HISTORY.append({"text": item['text'], "posted_at": now})
                            IN_MEMORY_STATS["posted_count"] = IN_MEMORY_STATS.get("posted_count", 0) + 1
                        else:
                            print(f"❌ Failed scheduled post: {message}")
            time.sleep(30)
        except Exception as e:
            print(f"Scheduler error: {e}")
            time.sleep(30)

def start_scheduler_if_needed():
    global scheduler_active, scheduler_thread
    if not scheduler_active:
        scheduler_active = True
        scheduler_thread = threading.Thread(target=scheduler_worker, daemon=True)
        scheduler_thread.start()

@app.route('/clear_stats', methods=['POST'])
def clear_stats():
    """Clear/Reset posting statistics counters."""
    if USE_MONGODB and stats_collection is not None:
        # Reset the posted counter to 0
        stats_collection.update_one(
            {"_id": "posted_count"},
            {"$set": {"count": 0}},
            upsert=True
        )
    else:
        IN_MEMORY_STATS["posted_count"] = 0
    flash('Statistics have been reset.', 'info')
    return redirect(url_for('index'))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
