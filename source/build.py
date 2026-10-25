#!/usr/bin/env python3
"""Generate HKNA_VFR_Landmarks (Nairobi region VFR landmarks) X-Plane 11 overlay from OSM
extracts + Ortho4XP imagery.

Usage: build.py <osm_dir> <out_pkg_dir> <ortho4xp_textures_dir> [DDSTool]

Building colours come from the same Ortho4XP DDS imagery the sim shows: low roofs are
textured with the imagery itself, walls/tall roofs are sampled from it (the imagery is
not true-ortho, so tall buildings lean; LEAN_E/LEAN_N compensate).
"""
import bisect, json, math, os, re, sys, random, hashlib, subprocess
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ortho import Ortho, crop_px, make_ortho, tiles_for  # noqa: E402

OSM, OUT, ORTHO_DIR = sys.argv[1], sys.argv[2], sys.argv[3]
DDSTOOL = sys.argv[4] if len(sys.argv) > 4 else None
# optional: the installed X-Plane Custom Scenery folder + DSFTool, to read simHeaven's own
# placements at build time (nothing from simHeaven is stored in this repo)
XP_SCENERY = os.environ.get("XP_CUSTOM_SCENERY")
DSFTOOL = os.environ.get("DSFTOOL")
os.makedirs(f"{OUT}/objects", exist_ok=True)
os.makedirs(f"{OUT}/Earth nav data/-10+030", exist_ok=True)

M_LAT = 110574.0
FLOOR = 3.4                  # metres per level
SINK = 4.0                   # walls extend below ground for sloping terrain
LOD = 30000

# ---------------------------------------------------------------- atlas layout
TEX = 4096
STRIP = 16                   # px per facade strip = 1 floor
FLOORS_PER_STRIP = 1
STRIP_M = FLOORS_PER_STRIP * 3.5
N_PUNCH, N_GLASS = 56, 6
BINS = N_PUNCH + N_GLASS               # NCPB silo elevator tower (concrete, small windows)
SHOPFRONT = BINS + 1                   # ground-floor shopfronts
FAC_H = 64 * STRIP                     # facade region rows 0..1023
U_M = (TEX // 16) * 3.5                # metres of wall per texture width (16 px bays)
BASE_H = 4.5                           # shopfront storey height

# imagery: IMAGERY=go2 (Ortho4XP's own textures, default) or open (Maxar via OpenAerialMap)
IMAGERY = os.environ.get("IMAGERY", "go2")
ortho = make_ortho(IMAGERY, ORTHO_DIR, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "imagery_cache"))
print(f"imagery: {IMAGERY} ({ortho.credit})")
# imagery lean of tall buildings (metres of roof displacement per metre of height, E and N):
# depends on the satellite's view, so each source carries its own (see ortho.py)
LEAN_E, LEAN_N = ortho.lean
MPP = 156543.03 * math.cos(math.radians(-1.3)) / 2 ** 19     # ZL19 metres per pixel


def gpx(lon, lat):
    return Ortho.gpx(lon, lat, 19)


def lean_px(h):
    return LEAN_E * h / MPP, -LEAN_N * h / MPP


# ---------------------------------------------------------------- imagery sampling
def poly_crop(pts, shift, pad=3):
    """Crop the imagery under lon/lat polygon pts shifted by (dx,dy) px. Returns
    (rgb array, mask, origin_px) or None."""
    P = [gpx(lo, la) for lo, la in pts]
    P = [(x + shift[0], y + shift[1]) for x, y in P]
    x0 = int(math.floor(min(p[0] for p in P))) - pad
    y0 = int(math.floor(min(p[1] for p in P))) - pad
    x1 = int(math.ceil(max(p[0] for p in P))) + pad
    y1 = int(math.ceil(max(p[1] for p in P))) + pad
    if (x1 - x0) * (y1 - y0) > 2500 * 2500:
        return None
    a = crop_px(ortho, x0, y0, x1, y1)
    if a is None:
        return None
    m = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(m).polygon([(x - x0, y - y0) for x, y in P], fill=255)
    return a, np.asarray(m) > 0, (x0, y0)


def robust_colour(a, mask, min_px=12):
    px = a[mask].astype(np.float32)
    if len(px) < min_px:
        return None
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    lum = 0.3 * r + 0.59 * g + 0.11 * b
    keep = (lum > 55) & ~((g > r + 10) & (g > b + 4))      # drop shadows and vegetation
    if keep.sum() < max(min_px, 0.15 * len(px)):
        keep = lum > 35
        if keep.sum() < min_px:
            return None
    sel = px[keep]
    lo, hi = np.percentile(sel, [20, 80], axis=0)
    trimmed = sel[np.all((sel >= lo) & (sel <= hi), axis=1)]
    return tuple(float(c) for c in (trimmed if len(trimmed) else sel).mean(axis=0))


def sat_lum(c):
    mx, mn = max(c), min(c)
    return (mx - mn) / (mx + 1e-6), 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]


def mix(c, d, t):
    return tuple(a + (b - a) * t for a, b in zip(c, d))


NAMED = {"white": (240, 238, 230), "grey": (166, 161, 159), "gray": (166, 161, 159), "black": (58, 58, 58),
         "blue": (95, 125, 150), "red": (150, 70, 60), "brown": (138, 106, 95), "yellow": (220, 195, 120),
         "beige": (232, 220, 176), "silver": (204, 199, 196)}


def parse_colour(s):
    if not s:
        return None
    s = s.strip().lower()
    if s in NAMED:
        return NAMED[s]
    h = s.lstrip("#")
    if re.fullmatch(r"[0-9a-f]{6}|[0-9a-f]{3}", h):
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return None


def outline_px(pts):
    return [gpx(lo, la) for lo, la in pts]


def refine_shift(pts, h):
    """Find where the building's roof outline actually sits in the imagery: slide the
    footprint around the expected lean offset and maximise image gradient along it.
    Returns (shift_px, confident)."""
    ex, ey = lean_px(h)
    R = int(max(6, 0.12 * h / MPP))
    P = outline_px(pts)
    x0 = int(min(p[0] for p in P) + ex) - R - 3
    y0 = int(min(p[1] for p in P) + ey) - R - 3
    x1 = int(max(p[0] for p in P) + ex) + R + 4
    y1 = int(max(p[1] for p in P) + ey) + R + 4
    if (x1 - x0) * (y1 - y0) > 1600 * 1600:
        return (ex, ey), False
    a = crop_px(ortho, x0, y0, x1, y1)
    if a is None:
        return (ex, ey), False
    g = a.astype(np.float32) @ np.array([0.3, 0.59, 0.11], np.float32)
    grad = np.zeros_like(g)
    grad[1:-1, 1:-1] = np.abs(g[1:-1, 2:] - g[1:-1, :-2]) + np.abs(g[2:, 1:-1] - g[:-2, 1:-1])
    S = []
    for k in range(len(P)):
        (ax, ay), (bx, by) = P[k - 1], P[k]
        n = max(1, int(math.hypot(bx - ax, by - ay)))
        for i in range(n):
            t = (i + 0.5) / n
            S.append((ax + (bx - ax) * t + ex - x0, ay + (by - ay) * t + ey - y0))
    S = np.array(S)
    if len(S) < 12:
        return (ex, ey), False
    offs = np.arange(-R, R + 1)
    dx, dy = np.meshgrid(offs, offs)
    dx, dy = dx.ravel(), dy.ravel()
    xi = np.clip((S[None, :, 0] + dx[:, None]).astype(int), 0, grad.shape[1] - 1)
    yi = np.clip((S[None, :, 1] + dy[:, None]).astype(int), 0, grad.shape[0] - 1)
    score = grad[yi, xi].mean(1)
    score -= 0.25 * score.mean() * np.hypot(dx, dy) / R          # prefer the expected lean
    best = int(np.argmax(score))
    confident = score[best] > 1.35 * np.median(score)
    return (ex + dx[best], ey + dy[best]), bool(confident)


def boost(c):
    """Imagery is hazy and darker than the real surfaces: lift contrast/saturation a bit."""
    m = sum(c) / 3
    return tuple(max(0, min(255, (m + (v - m) * 1.25) * 1.10)) for v in c)


def analyse(f):
    """Fill f['wall'] (rgb), f['glass'], f['roof'] ('img', crop) | ('rgb', colour), f['shift']."""
    t, h = f["t"], f["h"]
    mat = (t.get("building:material") or "").lower()
    osm_c = parse_colour(t.get("building:colour"))
    f["glass"] = mat in ("glass", "mirror")
    shift, ok = refine_shift(f["pts"], h)
    f["shift"] = shift
    roof_c = None
    f["roof"] = None
    c = poly_crop(f["pts"], shift)
    if c:
        roof_c = robust_colour(c[0], c[1])
        px = c[0][c[1]].astype(np.float32)
        veg = ((px[:, 1] > px[:, 0] + 10) & (px[:, 1] > px[:, 2] + 4)).mean() if len(px) else 1
        dark = ((px @ np.array([0.3, 0.59, 0.11], np.float32)) < 50).mean() if len(px) else 1
        plausible = h <= 30 or (veg < 0.2 and dark < 0.35)
        if (ok or h <= 30) and plausible:
            f["roof"] = ("img", c)
        elif not plausible:
            roof_c = None
    wall = None
    if h >= 25:
        # tall: facade pixels lie between the footprint and the displaced roof
        P = outline_px(f["pts"])
        xs = [p[0] + shift[0] * t_ for p in P for t_ in (0, 1)]
        ys = [p[1] + shift[1] * t_ for p in P for t_ in (0, 1)]
        x0, y0, x1, y1 = int(min(xs)) - 2, int(min(ys)) - 2, int(max(xs)) + 3, int(max(ys)) + 3
        a = crop_px(ortho, x0, y0, x1, y1) if (x1 - x0) * (y1 - y0) < 2500 * 2500 else None
        if a is not None:
            m = Image.new("L", (x1 - x0, y1 - y0), 0)
            d = ImageDraw.Draw(m)
            for t_ in (0.2, 0.35, 0.5, 0.65, 0.8):
                d.polygon([(p[0] + shift[0] * t_ - x0, p[1] + shift[1] * t_ - y0) for p in P], fill=255)
            d.polygon([(p[0] + shift[0] - x0, p[1] + shift[1] - y0) for p in P], fill=0)   # not the roof
            wall = robust_colour(a, np.asarray(m) > 0)
        if wall:
            wall = boost(wall)
            s_, l_ = sat_lum(wall)
            if not f["glass"] and wall[2] > wall[0] + 6 and l_ < 125:
                f["glass"] = True          # dark bluish facade = curtain wall
    elif roof_c:
        # low: only the roof is visible. painted iron-sheet roofs (blue/green/red) sit on
        # plastered walls; flat concrete roofs share the wall colour
        s_, l_ = sat_lum(roof_c)
        wall = mix((236, 230, 214), roof_c, 0.08) if s_ > 0.22 else boost(mix(roof_c, (240, 236, 226), 0.3))
    if f.get("core") and osm_c and h < 25:
        wall = osm_c
    if wall is None:
        wall = osm_c or (226, 218, 196)
    f["wall"] = wall
    if f["roof"] is None:
        f["roof"] = ("rgb", boost(roof_c) if roof_c else tuple(v * 0.8 for v in wall))


def kmeans(X, k, w=None, it=25):
    """Weighted k-means with farthest-first seeding, so rare colours of tall landmarks
    keep their own strip instead of being averaged into the common beige."""
    X = np.asarray(X, np.float32)
    w = np.ones(len(X), np.float32) if w is None else np.asarray(w, np.float32)
    if len(X) <= k:
        pad = np.tile(X.mean(0) if len(X) else np.array([200, 200, 200], np.float32), (k - len(X), 1))
        return np.vstack([X, pad])
    C = [X[int(np.argmax(w))]]
    d = ((X - C[0]) ** 2).sum(1)
    for _ in range(k - 1):
        C.append(X[int(np.argmax(d * np.sqrt(w)))])
        d = np.minimum(d, ((X - C[-1]) ** 2).sum(1))
    C = np.array(C)
    for _ in range(it):
        L = np.argmin(((X[:, None, :] - C[None]) ** 2).sum(-1), 1)
        for j in range(k):
            m = L == j
            if m.any():
                C[j] = (X[m] * w[m, None]).sum(0) / w[m].sum()
    return C


# ---------------------------------------------------------------- atlas
class Atlas:
    """Facade strips (top) + shelf-packed imagery roofs and solid swatches (bottom)."""

    def __init__(self):
        self.day = np.zeros((TEX, TEX, 3), np.float32)
        self.lit = np.zeros((TEX, TEX, 3), np.float32)
        self.items = []      # (key, w, h)
        self.place = {}
        self.swatches = {}

    def swatch(self, rgb):
        q = tuple(int(round(c / 3)) * 3 for c in rgb)
        if q not in self.swatches:
            key = ("sw", q)
            self.swatches[q] = key
            self.items.append((key, 8, 8, None))
        return self.swatches[q]

    def add_img(self, key, arr):
        self.items.append((key, arr.shape[1], arr.shape[0], arr))
        return key

    def pack(self):
        PAD = 4
        avail_h = TEX - FAC_H
        scale = 1.0
        while True:
            x = y = row_h = 0
            place = {}
            ok = True
            for key, w, h, arr in sorted(self.items, key=lambda it: -it[2]):
                sw = 8 if arr is None else max(2, int(w * scale))
                sh = 8 if arr is None else max(2, int(h * scale))
                if x + sw + 2 * PAD > TEX:
                    x, y, row_h = 0, y + row_h, 0
                if y + sh + 2 * PAD > avail_h:
                    ok = False
                    break
                place[key] = (x + PAD, FAC_H + y + PAD, sw, sh, w, h)
                x += sw + 2 * PAD
                row_h = max(row_h, sh + 2 * PAD)
            if ok:
                break
            scale *= 0.9
        self.place, self.scale = place, scale
        for key, w, h, arr in self.items:
            px, py, sw, sh, _, _ = place[key]
            if arr is None:
                block = np.ones((sh + 8, sw + 8, 3), np.float32) * np.array(key[1], np.float32)
            else:
                img = Image.fromarray(arr)
                if (sw, sh) != (w, h):
                    img = img.resize((sw, sh), Image.LANCZOS)
                a = np.asarray(img, np.float32)
                block = np.pad(a, ((4, 4), (4, 4), (0, 0)), mode="edge")   # bleed guard for mips
            self.day[py - 4:py + sh + 4, px - 4:px + sw + 4] = block
        return scale

    def uv_px(self, key, ix, iy):
        """Atlas uv for pixel (ix, iy) inside the source image registered as key."""
        px, py, sw, sh, w, h = self.place[key]
        ax = px + ix * sw / w
        ay = py + iy * sh / h
        return ax / TEX, 1.0 - ay / TEX

    def sw_uv(self, key):
        px, py, sw, sh, _, _ = self.place[key]
        return (px + sw / 2) / TEX, 1.0 - (py + sh / 2) / TEX

    def facades(self, punch, glass, bins):
        rnd = random.Random(7)
        fpx = STRIP // FLOORS_PER_STRIP
        bpx = 16
        styles = [("punch", c) for c in punch] + [("glass", c) for c in glass] + [("bins", bins), ("shop", (70, 72, 74))]
        for s, (style, col) in enumerate(styles):
            base = np.array(col, np.float32)
            y0 = s * STRIP
            self.day[y0:y0 + STRIP] = base
            if style == "bins":
                # silo elevator tower: plain concrete, a small dark window per floor every ~7 m
                for fl in range(FLOORS_PER_STRIP):
                    fy = y0 + fl * fpx
                    for bx in range(8, TEX, 32):
                        self.day[fy + 6:fy + 11, bx:bx + 5] = (52, 54, 56)
                        if rnd.random() < 0.15:
                            self.lit[fy + 7:fy + 10, bx + 1:bx + 4] = (255, 214, 150)
                    self.day[fy:fy + 1] *= 0.95
                continue
            for fl in range(FLOORS_PER_STRIP):
                fy = y0 + fl * fpx
                for b in range(TEX // bpx):
                    bx = b * bpx
                    if style == "punch":
                        wy0, wy1, wx0, wx1 = fy + 4, fy + 12, bx + 3, bx + 13
                        gl = np.array([46, 56, 66], np.float32) + rnd.uniform(-6, 10)
                        p_lit = 0.30
                    elif style == "glass":
                        wy0, wy1, wx0, wx1 = fy + 2, fy + 15, bx + 1, bx + 16
                        gl = base * 0.82 + rnd.uniform(-7, 7)
                        p_lit = 0.30
                    else:   # shopfront: tall dark glazing with frames
                        wy0, wy1, wx0, wx1 = fy + 3, fy + 15, bx + 1, bx + 15
                        gl = np.array([34, 40, 46], np.float32) + rnd.uniform(-4, 8)
                        p_lit = 0.65
                    self.day[wy0:wy1, wx0:wx1] = np.clip(gl, 0, 255)
                    self.day[wy0:wy0 + 2, wx0:wx1] += 14
                    if rnd.random() < p_lit:
                        warm = rnd.choice([(255, 214, 150), (255, 236, 200), (210, 230, 255)])
                        self.lit[wy0 + 1:wy1 - 1, wx0 + 1:wx1 - 1] = np.array(warm, np.float32) * rnd.uniform(0.55, 1.0)
                self.day[fy:fy + 1] *= 0.86

    def save(self):
        day = f"{OUT}/objects/landmarks.png"
        lit = f"{OUT}/objects/landmarks_LIT.png"
        Image.fromarray(np.clip(self.day, 0, 255).astype(np.uint8)).save(day)
        Image.fromarray(np.clip(self.lit, 0, 255).astype(np.uint8)).save(lit)
        if DDSTOOL:
            subprocess.run([DDSTOOL, "--png2dxt1", day, day[:-4] + ".dds"], check=True, capture_output=True)
            subprocess.run([DDSTOOL, "--png2dxt1", "--scale_half", lit, lit[:-4] + ".dds"], check=True,
                           capture_output=True)
            os.remove(day)
            os.remove(lit)
            return "landmarks.dds", "landmarks_LIT.dds"
        return "landmarks.png", "landmarks_LIT.png"


atlas = Atlas()


def strip_v(s, frac):
    """V coordinate (OBJ: v=0 bottom) for strip s at fractional height frac (0 bottom..1 top)."""
    top_px = s * STRIP + 0.5
    bot_px = (s + 1) * STRIP - 0.5
    return 1.0 - (bot_px - frac * (bot_px - top_px)) / TEX


# ---------------------------------------------------------------- geometry
def area2(P):
    return sum(P[i - 1][0] * P[i][1] - P[i][0] * P[i - 1][1] for i in range(len(P))) / 2


def centroid(P):
    return sum(p[0] for p in P) / len(P), sum(p[1] for p in P) / len(P)


def triangulate(P):
    """Ear clipping on a CCW polygon in (x, north). Returns index triples."""
    idx = list(range(len(P)))
    tris = []

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def inside(p, a, b, c):
        return cross(a, b, p) >= 0 and cross(b, c, p) >= 0 and cross(c, a, p) >= 0

    guard = 0
    while len(idx) > 3 and guard < 10000:
        guard += 1
        n = len(idx)
        for k in range(n):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % n]
            a, b, c = P[i0], P[i1], P[i2]
            if cross(a, b, c) <= 1e-9:
                continue
            if any(inside(P[j], a, b, c) for j in idx if j not in (i0, i1, i2)):
                continue
            tris.append((i0, i1, i2))
            idx.pop(k)
            break
        else:  # degenerate leftovers: fan them
            break
    for k in range(1, len(idx) - 1):
        tris.append((idx[0], idx[k], idx[k + 1]))
    return tris


