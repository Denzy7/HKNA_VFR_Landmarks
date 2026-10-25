#!/usr/bin/env python3
"""Place the user aircraft at a lat/lon in a running X-Plane 11 (UDP port 49000).

Usage: xpplace.py [--physics] <lat> <lon> [alt_ft_msl=6500] [heading_true=0] [speed_kt=120] [host=127.0.0.1]
       xpplace.py [--physics] <preset> [host]    # AIP HKNW AD 2.22 visual reference, e.g. silos
       xpplace.py list                          # show the presets

Default: instant VEHX teleport. VEHX hands the aircraft to "external control" (physics
off); the script then asks for physics back (override_planepath[0]=0) and sets the speed
along the heading. Fast, but the flight model may stay frozen until X-Plane takes it back.
--physics (alias --reinit): PREL with start type loc_specify_lle ("used by maps"), a normal flight re-init at
the position, heading and airspeed with physics running. Slower (X-Plane reloads the flight).

En-route presets are >= ~900 ft above the highest terrain within 3 km (SRTM) and head for
HKNW (statehouse heads outbound). The approach presets (monastery, langata, kdf) sit on
the approach path instead: 300-900 ft above the ground, slower, on the final course.

"""
import math, socket, struct, sys, time

PRESETS = {  # name: lat, lon, alt_ft, heading_true, speed_kt, description
    "silos": (-1.305556, 36.886111, 6500, 235, 120, "NCPB Silos Marker, NE access lane (011820S 0365310E)"),
    "ndula": (-1.03245, 37.24777, 6000, 236, 120, "Ndula Marker, checkered roof, NE access lane"),
    "beacon": (-1.40000, 36.78928, 7000, 18, 120, "Visual Marker beacon, Africa Nazarene Univ., SE access lane"),
    "twinbridges": (-1.57373, 36.92896, 6500, 335, 120, "Twin Bridges marker, SE access lane"),
    "whisky1": (-1.32576, 36.67487, 7500, 88, 120, "KBC Ngong masts (WHISKY 1)"),
    "loresho": (-1.22867, 36.70250, 7500, 129, 120, "LORESHO water tower, north arrival"),
    "statehouse": (-1.23417, 36.71278, 7500, 310, 120, "LORESHO water tower, State House departure (outbound)"),
    "monastery": (-1.34877, 36.75780, 6500, 65, 90, "MONASTERY FINAL, St. Thomas Aquinas Seminary, 3 NM RWY 07"),
    "langata": (-1.33252, 36.79274, 6000, 64, 80, "Langata Barracks, RWY 07 final ~0.9 NM"),
    "kdf": (-1.31064, 36.80454, 6500, 137, 90, "KDF Memorial Hospital, RWY 14 approach (cross >= 5900 ft)"),
}
LOC_SPECIFY_LLE = 6

a = sys.argv[1:]
FLAGS = ("--physics", "--reinit")
reinit = any(x in FLAGS for x in a)
a = [x for x in a if x not in FLAGS]
if not a or a[0] in ("-h", "--help"):
    sys.exit(__doc__)
if a[0] == "list":
    for k, (la, lo, alt, h, v, desc) in PRESETS.items():
        print(f"{k:12s} {la:9.5f} {lo:9.5f} {alt:5d} ft {h:03d}T {v:3d} kt  {desc}")
    sys.exit()
if a[0] in PRESETS:
    lat, lon, alt_ft, hdg, kt, _ = PRESETS[a[0]]
    host = a[1] if len(a) > 1 else "127.0.0.1"
else:
    lat, lon = float(a[0]), float(a[1])
    alt_ft = float(a[2]) if len(a) > 2 else 6500
    hdg = float(a[3]) if len(a) > 3 else 0
    kt = float(a[4]) if len(a) > 4 else 120
    host = a[5] if len(a) > 5 else "127.0.0.1"

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send(msg):
    sock.sendto(msg, (host, 49000))


def dref(name, value):
    send(b"DREF\0" + struct.pack("<f", value) + name.encode().ljust(500, b"\0"))


if reinit:
    # PREL_struct: type_start, p_idx, apt_id[8], apt_rwy_idx, apt_rwy_dir,
    #              lat_deg, lon_deg, ele_mtr, psi_tru, spd_msc (doubles); airport fields unused
    send(b"PREL\0" + struct.pack("<ii8sii5d", LOC_SPECIFY_LLE, 0, b"", 0, 0,
                                 lat, lon, alt_ft * 0.3048, hdg, kt * 0.514444))
    how = "re-init (PREL)"
else:
    # VEHX: aircraft index (0 = user), lat, lon, elevation m MSL (doubles), true heading, pitch, roll (floats)
    send(b"VEHX\0" + struct.pack("<iddd3f", 0, lat, lon, alt_ft * 0.3048, hdg, 0.0, 0.0))
    time.sleep(0.3)
    dref("sim/operation/override/override_planepath[0]", 0)   # physics back on
    v = kt * 0.514444
    dref("sim/flightmodel/position/local_vx", v * math.sin(math.radians(hdg)))
    dref("sim/flightmodel/position/local_vy", 0.0)
    dref("sim/flightmodel/position/local_vz", -v * math.cos(math.radians(hdg)))
    how = "teleport (VEHX)"
print(f"{how}: {lat:.6f}, {lon:.6f}, {alt_ft:.0f} ft MSL, heading {hdg:.0f} T, {kt:.0f} kt -> {host}:49000")
