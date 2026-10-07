# -*- coding: utf-8 -*-
"""
regions.py — daftar wilayah IndeksKAR (Kalimantan Selatan)

Modul mandiri: hanya mendefinisikan WILAYAH, tidak menyentuh logika
perhitungan indeks yang sudah berjalan di backend Anda.

Dipasang berdampingan dengan dua berkas GeoJSON:

    gee_backend/
    ├── regions.py                  ← berkas ini
    ├── regions_kalsel.geojson      ← 13 batas kabupaten/kota
    └── kecamatan_kalsel.geojson    ← 147 batas kecamatan

Sumber poligon: `KAB_GEOJSON` dan `KEC_REAL_GEOJSON` yang tertanam di
dss_indekskar_kalsel.html — jadi batas di backend dijamin identik dengan
batas yang digambar di peta frontend.

CATATAN PENTING — atribut kecamatan sudah dikoreksi.
Pada data asal, kolom `KAB` untuk 45 dari 147 kecamatan salah (mis. Martapura,
Gambut, Sungai Tabuk tertulis "Kota Banjarmasin"; Tanjung tertulis "Kab. Hulu
Sungai Utara"). Di berkas ini atribut tersebut ditetapkan ulang secara
GEOMETRIS — sentroid kecamatan diuji terhadap poligon kabupaten. Properti
`kab_dikoreksi: true` menandai baris yang berubah, dan `kab_label_asli`
menyimpan label lamanya untuk penelusuran.

Pemakaian minimal di backend Flask/FastAPI Anda:

    from regions import get_region, ee_geometry, grid_spec, list_regions

    @app.get("/api/indekskar/<rid>")
    def indekskar(rid):
        reg = get_region(rid)                 # None → balas 404
        if reg is None:
            return jsonify(error=f"region '{rid}' tidak dikenal"), 404
        geom = ee_geometry(rid)               # ee.Geometry siap pakai
        spec = grid_spec(rid)                 # nx, ny, bbox, cell_deg
        ...                                   # perhitungan Anda yang sudah ada

Ketergantungan: tidak ada, kecuali `earthengine-api` bila fungsi ee_* dipakai.
Python 3.8+.
"""

from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_REGION_FILE = os.path.join(_HERE, "regions_kalsel.geojson")
_KEC_FILE = os.path.join(_HERE, "kecamatan_kalsel.geojson")
_KHDTK_FILE = os.path.join(_HERE, "khdtk_ulm.geojson")

# Wilayah administratif — 13 kabupaten/kota.
KAB_IDS: Tuple[str, ...] = (
    "banjarbaru", "banjarmasin", "banjar", "baritokuala", "tapin",
    "hss", "hst", "hsu", "tabalong", "tala", "tanbu", "kotabaru", "balangan",
)

# Wilayah khusus non-administratif. Sengaja dipisah dari KAB_IDS supaya
# TIDAK ikut pada agregasi se-provinsi — KHDTK berada di dalam Kab. Banjar,
# kalau ikut dihitung wilayahnya akan tercacah dua kali.
EXTRA_IDS: Tuple[str, ...] = ("khdtk",)

REGION_IDS: Tuple[str, ...] = KAB_IDS + EXTRA_IDS

# Grid lebih rapat untuk wilayah kecil. KHDTK hanya 1.637 ha; dengan sel
# bawaan 444 m hanya tersisa puluhan piksel, terlalu kasar untuk hutan
# pendidikan. 0,0009 derajat = 100 m.
GRID_OVERRIDE = {
    "khdtk": {"target_cells": 40, "min_deg": 0.0009},
}

# Parameter grid bawaan — disamakan dengan mode pra-hitung di frontend agar
# jumlah sel tidak berubah drastis saat sebuah wilayah naik ke mode GEE.
GRID_TARGET_CELLS = 26      # jumlah sel pada sisi terpanjang
GRID_MIN_DEG = 0.004        # ≈ 440 m
GRID_MAX_DEG = 0.025        # ≈ 2,8 km
GRID_MAX_SIDE = 48          # batas atas sel per sisi (kendali biaya komputasi)