class Ctx:
    """Local frame of the object being built (origin lon/lat)."""
    lon0 = lat0 = kx = 0.0

    @classmethod
    def set(cls, lon0, lat0):
        cls.lon0, cls.lat0 = lon0, lat0
        cls.kx = 111320.0 * math.cos(math.radians(lat0))

    @classmethod
    def lonlat(cls, x, n):
        return cls.lon0 + x / cls.kx, cls.lat0 + n / M_LAT


class Mat:
    """Roof/solid material: a swatch colour, or imagery registered in the atlas."""

    def __init__(self, rgb=None, img=None):
        self.key = atlas.swatch(rgb) if rgb is not None else None
        self.img = img      # (atlas key, origin_px, shift_px)

    def uv(self, x, n):
        if self.img is None:
            return atlas.sw_uv(self.key)
        key, (ox, oy), (sx, sy) = self.img
        gx, gy = gpx(*Ctx.lonlat(x, n))
        return atlas.uv_px(key, gx + sx - ox, gy + sy - oy)


class Obj:
    def __init__(self):
        self.v, self.i, self.lights = [], [], []

    def vert(self, x, y, n, nx, ny, nn, u, v):
        # OBJ space: +x east, +y up, -z north
        self.v.append((x, y, -n, nx, ny, -nn, u, v))
        return len(self.v) - 1

    def tri(self, a, b, c):
        self.i += [a, b, c]

    def quad(self, a, b, c, d):
        self.tri(a, b, c)
        self.tri(a, c, d)

    def obs(self, x, y, n):
        self.lights.append(f"LIGHT_NAMED obs_red_night {x:.2f} {y:.2f} {-n:.2f}")

    def write(self, path, tex, lit):
        with open(path, "w") as f:
            f.write(f"I\n800\nOBJ\n\nTEXTURE {tex}\nTEXTURE_LIT {lit}\n")
            # the "lights" count is for VLIGHT entries only; LIGHT_NAMED/LIGHT_PARAM are not counted
            f.write(f"POINT_COUNTS {len(self.v)} 0 0 {len(self.i)}\n\n")
            for x, y, z, nx, ny, nz, u, v in self.v:
                f.write(f"VT {x:.3f} {y:.3f} {z:.3f} {nx:.4f} {ny:.4f} {nz:.4f} {u:.5f} {v:.5f}\n")
            full = len(self.i) - len(self.i) % 10
            for k in range(0, full, 10):
                f.write("IDX10 " + " ".join(map(str, self.i[k:k + 10])) + "\n")
            for c in self.i[full:]:
                f.write(f"IDX {c}\n")
            f.write(f"\nATTR_LOD 0 {LOD}\nATTR_no_cull\nTRIS 0 {len(self.i)}\n")
            for l in self.lights:
                f.write(l + "\n")


def wall_band(o, a, b, y0, y1, s, u0, ta=None, tb=None, base=False):
    """Wall y0..y1 along edge a->b (optionally tapering to ta->tb), in facade strip s;
    with base=True the ground storey uses the shopfront strip."""
    ta = ta or a
    tb = tb or b
    dx, dn = b[0] - a[0], b[1] - a[1]
    L = math.hypot(dx, dn)
    if L < 0.05:
        return u0
    nx, nn = dn / L, -dx / L          # outward normal for CCW polygon
    u1 = u0 + L / U_M
    cuts = {y0, y1}
    k = math.floor(y0 / STRIP_M) + 1
    while k * STRIP_M < y1:
        cuts.add(k * STRIP_M)
        k += 1
    if base and y0 < BASE_H < y1:
        cuts.add(BASE_H)
    cuts = sorted(cuts)
    for ya, yb in zip(cuts, cuts[1:]):
        st = SHOPFRONT if base and yb <= BASE_H + 1e-6 else s
        cell = math.floor(max(ya, 0) / STRIP_M + 1e-6)
        f0 = max(0.0, ya / STRIP_M - cell)
        f1 = min(1.0, yb / STRIP_M - cell)
        t0, t1 = (ya - y0) / (y1 - y0), (yb - y0) / (y1 - y0)
        P = [(a[0] + (ta[0] - a[0]) * t, a[1] + (ta[1] - a[1]) * t) for t in (t0, t1)]
        Q = [(b[0] + (tb[0] - b[0]) * t, b[1] + (tb[1] - b[1]) * t) for t in (t0, t1)]
        i0 = o.vert(P[0][0], ya, P[0][1], nx, 0, nn, u0, strip_v(st, f0))
        i1 = o.vert(Q[0][0], ya, Q[0][1], nx, 0, nn, u1, strip_v(st, f0))
        i2 = o.vert(Q[1][0], yb, Q[1][1], nx, 0, nn, u1, strip_v(st, f1))
        i3 = o.vert(P[1][0], yb, P[1][1], nx, 0, nn, u0, strip_v(st, f1))
        o.quad(i0, i1, i2, i3)
    return u1


def flat_cap(o, P, y, mat, up=True):
    ids = [o.vert(p[0], y, p[1], 0, 1 if up else -1, 0, *mat.uv(p[0], p[1])) for p in P]
    for a, b, c in triangulate(P):
        o.tri(ids[a], ids[b], ids[c]) if up else o.tri(ids[a], ids[c], ids[b])


def solid_walls(o, P, y0, y1, mat):
    for k in range(len(P)):
        a, b = P[k - 1], P[k]
        dx, dn = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dn) or 1
        nx, nn = dn / L, -dx / L
        u, v = mat.uv(*a)
        q = [o.vert(a[0], y0, a[1], nx, 0, nn, u, v), o.vert(b[0], y0, b[1], nx, 0, nn, u, v),
             o.vert(b[0], y1, b[1], nx, 0, nn, u, v), o.vert(a[0], y1, a[1], nx, 0, nn, u, v)]
        o.quad(*q)


def solid_prism(o, P, y0, y1, mat, roof=None):
    solid_walls(o, P, y0, y1, mat)
    flat_cap(o, P, y1, roof or mat)


def apex_roof(o, P, y0, y1, mat):
    cx, cn = centroid(P)
    for k in range(len(P)):
        a, b = P[k - 1], P[k]
        e1 = (b[0] - a[0], 0, b[1] - a[1])
        e2 = (cx - a[0], y1 - y0, cn - a[1])
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        nx, ny, nz = nx / L, ny / L, nz / L
        if ny < 0:
            nx, ny, nz = -nx, -ny, -nz
        i0 = o.vert(a[0], y0, a[1], nx, ny, nz, *mat.uv(*a))
        i1 = o.vert(b[0], y0, b[1], nx, ny, nz, *mat.uv(*b))
        i2 = o.vert(cx, y1, cn, nx, ny, nz, *mat.uv(cx, cn))
        o.tri(i0, i1, i2)


def prism(o, P, h, minh, s, roof, roof_shape="flat", roof_h=0.0, taper=None, base=False):
    """P: CCW (x, north) local metres. s: facade strip, roof: Mat."""
    wall_top = h
    if roof_shape in ("cone", "pyramidal", "dome", "onion"):
        roof_h = roof_h or min(h * 0.25, 0.5 * math.sqrt(abs(area2(P))))
        wall_top = max(minh, h - roof_h)
    y0 = minh if minh > 0 else -SINK
    T = None
    if taper:
        cx, cn = centroid(P)
        T = [(cx + (p[0] - cx) * taper, cn + (p[1] - cn) * taper) for p in P]
    if wall_top > y0 + 0.1:
        u = 0.0
        for k in range(len(P)):
            u = wall_band(o, P[k - 1], P[k], y0, wall_top, s, u,
                          T[k - 1] if T else None, T[k] if T else None, base and minh <= 0)
    top = T or P
    if wall_top < h - 0.1:
        apex_roof(o, top, wall_top, h, roof)
    else:
        flat_cap(o, top, wall_top, roof)
    if minh > 0:
        flat_cap(o, P, minh, roof, up=False)
    return top, h


def pip(pt, poly):
    x, y = pt
    c = False
    for i in range(len(poly)):
        x1, y1 = poly[i - 1]
        x2, y2 = poly[i]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            c = not c
    return c


PLANT = (196, 194, 186)


def roof_details(o, P, h, seed):
    """Lift/plant room and AC units on a flat tower roof."""
    A = abs(area2(P))
    if A < 120:
        return
    rnd = random.Random(seed)
    cx, cn = centroid(P)
    wall = Mat(PLANT)
    top = Mat(tuple(c * 0.82 for c in PLANT))
    k = min(0.45, math.sqrt(90.0 / A) + 0.12)
    room = [(cx + (p[0] - cx) * k, cn + (p[1] - cn) * k) for p in P]
    solid_prism(o, room, h, h + 4.0, wall, top)
    unit = Mat((150, 152, 150))
    inner = [(cx + (p[0] - cx) * 0.8, cn + (p[1] - cn) * 0.8) for p in P]
    xs, ns = [p[0] for p in inner], [p[1] for p in inner]
    placed, tries = 0, 0
    while placed < min(6, int(A / 180) + 1) and tries < 60:
        tries += 1
        px, pn = rnd.uniform(min(xs), max(xs)), rnd.uniform(min(ns), max(ns))
        if not pip((px, pn), inner) or pip((px, pn), room):
            continue
        sq = [(px - 1.2, pn - 1.0), (px + 1.2, pn - 1.0), (px + 1.2, pn + 1.0), (px - 1.2, pn + 1.0)]
        solid_prism(o, sq, h, h + 1.6, unit)
        placed += 1


# ---------------------------------------------------------------- OSM helpers
def num(s):
    if not s:
        return None
    m = re.match(r"[-\d.]+", s.replace(",", "."))
    try:
        return float(m.group()) if m else None
    except ValueError:
        return None


def heights(t):
    h = num(t.get("height"))
    lv = num(t.get("building:levels"))
    if h is None and lv is not None:
        h = lv * FLOOR + 1.5
    minh = num(t.get("min_height"))
    ml = num(t.get("building:min_level"))
    if minh is None and ml is not None:
        minh = ml * FLOOR
    return h, (minh or 0.0)


def ring(geom):
    pts = [(p["lon"], p["lat"]) for p in geom]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def to_local(pts, lon0, lat0):
    kx = 111320.0 * math.cos(math.radians(lat0))
    P = [((lo - lon0) * kx, (la - lat0) * M_LAT) for lo, la in pts]
    Q = []
    for p in P:
        if not Q or math.hypot(p[0] - Q[-1][0], p[1] - Q[-1][1]) > 0.2:
            Q.append(p)
    if len(Q) > 2 and math.hypot(Q[0][0] - Q[-1][0], Q[0][1] - Q[-1][1]) < 0.2:
        Q.pop()
    if area2(Q) < 0:
        Q.reverse()
    return Q


# ---------------------------------------------------------------- collect features
raw = json.load(open(f"{OSM}/raw.json"))["elements"]
parts = json.load(open(f"{OSM}/parts.json"))["elements"]

# simHeaven_X-Africa-1-vfr has hand-made models of these (Time_tower, Hilton_Hotel,
# Kenyatta_International_Conference_Centre). If that pack is installed, don't draw ours on
# top of them; if it isn't, model them from OSM like every other tower.
HANDMADE_OSM = {"Times Tower": {1429682196, 1431934015, 1436349700},
                "Hilton": {239055333},
                "KICC": {123414928, 123414930, 1396270912, 1553682723}}   # tower/amphitheatre/saucer/podium
KICC_PODIUM = 1553682723
SIMHEAVEN_VFR = bool(XP_SCENERY) and os.path.exists(
    f"{XP_SCENERY}/simHeaven_X-Africa-1-vfr/Earth nav data/-10+030/-02+036.dsf")
SIMHEAVEN_HAS = set().union(*HANDMADE_OSM.values()) if SIMHEAVEN_VFR else set()
print("simHeaven X-Africa-1-vfr " + ("installed: KICC, Times Tower, Hilton left to it" if SIMHEAVEN_VFR
                                     else "not installed: modelling KICC, Times Tower, Hilton"))
BRITAM_OUTLINE = 1413935255
# 1413935254: a 133 m core part inside the tapered tower, which would poke through its faces
BRITAM_SKIP = {1412128802, 1412128801, 1413935254} | {x["id"] for x in parts if x["tags"].get("roof:shape") == "skillion"
                              and abs(x["geometry"][0]["lat"] + 1.3000) < 0.0005
                              and abs(x["geometry"][0]["lon"] - 36.8132) < 0.0005}

