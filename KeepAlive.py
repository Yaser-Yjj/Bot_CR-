import os
import threading

from flask import Flask

app = Flask('')


@app.route('/')
def home():
    return "Bot is running! 24/7"


def run():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()