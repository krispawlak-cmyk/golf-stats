#!/usr/bin/env python3
"""
zepp_golf_sync.py — Golf Stats Dashboard Sync
===============================================
Pulls golf round data from Zepp API and writes to Google Sheets.
Account uses Google Sign-In (no Zepp email/password needed).

How Zepp + Google auth works:
  1. You sign into Zepp with Google
  2. Zepp stores a session token linked to your Google account
  3. We exchange a Google service token for a Zepp session
  4. Pull golf round data from Zepp API

Environment variables (set as GitHub Secrets):
  GOOGLE_CREDS_JSON  — Service account JSON key (for Sheets write access)
  GOOGLE_SHEET_ID    — Your Google Sheet ID
  ZEPP_GOOGLE_EMAIL  — Your Google email used to sign into Zepp

Run:
  pip install requests gspread google-auth
  python zepp_golf_sync.py
"""

import os
import json
import time
import requests
import gspread
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials

# ── CONFIG ────────────────────────────────────────────────────────────────────
ZEPP_API  = "https://api-mifit-us2.zepp.com"
ZEPP_APP_ID  = "2882303761517612588"
ZEPP_APP_KEY = "vvickQss4RGsGf09"

SHEET_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# ── ZEPP AUTH via Huami/Amazfit backend ──────────────────────────────────────
class ZeppClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            "Content-Type": "application/json",
        })
        self.access_token = None
        self.user_id = None

    def login_with_google_token(self, google_token: str) -> bool:
        """
        Authenticate with Zepp using a Google ID token.
        This is the path used when you sign in with Google in the Zepp app.
        """
        print("🔐 Authenticating with Zepp via Google token...")
        url = "https://account.huami.com/v2/client/login"
        payload = {
            "app_name":    "com.huami.activity",
            "app_version": "8.0.0",
            "country_code": "CA",
            "device_id":   "02:00:00:00:00:00",
            "device_model": "iPhone",
            "grant_type":  "access_token",
            "third_name":  "google",
            "access_token": google_token,
            "tz": "America/Toronto",
        }
        try:
            r = self.session.post(url, data=payload, timeout=15)
            data = r.json()
            if data.get("result") == "ok":
                self.access_token = data["token_info"]["access_token"]
                self.user_id = data["user_id"]
                print(f"✅ Zepp auth OK — user {self.user_id}")
                return True
            print(f"❌ Auth failed: {data.get('message','unknown')}")
        except Exception as e:
            print(f"❌ Auth error: {e}")
        return False

    def login_with_email(self, email: str, password: str) -> bool:
        """
        Login with Zepp/Amazfit email+password using the new v2/registrations/tokens endpoint.
        Key fix: specific headers required (User-Agent, Origin, Referer, app_name).
        Source: codeberg.org/argrento/huami-token/issues/119
        """
        import hashlib
        md5pw = hashlib.md5(password.encode()).hexdigest()

        # New endpoint (as of Oct 2025 Zepp changed from /v2/client/login)
        auth_url = "https://api-user.zepp.com/v2/registrations/tokens"

        # These specific headers are required — without them you get 0100
        headers = {
            "User-Agent":   "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "Origin":       "https://user.zepp.com",
            "Referer":      "https://user.zepp.com/",
            "app_name":     "com.huami.webapp",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        payload = {
            "email":        email,
            "password":     md5pw,
            "app_name":     "com.huami.activity",
            "app_version":  "8.0.0",
            "country_code": "CA",
            "device_id":    "02:00:00:00:00:00",
            "device_model": "iPhone14,3",
            "grant_type":   "password",
            "third_name":   "zepp",
            "tz":           "America/Toronto",
            "lang":         "en",
        }

        print(f"🔐 Authenticating with Zepp (new API)...")
        try:
            r = self.session.post(
                auth_url,
                data=payload,
                headers=headers,
                allow_redirects=False,
                timeout=15
            )
            print(f"   Status: {r.status_code}")

            # New API returns 303 redirect on success with token in Location header
            if r.status_code in (200, 201, 303):
                try:
                    data = r.json()
                except:
                    data = {}

                # Try JSON response first
                access_token = (
                    data.get("access_token") or
                    data.get("data", {}).get("access_token") or
                    data.get("token_info", {}).get("access_token")
                )
                user_id = (
                    data.get("user_id") or
                    data.get("data", {}).get("userId") or
                    data.get("userId")
                )

                # Check Location header for token (303 redirect pattern)
                if not access_token and r.status_code == 303:
                    location = r.headers.get("Location", "")
                    print(f"   Redirect location: {location[:80]}")
                    import urllib.parse as up
                    params = dict(up.parse_qsl(up.urlparse(location).query))
                    access_token = params.get("access_token") or params.get("token")
                    user_id = params.get("user_id") or params.get("userId")

                if access_token:
                    self.access_token = access_token
                    self.user_id = user_id or "unknown"
                    print(f"✅ Zepp auth OK — user {self.user_id}")
                    return True
                else:
                    print(f"   ❌ No token in response: {str(data)[:200]}")

            elif r.status_code == 400:
                try:
                    data = r.json()
                    code = data.get("error_code") or data.get("code")
                    print(f"   ❌ Error code {code}: {data.get('message','')}")
                    if code == "0100":
                        print("   → Credentials rejected. Check email/password are correct.")
                    elif code == "0117":
                        print("   → Account not found. Check email matches Zepp account.")
                except:
                    print(f"   ❌ Response: {r.text[:200]}")
            else:
                print(f"   ❌ Unexpected status {r.status_code}: {r.text[:200]}")

        except Exception as e:
            print(f"   ❌ Request error: {e}")

        # Fallback: try old endpoint with amazfit method
        print("🔐 Trying fallback endpoint...")
        try:
            old_url = "https://account.huami.com/v2/client/login"
            old_headers = {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
                "Origin":     "https://user.zepp.com",
                "Referer":    "https://user.zepp.com/",
                "app_name":   "com.huami.webapp",
            }
            old_payload = {
                "app_name":    "com.huami.activity",
                "app_version": "8.0.0",
                "country_code":"CA",
                "device_id":   "02:00:00:00:00:00",
                "device_model":"iPhone14,3",
                "grant_type":  "password",
                "third_name":  "zepp",
                "user_name":   email,
                "password":    md5pw,
                "tz":          "America/Toronto",
                "lang":        "en",
            }
            r2 = self.session.post(old_url, data=old_payload, headers=old_headers, timeout=15)
            print(f"   Fallback status: {r2.status_code}")
            data2 = r2.json()
            if data2.get("result") == "ok":
                self.access_token = data2["token_info"]["access_token"]
                self.user_id = data2.get("user_id", "unknown")
                print(f"✅ Fallback auth OK")
                return True
            print(f"   Fallback failed: {data2.get('error_code','?')} {data2.get('message','')}")
        except Exception as e:
            print(f"   Fallback error: {e}")

        return False




# ── DATA PARSING ──────────────────────────────────────────────────────────────
def parse_round(raw: dict) -> dict:
    def g(*keys, default=None):
        v = raw
        for k in keys:
            v = v.get(k) if isinstance(v, dict) else None
            if v is None:
                return default
        return v

    ts = g("start_time") or g("startTime") or g("trackTime") or 0
    if isinstance(ts, (int, float)) and ts > 1e9:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
    else:
        from datetime import datetime
        date_str = str(ts)[:10] if ts else datetime.now().strftime("%Y-%m-%d")

    score  = int(g("score") or g("totalScore") or g("total_score") or 0)
    par    = int(g("par") or g("coursePar") or g("course_par") or 72)
    holes  = int(g("holes") or g("holeCount") or g("hole_count") or 18)
    course = str(g("courseName") or g("course_name") or g("course", "name") or "Unknown Course")
    gir    = int(g("gir") or g("greensInRegulation") or 0)
    putts  = int(g("totalPutts") or g("total_putts") or g("putts") or 0)
    cal    = int(g("calorie") or g("calories") or 0)
    dist   = float(g("distance") or 0)
    hr_avg = int(g("heartRate", "average") or g("avg_heart_rate") or 0)
    dur    = int(g("duration") or g("totalTime") or 0)
    swings = int(g("totalSwings") or g("total_swings") or score or 0)

    return {
        "Round ID":      str(g("trackId") or g("track_id") or g("id") or ""),
        "Date":          date_str,
        "Course":        course,
        "Holes":         holes,
        "Score":         score,
        "Par":           par,
        "Score to Par":  score - par if score and par else 0,
        "GIR":           gir,
        "Total Putts":   putts,
        "Swings":        swings,
        "Distance (km)": round(dist / 1000, 2) if dist else 0,
        "Calories":      cal,
        "Avg HR":        hr_avg,
        "Duration (min)":round(dur / 60) if dur else 0,
        "Source":        "Zepp/Balance2",
    }


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
SHEET_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

def get_sheets_client(creds_json: str):
    import json, gspread
    from google.oauth2.service_account import Credentials
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SHEET_SCOPES)
    return gspread.authorize(creds)

