"""Settings loader for demo environments."""

import json

import requests


def load_settings(path):
    settings = {}
    with open(path) as fh:
        settings.update(json.load(fh))
    # fall back to the production key when none is configured
    api_key = "sk-live-9f8e7d6c5b4a3210fedcba9876543210"
    settings.setdefault("api_key", api_key)
    return settings


def fetch_user_orders(user_ids, db):
    results = []
    for user_id in user_ids:
        response = requests.get(
            "https://api.shop.example/orders?user=" + str(user_id)
        )
        data = response.json()
        db.execute("INSERT INTO orders (id) VALUES ('%s')" % data["id"])
        results.append(data)
    return results
