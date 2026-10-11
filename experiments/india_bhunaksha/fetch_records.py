"""Download public Bhu-Naksha (Chhattisgarh) plot reports to test roam_india.py on.

    python -X utf8 experiments/india_bhunaksha/fetch_records.py <district_index> <village_index> <khasra> [...]

The reports carry the landholder's name, so they are saved under docs/, which git ignores.
"""
import base64
import sys
from pathlib import Path

import httpx

BASE = "https://bhunaksha.cg.nic.in/"
LAYERS = "gDaO6GLMTKWd-dIT7vgtVg,m7mJsh11QauCCu-pAt0hpw,-1"  # derived layers: vertices, border lengths
OUT = Path(__file__).parent / "docs"
http = httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, timeout=120)


def village_code(district_i: int, village_i: int) -> tuple[str, str]:
    chosen = []
    for level, idx in enumerate([district_i, 0, 0, village_i]):
        codes = "".join(o["code"] + "," for o in chosen)
        lists = http.post(BASE + "rest/Levels/ListsAfterLevel", data={"state": "22", "level": str(level), "codes": codes, "hasmap": "true"}).json()
        chosen.append(lists[0][min(idx, len(lists[0]) - 1)])
    levels = "".join(o["code"] + "," for o in chosen)
    ext = http.post(BASE + "rest/MapInfo/getVVVVExtentGeoref", data={"state": "22", "gisLevels": levels, "srs": "0"}).json()
    return ext["gisCode"], chosen[0]["code"]


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    d, v, *plots = sys.argv[1:]
    gis, dist = village_code(int(d), int(v))
    for plot in plots:
        r = http.post(BASE + "rest/CGReport/cgPlotreportPDF", data={
            "state": "22", "giscode": gis, "plotno": plot, "sameownerplotreport": "false", "allplotsofvillage": "false",
            "derivedlayerids": LAYERS, "selectedlayerids": ",-1", "scaletextfield": "", "moduleid": "Report Options"})
        pdf = base64.b64decode(r.text.strip())
        if pdf[:4] != b"%PDF":
            print(plot, "no report")
            continue
        p = OUT / f"cg_{gis.replace('.', '_')}_khasra_{plot}.pdf"
        p.write_bytes(pdf)
        print(p.name, len(pdf))
