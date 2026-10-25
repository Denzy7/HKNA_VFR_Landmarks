"""Georeferenced imagery for the build and the viewer, as web-mercator 256 px tiles.

Two sources (IMAGERY in build.local.sh):
  go2  - the Ortho4XP tile's own textures (ZL19 GO2, fallback ZL16): the same imagery X-Plane
         drapes on the ground, so models match it exactly.
  open - Maxar 30 cm satellite mosaics from OpenAerialMap (CC BY-NC 4.0): May 2024 where it
         covers, else May 2019. Fetched once as tiles and cached under imagery_cache/.
"""
import io, math, os, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import numpy as np


def gpx(lon, lat, z):
    """global web-mercator pixel at zoom z"""
    n = 2 ** z * 256
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


class Ortho:
    """Ortho4XP textures: each <ty>_<tx>_GO2<z>.dds is 4096 px = 16x16 tiles."""
    name = "go2"
    credit = "Ortho4XP GO2 imagery"
    lean = (0.15, 0.26)           # roof displacement per metre of height (E, N), GTC/Britam/KICC

    def __init__(self, tex_dir, prov="GO2"):
        self.dir, self.prov, self.cache = tex_dir, prov, {}

    gpx = staticmethod(gpx)

    def _block(self, fy, fx, z):
        k = (fy, fx, z)
        if k not in self.cache:
            f = f"{self.dir}/{fy}_{fx}_{self.prov}{z}.dds"
            self.cache[k] = np.asarray(Image.open(f).convert("RGB")) if os.path.exists(f) else None
            if len(self.cache) > 24:          # keep memory bounded
                self.cache.pop(next(iter(self.cache)))
        return self.cache[k]

    def tile256(self, x, y, z):
        a = self._block(y - y % 16, x - x % 16, z)
        if a is None:
            return None
        ox, oy = (x % 16) * 256, (y % 16) * 256
        return a[oy:oy + 256, ox:ox + 256]

    def prefetch(self, tiles):
        pass

    def crop(self, w, s, e, n):
        """RGB array for a lon/lat box (north-up) at ZL19 pixels; (None, None) if not covered."""
        x0, y0 = gpx(w, n, 19)
        x1, y1 = gpx(e, s, 19)
        a = crop_px(self, int(math.floor(x0)), int(math.floor(y0)), int(math.ceil(x1)), int(math.ceil(y1)))
        return (a, 19) if a is not None else (None, None)


