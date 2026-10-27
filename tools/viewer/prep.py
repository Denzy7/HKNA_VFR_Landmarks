#!/usr/bin/env python3
"""Prepare the browser viewer's data from a finished ./build.sh run.

Usage: prep.py [ortho4xp_textures_dir]     (run from the repo root; view.sh does this)
IMAGERY=go2 (default) reads Ortho4XP's textures; IMAGERY=open downloads OpenAerialMap tiles.

Writes tools/viewer/data/: scene.json (object placements in local metres, landmark viewpoints,
ground tiles), features.json (each object's buildings and triangle ranges), atlas.png (the DDS atlas as PNG) and ground_<area>.jpg (Ortho4XP imagery
under each area, the same imagery the sim drapes).
"""
import json, math, os, shutil, sys
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "source"))
from ortho import Ortho, gpx, make_ortho, mosaic, tiles_for  # noqa: E402

M_LAT = 110574.0
LON0, LAT0 = 36.8217, -1.2865            # viewer origin: Nairobi CBD
KX = 111320.0 * math.cos(math.radians(LAT0))
OUT = "tools/viewer/data"
# imagery ground under the areas with modelled landmarks: name, (S, W, N, E)
AREAS = [
    ("cbd", (-1.2960, 36.8130, -1.2805, 36.8320)),
    ("upperhill", (-1.3060, 36.8030, -1.2890, 36.8180)),
    ("westlands", (-1.2720, 36.7720, -1.2560, 36.8110)),
]
GROUND_MAX_PX = 4096
OVERVIEW_PX = 1024          # per 4096 px ZL16 file: ~9.6 m/px


def local(lat, lon):
    return (lon - LON0) * KX, -(lat - LAT0) * M_LAT      # x east, z south (X-Plane OBJ axes)


def objects():
    out = []
    for tile in ("-02+036", "-02+037"):
        path = f"build/{tile}.txt"
        if not os.path.exists(path):
            continue
        defs = []
        for line in open(path):
            w = line.split()
            if not w:
                continue
            if w[0] == "OBJECT_DEF":
                defs.append(w[1])
            elif w[0] == "OBJECT":
                lon, lat, hdg = float(w[2]), float(w[3]), float(w[4])
                x, z = local(lat, lon)
                d = defs[int(w[1])]
                out.append(dict(obj=d if d.startswith("objects/") else None, lib=None if d.startswith("objects/") else d,
                                x=round(x, 2), z=round(z, 2), hdg=hdg))
    return out


def landmarks():
    out = []
    for line in open("landmarks.txt"):
        if line.startswith("#") or not line.strip():
            continue
        w = line.split()
        lat, lon, h = float(w[0]), float(w[1]), float(w[2].rstrip("m"))
        rest = line.split(None, 3)[3].strip()
        name = rest.split(" [")[0]
        kind = rest.split("[")[1].split("]")[0] if "[" in rest else ""
        x, z = local(lat, lon)
        out.append(dict(name=name, kind=kind, h=h, x=round(x, 1), z=round(z, 1), lat=lat, lon=lon,
                        ours="not modelled here" not in rest))
    return out


def ground(ortho):
    tiles = []
    for name, (s, w, n, e) in AREAS:
        dst = f"{OUT}/ground_{name}_{ortho.name}.jpg"
        if not os.path.exists(dst):      # imagery doesn't change between builds
            if ortho.name == "go2":
                arr, z = ortho.crop(w, s, e, n)
            else:                        # downloaded: ZL18 is plenty for a 4096 px ground image
                x0, y0 = map(int, gpx(w, n, 18))
                x1, y1 = map(int, gpx(e, s, 18))
                ortho.prefetch(tiles_for(x0, y0, x1, y1, 18))
                arr = mosaic(ortho, x0, y0, x1, y1, 18)
            if arr is None:
                print(f"ground {name}: no imagery")
                continue
            img = Image.fromarray(arr)
            k = GROUND_MAX_PX / max(img.size)
            if k < 1:
                img = img.resize((round(img.width * k), round(img.height * k)), Image.LANCZOS)
            img.save(dst, quality=88)
            print(f"ground {name}: {img.width}x{img.height}")
        x0, z0 = local(n, w)
        x1, z1 = local(s, e)
        tiles.append(dict(name=name, img=os.path.basename(dst), x0=x0, z0=z0, x1=x1, z1=z1, layer=1))
    return tiles


