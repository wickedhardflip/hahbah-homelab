"""One-time Eero sign-in. Run this yourself in a terminal (not through Claude):

    python homelab/tools/eero_login.py

It asks for the email or phone on your Eero account, Eero sends you a code, you type the code,
and the session token is saved straight to Windows Credential Manager ("eero-api" / "session").
The token is never printed or written to disk. This uses the Eero app's unofficial cloud API.
"""
import getpass
import json
import urllib.error
import urllib.request

import keyring

API = "https://api-user.e2ro.com/2.2"
UA = "homelab-portal/0.1"


def post(path, payload, token=None):
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if token:
        headers["Cookie"] = f"s={token}"
    req = urllib.request.Request(API + path, json.dumps(payload).encode(), headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def main():
    login = input("Eero account email or phone (e.g. +15551234567): ").strip()
    try:
        token = post("/login", {"login": login})["data"]["user_token"]
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Eero refused that login (HTTP {e.code}). Accounts that use 'Login with Amazon' can't "
                         "sign in this way: invite a separate email as a network admin in the Eero app and use that.")
    code = getpass.getpass("Verification code from Eero (hidden as you type): ").strip()
    try:
        post("/login/verify", {"code": code}, token)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Eero didn't accept that code (HTTP {e.code}). Run the script again for a fresh code.")
    keyring.set_password("eero-api", "session", token)
    print("Signed in. Token saved to Windows Credential Manager as 'eero-api'. You can tell Claude it's done.")


if __name__ == "__main__":
    main()
