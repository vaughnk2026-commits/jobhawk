"""
JobHawk — Job source scrapers. Hard 75s timeout, clean executor shutdown.
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

log  = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
HDRS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def _cfg():
    try:
        with open(BASE/"config.yaml","r",encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def _norm(t):
    return re.sub(r"\s+"," ",(t or "")).strip()


def fetch_remotive():
    jobs=[]
    try:
        r=requests.get("https://remotive.com/api/remote-jobs",timeout=18)
        r.raise_for_status()
        for i in r.json().get("jobs",[]):
            jobs.append({"source":"Remotive","title":_norm(i.get("title")),
                "company":_norm(i.get("company_name")),
                "location":_norm(i.get("candidate_required_location") or "Remote"),
                "remote":True,"url":i.get("url",""),
                "date_posted":_norm(i.get("publication_date")),
                "description":BeautifulSoup(i.get("description") or "","html.parser").get_text(" ")})
    except Exception as e: log.warning("Remotive: %s",e)
    return jobs


def fetch_arbeitnow():
    jobs=[]
    try:
        r=requests.get("https://www.arbeitnow.com/api/job-board-api",timeout=18)
        r.raise_for_status()
        for i in r.json().get("data",[]):
            loc=_norm(i.get("location"))
            jobs.append({"source":"Arbeitnow","title":_norm(i.get("title")),
                "company":_norm(i.get("company_name")),"location":loc or "Remote",
                "remote":bool(i.get("remote")) or "remote" in loc.lower(),
                "url":i.get("url",""),"date_posted":str(i.get("created_at") or ""),
                "description":BeautifulSoup(i.get("description") or "","html.parser").get_text(" ")})
    except Exception as e: log.warning("Arbeitnow: %s",e)
    return jobs


def fetch_remoteok():
    jobs=[]
    try:
        r=requests.get("https://remoteok.io/api",timeout=18,
                       headers={"User-Agent":"JobHawk/2.0"})
        r.raise_for_status()
        data=r.json()
        for i in (data[1:] if len(data)>1 else []):
            if not isinstance(i,dict) or not i.get("position"): continue
            jobs.append({"source":"RemoteOK","title":_norm(i.get("position")),
                "company":_norm(i.get("company")),
                "location":_norm(i.get("location") or "Remote"),"remote":True,
                "url":i.get("url") or f"https://remoteok.io/remote-jobs/{i.get('id','')}",
                "date_posted":str(i.get("date") or ""),
                "description":BeautifulSoup(i.get("description") or "","html.parser").get_text(" ")
                            +" ".join(i.get("tags") or [])})
    except Exception as e: log.warning("RemoteOK: %s",e)
    return jobs


def fetch_weworkremotely():
    jobs=[]
    try:
        r=requests.get("https://weworkremotely.com/remote-jobs.rss",timeout=18,headers=HDRS)
        r.raise_for_status()
        root=ET.fromstring(r.content)
        for item in root.findall(".//item"):
            def t(tag,_i=item):
                el=_i.find(tag); return (el.text or "") if el is not None else ""
            raw=t("title"); parts=raw.split(":",1)
            company=_norm(parts[0]) if len(parts)>1 else ""
            title  =_norm(parts[1]) if len(parts)>1 else _norm(raw)
            jobs.append({"source":"WeWorkRemotely","title":title,"company":company,
                "location":"Remote","remote":True,"url":t("link") or t("guid"),
                "date_posted":_norm(t("pubDate")),
                "description":BeautifulSoup(t("description"),"html.parser").get_text(" ")})
    except Exception as e: log.warning("WeWorkRemotely: %s",e)
    return jobs


def fetch_jobicy():
    jobs=[]
    try:
        r=requests.get("https://jobicy.com/?feed=job_feed",timeout=18,headers=HDRS)
        r.raise_for_status()
        root=ET.fromstring(r.content)
        for item in root.findall(".//item"):
            def t(tag,_i=item):
                el=_i.find(tag); return (el.text or "") if el is not None else ""
            title=_norm(t("title")); url=t("link") or t("guid")
            desc =BeautifulSoup(t("description"),"html.parser").get_text(" ")
            company=""
            for child in item:
                if "company" in child.tag.lower(): company=_norm(child.text or ""); break
            if not company and " @ " in title:
                parts=title.split(" @ ",1); title,company=_norm(parts[0]),_norm(parts[1])
            jobs.append({"source":"Jobicy","title":title,"company":company or "—",
                "location":"Remote","remote":True,"url":url,
                "date_posted":_norm(t("pubDate")),"description":desc})
    except Exception as e: log.warning("Jobicy: %s",e)
    return jobs


def fetch_indeed(queries=None):
    jobs=[]
    if not queries:
        queries=_cfg().get("sources",{}).get("indeed_queries",
            ["sales manager Canada","business development Canada"])
    for q in queries[:6]:   # cap at 6 to bound runtime
        try:
            r=requests.get(f"https://www.indeed.com/rss?{urlencode({'q':q,'sort':'date'})}",
                           timeout=12,headers=HDRS)
            if r.status_code!=200: continue
            root=ET.fromstring(r.content)
            for item in root.findall(".//item"):
                def t(tag,_i=item):
                    el=_i.find(tag); return (el.text or "") if el is not None else ""
                title=_norm(t("title"))
                se=item.find("source"); company=_norm(se.text if se is not None and se.text else "")
                desc=BeautifulSoup(t("description"),"html.parser").get_text(" ")
                jobs.append({"source":"Indeed","title":title,"company":company or "—",
                    "location":_norm(t("location") or ""),"remote":"remote" in desc.lower(),
                    "url":t("link"),"date_posted":_norm(t("pubDate")),"description":desc})
        except Exception as e: log.warning("Indeed '%s': %s",q,e)
    return jobs


def fetch_linkedin(searches=None):
    jobs=[]
    if not searches:
        searches=_cfg().get("sources",{}).get("linkedin_searches",
            [{"keywords":"sales manager","location":"Canada"}])
    for s in searches[:4]:   # cap at 4 searches
        try:
            r=requests.get(
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search",
                params={"keywords":s.get("keywords",""),"location":s.get("location",""),
                        "f_TPR":"r86400","start":0},
                timeout=12,headers=HDRS)
            if r.status_code!=200: continue
            soup=BeautifulSoup(r.text,"html.parser")
            for card in soup.find_all("li"):
                te=card.find("h3"); ce=card.find("h4")
                le=card.find("span",class_=lambda c:c and "location" in (c or ""))
                ae=card.find("a",href=True)
                if not te or not te.get_text(strip=True): continue
                jobs.append({"source":"LinkedIn","title":_norm(te.get_text()),
                    "company":_norm(ce.get_text()) if ce else "—",
                    "location":_norm(le.get_text()) if le else s.get("location",""),
                    "remote":"remote" in _norm(te.get_text()).lower(),
                    "url":ae["href"].split("?")[0] if ae else "",
                    "date_posted":"","description":_norm(card.get_text())})
        except Exception as e: log.warning("LinkedIn '%s': %s",s.get("keywords"),e)
    return jobs


def fetch_all_jobs(indeed_queries=None, linkedin_searches=None):
    cfg=_cfg(); sc=cfg.get("sources",{})

    fns=[]
    if sc.get("remotive",True):       fns.append(("Remotive",       fetch_remotive))
    if sc.get("arbeitnow",True):      fns.append(("Arbeitnow",      fetch_arbeitnow))
    if sc.get("remoteok",True):       fns.append(("RemoteOK",       fetch_remoteok))
    if sc.get("weworkremotely",True): fns.append(("WeWorkRemotely", fetch_weworkremotely))
    if sc.get("jobicy",True):         fns.append(("Jobicy",         fetch_jobicy))

    q=indeed_queries or sc.get("indeed_queries",[])
    s=linkedin_searches or sc.get("linkedin_searches",[])
    if sc.get("indeed",True):   fns.append(("Indeed",   lambda q=q:fetch_indeed(q)))
    if sc.get("linkedin",True): fns.append(("LinkedIn", lambda s=s:fetch_linkedin(s)))

    jobs=[]
    executor=concurrent.futures.ThreadPoolExecutor(max_workers=max(len(fns),1))
    fmap={executor.submit(fn):name for name,fn in fns}
    try:
        for fut in concurrent.futures.as_completed(fmap,timeout=75):
            name=fmap[fut]
            try:
                result=fut.result(timeout=3)
                jobs.extend(result)
                log.info("%s -> %d jobs",name,len(result))
            except Exception as e:
                log.warning("%s failed: %s",name,e)
    except concurrent.futures.TimeoutError:
        log.warning("fetch_all_jobs: 75s timeout — partial results (%d jobs so far)",len(jobs))
    finally:
        executor.shutdown(wait=False)   # KEY FIX: don't block on hung threads

    seen,deduped=set(),[]
    for j in jobs:
        key=j.get("url") or f"{j.get('company')}|{j.get('title')}"
        if key and key not in seen: seen.add(key); deduped.append(j)

    log.info("Total unique jobs: %d",len(deduped))
    return deduped
