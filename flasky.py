import os
import requests
import base64
from flask import Flask, request, redirect, url_for, session
from dotenv import load_dotenv
from rest import save_tokens # We'll use this to save our tokens

load_dotenv() # Load variables from .env file

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Get credentials from .env
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

# Twitter API endpoints
AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"

@app.route('/')
def authorize():
    # Scopes needed for the bot to post tweets
    scopes = ["tweet.read", "tweet.write", "users.read", "offline.access"]
    
    # Generate a secure state variable
    state = base64.urlsafe_b64encode(os.urandom(30)).decode("utf-8")
    session['state'] = state

    # Create the authorization URL
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": "challenge", # Plain challenge for simplicity
        "code_challenge_method": "plain"
    }
    auth_url_with_params = f"{AUTH_URL}?{"&".join([f'{k}={v}' for k, v in params.items()])}"
    
    return f'<h1>Step 1: Authorize Your App</h1><p><a href="{auth_url_with_params}">Click here to authorize with Twitter</a></p>'

@app.route('/callback')
def callback():
    code = request.args.get('code')
    state = request.args.get('state')

    # Check if we have a session state
    if 'state' not in session:
        return "No session found. Please start the authorization process from the beginning by visiting <a href='/'>the home page</a>.", 400

    # Verify the state to prevent CSRF attacks
    if state != session.get('state'):
        return "State mismatch. Please try again.", 400

    # Exchange the authorization code for an access token
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "code": code,
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": "challenge"
    }
    
    auth_str = f"{CLIENT_ID}:{CLIENT_SECRET}"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()
    headers['Authorization'] = f"Basic {b64_auth_str}"

    response = requests.post(TOKEN_URL, headers=headers, data=data)
    
    if response.status_code != 200:
        return f"Error getting token: {response.text}", 500

    token_data = response.json()
    access_token = token_data.get('access_token')
    refresh_token = token_data.get('refresh_token')

    # Save the tokens to your MongoDB database
    save_tokens(access_token, refresh_token)

    return '<h1>Success!</h1><p>Your tokens have been saved to the database. You can now close this window and stop the script.</p>'


if __name__ == "__main__":
    # This will run a temporary web server on your local machine
    # Make sure the port matches the one in your REDIRECT_URI
    app.run(host='0.0.0.0', port=8080, debug=True)
