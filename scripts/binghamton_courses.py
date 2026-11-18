#!/usr/bin/env python3
"""Scrape Binghamton University's public class schedule (Banner 9) to CSV/JSON.

Usage:
    python3 scripts/binghamton_courses.py                  # Spring 2027, all subjects
    python3 scripts/binghamton_courses.py --subject CS --subject MATH
    python3 scripts/binghamton_courses.py --descriptions   # also fetch course descriptions (slow)
    python3 scripts/binghamton_courses.py --list-terms     # show term codes

Standard library only. Writes <term>_courses.csv and <term>_courses.json.
"""

import argparse
import csv
import html
import http.cookiejar
import json
import random
import re
import string
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://ssb.cc.binghamton.edu:8484/StudentRegistrationSsb/ssb"
DEFAULT_TERM = "202720"  # Spring 2027
PAGE_SIZE = 500
DAYS = [("monday", "M"), ("tuesday", "T"), ("wednesday", "W"), ("thursday", "R"),
        ("friday", "F"), ("saturday", "S"), ("sunday", "U")]


class Banner:
    def __init__(self, delay=0.3):
        self.delay = delay
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (course-schedule-scraper)"),
            ("Accept", "application/json, text/javascript, */*; q=0.01"),
            ("X-Requested-With", "XMLHttpRequest"),
        ]
        self.session_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=5)) \
            + str(int(time.time() * 1000))

    def _req(self, path, params=None, data=None):
        url = f"{BASE}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        for attempt in range(4):
            try:
                with self.opener.open(url, data=body, timeout=60) as r:
                    text = r.read().decode("utf-8", "replace")
                time.sleep(self.delay)
                return text
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                print(f"  retry {path}: {e}", file=sys.stderr)
                time.sleep(2 ** attempt)

    def _json(self, *a, **kw):
        return json.loads(self._req(*a, **kw))

    def terms(self):
        return self._json("classSearch/getTerms", {"searchTerm": "", "offset": 1, "max": 50})

    def subjects(self, term):
        return self._json("classSearch/get_subject",
                          {"searchTerm": "", "term": term, "offset": 1, "max": 1000,
                           "uniqueSessionId": self.session_id})

    def select_term(self, term):
        self._req("term/termSelection", {"mode": "search"})
        self._req("term/search", {"mode": "search"},
                  {"term": term, "studyPath": "", "studyPathText": "",
                   "startDatepicker": "", "endDatepicker": "",
                   "uniqueSessionId": self.session_id})

    def reset(self):
        self._req("classSearch/resetDataForm", data={})

    def search(self, term, subject=None):
        """Yield every section for the term (optionally one subject), paging through results."""
        self.reset()
        offset, total = 0, None
        while total is None or offset < total:
            params = {"txt_term": term, "startDatepicker": "", "endDatepicker": "",
                      "uniqueSessionId": self.session_id, "pageOffset": offset,
                      "pageMaxSize": PAGE_SIZE, "sortColumn": "subjectDescription",
                      "sortDirection": "asc"}
            if subject:
                params["txt_subject"] = subject
            res = self._json("searchResults/searchResults", params)
            if not res.get("success", True) and not res.get("data"):
                raise RuntimeError(f"search failed: {res}")
            data = res.get("data") or []
            total = res.get("totalCount", 0)
            yield from data
            if not data:
                break
            offset += len(data)

    def description(self, term, crn):
        raw = self._req("searchResults/getCourseDescription",
                        data={"term": term, "courseReferenceNumber": crn})
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def fmt_time(t):
    if not t:
        return ""
    h, m = int(t[:2]), t[2:]
    return f"{(h % 12) or 12}:{m}{'am' if h < 12 else 'pm'}"


