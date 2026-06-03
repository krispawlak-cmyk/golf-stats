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
            "app_name":    "com.huami.watch.hmwatchmanager",
            "app_version": "6.7.1",
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
        """Fallback: direct email/password login (if user adds password to account)."""
        import hashlib
        md5pw = hashlib.md5(password.encode()).hexdigest()
        url = "https://account.huami.com/v2/client/login"
        payload = {
            "app_name":    "com.huami.watch.hmwatchmanager",
            "app_version": "6.7.1",
            "country_code": "CA",
            "device_id":   "02:00:00:00:00:00",
            "device_model": "iPhone",
            "grant_type":  "password",
            "third_name":  "huami",
            "user_name":   email,
            "password":    md5pw,
            "tz": "America/Toronto",
        }
        try:
            r = self.session.post(url, data=payload, timeout=15)
            data = r.json()
            if data.get("result") == "ok":
                self.access_token = data["token_info"]["access_token"]
                self.user_id = data["user_id"]
                print(f"✅ Zepp email auth OK")
                return True
            print(f"❌ Email auth failed: {data.get('message','unknown')}")
        except Exception as e:
            print(f"❌ Email auth error: {e}")
        return False

    def get_golf_rounds(self, limit=100):
        """Fetch golf activity records from Zepp."""
        if not self.access_token:
            raise RuntimeError("Not authenticated")

        headers = {"apptoken": self.access_token}
        rounds = []

        # Try sport records endpoint (sport_type=17 = Golf)
        try:
            r = self.session.get(
                f"{ZEPP_API}/v1/sport/record/list",
                headers=headers,
                params={"sport_type": 17, "limit": limit, "page": 1},
                timeout=15
            )
            if r.status_code == 200:
                data = r.json()
                rounds = data.get("data", {}).get("records", [])
                print(f"📊 Found {len(rounds)} golf rounds")
        except Exception as e:
            print(f"⚠️  Sport records failed: {e}")

        # Fallback: workout records
        if not rounds:
            try:
                r = self.session.get(
                    f"{ZEPP_API}/v1/workout/records",
                    headers=headers,
                    params={"type": 17, "limit": limit},
                    timeout=15
                )
                if r.status_code == 200:
                    data = r.json()
                    rounds = data.get("data", {}).get("summary", [])
                    print(f"📊 Found {len(rounds)} rounds via workout endpoint")
            except Exception as e:
                print(f"⚠️  Workout records failed: {e}")

        return rounds

    def get_round_detail(self, track_id):
        """Get hole-by-hole detail for a specific round."""
        headers = {"apptoken": self.access_token}
        try:
            r = self.session.get(
                f"{ZEPP_API}/v1/sport/record/detail",
                headers=headers,
                params={"track_id": track_id},
                timeout=15
            )
            if r.status_code == 200:
                return r.json().get("data", {})
        except Exception as e:
            print(f"⚠️  Detail fetch failed: {e}")
        return {}


# ── DATA PARSING ──────────────────────────────────────────────────────────────
def parse_round(raw: dict) -> dict:
    """Normalize a Zepp golf round record."""
    def g(*keys, default=None):
        v = raw
        for k in keys:
            v = v.get(k) if isinstance(v, dict) else None
            if v is None:
                return default
        return v

    # Timestamp → date string
    ts = g("start_time") or g("startTime") or g("trackTime") or 0
    if isinstance(ts, (int, float)) and ts > 1e9:
        dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
    else:
        date_str = str(ts)[:10] if ts else datetime.now().strftime("%Y-%m-%d")

    score    = int(g("score") or g("totalScore") or g("total_score") or 0)
    par      = int(g("par") or g("coursePar") or g("course_par") or 72)
    holes    = int(g("holes") or g("holeCount") or g("hole_count") or 18)
    course   = str(g("courseName") or g("course_name") or g("course", "name") or "Unknown Course")
    gir      = int(g("gir") or g("greensInRegulation") or g("greens_in_regulation") or 0)
    putts    = int(g("totalPutts") or g("total_putts") or g("putts") or 0)
    calories = int(g("calorie") or g("calories") or 0)
    distance = float(g("distance") or 0)  # meters walked
    hr_avg   = int(g("heartRate", "average") or g("avg_heart_rate") or 0)
    duration = int(g("duration") or g("totalTime") or g("total_time") or 0)  # seconds
    swings   = int(g("totalSwings") or g("total_swings") or score or 0)

    return {
        "Round ID":     str(g("trackId") or g("track_id") or g("id") or ""),
        "Date":         date_str,
        "Course":       course,
        "Holes":        holes,
        "Score":        score,
        "Par":          par,
        "Score to Par": score - par if score and par else 0,
        "GIR":          gir,
        "Total Putts":  putts,
        "Swings":       swings,
        "Distance (km)": round(distance / 1000, 2) if distance else 0,
        "Calories":     calories,
        "Avg HR":       hr_avg,
        "Duration (min)": round(duration / 60) if duration else 0,
        "Source":       "Zepp/Balance2",
    }


