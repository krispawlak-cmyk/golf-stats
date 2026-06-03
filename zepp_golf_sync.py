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


