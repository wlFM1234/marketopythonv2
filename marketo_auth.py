import os
import time
import threading
import requests

_token: str = ""
_token_expires: float = 0.0
_lock = threading.Lock()


def get_valid_mkto_token() -> str:
    """Return a valid Marketo access token, fetching a fresh one when expired."""
    global _token, _token_expires
    with _lock:
        if _token and time.time() < _token_expires:
            return _token
        base = os.getenv("MARKETO_BASE_URL", "").strip().rstrip("/")
        r = requests.get(
            f"{base}/identity/oauth/token",
            params={
                "grant_type": "client_credentials",
                "client_id": os.getenv("MARKETO_CLIENT_ID", "").strip(),
                "client_secret": os.getenv("MARKETO_CLIENT_SECRET", "").strip(),
            },
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        tok = j.get("access_token")
        if not tok:
            raise RuntimeError(f"Marketo auth failed: {j}")
        _token = tok
        _token_expires = time.time() + j.get("expires_in", 3600) - 60
        return _token


def invalidate_mkto_token() -> None:
    """Force the next get_valid_mkto_token() call to fetch a fresh token."""
    global _token
    with _lock:
        _token = ""


def marketo_request(method: str, url: str, **kwargs) -> dict:
    """Make a Marketo REST API call with Bearer auth.
    Renews the token and retries on error codes 601/602 (invalid/expired token).
    """
    base_headers = kwargs.pop("headers", {})
    last_error = None
    for attempt in range(4):
        headers = {**base_headers, "Authorization": f"Bearer {get_valid_mkto_token()}"}
        r = requests.request(method, url, headers=headers, **kwargs)
        r.raise_for_status()
        j = r.json()
        if j.get("success"):
            return j
        codes = {str(e.get("code")) for e in (j.get("errors") or [])}
        last_error = j
        if codes & {"601", "602"}:
            invalidate_mkto_token()
            time.sleep(1.5 * (attempt + 1))  # small backoff before retry
            continue
        raise RuntimeError(f"Marketo API error: {j}")
    raise RuntimeError(f"Marketo API call failed after token refresh: {last_error}")
