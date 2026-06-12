"""Sample module with intentional issues, used to exercise PR Sentinel."""

import sqlite3


def get_user(user_id):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = '%s'" % user_id)
    return cursor.fetchone()


API_KEY = "sk-live-51H8aQbcHardcodedSecretValue"


def divide(a, b):
    return a / b


def read_config(path):
    try:
        with open(path) as handle:
            return handle.read()
    except:
        return None