class OpenAerialMap(Ortho):
    """Maxar mosaics on OpenAerialMap's tile server, newest first, cached on disk."""
    name = "open"
    credit = "Maxar via OpenAerialMap, CC BY-NC 4.0"
    lean = (-0.56, 0.39)          # Maxar 2024 is far off-nadir: Britam's roof sits ~0.68 m NW per m
    SOURCES = [
        ("maxar2024", "https://tiles.openaerialmap.org/6642a0266049ef00013b85d1/0/6642a0266049ef00013b85d2/{z}/{x}/{y}"),
        ("maxar2019", "https://tiles.openaerialmap.org/5ea27d04411bed00056803c5/0/5ea27d04411bed00056803c6/{z}/{x}/{y}"),
    ]
    UA = "HKNA-VFR-Landmarks/0.1 (X-Plane scenery, personal use)"

    def __init__(self, cache_dir):
        self.dir, self.mem, self.lock = cache_dir, {}, threading.Lock()

    def _path(self, x, y, z):
        return f"{self.dir}/{z}/{x}/{y}"

    def _fetch(self, x, y, z):
        """download into the cache: <y>.jpg (+ <y>.src naming the mosaic) or <y>.none"""
        p = self._path(x, y, z)
        if os.path.exists(p + ".jpg") or os.path.exists(p + ".none"):
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        for name, url in self.SOURCES:
            for k in range(4):
                try:
                    req = urllib.request.Request(url.format(z=z, x=x, y=y), headers={"User-Agent": self.UA})
                    body = urllib.request.urlopen(req, timeout=60).read()
                    with open(p + ".tmp", "wb") as f:
                        f.write(body)
                    os.replace(p + ".tmp", p + ".jpg")
                    with open(p + ".src", "w") as f:
                        f.write(name)
                    return
                except urllib.error.HTTPError as e:
                    if e.code == 404:            # outside this mosaic: try the next one
                        break
                    time.sleep(2 * (k + 1))
                except Exception:
                    time.sleep(2 * (k + 1))
            else:
                return                           # kept failing: leave uncached, retry next build
        open(p + ".none", "w").close()           # no mosaic covers it

    def prefetch(self, tiles, threads=6):
        todo = [t for t in set(tiles) if not (os.path.exists(self._path(*t) + ".jpg") or
                                              os.path.exists(self._path(*t) + ".none"))]
        if not todo:
            return
        print(f"imagery: fetching {len(todo)} OpenAerialMap tiles (cached for next time)…", flush=True)
        t0, done = time.time(), [0]

        def one(t):
            self._fetch(*t)
            with self.lock:
                done[0] += 1
                if done[0] % 200 == 0:
                    print(f"  {done[0]}/{len(todo)} ({done[0] / (time.time() - t0):.1f}/s)", flush=True)
        with ThreadPoolExecutor(threads) as ex:
            list(ex.map(one, todo))

    def tile256(self, x, y, z):
        k = (x, y, z)
        if k in self.mem:
            return self.mem[k]
        p = self._path(x, y, z)
        if not os.path.exists(p + ".jpg") and not os.path.exists(p + ".none"):
            self._fetch(x, y, z)
        a = np.asarray(Image.open(p + ".jpg").convert("RGB")) if os.path.exists(p + ".jpg") else None
        if a is not None and a.shape[:2] != (256, 256):
            a = np.asarray(Image.fromarray(a).resize((256, 256), Image.LANCZOS))
        self.mem[k] = a
        if len(self.mem) > 4000:
            self.mem.pop(next(iter(self.mem)))
        return a


def tiles_for(ix0, iy0, ix1, iy1, z=19):
    """256 px tiles covering a global pixel box at zoom z"""
    return [(tx, ty, z) for ty in range(iy0 // 256, (iy1 - 1) // 256 + 1) for tx in range(ix0 // 256, (ix1 - 1) // 256 + 1)]


def mosaic(o, ix0, iy0, ix1, iy1, z):
    """RGB array for the global pixel box at zoom z, or None if any tile is missing"""
    out = np.zeros((iy1 - iy0, ix1 - ix0, 3), np.uint8)
    for tx, ty, _ in tiles_for(ix0, iy0, ix1, iy1, z):
        a = o.tile256(tx, ty, z)
        if a is None:
            return None
        gx0, gy0 = tx * 256, ty * 256
        sx0, sy0 = max(ix0, gx0), max(iy0, gy0)
        sx1, sy1 = min(ix1, gx0 + 256), min(iy1, gy0 + 256)
        out[sy0 - iy0:sy1 - iy0, sx0 - ix0:sx1 - ix0] = a[sy0 - gy0:sy1 - gy0, sx0 - gx0:sx1 - gx0]
    return out


def crop_px(o, ix0, iy0, ix1, iy1):
    """RGB array for ZL19 global pixel box [ix0,ix1)x[iy0,iy1); falls back to ZL16 (upsampled)."""
    a = mosaic(o, ix0, iy0, ix1, iy1, 19)
    if a is not None:
        return a
    jx0, jy0, jx1, jy1 = ix0 // 8, iy0 // 8, (ix1 + 7) // 8, (iy1 + 7) // 8      # ZL16: 8x coarser
    small = mosaic(o, jx0, jy0, jx1, jy1, 16)
    if small is None:
        return None
    big = np.asarray(Image.fromarray(small).resize((small.shape[1] * 8, small.shape[0] * 8), Image.BILINEAR))
    ox, oy = ix0 - jx0 * 8, iy0 - jy0 * 8
    return big[oy:oy + (iy1 - iy0), ox:ox + (ix1 - ix0)]


def make_ortho(kind, tex_dir, cache_dir="imagery_cache"):
    if kind == "open":
        return OpenAerialMap(cache_dir)
    if kind != "go2":
        raise SystemExit(f"IMAGERY must be go2 or open, not {kind!r}")
    return Ortho(tex_dir)
