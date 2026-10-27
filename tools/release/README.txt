HKNA VFR Landmarks (open imagery build)
=======================================

X-Plane 11 overlay with 3D landmarks for VFR flying in the Nairobi region (HKNW / HKJK),
tiles -02+036 and -02+037. Source, screenshots and full notes:
https://github.com/Denzy7/HKNA_VFR_Landmarks

Install
-------
1. Unzip so that "Custom Scenery/HKNA_VFR_Landmarks/Earth nav data" exists.
2. In scenery_packs.ini, keep it above your ortho/mesh packs and any other overlays of the area.
3. The Ngong Hills wind farm uses OpenSceneryX; install it or the turbines won't show.

This build's roofs and facade colours come from Maxar satellite imagery via OpenAerialMap, not
from your own Ortho4XP tiles, so they may not match your ground exactly. For a pack that matches,
build it yourself from the source with your Ortho4XP textures (IMAGERY=go2).

It was built without simHeaven installed: KICC, Times Tower and the Hilton are modelled from OSM,
and the Ngong turbines aren't de-duplicated against simHeaven X-Africa's. If you use
simHeaven_X-Africa-1-vfr, building from source with XP_CUSTOM_SCENERY set avoids both.

landmarks.txt lists every landmark with its coordinates.

Credits and licences
--------------------
- Map data © OpenStreetMap contributors, ODbL 1.0
  (https://www.openstreetmap.org/copyright).
- Imagery © Maxar, via OpenAerialMap, CC BY-NC 4.0
  (https://creativecommons.org/licenses/by-nc/4.0/). Because of this, the pack may only be
  shared and used non-commercially, with these credits kept.
- The code that builds it is MIT-licensed; see the source repository.
