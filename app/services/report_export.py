"""
Exports of a report model (app/services/report.py): GeoJSON, KML, Shapefile (WGS84 and the state-plane
zone), attribute / vertex / call CSVs, the PDF report, and a ZIP package holding all of them plus the
figures, metadata and a README -- the deliverable set a GIS client receives.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pymupdf
import shapefile
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pyproj import CRS, Transformer

from app.services import static_map
from app.services.report import attribute_row

_TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_CSS = """
* { font-family: sans-serif; }
body { font-size: 9pt; color: #1f2328; }
h1 { font-size: 20pt; margin: 4pt 0 12pt 0; color: #111; }
h2 { font-size: 12.5pt; margin: 16pt 0 6pt 0; color: #c2410c; border-bottom: 1px solid #e5e7eb; }
h2.parcel, h2.newpage { page-break-before: always; }
.kicker { font-size: 8pt; color: #c2410c; letter-spacing: 2pt; margin: 0; }
.caption { font-size: 7.5pt; color: #6b7280; margin: 2pt 0 6pt 0; }
.sub { font-size: 8.5pt; font-weight: bold; margin: 8pt 0 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 2pt 0 6pt 0; }
.kvblock { margin: 2pt 0 8pt 0; }
p.kv { margin: 0 0 2.5pt 0; }
p.kv b { color: #4b5563; }
table.grid th { background: #f3f4f6; text-align: left; padding: 3pt; border: 1px solid #d1d5db; font-size: 8pt; }
table.grid td { padding: 2.5pt 3pt; border: 1px solid #e5e7eb; }
table.small td, table.small th { font-size: 7pt; }
td.ok { color: #15803d; font-weight: bold; }
td.warn { color: #b45309; font-weight: bold; }
.foot { font-size: 7pt; color: #9ca3af; margin-top: 14pt; }
"""


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=select_autoescape(["html"]))


def figures(report: dict) -> dict[str, bytes]:
    rings = [p["ring"] for p in report["parcels"]]
    if not rings:
        return {}
    return {
        "parcels.png": static_map.render(rings, [p["label"] for p in report["parcels"]], width=1400, height=1000),
        "location.png": static_map.render(
            rings, zoom_out=5, marker_only=True, place_labels=True, width=1400, height=800
        ),
    }


# ---------------------------------------------------------------- vector formats

def geojson(report: dict) -> bytes:
    feats = [
        {"type": "Feature", "properties": attribute_row(report, p),
         "geometry": {"type": "Polygon", "coordinates": [p["ring"]]}}
        for p in report["parcels"]
    ]
    return json.dumps({"type": "FeatureCollection", "name": "roam_parcels", "features": feats}, indent=1).encode()


def kml(report: dict) -> bytes:
    marks = []
    for i, p in enumerate(report["parcels"]):
        row = attribute_row(report, p)
        colour = static_map.COLOURS[i % len(static_map.COLOURS)].lstrip("#")
        abgr = "ff" + colour[4:6] + colour[2:4] + colour[0:2]
        data = "".join(f'<Data name="{escape(k)}"><value>{escape(str(v if v is not None else ""))}</value></Data>' for k, v in row.items())
        coords = " ".join(f"{lon:.9f},{lat:.9f},0" for lon, lat in p["ring"])
        marks.append(
            f"<Placemark><name>{escape(p['label'])}</name><Style><LineStyle><color>{abgr}</color><width>3</width></LineStyle>"
            f"<PolyStyle><color>40{abgr[2:]}</color></PolyStyle></Style><ExtendedData>{data}</ExtendedData>"
            f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{coords}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
        )
    doc = (
        '<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(report['project']['title'])}</name>{''.join(marks)}</Document></kml>"
    )
    return doc.encode()


def _crs_from_label(label: str | None) -> CRS | None:
    if not label or "EPSG:" not in label:
        return None
    return CRS.from_epsg(int(label.rsplit("EPSG:", 1)[1].rstrip(")")))


_NUMERIC_FIELDS = {"StatedSF", "CalcSF", "CalcAcres", "AreaDiffPc", "PerimFt", "ClosureFt", "SrcPage"}


def shapefile_zip(report: dict, crs_label: str | None = None) -> bytes:
    """A zipped shapefile (shp/shx/dbf/prj/cpg) in WGS84, or in `crs_label`'s EPSG (e.g. the state plane)."""

    target = _crs_from_label(crs_label)
    to = Transformer.from_crs("EPSG:4326", target, always_xy=True) if target else None
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    rows = [attribute_row(report, p) for p in report["parcels"]]
    for name in (rows[0] if rows else {"ParcelID": ""}):
        if name in _NUMERIC_FIELDS:
            w.field(name[:10], "N", 18, 3)
        else:
            w.field(name[:10], "C", 254)
    for p, row in zip(report["parcels"], rows):
        ring = [to.transform(x, y) if to else (x, y) for x, y in p["ring"]]
        # shapefile outer rings are clockwise
        area2 = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))
        if area2 > 0:
            ring = ring[::-1]
        w.poly([ring])
        w.record(*[(v if v is not None else None) if k in _NUMERIC_FIELDS else ("" if v is None else str(v)) for k, v in row.items()])
    w.close()
    prj = (target or CRS.from_epsg(4326)).to_wkt("WKT1_ESRI")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("parcels.shp", shp.getvalue())
        z.writestr("parcels.shx", shx.getvalue())
        z.writestr("parcels.dbf", dbf.getvalue())
        z.writestr("parcels.prj", prj)
        z.writestr("parcels.cpg", "UTF-8")
    return out.getvalue()


def _csv(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def attributes_csv(report: dict) -> bytes:
    return _csv([attribute_row(report, p) for p in report["parcels"]])


def vertices_csv(report: dict) -> bytes:
    return _csv([{"ParcelID": p["id"], "Label": p["label"], **v} for p in report["parcels"] for v in p["vertices"]])


def calls_csv(report: dict) -> bytes:
    return _csv([
        {"ParcelID": p["id"], "Label": p["label"], **{k: c.get(k) for k in ("course", "type", "bearing", "distance", "radius", "delta")}}
        for p in report["parcels"] for c in p["calls"]
    ])


# ---------------------------------------------------------------- PDF

def _jpeg(png: bytes) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(out, format="JPEG", quality=82, optimize=True)
    return out.getvalue()


def pdf(report: dict, figs: dict[str, bytes] | None = None) -> bytes:
    figs = figs if figs is not None else figures(report)
    html = _env().get_template("report.html").render(r=report)
    # whitespace between tags would become empty table rows in the Story layout
    html = re.sub(r">\s+<", "><", html)
    archive = pymupdf.Archive()
    for name, data in figs.items():
        archive.add(_jpeg(data), name)
    story = pymupdf.Story(html=html, user_css=_CSS, archive=archive)
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    page = pymupdf.paper_rect("letter")
    where = page + (40, 46, -40, -46)
    n = 0
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        n += 1
        if n > 400:
            break
    writer.close()
    # page numbers and a running header
    doc = pymupdf.open("pdf", out.getvalue())
    title = report["project"]["title"][:90]
    for i, pg in enumerate(doc, 1):
        pg.insert_text((40, 30), f"ROAM | {title}", fontsize=7, color=(0.55, 0.55, 0.55))
        pg.insert_text((pg.rect.width - 90, pg.rect.height - 22), f"Page {i} of {len(doc)}", fontsize=7, color=(0.55, 0.55, 0.55))
    return doc.tobytes(deflate=True)


# ---------------------------------------------------------------- package

def metadata(report: dict) -> dict:
    """ISO 19115-style essentials, as JSON."""

    return {
        "title": report["project"]["title"],
        "abstract": f"{report['summary']['parcel_count']} parcel(s) digitised and georeferenced from "
                    f"{report['project']['source_document']}.",
        "date": report["generated_at"],
        "point_of_contact": report["project"]["prepared_by"],
        "client": report["project"]["client"],
        "reference_systems": [v for v in (report["crs"]["geographic"], report["crs"]["state_plane"], report["crs"]["utm"]) if v],
        "extent_wgs84": {"centre": report["location"]["centre"]},
        "lineage": report["lineage"],
        "positional_accuracy": report["accuracy_statement"],
        "data_quality": [
            {"parcel": p["id"], "location": "confirmed" if p["location_confirmed"] else "approximate",
             "method": p["placement_label"], "area_diff_pct": p["area_diff_pct"], "checks": p["qa"]}
            for p in report["parcels"]
        ],
        "attribute_fields": list(attribute_row(report, report["parcels"][0]).keys()) if report["parcels"] else [],
    }


_README = """ROAM parcel deliverable
=======================

{title}
Generated {date}

report.pdf                 Report: summary, location map, parcels over imagery, parcel schedule,
                           georeferencing evidence, per-parcel calls / coordinates / QA, accuracy statement.
parcels.geojson            Parcels with attributes, WGS 84 (EPSG:4326).
parcels.kml                Same, for Google Earth.
shapefile_wgs84/           Esri shapefile, WGS 84.
{sp_line}attributes.csv             Attribute table (one row per parcel).
vertices.csv               Every vertex: lat/lon{sp_cols}, UTM.
calls.csv                  Record boundary calls as read from the source drawing.
figures/                   The report's map figures (PNG).
metadata.json              Reference systems, lineage, positional accuracy, data quality per parcel.

Locations marked "approximate" were not independently corroborated -- verify before boundary use.
"""


def package(report: dict) -> bytes:
    figs = figures(report)
    sp = report["crs"]["state_plane"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.pdf", pdf(report, figs))
        z.writestr("parcels.geojson", geojson(report))
        z.writestr("parcels.kml", kml(report))
        folders = [("shapefile_wgs84", None)] + ([("shapefile_stateplane", sp)] if sp else [])
        for folder, label in folders:
            inner = zipfile.ZipFile(io.BytesIO(shapefile_zip(report, label)))
            for name in inner.namelist():
                z.writestr(f"{folder}/{name}", inner.read(name))
        z.writestr("attributes.csv", attributes_csv(report))
        z.writestr("vertices.csv", vertices_csv(report))
        z.writestr("calls.csv", calls_csv(report))
        for name, data in figs.items():
            z.writestr(f"figures/{name}", data)
        z.writestr("metadata.json", json.dumps(metadata(report), indent=1))
        z.writestr("README.txt", _README.format(
            title=report["project"]["title"], date=report["generated_at"],
            sp_line=f"shapefile_stateplane/      Esri shapefile, {sp}.\n" if sp else "",
            sp_cols=", state plane" if sp else "",
        ))
    return out.getvalue()