features = []   # dicts: pts(lonlat), t, h, minh, kind, id, name
part_centroids = []
for x in parts:
    t = x["tags"]
    if x["id"] in BRITAM_SKIP or x["id"] in SIMHEAVEN_HAS:
        continue
    pts = ring(x["geometry"])
    if len(pts) < 3:
        continue
    if x["id"] == BRITAM_OUTLINE:
        features.append(dict(id=x["id"], pts=pts, t={}, h=190.0, minh=0.0,
                             kind="britam", name="Britam Tower"))
        part_centroids.append((centroid(pts), 0.0))
        continue
    h, minh = heights(t)
    if h is None:
        continue
    if "building:part" in t:
        part_centroids.append((centroid(pts), minh))
    if h < 18 and x["id"] != KICC_PODIUM:
        continue
    features.append(dict(id=x["id"], pts=pts, t=t, h=h, minh=minh, kind="part", name=t.get("name", "")))

seen = {f["id"] for f in features}
floating_bases = []
for x in raw:
    t = x["tags"]
    if x["type"] != "way" or x["id"] in seen or x["id"] in SIMHEAVEN_HAS:
        continue
    pts = ring(x["geometry"])
    if t.get("leisure") == "stadium":
        if t.get("name") in ("Nyayo National Stadium", "Kasarani Stadium"):
            features.append(dict(id=x["id"], pts=pts, t=t, kind="stadium", name=t["name"],
                                 h=22.0 if "Nyayo" in t["name"] else 30.0, minh=0))
        continue
    if t.get("man_made") == "silo":
        features.append(dict(id=x["id"], pts=pts, t=t, kind="silo", name="NCPB silos", h=42.0, minh=0))
        continue
    if "building" not in t:
        continue
    h, minh = heights(t)
    inner = [m for c, m in part_centroids if pip(c, pts)]
    if inner and min(inner) <= 0.5:
        continue   # modelled by its building:parts
    if inner:      # its parts all start above ground: the outline is the base under them
        h = max(h or 0.0, min(inner))
        floating_bases.append(x["id"])
    elif not h or h < 55:
        continue
    features.append(dict(id=x["id"], pts=pts, t=t, h=h, minh=minh, kind="bldg", name=t.get("name", "")))

if floating_bases:
    print(f"ground sections added under floating building:parts: {floating_bases}")
turbines = [(x["lon"], x["lat"]) for x in raw if x["type"] == "node"]

# core national landmarks: always modelled, whatever their height (osm_fetch "core" query).
# CORE_SPEC (by OSM way id, or relation member id) overrides height/colour from the user's
# street-view screenshots and adds the details built by build_core(); facades are picked by
# the compass bearing they face, checked against the OSM streets.
CORE_SPEC = {
    # Kenya National Archives: cream, 3 storeys, giant red columns with gold capitals on the
    # City Hall Way (S) and Moi Avenue (WSW) fronts, paired at the ends
    78308386: dict(h=14.0, wall=(222, 200, 140),
                   colonnades=[dict(face=179, n=8, paired=True, out=1.1, r=0.55, top=10.0,
                                    col=(150, 48, 42), cap=(205, 165, 70), beam=(226, 208, 156)),
                               dict(face=243, n=8, paired=True, out=1.1, r=0.55, top=10.0,
                                    col=(150, 48, 42), cap=(205, 165, 70), beam=(226, 208, 156))]),
    # McMillan Memorial Library: off-white neoclassical, columned portico + pediment to Banda St
    123357804: dict(h=10.0, wall=(226, 222, 208),
                    colonnades=[dict(face=154, n=6, out=3.5, r=0.45, top=8.0, col=(232, 230, 220),
                                     beam=(226, 222, 208), roof=True, pediment=2.6)]),
    123357801: dict(h=7.0, wall=(226, 222, 208)),
    123357803: dict(h=7.0, wall=(226, 222, 208)),
    # Nairobi City Hall (relation 21299747 members): dressed brown stone; the old City Hall
    # has a tall slim white-pillared portico with a flat canopy facing City Hall Way
    241193505: dict(h=13.0, wall=(146, 124, 98),
                    colonnades=[dict(face=154, n=9, out=5.0, r=0.3, top=9.0, col=(236, 236, 230),
                                     beam=(236, 236, 230), roof=True)]),
    241193506: dict(h=13.0, wall=(146, 124, 98)),
    241193507: dict(h=10.0, wall=(146, 124, 98)),
    241193508: dict(h=15.0, wall=(150, 130, 104)),
    # Parliament (relation 3224460): buff, 4 storeys around a courtyard, colour from OSM
    77397161: dict(h=4 * FLOOR + 1.5),
    # Nairobi Central Railway Station: buff stone, red-brown gabled roof, arcade to Station Rd
    499873616: dict(h=6.0, wall=(214, 196, 160), gable=4.0,
                    colonnades=[dict(face=333, n=30, out=3.0, r=0.35, top=4.2, col=(214, 196, 160),
                                     beam=(214, 196, 160), roof=True)]),
    # Jamia Mosque: grey-stone prayer hall (N) with three silver domes, two tall minarets and
    # two corner turrets, positions from the imagery corrected for its lean; white arcaded
    # courtyard ranges (S) and a green-domed gate pavilion on Banda St
    140320795: dict(jamia=True),
    # Westlands (osm_fetch "westlands"). Safaricom House 2 (W) and House (E), Waiyaki Way:
    # ~9-storey reflective blue-grey glass in light grey frames (street view); House 2 has
    # full-height blue glass drums at both front corners, House a green glass drum at its SE
    # corner
    241570054: dict(h=34.0, wall=(128, 156, 178), glass=True, crown=(2.0, (206, 206, 200)),
                    drums=[dict(face=200, end=0, r=3.2, col=(64, 104, 156)),
                           dict(face=200, end=1, r=3.2, col=(64, 104, 156))]),
    241570055: dict(h=36.0, wall=(120, 150, 170), glass=True, crown=(2.0, (206, 206, 200)),
                    drums=[dict(face=200, end=1, r=4.0, col=(82, 140, 120))]),
    # Nation Centre (1997, Kimathi St; street views from all sides): a ~10-storey white/grey
    # banded block with a concave SW face, the two OSM drums rising from its roof to 83 m
    # (levels 10-17). OSM's outline is only 10 levels and was dropped under the 55 m cut
    1408601890: dict(h=49.0, wall=(222, 222, 218)),
    123355543: dict(minh=49.0, wall=(222, 222, 218)),
    123355544: dict(minh=49.0, wall=(222, 222, 218)),
    # ABC Place mall: pink concrete, 4 storeys at the west end (street view)
    26989734: dict(h=13.0, wall=(196, 140, 122), name="ABC Place"),
    # CBK Pension Towers (2022, 110 m): camera-matched to street views from N and S. A 24 m
    # blue-glass podium over the plot; on it a narrower tower of two slabs, each top sloping up
    # to a peak at the gap between them, gold louvres in the triangular ends; grey cladding with
    # balcony rows, the west slab's north face dark blue triangulated glass
    699395720: dict(wall=(176, 174, 170), cbk=dict(axis=63.0, podium=24.0, tw=24.0, td=30.0, off_a=-7.0, off_b=-8.0,
                                                   gap=4.0, peak=116.0, low=90.0, glass=(44, 62, 96),
                                                   podium_glass=(78, 104, 140), frame=(168, 168, 166),
                                                   gold=(196, 160, 70))),
    # Prism Tower (2018, 133 m; street views from the N, SE and below): pale blue-grey glass folded
    # into triangular facets, the tower zig-zagging up in 8 tiers, an open white frame crown
    1189990505: dict(wall=(178, 200, 212), prism=dict(tiers=8, twist=9.0, crown=7.0,
                                                      glass=(178, 200, 212), frame=(236, 238, 238))),
    # Britam Tower (2017, 190 m + mast; street views from N, W, SE): tapering faces of bronze-grey
    # glass to a flat top ~36% of the base, a blue glass V channel widening up each wide face
    1413935255: dict(wall=(178, 166, 138), tapered=dict(taper=0.36, colour=(178, 166, 138),
                                                        vglass=(72, 122, 152), v_top=0.55, mast=12.0)),
}
# ABC Place: the OSM outline (12 levels) is the whole tower complex; the two towers are cut
# out of it with boxes read off the imagery (lean-corrected), the rest is the dark glass podium.
# Salmon-pink towers with full-height dark blue glass bands; the taller north tower carries
# the "ABC PLACE" crown (street view)
ABC_OUTLINE = 1317733647
ABC_PARTS = [  # id, (S, W, N, E) or None = remainder, spec
    ("abcA", (-1.259610, 36.777783, -1.259389, 36.778085),
     dict(h=46.0, wall=(196, 140, 122), bands=(38, 52, 80), crown=(3.5, (196, 140, 122)))),
    ("abcB", (-1.259912, 36.777797, -1.259650, 36.778085),
     dict(h=40.0, wall=(196, 140, 122), bands=(38, 52, 80))),
    ("abcP", None, dict(h=12.0, wall=(52, 60, 72), glass=True)),
]
JAMIA = dict(hall_south=(-1.283649, 36.820711), axis=333.0,
             domes=[(-1.283202, 36.820528, 5.0, 15.5), (-1.283200, 36.820455, 3.2, 14.5),
                    (-1.283184, 36.820602, 3.2, 14.5)],                       # lat, lon, r, base
             turrets=[(-1.283216, 36.820436), (-1.283200, 36.820677)],
             minarets=[(-1.283507, 36.820432), (-1.283496, 36.820689)])
PARLIAMENT_TOWER = 1560748160      # building:part, 56 m clock tower
WALL_FIX = {PARLIAMENT_TOWER: (204, 160, 96)}   # ochre (photo)


# per-building edits from the viewer (source/overrides.json, in git): merged over CORE_SPEC
OVERRIDES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "overrides.json")
OVERRIDES = json.load(open(OVERRIDES_FILE)) if os.path.exists(OVERRIDES_FILE) else {}


def as_tuples(v):
    """JSON lists of 3 numbers (colours) back to tuples, recursively"""
    if isinstance(v, list) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v):
        return tuple(v)
    if isinstance(v, dict):
        return {k: as_tuples(x) for k, x in v.items()}
    if isinstance(v, list):
        return [as_tuples(x) for x in v]
    return v


def with_override(key, spec):
    """spec with the viewer's edits for this building applied (one level deep for sub-dicts)"""
    ov = OVERRIDES.get(str(key))
    if not ov:
        return spec
    out = dict(spec)
    for k, v in as_tuples(ov).items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


for _k in OVERRIDES:
    _key = int(_k) if _k.isdigit() else _k
    CORE_SPEC[_key] = with_override(_k, CORE_SPEC.get(_key, {}))
if OVERRIDES:
    print(f"viewer edits: {len(OVERRIDES)} buildings from source/overrides.json")


def core_feature(fid, pts, t, holes=()):
    spec = CORE_SPEC.get(fid, {})
    h, minh = heights(t)
    f = dict(id=fid, pts=pts, t=t, h=spec.get("h") or h or 12.0, minh=minh, kind="part",
             name=t.get("name", ""), core=True, spec=spec, holes=list(holes))
    if "wall" in spec:
        WALL_FIX[fid] = spec["wall"]
    return f


if os.path.exists(f"{OSM}/core.json"):
    seen = {f["id"] for f in features}
    for x in json.load(open(f"{OSM}/core.json"))["elements"]:
        t = x["tags"]
        if x["id"] in seen or x["id"] in SIMHEAVEN_HAS:
            continue
        if x["type"] == "relation" and t.get("type") == "multipolygon":
            outers = [(m["ref"], ring(m["geometry"])) for m in x["members"]
                      if m.get("role") == "outer" and m.get("geometry")]
            inners = [ring(m["geometry"]) for m in x["members"] if m.get("role") == "inner" and m.get("geometry")]
            for ref, pts in outers:
                if ref in seen:
                    continue
                f = core_feature(ref, pts, t, [r for r in inners if pip(centroid(r), pts)])
                f["rel_name"] = t.get("name", "")
                f["name"] = ""            # one landmark-table entry per relation
                features.append(f)
            continue
        if x["type"] != "way" or "building" not in t:
            continue
        features.append(core_feature(x["id"], ring(x["geometry"]), t))
# CORE_SPEC also restyles ordinary towers (raw/parts features) by OSM id
for f in features:
    if f["id"] in CORE_SPEC and "spec" not in f:
        f["spec"] = CORE_SPEC[f["id"]]
        if "wall" in f["spec"]:
            WALL_FIX[f["id"]] = f["spec"]["wall"]
        if f["spec"].get("h"):
            f["h"] = f["spec"]["h"]
        if "minh" in f["spec"]:
            f["minh"] = f["spec"]["minh"]

if os.path.exists(f"{OSM}/westlands.json"):
    from shapely.geometry import Polygon, box
    seen = {f["id"] for f in features}
    for x in json.load(open(f"{OSM}/westlands.json"))["elements"]:
        if x["id"] in seen or x["type"] != "way":
            continue
        if x["id"] == ABC_OUTLINE:
            rest = Polygon(ring(x["geometry"])).buffer(0)
            for pid, bb, spec in ABC_PARTS:
                g = rest.intersection(box(bb[1], bb[0], bb[3], bb[2])) if bb else rest
                if bb:
                    rest = rest.difference(g)
                for k, part in enumerate(getattr(g, "geoms", [g])):
                    if part.geom_type != "Polygon" or part.area < 1e-9:
                        continue
                    CORE_SPEC[f"{pid}{k}"] = with_override(f"{pid}{k}", spec)
                    f = core_feature(f"{pid}{k}", list(part.exterior.coords)[:-1], {})
                    f["rel_name"] = "ABC Place"
                    features.append(f)
        elif x["id"] in CORE_SPEC:
            f = core_feature(x["id"], ring(x["geometry"]), x["tags"])
            if CORE_SPEC[x["id"]].get("name"):
                f["name"], f["rel_name"] = "", CORE_SPEC[x["id"]]["name"]
            features.append(f)


# ---------------------------------------------------------------- core landmark details
GOLD, SILVER, MOSQUE_GREEN = (205, 165, 70), (196, 198, 200), (38, 120, 72)
JAMIA_STONE, JAMIA_WHITE, JAMIA_OCHRE = (128, 126, 120), (236, 234, 226), (214, 170, 90)
CLOCK_FACE, CLOCK_HAND = (240, 238, 228), (30, 30, 30)
TALANTA_SKIN, TALANTA_SEATS, TALANTA_FASCIA = (178, 182, 188), (176, 44, 44), (70, 72, 76)


def DETAIL_COLOURS():
    cs = {GOLD, SILVER, MOSQUE_GREEN, JAMIA_STONE, JAMIA_WHITE, JAMIA_OCHRE, CLOCK_FACE, CLOCK_HAND}
    for sp in list(CORE_SPEC.values()) + [p[2] for p in ABC_PARTS]:
        for c in sp.get("colonnades", []):
            cs |= {tuple(c[k]) for k in ("col", "cap", "beam") if k in c}
        cs |= {tuple(d["col"]) for d in sp.get("drums", [])}
        if "bands" in sp:
            cs.add(tuple(sp["bands"]))
        if "crown" in sp:
            cs.add(tuple(sp["crown"][1]))
        if "tapered" in sp:
            cs |= {tuple(sp["tapered"]["colour"]), tuple(sp["tapered"]["vglass"])}
        if "prism" in sp:
            cs |= {tuple(sp["prism"]["glass"]), tuple(sp["prism"]["frame"])}
        if "cbk" in sp:
            cs |= {tuple(sp["cbk"][k]) for k in ("glass", "gold", "podium_glass", "frame") if k in sp["cbk"]}
    cs |= {tuple(c) for c in WALL_FIX.values()}
    cs |= {TALANTA_SKIN, TALANTA_SEATS, TALANTA_FASCIA}
    return sorted(cs)


def loc(lat, lon):
    return (lon - Ctx.lon0) * Ctx.kx, (lat - Ctx.lat0) * M_LAT


def circle(cx, cn, r, seg=8):
    return [(cx + r * math.cos(2 * math.pi * k / seg), cn + r * math.sin(2 * math.pi * k / seg)) for k in range(seg)]


def rect(c, u, nrm, u0, u1, d0, d1):
    """CCW rectangle spanning u0..u1 along unit u and d0..d1 along unit nrm from point c."""
    pt = lambda s, d: (c[0] + u[0] * s + nrm[0] * d, c[1] + u[1] * s + nrm[1] * d)
    R = [pt(u0, d0), pt(u1, d0), pt(u1, d1), pt(u0, d1)]
    return R if area2(R) > 0 else R[::-1]


