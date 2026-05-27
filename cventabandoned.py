"""
cventabandoned.py
-----------------
Pulls abandoned registrants from all active Cvent events in the last 24 hours,
then for each person:
  - If email exists in Marketo → updates freefielduniqueurl + adds to static list
  - If email doesn't exist    → creates new lead with freefielduniqueurl + adds to static list

Required GitHub secrets:
  CVENT_CLIENT_ID
  CVENT_CLIENT_SECRET
  MARKETO_BASE_URL
  MARKETO_CLIENT_ID
  MARKETO_CLIENT_SECRET
  MARKETO_ABANDONED_REG_LIST_ID
"""

import os
import sys
import base64
import requests
from datetime import datetime, timezone, timedelta

# ── Config ────────────────────────────────────────────────────────────────────

CVENT_CLIENT_ID       = os.environ["CVENT_CLIENT_ID"]
CVENT_CLIENT_SECRET   = os.environ["CVENT_CLIENT_SECRET"]
CVENT_BASE_URL        = "https://api-platform-eur.cvent.com/ea"

MARKETO_BASE_URL      = os.environ["MARKETO_BASE_URL"].rstrip("/")
MARKETO_CLIENT_ID     = os.environ["MARKETO_CLIENT_ID"]
MARKETO_CLIENT_SECRET = os.environ["MARKETO_CLIENT_SECRET"]
MARKETO_LIST_ID       = os.environ["MARKETO_ABANDONED_REG_LIST_ID"]

# Only pull abandons from the last 24 hours
SINCE = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Cvent auth ────────────────────────────────────────────────────────────────

def get_cvent_token():
    credentials = base64.b64encode(
        f"{CVENT_CLIENT_ID}:{CVENT_CLIENT_SECRET}".encode()
    ).decode()

    resp = requests.post(
        f"{CVENT_BASE_URL}/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "client_id": CVENT_CLIENT_ID,
        },
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        print(f"Cvent auth response: {resp.json()}")
        raise ValueError("No access_token in Cvent auth response")
    print("✅ Cvent auth OK")
    return token


# ── Cvent: get all active events ──────────────────────────────────────────────

def get_active_events(cvent_token):
    headers = {"Authorization": f"Bearer {cvent_token}"}
    events = []
    next_token = None

    while True:
        params = {"filter": "status eq 'Active'", "limit": 50}
        if next_token:
            params["token"] = next_token

        resp = requests.get(
            f"{CVENT_BASE_URL}/event-management/v1/events",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        events.extend(data.get("data", []))

        next_token = data.get("paging", {}).get("nextToken")
        if not next_token:
            break

    print(f"📅 Found {len(events)} active Cvent event(s)")
    return events


# ── Cvent: get abandoned registrants from last 24hrs for one event ────────────

def get_abandoned_registrants(cvent_token, event_id):
    headers = {"Authorization": f"Bearer {cvent_token}"}
    registrants = []
    next_token = None

    while True:
        # Filter by abandoned status AND last modified in last 24 hours
        params = {
            "filter": f"status eq 'Abandoned' and lastModified gt '{SINCE}'",
            "limit": 100,
        }
        if next_token:
            params["token"] = next_token

        resp = requests.get(
            f"{CVENT_BASE_URL}/event-management/v1/events/{event_id}/invitees",
            headers=headers,
            params=params,
        )

        if resp.status_code == 404:
            return []

        resp.raise_for_status()
        data = resp.json()
        registrants.extend(data.get("data", []))

        next_token = data.get("paging", {}).get("nextToken")
        if not next_token:
            break

    return registrants


# ── Marketo auth ──────────────────────────────────────────────────────────────

def get_marketo_token():
    resp = requests.get(
        f"{MARKETO_BASE_URL}/identity/oauth/token",
        params={
            "grant_type": "client_credentials",
            "client_id": MARKETO_CLIENT_ID,
            "client_secret": MARKETO_CLIENT_SECRET,
        },
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("No access_token in Marketo auth response")
    print("✅ Marketo auth OK")
    return token


# ── Marketo: check if lead exists ────────────────────────────────────────────

def find_marketo_lead(marketo_token, email):
    resp = requests.get(
        f"{MARKETO_BASE_URL}/rest/v1/leads.json",
        headers={"Authorization": f"Bearer {marketo_token}"},
        params={
            "filterType": "email",
            "filterValues": email,
            "fields": "id,email,firstName,lastName",
        },
    )
    resp.raise_for_status()
    results = resp.json().get("result", [])
    return results[0] if results else None


# ── Marketo: create or update lead ───────────────────────────────────────────

def upsert_marketo_lead(marketo_token, email, first_name, last_name, event_title):
    resp = requests.post(
        f"{MARKETO_BASE_URL}/rest/v1/leads.json",
        headers={"Authorization": f"Bearer {marketo_token}"},
        json={
            "action": "createOrUpdate",
            "lookupField": "email",
            "input": [{
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "freefielduniqueurl": event_title,
            }],
        },
    )
    resp.raise_for_status()
    result = resp.json().get("result", [{}])[0]
    lead_id = result.get("id")
    status  = result.get("status")
    print(f"   ➕ Upserted {email} → Marketo ID {lead_id} (status: {status})")
    return lead_id


# ── Marketo: add to static list ───────────────────────────────────────────────

def add_to_marketo_list(marketo_token, list_id, lead_ids):
    if not lead_ids:
        return
    resp = requests.post(
        f"{MARKETO_BASE_URL}/rest/v1/lists/{list_id}/leads.json",
        headers={"Authorization": f"Bearer {marketo_token}"},
        json={"input": [{"id": lid} for lid in lead_ids]},
    )
    resp.raise_for_status()
    print(f"   📋 Added {len(lead_ids)} lead(s) to Marketo list {list_id}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"Cvent Abandoned Reg Sync — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Pulling abandons since: {SINCE}")
    print(f"{'='*60}\n")

    cvent_token   = get_cvent_token()
    marketo_token = get_marketo_token()

    events = get_active_events(cvent_token)
    if not events:
        print("No active events found — exiting.")
        sys.exit(0)

    total_upserted = 0
    total_listed   = 0
    total_skipped  = 0

    for event in events:
        event_id    = event.get("id")
        event_title = event.get("title", event_id)
        print(f"\n📌 Event: {event_title} ({event_id})")

        abandoned = get_abandoned_registrants(cvent_token, event_id)
        if not abandoned:
            print("   No abandoned registrants in last 24hrs.")
            continue

        print(f"   Found {len(abandoned)} abandoned registrant(s)")

        for person in abandoned:
            email      = (person.get("email") or "").strip().lower()
            first_name = person.get("firstName", "")
            last_name  = person.get("lastName", "")

            if not email:
                print(f"   ⚠️  Skipping record with no email")
                total_skipped += 1
                continue

            print(f"   → Processing {email}")

            # Upsert lead and write event title to freefielduniqueurl
            lead_id = upsert_marketo_lead(
                marketo_token, email, first_name, last_name, event_title
            )

            if lead_id:
                add_to_marketo_list(marketo_token, MARKETO_LIST_ID, [lead_id])
                total_upserted += 1
                total_listed   += 1

    print(f"\n{'='*60}")
    print(f"✅ Done — Upserted: {total_upserted} | Added to list: {total_listed} | Skipped: {total_skipped}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
