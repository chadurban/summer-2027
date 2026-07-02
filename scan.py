#!/usr/bin/env python3
"""
Summer 2027 destination COMPARISON scanner — Japan vs Brazil (Vitória) vs Iberia.

Chad is choosing one summer-2027 trip (early-mid June, 7-15 nights). This pulls seats.aero
business + premium award space US-hubs -> each destination's gateways for the Jun 2027 window,
layover-vets connections (2.5-6h, same-airport), and emits the cheapest ease-ranked one-way per
destination/cabin at BOTH 3 and 4 award seats — so the dashboard shows a clean head-to-head.

Award-only for now (Jun-2027 cash fares aren't on sale until ~Aug 2026). Reuses the proven
seats.aero plumbing from brazil-xmas-tracker.

Run:  python3 scan.py         # live pull, write data.json + inject index.html
      python3 scan.py --dry   # live pull, print the comparison, no write
"""
import json, sys, os, time, re, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = "/Users/admin/Library/CloudStorage/GoogleDrive-urbanc@acba.edu/.shortcut-targets-by-id/1Tc-st1PSSbOMdWmS2DRk_5DLXRb8WpFb/ATS1/Operations/Claude/Personal/.flight-scanner-secrets.json"
KEY = json.load(open(SECRETS))["seats_aero_partner_authorization"]
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
HDR = {"Partner-Authorization": KEY, "Accept": "application/json", "User-Agent": UA}

# ── SHARED CONFIG NOTES (all 3 flight scanners — keep in sync) ──────────────
# * ANA is NOT a seats.aero source (HTTP 400) — do NOT add "ana"; NH metal surfaces via united/aeroplan.
# * Avianca LifeMiles IS an Amex MR 1:1 partner (verified Jun 2026) — tier 1, "MR → LifeMiles".
# * La Compagnie (EWR all-business) is INVISIBLE to Google Flights/SerpApi — watch lacompagnie.com manually.
# * seats.aero Pro quota = 1000/day SHARED: japan 6:20, brazil-xmas 6:40, summer-2027 7:00.
# * Push alerts: notify() → ntfy.sh topic from the secrets JSON ("ntfy_topic"); never commit the topic.

# --- windows ---
OUT_START, OUT_END = "2027-06-01", "2027-06-16"     # early-mid June depart
RET_START, RET_END = "2027-06-08", "2027-07-01"     # return (7-15 nights out; mostly >360d, sparse now)
LAY_MIN, LAY_MAX = 150, 360
CABINS = ["business", "premium"]
PARTIES = [3, 4]

ALL_US  = {"CHS","ATL","JFK","EWR","IAD","IAH","ORD","BOS","CLT","DFW","DTW","PHL","MIA","SFO","LAX","SEA","DEN"}
EAST_US = {"CHS","ATL","JFK","EWR","IAD","IAH","ORD","BOS","CLT","DFW","DTW","PHL","MIA"}   # no west-coast backtrack to Brazil/Iberia

BT_ME_SA = {"CAI","DEL","BOM","IST","DXB","AUH","DOH","ADD","JNB","NBO","TLV","SVO"}          # Middle East / South Asia / Africa detours
BT_BRAZIL = {"CDG","ORY","AMS","LHR","LGW","FRA","MUC","MAD","BCN","LIS","OPO","FCO","MXP","ZRH","IST","DXB","AUH","DOH","CAI","ADD","CMN","HND","NRT","ICN","PEK","PVG","HKG","SIN","DEL","BOM","SFO","LAX","SEA","PDX"}
BT_IBERIA = BT_ME_SA | {"HND","NRT","ICN","PEK","PVG","HKG","SIN"}                                # CMN allowed — Casablanca is a real Lisbon/Madrid path
DESTS = [
    {"key":"japan",  "name":"Japan / Korea",        "region":"Asia",          "gw":{"ICN","NRT","HND"},          "hubs":ALL_US,  "hop":None, "bt":BT_ME_SA | {"XMN","CAN","PVG","PEK","CGO","CTU"}},
    {"key":"brazil", "name":"Brazil · Vitória",     "region":"South America", "gw":{"GRU","GIG","VCP","CNF"},    "hubs":EAST_US, "hop":"+ VIX hop (~$60–130)", "bt":BT_BRAZIL},
    # Kami's route is a west-east LINE (LIS-SVQ-BCN-NCE): fly INTO one ENDPOINT, home from the other
    # (works either direction); mid-route cities are ground legs, so they are NOT entry candidates.
    {"key":"iberia", "name":"Iberia+Med · Lisbon→Nice (Kami's route)","region":"Europe",        "gw":{"LIS","NCE"},    "hubs":EAST_US, "hop":None, "bt":BT_IBERIA},
]
PROGRAMS = ["aeroplan","united","virginatlantic","flyingblue","delta","aeromexico","singapore","lifemiles","american","alaska","turkish"]
CABKEY = {"business":"J","premium":"W"}
MAX_MILES = {"business":200000,"premium":150000}     # drop non-saver dynamic garbage (one-way)
BOOK_EASE = {"united":1,"aeroplan":1,"turkish":1,"virginatlantic":1,"flyingblue":1,"delta":1,"singapore":1,
             "aeromexico":2,"american":3,"alaska":3,"lifemiles":1}