def facing_edge(P, bearing, tol=35.0, outermost=False):
    """Longest edge of CCW P whose outward normal faces the compass bearing (or, with
    outermost, the one furthest out that way, e.g. the street front of a stepped outline)."""
    best = None
    for k in range(len(P)):
        a, b = P[k - 1], P[k]
        dx, dn = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dn)
        if L < 1:
            continue
        nx, nn = dn / L, -dx / L
        diff = abs((math.degrees(math.atan2(nx, nn)) - bearing + 180) % 360 - 180)
        key = (a[0] + b[0]) / 2 * nx + (a[1] + b[1]) / 2 * nn if outermost else L
        if diff <= tol and (best is None or key > best[0]):
            best = (key, L, a, (dx / L, dn / L), (nx, nn))
    return best[1:] if best else None


def dome(o, cx, cn, r, y0, mat, rise=1.15, seg=16, rings=6):
    """Hemisphere (rise > 1 = slightly pointed) on a circle of radius r at height y0."""
    us, vs = mat.uv(cx, cn)
    rows = []
    for i in range(rings + 1):
        phi = math.pi / 2 * i / rings
        rr, yy = r * math.cos(phi), y0 + r * rise * math.sin(phi)
        row = []
        for k in range(seg):
            t = 2 * math.pi * k / seg
            nx, ny, nz = math.cos(phi) * math.cos(t), math.sin(phi), math.cos(phi) * math.sin(t)
            row.append(o.vert(cx + rr * math.cos(t), yy, cn + rr * math.sin(t), nx, ny, nz, us, vs))
        rows.append(row)
    for i in range(rings):
        for k in range(seg):
            a, b = rows[i][k], rows[i][(k + 1) % seg]
            c, d = rows[i + 1][(k + 1) % seg], rows[i + 1][k]
            o.quad(a, b, c, d)
    return y0 + r * rise


def column(o, cx, cn, r, y0, y1, mat, seg=8):
    C = circle(cx, cn, r, seg)
    solid_walls(o, C, y0, y1, mat)
    flat_cap(o, C, y1, mat)


def colonnade(o, P, c):
    e = facing_edge(P, c["face"])
    if not e:
        return
    L, a, u, nrm = e
    n, r, out, top = c["n"], c["r"], c["out"], c["top"]
    n = max(2, min(n, int(L / (r * 6)) + 1))
    inset = r * 2.5
    if c.get("paired") and n >= 6:
        mid = [inset + r * 3.2 + (L - 2 * inset - r * 6.4) * k / (n - 3) for k in range(1, n - 3)]
        ss = [inset, inset + r * 3.2] + mid + [L - inset - r * 3.2, L - inset]
    else:
        ss = [inset + (L - 2 * inset) * k / (n - 1) for k in range(n)]
    col, beam = Mat(c["col"]), Mat(c["beam"])
    cap = Mat(c["cap"]) if "cap" in c else col
    shaft0 = 1.2 if "cap" in c else 0.0
    for s_ in ss:
        cx, cn = a[0] + u[0] * s_ + nrm[0] * out, a[1] + u[1] * s_ + nrm[1] * out
        if shaft0:   # plinth
            solid_prism(o, rect((cx, cn), u, nrm, -r * 1.3, r * 1.3, -r * 1.3, r * 1.3), -1.0, shaft0, beam)
        column(o, cx, cn, r, shaft0 or -1.0, top - 0.5, col)
        solid_prism(o, rect((cx, cn), u, nrm, -r * 1.25, r * 1.25, -r * 1.25, r * 1.25), top - 0.5, top, cap)
    u0, u1 = ss[0] - r * 1.6, ss[-1] + r * 1.6
    depth = out + r * 1.6
    bh = 0.6 if c.get("roof") and not c.get("pediment") else 1.0
    solid_prism(o, rect(a, u, nrm, u0, u1, -0.2, depth), top, top + bh, beam)
    if c.get("pediment"):
        y0, y1, um = top + bh, top + bh + c["pediment"], (u0 + u1) / 2
        us, vs = beam.uv(*a)
        pt = lambda s_, d: (a[0] + u[0] * s_ + nrm[0] * d, a[1] + u[1] * s_ + nrm[1] * d)
        for d, sign in ((depth, 1), (-0.2, -1)):   # front and back triangles
            A, B, C = pt(u0, d), pt(u1, d), pt(um, d)
            nx, nn = nrm[0] * sign, nrm[1] * sign
            i0, i1, i2 = (o.vert(A[0], y0, A[1], nx, 0, nn, us, vs), o.vert(B[0], y0, B[1], nx, 0, nn, us, vs),
                          o.vert(C[0], y1, C[1], nx, 0, nn, us, vs))
            o.tri(i0, i1, i2) if sign > 0 else o.tri(i0, i2, i1)
        for s0, s1 in ((u0, um), (um, u1)):        # sloping tops
            ys0, ys1 = (y0, y1) if s0 == u0 else (y1, y0)
            q = [o.vert(*(lambda p: (p[0], yy, p[1]))(pt(ss_, d)), 0, 1, 0, us, vs)
                 for ss_, yy, d in ((s0, ys0, -0.2), (s1, ys1, -0.2), (s1, ys1, depth), (s0, ys0, depth))]
            o.quad(q[0], q[3], q[2], q[1])
        return y1
    return top + bh


def holes_local(f):
    H = []
    for r in f.get("holes", []):
        Q = to_local(r, Ctx.lon0, Ctx.lat0)
        if len(Q) >= 3:
            H.append(Q[::-1])     # clockwise: walls face into the courtyard
    return H


def cap_with_holes(o, P, H, y, mat):
    import shapely
    from shapely.geometry import Polygon
    poly = Polygon(P, [h[::-1] for h in H])
    for tri in shapely.constrained_delaunay_triangles(poly).geoms:
        T = list(tri.exterior.coords)[:3]
        if area2(T) < 0:
            T = T[::-1]
        ids = [o.vert(p[0], y, p[1], 0, 1, 0, *mat.uv(p[0], p[1])) for p in T]
        o.tri(*ids)


def walls_and_cap(o, P, H, y0, y1, strip, roof):
    for R in [P] + H:
        u = 0.0
        for k in range(len(R)):
            u = wall_band(o, R[k - 1], R[k], y0, y1, strip, u)
    if H:
        cap_with_holes(o, P, H, y1, roof)
    else:
        flat_cap(o, P, y1, roof)


def gable_roof(o, P, h, rise, wall, roof):
    """Ridge along the long axis of P's minimum rectangle, gable ends in the wall colour."""
    ang, x0, x1, y0, y1 = min_rect(P)
    if x1 - x0 < y1 - y0:
        ang += math.pi / 2
        x0, x1, y0, y1 = y0, y1, -x1, -x0
    c, s_ = math.cos(ang), math.sin(ang)
    W = lambda uu, vv: (uu * c - vv * s_, uu * s_ + vv * c)
    ov = 0.5
    x0, x1, y0, y1 = x0 - ov, x1 + ov, y0 - ov, y1 + ov
    ym = (y0 + y1) / 2
    for (va, vb) in ((y0, ym), (y1, ym)):
        A, B, C, D = W(x0, va), W(x1, va), W(x1, vb), W(x0, vb)
        ids = [o.vert(A[0], h, A[1], 0, 1, 0, *roof.uv(*A)), o.vert(B[0], h, B[1], 0, 1, 0, *roof.uv(*B)),
               o.vert(C[0], h + rise, C[1], 0, 1, 0, *roof.uv(*C)), o.vert(D[0], h + rise, D[1], 0, 1, 0, *roof.uv(*D))]
        o.quad(*ids)          # objects are ATTR_no_cull, so winding doesn't matter
    us, vs = wall.uv(0, 0)
    for xx in (x0 + ov, x1 - ov):
        A, B, C = W(xx, y0 + ov), W(xx, y1 - ov), W(xx, ym)
        i = [o.vert(A[0], h, A[1], 0, 0, 1, us, vs), o.vert(B[0], h, B[1], 0, 0, 1, us, vs),
             o.vert(C[0], h + rise, C[1], 0, 0, 1, us, vs)]
        o.tri(*i)
    return h + rise


def build_clock_tower(o, P, f):
    """Parliament clock tower (photo): ochre shaft with small windows, a clock face on each
    side, open lantern on corner piers, a lattice-like crown and a flagpole."""
    h, wall = f["h"], Mat(f["wall"])
    shaft = h - 9.0
    u = 0.0
    for k in range(len(P)):
        u = wall_band(o, P[k - 1], P[k], -SINK, shaft, f["strip"], u)
    flat_cap(o, P, shaft, wall)
    cx, cn = centroid(P)
    face, hand = Mat(CLOCK_FACE), Mat(CLOCK_HAND)
    for k in range(len(P)):
        a, b = P[k - 1], P[k]
        dx, dn = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dn)
        if L < 3:
            continue
        ux, un, nx, nn = dx / L, dn / L, dn / L, -dx / L
        mx, mn = (a[0] + b[0]) / 2 + nx * 0.2, (a[1] + b[1]) / 2 + nn * 0.2
        r, yc = min(2.6, L * 0.33), shaft - 4.0
        for mat, rr, off in ((face, r, 0.0), (hand, r * 0.1, 0.1)):
            us, vs = mat.uv(0, 0)
            ctr = o.vert(mx + nx * off, yc, mn + nn * off, nx, 0, nn, us, vs)
            rim = [o.vert(mx + nx * off + ux * rr * math.cos(t), yc + rr * math.sin(t), mn + nn * off + un * rr * math.cos(t),
                          nx, 0, nn, us, vs) for t in [2 * math.pi * j / 16 for j in range(16)]]
            for j in range(16):
                o.tri(ctr, rim[(j + 1) % 16], rim[j])
        us, vs = hand.uv(0, 0)
        for ang, ln in ((math.radians(60), r * 0.55), (math.radians(-80), r * 0.8)):   # hour, minute
            ex, ey = math.cos(ang), math.sin(ang)
            px, py = -ey * 0.09, ex * 0.09
            q = [(0 + px, 0 + py), (ex * ln + px, ey * ln + py), (ex * ln - px, ey * ln - py), (0 - px, 0 - py)]
            ids = [o.vert(mx + nx * 0.1 + ux * qx, yc + qy, mn + nn * 0.06 + un * qx, nx, 0, nn, us, vs) for qx, qy in q]
            o.quad(*ids)
    # open lantern: corner piers, then cornice slab, then the crown
    for p in P:
        k = 0.85
        qx, qn = cx + (p[0] - cx) * k, cn + (p[1] - cn) * k
        solid_prism(o, circle(qx, qn, 0.7, 4), shaft, shaft + 4.5, wall)
    big = [(cx + (p[0] - cx) * 1.06, cn + (p[1] - cn) * 1.06) for p in P]
    solid_prism(o, big, shaft + 4.5, shaft + 5.3, wall)
    crown = [(cx + (p[0] - cx) * 0.7, cn + (p[1] - cn) * 0.7) for p in P]
    solid_walls(o, crown, shaft + 5.3, h - 1.2, wall)
    apex_roof(o, crown, h - 1.2, h, wall)
    column(o, cx, cn, 0.1, h, h + 5.0, Mat(STEEL), 4)
    return h


def minaret(o, cx, cn, h, r, wall, cap):
    column(o, cx, cn, r, -SINK, h * 0.72, wall)
    solid_prism(o, circle(cx, cn, r * 1.55), h * 0.72, h * 0.72 + 0.6, wall)
    column(o, cx, cn, r * 0.8, h * 0.72 + 0.6, h * 0.9, wall)
    solid_prism(o, circle(cx, cn, r * 1.25), h * 0.9, h * 0.9 + 0.4, wall)
    top = dome(o, cx, cn, r * 0.85, h * 0.9 + 0.4, cap, rise=1.5, seg=8, rings=4)
    column(o, cx, cn, 0.07, top, top + 1.2, Mat(GOLD), 4)


def build_jamia(o, P, f):
    from shapely.geometry import Polygon
    roof, stone, white, ochre = roof_mat(f), Mat(JAMIA_STONE), Mat(JAMIA_WHITE), Mat(JAMIA_OCHRE)
    green, silver = Mat(MOSQUE_GREEN), Mat(SILVER)
    sx, sn = loc(*JAMIA["hall_south"])
    b = math.radians(JAMIA["axis"])
    d = (math.sin(b), math.cos(b))                    # towards the qibla (prayer hall)
    e = (d[1], -d[0])
    big = 400.0
    half = lambda sgn: Polygon([(sx + e[0] * s1 + d[0] * s2, sn + e[1] * s1 + d[1] * s2)
                                for s1, s2 in ((-big, 0), (big, 0), (big, sgn * big), (-big, sgn * big))])
    poly = Polygon(P).buffer(0)

    def parts(g):
        return [g] if g.geom_type == "Polygon" else [x for x in getattr(g, "geoms", []) if x.geom_type == "Polygon"]

    def ccw(coords):
        Q = list(coords)[:-1]
        return Q if area2(Q) > 0 else Q[::-1]

    HALL_H, RANGE_H = 12.0, 8.0
    for g in parts(poly.intersection(half(1))):       # prayer hall: grey stone, white parapet
        Q = ccw(g.exterior.coords)
        solid_walls(o, Q, -SINK, HALL_H, stone)
        flat_cap(o, Q, HALL_H, roof)
        solid_walls(o, Q, HALL_H, HALL_H + 1.0, white)
    for g in parts(poly.intersection(half(-1))):      # courtyard ranges: white arcades, ochre bays
        Q = ccw(g.exterior.coords)
        court = g.buffer(-7.0, join_style=2)
        H = [ccw(c.exterior.coords)[::-1] for c in parts(court) if c.area > 50]
        for R in [Q] + H:
            solid_walls(o, R, -SINK, 3.6, ochre)
            solid_walls(o, R, 3.6, RANGE_H, white)
        cap_with_holes(o, Q, H, RANGE_H, roof)
    for lat, lon, r, base in JAMIA["domes"]:
        cx, cn = loc(lat, lon)
        column(o, cx, cn, r * 0.92, HALL_H, base, white, 16)
        dome(o, cx, cn, r, base, silver)
        column(o, cx, cn, 0.09, base + r * 1.15, base + r * 1.15 + 1.6, Mat(GOLD), 4)
    for lat, lon in JAMIA["turrets"]:
        minaret(o, *loc(lat, lon), 19.0, 0.9, white, green)
    for lat, lon in JAMIA["minarets"]:
        minaret(o, *loc(lat, lon), 30.0, 1.4, white, green)
    gate = facing_edge(P, 153.0, outermost=True)      # Banda St gate pavilion
    if gate:
        L, a, u, nrm = gate
        mid = (a[0] + u[0] * L / 2, a[1] + u[1] * L / 2)
        G = rect(mid, u, nrm, -4.5, 4.5, -7.0, 1.5)
        solid_walls(o, G, -SINK, 3.6, ochre)
        solid_walls(o, G, 3.6, 10.0, white)
        flat_cap(o, G, 10.0, white)
        gc = centroid(G)
        column(o, gc[0], gc[1], 3.0, 10.0, 11.0, white, 16)
        dome(o, gc[0], gc[1], 3.2, 11.0, green, rise=1.25)
        for p in G:
            column(o, gc[0] + (p[0] - gc[0]) * 0.9, gc[1] + (p[1] - gc[1]) * 0.9, 0.45, 10.0, 12.5, white)
    return 30.0