def parse_holes(track_id: str, detail: dict) -> list:
    """Extract hole-by-hole data."""
    holes_raw = detail.get("holes", detail.get("holeDetails", []))
    rows = []
    for h in holes_raw:
        hole_num = h.get("holeNumber") or h.get("hole_number") or 0
        rows.append({
            "Round ID":   track_id,
            "Hole":       int(hole_num),
            "Par":        int(h.get("par") or 3),
            "Score":      int(h.get("score") or h.get("strokes") or 0),
            "Putts":      int(h.get("putts") or h.get("puttCount") or 0),
            "GIR":        "Yes" if h.get("gir") or h.get("greenInRegulation") else "No",
            "FIR":        "Yes" if h.get("fairwayHit") or h.get("fairway_hit") else "No",
        })
    return rows


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def get_sheets_client(creds_json: str):
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SHEET_SCOPES)
    return gspread.authorize(creds)


def upsert_tab(ss, tab_name: str, headers: list, rows: list, key_col: str = None):
    """Write rows to a sheet tab, creating it if needed."""
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=500, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        print(f"  📋 Created tab: {tab_name}")

    if not rows:
        print(f"  ⚠️  No rows for {tab_name}")
        return 0

    existing = ws.get_all_records() if key_col else []
    existing_keys = {str(r.get(key_col, "")) for r in existing}

    new_rows = [r for r in rows if not key_col or str(r.get(key_col, "")) not in existing_keys]

    if not new_rows:
        print(f"  ✅ {tab_name}: all {len(rows)} rows already synced")
        return 0

    if not existing:
        ws.clear()
        ws.append_row(headers, value_input_option="RAW")

    for row in new_rows:
        ws.append_row([row.get(h, "") for h in headers], value_input_option="USER_ENTERED")
        time.sleep(0.1)  # rate limit friendly

    print(f"  ✅ {tab_name}: added {len(new_rows)} new rows")
    return len(new_rows)


def format_sheets(ss):
    """Apply dark green header formatting."""
    for tab_name in ["Rounds", "Holes"]:
        try:
            ws = ss.worksheet(tab_name)
            ws.freeze(rows=1)
            ws.format("1:1", {
                "backgroundColor": {"red": 0.05, "green": 0.16, "blue": 0.05},
                "textFormat": {"bold": True, "foregroundColor": {"red": 0.9, "green": 1.0, "blue": 0.9}},
            })
        except Exception:
            pass


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("⛳  Zepp Golf → Google Sheets Sync")
    print(f"    {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 55)

    # Read env vars
    creds_raw  = os.environ.get("GOOGLE_CREDS_JSON", "")
    sheet_id   = os.environ.get("GOOGLE_SHEET_ID", "")
    zepp_email = os.environ.get("ZEPP_EMAIL", "")       # optional, if email/pw added later
    zepp_pass  = os.environ.get("ZEPP_PASSWORD", "")    # optional
    zepp_token = os.environ.get("ZEPP_GOOGLE_TOKEN", "") # Google token for Zepp

    if not creds_raw or not sheet_id:
        raise EnvironmentError("Missing GOOGLE_CREDS_JSON or GOOGLE_SHEET_ID")

    # ── Authenticate with Zepp ──────────────────────────────────────────────
    client = ZeppClient()
    authenticated = False

    if zepp_email and zepp_pass:
        authenticated = client.login_with_email(zepp_email, zepp_pass)

    if not authenticated and zepp_token:
        authenticated = client.login_with_google_token(zepp_token)

    if not authenticated:
        print("\n⚠️  Could not authenticate with Zepp.")
        print("   Options:")
        print("   1. Add ZEPP_EMAIL + ZEPP_PASSWORD secrets (set a password in Zepp app)")
        print("   2. Add ZEPP_GOOGLE_TOKEN secret (see README for how to get this)")
        print("\n   Exiting without error — will retry on next scheduled run.")
        return

    # ── Fetch rounds ────────────────────────────────────────────────────────
    raw_rounds = client.get_golf_rounds(limit=200)

    if not raw_rounds:
        print("⚠️  No golf rounds returned from Zepp API.")
        print("   Make sure you have completed golf rounds in the Zepp app.")
        return

    # ── Parse data ──────────────────────────────────────────────────────────
    print(f"\n📊 Parsing {len(raw_rounds)} rounds...")
    parsed_rounds = [parse_round(r) for r in raw_rounds]
    parsed_rounds = [r for r in parsed_rounds if r["Score"] > 0]

    # Fetch hole detail for each round
    all_holes = []
    for rnd in parsed_rounds:
        rid = rnd["Round ID"]
        if rid:
            detail = client.get_round_detail(rid)
            holes = parse_holes(rid, detail)
            all_holes.extend(holes)
            time.sleep(0.3)

    # ── Write to Sheets ─────────────────────────────────────────────────────
    print(f"\n📤 Syncing to Google Sheet...")
    gc = get_sheets_client(creds_raw)
    ss = gc.open_by_key(sheet_id)

    round_headers = [
        "Round ID", "Date", "Course", "Holes", "Score", "Par",
        "Score to Par", "GIR", "Total Putts", "Swings",
        "Distance (km)", "Calories", "Avg HR", "Duration (min)", "Source",
    ]
    hole_headers = ["Round ID", "Hole", "Par", "Score", "Putts", "GIR", "FIR"]

    upsert_tab(ss, "Rounds", round_headers, parsed_rounds, key_col="Round ID")
    upsert_tab(ss, "Holes",  hole_headers,  all_holes,    key_col=None)
    format_sheets(ss)

    print(f"\n✅ Sync complete!")
    print(f"   Rounds synced: {len(parsed_rounds)}")
    print(f"   Hole records:  {len(all_holes)}")
    print(f"   Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}")


if __name__ == "__main__":
    main()