def flatten(s):
    meetings = []
    for mf in s.get("meetingsFaculty") or []:
        mt = mf.get("meetingTime") or {}
        days = "".join(code for key, code in DAYS if mt.get(key))
        times = f"{fmt_time(mt.get('beginTime'))}-{fmt_time(mt.get('endTime'))}" \
            if mt.get("beginTime") else "TBA"
        where = " ".join(x for x in (mt.get("building"), mt.get("room")) if x) or "TBA"
        meetings.append(f"{days or 'TBA'} {times} @ {where}")
    instructors = [f.get("displayName", "") for f in sorted(
        s.get("faculty") or [], key=lambda f: not f.get("primaryIndicator"))]
    credits = s.get("creditHours") if s.get("creditHours") is not None else s.get("creditHourLow")
    if s.get("creditHourHigh"):
        credits = f"{s.get('creditHourLow')}-{s.get('creditHourHigh')}"
    first = ((s.get("meetingsFaculty") or [{}])[0].get("meetingTime") or {})
    return {
        "crn": s.get("courseReferenceNumber"),
        "subject": s.get("subject"),
        "number": s.get("courseNumber"),
        "section": s.get("sequenceNumber"),
        "title": html.unescape(s.get("courseTitle") or ""),
        "credits": credits,
        "schedule_type": s.get("scheduleTypeDescription"),
        "instruction_method": s.get("instructionalMethodDescription"),
        "campus": s.get("campusDescription"),
        "instructors": "; ".join(instructors),
        "meetings": " | ".join(meetings),
        "start_date": first.get("startDate"),
        "end_date": first.get("endDate"),
        "capacity": s.get("maximumEnrollment"),
        "enrolled": s.get("enrollment"),
        "seats_available": s.get("seatsAvailable"),
        "waitlist_capacity": s.get("waitCapacity"),
        "waitlist_count": s.get("waitCount"),
        "open": s.get("openSection"),
        "attributes": "; ".join(a.get("description", "") for a in s.get("sectionAttributes") or []),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", default=DEFAULT_TERM, help="Banner term code (default 202720 = Spring 2027)")
    ap.add_argument("--subject", action="append", help="Subject code, e.g. CS (repeatable). Default: all")
    ap.add_argument("--descriptions", action="store_true", help="Fetch each course's description (one request per course)")
    ap.add_argument("--out", help="Output file prefix (default: <term>_courses)")
    ap.add_argument("--list-terms", action="store_true")
    args = ap.parse_args()

    b = Banner()
    if args.list_terms:
        for t in b.terms():
            print(t["code"], t["description"])
        return

    b.select_term(args.term)
    subjects = [s.upper() for s in args.subject] if args.subject else None

    rows, seen = [], set()
    if subjects is None:
        # Try one search over everything; some Banner setups require a subject, so fall back.
        try:
            raw = list(b.search(args.term))
        except Exception as e:  # noqa: BLE001
            print(f"Full search failed ({e}); going subject by subject.", file=sys.stderr)
            raw = []
        if raw:
            rows = [flatten(s) for s in raw]
        else:
            subjects = [s["code"] for s in b.subjects(args.term)]
    if subjects:
        for i, subj in enumerate(subjects, 1):
            got = [flatten(s) for s in b.search(args.term, subj)]
            print(f"[{i}/{len(subjects)}] {subj}: {len(got)} sections", file=sys.stderr)
            rows.extend(got)

    rows = [r for r in rows if not (r["crn"] in seen or seen.add(r["crn"]))]

    if args.descriptions:
        cache = {}
        for i, r in enumerate(rows, 1):
            key = (r["subject"], r["number"])
            if key not in cache:
                cache[key] = b.description(args.term, r["crn"])
                print(f"  description {i}/{len(rows)} {key[0]} {key[1]}", file=sys.stderr)
            r["description"] = cache[key]

    prefix = args.out or f"{args.term}_courses"
    with open(f"{prefix}.json", "w") as f:
        json.dump(rows, f, indent=2)
    if rows:
        with open(f"{prefix}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    courses = len({(r["subject"], r["number"]) for r in rows})
    print(f"Done: {len(rows)} sections across {courses} courses -> {prefix}.csv / {prefix}.json")


if __name__ == "__main__":
    main()