def build_twin_slabs(o, P, f):
    """CBK Pension Towers, low-poly: a full-footprint podium (blue glass between grey bands), and on
    it a narrower tower (`tw` x `td` m, shifted `off_a`/`off_b`) split across `axis` into two slabs
    by a `gap`; each slab's top slopes from `low` at its outer side to `peak` at the gap, with
    gold louvres filling the triangular ends. The west slab's north face is blue glass."""
    c = f["spec"]["cbk"]
    b = math.radians(c["axis"])
    ua = (math.sin(b), math.cos(b))                   # across the slabs (compass bearing axis)
    ub = (ua[1], -ua[0])                              # along them
    cx, cn = centroid(P)
    A = [(p[0] - cx) * ua[0] + (p[1] - cn) * ua[1] for p in P]
    B = [(p[0] - cx) * ub[0] + (p[1] - cn) * ub[1] for p in P]
    W = lambda a, bb: (cx + ua[0] * a + ub[0] * bb, cn + ua[1] * a + ub[1] * bb)
    wall, glass, gold = Mat(f["wall"]), Mat(c["glass"]), Mat(c["gold"])
    grey = Mat(c.get("frame", f["wall"]))
    s = f["strip"]

    def quad(p, q, y0, y1, mat):
        """vertical quad p->q (outward normal on the right, as for a CCW outline)"""
        if mat is None:
            wall_band(o, p, q, y0, y1, s, 0.0)
            return
        dx, dn = q[0] - p[0], q[1] - p[1]
        L = math.hypot(dx, dn) or 1
        nx, nn = dn / L, -dx / L
        us, vs = mat.uv(*p)
        o.quad(o.vert(p[0], y0, p[1], nx, 0, nn, us, vs), o.vert(q[0], y0, q[1], nx, 0, nn, us, vs),
               o.vert(q[0], y1, q[1], nx, 0, nn, us, vs), o.vert(p[0], y1, p[1], nx, 0, nn, us, vs))

    def ring(a0, a1, b0, b1):
        R = [W(a0, b0), W(a1, b0), W(a1, b1), W(a0, b1)]
        return R if area2(R) > 0 else R[::-1]

    # podium: grey base band, blue glass, grey parapet (3 x 4 quads + a roof)
    ph = c.get("podium", 24.0)
    R = ring(min(A), max(A), min(B), max(B))
    for k in range(4):
        quad(R[k - 1], R[k], -SINK, 4.0, grey)
        quad(R[k - 1], R[k], 4.0, ph - 1.5, Mat(c.get("podium_glass", c["glass"])))
        quad(R[k - 1], R[k], ph - 1.5, ph, grey)
    flat_cap(o, R, ph, grey)
    # tower: two slabs
    am, bm = c.get("off_a", 0.0), c.get("off_b", 0.0)
    a0, a1 = am - c["tw"] / 2, am + c["tw"] / 2
    b0, b1 = bm - c["td"] / 2, bm + c["td"] / 2
    g = c["gap"] / 2
    lo_, pk = c["low"], c["peak"]
    for side in (-1, 1):                              # -1: low-a slab (west for axis 63), +1: other
        ao, ai = (a0, am - g) if side < 0 else (a1, am + g)   # outer and inner (gap) edges
        po, qo = (W(ao, b0), W(ao, b1)) if side < 0 else (W(ao, b1), W(ao, b0))
        pi, qi = (W(ai, b1), W(ai, b0)) if side < 0 else (W(ai, b0), W(ai, b1))
        quad(po, qo, ph, lo_, None)                   # outer face: cladding with balcony rows
        quad(pi, qi, ph, pk, grey)                    # gap face: plain
        for bb, sgn in ((b0, -1), (b1, 1)):           # end faces (normals along -ub / +ub)
            p, q = (W(ao, bb), W(ai, bb)) if (sgn > 0) == (side < 0) else (W(ai, bb), W(ao, bb))
            brg = math.degrees(math.atan2(ub[0] * sgn, ub[1] * sgn)) % 360
            north = brg > 270 or brg < 45
            quad(p, q, ph, lo_, glass if (side < 0 and north) else None)
            tri = [W(ao, bb), W(ai, bb)]
            us, vs = gold.uv(*tri[0])
            nx, nn = ub[0] * sgn, ub[1] * sgn
            o.tri(o.vert(tri[0][0], lo_, tri[0][1], nx, 0, nn, us, vs), o.vert(tri[1][0], lo_, tri[1][1], nx, 0, nn, us, vs),
                  o.vert(tri[1][0], pk, tri[1][1], nx, 0, nn, us, vs))
        Q = [W(ao, b0), W(ai, b0), W(ai, b1), W(ao, b1)]   # sloping top
        us, vs = grey.uv(*Q[0])
        o.quad(*[o.vert(q[0], y, q[1], 0, 1, 0, us, vs) for q, y in zip(Q, (lo_, pk, pk, lo_))])
    return pk


def build_prism(o, P, f):
    """Prism Tower, low-poly: the footprint's rectangle in `tiers` stacked tiers whose corners turn
    alternately -twist/+twist degrees, each face two flat triangles (diagonals alternating), so the
    glass folds into the zig-zag facets of the real tower; a white open crown on top."""
    c = f["spec"]["prism"]
    h = f["h"]
    ang, x0, x1, y0, y1 = min_rect(P)
    ca, sa = math.cos(ang), math.sin(ang)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 * c.get("shrink", 0.94), (y1 - y0) / 2 * c.get("shrink", 0.94)
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]

    def ring(rot):
        cr, sr = math.cos(rot), math.sin(rot)
        out = []
        for u, v in corners:
            u, v = u * cr - v * sr + mx, u * sr + v * cr + my
            out.append((u * ca - v * sa, u * sa + v * ca))
        return out if area2(out) > 0 else out[::-1]

    glass, frame = Mat(c["glass"]), Mat(c["frame"])
    top = h - c["crown"]
    n = c["tiers"]
    tw = math.radians(c["twist"])
    rings = [ring(tw if k % 2 else -tw) for k in range(n + 1)]
    ys = [top * k / n for k in range(n + 1)]

    def tri(a, b, cc, mat):
        e1 = [b[i] - a[i] for i in range(3)]
        e2 = [cc[i] - a[i] for i in range(3)]
        nx, ny, nz = e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]
        L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        us, vs = mat.uv(a[0], a[2])
        o.tri(*[o.vert(p[0], p[1], p[2], nx / L, ny / L, -nz / L, us, vs) for p in (a, b, cc)])

    solid_walls(o, rings[0], -SINK, 0.0, glass)          # below ground, for sloping terrain
    for k in range(n):
        A, B = rings[k], rings[k + 1]
        for j in range(4):
            a0 = (A[j - 1][0], ys[k], A[j - 1][1]); a1 = (A[j][0], ys[k], A[j][1])
            b0 = (B[j - 1][0], ys[k + 1], B[j - 1][1]); b1 = (B[j][0], ys[k + 1], B[j][1])
            if k % 2:                                      # alternate the fold diagonal
                tri(a0, a1, b1, glass); tri(a0, b1, b0, glass)
            else:
                tri(a0, a1, b0, glass); tri(a1, b1, b0, glass)
    solid_walls(o, rings[-1], top, h, frame)              # open white crown above the roof
    flat_cap(o, rings[-1], top - 1.0, frame)
    return h


def build_tapered_glass(o, P, f):
    """Low-poly tapered glass tower (Britam): each face one sloped panel of solid glass colour from
    the footprint up to the outline scaled by `taper` at `h`; a blue glass V channel widening up
    the middle of each wide face; a mast with an obstruction light."""
    c = f["spec"]["tapered"]
    h = f["h"]
    cx, cn = centroid(P)
    T = [(cx + (p[0] - cx) * c["taper"], cn + (p[1] - cn) * c["taper"]) for p in P]
    glass, vg, steel = Mat(c["colour"]), Mat(c["vglass"]), Mat(STEEL)

    def put(p, nrm, mat):
        us, vs = mat.uv(0, 0)
        return o.vert(p[0], p[1], p[2], nrm[0], nrm[1], nrm[2], us, vs)

    for k in range(len(P)):
        a, b, ta, tb = P[k - 1], P[k], T[k - 1], T[k]
        A, B = (a[0], -SINK, a[1]), (b[0], -SINK, b[1])
        TA, TB = (ta[0], h, ta[1]), (tb[0], h, tb[1])
        e1 = [B[i] - A[i] for i in range(3)]
        e2 = [TA[i] - A[i] for i in range(3)]
        nx, ny, nz = e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]
        L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        nrm = (-nx / L, -ny / L, -nz / L)        # outward (CCW outline, OBJ z = -north)
        if nrm[1] < -0.5:
            nrm = tuple(-v for v in nrm)
        o.quad(put(A, nrm, glass), put(B, nrm, glass), put(TB, nrm, glass), put(TA, nrm, glass))
        if math.hypot(b[0] - a[0], b[1] - a[1]) < c.get("v_min", 15.0):
            continue
        # V channel: narrow at the foot, wide at the top, slightly proud of the face
        off = 0.25

        def on_face(s_, t_):
            lo = (a[0] + (b[0] - a[0]) * s_, a[1] + (b[1] - a[1]) * s_)
            hi = (ta[0] + (tb[0] - ta[0]) * s_, ta[1] + (tb[1] - ta[1]) * s_)
            x, n_ = lo[0] + (hi[0] - lo[0]) * t_, lo[1] + (hi[1] - lo[1]) * t_
            return (x + nrm[0] * off, -SINK + (h + SINK) * t_ + nrm[1] * off, n_ - nrm[2] * off)
        v = [on_face(0.47, 0.06), on_face(0.53, 0.06), on_face(0.5 + c["v_top"] / 2, 0.985), on_face(0.5 - c["v_top"] / 2, 0.985)]
        o.quad(*[put(p, nrm, vg) for p in v])
    flat_cap(o, T, h, glass)
    mast = [(cx + 0.8 * math.cos(t), cn + 0.8 * math.sin(t)) for t in [2 * math.pi * k / 4 for k in range(4)]]
    solid_walls(o, mast, h, h + c["mast"] - 2, steel)
    apex_roof(o, mast, h + c["mast"] - 2, h + c["mast"], steel)
    o.obs(cx, h + c["mast"] + 0.5, cn)
    return h + c["mast"]


def build_core(o, P, f):
    """Core landmark from CORE_SPEC: walls (with courtyards), roof, and its details."""
    sp = f.get("spec", {})
    if f["id"] == PARLIAMENT_TOWER:
        return build_clock_tower(o, P, f)
    if sp.get("jamia"):
        return build_jamia(o, P, f)
    if sp.get("cbk"):
        return build_twin_slabs(o, P, f)
    if sp.get("prism"):
        return build_prism(o, P, f)
    if sp.get("tapered"):
        return build_tapered_glass(o, P, f)
    h, roof = f["h"], roof_mat(f)
    H = holes_local(f)
    if sp.get("gable"):
        u = 0.0
        for k in range(len(P)):
            u = wall_band(o, P[k - 1], P[k], -SINK, h, f["strip"], u)
        top = gable_roof(o, P, h, sp["gable"], Mat(f["wall"]), roof)
    else:
        walls_and_cap(o, P, H, -SINK, h, f["strip"], roof)
        top = h
    for c in sp.get("colonnades", []):
        colonnade(o, P, c)
    if "bands" in sp:               # full-height glass band down the middle of each face
        band = Mat(sp["bands"])
        us, vs = band.uv(0, 0)
        for k in range(len(P)):
            a, b = P[k - 1], P[k]
            dx, dn = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dn)
            if L < 8:
                continue
            nx, nn = dn / L, -dx / L
            w = min(L * 0.36, 9.0)
            pt = lambda s_: (a[0] + dx / L * s_ + nx * 0.12, a[1] + dn / L * s_ + nn * 0.12)
            A, B = pt((L - w) / 2), pt((L + w) / 2)
            o.quad(o.vert(A[0], 4.0, A[1], nx, 0, nn, us, vs), o.vert(B[0], 4.0, B[1], nx, 0, nn, us, vs),
                   o.vert(B[0], h - 0.8, B[1], nx, 0, nn, us, vs), o.vert(A[0], h - 0.8, A[1], nx, 0, nn, us, vs))
    for d in sp.get("drums", []):   # full-height glass corner drums
        e = facing_edge(P, d["face"], 50.0)
        if e:
            L, a, u, _ = e
            cx, cn = (a[0] + u[0] * L * d["end"], a[1] + u[1] * L * d["end"])
            column(o, cx, cn, d["r"], -SINK, h + 1.0, Mat(d["col"]), 16)
    if "crown" in sp:               # parapet / sign crown in the given colour
        ch, col = sp["crown"]
        solid_walls(o, P, h, h + ch, Mat(col))
        top = max(top, h + ch)
    return top

# Talanta Stadium (Jamhuri / Ngong Rd), under construction: OSM only has its pitch, so the
# bowl is a rounded rectangle around that pitch, sized from the Ortho4XP imagery (~210 x 260 m)
if os.path.exists(f"{OSM}/kasongo.json"):
    pitch = next((e for e in json.load(open(f"{OSM}/kasongo.json"))["elements"] if e["id"] == 1466403080), None)
    if pitch:
        g = ring(pitch["geometry"])
        lo0, la0 = centroid(g)
        kx = 111320.0 * math.cos(math.radians(la0))
        # bowl base measured on the imagery: centre 3 m W / 13 m N of the pitch's vertex mean
        lo0, la0 = lo0 - 3.0 / kx, la0 + 13.0 / M_LAT
        A, B, N = 105.0, 130.0, 3.0          # semi-axes E-W / N-S (m), superellipse exponent
        rot = math.radians(1.0)              # pitch long axis is ~1 deg off north
        pts = []
        for k in range(48):
            t = 2 * math.pi * k / 48
            c, s_ = math.cos(t), math.sin(t)
            x = A * math.copysign(abs(c) ** (2 / N), c)
            n = B * math.copysign(abs(s_) ** (2 / N), s_)
            x, n = x * math.cos(rot) - n * math.sin(rot), x * math.sin(rot) + n * math.cos(rot)
            pts.append((lo0 + x / kx, la0 + n / M_LAT))
        features.append(dict(id="talanta", pts=pts, t={}, kind="stadium", name="Talanta Stadium",
                             h=38.0, minh=0, inner=0.50, talanta=True))


def point_feature(fid, lon, lat, kind, name, h, r=2.0):
    kx = 111320.0 * math.cos(math.radians(lat))
    dl, dn = r / kx, r / M_LAT
    pts = [(lon - dl, lat - dn), (lon + dl, lat - dn), (lon + dl, lat + dn), (lon - dl, lat + dn)]
    return dict(id=fid, pts=pts, t={}, kind=kind, name=name, h=h, minh=0)


report = []
# AIP HKNW AD 2.22 visual reference sites: real OSM buildings replace simHeaven's generic
# houses inside each site boundary. Default storeys per site.
SITES = [  # name, default levels, OSM boundary id
    ("Langata Barracks", 2, 26744501),
    ("Kenya Defence Forces Memorial Hospital", 3, 1544752632),
    ("KBC Ngong transmitter (WHISKY 1)", 1, 21187670),
]
site_bounds = {}   # name -> list of rings (lon, lat)
if os.path.exists(f"{OSM}/sites.json"):
    site_els = json.load(open(f"{OSM}/sites.json"))["elements"]
    ids = {sid: name for name, _, sid in SITES}
    for x in site_els:
        if x["id"] in ids and x["type"] == "way":
            site_bounds[ids[x["id"]]] = [ring(x["geometry"])]
        elif x["id"] in ids and x["type"] == "relation":
            site_bounds[ids[x["id"]]] = [ring(m["geometry"]) for m in x["members"]
                                         if m.get("role") == "outer" and m.get("geometry")]
    seen = {f["id"] for f in features}
    for x in site_els:
        t = x["tags"]
        if x["type"] == "relation" and x["id"] == 13995868:   # MONASTERY FINAL, 3 NM RWY 07
            for k, m in enumerate(m for m in x["members"] if m.get("role") == "outer" and m.get("geometry")):
                features.append(dict(id=f"sem{k}", pts=ring(m["geometry"]), t={},
                                     h=2 * FLOOR + 3, minh=0, kind="part", name="", site="seminary"))
            report.append("St. Thomas Aquinas Seminary (MONASTERY FINAL)")
            continue
        if x["type"] == "node" and t.get("man_made") == "mast":
            features.append(point_feature(x["id"], x["lon"], x["lat"], "mast", "KBC mast",
                                          num(t.get("height")) or 60.0, 1.2))
            continue
        if x["type"] != "way" or "building" not in t or x["id"] in seen or x["id"] in SIMHEAVEN_HAS:
            continue
        if t.get("building") in ("bridge", "roof"):
            continue
        pts = ring(x["geometry"])
        if len(pts) < 3:
            continue
        c = centroid(pts)
        site = next((sd for sd in SITES if any(pip(c, r) for r in site_bounds.get(sd[0], []))), None)
        if not site:
            continue
        h, minh = heights(t)
        if h is None:
            h = site[1] * FLOOR + 1.5
        features.append(dict(id=x["id"], pts=pts, t=t, h=h, minh=minh, kind="part", name="", site=site[0]))
        seen.add(x["id"])
    for name, _, _ in SITES:
        n = sum(1 for f in features if f.get("site") == name)
        if n:
            report.append(f"{name}: {n} buildings")

