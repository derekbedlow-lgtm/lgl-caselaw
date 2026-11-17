#!/usr/bin/env python3
"""Build decisions.json for the Local Government Lawyer case law embed.

Pulls recent decisions from the Find Case Law (The National Archives) Atom
feeds, tags them by topic, merges them with the decisions already in
decisions.json, drops anything older than 90 days or listed in
dismissed.json, and writes the file back.

Usage:  python3 build.py
"""
import concurrent.futures as cf
import datetime as dt
import html
import json
import os
import re
import subprocess
import urllib.parse
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "decisions.json")
DISMISSED = os.path.join(HERE, "dismissed.json")
WINDOW_DAYS = 90
MAX_PAGES = 12          # safety cap per court when back-filling
FIELDS = ("id", "title", "citation", "url", "court", "date", "topics", "snippet")

BASE = "https://caselaw.nationalarchives.gov.uk"
COURTS = {
    "ukftt/grc": "FTT (GRC)",
    "ukut/aac": "Upper Tribunal (AAC)",
    "ewhc/admin": "Administrative Court",
    "ewhc/kb": "King's Bench Division",
    "ewhc/tcc": "TCC",
    "ewhc/ch": "Chancery Division",
    "ewca/civ": "Court of Appeal (Civil)",
    "uksc": "Supreme Court",
}
NS = {"a": "http://www.w3.org/2005/Atom", "tna": BASE}

# Topic rules: (topic, regex over title + snippet). Case-insensitive.
TOPICS = [
    ("FOI / EIR", r"\bInformation Commissioner\b|\bFreedom of Information\b|\bFOIA\b|\bEnvironmental Information Regulations\b|\bEIR\b|\bsection 1\(1\)|\bvexatious\b|\bsection 14\b"),
    ("Data protection", r"\bdata protection\b|\bUK GDPR\b|\bGDPR\b|\bpersonal data\b|\bsubject access\b|\bData Protection Act\b|\bmonetary penalty notice\b"),
    ("Procurement", r"\bprocurement\b|\bPublic Contracts Regulations\b|\bProcurement Act\b|\btender\b|\bstandstill\b|\bframework agreement\b|\bcontract award\b"),
    ("Planning", r"\bplanning\b|\bTown and Country Planning\b|\bSecretary of State for Housing\b|\bplanning permission\b|\bplanning inspector\b|\blocal plan\b|\bNPPF\b"),
    ("Housing", r"\bhousing\b|\bhomeless|\bHousing Act\b|\ballocation scheme\b|\bdisrepair\b|\bpossession\b|\bLandlord and Tenant\b|\bsection 21\b|\bHMO\b"),
    ("Children & social care", r"\bChildren Act\b|\bcare order\b|\bsocial care\b|\bsection 17\b|\bsection 20\b|\badult social care\b|\bCare Act\b|\bsafeguarding\b|\bcare proceedings\b"),
    ("Education & SEND", r"\bEducation\b|\bSEND\b|\bEHCP\b|\bEHC plan\b|\bschool\b|\bSchool Admissions?\b|\bacademy\b"),
    ("Judicial review", r"\bjudicial review\b|\bR \(|\bR\(|\bThe King on the application\b|\bon the application of\b|\bthe King \(|\bKing \(on the application"),
    ("Licensing & regulation", r"\blicensing\b|\bLicensing Act\b|\btaxi\b|\bprivate hire\b|\bgambling\b|\bpremises licence\b|\bregulat"),
    ("Highways & environment", r"\bhighway\b|\bHighways Act\b|\btraffic\b|\bparking\b|\bpollution\b|\bwaste\b|\bEnvironment Act\b|\bflood"),
    ("Governance & finance", r"\bcouncil tax\b|\bbusiness rates\b|\bnon-domestic rat|\bLocal Government Act\b|\bstandards\b|\bcode of conduct\b|\bmonitoring officer\b|\bsection 114\b|\baudit\b|\bbest value\b"),
    ("Employment", r"\bemployment\b|\bunfair dismissal\b|\bdiscrimination\b|\bEquality Act\b|\bwhistleblow"),
    ("Human rights", r"\bHuman Rights Act\b|\bArticle 8\b|\bArticle 6\b|\bArticle 10\b|\bECHR\b|\bConvention right"),
]
LOCAL_GOV = r"\bCouncil\b|\bBorough\b|\bCounty\b|\bCity of\b|\bLondon Borough\b|\bDistrict\b|\bMetropolitan\b|\bCombined Authority\b|\bGreater London Authority\b|\bMayor of London\b|\bParish\b|\blocal authority\b|\blocal authorities\b|\bNHS\b|\bPolice\b|\bChief Constable\b|\bCommissioner\b|\bSecretary of State\b|\bGovernment\b|\bDepartment\b|\bMinistry\b|\bHM\b|\bOfsted\b|\bOfcom\b|\bAgency\b|\bAuthority\b|\bTrust\b|\bUniversity\b|\bSchool\b"


