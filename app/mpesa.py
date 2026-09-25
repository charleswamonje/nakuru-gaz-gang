import base64
import os

import requests


def mpesa_base_url():
    environment = os.getenv("MPESA_ENV", "sandbox").lower()

    if environment == "production":
        return "https://api.safaricom.co.ke"

    return "https://sandbox.safaricom.co.ke"


def get_mpesa_access_token():
    consumer_key = os.getenv("MPESA_CONSUMER_KEY", "")
    consumer_secret = os.getenv("MPESA_CONSUMER_SECRET", "")

    if not consumer_key or not consumer_secret:
        raise RuntimeError("M-PESA API credentials are not configured.")

    credentials = f"{consumer_key}:{consumer_secret}".encode("utf-8")
    encoded = base64.b64encode(credentials).decode("utf-8")

    response = requests.get(
        f"{mpesa_base_url()}/oauth/v1/generate?grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {encoded}",
            "Accept": "application/json",
        },
        timeout=15,
    )

    response.raise_for_status()

    data = response.json()
    token = data.get("access_token")

    if not token:
        raise RuntimeError("M-PESA did not return an access token.")

    return token
