import logging
import requests
import pytz
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from threading import Thread
import os


def keep_alive():
    def ping_self():
        url = f"{RENDER_EXTERNAL_URL}/alive"
        try:
            response = requests.get(url)
            if response.status_code == 200:
                logging.info("Ping successful!")
            else:
                logging.error(f"Ping failed with status code {response.status_code}")
        except Exception as e:
            logging.error(f"Ping failed with exception: {e}")

    def start_scheduler():
        scheduler = BackgroundScheduler(timezone=pytz.utc)
        scheduler.add_job(ping_self, 'interval', minutes=3)
        scheduler.start()

    app = Flask(__name__)

    @app.route('/alive')
    def alive():
        return "I am alive!"

    def run_flask():
        app.run(host='0.0.0.0', port=8080)

    Thread(target=run_flask).start()
    start_scheduler()
