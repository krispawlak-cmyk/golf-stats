#!/usr/bin/env python3
"""
zepp_golf_sync.py — Golf Stats Dashboard Sync
Pulls golf rounds from Zepp API → Google Sheets
"""
import os, json, time, hashlib, requests, gspread
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials

SHEET_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

class ZeppClient:
    def __init__(self):
        self.session = requests.Session()
        self.access_token = None
        self.user_id = None

    def login(self, email, password):
        md5pw = hashlib.md5(password.encode()).hexdigest()

        attempts = [
            # 1. New Zepp API — JSON body
            {
                "label": "new API / JSON",
                "url": "https://api-user.zepp.com/v2/registrations/tokens",
                "json": {
                    "email": email, "password": md5pw,
                    "app_name": "com.huami.activity", "app_version": "8.0.0",
                    "country_code": "CA", "device_id": "02:00:00:00:00:00",
                    "device_model": "iPhone14,3", "grant_type": "password",
                    "third_name": "zepp", "tz": "America/Toronto", "lang": "en",
                },
                "headers": {
                    "User-Agent": "Zepp/8.0.0 (iPhone; iOS 17.0)",
                    "Origin": "https://user.zepp.com",
                    "Referer": "https://user.zepp.com/",
                    "Content-Type": "application/json",
                    "app_name": "com.huami.webapp",
                },
            },
            # 2. New Zepp API — form data
            {
                "label": "new API / form",
                "url": "https://api-user.zepp.com/v2/registrations/tokens",
                "data": {
                    "email": email, "password": md5pw,
                    "app_name": "com.huami.activity", "app_version": "8.0.0",
                    "country_code": "CA", "device_id": "02:00:00:00:00:00",
                    "device_model": "iPhone14,3", "grant_type": "password",
                    "third_name": "zepp", "tz": "America/Toronto", "lang": "en",
                },
                "headers": {
                    "User-Agent": "Zepp/8.0.0 (iPhone; iOS 17.0)",
                    "Origin": "https://user.zepp.com",
                    "Referer": "https://user.zepp.com/",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "app_name": "com.huami.webapp",
                },
            },
            # 3. Legacy Huami — zepp third_name
            {
                "label": "legacy / zepp",
                "url": "https://account.huami.com/v2/client/login",
                "data": {
                    "app_name": "com.huami.activity", "app_version": "8.0.0",
                    "country_code": "CA", "device_id": "02:00:00:00:00:00",
                    "device_model": "iPhone14,3", "grant_type": "password",
                    "third_name": "zepp", "user_name": email, "password": md5pw,
                    "tz": "America/Toronto", "lang": "en",
                },
                "headers": {
                    "User-Agent": "Zepp/8.0.0 (iPhone; iOS 17.0)",
                    "Origin": "https://user.zepp.com",
                    "Referer": "https://user.zepp.com/",
                    "app_name": "com.huami.webapp",
                },
            },
            # 4. Legacy Huami — amazfit third_name
            {
                "label": "legacy / amazfit",
                "url": "https://account.huami.com/v2/client/login",
                "data": {
                    "app_name": "com.huami.activity", "app_version": "8.0.0",
                    "country_code": "CA", "device_id": "02:00:00:00:00:00",
                    "device_model": "iPhone14,3", "grant_type": "password",
                    "third_name": "amazfit", "user_name": email, "password": md5pw,
                    "tz": "America/Toronto", "lang": "en",
                },
                "headers": {
                    "User-Agent": "Zepp/8.0.0 (iPhone; iOS 17.0)",
                    "Origin": "https://user.zepp.com",
                    "Referer": "https://user.zepp.com/",
                    "app_name": "com.huami.webapp",
                },
            },
            # 5. MiFit US2 direct
            {
                "label": "mifit-us2 direct",
                "url": "https://api-mifit-us2.zepp.com/v1/account/token",
                "json": {"email": email, "password": md5pw},
                "headers": {
                    "User-Agent": "Zepp/8.0.0 (iPhone; iOS 17.0)",
                    "Content-Type": "application/json",
                },
            },
        ]

        for a in attempts:
            print(f"\n🔐 Trying: {a['label']} → {a['url']}", flush=True)
            try:
                kwargs = {
                    "headers": a.get("headers", {}),
                    "allow_redirects": False,
                    "timeout": 15,
                }
                if "json" in a:
                    kwargs["json"] = a["json"]
                else:
                    kwargs["data"] = a["data"]

                r = self.session.post(a["url"], **kwargs)
                print(f"   Status: {r.status_code}", flush=True)
                print(f"   Response: {r.text[:200]!r}", flush=True)

                # Check redirect
                if r.status_code == 303:
                    loc = r.headers.get("Location", "")
                    print(f"   Redirect: {loc[:120]}", flush=True)
                    import urllib.parse as up
                    params = dict(up.parse_qsl(up.urlparse(loc).query))
                    tok = params.get("access_token") or params.get("token")
                    if tok:
                        self.access_token = tok
                        self.user_id = params.get("user_id", "unknown")
                        print(f"✅ Auth OK via redirect!", flush=True)
                        return True

                if r.status_code in (200, 201):
                    try:
                        d = r.json()
                    except:
                        continue
                    tok = (
                        d.get("access_token") or
                        (d.get("token_info") or {}).get("access_token") or
                        (d.get("data") or {}).get("access_token")
                    )
                    uid = d.get("user_id") or (d.get("data") or {}).get("userId") or "unknown"
                    if d.get("result") == "ok":
                        tok = d["token_info"]["access_token"]
                        uid = d.get("user_id", "unknown")
                    if tok:
                        self.access_token = tok
                        self.user_id = uid
                        print(f"✅ Auth OK!", flush=True)
                        return True

            except Exception as e:
                print(f"   Error: {e}", flush=True)

        return False

    def get_golf_rounds(self, limit=100):
        headers = {"apptoken": self.access_token}
        for endpoint in [
            f"https://api-mifit-us2.zepp.com/v1/sport/record/list?sport_type=17&limit={limit}&page=1",
            f"https://api-mifit-us2.zepp.com/v1/workout/records?type=17&limit={limit}",
        ]:
            try:
                r = self.session.get(endpoint, headers=headers, timeout=15)
                print(f"   Rounds endpoint {r.status_code}: {endpoint}", flush=True)
                if r.status_code == 200:
                    d = r.json()
                    rounds = d.get("data", {}).get("records") or d.get("data", {}).get("summary") or []
                    if rounds:
                        print(f"   Found {len(rounds)} rounds", flush=True)
                        return rounds
            except Exception as e:
                print(f"   Rounds error: {e}", flush=True)
        return []


