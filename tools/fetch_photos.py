#!/usr/bin/env python3
"""Fetch openly licensed photos of the pack's buildings, for checking models against.

Usage (from the repo root, after ./build.sh):
  tools/fetch_photos.py                 # named landmarks and towers
  tools/fetch_photos.py 140320795 abcA0 # these building ids (as shown in the viewer)
  tools/fetch_photos.py --all           # every building
  options: --refresh (ignore cached results)  --no-commons  --no-mapillary

Sources (open licences only; never Google):
  - Wikimedia Commons: geotagged files near the building; title matches on the building's name
    rank first. No account needed.
  - Mapillary (CC BY-SA 4.0): street-level images whose camera faces the building. Needs a free
    token: MAPILLARY_TOKEN in build.local.sh or the environment; skipped without one.

Writes photos/img/<source>_<key>.jpg (each image once) and photos/<id>.json per building:
source, page, licence, author, camera position, its distance and bearing from the building.
photos/ is not in git: the images belong to their authors under their licences.
"""
import functools, json, math, os, re, sys, time, urllib.parse, urllib.request
print = functools.partial(print, flush=True)       # progress shows up in logs as it happens

UA = "HKNA-VFR-Landmarks/0.1 (X-Plane scenery, personal use; https://github.com/)"
OUT = "photos"
COMMONS = "https://commons.wikimedia.org/w/api.php"
MAPILLARY = "https://graph.mapillary.com/images"
PER_BUILDING = 6
# words that don't identify a building (every other word of its name must be in a photo's title)
FILLER = {"the", "and", "ltd", "limited", "co", "of", "by", "a", "b", "c", "d"}
SPELL = {"centre": "center", "theatre": "theater", "houe": "house", "eigthy": "eighty"}
SKIP_TITLE = re.compile(r"interior|inside|\.(svg|pdf|tiff?|gif|webm|ogv|ogg|mp4)$|logo|\bmap\b|\bplan\b|diagram|"
                        r"portrait|archives uk|exhibition|installation|guest|staircase|stairs|rate card|"
                        r"\bsign\b|plaque|ticket|gallery|lobby|hall interior|office|desk|animal|leopard|lion|"
                        r"tortoise|buffalo|giraffe|safari", re.I)
# a title that reads like a street scene: such photos usually show the buildings along it
STREET = re.compile(r"street|avenue|\broad\b|\bway\b|highway|\blane\b|skyline|downtown|\bcbd\b|"
                    r"city cent(er|re)|aerial", re.I)
NEARBY_MAX = 3
M_LAT = 110574.0
last_call = [0.0]


def get(url, params=None, binary=False, tries=5):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for k in range(tries):
        wait = 0.6 - (time.time() - last_call[0])       # under 2 requests/s
        if wait > 0:
            time.sleep(wait)
        last_call[0] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
                body = r.read()
            if binary:
                return body
            j = json.loads(body)
            if isinstance(j, dict) and "error" in j:    # API-level error (e.g. rate limit): retry
                raise RuntimeError(j["error"].get("info") or j["error"].get("code") or j["error"])
            return j
        except Exception as e:      # 429/5xx/timeouts/API errors: back off and retry
            if k == tries - 1:
                raise
            print(f"  retry {k + 1}: {e}", file=sys.stderr)
            time.sleep(5 * (k + 1))


def dist_brg(la0, lo0, la1, lo1):
    """metres and compass bearing from point 0 to point 1"""
    kx = 111320.0 * math.cos(math.radians(la0))
    dx, dn = (lo1 - lo0) * kx, (la1 - la0) * M_LAT
    return math.hypot(dx, dn), (math.degrees(math.atan2(dx, dn)) + 360) % 360


def norm_words(text):
    return [SPELL.get(w, w) for w in re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))]


def name_match(name, title):
    """every identifying word of the building's name appears in the title"""
    ws = [w for w in norm_words(name) if w not in FILLER]
    tw = set(norm_words(title))
    return bool(ws) and all(w in tw for w in ws)


def save_image(key, url):
    path = f"{OUT}/img/{key}.jpg"
    if not os.path.exists(path):
        data = get(url, binary=True)
        with open(path + ".tmp", "wb") as f:
            f.write(data)
        os.replace(path + ".tmp", path)
    return f"img/{key}.jpg"


def save_pano_view(key, url, centre_heading, look, fov=95):
    """crop an equirectangular panorama (its centre column faces centre_heading) to fov
    degrees around the compass direction look, and the middle band of its height"""
    from PIL import Image
    import io
    path = f"{OUT}/img/{key}.jpg"
    if not os.path.exists(path):
        img = Image.open(io.BytesIO(get(url, binary=True))).convert("RGB")
        w, h = img.size
        cx = ((look - centre_heading) / 360.0 + 0.5) % 1.0 * w
        half = fov / 360.0 * w / 2
        x0, x1 = int(cx - half), int(cx + half)
        if x0 < 0 or x1 > w:              # wraps around the seam: tile it twice side by side
            wide = Image.new("RGB", (w * 2, h))
            wide.paste(img, (0, 0)); wide.paste(img, (w, 0))
            img, x0, x1 = wide, x0 + w if x0 < 0 else x0, x1 + w if x0 < 0 else x1
        img.crop((x0, int(h * 0.2), x1, int(h * 0.75))).save(path + ".tmp.jpg", quality=88)
        os.replace(path + ".tmp.jpg", path)
    return f"img/{key}.jpg"


def strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def commons(t):
    """geotagged Commons files near the building, best name matches first"""
    radius = int(min(10000, max(80, t["r"] + 120)))
    res = get(COMMONS, dict(action="query", list="geosearch", gscoord=f"{t['lat']}|{t['lon']}", gsradius=radius,
                            gsnamespace=6, gslimit=100, format="json"))["query"]["geosearch"]
    named, nearby = [], []
    for g in res:
        title = g["title"].removeprefix("File:")
        if SKIP_TITLE.search(title):
            continue
        if t["name"] and name_match(t["name"], title) and g["dist"] <= t["r"] + 400:
            named.append(g)
        elif g["dist"] <= t["r"] + 30 and STREET.search(title):
            nearby.append(g)          # street scene right beside it: usually shows the building
    named.sort(key=lambda g: g["dist"])
    nearby.sort(key=lambda g: g["dist"])
    for g in named:
        g["match"] = "name"
    for g in nearby:
        g["match"] = "nearby"
    cand = named[:PER_BUILDING] + nearby[:max(0, min(NEARBY_MAX, PER_BUILDING - len(named)))]
    if not cand:
        return []
    info = get(COMMONS, dict(action="query", titles="|".join(c["title"] for c in cand), prop="imageinfo",
                             iiprop="url|extmetadata|mime", iiurlwidth=1280, format="json",
                             iiextmetadatafilter="LicenseShortName|Artist|DateTimeOriginal|UsageTerms"))
    by_title = {p["title"]: p for p in info["query"]["pages"].values()}
    out = []
    for c in cand:
        p = by_title.get(c["title"])
        ii = (p or {}).get("imageinfo", [{}])[0]
        if not ii.get("thumburl") or not str(ii.get("mime", "")).startswith("image/"):
            continue
        md = ii.get("extmetadata", {})
        lic = md.get("LicenseShortName", {}).get("value", "")
        if not lic:
            continue      # no stated licence: leave it
        key = f"commons_{p['pageid']}"
        d, brg = dist_brg(t["lat"], t["lon"], c["lat"], c["lon"])
        out.append(dict(src="Wikimedia Commons", file=save_image(key, ii["thumburl"]), page=ii.get("descriptionurl"),
                        title=c["title"].removeprefix("File:"), license=lic, author=strip_html(md.get("Artist", {}).get("value")),
                        date=strip_html(md.get("DateTimeOriginal", {}).get("value"))[:10],
                        lat=c["lat"], lon=c["lon"], dist=round(d), bearing=round(brg), heading=None,
                        match=c["match"]))
        if len(out) >= PER_BUILDING:
            break
    return out


def mapillary(t, token):
    """street-level images whose camera points at the building, spread around it"""
    # close shots are the useful ones; tall buildings may be a little further off
    reach = min(t["r"], 150) + min(300, max(120, 3 * t["h"]))
    kx = 111320.0 * math.cos(math.radians(t["lat"]))
    bbox = (t["lon"] - reach / kx, t["lat"] - reach / M_LAT, t["lon"] + reach / kx, t["lat"] + reach / M_LAT)
    res = get(MAPILLARY, dict(access_token=token, bbox=",".join(f"{v:.6f}" for v in bbox), limit=500,
                              fields="id,thumb_1024_url,thumb_2048_url,computed_geometry,geometry,compass_angle,"
                                     "computed_compass_angle,captured_at,creator,is_pano")).get("data", [])
    cand = []
    for im in res:
        geo = im.get("computed_geometry") or im.get("geometry")
        heading = im.get("computed_compass_angle", im.get("compass_angle"))
        if not geo or heading is None or not im.get("thumb_1024_url"):
            continue
        lo, la = geo["coordinates"]
        d, brg_to_cam = dist_brg(t["lat"], t["lon"], la, lo)
        if not (max(12, t["r"] * 0.6) <= d <= reach):
            continue
        look = (brg_to_cam + 180) % 360                 # direction from the camera to the building
        # a panorama sees every way (it is cropped towards the building), a photo must face it
        off = 0 if im.get("is_pano") else abs((heading - look + 180) % 360 - 180)
        if off > 28:
            continue
        age = (time.time() - (im.get("captured_at") or 0) / 1000) / (365.25 * 86400)
        # panoramas come last: usually car-roof shots, often looking into roadside trees
        cand.append((off / 28 + 2 * d / reach + min(age, 15) * 0.12 + (0.6 if im.get("is_pano") else 0),
                     brg_to_cam, d, heading, la, lo, im))
    cand.sort(key=lambda c: c[0])
    out, used = [], []
    panos = 0
    for score, brg, d, heading, la, lo, im in cand:   # spread the views around the building
        if any(abs((brg - b + 180) % 360 - 180) < 35 for b in used):
            continue
        if im.get("is_pano"):
            if panos >= 2:
                continue
            panos += 1
        used.append(brg)
        key = f"mapillary_{im['id']}"
        if im.get("is_pano"):      # 360° image: keep the ~90° slice that looks at the building
            look = (brg + 180) % 360
            key += f"_{round(look)}"
            file = save_pano_view(key, im.get("thumb_2048_url") or im["thumb_1024_url"], heading, look)
            heading = look
        else:
            file = save_image(key, im["thumb_1024_url"])
        out.append(dict(src="Mapillary", file=file,
                        page=f"https://www.mapillary.com/app/?pKey={im['id']}", title="", license="CC BY-SA 4.0",
                        author=(im.get("creator") or {}).get("username", "Mapillary contributors"),
                        date=time.strftime("%Y-%m-%d", time.gmtime((im.get("captured_at") or 0) / 1000)),
                        lat=la, lon=lo, dist=round(d), bearing=round(brg), heading=round(heading),
                        pano=bool(im.get("is_pano")), match="facing"))
        if len(out) >= PER_BUILDING:
            break
    return out