def overview(ortho, tex_dir, objs):
    """Low-res imagery under everything, over the span of the placed objects: Ortho4XP's ZL16
    textures scaled to OVERVIEW_PX per 4096 px file (go2), or ZL14 tiles (open, same scale)."""
    dst = f"{OUT}/ground_overview_{ortho.name}.jpg"
    lats = [LAT0 - o["z"] / M_LAT for o in objs]
    lons = [LON0 + o["x"] / KX for o in objs]
    s, n = max(min(lats) - 0.01, -1.999), min(max(lats) + 0.01, -1.001)        # stay inside -02+036
    w, e = max(min(lons) - 0.01, 36.001), min(max(lons) + 0.01, 36.999)
    z, F = 16, 4096
    x0, y0 = gpx(w, n, z)
    x1, y1 = gpx(e, s, z)
    fx0, fy0 = int(x0 // F) * F, int(y0 // F) * F
    fx1, fy1 = int(x1 // F) * F, int(y1 // F) * F
    k = OVERVIEW_PX / F
    if not os.path.exists(dst) and ortho.name != "go2":
        a0, b0 = map(int, gpx(w, n, 14))
        a1, b1 = map(int, gpx(e, s, 14))
        ortho.prefetch(tiles_for(a0, b0, a1, b1, 14))
        arr = mosaic(ortho, a0, b0, a1, b1, 14)
        if arr is not None:
            Image.fromarray(arr).save(dst, quality=85)
            print(f"ground overview: {a1 - a0}x{b1 - b0} px")
    if not os.path.exists(dst):
        cols, rows = (fx1 - fx0) // F + 1, (fy1 - fy0) // F + 1
        canvas = Image.new("RGB", (cols * OVERVIEW_PX, rows * OVERVIEW_PX), (110, 106, 80))
        for fy in range(fy0, fy1 + 1, F):
            for fx in range(fx0, fx1 + 1, F):
                f = f"{tex_dir}/{fy // 256}_{fx // 256}_GO2{z}.dds"
                if os.path.exists(f):
                    img = Image.open(f).convert("RGB").resize((OVERVIEW_PX, OVERVIEW_PX), Image.LANCZOS)
                    canvas.paste(img, ((fx - fx0) // F * OVERVIEW_PX, (fy - fy0) // F * OVERVIEW_PX))
        crop = tuple(round(v * k) for v in (x0 - fx0, y0 - fy0, x1 - fx0, y1 - fy0))
        canvas.crop(crop).save(dst, quality=85)
        print(f"ground overview: {crop[2] - crop[0]}x{crop[3] - crop[1]} px")
    xa, za = local(n, w)
    xb, zb = local(s, e)
    return dict(name="overview", img=os.path.basename(dst), x0=xa, z0=za, x1=xb, z1=zb, layer=0)


def main():
    os.makedirs(OUT, exist_ok=True)
    Image.open("objects/landmarks.dds").convert("RGB").save(f"{OUT}/atlas.png")
    if os.path.exists("build/features.json"):     # per-building triangle ranges (build.py)
        shutil.copyfile("build/features.json", f"{OUT}/features.json")
    objs = objects()
    tex_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    ortho = make_ortho(os.environ.get("IMAGERY", "go2"), tex_dir, "imagery_cache")
    scene = dict(origin=dict(lat=LAT0, lon=LON0), objects=objs, landmarks=landmarks(), imagery=ortho.credit,
                 ground=[overview(ortho, tex_dir, objs)] + ground(ortho))
    json.dump(scene, open(f"{OUT}/scene.json", "w"))
    print(f"viewer: {len(scene['objects'])} objects, {len(scene['landmarks'])} landmarks, "
          f"{len(scene['ground'])} ground tiles")


if __name__ == "__main__":
    main()
