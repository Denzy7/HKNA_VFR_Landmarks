#!/usr/bin/env python3
"""Local server for the viewer: static files from the repo root, plus
  GET  /api/removed            -> {"ids": [...], "notes": {id: note}}   (source/removed.txt)
  POST /api/removed            <- {"ids": [...], "notes": {...}}        rewrites the list
  GET  /api/overrides          -> {id: {...}}                           (source/overrides.json)
  POST /api/overrides          <- {"id": id, "spec": {...} | null}      sets / clears one building
  POST /api/photo              <- {"id", "name", "data" (data: URL), "pose": {...}}  adds your photo
  POST /api/photo/pose         <- {"id", "file", "pose": {...}}         saves a tuned camera pose
  POST /api/photo/delete       <- {"id", "file"}                        drops a photo (and your file)
  POST /api/build              -> starts ./build.sh in the background
  GET  /api/build/status?since=N -> {"state", "stage", "stages", "lines": [...], "next": N}

Usage: serve.py <port>   (run from the repo root; view.sh does this). Binds 127.0.0.1 only.
"""
import base64, http.server, json, os, re, subprocess, sys, threading, time, urllib.parse

REMOVED = "source/removed.txt"
OVERRIDES = "source/overrides.json"
PHOTOS = "photos"
lock = threading.Lock()

# build progress: each stage starts at a line ./build.sh prints when it begins (python runs -u)
STAGES = [
    ("osm", "OpenStreetMap data", None),
    ("features", "Buildings, specs and your edits", re.compile(r"^imagery: (go2|open)")),
    ("imagery", "Colours and roofs from imagery", re.compile(r"^features: ")),
    ("objects", "3D objects", re.compile(r"^atlas: ")),
    ("dsf", "Scenery tiles (DSF)", re.compile(r"^objects: \d+ placements")),
    ("viewer", "Viewer data", re.compile(r"^done: ")),
]
build = dict(state="idle", stage=None, lines=[], started=None, finished=None)


def read_removed():
    ids, notes, header = [], {}, []
    if os.path.exists(REMOVED):
        for line in open(REMOVED):
            body, _, note = line.rstrip("\n").partition("#")
            body = body.strip()
            if not body:
                if not ids:
                    header.append(line.rstrip("\n"))
                continue
            ids.append(body)
            if note.strip():
                notes[body] = note.strip()
    return ids, notes, header


def write_removed(ids, notes):
    _, _, header = read_removed()
    with open(REMOVED + ".tmp", "w") as f:
        for line in header:
            f.write(line + "\n")
        for i in ids:
            note = notes.get(i, "")
            f.write(f"{i}  # {note}\n" if note else f"{i}\n")
    os.replace(REMOVED + ".tmp", REMOVED)