def targets(args):
    index = json.load(open("build/features.json"))
    feats = [f for fs in index.values() for f in fs]
    ids = [a for a in args if not a.startswith("--")]
    if ids:
        sel = [f for f in feats if f["id"] in ids]
        missing = set(ids) - {f["id"] for f in sel}
        if missing:
            print("not in the build:", " ".join(sorted(missing)))
    elif "--all" in args:
        sel = feats
    else:
        sel = [f for f in feats if f["name"]]
    # buildings sharing a name (parts of one landmark) are searched once, around all of them
    groups = {}
    for f in sel:
        groups.setdefault(f["name"] or f"#{f['id']}", []).append(f)
    out = []
    for name, fs in groups.items():
        la = sum(f["lat"] for f in fs) / len(fs)
        lo = sum(f["lon"] for f in fs) / len(fs)
        r = max(f["r"] + dist_brg(la, lo, f["lat"], f["lon"])[0] for f in fs)
        out.append(dict(name=fs[0]["name"], ids=[f["id"] for f in fs], lat=la, lon=lo, r=r, h=max(f["h"] for f in fs)))
    return out


def main():
    args = sys.argv[1:]
    os.makedirs(f"{OUT}/img", exist_ok=True)
    token = os.environ.get("MAPILLARY_TOKEN", "")
    if not token and os.path.exists("build.local.sh"):       # MAPILLARY_TOKEN="..." there
        m = re.search(r'^\s*(?:export\s+)?MAPILLARY_TOKEN=["\']?([^"\'\s]+)', open("build.local.sh").read(), re.M)
        token = m.group(1) if m else ""
    use_commons = "--no-commons" not in args
    use_mly = bool(token) and "--no-mapillary" not in args
    if not token and "--no-mapillary" not in args:
        print("Mapillary: no MAPILLARY_TOKEN, skipped (Commons only)")
    ts = targets(args)
    print(f"{len(ts)} buildings to look for")
    total = 0
    for k, t in enumerate(ts, 1):
        first = f"{OUT}/{t['ids'][0]}.json"
        if os.path.exists(first) and "--refresh" not in args:
            continue
        photos = []
        try:
            if use_commons:
                photos += commons(t)
            if use_mly:
                photos += mapillary(t, token)
        except Exception as e:
            print(f"  {t['name'] or t['ids'][0]}: failed ({e}); will retry next run")
            continue
        rec = dict(name=t["name"], ids=t["ids"], lat=round(t["lat"], 6), lon=round(t["lon"], 6),
                   fetched=time.strftime("%Y-%m-%d"), photos=photos)
        for i in t["ids"]:
            with open(f"{OUT}/{i}.json", "w") as f:
                json.dump(rec, f, indent=1)
        total += len(photos)
        print(f"[{k}/{len(ts)}] {t['name'] or t['ids'][0]}: {len(photos)} photos")
    used = set()
    for f in os.listdir(OUT):
        if f.endswith(".json"):
            used |= {p["file"] for p in json.load(open(f"{OUT}/{f}"))["photos"]}
    stale = [f for f in os.listdir(f"{OUT}/img") if f"img/{f}" not in used]
    for f in stale:
        os.remove(f"{OUT}/img/{f}")
    print(f"done: {total} new photos in {OUT}/" + (f", {len(stale)} unused images deleted" if stale else ""))


if __name__ == "__main__":
    main()
