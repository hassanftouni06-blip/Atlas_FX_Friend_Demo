"""Timestamped, quality-labelled headline context; never has trading access."""
import json, os, time, xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parent; CACHE=ROOT/"runtime"/"news_context.json"
FEEDS=[("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml"),("Google News · central banks", "https://news.google.com/rss/search?q="+quote_plus("(forex OR central bank OR interest rates OR inflation) when:1d")+"&hl=en-US&gl=US&ceid=US:en")]
FRED_API="https://api.stlouisfed.org/fred/"
MACRO_SERIES={"CPIAUCSL":"US consumer prices","UNRATE":"US unemployment rate","FEDFUNDS":"US effective federal funds rate"}

def fred_json(endpoint,parameters,api_key=None):
    key=api_key or os.environ.get("FRED_API_KEY")
    if not key: raise RuntimeError("FRED_API_KEY is not configured")
    query=urlencode({**parameters,"api_key":key,"file_type":"json"})
    # The key travels only in the request, never in logs, news items, or replays.
    with urlopen(Request(FRED_API+endpoint+"?"+query,headers={"User-Agent":"Atlas-FX-demo-research/2.4"}),timeout=12) as response:
        return json.loads(response.read())

def fred_vintage_observations(series_id,as_of_date,api_key=None):
    """ALFRED point-in-time observations for offline research, not an entry signal."""
    if series_id not in MACRO_SERIES: raise ValueError("Unsupported macro series")
    vintage=date.fromisoformat(as_of_date).isoformat()
    return fred_json("series/observations",{"series_id":series_id,"realtime_start":vintage,"realtime_end":vintage,"sort_order":"desc","limit":2},api_key)

def fred_context(api_key=None):
    if not (api_key or os.environ.get("FRED_API_KEY")):
        return {"status":"not_configured","series":[],"release_dates":[],"limitations":"Set FRED_API_KEY locally. FRED release dates have no reliable intraday time and cannot replace the FX event veto."}
    today=date.today(); series=[]
    for series_id,label in MACRO_SERIES.items():
        observations=fred_vintage_observations(series_id,today.isoformat(),api_key).get("observations",[])
        if observations:
            row=observations[0]
            series.append({"id":series_id,"title":label,"period":row.get("date"),"value":row.get("value"),"known_as_of":today.isoformat(),"source":"FRED/ALFRED"})
    releases=fred_json("releases/dates",{"include_release_dates_with_no_data":"true","sort_order":"desc","limit":1000},api_key).get("release_dates",[])
    upcoming=[]
    for row in releases:
        try: release_day=date.fromisoformat(row["date"])
        except (ValueError,KeyError,TypeError): continue
        if today<=release_day<=today+timedelta(days=14):
            upcoming.append({"date":row["date"],"title":row.get("release_name"),"release_id":row.get("release_id"),"time_precision":"date_only","source":"FRED"})
    return {"status":"ok","series":series,"release_dates":upcoming[:30],"limitations":"U.S. macro only. Release dates are date-only and not a substitute for an intraday FX calendar; observations may be available later than their source release."}

def get(force=False):
    try:cached=json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:cached={}
    age=time.time()-float(cached.get("fetched",0) or 0)
    if not force and cached.get("items") and 0<=age<=900:return cached
    items=[]; errors=[]
    for source,url in FEEDS:
        try:
            req=Request(url,headers={"User-Agent":"Atlas-FX-demo-research/2.0"})
            with urlopen(req,timeout=12) as response:root=ET.fromstring(response.read())
            for node in root.findall(".//item")[:20]:
                title=(node.findtext("title") or "").strip(); published=(node.findtext("pubDate") or "").strip()
                try:published=parsedate_to_datetime(published).isoformat()
                except Exception:published=None
                if title:items.append({"source":source,"source_quality":"official" if source=="Federal Reserve" else "aggregator","title":title[:300],"url":(node.findtext("link") or "")[:500],"published_utc":published,"impact":"requires_analysis"})
        except Exception as exc:errors.append(source+": "+str(exc))
    unique={x["title"].lower():x for x in items}
    fred=cached.get("fred",{})
    if force or time.time()-float(cached.get("fred_fetched",0) or 0)>21600 or (os.environ.get("FRED_API_KEY") and fred.get("status")=="not_configured"):
        try:
            fred=fred_context()
            fred_fetched=time.time()
        except Exception as exc:
            fred={"status":"unavailable","series":[],"release_dates":[],"limitations":"FRED macro context unavailable; intraday event veto remains separate.","error_type":type(exc).__name__}
            fred_fetched=time.time()
    else:fred_fetched=cached.get("fred_fetched",0)
    result={"status":"ok" if unique else "unavailable","fetched":time.time() if unique else cached.get("fetched"),"items":list(unique.values())[:40] if unique else cached.get("items",[]) if age<1800 else [],"fred":fred,"fred_fetched":fred_fetched,"errors":errors,"limitations":"Fed RSS is primary U.S. headline context; Google News is supplemental and may be late or wrong. FRED/ALFRED provide U.S. macro context and point-in-time research, not real-time FX headlines or intraday veto times."}
    CACHE.parent.mkdir(parents=True,exist_ok=True); temporary=CACHE.with_suffix(".tmp"); temporary.write_text(json.dumps(result),encoding="utf-8"); os.replace(temporary,CACHE)
    return result
