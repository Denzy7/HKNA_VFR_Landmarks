#!/usr/bin/env python3
"""Cached Overpass fetcher. Public Overpass servers throttle hard, so every
query result is stored under <osm_dir>/cache/<sha1-of-query>.json and reused;
the network is only hit for a new/changed query or with --refresh.

Usage: osm_fetch.py <osm_dir> [--refresh] [name ...]
Writes <osm_dir>/<name>.json for each named query (all by default).
"""
import hashlib, json, os, shutil, sys, time, urllib.parse, urllib.request

MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
UA = "xp11-hknw-vfr-landmarks (cached, low volume)"

QUERIES = {
    # tall buildings, stadiums, Ngong wind farm, silos
    "raw": """[out:json][timeout:90];
(
 way["building"]["building:levels"~"^([1-9][0-9]+)$"](-1.40,36.70,-1.20,36.95);
 way["building"]["height"~"^([3-9][0-9]|[1-9][0-9][0-9])"](-1.40,36.70,-1.20,36.95);
 way["leisure"="stadium"](-1.40,36.70,-1.20,36.95);
 node["generator:source"="wind"](-1.50,36.55,-1.30,36.75);
 way["man_made"="silo"](-1.40,36.70,-1.20,36.95);
);
out body geom;""",
    # building:part detail for CBD / Upper Hill / Westlands landmarks
    "parts": """[out:json][timeout:60];
(
 way["building:part"](-1.305,36.800,-1.280,36.830);
 way["building:part"](-1.275,36.800,-1.258,36.815);
 way(id:1553682723,1413935255,1189979678,123414930,123414928,1396270912);
);
out body geom;""",
    "named": """[out:json][timeout:60];
(
 nwr["name"~"Kenyatta International|KICC|Britam|Old Mutual|UAP|Pinnacle|Hazina Trade|NSSF|Parliament|Silo|NCPB|National Cereals|Bomas|Railway Museum|Uhuru Park|Kenyatta National Hospital|Moi Air|Carnivore",i](-1.40,36.70,-1.20,36.95);
);
out body geom;""",
    # AIP visual reference sites (masts only are modelled; buildings come from simHeaven)
    "sites": """[out:json][timeout:90];
(
 way["building"](-1.3420,36.7820,-1.3230,36.8030);
 way["building"](-1.3520,36.7530,-1.3450,36.7620);
 way["building"](-1.3135,36.8010,-1.3075,36.8080);
 way["building"](-1.3320,36.6700,-1.3220,36.6820);
 relation["building"](-1.3520,36.7530,-1.3450,36.7620);
 node["man_made"="mast"](-1.3320,36.6700,-1.3220,36.6820);
 way(id:26744501,174310524,1544752632);
 relation(id:13995868,21187670);
);
out body geom;""",
    # new stadium at Jamhuri / Ngong Rd ("Kasongo", -1.30588 36.7496), under construction
    "kasongo": """[out:json][timeout:60];
(
 way["leisure"~"stadium|pitch|sports_centre"](-1.3120,36.7430,-1.3000,36.7560);
 way["building"](-1.3120,36.7430,-1.3000,36.7560);
 way["landuse"="construction"](-1.3120,36.7430,-1.3000,36.7560);
 way["construction"](-1.3120,36.7430,-1.3000,36.7560);
);
out body geom;""",
    # low-rise national landmarks included regardless of height (build.py CORE)
    "core": """[out:json][timeout:60];
(
 nwr["name"~"^(Kenya National Archives|Nairobi City Hall|Kenya Parliament Buildings|Jamia Mosque|McMillan Library|Nairobi Central Railway Station)$"](-1.300,36.810,-1.275,36.835);
);
out body geom;""",
    # every building at ABC Place and Safaricom House (Westlands), and the ABC Place site
    "westlands": """[out:json][timeout:60];
(
 way["building"](-1.2610,36.7750,-1.2582,36.7785);
 way["building"](-1.2602,36.7843,-1.2580,36.7871);
 way(id:1306396205);
);
out body geom;""",
    # streets and entrances around the core landmarks, to check which way each facade faces
    "core_ctx": """[out:json][timeout:60];
(
 way["highway"]["name"](-1.2935,36.8180,-1.2810,36.8320);
 node["entrance"](-1.2935,36.8180,-1.2810,36.8320);
);
out body geom;""",
}


def fetch(q, cache_dir, refresh=False):
    key = hashlib.sha1(q.strip().encode()).hexdigest()
    path = os.path.join(cache_dir, key + ".json")
    if os.path.exists(path) and not refresh:
        return path, "cache"
    data = urllib.parse.urlencode({"data": q}).encode()
    wait = 10
    for attempt in range(4):
        for url in MIRRORS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=180) as r:
                    body = r.read()
                json.loads(body)          # reject HTML error pages
                os.makedirs(cache_dir, exist_ok=True)
                with open(path + ".tmp", "wb") as f:
                    f.write(body)
                os.replace(path + ".tmp", path)
                return path, url
            except Exception as e:     # 429/502/504/timeouts/HTML: try next mirror
                print(f"  {url}: {e}", file=sys.stderr)
        time.sleep(wait)
        wait *= 2
    raise SystemExit("all Overpass mirrors failed; cached data left untouched")


def main():
    osm_dir = sys.argv[1]
    refresh = "--refresh" in sys.argv
    names = [a for a in sys.argv[2:] if not a.startswith("--")] or list(QUERIES)
    for n in names:
        path, src = fetch(QUERIES[n], os.path.join(osm_dir, "cache"), refresh)
        shutil.copyfile(path, os.path.join(osm_dir, n + ".json"))
        print(f"{n}: {src}")


if __name__ == "__main__":
    main()