# AIP: "Visual Marker" beacon, black/white horizontal stripes, 100 ft white circle on ground
# position S01 24 00.01 E036 47 21.41 (Africa Nazarene University)
features.append(point_feature(-1, 36.0 + 47 / 60 + 21.41 / 3600, -(1 + 24 / 60 + 0.01 / 3600),
                              "beacon", "Visual Marker (Nazarene Univ.)", 12.0, 16.0))
# buildings removed by hand (viewer "Remove", or edit the file): one feature id per line
REMOVED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "removed.txt")
REMOVED = set()
if os.path.exists(REMOVED_FILE):
    for line in open(REMOVED_FILE):
        line = line.split("#")[0].strip()
        if line:
            REMOVED.add(line)
n0 = len(features)
features = [f for f in features if str(f["id"]) not in REMOVED]
if REMOVED:
    print(f"removed by hand: {n0 - len(features)} of {len(REMOVED)} listed in source/removed.txt")
print(f"features: {len(features)}  turbines: {len(turbines)}")

# downloaded imagery: fetch every tile pass 1 will look at in one parallel go
if IMAGERY == "open":
    need = set()
    for f in features:
        if f["kind"] not in ("part", "bldg", "britam", "silo", "stadium"):
            continue
        P = [gpx(lo, la) for lo, la in f["pts"]]
        ex, ey = lean_px(f["h"])
        m = 12 + 0.12 * f["h"] / MPP
        xs = [p[0] + ex * t for p in P for t in (0, 1)]
        ys = [p[1] + ey * t for p in P for t in (0, 1)]
        x0, y0, x1, y1 = int(min(xs) - m), int(min(ys) - m), int(max(xs) + m) + 1, int(max(ys) + m) + 1
        if (x1 - x0) * (y1 - y0) < 2500 * 2500:
            need.update(tiles_for(x0, y0, x1, y1))
    ortho.prefetch(need)

# ---------------------------------------------------------------- pass 1: colours from imagery
for f in features:
    if f["kind"] in ("part", "bldg", "britam", "silo"):
        analyse(f)
        if f["kind"] == "silo":
            f["glass"] = False
    elif f["kind"] == "stadium":
        h = f["h"]
        c = poly_crop(f["pts"], lean_px(h * 0.5))
        f["roof"] = ("img", c) if c else ("rgb", (170, 168, 160))
        wall = robust_colour(c[0], c[1]) if c else None
        f["wall"] = mix(wall, (235, 232, 222), 0.4) if wall else (215, 212, 204)
        f["glass"] = False

for f in features:
    if f["id"] in WALL_FIX and "wall" in f:
        f["wall"] = WALL_FIX[f["id"]]      # street-view colour beats the imagery sample
    if f.get("spec", {}).get("glass"):
        f["glass"] = True
bld = [f for f in features if "wall" in f and f["kind"] != "silo"]


def spread_colours(group, k_lum, k_chroma):
    """The imagery puts most towers in a narrow grey-beige band: push each facade away from
    the group mean (luma and chroma separately) so neighbouring towers stay distinguishable.
    Hue order is preserved; nothing is invented."""
    if len(group) < 3:
        return
    Y = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    ys = np.array([Y(f["wall"]) for f in group])
    chroma = np.array([[c - Y(f["wall"]) for c in f["wall"]] for f in group])   # rgb minus luma
    my, mc = ys.mean(), chroma.mean(0)
    for f, y, c in zip(group, ys, chroma):
        f["wall_raw"] = f["wall"]
        ny = my + (y - my) * k_lum
        nc = mc + (c - mc) * k_chroma
        f["wall"] = tuple(float(np.clip(ny + v, 18, 245)) for v in nc)


tall = [f for f in bld if f["h"] >= 25 and f["kind"] != "stadium" and f["id"] not in WALL_FIX]
spread_colours([f for f in tall if not f["glass"]], 1.35, 1.8)
spread_colours([f for f in tall if f["glass"]], 1.2, 1.4)
# the facade sample at the silos is dominated by the red roof; the bin bottoms visible in
# the imagery are pale grey concrete
silo_wall = (208, 205, 196)
punch_X = [f["wall"] for f in bld if not f["glass"]]
glass_X = [f["wall"] for f in bld if f["glass"]] or [(90, 110, 125)]
# colour weight: tall towers, and named core landmarks as if 100 m, get the most faithful strips
weight = lambda f: max(100.0 if f.get("core") else f["h"], 8) ** 1.5
PUNCH_C = kmeans(punch_X, N_PUNCH, [weight(f) for f in bld if not f["glass"]])
PUNCH_C = PUNCH_C[np.argsort(PUNCH_C @ np.array([0.3, 0.59, 0.11], np.float32))]
GLASS_C = kmeans(glass_X, N_GLASS, [max(f["h"], 8) ** 1.5 for f in bld if f["glass"]] or None)
i = j = 0
for f in bld:
    if f["glass"]:
        f["strip"] = N_PUNCH + int(np.argmin(((GLASS_C - np.array(f["wall"])) ** 2).sum(1)))
    else:
        f["strip"] = int(np.argmin(((PUNCH_C - np.array(f["wall"])) ** 2).sum(1)))
    kind, val = f["roof"]
    if kind == "img":
        arr, mask, origin = val
        key = ("img", f["id"])
        atlas.add_img(key, arr)
        f["roof_img"] = (key, origin)
    else:
        atlas.swatch(val)

for f in features:
    if f["kind"] == "silo":
        f["strip"] = BINS
        kind, val = f["roof"]
        if kind == "img":
            f["roof_img"] = (atlas.add_img(("img", f["id"]), val[0]), val[2])
        else:
            atlas.swatch(val)
atlas.facades([tuple(c) for c in PUNCH_C], [tuple(c) for c in GLASS_C], silo_wall)
# fixed swatches used by masts, markers, stadium furniture
RED, WHITE, BLACK, STEEL = (178, 40, 36), (238, 238, 232), (28, 28, 28), (120, 122, 124)
SILO_CAP = (140, 150, 142)             # grey-green roof cap edge seen in street view
BRICK = (176, 112, 78)                 # Ndula seed plant brick walls (street view)
for c in (RED, WHITE, BLACK, STEEL, PLANT, tuple(v * 0.82 for v in PLANT), (150, 152, 150), silo_wall, SILO_CAP, BRICK):
    atlas.swatch(c)
for c in DETAIL_COLOURS():
    atlas.swatch(c)
scale = atlas.pack()
print(f"atlas: {len(atlas.items)} items, roof imagery scale {scale:.2f} "
      f"({MPP / scale:.2f} m/px)")


def roof_mat(f):
    if "roof_img" in f:
        key, origin = f["roof_img"]
        sh = f.get("shift") or lean_px(f["h"] * 0.5 if f["kind"] == "stadium" else f["h"])
        return Mat(img=(key, origin, sh))
    return Mat(f["roof"][1])


# ---------------------------------------------------------------- special builders


