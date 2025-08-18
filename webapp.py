#!/usr/bin/env python3
"""
Twitter Bot Web Frontend
Simple interface to manage tweet queue
"""
import os
import re
import requests
import base64
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, make_response
from dotenv import load_dotenv
from rest import get_valid_token, save_tokens
from datetime import datetime, timezone, timedelta
import threading
import time
import random
from functools import wraps
from pymongo import MongoClient
from bson.objectid import ObjectId

load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Minimal shared-secret protection
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

def require_admin(fn):
    """Minimal shared-secret check.
    Provide token via header X-Admin-Token, query ?admin_token=, or cookie admin_token.
    If ADMIN_TOKEN is not set, this check is effectively disabled.
    """
    @wraps(fn)
    def _wrapped(*args, **kwargs):
        if not ADMIN_TOKEN:
            return fn(*args, **kwargs)
        token = (
            request.headers.get("X-Admin-Token")
            or request.args.get("admin_token")
            or request.cookies.get("admin_token")
        )
        if token == ADMIN_TOKEN:
            return fn(*args, **kwargs)
        return jsonify({"error": "forbidden"}), 403
    return _wrapped

# Twitter credentials
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

# Twitter API endpoints
AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"

# Tweet queue management
MONGO_URI = os.getenv("MONGO_URI")

# Safety: prevent accidental posting to profile when using community actions
COMMUNITY_POST_TO_PROFILE_ALLOWED = os.getenv("ALLOW_COMMUNITY_PROFILE_POST", "false").lower() == "true"

posting_active = False
auto_post_thread = None
community_posting_active = False
community_auto_post_thread = None


# In-memory data stores (fallback if MongoDB is not used)
IN_MEMORY_QUEUE = []
IN_MEMORY_HISTORY = []
IN_MEMORY_SCHEDULED = []
IN_MEMORY_STATS = {"posted_count": 0}
IN_MEMORY_COMMUNITIES = []
USE_MONGODB = False

# Database and collection variables
client = None
db = None
queue_collection = None
history_collection = None
stats_collection = None
scheduled_collection = None
communities_collection = None
community_queue_collection = None

# Try MongoDB connection with SSL error handling
try:
    print("🔄 Attempting MongoDB connection...")
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command('ping') # Test the connection
    db = client["twitter"]
    queue_collection = db["tweet_queue"]
    history_collection = db["post_history"]
    stats_collection = db["bot_stats"]
    scheduled_collection = db["scheduled_posts"]
    communities_collection = db["communities"]
    community_queue_collection = db["community_tweet_queue"]
    USE_MONGODB = True
    print("✅ MongoDB connection successful!")
except Exception as e:
    print(f"❌ MongoDB connection failed: {str(e)[:100]}...")
    print("⚠️  Using in-memory storage - data will not persist after restart")

def parse_tweets_from_input(input_text):
    """Parse tweets from input text using square brackets as delimiters.
    Example: [First tweet] [Second tweet]
    This avoids conflicts with quotes inside tweet text.
    """
    # Find all text within single-level square brackets
    tweets = re.findall(r"\[([^\]]+)\]", input_text)
    
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

def add_tweets_to_community_queue(tweets, community_id):
    """Add tweets to a specific community's queue."""
    if not tweets:
        return 0
    
    if USE_MONGODB and community_queue_collection is not None:
        tweet_docs = []
        for tweet in tweets:
            tweet_docs.append({
                "text": tweet,
                "community_id": community_id,
                "created_at": datetime.now(timezone.utc),
                "posted": False
            })
        
        result = community_queue_collection.insert_many(tweet_docs)
        return len(result.inserted_ids)
    else:
        # In-memory not supported for this feature yet
        return 0

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
        unposted = queue_collection.count_documents({"posted": False})
        
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

def post_to_profile(caption):
    """Post to the user's profile timeline."""
    try:
        token = get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {"text": caption}
        response = requests.post("https://api.x.com/2/tweets", headers=headers, json=payload)
        if response.status_code == 201:
            return True, "Tweet posted successfully"
        else:
            return False, f"Failed to post tweet: {response.status_code} - {response.text}"
    except Exception as e:
        return False, f"Error posting tweet: {str(e)}"

