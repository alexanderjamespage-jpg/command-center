#!/usr/bin/env python3
"""
Infinite Campus scraper for Paradigm High School.

Two modes:
  python ic_scraper.py --discover   -> log in, probe endpoints, report what works
  python ic_scraper.py              -> log in, pull grades, write grades.json

Credentials come from a .env file in the same directory:
  IC_USERNAME=yourusername
  IC_PASSWORD=yourpassword

Do NOT commit .env. Add it to .gitignore.
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = "https://paradigmut.infinitecampus.org/campus"
APP_NAME = "paradigm"
HERE = Path(__file__).parent
OUT = HERE / "grades.json"

# Campus Student app user-agent. IC sometimes behaves differently for
# unrecognised clients, so we present as a normal browser.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def load_env():
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

    user = os.environ.get("IC_USERNAME")
    pw = os.environ.get("IC_PASSWORD")
    if not user or not pw:
        sys.exit("Missing IC_USERNAME / IC_PASSWORD. Create a .env file next to this script.")
    return user, pw


def login(session, username, password):
    """
    Authenticate against verify.jsp. On success IC sets a JSESSIONID cookie.
    Returns True if the session looks authenticated.
    """
    url = f"{BASE}/verify.jsp"
    params = {
        "nonBrowser": "true",
        "username": username,
        "password": password,
        "appName": APP_NAME,
    }
    r = session.get(url, params=params, timeout=20)

    body = r.text.lower()
    # IC returns 200 even on bad credentials, so inspect the body.
    if "password-error" in body or "invalid" in body or "authentication failed" in body:
        return False
    if "success" in body or session.cookies.get("JSESSIONID"):
        return True
    return False


# Candidate endpoints, newest API style first. The discovery pass tells us
# which of these your build actually serves.
CANDIDATES = [
    # Modern REST-ish portal API (Campus 2020+)
    "/resources/portal/students",
    "/resources/portal/roster",
    "/resources/portal/grades",
    "/resources/portal/assignment/student",
    "/resources/portal/schedule",
    "/resources/portal/notification",
    "/api/portal/students",
    "/resources/my/demographics",
    # Legacy prism endpoints (pre-2020 builds)
    f"/prism?x=portal.PortalOutline&appName={APP_NAME}",
    "/prism?x=portal.PortalClassbook-getClassbookForAllSections&mode=classbook",
]


def discover(session):
    print(f"\nProbing {len(CANDIDATES)} endpoints...\n")
    working = []

    for path in CANDIDATES:
        url = f"{BASE}{path}"
        try:
            r = session.get(url, timeout=20)
        except requests.RequestException as e:
            print(f"  ERR   {path}  ({e.__class__.__name__})")
            continue

        ctype = r.headers.get("content-type", "")
        size = len(r.content)

        if r.status_code != 200:
            print(f"  {r.status_code}   {path}")
            continue

        # A login page served at 200 means the session isn't valid for this path.
        if "text/html" in ctype and "password" in r.text.lower():
            print(f"  AUTH  {path}  (bounced to login)")
            continue

        kind = "json" if "json" in ctype else ("xml" if "xml" in ctype else ctype[:24])
        print(f"  OK    {path}  [{kind}, {size}b]")
        working.append((path, r))
        time.sleep(0.3)  # be polite

    if not working:
        print("\nNothing responded. See notes at the bottom of this file.")
        return

    dump_dir = HERE / "discovery"
    dump_dir.mkdir(exist_ok=True)
    for path, r in working:
        name = path.strip("/").replace("/", "_").replace("?", "_").replace("&", "_")[:80]
        (dump_dir / f"{name}.txt").write_text(r.text[:200000])

    print(f"\nSaved {len(working)} responses to {dump_dir}/")
    print("Send me those and I'll write the parser against the real shapes.")


def scrape(session):
    """
    Fill this in once discovery tells us which endpoints work.
    Writes a normalised structure the dashboard can read directly.
    """
    r = session.get(f"{BASE}/resources/portal/grades", timeout=20)
    r.raise_for_status()
    raw = r.json()

    courses = []
    for term in raw if isinstance(raw, list) else raw.get("terms", []):
        for course in term.get("courses", []):
            courses.append({
                "name": course.get("courseName"),
                "teacher": course.get("teacherDisplay"),
                "period": course.get("periodName"),
                "grade": course.get("gradingTasks", [{}])[0].get("progressScore"),
                "percent": course.get("gradingTasks", [{}])[0].get("progressPercent"),
            })

    payload = {"fetched": int(time.time()), "courses": courses}
    OUT.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(courses)} courses to {OUT}")


def main():
    username, password = load_env()

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    print("Logging in...")
    if not login(session, username, password):
        sys.exit("Login failed. Check credentials, and watch for a CAPTCHA after ~5 bad attempts.")
    print("Session established.")

    if "--discover" in sys.argv:
        discover(session)
    else:
        scrape(session)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# NOTES
#
# If discovery comes back empty across the board, the likely cause is that
# newer Campus builds issue a bearer token during login rather than relying
# on a plain session cookie. The fix is to log in once with Playwright, open
# devtools -> Network, and watch which XHR calls the portal fires when you
# click into Grades. Those are the real endpoints. Send me the paths and
# we'll skip the guessing.
#
# Rate limiting: run this every 30 minutes at most. IC shows a CAPTCHA after
# roughly five failed logins, and a tight loop against a school SIS from an
# unfamiliar IP is the kind of thing that gets an account locked.
# ---------------------------------------------------------------------------