def parse_round(raw):
    def g(*keys, default=None):
        v = raw
        for k in keys:
            v = v.get(k) if isinstance(v, dict) else None
            if v is None: return default
        return v

    ts = g("start_time") or g("startTime") or 0
    if isinstance(ts, (int, float)) and ts > 1e9:
        dt = datetime.fromtimestamp(ts/1000 if ts>1e12 else ts, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d")
    else:
        date_str = datetime.now().strftime("%Y-%m-%d")

    return {
        "Round ID":       str(g("trackId") or g("track_id") or g("id") or ""),
        "Date":           date_str,
        "Course":         str(g("courseName") or g("course_name") or "Unknown"),
        "Holes":          int(g("holes") or g("holeCount") or 18),
        "Score":          int(g("score") or g("totalScore") or 0),
        "Par":            int(g("par") or g("coursePar") or 72),
        "Score to Par":   int(g("score") or 0) - int(g("par") or 72),
        "GIR":            int(g("gir") or 0),
        "Total Putts":    int(g("totalPutts") or g("putts") or 0),
        "Swings":         int(g("totalSwings") or 0),
        "Distance (km)":  round(float(g("distance") or 0)/1000, 2),
        "Calories":       int(g("calorie") or g("calories") or 0),
        "Avg HR":         int(g("heartRate","average") or 0),
        "Duration (min)": round(int(g("duration") or 0)/60),
        "Source":         "Zepp/Balance2",
    }


def get_sheets_client(creds_json):
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SHEET_SCOPES)
    return gspread.authorize(creds)


def upsert_tab(ss, tab_name, headers, rows, key_col=None):
    try:
        ws = ss.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab_name, rows=500, cols=len(headers))
        ws.append_row(headers)

    if not rows:
        print(f"  ⚠️  No rows for {tab_name}", flush=True)
        return 0

    existing_keys = set()
    if key_col:
        existing_keys = {str(r.get(key_col,"")) for r in ws.get_all_records()}

    new_rows = [r for r in rows if str(r.get(key_col,"")) not in existing_keys] if key_col else rows

    if not new_rows:
        print(f"  ✅ {tab_name}: already up to date", flush=True)
        return 0

    for row in new_rows:
        ws.append_row([row.get(h,"") for h in headers], value_input_option="USER_ENTERED")
        time.sleep(0.1)

    print(f"  ✅ {tab_name}: added {len(new_rows)} rows", flush=True)
    return len(new_rows)


def main():
    print("="*55, flush=True)
    print("⛳  Zepp Golf → Google Sheets Sync", flush=True)
    print(f"    {datetime.now().strftime('%Y-%m-%d %H:%M')}", flush=True)
    print("="*55, flush=True)

    creds_raw  = os.environ.get("GOOGLE_CREDS_JSON","")
    sheet_id   = os.environ.get("GOOGLE_SHEET_ID","")
    zepp_email = os.environ.get("ZEPP_EMAIL","")
    zepp_pass  = os.environ.get("ZEPP_PASSWORD","")

    print(f"GOOGLE_CREDS_JSON: {'✅' if creds_raw else '❌'}", flush=True)
    print(f"GOOGLE_SHEET_ID:   {'✅' if sheet_id else '❌'}", flush=True)
    print(f"ZEPP_EMAIL:        {'✅' if zepp_email else '❌'}", flush=True)
    print(f"ZEPP_PASSWORD:     {'✅' if zepp_pass else '❌'}", flush=True)

    if not creds_raw or not sheet_id:
        raise EnvironmentError("Missing Google credentials")

    client = ZeppClient()
    if not client.login(zepp_email, zepp_pass):
        print("\n⚠️  All auth attempts failed.", flush=True)
        return

    raw = client.get_golf_rounds(200)
    if not raw:
        print("⚠️  No rounds found.", flush=True)
        return

    parsed = [parse_round(r) for r in raw if parse_round(r)["Score"] > 0]
    print(f"\n📤 Writing {len(parsed)} rounds to Sheet...", flush=True)

    gc = get_sheets_client(creds_raw)
    ss = gc.open_by_key(sheet_id)
    headers = ["Round ID","Date","Course","Holes","Score","Par","Score to Par",
               "GIR","Total Putts","Swings","Distance (km)","Calories","Avg HR","Duration (min)","Source"]
    upsert_tab(ss, "Rounds", headers, parsed, key_col="Round ID")
    print(f"\n✅ Done!", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        print(f"\n💥 {e}", flush=True)
        traceback.print_exc()
        raise