def post_to_community(caption, community_id):
    """Post to a specific community and return the new tweet's ID."""
    try:
        if not community_id:
            return False, "community_id is required for community posting", None
        token = get_valid_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {"text": caption, "community_id": str(community_id)}
        response = requests.post("https://api.x.com/2/tweets", headers=headers, json=payload)
        if response.status_code == 201:
            tweet_id = response.json().get('data', {}).get('id')
            return True, "Community post created successfully", tweet_id
        else:
            return False, f"Failed to post to community: {response.status_code} - {response.text}", None
    except Exception as e:
        return False, f"Error posting to community: {str(e)}", None

def post_tweet(caption):
    """Backward-compatible alias for posting to profile."""
    return post_to_profile(caption)

def retweet(tweet_id):
    """Retweets a tweet by its ID using the user ID from get_valid_token."""
    try:
        token_info = get_valid_token(return_full_response=True)
        if not token_info or 'access_token' not in token_info:
            return False, "Could not get a valid access token."
        
        user_id = token_info.get('user_id')
        if not user_id:
            return False, "Could not retrieve user ID to perform retweet."

        headers = {
            "Authorization": f"Bearer {token_info['access_token']}",
            "Content-Type": "application/json"
        }

        retweet_url = f"https://api.x.com/2/users/{user_id}/retweets"
        payload = {"tweet_id": tweet_id}
        response = requests.post(retweet_url, headers=headers, json=payload)

        if response.status_code == 200:
            retweeted = response.json().get('data', {}).get('retweeted')
            if retweeted:
                return True, "Retweet successful"
            else:
                return False, f"Retweet action returned false. Response: {response.text}"
        else:
            if response.status_code == 403 and "You have already retweeted this Tweet" in response.text:
                return True, "Already retweeted"
            return False, f"Failed to retweet: {response.status_code} - {response.text}"

    except Exception as e:
        print(f"An exception occurred in retweet: {e}")
        return False, f"An exception occurred: {e}"