BOOK_VIA = {"aeroplan":"UR/MR → Aeroplan","united":"UR → United","turkish":"MR → Turkish","delta":"UR → Virgin (Delta)",
            "virginatlantic":"UR/MR → Virgin Atlantic","flyingblue":"UR/MR → Flying Blue","singapore":"UR/MR → Singapore",
            "aeromexico":"UR → Flying Blue/Virgin","american":"AA miles","alaska":"Alaska miles","lifemiles":"MR → LifeMiles"}
STOP_PEN, TIER_PEN = 20000, {1:0,2:30000,3:300000}

errors = []
REMAINING = None

def quota_low(): return REMAINING is not None and 0 <= REMAINING < 40

def api(path, params=None):
    global REMAINING
    url = f"https://seats.aero/partnerapi/{path}"
    if params: url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HDR)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                rem = r.headers.get("X-RateLimit-Remaining")
                if rem is not None and rem.lstrip("-").isdigit(): REMAINING = int(rem)
                return json.loads(r.read().decode("utf-8","replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429: REMAINING = 0; errors.append(f"{path} 429"); return None
            if e.code in (500,502,503) and attempt < 2: time.sleep(2*(attempt+1)); continue
            errors.append(f"{path} HTTP {e.code}"); return None
        except Exception as ex:
            if attempt < 2: time.sleep(1.5); continue
            errors.append(f"{path} {type(ex).__name__}"); return None

def bulk(source, cabin, o_region, d_region, start, end):
    out, cursor = [], None
    for _ in range(6):
        p = {"source":source,"cabin":cabin,"start_date":start,"end_date":end,
             "origin_region":o_region,"destination_region":d_region,"take":1000}
        if cursor: p["cursor"] = cursor
        d = api("availability", p)
        if not d or not isinstance(d, dict): break
        out += d.get("data", [])
        if not d.get("hasMore"): break
        cursor = d.get("cursor")
        if not cursor: break
    return out

def parse_dt(s):
    try: return datetime.fromisoformat(s.replace("Z","+00:00"))
    except Exception: return None

def vet(segs):
    lays, same = [], True
    for i in range(len(segs)-1):
        a, b = parse_dt(segs[i].get("ArrivesAt")), parse_dt(segs[i+1].get("DepartsAt"))
        if segs[i].get("DestinationAirport") != segs[i+1].get("OriginAirport"): same = False
        if a and b: lays.append(int((b-a).total_seconds()//60))
    return (same and all(LAY_MIN <= L <= LAY_MAX for L in lays)), lays

def best_trip(avail_id, cabin):
    if quota_low(): return None
    d = api(f"trips/{avail_id}")
    trips = (d.get("data") if isinstance(d, dict) else d) or []
    cands = [t for t in trips if (t.get("Cabin") or "").lower() == cabin]
    if not cands: return None
    scored = []
    for t in cands:
        segs = t.get("AvailabilitySegments") or []
        ok, _ = vet(segs)
        scored.append((t.get("Stops", len(segs)-1), not ok, t.get("MileageCost",1e9), t, segs, ok))
    scored.sort(key=lambda x:(x[0],x[1],x[2]))
    stops, _, _, t, segs, ok = scored[0]
    path = "-".join([segs[0]["OriginAirport"]] + [s["DestinationAirport"] for s in segs]) if segs else ""
    return {"stops":stops,"layoverOK":ok,"path":path}

def eff(r):
    return int(r["miles"]) + r["stops"]*STOP_PEN + TIER_PEN.get(BOOK_EASE.get(r["source"],2),30000)

def collect(dest, cabin):
    """Cheapest-per-route US->gateway outbound options for a destination/cabin, layover-vetted."""
    ck = CABKEY[cabin]
    rows, ded = [], {}
    for source in PROGRAMS:
        if quota_low(): errors.append(f"{source}/{dest['key']}: quota low"); continue
        for r in bulk(source, cabin, "North America", dest["region"], OUT_START, OUT_END):
            rt = r.get("Route", {}); o, d = rt.get("OriginAirport"), rt.get("DestinationAirport")
            if o not in dest["hubs"] or d not in dest["gw"]: continue
            if not r.get(f"{ck}Available"): continue
            seats = r.get(f"{ck}RemainingSeats") or 0
            mi = int(r.get(f"{ck}MileageCost") or 0)
            if seats < 1 or mi <= 0 or mi > MAX_MILES[cabin]: continue
            key = (o, d, mi)
            row = {"id":r.get("ID"),"source":source,"o":o,"d":d,"miles":mi,"seats":seats,
                   "date":r.get("Date"),"direct":r.get(f"{ck}Direct"),"airlines":r.get(f"{ck}Airlines")}
            if key not in ded or seats > ded[key]["seats"]: ded[key] = row
    for k in sorted(ded.values(), key=lambda x:x["miles"])[:8]:
        if k["direct"]: k["stops"], k["layoverOK"], path = 0, True, ""
        else:
            rt = best_trip(k["id"], cabin)
            k["stops"], k["layoverOK"] = (rt["stops"], rt["layoverOK"]) if rt else (1, False)
            path = (rt or {}).get("path", "")
        if any(h in dest.get("bt", set()) for h in path.split("-") if h):
            continue   # drop never-fly detours (JFK-CAI-ICN etc.)
        rows.append(k)
    return rows

def pick(rows, min_seats):
    cand = [r for r in rows if (r["seats"] or 0) >= min_seats and (r["direct"] or r["layoverOK"])
            and BOOK_EASE.get(r["source"],2) <= 2]
    cand.sort(key=eff)
    if cand: return cand[0]
    relaxed = sorted([r for r in rows if (r["seats"] or 0) >= min_seats], key=eff)  # allow non-UR/MR or off-spec layover
    return relaxed[0] if relaxed else None

def opt(r):
    if not r: return None
    return {"ow": int(r["miles"]), "route": (r["o"]+"→"+r["d"]) if r["direct"] else f'{r["o"]}→{r["d"]} ({r["stops"]}-stop)',
            "seats": r["seats"], "date": r["date"], "direct": bool(r["direct"]),
            "airlines": r["airlines"], "via": BOOK_VIA.get(r["source"], r["source"]),
            "ur": BOOK_EASE.get(r["source"],2) <= 2}

def main():
    dry = "--dry" in sys.argv
    out = []
    for dest in DESTS:
        d = {"key":dest["key"], "name":dest["name"], "hop":dest["hop"], "gateways":"/".join(sorted(dest["gw"])), "cabins":{}}
        for cabin in CABINS:
            rows = collect(dest, cabin)
            d["cabins"][cabin] = {"n": len(rows), "p3": opt(pick(rows, 3)), "p4": opt(pick(rows, 4))}
        out.append(d)
    payload = {"lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "window":"early–mid June 2027 · 7–15 nights", "dests": out, "errors": errors}
    if dry:
        for d in out:
            print(f'\n=== {d["name"]}  ({d["gateways"]}) {d["hop"] or ""} ===')
            for cab in CABINS:
                c = d["cabins"][cab]
                for tag, pk in (("≥3", c["p3"]), ("≥4", c["p4"])):
                    if pk:
                        rt3 = pk["ow"]*2
                        print(f'  {cab:9} {tag}: {pk["ow"]//1000}K ow → ~{rt3//1000}K RT/pp · {rt3*3//1000}K(3)/{rt3*4//1000}K(4) · {pk["route"]} {pk["seats"]}st {pk["via"]}{"" if pk["ur"] else "  ⚠ not UR/MR"}')
                    else:
                        print(f'  {cab:9} {tag}: none in-window yet')
        print("\nerrors:", errors or "none")
        return
    with open(os.path.join(HERE,"data.json"),"w") as f: json.dump(payload, f, indent=2)
    injected = inject(payload)
    print(json.dumps({"dests":[d["key"] for d in out], "injected":injected, "errors":errors}))

def inject(payload):
    path = os.path.join(HERE, "index.html")
    if not os.path.exists(path): return False
    html = open(path).read()
    pat = re.compile(r'(<script id="cmp-data" type="application/json">).*?(</script>)', re.DOTALL)
    if not pat.search(html): return False
    open(path, "w").write(pat.sub(lambda m: m.group(1) + json.dumps(payload, separators=(",", ":")) + m.group(2), html, count=1))
    return True

if __name__ == "__main__":
    main()