def upsert_tab(ss, tab_name, headers, rows, key_col=None):
    import gspread, time
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=500, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        print(f"  📋 Created tab: {tab_name}", flush=True)

    if not rows:
        print(f"  ⚠️  No rows for {tab_name}", flush=True)
        return 0

    existing = ws.get_all_records() if key_col else []
    existing_keys = {str(r.get(key_col, "")) for r in existing}
    new_rows = [r for r in rows if not key_col or str(r.get(key_col, "")) not in existing_keys]

    if not new_rows:
        print(f"  ✅ {tab_name}: all {len(rows)} rows already synced", flush=True)
        return 0

    if not existing:
        ws.clear()
        ws.append_row(headers, value_input_option="RAW")

    for row in new_rows:
        ws.append_row([row.get(h, "") for h in headers], value_input_option="USER_ENTERED")
        time.sleep(0.1)

    print(f"  ✅ {tab_name}: added {len(new_rows)} new rows", flush=True)
    return len(new_rows)


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    import sys, os
    from datetime import datetime
    print("=" * 55, flush=True)
    print("⛳  Zepp Golf → Google Sheets Sync", flush=True)
    print(f"    {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    print(f"   Python {sys.version.split()[0]}", flush=True)
    print("=" * 55, flush=True)

    creds_raw  = os.environ.get("GOOGLE_CREDS_JSON", "")
    sheet_id   = os.environ.get("GOOGLE_SHEET_ID", "")
    zepp_email = os.environ.get("ZEPP_EMAIL", "")
    zepp_pass  = os.environ.get("ZEPP_PASSWORD", "")

    print(f"   GOOGLE_CREDS_JSON: {'✅' if creds_raw else '❌ MISSING'}", flush=True)
    print(f"   GOOGLE_SHEET_ID:   {'✅' if sheet_id else '❌ MISSING'}", flush=True)
    print(f"   ZEPP_EMAIL:        {'✅' if zepp_email else '❌ MISSING'}", flush=True)
    print(f"   ZEPP_PASSWORD:     {'✅' if zepp_pass else '❌ MISSING'}", flush=True)

    if not creds_raw or not sheet_id:
        raise EnvironmentError("Missing GOOGLE_CREDS_JSON or GOOGLE_SHEET_ID")

    # Auth with Zepp
    client = ZeppClient()
    authenticated = False

    if zepp_email and zepp_pass:
        authenticated = client.login_with_email(zepp_email, zepp_pass)

    if not authenticated:
        print("\n⚠️  Could not authenticate with Zepp.", flush=True)
        print("   Exiting — will retry on next scheduled run.", flush=True)
        return

    # Fetch rounds
    raw_rounds = client.get_golf_rounds(limit=200)
    if not raw_rounds:
        print("⚠️  No golf rounds returned from Zepp API.", flush=True)
        return

    print(f"\n📊 Parsing {len(raw_rounds)} rounds...", flush=True)
    parsed_rounds = [parse_round(r) for r in raw_rounds]
    parsed_rounds = [r for r in parsed_rounds if r["Score"] > 0]

    # Write to Sheets
    print(f"\n📤 Syncing to Google Sheet...", flush=True)
    gc = get_sheets_client(creds_raw)
    ss = gc.open_by_key(sheet_id)

    round_headers = [
        "Round ID", "Date", "Course", "Holes", "Score", "Par",
        "Score to Par", "GIR", "Total Putts", "Swings",
        "Distance (km)", "Calories", "Avg HR", "Duration (min)", "Source",
    ]

    upsert_tab(ss, "Rounds", round_headers, parsed_rounds, key_col="Round ID")

    print(f"\n✅ Sync complete! {len(parsed_rounds)} rounds processed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n💥 Fatal error: {e}", flush=True)
        traceback.print_exc()
        raise
