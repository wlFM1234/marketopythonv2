"""
cventabandoned.py
-----------------
Triggered by GitHub Actions repository_dispatch from Cloudflare Worker relay.
Reads abandoned registrant data from the dispatch client_payload sent by Cvent,
then for each person:
  - Creates or updates lead in Marketo
  - Writes event title to freefielduniqueurl
  - Adds to static list

Required GitHub secrets:
  MARKETO_BASE_URL
  MARKETO_CLIENT_ID
  MARKETO_CLIENT_SECRET
  MARKETO_ABANDONED_REG_LIST_ID
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

MARKETO_BASE_URL      = os.environ["MARKETO_BASE_URL"].rstrip("/")
MARKETO_CLIENT_ID     = os.environ["MARKETO_CLIENT_ID"]
MARKETO_CLIENT_SECRET = os.environ["MARKETO_CLIENT_SECRET"]
MARKETO_LIST_ID       = os.environ["MARKETO_ABANDONED_REG_LIST_ID"]

# GitHub Actions passes client_payload as a JSON string in the PAYLOAD env var
# (set in the workflow yml via ${{ toJson(github.event.client_payload) }})
PAYLOAD = json.loads(os.environ["PAYLOAD"])


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


# ── Marketo: upsert lead ──────────────────────────────────────────────────────

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
    print(f"{'='*60}\n")

    event_type = PAYLOAD.get("eventType", "unknown")
    invitees   = PAYLOAD.get("message", [])

    print(f"Event type: {event_type}")
    print(f"Invitees in payload: {len(invitees)}")

    if not invitees:
        print("No invitees in payload — exiting.")
        sys.exit(0)

    marketo_token = get_marketo_token()

    total_upserted = 0
    total_listed   = 0
    total_skipped  = 0

    for person in invitees:
        email      = (person.get("email") or "").strip().lower()
        first_name = person.get("firstName", "")
        last_name  = person.get("lastName", "")
        event_title = person.get("eventForInvitee", {}).get("eventTitle", "")

        if not email:
            print(f"   ⚠️  Skipping record with no email")
            total_skipped += 1
            continue

        print(f"   → Processing {email} (event: {event_title})")

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