def fetch(url, byte_range=None, timeout=25):
    cmd = ["curl", "-sS", "-L", "--retry", "2", "-m", str(timeout), url]
    if byte_range:
        cmd[1:1] = ["-r", byte_range]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout * 3 + 10).stdout
    except Exception:
        return b""


def feed_page(court, page):
    url = (f"{BASE}/atom.xml?court={urllib.parse.quote(court, safe='')}"
           f"&order=-date&per_page=50&page={page}")
    raw = fetch(url)
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    out = []
    for e in root.findall("a:entry", NS):
        title = (e.findtext("a:title", "", NS) or "").strip()
        link = ""
        for l in e.findall("a:link", NS):
            if l.get("rel") == "alternate" and not l.get("type"):
                link = l.get("href", "")
        slug = ""
        ncn = ""
        for ident in e.findall("tna:identifier", NS):
            if ident.get("type") == "ukncn":
                slug = ident.get("slug", "")
                ncn = (ident.text or "").strip()
        if not slug:
            slug = link.replace(BASE + "/", "")
        published = (e.findtext("a:published", "", NS) or "")[:10]
        out.append({
            "id": slug.replace("/", "-"),
            "slug": slug,
            "title": html.unescape(title),
            "citation": ncn,
            "url": link,
            "court": COURTS.get(court, court),
            "courtKey": court,
            "date": published,
        })
    return out


def feed_entries(court, cutoff):
    """Newest-first pages for one court, stopping once past the cutoff date.
    Returns (entries, ok) where ok is False if the first page could not be read."""
    entries = []
    for page in range(1, MAX_PAGES + 1):
        lst = feed_page(court, page)
        if lst is None:
            return entries, page > 1
        entries.extend(lst)
        if len(lst) < 50 or (lst and lst[-1]["date"] < cutoff):
            break
    return entries, True


def body_snippet(slug):
    raw = fetch(f"{BASE}/{slug}/data.xml", byte_range="0-90000", timeout=20)
    if not raw:
        return ""
    text = raw.decode("utf-8", errors="ignore")
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<meta.*?</meta>", " ", text, flags=re.S)
    text = re.sub(r"<header.*?</header>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"(DECISION|Decision|JUDGMENT|Judgment|Introduction|INTRODUCTION|Background|BACKGROUND)[:.]?\s", text)
    if m:
        text = text[m.end():]
    return text[:700]


def classify(d, snippet):
    hay = f"{d['title']} {snippet}"
    topics = [t for t, rx in TOPICS if re.search(rx, hay, re.I)]
    if d["courtKey"] == "ukftt/grc" and "FOI / EIR" not in topics and "Data protection" not in topics:
        if re.search(r"Information Commission", d["title"], re.I):
            topics.insert(0, "FOI / EIR")
    if not topics:
        topics = ["Other public law"]
    relevant = bool(re.search(LOCAL_GOV, hay)) or d["courtKey"] in ("ukftt/grc", "ewhc/admin")
    if d["courtKey"] == "ukut/aac":
        relevant = bool(re.search(r"Information Commission|Council\b|Borough|local authorit|Education|SEND|EHC|Special Educational|Care Act|Children Act", hay, re.I))
    return topics, relevant


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def main():
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()

    existing = {d["id"]: d for d in load_json(OUT, {}).get("decisions", []) if d.get("id")}
    dismissed = set(load_json(DISMISSED, []))

    entries, failed = [], []
    with cf.ThreadPoolExecutor(8) as ex:
        for court, (lst, ok) in zip(COURTS, ex.map(lambda c: feed_entries(c, cutoff), COURTS)):
            entries.extend(lst)
            if not ok:
                failed.append(court)
    if len(failed) == len(COURTS):
        raise SystemExit("Find Case Law feeds could not be reached; decisions.json left unchanged.")

    entries = [e for e in entries if e["date"] >= cutoff]
    # GRC also hears non-information cases (driving instructors, charities): keep only information rights there
    entries = [e for e in entries if e["courtKey"] != "ukftt/grc"
               or re.search(r"Information Commission|Commissioner", e["title"], re.I)]
    seen = set()
    fresh = []
    for e in entries:
        if e["id"] in existing or e["id"] in dismissed or e["id"] in seen:
            continue
        seen.add(e["id"])
        fresh.append(e)

    with cf.ThreadPoolExecutor(12) as ex:
        snippets = list(ex.map(lambda e: body_snippet(e["slug"]), fresh))

    added = 0
    for e, snip in zip(fresh, snippets):
        topics, relevant = classify(e, snip)
        if not relevant:
            continue
        existing[e["id"]] = {**e, "topics": topics, "snippet": snip}
        added += 1

    decisions = [
        {k: d.get(k, "") for k in FIELDS}
        for d in existing.values()
        if d.get("date", "") >= cutoff and d["id"] not in dismissed
    ]
    decisions.sort(key=lambda d: d["title"])
    decisions.sort(key=lambda d: d["date"], reverse=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"generated": today.isoformat(), "decisions": decisions}, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"{len(decisions)} decisions in decisions.json ({added} new, {len(fresh)} unseen feed entries since {cutoff})"
          + (f"; feeds not reached: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
