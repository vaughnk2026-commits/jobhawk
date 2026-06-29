"""
JobHawk — Job source scrapers (7 sources).
Returns raw job dicts; scoring happens separately per user.
"""

import concurrent.futures
import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List
from urllib.parse import urlencode

import requests
import yaml
from bs4 import BeautifulSoup
from pathlib import Path

log = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent


def _cfg() -> Dict:
    try:
        with open(BASE / "config.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        return {}


def _norm(t) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


# ── Individual fetchers ───────────────────────────────────────────────────────

def fetch_remotive() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", timeout=25)
        r.raise_for_status()
        for item in r.json().get("jobs", []):
            jobs.append({
                "source": "Remotive",
                "title": _norm(item.get("title")),
                "company": _norm(item.get("company_name")),
                "location": _norm(item.get("candidate_required_location") or "Remote"),
                "remote": True,
                "url": item.get("url", ""),
                "date_posted": _norm(item.get("publication_date")),
                "description": BeautifulSoup(
                    item.get("description") or "", "html.parser"
                ).get_text(" "),
            })
    except Exception as e:
        log.warning("Remotive: %s", e)
    return jobs


def fetch_arbeitnow() -> List[Dict]:
    jobs = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=25)
        r.raise_for_status()
        for item in r.json().get("data", []):
            loc = _norm(item.get("location"))
            remote = bool(item.get("remote")) or "remote" in loc.lower()
            jobs.append({
                "source": "Arbeitnow",
                "title": _norm(item.get("title")),
                "company": _norm(item.get("company_name")),
                "location": loc or "Remote",
                "remote": remote,
                "url": item.get("url", ""),
                "date_posted": str(item.get("created_at") or ""),
                "description": BeautifulSoup(
                    item.get("description") or "", "html.parser"
                ).get_text(" "),
            })
    except Exception as e:
        log.warning("Arbeitnow: %s", e)
    return jobs


def fetch_remoteok() -> List[Dict]:
    jobs = []
    try:
        r = requests.get(
            "https://remoteok.io/api", timeout=25,
            headers={"User-Agent": "JobHawk/2.0 (job search platform)"},
        )
        r.raise_for_status()
        data = r.json()
        for item in (data[1:] if len(data) > 1 else []):
            if not isinstance(item, dict) or not item.get("position"):
                continue
            tags = " ".join(item.get("tags") or [])
            desc = BeautifulSoup(
                item.get("description") or "", "html.parser"
            ).get_text(" ")
            jobs.append({
                "source": "RemoteOK",
                "title": _norm(item.get("position")),
                "company": _norm(item.get("company")),
                "location": _norm(item.get("location") or "Remote"),
                "remote": True,
                "url": item.get("url") or f"https://remoteok.io/remote-jobs/{item.get('id','')}",
                "date_posted": str(item.get("date") or ""),
                "description": f"{desc} {tags}",
            })
    except Exception as e:
        log.warning("RemoteOK: %s", e)
    return jobs