# Web routes
@app.route('/')
@require_admin
def index():
    # Fetch all necessary data
    if USE_MONGODB and queue_collection is not None:
        tweets = list(queue_collection.find({"posted": False}).sort("created_at", 1))
        history = list(history_collection.find().sort("posted_at", -1).limit(50))
        stats = get_queue_stats()
        scheduled = list(scheduled_collection.find({"posted": False}).sort("scheduled_at", 1))
        communities = list(communities_collection.find())
    else:
        # In-memory fallback
        tweets = [t for t in IN_MEMORY_QUEUE if not t.get("posted", False)]
        history = sorted(IN_MEMORY_HISTORY, key=lambda x: x["posted_at"], reverse=True)[:50]
        stats = get_queue_stats()
        scheduled = [s for s in IN_MEMORY_SCHEDULED if not s.get('posted')]
        communities = IN_MEMORY_COMMUNITIES

    # Handle active community and its specific queue
    active_community_id = session.get('active_community_id')
    active_community = None
    community_tweets = []
    if active_community_id:
        if USE_MONGODB and communities_collection is not None:
            active_community = communities_collection.find_one({'_id': active_community_id})
            if community_queue_collection is not None:
                community_tweets = list(community_queue_collection.find({'community_id': active_community_id, 'posted': False}).sort("created_at", 1))
        else:
            active_community = next((c for c in communities if c['_id'] == active_community_id), None)
            # In-memory community tweets not supported

    # Format scheduled datetimes for display
    for item in scheduled:
        if 'scheduled_at' in item and isinstance(item['scheduled_at'], datetime):
            item['scheduled_at_str'] = item['scheduled_at'].replace(tzinfo=timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')
        else:
            item['scheduled_at_str'] = ''

    resp = make_response(render_template('index.html', 
                         queue=tweets,
                         history=history,
                         stats=stats,
                         scheduled_count=len(scheduled),
                         use_mongodb=USE_MONGODB,
                         posting_active=posting_active,
                         community_posting_active=community_posting_active,
                         communities=communities,
                         active_community=active_community,
                         community_queue=community_tweets))
    
    # Persist admin token in cookie if provided
    try:
        token = request.args.get('admin_token')
        if token and token == ADMIN_TOKEN:
            expire_date = datetime.now() + timedelta(days=30)
            resp.set_cookie('admin_token', token, expires=expire_date, httponly=True, samesite='Lax')
    except Exception as e:
        print(f"Error setting admin cookie: {e}")

    return resp

@app.route('/keepalive')
def keepalive():
    """An endpoint to keep the server alive on free hosting services."""
    return jsonify({"status": "alive", "timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/add_tweets', methods=['POST'])
@require_admin
def add_tweets():
    # TODO: Add authentication check when OAuth is implemented
    # if not session.get('oauth_token'):
    #     return redirect(url_for('index'))
    action = request.form.get('action')
    tweets_input = request.form.get('tweets_input', '')
    tweet_texts = parse_tweets_from_input(tweets_input)

    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in [square brackets].', 'error')
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

@app.route('/add_community_tweets', methods=['POST'])
@require_admin
def add_community_tweets():
    """Add tweets to the active community's queue."""
    tweets_input = request.form.get('community_tweets_input', '')
    tweet_texts = parse_tweets_from_input(tweets_input)

    if not tweet_texts:
        flash('No valid tweets found. Please enclose each tweet in [square brackets].', 'error')
        return redirect(url_for('index'))

    active_community_id = session.get('active_community_id')
    if not active_community_id:
        flash('No active community set. Please set one before adding tweets.', 'error')
        return redirect(url_for('index'))

    added_count = add_tweets_to_community_queue(tweet_texts, active_community_id)
    
    if added_count > 0:
        flash(f'{added_count} tweet(s) added to the community queue!', 'success')
    else:
        flash('Could not add tweets to the community queue.', 'error')

    return redirect(url_for('index'))

@app.route('/scheduled/delete/<sid>', methods=['POST'])
@require_admin
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
@require_admin
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

def get_next_community_tweet(community_id):
    """Get the next unposted tweet for a specific community."""
    if USE_MONGODB and community_queue_collection is not None:
        return community_queue_collection.find_one(
            {"community_id": community_id, "posted": False},
            sort=[("created_at", 1)]
        )
    # In-memory not supported for this feature yet
    return None

def mark_community_tweet_posted(tweet_id, tweet_text):
    """Mark a community tweet as posted."""
    if USE_MONGODB and community_queue_collection is not None and history_collection is not None:
        history_collection.insert_one({
            "text": tweet_text,
            "posted_at": datetime.now(timezone.utc),
            "type": "community"
        })
        community_queue_collection.delete_one({"_id": tweet_id})
    # In-memory not supported for this feature yet
    pass

def community_auto_post_worker(active_community):
    """Background worker for posting tweets to the active community."""
    global community_posting_active
    community_id = active_community.get('_id')
    print(f"[community-auto-post] Worker started for community: {active_community.get('name', 'N/A')}.")
    while community_posting_active:
        try:
            next_tweet = get_next_community_tweet(community_id)
            if next_tweet:
                print(f"[community-auto-post] Posting: {next_tweet['text']}")
                success, message, new_tweet_id = post_to_community(next_tweet['text'], community_id)
                if success and new_tweet_id:
                    mark_community_tweet_posted(next_tweet['_id'], next_tweet['text'])
                    print(f"[community-auto-post] ✅ Posted to community: {next_tweet['text']}")

                    # Retweet to profile
                    retweet_success, retweet_message = retweet(new_tweet_id)
                    if retweet_success:
                        print(f"[community-auto-post] ✅ Retweeted to profile.")
                    else:
                        print(f"[community-auto-post] ❌ Failed to retweet: {retweet_message}")

                    # Wait after successful post
                    time.sleep(60 * 60 * 2) # Wait 2 hours
                else:
                    print(f"[community-auto-post] ❌ Failed to post to community: {message}")
                    time.sleep(60) # Wait 1 minute before retry
            else:
                print(f"[community-auto-post] No tweets in queue for community {community_id}. Checking in 30s...")
                time.sleep(30)
        except Exception as e:
            print(f"[community-auto-post] Error in worker: {e}")
            time.sleep(60)

    print(f"[community-auto-post] Worker stopped for community {community_id}.")

@app.route('/start_community_auto_post', methods=['POST'])
@require_admin
def start_community_auto_post():
    global community_posting_active, community_auto_post_thread
    if not community_posting_active:
        active_community_id = session.get('active_community_id')
        if not active_community_id:
            flash('Set an active community before starting auto-posting.', 'error')
            return redirect(url_for('index'))

        if USE_MONGODB:
            active_community = communities_collection.find_one({'_id': active_community_id})
        else:
            active_community = next((c for c in IN_MEMORY_COMMUNITIES if c['_id'] == active_community_id), None)

        if not active_community:
            flash('Active community not found.', 'error')
            return redirect(url_for('index'))

        community_posting_active = True
        community_auto_post_thread = threading.Thread(target=community_auto_post_worker, args=(active_community,))
        community_auto_post_thread.daemon = True
        community_auto_post_thread.start()
        flash('Community auto-posting started!', 'success')
    else:
        flash('Community auto-posting is already running.', 'info')
    return redirect(url_for('index'))

@app.route('/pause_community_auto_post', methods=['POST'])
@require_admin
def pause_community_auto_post():
    global community_posting_active
    if community_posting_active:
        community_posting_active = False
        if community_auto_post_thread:
            community_auto_post_thread.join(timeout=1) # Give thread a moment to exit cleanly
        flash('Community auto-posting paused.', 'success')
    else:
        flash('Community auto-posting is not running.', 'info')
    return redirect(url_for('index'))

# Alias to match frontend form action
@app.route('/stop_community_auto_posting', methods=['POST'])
@require_admin
def stop_community_auto_posting():
    """Alias for pausing community auto-posting to match frontend route."""
    return pause_community_auto_post()

@app.route('/post_next_community', methods=['POST'])
@require_admin
def post_next_community():
    """Manually post the next tweet in the active community queue."""
    active_community_id = session.get('active_community_id')
    if not active_community_id:
        flash('No active community set. Please set one before posting.', 'error')
        return redirect(url_for('index'))

    next_tweet = get_next_community_tweet(active_community_id)
    if not next_tweet:
        flash('No tweets in the community queue to post!', 'error')
        return redirect(url_for('index'))

    success, message, new_tweet_id = post_to_community(next_tweet['text'], community_id=active_community_id)
    if success and new_tweet_id:
        mark_community_tweet_posted(next_tweet['_id'], next_tweet['text'])
        flash('Community tweet posted successfully!', 'success')

        # Retweet the new post to the main profile
        retweet_success, retweet_message = retweet(new_tweet_id)
        if retweet_success:
            flash('Post was retweeted to your profile.', 'info')
        else:
            flash(f'Could not retweet to profile: {retweet_message}', 'error')
    else:
        flash(f'Failed to post community tweet: {message}', 'error')
    return redirect(url_for('index'))

@app.route('/post_community_tweet/<tweet_id>', methods=['POST'])
@require_admin
def post_specific_community_tweet(tweet_id):
    """Post a specific tweet in the active community queue by ID."""
    if not USE_MONGODB or community_queue_collection is None:
        flash('In-memory community queue operations are not supported.', 'error')
        return redirect(url_for('index'))

    try:
        from bson.objectid import ObjectId
        obj_id = ObjectId(tweet_id)
    except Exception:
        flash('Invalid community tweet ID.', 'error')
        return redirect(url_for('index'))

    tweet_doc = community_queue_collection.find_one({'_id': obj_id})
    if not tweet_doc:
        flash('Community tweet not found in queue.', 'error')
        return redirect(url_for('index'))

    active_community_id = session.get('active_community_id') or tweet_doc.get('community_id')
    success, message, new_tweet_id = post_to_community(tweet_doc['text'], community_id=active_community_id)
    if success and new_tweet_id:
        mark_community_tweet_posted(obj_id, tweet_doc['text'])
        flash('Community tweet posted successfully!', 'success')

        # Retweet the new post to the main profile
        retweet_success, retweet_message = retweet(new_tweet_id)
        if retweet_success:
            flash('Post was retweeted to your profile.', 'info')
        else:
            flash(f'Could not retweet to profile: {retweet_message}', 'error')
    else:
        flash(f'Failed to post community tweet: {message}', 'error')
    return redirect(url_for('index'))

@app.route('/delete_community_tweet/<tweet_id>', methods=['POST'])
@require_admin
def delete_community_tweet(tweet_id):
    """Delete a single tweet from the active community queue."""
    if USE_MONGODB and community_queue_collection is not None:
        from bson.objectid import ObjectId
        result = community_queue_collection.delete_one({'_id': ObjectId(tweet_id)})
        if result.deleted_count == 0:
            flash('Community tweet not found or already deleted.', 'error')
        else:
            flash('Community tweet successfully deleted.', 'success')
    else:
        flash('In-memory community queue operations are not supported.', 'error')
    return redirect(url_for('index'))

@app.route('/update_community_tweet/<tweet_id>', methods=['POST'])
@require_admin
def update_community_tweet(tweet_id):
    """Update the text of a single tweet in the community queue."""
    new_text = request.form.get('new_text', '').strip()
    if not new_text:
        flash('Tweet text cannot be empty.', 'error')
        return redirect(url_for('index'))

    if USE_MONGODB and community_queue_collection is not None:
        from bson.objectid import ObjectId
        result = community_queue_collection.update_one(
            {'_id': ObjectId(tweet_id)},
            {'$set': {'text': new_text}}
        )
        if result.matched_count == 0:
            flash('Community tweet not found.', 'error')
        else:
            flash('Community tweet updated successfully.', 'success')
    else:
        flash('In-memory community queue operations are not supported.', 'error')
    return redirect(url_for('index'))

@app.route('/clear_community_queue', methods=['POST'])
@require_admin
def clear_community_queue():
    """Clear all tweets from the active community queue."""
    active_community_id = session.get('active_community_id')
    if not active_community_id:
        flash('No active community set. Please set one before clearing queue.', 'error')
        return redirect(url_for('index'))

    if USE_MONGODB and community_queue_collection is not None:
        result = community_queue_collection.delete_many({'community_id': active_community_id})
        flash(f'Cleared {result.deleted_count} tweets from community queue', 'info')
    else:
        flash('In-memory community queue operations are not supported.', 'error')
    return redirect(url_for('index'))

@app.route('/schedule', methods=['POST'])
@require_admin
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

@app.route('/post_next', methods=['POST'])
@require_admin
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
@require_admin
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
@require_admin
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
@require_admin
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
@require_admin
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
@require_admin
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
def auto_post_worker():
    global posting_active
    print("[auto-post] Worker started. Will post every 2 hours when items exist.")
    while posting_active:
        try:
            next_tweet = get_next_tweet()
            if next_tweet:
                print(f"[auto-post] Posting next tweet: {next_tweet['text']}")
                success, message = post_tweet(next_tweet['text'])
                if success:
                    mark_tweet_posted(next_tweet['_id'], next_tweet['text'])
                    print(f"[auto-post] ✅ Posted and removed: {next_tweet['text']}")
                    time.sleep(60 * 60 * 2) # Wait 2 hours
                else:
                    print(f"[auto-post] ❌ Failed to post: {message}")
                    time.sleep(60) # Wait 1 minute before retry
            else:
                print("[auto-post] No tweets in queue, checking again in 30s...")
                time.sleep(30)
        except Exception as e:
            print(f"[auto-post] Error in worker: {e}")
            time.sleep(60) # Wait 1 minute before retry

@app.route('/start_auto_posting', methods=['POST'])
@require_admin
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
@require_admin
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

@app.route('/set_active_community', methods=['POST'])
@require_admin
def set_active_community():
    """Add or update a community and set it as active in the session."""
    community_id = request.form.get('community_id', '').strip()
    provided_name = request.form.get('community_name', '').strip()
    if not community_id or not community_id.isdigit():
        flash('Please provide a valid numerical Community ID.', 'error')
        return redirect(url_for('index'))

    community_name = provided_name if provided_name else f"Community {community_id}" # Placeholder fallback

    if USE_MONGODB and communities_collection is not None:
        set_on_insert = {
            'auto_posting_active': False,
            'created_at': datetime.now(timezone.utc)
        }
        # Only include 'name' in one operator to avoid conflict
        if provided_name:
            update_doc = {
                '$setOnInsert': set_on_insert,
                '$set': {'name': provided_name}
            }
        else:
            update_doc = {
                '$setOnInsert': {
                    **set_on_insert,
                    'name': community_name
                }
            }
        communities_collection.update_one({'_id': community_id}, update_doc, upsert=True)
    else:
        existing = next((c for c in IN_MEMORY_COMMUNITIES if c['_id'] == community_id), None)
        if existing:
            if provided_name:
                existing['name'] = provided_name
        else:
            IN_MEMORY_COMMUNITIES.append({
                '_id': community_id,
                'name': community_name,
                'auto_posting_active': False
            })

    session['active_community_id'] = community_id
    flash(f'Active community set to {community_name}', 'success')
    return redirect(url_for('index'))

@app.route('/clear_stats', methods=['POST'])
@require_admin
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