def read_json(path, default):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    with open(path + ".tmp", "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(path + ".tmp", path)


def safe_id(i):
    i = str(i)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", i):
        raise ValueError("bad building id")
    return i


def run_build():
    build.update(state="running", stage=STAGES[0][0], lines=[], started=time.time(), finished=None)
    p = subprocess.Popen(["./build.sh"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        line = line.rstrip("\n")
        with lock:
            build["lines"].append(line)
            for key, _, rx in STAGES:
                if rx and rx.search(line):
                    build["stage"] = key
    p.wait()
    with lock:
        build["state"] = "done" if p.returncode == 0 else "failed"
        build["finished"] = time.time()


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        line = str(args[0]) if args else ""     # request line, or an HTTPStatus from log_error
        if "/api/" in line and "/status" not in line:
            super().log_message(fmt, *args)

    def reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/removed":
            ids, notes, _ = read_removed()
            return self.reply(dict(ids=ids, notes=notes))
        if u.path == "/api/overrides":
            return self.reply(read_json(OVERRIDES, {}))
        if u.path == "/api/build/status":
            since = int(urllib.parse.parse_qs(u.query).get("since", ["0"])[0])
            with lock:
                return self.reply(dict(state=build["state"], stage=build["stage"],
                                       stages=[(k, label) for k, label, _ in STAGES],
                                       lines=build["lines"][since:], next=len(build["lines"]),
                                       elapsed=round((build["finished"] or time.time()) - build["started"], 1)
                                       if build["started"] else 0))
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/removed":
                ids = [str(i) for i in req.get("ids", [])]
                write_removed(list(dict.fromkeys(ids)), {str(k): str(v) for k, v in req.get("notes", {}).items()})
                return self.reply(dict(ok=True, n=len(ids)))
            if path == "/api/overrides":
                i = safe_id(req["id"])
                ov = read_json(OVERRIDES, {})
                if req.get("spec"):
                    ov[i] = req["spec"]
                else:
                    ov.pop(i, None)
                write_json(OVERRIDES, ov)
                return self.reply(dict(ok=True))
            if path == "/api/photo":
                i = safe_id(req["id"])
                head, _, b64 = req["data"].partition(",")
                ext = "png" if "png" in head else "jpg"
                os.makedirs(f"{PHOTOS}/user", exist_ok=True)
                file = f"user/{i}_{int(time.time() * 1000)}.{ext}"
                with open(f"{PHOTOS}/{file}", "wb") as f:
                    f.write(base64.b64decode(b64))
                rec_path = f"{PHOTOS}/{i}.json"
                rec = read_json(rec_path, dict(name=req.get("name", ""), ids=[i], photos=[]))
                pose = req.get("pose", {})
                rec["photos"].insert(0, dict(src="Your photo", file=file, title=req.get("title", ""), license="",
                                             author="you", page=pose.get("url", ""), match="user",
                                             date=time.strftime("%Y-%m-%d"), **{k: pose.get(k) for k in
                                             ("lat", "lon", "heading", "pitch", "fov", "fov_axis", "eye", "trim_top", "trim_bottom")}))
                write_json(rec_path, rec)
                return self.reply(dict(ok=True, file=file))
            if path == "/api/photo/pose":
                i = safe_id(req["id"])
                rec_path = f"{PHOTOS}/{i}.json"
                rec = read_json(rec_path, None)
                if not rec:
                    return self.reply(dict(ok=False, error="no photos for this building"), 404)
                for p in rec["photos"]:
                    if p["file"] == req["file"]:
                        p.update({k: v for k, v in req.get("pose", {}).items() if k in
                                  ("lat", "lon", "heading", "pitch", "fov", "fov_axis", "eye", "trim_top", "trim_bottom")})
                        p["pose_matched"] = True
                write_json(rec_path, rec)
                return self.reply(dict(ok=True))
            if path == "/api/photo/delete":
                i = safe_id(req["id"])
                rec_path = f"{PHOTOS}/{i}.json"
                rec = read_json(rec_path, None)
                if not rec or not any(p["file"] == req["file"] for p in rec["photos"]):
                    return self.reply(dict(ok=False, error="no such photo for this building"), 404)
                rec["photos"] = [p for p in rec["photos"] if p["file"] != req["file"]]
                write_json(rec_path, rec)
                f = os.path.normpath(f"{PHOTOS}/{req['file']}")
                if f.startswith(f"{PHOTOS}/user/") and os.path.isfile(f):
                    os.remove(f)       # your upload; fetched open-licence images stay in the cache
                return self.reply(dict(ok=True, left=len(rec["photos"])))
            if path == "/api/build":
                with lock:
                    if build["state"] == "running":
                        return self.reply(dict(ok=False, error="a build is already running"), 409)
                    build["state"] = "running"
                threading.Thread(target=run_build, daemon=True).start()
                return self.reply(dict(ok=True))
        except (KeyError, ValueError) as e:
            return self.reply(dict(ok=False, error=str(e)), 400)
        self.send_error(404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"serving on http://127.0.0.1:{port}")
    srv.serve_forever()