# ─────────────────────────────────────────────────────────────────────────
# Pemuatan berkas
# ─────────────────────────────────────────────────────────────────────────

def _load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{os.path.basename(path)} tidak ditemukan di {_HERE}. "
            "Salin berkas GeoJSON berdampingan dengan regions.py."
        )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _gabung_khdtk() -> Optional[Dict[str, Any]]:
    """Empat blok KHDTK digabung jadi satu MultiPolygon = satu wilayah.
       Blok aslinya tetap tersedia lewat kecamatan("khdtk")."""
    if not os.path.exists(_KHDTK_FILE):
        return None
    fc = _load(_KHDTK_FILE)
    polys: List[Any] = []
    luas = 0.0
    for f in fc["features"]:
        g = f["geometry"]
        polys += [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        luas += f["properties"].get("hectares") or 0.0
    n, s_, w, e = -90.0, 90.0, 180.0, -180.0
    for rings in polys:
        for x, y in rings[0]:
            n = max(n, y); s_ = min(s_, y); w = min(w, x); e = max(e, x)
    return {
        "type": "Feature",
        "properties": {
            "id": "khdtk", "name": "Areal KHDTK ULM",
            "name_source": "Blok_dalam_KHDTK", "hectares": round(luas, 1),
            "bbox": [round(w, 6), round(s_, 6), round(e, 6), round(n, 6)],
            "spi3_arsip": None,
            "induk": "Kab. Banjar",     # untuk meminjam nilai skala kabupaten
            "catatan": "Kawasan Hutan Dengan Tujuan Khusus ULM, 4 blok, "
                       "berada di dalam Kab. Banjar.",
        },
        "geometry": {"type": "MultiPolygon", "coordinates": polys},
    }


@lru_cache(maxsize=1)
def _regions() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for feat in _load(_REGION_FILE)["features"]:
        out[feat["properties"]["id"]] = feat
    kh = _gabung_khdtk()
    if kh:
        out["khdtk"] = kh
    missing = [r for r in REGION_IDS if r not in out]
    if missing:
        raise ValueError(f"region hilang dari GeoJSON: {missing}")
    return out


@lru_cache(maxsize=1)
def _kecamatan() -> List[Dict[str, Any]]:
    return _load(_KEC_FILE)["features"]


# ─────────────────────────────────────────────────────────────────────────
# API utama
# ─────────────────────────────────────────────────────────────────────────

def list_regions() -> List[Dict[str, Any]]:
    """Ringkasan seluruh wilayah — cocok untuk endpoint /api/regions."""
    reg = _regions()
    out = []
    for rid in REGION_IDS:
        p = reg[rid]["properties"]
        out.append({
            "id": rid,
            "name": p["name"],
            "bbox": p["bbox"],
            "hectares": p.get("hectares"),
            "kecamatan_count": len(kecamatan(rid)),
        })
    return out


def get_region(rid: str) -> Optional[Dict[str, Any]]:
    """Fitur GeoJSON satu wilayah, atau None bila id tidak dikenal."""
    return _regions().get(rid)


def region_name(rid: str) -> Optional[str]:
    reg = get_region(rid)
    return reg["properties"]["name"] if reg else None


def bbox(rid: str) -> Optional[List[float]]:
    """[w, s, e, n] dalam derajat."""
    reg = get_region(rid)
    return list(reg["properties"]["bbox"]) if reg else None


def boundary_coords(rid: str) -> Optional[List[Any]]:
    """Koordinat poligon apa adanya — bentuknya sudah sesuai dengan yang
    diharapkan frontend pada field `boundary` (daftar poligon → cincin → [lng, lat])."""
    reg = get_region(rid)
    if not reg:
        return None
    geom = reg["geometry"]
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


@lru_cache(maxsize=1)
def _khdtk_blok() -> List[Dict[str, Any]]:
    """Blok KHDTK disajikan dengan bentuk properti yang SAMA seperti kecamatan,
       sehingga seluruh alur hilir (tabel rincian, riwayat, skoring) berlaku
       tanpa perubahan."""
    if not os.path.exists(_KHDTK_FILE):
        return []
    out = []
    for f in _load(_KHDTK_FILE)["features"]:
        p = f["properties"]
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        pts = [pt for rings in polys for pt in rings[0]]
        cx = sum(q[0] for q in pts) / len(pts)
        cy = sum(q[1] for q in pts) / len(pts)
        out.append({
            "type": "Feature",
            "properties": {
                "nama_kec": "Blok " + str(p.get("blok")),
                "kab_id": "khdtk", "kab_name": "Areal KHDTK ULM",
                "kab_label_asli": p.get("kawasan"), "kab_dikoreksi": False,
                "cx": round(cx, 4), "cy": round(cy, 4),
                "hectares": p.get("hectares"), "dta": p.get("dta"),
                "sub_das": p.get("sub_das"),
                "ndmi_arsip": None, "lst_arsip": None, "peat_pct": None,
            },
            "geometry": g,
        })
    return out


def kecamatan(rid: str) -> List[Dict[str, Any]]:
    """Unit rincian di dalam wilayah.
       Untuk kabupaten/kota → kecamatan (atribut sudah dikoreksi geometris).
       Untuk KHDTK          → blok kawasan."""
    if rid == "khdtk":
        return _khdtk_blok()
    return [f for f in _kecamatan() if f["properties"]["kab_id"] == rid]


def grid_spec(rid: str,
              target_cells: int = None,
              min_deg: float = None,
              max_deg: float = GRID_MAX_DEG,
              max_side: int = GRID_MAX_SIDE) -> Optional[Dict[str, Any]]:
    """Rencana grid untuk satu wilayah.

    Mengembalikan nx, ny, cell_deg, cell_m, dan bbox — nilai yang langsung
    dapat dipakai membangun respons `nx`, `ny`, `bbox` untuk frontend.
    Ukuran sel adaptif: wilayah kecil dapat grid rapat, wilayah luas tidak
    meledak jumlah selnya.
    """
    bb = bbox(rid)
    if bb is None:
        return None
    ov = GRID_OVERRIDE.get(rid, {})
    if target_cells is None:
        target_cells = ov.get("target_cells", GRID_TARGET_CELLS)
    if min_deg is None:
        min_deg = ov.get("min_deg", GRID_MIN_DEG)
    w, s, e, n = bb
    span = max(e - w, n - s)
    cell = max(min_deg, min(max_deg, span / float(target_cells)))
    nx = max(6, min(max_side, round((e - w) / cell)))
    ny = max(6, min(max_side, round((n - s) / cell)))
    return {
        "nx": int(nx), "ny": int(ny),
        "cell_deg": round(cell, 5),
        "cell_m": int(round(cell * 111_000)),
        "bbox": {"w": w, "s": s, "e": e, "n": n},
        "cells_total": int(nx) * int(ny),
    }


def pixel_centers(rid: str, **kw):
    """Generator (row, col, lat, lng) untuk pusat tiap sel grid.

    Belum dipotong batas wilayah — pemotongan sebaiknya dilakukan di sisi
    Earth Engine (`.clip(geom)`) atau lewat `ee.Geometry.contains`.
    """
    spec = grid_spec(rid, **kw)
    if not spec:
        return
    bb = spec["bbox"]
    d_lat = (bb["n"] - bb["s"]) / spec["ny"]
    d_lng = (bb["e"] - bb["w"]) / spec["nx"]
    for r in range(spec["ny"]):
        for c in range(spec["nx"]):
            yield r, c, bb["n"] - (r + 0.5) * d_lat, bb["w"] + (c + 0.5) * d_lng


# ─────────────────────────────────────────────────────────────────────────
# Jembatan Earth Engine (impor malas — modul tetap bisa dipakai tanpa EE)
# ─────────────────────────────────────────────────────────────────────────

def _ee():
    try:
        import ee  # type: ignore
    except ImportError as exc:                              # pragma: no cover
        raise ImportError(
            "earthengine-api belum terpasang. Jalankan: pip install earthengine-api"
        ) from exc
    return ee


def ee_geometry(rid: str, simplify_m: Optional[float] = None):
    """ee.Geometry batas wilayah.

    `simplify_m` — toleransi penyederhanaan dalam meter. Berguna untuk
    Kab. Kotabaru yang punya ±27.000 simpul karena banyak pulau; nilai
    100–500 m biasanya sudah memangkas beban tanpa menggeser batas berarti.
    """
    reg = get_region(rid)
    if not reg:
        return None
    ee = _ee()
    geom = ee.Geometry(reg["geometry"], None, False)   # geodesic=False → planar, cepat
    return geom.simplify(simplify_m) if simplify_m else geom


def ee_kecamatan_fc(rid: str):
    """ee.FeatureCollection kecamatan di wilayah tersebut.

    Properti tiap fitur: nama_kec, kab_id, kab_name, peat_pct,
    ndmi_arsip, lst_arsip (dua terakhir hanya rujukan arsip 2025 —
    jangan dipakai bila nilai GEE real-time tersedia).
    """
    ee = _ee()
    feats = [
        ee.Feature(ee.Geometry(f["geometry"], None, False), f["properties"])
        for f in kecamatan(rid)
    ]
    return ee.FeatureCollection(feats)


def ee_grid_fc(rid: str, clip_to_region: bool = True, **kw):
    """Grid sel sebagai ee.FeatureCollection persegi, siap untuk reduceRegions().

    Tiap fitur membawa properti `r` dan `c` sehingga hasil reduksi dapat
    dipetakan kembali ke posisi sel di frontend.
    """
    ee = _ee()
    spec = grid_spec(rid, **kw)
    if not spec:
        return None
    bb = spec["bbox"]
    d_lat = (bb["n"] - bb["s"]) / spec["ny"]
    d_lng = (bb["e"] - bb["w"]) / spec["nx"]
    feats = []
    for r in range(spec["ny"]):
        for c in range(spec["nx"]):
            s_ = bb["n"] - (r + 1) * d_lat
            n_ = bb["n"] - r * d_lat
            w_ = bb["w"] + c * d_lng
            e_ = bb["w"] + (c + 1) * d_lng
            feats.append(ee.Feature(
                ee.Geometry.Rectangle([w_, s_, e_, n_], None, False),
                {"r": r, "c": c},
            ))
    fc = ee.FeatureCollection(feats)
    if clip_to_region:
        fc = fc.filterBounds(ee_geometry(rid))
    return fc


# ─────────────────────────────────────────────────────────────────────────
# Pemeriksaan mandiri:  python regions.py
# ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"{'id':<13}{'nama':<26}{'grid':>9}{'sel':>7}{'ukuran sel':>13}{'kec':>6}")
    print("─" * 74)
    total = 0
    for row in list_regions():
        g = grid_spec(row["id"])
        total += g["cells_total"]
        print(f"{row['id']:<13}{row['name']:<26}"
              f"{g['nx']}×{g['ny']:<6}{g['cells_total']:>6}"
              f"{g['cell_m']:>9} m {row['kecamatan_count']:>6}")
    print("─" * 74)
    print(f"{'TOTAL':<39}{total:>6} sel")

    fixed = [f["properties"] for f in _kecamatan() if f["properties"]["kab_dikoreksi"]]
    print(f"\nKecamatan dengan atribut KAB dikoreksi geometris: "
          f"{len(fixed)} dari {len(_kecamatan())}")
    for p in fixed[:8]:
        print(f"   {p['nama_kec']:<18}{p['kab_label_asli']:<26}→ {p['kab_name']}")
    if len(fixed) > 8:
        print(f"   … dan {len(fixed) - 8} lainnya")