def fetch_weworkremotely() -> List[Dict]:
    jobs = []
    try:
        r = requests.get(
            "https://weworkremotely.com/remote-jobs.rss", timeout=25,
            headers={"User-Agent": "JobHawk/2.0"},
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            def txt(tag):
                el = item.find(tag)
                return (el.text or "") if el is not None else ""
            raw = txt("title")
            parts = raw.split(":", 1)
            company = _norm(parts[0]) if len(parts) > 1 else ""
            title = _norm(parts[1]) if len(parts) > 1 else _norm(raw)
            desc = BeautifulSoup(txt("description"), "html.parser").get_text(" ")
            jobs.append({
                "source": "WeWorkRemotely",
                "title": title, "company": company,
                "location": "Remote", "remote": True,
                "url": txt("link") or txt("guid"),
                "date_posted": _norm(txt("pubDate")),
                "description": desc,
            })
    except Exception as e:
        log.warning("WeWorkRemotely: %s", e)
    return jobs


def fetch_jobicy() -> List[Dict]:
    jobs = []
    try:
        r = requests.get(
            "https://jobicy.com/?feed=job_feed", timeout=25,
            headers={"User-Agent": "JobHawk/2.0"},
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item"):
            def txt(tag):
                el = item.find(tag)
                return (el.text or "") if el is not None else ""
            title = _norm(txt("title"))
            url = txt("link") or txt("guid")
            desc = BeautifulSoup(txt("description"), "html.parser").get_text(" ")
            company = ""
            for child in item:
                if "company" in child.tag.lower():
                    company = _norm(child.text or "")
                    break
            if not company and " @ " in title:
                parts = title.split(" @ ", 1)
                title, company = _norm(parts[0]), _norm(parts[1])
            jobs.append({
                "source": "Jobicy",
                "title": title, "company": company or "—",
                "location": "Remote", "remote": True,
                "url": url, "date_posted": _norm(txt("pubDate")),
                "description": desc,
            })
    except Exception as e:
        log.warning("Jobicy: %s", e)
    return jobs


def fetch_indeed(queries: List[str] = None) -> List[Dict]:
    jobs = []
    if queries is None:
        queries = _cfg().get("sources", {}).get("indeed_queries", [
            "software engineer Canada",
            "marketing manager Canada",
            "sales manager Canada",
        ])
    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    for q in queries:
        try:
            params = urlencode({"q": q, "sort": "date", "radius": "100"})
            r = requests.get(
                f"https://www.indeed.com/rss?{params}", timeout=20, headers=hdrs
            )
            if r.status_code != 200:
                continue
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                def txt(tag):
                    el = item.find(tag)
                    return (el.text or "") if el is not None else ""
                title = _norm(txt("title"))
                src_el = item.find("source")
                company = _norm(src_el.text if src_el is not None and src_el.text else "")
                desc = BeautifulSoup(txt("description"), "html.parser").get_text(" ")
                jobs.append({
                    "source": "Indeed",
                    "title": title, "company": company or "—",
                    "location": _norm(txt("location") or ""),
                    "remote": "remote" in title.lower() or "remote" in desc.lower(),
                    "url": txt("link"),
                    "date_posted": _norm(txt("pubDate")),
                    "description": desc,
                })
        except Exception as eq:
            log.warning("Indeed query '%s': %s", q, eq)
    return jobs


def fetch_linkedin(searches: List[Dict] = None) -> List[Dict]:
    jobs = []
    if searches is None:
        searches = _cfg().get("sources", {}).get("linkedin_searches", [
            {"keywords": "software engineer", "location": "Canada"},
        ])
    hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    for s in searches:
        try:
            r = requests.get(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params={
                    "keywords": s.get("keywords", ""),
                    "location": s.get("location", ""),
                    "f_TPR": "r86400",
                    "start": 0,
                },
                timeout=20, headers=hdrs,
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.find_all("li"):
                title_el = card.find("h3")
                company_el = card.find("h4")
                loc_el = card.find(
                    "span", class_=lambda c: c and "location" in (c or "")
                )
                link_el = card.find("a", href=True)
                if not title_el or not title_el.get_text(strip=True):
                    continue
                url = link_el["href"].split("?")[0] if link_el else ""
                jobs.append({
                    "source": "LinkedIn",
                    "title": _norm(title_el.get_text()),
                    "company": _norm(company_el.get_text()) if company_el else "—",
                    "location": _norm(loc_el.get_text()) if loc_el else s.get("location", ""),
                    "remote": "remote" in _norm(title_el.get_text()).lower(),
                    "url": url,
                    "date_posted": "",
                    "description": _norm(card.get_text()),
                })
        except Exception as es:
            log.warning("LinkedIn '%s': %s", s.get("keywords"), es)
    return jobs


# ── Main fetch-all function ───────────────────────────────────────────────────

def fetch_all_jobs(
    indeed_queries: List[str] = None,
    linkedin_searches: List[Dict] = None,
) -> List[Dict]:
    """
    Scrape all 7 sources in parallel. Returns deduplicated list of raw job dicts.
    Call this once per scan; scoring is done per-user separately.
    """
    cfg = _cfg()
    sc = cfg.get("sources", {})

    tasks = []
    if sc.get("remotive", True):       tasks.append(fetch_remotive)
    if sc.get("arbeitnow", True):      tasks.append(fetch_arbeitnow)
    if sc.get("remoteok", True):       tasks.append(fetch_remoteok)
    if sc.get("weworkremotely", True): tasks.append(fetch_weworkremotely)
    if sc.get("jobicy", True):         tasks.append(fetch_jobicy)

    jobs: List[Dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks) + 2) as ex:
        futures = {ex.submit(fn): fn.__name__ for fn in tasks}

        # Indeed + LinkedIn need extra args
        if sc.get("indeed", True):
            q = indeed_queries or sc.get("indeed_queries", [])
            futures[ex.submit(fetch_indeed, q)] = "fetch_indeed"
        if sc.get("linkedin", True):
            s = linkedin_searches or sc.get("linkedin_searches", [])
            futures[ex.submit(fetch_linkedin, s)] = "fetch_linkedin"

        for fut in concurrent.futures.as_completed(futures, timeout=90):
            name = futures[fut]
            try:
                result = fut.result()
                jobs.extend(result)
                log.info("%s -> %d jobs", name, len(result))
            except Exception as e:
                log.warning("Fetcher %s failed: %s", name, e)

    # Deduplicate by URL
    seen, deduped = set(), []
    for j in jobs:
        key = j.get("url") or f"{j.get('company')}|{j.get('title')}"
        if key and key not in seen:
            seen.add(key)
            deduped.append(j)

    log.info("Total unique jobs fetched: %d", len(deduped))
    return deduped