def build_talanta(o, P, f):
    """Talanta Stadium (drone photo + renders): silver-grey facade crossed by a white diamond
    lattice, red/black Maasai shields at the four corners and mid-sides, a white cushioned roof
    ring sloping up to an oval opening, red seats. Low-poly: plain swatch colours."""
    h = f["h"]
    cx, cn = centroid(P)
    nP = len(P)
    skin, white, black, red = Mat(TALANTA_SKIN), Mat(WHITE), Mat(BLACK), Mat(RED)
    solid_walls(o, P, -SINK, h, skin)
    # stands: red seats from the facade top down to the pitch edge, low inner wall
    ki = f.get("inner", 0.5)
    inner = [(cx + (p[0] - cx) * ki, cn + (p[1] - cn) * ki) for p in P]
    seats = Mat(TALANTA_SEATS)
    us, vs = seats.uv(0, 0)
    for k in range(nP):
        a, b, ia, ib = P[k - 1], P[k], inner[k - 1], inner[k]
        o.quad(o.vert(a[0], h - 4, a[1], 0, 1, 0, us, vs), o.vert(b[0], h - 4, b[1], 0, 1, 0, us, vs),
               o.vert(ib[0], 3, ib[1], 0, 1, 0, us, vs), o.vert(ia[0], 3, ia[1], 0, 1, 0, us, vs))
    solid_walls(o, inner[::-1], -SINK, 3, skin)
    # roof ring: rounded shoulder at the facade top, rising gently to the opening's edge,
    # with a dark fascia hanging under the edge (the steel truss seen through the opening)
    ring_at = lambda kk: [(cx + (p[0] - cx) * kk, cn + (p[1] - cn) * kk) for p in P]
    profile = [(1.0, h), (0.96, h + 4.0), (0.57, h + 7.0)]
    wu, wv = white.uv(0, 0)
    for (k0, y0), (k1, y1) in zip(profile, profile[1:]):
        R0, R1 = ring_at(k0), ring_at(k1)
        for k in range(nP):
            o.quad(o.vert(R0[k - 1][0], y0, R0[k - 1][1], 0, 1, 0, wu, wv), o.vert(R0[k][0], y0, R0[k][1], 0, 1, 0, wu, wv),
                   o.vert(R1[k][0], y1, R1[k][1], 0, 1, 0, wu, wv), o.vert(R1[k - 1][0], y1, R1[k - 1][1], 0, 1, 0, wu, wv))
    edge = ring_at(0.57)
    solid_walls(o, edge[::-1], h + 1.0, h + 7.0, Mat(TALANTA_FASCIA))
    for k in range(0, nP, 2):       # floodlights along the opening's edge, aimed at the pitch
        ex, en = edge[k]
        dx, dn = cx - ex, cn - en
        L = math.hypot(dx, dn) or 1
        o.lights.append(f"LIGHT_PARAM spot_params_bb {ex:.2f} {h + 1.5:.2f} {-en:.2f} "
                        f"1 1 0.95 2.5 {dx / L * 2.5:.3f} -1.5 {-dn / L * 2.5:.3f} -2")

    # facade frame by arc length: point, tangent and outward normal at distance u
    cum = [0.0]
    for k in range(nP):
        a, b = P[k], P[(k + 1) % nP]
        cum.append(cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
    perim = cum[-1]

    def seg_of(u):
        u %= perim
        k = max(0, min(nP - 1, bisect.bisect_right(cum, u) - 1))
        return k, u - cum[k]

    def frame(k):
        a, b = P[k], P[(k + 1) % nP]
        L = math.hypot(b[0] - a[0], b[1] - a[1]) or 1
        t = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        return a, t, (t[1], -t[0])

    def at(u, off):
        k, d = seg_of(u)
        a, t, nr = frame(k)
        return (a[0] + t[0] * d + nr[0] * off, a[1] + t[1] * d + nr[1] * off), nr

    # white diamond lattice: straight diagonals crossing at mid-height, diamonds a full storey of
    # the facade tall; each diagonal is split at the facade's vertices so it hugs the curve
    n_d = max(8, round(perim / 36.0))
    W, y_lo, y_hi, tw = perim / n_d, 0.0, h, 2.2
    wu, wv = white.uv(0, 0)
    for j in range(n_d):
        for sgn in (1, -1):
            u0 = j * W
            cuts = sorted({0.0, W} | {((c - u0) * sgn) % perim for c in cum[:-1]
                                       if 0 < ((c - u0) * sgn) % perim < W})
            for s0, s1 in zip(cuts, cuts[1:]):
                um = u0 + sgn * (s0 + s1) / 2
                k, _ = seg_of(um)
                a, t, nr = frame(k)
                pts = []
                for sv in (s0, s1):
                    q, _ = at(u0 + sgn * sv, 0.25)
                    y = y_lo + (y_hi - y_lo) * sv / W
                    pts.append((q, y))
                (qa, ya), (qb, yb) = pts
                if sgn < 0:
                    (qa, ya), (qb, yb) = (qb, yb), (qa, ya)
                o.quad(o.vert(qa[0], ya - tw / 2, qa[1], nr[0], 0, nr[1], wu, wv),
                       o.vert(qb[0], yb - tw / 2, qb[1], nr[0], 0, nr[1], wu, wv),
                       o.vert(qb[0], yb + tw / 2, qb[1], nr[0], 0, nr[1], wu, wv),
                       o.vert(qa[0], ya + tw / 2, qa[1], nr[0], 0, nr[1], wu, wv))

    # Maasai shields: at the four corners and mid-sides, by angle around the centre
    def u_at_bearing(deg):
        best = None
        for k in range(nP):
            bx, bn = P[k][0] - cx, P[k][1] - cn
            d = abs((math.degrees(math.atan2(bx, bn)) - deg + 180) % 360 - 180)
            if best is None or d < best[0]:
                best = (d, cum[k])
        return best[1]

    def ellipse(u, yc, w, hh, off, mat, seg=14):
        k, d = seg_of(u)
        a, t, nr = frame(k)
        base = (a[0] + t[0] * d + nr[0] * off, a[1] + t[1] * d + nr[1] * off)
        mu, mv = mat.uv(0, 0)
        c = o.vert(base[0], yc, base[1], nr[0], 0, nr[1], mu, mv)
        ring_ids = []
        for i in range(seg):
            th = 2 * math.pi * i / seg
            x, y = w / 2 * math.cos(th), hh / 2 * math.sin(th)
            ring_ids.append(o.vert(base[0] + t[0] * x, yc + y, base[1] + t[1] * x, nr[0], 0, nr[1], mu, mv))
        for i in range(seg):
            o.tri(c, ring_ids[i], ring_ids[(i + 1) % seg])

    def bar(u, y0, y1, w, off, mat):
        k, d = seg_of(u)
        a, t, nr = frame(k)
        mu, mv = mat.uv(0, 0)
        p0 = (a[0] + t[0] * (d - w / 2) + nr[0] * off, a[1] + t[1] * (d - w / 2) + nr[1] * off)
        p1 = (a[0] + t[0] * (d + w / 2) + nr[0] * off, a[1] + t[1] * (d + w / 2) + nr[1] * off)
        o.quad(o.vert(p0[0], y0, p0[1], nr[0], 0, nr[1], mu, mv), o.vert(p1[0], y0, p1[1], nr[0], 0, nr[1], mu, mv),
               o.vert(p1[0], y1, p1[1], nr[0], 0, nr[1], mu, mv), o.vert(p0[0], y1, p0[1], nr[0], 0, nr[1], mu, mv))

    yc = h * 0.5
    for deg in range(0, 360, 45):
        u = u_at_bearing(deg)
        ellipse(u, yc, 14.0, 30.0, 1.0, black)
        ellipse(u, yc, 9.5, 28.0, 1.3, red)
        bar(u, yc - 12.0, yc + 12.0, 1.1, 1.6, white)
        ellipse(u, yc, 3.0, 3.0, 1.7, white, seg=8)
    return h + 7.0


def build_stadium(o, P, f):
    if f.get("talanta"):
        return build_talanta(o, P, f)
    h = f["h"]
    cx, cn = centroid(P)
    ki = f.get("inner", 0.80)          # stand edge as a fraction of the outer outline
    inner = [(cx + (p[0] - cx) * ki, cn + (p[1] - cn) * ki) for p in P]
    s = f["strip"]
    u = 0.0
    for k in range(len(P)):
        u = wall_band(o, P[k - 1], P[k], -SINK, h, s, u)
    seats = roof_mat(f)
    for k in range(len(P)):
        a, b, ia, ib = P[k - 1], P[k], inner[k - 1], inner[k]
        e1 = (b[0] - a[0], 0, b[1] - a[1])
        e2 = (ia[0] - a[0], 4 - h, ia[1] - a[1])
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        L = math.sqrt(nx * nx + ny * ny + nz * nz) or 1
        nx, ny, nz = nx / L, ny / L, nz / L
        if ny < 0:
            nx, ny, nz = -nx, -ny, -nz
        i0 = o.vert(a[0], h, a[1], nx, ny, nz, *seats.uv(*a))
        i1 = o.vert(b[0], h, b[1], nx, ny, nz, *seats.uv(*b))
        i2 = o.vert(ib[0], 4, ib[1], nx, ny, nz, *seats.uv(*ib))
        i3 = o.vert(ia[0], 4, ia[1], nx, ny, nz, *seats.uv(*ia))
        o.quad(i0, i1, i2, i3)
    u = 0.0
    inner_cw = inner[::-1]
    for k in range(len(inner_cw)):   # pitch-side wall (faces inwards)
        u = wall_band(o, inner_cw[k - 1], inner_cw[k], -SINK, 4, s, u)
    if f.get("canopy"):
        # roof canopy ring over the stands, lights along its inner edge aimed at the pitch
        white = Mat(WHITE)
        kc = ki + (1 - ki) * 0.35
        edge = [(cx + (p[0] - cx) * kc, cn + (p[1] - cn) * kc) for p in P]
        us, vs = white.uv(0, 0)
        for k in range(len(P)):
            a, b, ea, eb = P[k - 1], P[k], edge[k - 1], edge[k]
            q = [o.vert(a[0], h + 2, a[1], 0, 1, 0, us, vs), o.vert(b[0], h + 2, b[1], 0, 1, 0, us, vs),
                 o.vert(eb[0], h + 6, eb[1], 0, 1, 0, us, vs), o.vert(ea[0], h + 6, ea[1], 0, 1, 0, us, vs)]
            o.quad(*q)
        solid_walls(o, P, h, h + 2, white)
        for k in range(0, len(edge), 2):
            ex, en = edge[k]
            dx, dn = cx - ex, cn - en
            L = math.hypot(dx, dn) or 1
            o.lights.append(f"LIGHT_PARAM spot_params_bb {ex:.2f} {h + 5.5:.2f} {-en:.2f} "
                            f"1 1 0.95 2.5 {dx / L * 2.5:.3f} -1.5 {-dn / L * 2.5:.3f} -2")
        return h + 6
    # floodlight masts with lamp heads aimed at the pitch centre
    steel = Mat(STEEL)
    top = h + 18
    for p in (max(P), min(P), max(P, key=lambda q: q[1]), min(P, key=lambda q: q[1])):
        sq = [(p[0] - 1.0, p[1] - 1.0), (p[0] + 1.0, p[1] - 1.0), (p[0] + 1.0, p[1] + 1.0), (p[0] - 1.0, p[1] + 1.0)]
        solid_prism(o, sq, 0, top, steel)
        dx, dn = cx - p[0], cn - p[1]
        L = math.hypot(dx, dn) or 1
        dx, dn = dx / L, dn / L
        head = [(p[0] - 3 * dn, p[1] + 3 * dx), (p[0] + 3 * dn, p[1] - 3 * dx)]
        for k in range(5):
            t = k / 4
            lx = head[0][0] + (head[1][0] - head[0][0]) * t
            ln = head[0][1] + (head[1][1] - head[0][1]) * t
            o.lights.append(f"LIGHT_PARAM spot_params_bb {lx:.2f} {top + 1:.2f} {-ln:.2f} "
                            f"1 1 0.95 2.5 {dx * 2.5:.3f} -1.2 {-dn * 2.5:.3f} -2")
    return top


def min_rect(P):
    """Minimum-area bounding rectangle: (angle, x0, x1, y0, y1) in the rotated frame."""
    best = None
    for k in range(len(P)):
        a, b = P[k - 1], P[k]
        ang = math.atan2(b[1] - a[1], b[0] - a[0])
        c, s_ = math.cos(-ang), math.sin(-ang)
        R = [(p[0] * c - p[1] * s_, p[0] * s_ + p[1] * c) for p in P]
        xs, ys = [r[0] for r in R], [r[1] for r in R]
        A = (max(xs) - min(xs)) * (max(ys) - min(ys))
        if best is None or A < best[0]:
            best = (A, ang, min(xs), max(xs), min(ys), max(ys))
    return best[1:]


def build_silos(o, P, f):
    """NCPB Nairobi silo (street view): a grid of 9.2 m concrete bins under a flat roof deck
    with a grey-green cap, and a taller elevator tower with small windows at the north end."""
    h = f["h"]
    ang, x0, x1, y0, y1 = min_rect(P)
    if x1 - x0 < y1 - y0:                       # make u the long axis
        ang += math.pi / 2
        x0, x1, y0, y1 = y0, y1, -x1, -x0
    c, s_ = math.cos(ang), math.sin(ang)

    def world(u, v):
        return (u * c - v * s_, u * s_ + v * c)

    # the tower is at the north end (both street views)
    north_high = world(x1, 0)[1] > world(x0, 0)[1]
    TOWER, D = 18.0, 9.2
    tu0, tu1 = (x1 - TOWER, x1) if north_high else (x0, x0 + TOWER)
    bu0, bu1 = (x0, x1 - TOWER) if north_high else (x0 + TOWER, x1)
    rect = lambda a0, a1, b0, b1: [world(a0, b0), world(a1, b0), world(a1, b1), world(a0, b1)]
    roof = roof_mat(f)
    tower_strip = BINS
    prism(o, rect(tu0, tu1, y0, y1), h + 12, 0, tower_strip, roof)
    concrete = Mat(silo_wall)
    nu, nv = int((bu1 - bu0) // D), int((y1 - y0) // D)
    ou = bu0 + ((bu1 - bu0) - nu * D) / 2 + D / 2
    ov = y0 + ((y1 - y0) - nv * D) / 2 + D / 2
    for i in range(nu):
        for j in range(nv):
            cx, cn = world(ou + i * D, ov + j * D)
            circ = [(cx + 4.55 * math.cos(t), cn + 4.55 * math.sin(t)) for t in
                    [2 * math.pi * k / 16 for k in range(16)]]
            solid_walls(o, circ, -SINK, h - 1, concrete)
    deck = rect(bu0 - 0.3, bu1, y0 - 0.3, y1 + 0.3)
    solid_walls(o, deck, h - 1, h + 2, Mat(SILO_CAP))
    flat_cap(o, deck, h + 2, roof)
    flat_cap(o, deck, h - 1, Mat(SILO_CAP), up=False)
    return h + 12, nu * nv


# ---------------------------------------------------------------- pass 2: objects
def cell_key(f):
    lo, la = centroid(f["pts"])
    if f["kind"] in ("stadium", "silo"):
        return f"{f['kind']}_{f['id']}"
    return f"c_{int((lo - 36) / 0.0025)}_{int((la + 2) / 0.0025)}"


clusters = {}
for f in features:
    clusters.setdefault(cell_key(f), []).append(f)

TEX_NAME, LIT_NAME = atlas.save()
dsf_objs, dsf_excl = [], []
feature_index = {}   # objects/<cell>.obj -> buildings and their triangle ranges (viewer)
for key, fs in sorted(clusters.items()):
    allpts = [p for f in fs for p in f["pts"]]
    lon0, lat0 = centroid(allpts)
    Ctx.set(lon0, lat0)
    o = Obj()
    tallest = (0, None, None)
    starts = []          # each building's first index in o.i (it ends where the next starts)
    for f in fs:
        starts.append((f, len(o.i)))
        P = to_local(f["pts"], lon0, lat0)
        if len(P) < 3 or abs(area2(P)) < 0.5:
            continue
        t = f["t"]
        if f["kind"] == "stadium":
            top = build_stadium(o, P, f)
            report.append(f"{f['name']}: stadium bowl {f['h']:.0f} m, imagery stands, floodlights")
            tallest = max(tallest, (top, P, f), key=lambda z: z[0])
            continue
        if f["kind"] == "silo":
            top, n = build_silos(o, P, f)
            report.append(f"{f['name']}: {n} bins {f['h']:.0f} m + elevator tower {top:.0f} m, imagery roof")
            tallest = max(tallest, (top, P, f), key=lambda z: z[0])
            continue
        if f["kind"] == "mast":
            cx, cn = centroid(P)
            band = f["h"] / 7
            red, white = Mat(RED), Mat(WHITE)
            for k in range(7):   # aviation red/white bands
                solid_prism(o, P, k * band if k else -SINK, (k + 1) * band, red if k % 2 == 0 else white)
            o.obs(cx, f["h"] + 0.3, cn)
            o.obs(cx + 1.0, f["h"] / 2, cn)
            report.append(f"KBC Ngong mast: {f['h']:.0f} m")
            continue
        if f["kind"] == "beacon":
            cx, cn = centroid(P)
            post = [(cx + 1.3 * math.cos(a), cn + 1.3 * math.sin(a)) for a in [2 * math.pi * k / 12 for k in range(12)]]
            band = f["h"] / 6
            black, white = Mat(BLACK), Mat(WHITE)
            for k in range(6):   # alternate black and white horizontal stripes
                solid_prism(o, post, k * band if k else -SINK, (k + 1) * band, black if k % 2 == 0 else white)
            us, vs = white.uv(0, 0)  # 100 ft (30.5 m) diameter white circle on the ground
            seg = 48
            for k in range(seg):
                a0, a1 = 2 * math.pi * k / seg, 2 * math.pi * (k + 1) / seg
                q = [o.vert(cx + r * math.cos(a), 0.35, cn + r * math.sin(a), 0, 1, 0, us, vs)
                     for r, a in ((13.7, a0), (15.24, a0), (15.24, a1), (13.7, a1))]
                o.quad(q[0], q[3], q[2], q[1])
            report.append("Visual Marker beacon (Nazarene Univ.): striped post + 100 ft white circle")
            continue
        if f.get("spec") or f["id"] == PARLIAMENT_TOWER:
            top = build_core(o, P, f)
            report.append(f"{f.get('rel_name') or f['name'] or f['id']}: core landmark {top:.0f} m")
            tallest = max(tallest, (top, P, f), key=lambda z: z[0])
            continue
        if f["kind"] == "britam":
            top, _ = prism(o, P, 190, 0, f["strip"], roof_mat(f), taper=0.22, base=True)
            cx, cn = centroid(P)
            mast = [(cx + 0.8 * math.cos(a), cn + 0.8 * math.sin(a)) for a in [2 * math.pi * k / 6 for k in range(6)]]
            steel = Mat(STEEL)
            solid_walls(o, mast, 190, 198, steel)
            apex_roof(o, mast, 198, 200, steel)
            o.obs(cx, 200.5, cn)
            report.append("Britam Tower: tapered prism 190 m + mast 200 m")
            tallest = max(tallest, (190, top, f), key=lambda z: z[0])
            continue
        h, minh = f["h"], f["minh"]
        shape = (t.get("roof:shape") or "flat").lower()
        rh = num(t.get("roof:height")) or 0.0
        if shape in ("cone", "pyramidal", "dome", "onion") and rh >= h - minh:
            rh = h - minh
        if shape not in ("cone", "pyramidal", "dome", "onion"):
            shape = "flat"
        shop = "site" not in f and h >= 12 and minh <= 0
        top, _ = prism(o, P, h, minh, f["strip"], roof_mat(f), shape, rh, base=shop)
        if shape == "flat" and h >= 35 and "roof_img" not in f:
            roof_details(o, P, h, f["id"])
        if f["name"] and h >= 55:
            report.append(f"{f['name']}: {h:.0f} m")
        if h > tallest[0]:
            tallest = (h, top, f)
    if not o.i:
        continue
    # obstruction lights on the tallest structure of the cluster if >= 60 m
    if tallest[0] >= 60 and tallest[2]["kind"] not in ("britam",):
        P = tallest[1]
        for p in {max(P), min(P), max(P, key=lambda q: q[1]), min(P, key=lambda q: q[1])}:
            o.obs(p[0], tallest[0] + 0.5, p[1])
    name = f"{key}.obj"
    o.write(f"{OUT}/objects/{name}", TEX_NAME, LIT_NAME)
    ends = [i0 for _, i0 in starts[1:]] + [len(o.i)]
    def where(f):        # footprint centroid and radius, for the viewer and tools/fetch_photos.py
        lo, la = centroid(f["pts"])
        kx = 111320.0 * math.cos(math.radians(la))
        r = max(math.hypot((p[0] - lo) * kx, (p[1] - la) * M_LAT) for p in f["pts"])
        return dict(lat=round(la, 6), lon=round(lo, 6), r=round(r, 1))
    feature_index[f"objects/{name}"] = [
        dict(id=str(f["id"]), name=f.get("rel_name") or f.get("name") or f.get("site") or "",
             kind=f["kind"], h=round(f["h"], 1), tri0=i0 // 3, tri1=i1 // 3, **where(f),
             wall=[round(c) for c in f["wall"]] if f.get("wall") else None, glass=bool(f.get("glass")),
             spec=f.get("spec") or None, edited=str(f["id"]) in OVERRIDES)
        for (f, i0), i1 in zip(starts, ends) if i1 > i0]
    dsf_objs.append((f"objects/{name}", lon0, lat0, 0.0))
    for f in fs:
        lons, lats = [p[0] for p in f["pts"]], [p[1] for p in f["pts"]]
        dsf_excl.append(((min(lons), min(lats), max(lons), max(lats)), ("obj", "fac", "for")))

# whole-site exclusion of simHeaven generic houses/facades: ~44 m cells whose centre is
# inside the site boundary (trees are kept)
CELL = 0.0004
for name, rings in site_bounds.items():
    for r in rings:
        w, s_, e, n = min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)
        la = s_
        while la < n:
            lo = w
            while lo < e:
                if pip((lo + CELL / 2, la + CELL / 2), r):
                    dsf_excl.append(((lo, la, lo + CELL, la + CELL), ("obj", "fac")))
                lo += CELL
            la += CELL

def installed_objects(pack, tile="-02+036"):
    """(definition, lon, lat) of every object another installed pack places on a tile;
    empty if that pack (or DSFTool) isn't available."""
    f = f"{XP_SCENERY}/{pack}/Earth nav data/-10+030/{tile}.dsf"
    if not (XP_SCENERY and DSFTOOL and os.path.exists(f)):
        print(f"  {pack} {tile}: not installed, skipped")
        return []
    tmp = f"{OUT}/_{pack}{tile}.txt"
    subprocess.run([DSFTOOL, "--dsf2text", f, tmp], check=True, capture_output=True)
    defs, out = [], []
    with open(tmp) as fh:
        for line in fh:
            if line.startswith("OBJECT_DEF"):
                defs.append(line.split()[1])
            elif line.startswith("OBJECT "):
                _, i, lo, la, *_ = line.split()
                out.append((defs[int(i)], float(lo), float(la)))
    os.remove(tmp)
    return out


# never let an exclusion reach simHeaven's hand-made models (X-Africa-1-vfr: KICC, Times
# Tower, Hilton on this tile)
HANDMADE = [(lo, la) for _, lo, la in installed_objects("simHeaven_X-Africa-1-vfr")]
GUARD = 60.0


def box_dist(box, p):
    w, s_, e, n = box
    dx = max(w - p[0], 0, p[0] - e) * 111320 * math.cos(math.radians(p[1]))
    dy = max(s_ - p[1], 0, p[1] - n) * M_LAT
    return math.hypot(dx, dy)


n0 = len(dsf_excl)
dsf_excl = [(b, k) for b, k in dsf_excl if all(box_dist(b, h) > GUARD for h in HANDMADE)]
print(f"exclusions: {len(dsf_excl)} ({n0 - len(dsf_excl)} dropped near simHeaven hand-made models)")

# simHeaven X-Africa-6 places its own generic turbine on (older) OSM positions of the same
# Ngong farm, up to ~80 m off ours: exclude exactly those, so each turbine is drawn once
n_dup = 0
for d_, lo, la in installed_objects("simHeaven_X-Africa-6-scenery"):
    if d_.endswith("wind-turbine.obj") and any(
            math.hypot((lo - tlo) * 111320 * math.cos(math.radians(la)), (la - tla) * M_LAT) < 1500
            for tlo, tla in turbines):
        d = 12.0 / M_LAT
        dsf_excl.append(((lo - d, la - d, lo + d, la + d), ("obj",)))
        n_dup += 1
print(f"simHeaven duplicate Ngong turbines excluded: {n_dup}")

# ---------------------------------------------------------------- AIP painted markers
def axes(bearing):
    b = math.radians(bearing)
    return (math.sin(b), math.cos(b)), (math.cos(b), -math.sin(b))   # along, across (east, north)


def offset_ll(lat, lon, metres_along, bearing):
    b = math.radians(bearing)
    kx = 111320.0 * math.cos(math.radians(lat))
    return lat + metres_along * math.cos(b) / M_LAT, lon + metres_along * math.sin(b) / kx


def build_ndula(lat, lon):
    """Ndula Marker: gabled brick shed (Delmonte seed plant, Canary Rd) whose roof is
    painted in black and white squares. Position = Google pin; size/heading measured on
    the Google aerial (scale bar): ~26 x 13 m, ridge along 127 deg."""
    Ctx.set(lon, lat)
    o = Obj()
    (ax, an), (cx_, cn_) = axes(127.0)
    L, W, WALL, RISE, OH, SQ = 26.0, 13.0, 4.2, 3.2, 0.5, 1.8
    P = lambda s, t: (ax * s + cx_ * t, an * s + cn_ * t)
    base = [P(-L / 2, -W / 2), P(L / 2, -W / 2), P(L / 2, W / 2), P(-L / 2, W / 2)]
    if area2(base) < 0:
        base.reverse()
    brick = Mat(BRICK)
    solid_walls(o, base, -SINK, WALL, brick)
    for s_end in (-L / 2, L / 2):                         # gable triangles
        a, b, r = P(s_end, -W / 2), P(s_end, W / 2), P(s_end, 0)
        u, v = brick.uv(0, 0)
        nx, nn = ax * (1 if s_end > 0 else -1), an * (1 if s_end > 0 else -1)
        i0 = o.vert(a[0], WALL, a[1], nx, 0, nn, u, v)
        i1 = o.vert(b[0], WALL, b[1], nx, 0, nn, u, v)
        i2 = o.vert(r[0], WALL + RISE, r[1], nx, 0, nn, u, v)
        o.tri(i0, i1, i2)
    black, white = atlas.sw_uv(Mat(BLACK).key), atlas.sw_uv(Mat(WHITE).key)
    half = W / 2 + OH
    drop = RISE * OH / (W / 2)
    n_s, n_t = int(math.ceil((L + 2 * OH) / SQ)), int(math.ceil(math.hypot(half, RISE + drop) / SQ))
    for side in (-1, 1):                                  # two roof slopes
        eave_t, eave_y = side * half, WALL - drop
        nrm = (cx_ * side * RISE, half, cn_ * side * RISE)
        k = math.sqrt(sum(c * c for c in nrm))
        nx, ny, nn = nrm[0] / k, nrm[1] / k, nrm[2] / k
        for i in range(n_s):
            s0 = -L / 2 - OH + i * (L + 2 * OH) / n_s
            s1 = s0 + (L + 2 * OH) / n_s
            for j in range(n_t):
                f0, f1 = j / n_t, (j + 1) / n_t                 # 0 = eave, 1 = ridge
                uv = black if (i + j) % 2 else white
                q = []
                for s_, f_ in ((s0, f0), (s1, f0), (s1, f1), (s0, f1)):
                    x, n = P(s_, eave_t * (1 - f_))
                    q.append(o.vert(x, eave_y + (WALL + RISE - eave_y) * f_, n, nx, ny, nn, *uv))
                o.quad(*q)
    return o


def build_twin_bridge(lat, lon, bearing):
    """Twin Bridges marker (AIP: bridge painted black and white): striped parapets on the
    old bridge beside the Namanga Rd bridge, as seen in the user's street view."""
    Ctx.set(lon, lat)
    o = Obj()
    (ax, an), (cx_, cn_) = axes(bearing)
    L, HALF, T, SEG = 32.0, 3.8, 0.4, 2.0
    for side in (-1, 1):
        for i in range(int(L / SEG)):
            s0 = -L / 2 + i * SEG
            box = []
            for s_, t_ in ((s0, side * HALF - T / 2), (s0 + SEG, side * HALF - T / 2),
                           (s0 + SEG, side * HALF + T / 2), (s0, side * HALF + T / 2)):
                box.append((ax * s_ + cx_ * t_, an * s_ + cn_ * t_))
            if area2(box) < 0:
                box.reverse()
            solid_prism(o, box, -3.0, 1.2, Mat(BLACK) if i % 2 else Mat(WHITE))
    return o


NDULA = (-1.0324459, 37.2477668)                    # user's Google pin "NDULA MARKER"
o = build_ndula(*NDULA)
o.write(f"{OUT}/objects/marker_ndula.obj", TEX_NAME, LIT_NAME)
dsf_objs.append(("objects/marker_ndula.obj", NDULA[1], NDULA[0], 0.0))
d = 20.0 / M_LAT
dsf_excl.append(((NDULA[1] - d, NDULA[0] - d, NDULA[1] + d, NDULA[0] + d), ("obj", "fac")))
report.append("Ndula Marker: checkered gabled roof 26 x 13 m (tile -02+037)")

# street-view camera on the Namanga Rd bridge looking 310 deg at the old bridge; the road
# runs ~020 deg (imagery), the old bridge is ~22 m west of it
TWINB_CAM, ROAD = (-1.5738636, 36.929118), 20.0
la_, lo_ = offset_ll(*TWINB_CAM, 22.0, ROAD - 90)
la_, lo_ = offset_ll(la_, lo_, 8.0, ROAD)
TWINB = (la_, lo_)
o = build_twin_bridge(la_, lo_, ROAD)
o.write(f"{OUT}/objects/marker_twin_bridges.obj", TEX_NAME, LIT_NAME)
dsf_objs.append(("objects/marker_twin_bridges.obj", lo_, la_, 0.0))
report.append("Twin Bridges marker: black/white striped bridge parapets 32 m")

def build_water_tower(lat, lon):
    """Generic elevated water tower (no OSM/imagery detail available): four steel legs,
    white cylindrical tank with a cone roof, red obstruction light on top."""
    Ctx.set(lon, lat)
    o = Obj()
    steel, white = Mat(STEEL), Mat(WHITE)
    LEG_TOP, TANK_TOP, R = 22.0, 28.5, 4.6
    for sx, sn in ((-3, -3), (3, -3), (3, 3), (-3, 3)):
        leg = [(sx - 0.35, sn - 0.35), (sx + 0.35, sn - 0.35), (sx + 0.35, sn + 0.35), (sx - 0.35, sn + 0.35)]
        solid_prism(o, leg, -SINK, LEG_TOP, steel)
    for y in (8.0, 15.0):                                 # horizontal bracing rings
        brace = [(-3.4, -3.4), (3.4, -3.4), (3.4, 3.4), (-3.4, 3.4)]
        inner = [(-3.0, -3.0), (-3.0, 3.0), (3.0, 3.0), (3.0, -3.0)]
        solid_walls(o, brace, y, y + 0.5, steel)
        solid_walls(o, inner, y, y + 0.5, steel)
    tank = [(R * math.cos(t), R * math.sin(t)) for t in [2 * math.pi * k / 20 for k in range(20)]]
    solid_walls(o, tank, LEG_TOP, TANK_TOP, white)
    flat_cap(o, tank, LEG_TOP, white, up=False)
    apex_roof(o, tank, TANK_TOP, TANK_TOP + 2.0, white)
    o.obs(0, TANK_TOP + 2.3, 0)
    return o


# AIP HKNW AD 2.22 VFR route point LORESHO (water towers): exact towers unknown, so a generic
# tower at each published position (north arrival, State House departure)
for tag, (lat, lon) in (("arr", (-(1 + 13 / 60 + 43.211 / 3600), 36 + 42 / 60 + 8.997 / 3600)),
                        ("dep", (-(1 + 14 / 60 + 3 / 3600), 36 + 42 / 60 + 46 / 3600))):
    o = build_water_tower(lat, lon)
    o.write(f"{OUT}/objects/marker_loresho_{tag}.obj", TEX_NAME, LIT_NAME)
    dsf_objs.append((f"objects/marker_loresho_{tag}.obj", lon, lat, 0.0))
    d = 8.0 / M_LAT
    dsf_excl.append(((lon - d, lat - d, lon + d, lat + d), ("obj", "fac", "for")))
report.append("LORESHO water towers (AIP positions, generic model): 2 x 30 m")

TURBINE = "FlyAgi_Wind_Turbines/Onshore/Wind_turbine_onshore_0050_white.obj"
for lo, la in turbines:
    dsf_objs.append((TURBINE, lo, la, 90.0))
report.append(f"Ngong Hills wind farm: {len(turbines)} turbines (OpenSceneryX)")

# ---------------------------------------------------------------- DSF text (one per tile)
def tile_of(lon, lat):
    return math.floor(lat), math.floor(lon)


tiles = sorted({tile_of(lo, la) for _, lo, la, _ in dsf_objs})
for ts, tw in tiles:
    name = f"{ts:+03d}{tw:+04d}"
    objs = [o_ for o_ in dsf_objs if tile_of(o_[1], o_[2]) == (ts, tw)]
    excl = [e for e in dsf_excl if tile_of(e[0][0], e[0][1]) == (ts, tw)]
    defs = sorted({p for p, *_ in objs})
    with open(f"{OUT}/{name}.txt", "w") as f:
        f.write("I\n800\nDSF2TEXT\n\n")
        f.write("PROPERTY sim/planet earth\nPROPERTY sim/overlay 1\nPROPERTY sim/creation_agent HKNA_VFR_Landmarks\n")
        # without this X-Plane 11 only draws the objects at high "number of world objects";
        # 1/0 = every object (index >= 0) shows from the lowest object setting up (as WED's "Default")
        f.write("PROPERTY sim/require_object 1/0\n")
        for (w, s_, e, n), kinds in excl:
            box = f"{w - 0.00003:.7f}/{s_ - 0.00003:.7f}/{e + 0.00003:.7f}/{n + 0.00003:.7f}"
            for kind in kinds:
                f.write(f"PROPERTY sim/exclude_{kind} {box}\n")
        f.write(f"PROPERTY sim/west {tw}\nPROPERTY sim/east {tw + 1}\nPROPERTY sim/north {ts + 1}\nPROPERTY sim/south {ts}\n\n")
        for d_ in defs:
            f.write(f"OBJECT_DEF {d_}\n")
        f.write("\n")
        for p, lo, la, hdg in objs:
            f.write(f"OBJECT {defs.index(p)} {lo:.8f} {la:.8f} {hdg:.1f}\n")
    print(f"tile {name}: {len(objs)} objects, {len(excl)} exclusion boxes")


# ---------------------------------------------------------------- landmark coordinates table
def ll_centroid(pts):
    lo, la = centroid(pts)
    return la, lo


rows = {}   # section -> {name: (lat, lon, note, height_m)}


def add(section, name, lat, lon, note="", size=0.0):
    rows.setdefault(section, {})
    old = rows[section].get(name)
    if old is None or size > old[3]:
        rows[section][name] = (lat, lon, note, size)


AIP = "AIP visual references (HKNW AD 2.22)"
for f in features:
    k = f["kind"]
    la, lo = ll_centroid(f["pts"])
    if k == "britam":
        add("Towers", "Britam Tower", la, lo, "200 m", 200.0)
    elif k in ("part", "bldg") and f.get("name") and f["h"] >= 55:
        add("Towers", f["name"], la, lo, f"{f['h']:.0f} m", f["h"])
    elif k == "stadium":
        add("Stadiums", f["name"], la, lo, f"{f['h']:.0f} m stands", f["h"])
    elif k == "silo":
        add(AIP, "NCPB silos (Silos Marker, NE access lane)", la, lo, "42 m bins, 54 m tower", 54.0)
    elif k == "mast":
        add(AIP, f"KBC Ngong mast {f['h']:.0f} m (WHISKY 1)", la, lo, f"{f['h']:.0f} m", f["h"])
    elif k == "beacon":
        add(AIP, "Visual Marker beacon, Africa Nazarene Univ. (SE access lane)", la, lo, "12 m, 100 ft circle", 12.0)
    elif f.get("core"):
        add("Landmarks", f.get("rel_name") or f["name"], la, lo, f"{f['h']:.0f} m", f["h"])
    elif str(f["id"]).startswith("sem"):
        add(AIP, "St. Thomas Aquinas Seminary (MONASTERY FINAL, 3 NM RWY 07)", la, lo, "", f["h"])
for name, rings in site_bounds.items():
    la, lo = ll_centroid([p for r in rings for p in r])
    members = [f for f in features if f.get("site") == name]
    note = {"Langata Barracks": "RWY 07 final ~0.9 NM", "Kenya Defence Forces Memorial Hospital":
            "RWY 14 approach, cross >= 5900 ft"}.get(name, "")
    add(AIP, name, la, lo, f"{len(members)} buildings" + (f"; {note}" if note else ""),
        max((f["h"] for f in members), default=0.0))
add(AIP, "Ndula Marker (checkered roof, NE access lane)", *NDULA, "tile -02+037", 7.4)
add(AIP, "Twin Bridges marker (SE access lane)", *TWINB, "striped parapets", 1.2)
add(AIP, "LORESHO water tower (north arrival)", -(1 + 13 / 60 + 43.211 / 3600), 36 + 42 / 60 + 8.997 / 3600,
    "generic, 30 m", 30.5)
add(AIP, "LORESHO water tower (State House departure)", -(1 + 14 / 60 + 3 / 3600), 36 + 42 / 60 + 46 / 3600,
    "generic, 30 m", 30.5)
if turbines:
    add("Other", f"Ngong Hills wind farm ({len(turbines)} turbines, centre)",
        sum(t[1] for t in turbines) / len(turbines), sum(t[0] for t in turbines) / len(turbines), "50 m hubs", 50.0)
osm_by_id = {e["id"]: e for e in raw + parts if e["type"] == "way"}
for name, wid, h in (("KICC", 123414928, 105.0), ("Times Tower", 1436349700, 140.0), ("Hilton", 239055333, 80.0)):
    if wid in osm_by_id:
        if SIMHEAVEN_VFR:
            add("Other", f"{name} (simHeaven X-Africa's hand-made model)",
                *ll_centroid(ring(osm_by_id[wid]["geometry"])), "not modelled here", h)
        else:
            add("Towers", name, *ll_centroid(ring(osm_by_id[wid]["geometry"])), f"{h:.0f} m", h)

SECTIONS = ("Towers", "Landmarks", "Stadiums", AIP, "Other")
with open(f"{OUT}/landmarks.md", "w") as f:
    for section in SECTIONS:
        items = rows.get(section, {})
        if not items:
            continue
        f.write(f"\n### {section}\n\n| Landmark | Lat | Lon | Notes |\n|---|---|---|---|\n")
        for name, (la, lo, note, size) in sorted(items.items(), key=lambda kv: (-kv[1][3], kv[0])):
            f.write(f"| {name} | {la:.5f} | {lo:.5f} | {note} |\n")

# every landmark as plain text, tallest first: "lat lon height name [section] (notes)"
short = {"Towers": "tower", "Landmarks": "landmark", "Stadiums": "stadium", AIP: "AIP", "Other": "other"}
everything = [(size, name, la, lo, note, short[sec]) for sec in SECTIONS
              for name, (la, lo, note, size) in rows.get(sec, {}).items()]
with open(f"{OUT}/landmarks.txt", "w") as f:
    f.write("# HKNA VFR Landmarks, tallest first: lat lon height name [type] (notes)\n")
    for size, name, la, lo, note, tag in sorted(everything, key=lambda r: (-r[0], r[1])):
        extra = note if note and note != f"{size:.0f} m" else ""
        f.write(f"{la:.5f} {lo:.5f} {size:4.0f}m  {name} [{tag}]" + (f" ({extra})" if extra else "") + "\n")
print(f"objects: {len(dsf_objs)} placements, {len(dsf_excl)} exclusion boxes")
json.dump(feature_index, open(f"{OUT}/features.json", "w"))

# colour decisions, for checking against the imagery
with open(f"{OUT}/colours.json", "w") as f:
    json.dump([dict(id=str(b["id"]), name=b.get("name", ""), h=b["h"], pts=b["pts"], shift=b.get("shift"), roof_img="roof_img" in b, wall=b["wall"], wall_raw=b.get("wall_raw"), glass=b["glass"],
                    strip=b["strip"], strip_rgb=[float(c) for c in (PUNCH_C if b["strip"] < N_PUNCH else GLASS_C)
                                                 [b["strip"] % N_PUNCH if b["strip"] < N_PUNCH else b["strip"] - N_PUNCH]])
               for b in bld], f)
