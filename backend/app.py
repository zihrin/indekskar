# -*- coding: utf-8 -*-
"""
IndeksKAR — Backend Google Earth Engine (real-time, per-piksel)
================================================================
FastAPI + earthengine-api. Menghitung nilai NYATA per sel grid untuk Kota
Banjarbaru dari koleksi GEE, menghitung dimensi K/T/P IndeksKAR, dan
menyajikannya sebagai JSON yang dikonsumsi langsung oleh dss_kebakaran_kalsel.html.

Mekanisme temporal-updating:
  Setiap sumber punya jendela `revisit` (hari). Hasil per sumber di-cache.
  Sumber hanya dihitung ulang ke GEE bila umur cache melebihi revisit-nya
  (atau bila diminta paksa via ?force=1). Ini persis konsep yang Anda minta:
  selama data tersimpan masih dalam jendela revisit → pakai cache; bila lewat →
  akuisisi baru dari GEE.

Jalankan:
  pip install -r requirements.txt
  export EE_SERVICE_ACCOUNT="....@....iam.gserviceaccount.com"
  export EE_KEY_FILE="/path/service-account-key.json"
  export EE_PROJECT="your-gcp-project-id"
  uvicorn app:app --host 0.0.0.0 --port 8000

Endpoint:
  GET /api/regions                        -> daftar wilayah yang dilayani
  GET /api/indekskar/{region}             -> pakai cache bila masih segar
  GET /api/indekskar/{region}?force=1     -> paksa akuisisi ulang semua sumber
  GET /api/provinsi                       -> reaksi cepat per kecamatan se-Kalsel
  GET /health

Wilayah:
  Definisi wilayah dipindahkan ke modul `regions.py` (13 kabupaten/kota Kalsel).
  Grid Kota Banjarbaru DIPATOK pada 42x31 seperti semula agar cache GCS lama
  tetap sahih; wilayah lain memakai grid adaptif dari regions.grid_spec().
"""
import os, io, json, math, datetime as dt
import urllib.request
from typing import Dict, Any, List
import ee
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ─────────────────────────────────────────────────────────────
# Konfigurasi wilayah & grid (SAMA dengan frontend agar konsisten)
# ─────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))

from functools import lru_cache
import regions as REG          # regions.py + regions_kalsel.geojson + kecamatan_kalsel.geojson

# Grid Kota Banjarbaru DIPATOK pada nilai yang sudah berjalan sejak awal.
# Alasannya: cache GCS lama dikunci pada bbox & jumlah sel ini. Kalau diubah,
# seluruh cache Banjarbaru jadi tidak sebanding dan tampilan ikut berubah.
# Wilayah baru memakai grid adaptif dari regions.grid_spec().
REGION_PINNED = {
    "banjarbaru": {"bbox": {"s": -3.5701, "n": -3.3735, "w": 114.6581, "e": 114.9224},
                   "nx": 42, "ny": 31},
}

def _load_json(fname):
    try:
        return json.load(open(os.path.join(HERE, fname), encoding="utf-8"))
    except Exception:
        return None

def _boundary_of(rid):
    """Poligon batas. Berkas lama Banjarbaru tetap diutamakan bila ada."""
    if rid == "banjarbaru":
        b = _load_json("banjarbaru_boundary.json")
        if b:
            return b
    return REG.boundary_coords(rid)

def _kecamatan_of(rid):
    """Kecamatan dalam bentuk {name, polys} — sama seperti yang dipakai kode lama."""
    if rid == "banjarbaru":
        k = _load_json("banjarbaru_kecamatan.json")
        if k:
            return k
    out = []
    for f in REG.kecamatan(rid):
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        out.append({"name": f["properties"]["nama_kec"], "polys": polys})
    return out

def _desa_of(rid):
    """Rincian desa hanya ada untuk wilayah yang berkasnya disediakan."""
    return _load_json("%s_desa.json" % rid) or []

@lru_cache(maxsize=None)
def region_conf(rid):
    """Konfigurasi satu wilayah, atau None bila id tidak dikenal."""
    if REG.get_region(rid) is None:
        return None
    pin = REGION_PINNED.get(rid)
    if pin:
        bbox, nx, ny = pin["bbox"], pin["nx"], pin["ny"]
    else:
        spec = REG.grid_spec(rid)
        bbox, nx, ny = spec["bbox"], spec["nx"], spec["ny"]
    return {"id": rid, "name": REG.region_name(rid),
            "bbox": bbox, "nx": nx, "ny": ny,
            "boundary": _boundary_of(rid),
            "kecamatan": _kecamatan_of(rid),
            "desa": _desa_of(rid)}

# Sumber data + jendela revisit (hari). Tiap sumber mengisi sejumlah parameter.
# Penanda build. SATU-SATUNYA cara memastikan kode mana yang benar-benar
# hidup di Cloud Run: GET /api/versi. Naikkan setiap kali app.py diubah.
VERSI = "2026-09-05c · lead GFS digeser ke 14:00 WITA + ENSO otomatis di frontend"

# Ambang kolom CO Sentinel-5P (mmol/m2) -> kelas p-smol 0..4.
# Latar bersih Kalsel ±34; kabut asap karhutla umumnya >45. SEMENTARA.
CO_AMBANG = (40.0, 48.0, 60.0, 80.0)

SOURCES = [
    {"id": "sentinel2",  "name": "Sentinel-2 (NDMI/NDVI)",       "revisit": 5, "res": "10 m",        "params": "NDMI"},
    {"id": "modis",      "name": "MODIS / VIIRS (LST·Hotspot)",  "revisit": 3, "res": "250 m–1 km",  "params": "LST, hotspot, T permukaan"},
    {"id": "era5",       "name": "ERA5-Land (Cuaca)",            "revisit": 7, "res": "~9 km",       "params": "RH, angin, suhu tanah, SM"},
    {"id": "chirps",     "name": "CHIRPS (Presipitasi·SPI)",     "revisit": 10, "res": "~5 km",      "params": "SPI-3/6, hari kering"},
    {"id": "sentinel1",  "name": "Sentinel-1 SAR (TMA estimasi)","revisit": 12,"res": "10 m",       "params": "TMA gambut — estimasi SAR (perlu kalibrasi)"},
    {"id": "s5p",        "name": "Sentinel-5P (CO)",             "revisit": 3, "res": "7 km",        "params": "CO — asap/bara (p-smol) & indikasi kebakaran aktif"},
    {"id": "co2",        "name": "CO₂ udara (stasiun/lapangan)", "revisit": 1, "res": "titik sensor","params": "CO₂ pra-kebakaran (data darat)"},
]

# ─────────────────────────────────────────────────────────────
# Inisialisasi Earth Engine (service account)
# ─────────────────────────────────────────────────────────────
def init_ee():
    project = os.environ.get("EE_PROJECT", "indekskar-kalsel")  # ganti ke Project ID Anda
    sa = os.environ.get("EE_SERVICE_ACCOUNT")
    key = os.environ.get("EE_KEY_FILE")
    if sa and key and os.path.exists(key):
        # Mode PRODUKSI: service account (untuk Cloud Run / server tanpa login manusia)
        creds = ee.ServiceAccountCredentials(sa, key)
        ee.Initialize(creds, project=project)
    else:
        # Mode LOKAL/DEV: kredensial pengguna Anda sendiri.
        # Jalankan sekali sebelumnya:  python -c "import ee; ee.Authenticate()"
        ee.Initialize(project=project)

# ─────────────────────────────────────────────────────────────
# Grid: bangun sel di dalam poligon Banjarbaru
# ─────────────────────────────────────────────────────────────
def _pip(x, y, ring):
    inside = False
    n = len(ring); j = n - 1
    for i in range(n):
        xi, yi = ring[i]; xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def _inside(x, y, boundary):
    for poly in boundary:
        if _pip(x, y, poly[0]):
            hole = any(_pip(x, y, poly[k]) for k in range(1, len(poly)))
            if not hole:
                return True
    return False

def build_cells(conf):
    """Kembalikan daftar sel (r,c,lat,lng,inside). Sel di luar poligon ditandai void."""
    bb, nx, ny = conf["bbox"], conf["nx"], conf["ny"]
    boundary = conf["boundary"]
    cells = []
    for r in range(ny):
        for c in range(nx):
            lat = bb["n"] + (bb["s"] - bb["n"]) * (r + 0.5) / ny
            lng = bb["w"] + (bb["e"] - bb["w"]) * (c + 0.5) / nx
            cells.append({"r": r, "c": c, "lat": lat, "lng": lng, "in": _inside(lng, lat, boundary)})
    return cells

def ee_grid(cells, conf):
    """FeatureCollection sel-dalam-poligon, tiap Feature = rectangle dgn properti id (index r*nx+c)."""
    bb, nx, ny = conf["bbox"], conf["nx"], conf["ny"]
    dlat = (bb["n"] - bb["s"]) / ny
    dlng = (bb["e"] - bb["w"]) / nx
    feats = []
    for cell in cells:
        if not cell["in"]:
            continue
        r, c = cell["r"], cell["c"]
        s = bb["n"] - (r + 1) * dlat; n = bb["n"] - r * dlat
        w = bb["w"] + c * dlng;       e = bb["w"] + (c + 1) * dlng
        geom = ee.Geometry.Rectangle([w, s, e, n])
        feats.append(ee.Feature(geom, {"cid": r * nx + c}))
    return ee.FeatureCollection(feats)

def roi_geometry(conf):
    return ee.Geometry.MultiPolygon(conf["boundary"])

# ─────────────────────────────────────────────────────────────
# Fungsi komputasi per SUMBER (mengembalikan {cid: {param: value}}, acq_date)
# Catatan: asset ID & band bisa Anda sesuaikan; ini pola umum yang benar.
# ─────────────────────────────────────────────────────────────
_MIN_SCALE = 0  # lantai skala reduceRegions; dinaikkan saat mode provinsi agar ringan

# Ambang cakupan: bila sebuah band terisi pada < 60% sel, reduceRegions dianggap
# gagal untuk band itu dan nilainya diambil ulang pada skala SELURUH WILAYAH.
_COVER_MIN = 0.60

# Jejak band yang terpaksa diisi dengan nilai skala-wilayah. Dibersihkan oleh
# _compute_payload() sebelum tiap sumber dipanggil, lalu dicatat di src_meta
# supaya pengguna tahu band mana yang seragam se-wilayah.
_ROI_ISI: List[str] = []


def _reduce(image: ee.Image, grid: ee.FeatureCollection, scale: int,
            single_name: str = None, roi=None) -> Dict[int, Dict[str, float]]:
    """Rata-rata citra per sel grid.

    MASALAH YANG DIPERBAIKI DI SINI. reduceRegions() mengembalikan null bila
    sel jauh lebih kecil daripada piksel sumber — tidak ada pusat piksel yang
    jatuh di dalam sel. Akibatnya wilayah kecil kehilangan seluruh sumber
    beresolusi kasar tanpa satu pun pesan galat:

        KHDTK ULM  sel 100 m   ← CHIRPS 5.566 m, ERA5 11.132 m, S5P 7.000 m
                               semuanya kosong: k-sm, k-spi3, k-spi6,
                               t-rh, t-dry, t-wind, t-tsoil hilang
        Banjarmasin sel 495 m  ← ERA5 & S5P kosong
        Banjarbaru  sel 1.129 m ← lengkap (karena selnya cukup besar)

    Karena itu dimensi K dan P jatuh ke 0% di KHDTK dan indeks akhir terbaca
    TERKENDALI, padahal K sesungguhnya tidak terukur — bukan bernilai nol.

    Perbaikan: bila cakupan sebuah band di bawah _COVER_MIN, nilai band itu
    diambil satu kali untuk SELURUH ROI lalu disebar ke semua sel. Ini bukan
    mengarang data — pada wilayah 1.637 ha, satu piksel CHIRPS 5,5 km atau
    ERA5 9 km memang menutupi seluruh kawasan; nilai seragam adalah resolusi
    sebenarnya dari sumber tersebut.
    """
    sc = max(scale, _MIN_SCALE)
    fc = image.reduceRegions(collection=grid, reducer=ee.Reducer.mean(), scale=sc)
    info = fc.getInfo()
    out: Dict[int, Dict[str, float]] = {}
    cids: List[int] = []
    for f in info["features"]:
        p = f["properties"]
        cid = int(p.get("cid"))
        cids.append(cid)
        d = {}
        for k, v in p.items():
            if k == "cid" or v is None:
                continue
            # Citra 1-band -> reduceRegions menamai kolom 'mean'; petakan ke nama target
            key = single_name if (single_name and k == "mean") else k
            d[key] = v
        out[cid] = d

    if roi is None or not cids:
        return out

    try:
        asli = list(image.bandNames().getInfo() or [])
    except Exception:
        return out
    if not asli:
        return out
    target = [single_name] if (single_name and len(asli) == 1) else list(asli)

    batas = len(cids) * _COVER_MIN
    kurang = [i for i, b in enumerate(target)
              if sum(1 for c in cids if out.get(c, {}).get(b) is not None) < batas]
    if not kurang:
        return out

    try:
        rr = image.reduceRegion(reducer=ee.Reducer.mean(), geometry=roi, scale=sc,
                                bestEffort=True, maxPixels=int(1e9)).getInfo() or {}
    except Exception:
        return out

    for i in kurang:
        v = rr.get(asli[i])
        if v is None:
            continue
        nama = target[i]
        _ROI_ISI.append(nama)
        for c in cids:
            # setdefault: sel yang SUDAH punya nilai per-piksel tidak ditimpa
            out.setdefault(c, {}).setdefault(nama, v)
    return out

def _tambal(col, nama_band):
    """Sisipkan SATU citra bertopeng penuh berband sama agar koleksi tak pernah kosong.

    Ini menutup satu kelas galat yang sudah tiga kali menjatuhkan sumber:
    koleksi kosong -> reducer menghasilkan citra 0 BAND -> operator berikutnya
    (gt, subtract, normalizedDifference) menolak dengan pesan seperti
        "Image.gt: If one image has no bands, the other must also have no bands."
    dan SELURUH sumber gagal, bukan hanya satu indikator.

    Citra tambalan bertopeng penuh (selfMask atas konstanta 0), jadi ia tidak
    pernah menyumbang nilai. Bila koleksi aslinya memang kosong, hasil akhirnya
    bertopeng seluruhnya -> indikator TIDAK TERISI (bukan bernilai nol).
    Pilihan ini sejalan dengan K1: tidak terukur bukan berarti aman.

    ee.Algorithms.If sengaja TIDAK dipakai di sini karena kedua cabangnya
    tetap dievaluasi, sehingga cabang yang salah pun ikut melempar galat.
    """
    if isinstance(nama_band, str):
        nama_band = [nama_band]
    tambal = (ee.Image.constant([0] * len(nama_band))
              .rename(nama_band).selfMask().toFloat())
    return (col.select(nama_band).map(lambda im: im.toFloat())
               .merge(ee.ImageCollection([tambal])))


def compute_sentinel2(grid, roi):
    """NDMI = (B8 - B11)/(B8 + B11) dari Sentinel-2 SR, median tapak-awan terbaru."""
    today = ee.Date(dt.datetime.utcnow().isoformat())
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(roi).filterDate(today.advance(-20, "day"), today)
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60)))
    acq = col.aggregate_max("system:time_start")   # dari koleksi ASLI, sebelum ditambal
    img = _tambal(col, ["B8", "B11"]).median()
    ndmi = img.normalizedDifference(["B8", "B11"]).rename("k-ndmi")
    data = _reduce(ndmi, grid, 30, single_name="k-ndmi", roi=roi)
    return data, _fmt(acq)

def compute_modis(grid, roi):
    """LST siang clear-sky (°C, QC-masked) dari MOD11A1 + hotspot FIRMS + T permukaan."""
    today = ee.Date(dt.datetime.utcnow().isoformat())
    def qmask(img):
        # QC_Day bit 6-7 = error LST; simpan hanya piksel berkualitas baik (error <= 2K)
        good = img.select("QC_Day").rightShift(6).bitwiseAnd(3).lte(1)
        return img.updateMask(good)
    lst_col = ee.ImageCollection("MODIS/061/MOD11A1").filterDate(today.advance(-16, "day"), today).map(qmask)
    lst = (_tambal(lst_col, "LST_Day_1km").mean()
           .multiply(0.02).subtract(273.15).rename("t-lst"))
    # K3 — band "p-tsurf" tidak lagi dikirim: ia salinan t-lst.

    # INI YANG JATUH pada 2026-09-05: koleksi FIRMS kosong pada jendela 2 hari
    # -> max() memberi citra 0 band -> gt(0) menolak -> compute_modis GAGAL
    # seluruhnya, sehingga t-lst dan p-tsurf ikut hilang padahal keduanya
    # berasal dari MOD11A1 yang baik-baik saja. Satu koleksi kosong
    # mematikan tiga indikator sekaligus.
    fire_col = ee.ImageCollection("FIRMS").filterDate(today.advance(-2, "day"), today)
    firms = _tambal(fire_col, "T21").max().gt(0).rename("p-hotspot")

    lst_data = _reduce(lst, grid, 1000, single_name="t-lst", roi=roi)
    fire = _reduce(firms, grid, 1000, roi=roi)  # mean fraksi sel terbakar; frontend memakai ambang jumlah
    for cid, v in fire.items():
        h = v.get("p-hotspot")
        if h is None:      # FIRMS tak tersedia -> biarkan kosong, jangan tulis 0
            continue
        lst_data.setdefault(cid, {})["p-hotspot"] = round(h * 5)  # skala kasar -> "titik"
    acq = (_fmt(lst_col.aggregate_max("system:time_start"))
           or _fmt(fire_col.aggregate_max("system:time_start")))
    return lst_data, acq

def compute_era5(grid, roi):
    """RH, angin (km/j), suhu tanah (°C), soil moisture (%) dari ERA5-Land jam-an terbaru."""
    today = ee.Date(dt.datetime.utcnow().isoformat())
    col = ee.ImageCollection("ECMWF/ERA5_LAND/HOURLY").filterDate(today.advance(-12, "day"), today)
    # Ambil jam sore lokal (~14:00 WITA = 06:00 UTC) yang relevan untuk cuaca api,
    # bukan sekadar jam terakhir (sering malam -> RH terlalu tinggi, angin terlalu tenang).
    col06 = col.filter(ee.Filter.calendarRange(6, 6, "hour"))
    img = ee.Image(ee.Algorithms.If(col06.size().gt(0),
                                    col06.sort("system:time_start", False).first(),
                                    col.sort("system:time_start", False).first()))
    t2m = img.select("temperature_2m").subtract(273.15)
    d2m = img.select("dewpoint_temperature_2m").subtract(273.15)
    # RH dari suhu & titik embun (rumus Magnus)
    rh = (d2m.expression("100*exp((17.625*Td)/(243.04+Td))/exp((17.625*T)/(243.04+T))",
          {"Td": d2m, "T": t2m})).rename("t-rh")
    u = img.select("u_component_of_wind_10m"); v = img.select("v_component_of_wind_10m")
    wind = u.hypot(v).multiply(3.6).rename("t-wind")            # m/s -> km/j
    # arah angin DATANG (meteorologi), derajat 0-360: atan2(-u, -v)
    wdir = u.multiply(-1).atan2(v.multiply(-1)).multiply(180.0 / math.pi).add(360).mod(360).rename("wdir_deg")
    tsoil = img.select("soil_temperature_level_1").subtract(273.15).rename("t-tsoil")
    sm = img.select("volumetric_soil_water_layer_1").multiply(100).rename("k-sm")   # -> %
    stack = rh.addBands(wind).addBands(wdir).addBands(tsoil).addBands(sm)
    data = _reduce(stack, grid, 11132, roi=roi)
    # klasifikasi arah: nw (barat laut, laut/basah, aman) / other (tenggara, Australia/kering, bahaya) / trans
    for cid, vv in data.items():
        deg = vv.pop("wdir_deg", None)
        if deg is None:
            continue
        if deg >= 270 or deg < 22.5:
            vv["t-wdir"] = "nw"
        elif 90 <= deg < 202.5:
            vv["t-wdir"] = "other"
        else:
            vv["t-wdir"] = "trans"
    acq = col.aggregate_max("system:time_start")
    return data, _fmt(acq)

def compute_chirps(grid, roi):
    """SPI-3, SPI-6 (z-score sederhana thd klimatologi) + hari kering beruntun dari CHIRPS."""
    today = ee.Date(dt.datetime.utcnow().isoformat())
    chirps = ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY").select("precipitation")
    # Jangkar jendela pada citra CHIRPS TERBARU yang benar-benar tersedia.
    # "today - 7 hari" hanyalah tebakan: keterlambatan CHIRPS DAILY bervariasi
    # dan kerap melebihi 30 hari. Bila tebakan itu meleset, jendela 30 hari
    # terakhir menjadi KOSONG — dan koleksi kosong menghasilkan citra 0 band.
    _n = chirps.size()
    _latest = chirps.sort("system:time_start", False).first()
    end = ee.Date(ee.Algorithms.If(_n.gt(0),
                                   ee.Image(_latest).date().advance(1, "day"),
                                   today.advance(-7, "day")))

    def _sum_or_zero(col):
        """ee.ImageCollection.sum() pada koleksi KOSONG menghasilkan citra 0 band.
           Citra 0-band inilah penyebab galat yang tercatat di lapangan:
             "Image.subtract: If one image has no bands, the other must also
              have no bands. Got 1 and 0."
           Guard di bawah menjamin setiap operand selalu punya tepat 1 band
           bernama 'v', sehingga subtract/divide tidak pernah beda jumlah band."""
        return ee.Image(ee.Algorithms.If(col.size().gt(0), col.sum(),
                                         ee.Image.constant(0))).rename("v").toFloat()

    def spi(months, name):
        days = 30 * months
        cur = _sum_or_zero(chirps.filterDate(end.advance(-days, "day"), end))
        # klimatologi jendela hari-tanggal yang sama selama 20 tahun (2004-2023)
        yrs = ee.List.sequence(2004, 2023)
        def yr(y):
            e = ee.Date.fromYMD(ee.Number(y), end.get("month"), end.get("day"))
            return _sum_or_zero(chirps.filterDate(e.advance(-days, "day"), e))
        hist = ee.ImageCollection.fromImages(yrs.map(yr))
        mean = hist.mean().rename("v")
        std = hist.reduce(ee.Reducer.stdDev()).rename("v").max(0.1)
        return cur.subtract(mean).divide(std).rename(name)

    spi3 = spi(3, "k-spi3")
    spi6 = spi(6, "k-spi6")
    # hari kering (proksi robust): jumlah hari tanpa hujan >1 mm dalam 30 hari terakhir
    last30 = chirps.filterDate(end.advance(-30, "day"), end)
    # INI penyebab galat "Got 1 and 0" yang sebenarnya: ee.Image(30) punya
    # 1 band, sedangkan rainy punya 0 band bila last30 kosong. Jendela SPI
    # (90/180 hari) masih menangkap citra lama sehingga lolos; jendela 30
    # hari inilah yang kosong.
    rainy = _sum_or_zero(last30.map(lambda im: im.gt(1.0)))
    dry = ee.Image.constant(30).rename("v").toFloat().subtract(rainy).rename("t-dry")
    stack = spi3.addBands(spi6).addBands(dry)
    data = _reduce(stack, grid, 5566, roi=roi)
    acq = last30.aggregate_max("system:time_start")
    return data, _fmt(acq)

def compute_co2_ground(grid, roi):
    """CO2 udara pra-kebakaran = parameter DARAT (gas analyzer/stasiun lapangan), BUKAN satelit.
       Sentinel-5P hanya mengukur CO (produk pembakaran -> indikator saat/pascakebakaran), bukan CO2.
       Belum ada produk CO2 satelit per-piksel near-real-time yang andal untuk area kecil
       (OCO-2/GOSAT jarang; CAMS ~80 km terlalu kasar). Sisipkan data stasiun/lapangan CO2 di sini
       (pola sama seperti compute_sumur untuk TMA). Default kosong -> model IndeksKAR melewati
       parameter ini & menormalisasi ulang bobotnya (tanpa nilai palsu).

       Contoh integrasi data darat:
         pts = ee.FeatureCollection("users/anda/stasiun_co2")   # properti 'co2' (ppm)
         ... interpolasi/join ke grid, isi vals['p-co2'] ...
    """
    return {}, None

def compute_s5p_co(grid, roi):
    """CO kolom (Sentinel-5P) -> indikator KEBAKARAN AKTIF / asap (produk pembakaran).
       Ditampilkan sebagai info validasi; BUKAN CO2 pra-kebakaran. Nilai naik = ada pembakaran."""
    today = ee.Date(dt.datetime.utcnow().isoformat())
    col = (ee.ImageCollection("COPERNICUS/S5P/NRTI/L3_CO")
           .filterDate(today.advance(-5, "day"), today).select("CO_column_number_density"))
    co = _tambal(col, "CO_column_number_density").mean().multiply(1000).rename("p-co")   # mol/m^2 -> mmol/m^2 (angka enak dibaca)
    # p-smol (bobot 30 pada dimensi P) selama ini TIDAK PERNAH terisi: band ini
    # bernama "p-co", sedangkan penskor mencari "p-smol". Akibatnya 30 dari 100
    # bobot dimensi Pemicu mati di SEMUA wilayah, bukan hanya KHDTK.
    # Kelas 0-4 diturunkan dari kolom CO. Latar Kalsel ±34 mmol/m2 (terukur
    # 2026-08-20 di Banjarbaru); pembakaran biomassa menaikkannya tajam.
    # AMBANG INI SEMENTARA — kalibrasi dengan kejadian karhutla lokal.
    a0, a1, a2, a3 = CO_AMBANG
    smol = (co.gte(a0).add(co.gte(a1)).add(co.gte(a2)).add(co.gte(a3))
              .toFloat().rename("p-smol"))
    data = _reduce(co.addBands(smol), grid, 7000, roi=roi)
    acq = col.aggregate_max("system:time_start")
    return data, _fmt(acq)

def compute_tma_sar(grid, roi):
    """Estimasi TMA muka air gambut dari Sentinel-1 SAR (VV, dB).
       Backscatter VV berkorelasi dgn kelembapan/muka air gambut (studi tergambut R^2~0.6-0.75).
       INI ESTIMASI (confidence lebih rendah), bukan pengukuran langsung -> KALIBRASI dgn sumur pantau.
       Transform linear placeholder:  TMA_cm ~= A*VV_dB + B  (ganti A,B dgn regresi lapangan Anda)."""
    today = ee.Date(dt.datetime.utcnow().isoformat())
    s1 = (ee.ImageCollection("COPERNICUS/S1_GRD")
          .filterBounds(roi).filterDate(today.advance(-16, "day"), today)
          .filter(ee.Filter.eq("instrumentMode", "IW"))
          .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
          .select("VV"))
    vv = _tambal(s1, "VV").mean()   # dB (S1_GRD sudah log-scaled)
    A, B = 6.0, 40.0     # contoh: VV=-8dB->TMA~-8cm ; VV=-14dB->TMA~-44cm (GANTI dgn kalibrasi Anda)
    tma = vv.multiply(A).add(B).min(-2).max(-115).rename("k-tma")
    data = _reduce(tma, grid, 100, single_name="k-tma", roi=roi)
    acq = s1.aggregate_max("system:time_start")
    return data, _fmt(acq)

COMPUTERS = {
    "sentinel2": compute_sentinel2,
    "sentinel1": compute_tma_sar,
    "modis": compute_modis,
    "era5": compute_era5,
    "chirps": compute_chirps,
    "s5p": compute_s5p_co,
    "co2": compute_co2_ground,
}

def _fmt(millis_ee):
    try:
        m = millis_ee.getInfo()
        if not m:
            return None
        return dt.datetime.utcfromtimestamp(m / 1000.0).strftime("%Y-%m-%d")
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────
# Cache per-sumber (temporal-updating)
# ─────────────────────────────────────────────────────────────
# CATATAN: cache per-sumber di bawah ini TIDAK dipakai oleh _compute_payload()
# (yang memanggil COMPUTERS langsung dan mengandalkan cache GCS). Kuncinya juga
# hanya src_id — kalau kelak dipakai untuk banyak wilayah, kunci WAJIB diberi
# awalan region, sebab cid tiap wilayah berbeda arti.
CACHE: Dict[str, Dict[str, Any]] = {}   # src_id -> {"t": datetime, "acq": str, "data": {cid: {...}}}

def source_fresh(src_id: str, revisit_days: int) -> bool:
    ent = CACHE.get(src_id)
    if not ent:
        return False
    age = (dt.datetime.utcnow() - ent["t"]).total_seconds() / 86400.0
    return age <= revisit_days

def get_source(src_id, grid, roi, revisit, force):
    if (not force) and source_fresh(src_id, revisit):
        return CACHE[src_id]
    data, acq = COMPUTERS[src_id](grid, roi)
    CACHE[src_id] = {"t": dt.datetime.utcnow(), "acq": acq, "data": data}
    return CACHE[src_id]

# ─────────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────────
app = FastAPI(title="IndeksKAR GEE Backend")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_INIT = {"done": False}

@app.on_event("startup")
def _startup():
    try:
        init_ee(); _INIT["done"] = True
        print("Earth Engine terinisialisasi.")
    except Exception as e:
        print("GAGAL init EE:", e)

@app.get("/health")
def health():
    return {"ok": True, "ee": _INIT["done"]}

# ─────────────────────────────────────────────────────────────
# Cache persisten di Google Cloud Storage (GCS)
# Hasil GEE disimpan sebagai file JSON & disegarkan oleh Cloud Scheduler (?force=1).
# Pengguna membaca file itu -> selalu cepat. Jika GCS tak tersedia (mis. lokal),
# otomatis fallback menghitung langsung dari GEE.
# ─────────────────────────────────────────────────────────────
try:
    from google.cloud import storage as _gcs_storage
    _GCS = _gcs_storage.Client()
except Exception as _e:
    _GCS = None
    print("GCS nonaktif (fallback hitung langsung):", _e)

GCS_BUCKET = os.environ.get("GCS_BUCKET")
CACHE_MAX_AGE_H = float(os.environ.get("CACHE_MAX_AGE_H", "26"))  # jam; > interval scheduler harian

def _blob(region):
    if not (_GCS and GCS_BUCKET):
        return None
    return _GCS.bucket(GCS_BUCKET).blob("indekskar_%s.json" % region)

def gcs_read(region):
    b = _blob(region)
    if not b:
        return None
    try:
        if not b.exists():
            return None
        return json.loads(b.download_as_text())
    except Exception as e:
        print("gcs_read err:", e); return None

def gcs_write(region, data):
    b = _blob(region)
    if not b:
        return
    try:
        b.cache_control = "no-cache"
        b.upload_from_string(json.dumps(data), content_type="application/json")
    except Exception as e:
        print("gcs_write err:", e)

def _age_hours(iso):
    try:
        t = dt.datetime.fromisoformat(str(iso).replace("Z", ""))
        return (dt.datetime.utcnow() - t).total_seconds() / 3600.0
    except Exception:
        return 1e9

# ─────────────────────────────────────────────────────────────
# ENSO OTOMATIS — Oceanic Nino Index (ONI) dari NOAA CPC
# ─────────────────────────────────────────────────────────────
# ONI = anomali SST rerata-3-bulan di kotak Nino 3.4 (5LU-5LS, 170BB-120BB).
# Indeks ini BASIN-SCALE: tidak ada versi "khusus Kalsel" dan tidak akan ada.
# Yang bersifat regional adalah kekuatan respons (teleconnection), bukan
# indeksnya. Karena itu nilai ONI dipakai apa adanya sebagai konteks makro.
#
# Berkas sumber berupa teks polos, satu baris per musim:
#     SEAS  YR   TOTAL   ANOM
#      MJJ 2026  29.02   1.39
# Baris terakhir = musim terbaru yang sudah diterbitkan. ONI adalah rerata
# bergerak 3 bulan sehingga tertinggal sekitar satu bulan dari hari ini.
ONI_URL = os.environ.get("ONI_URL",
                         "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt")
ONI_TTL_H = float(os.environ.get("ONI_TTL_H", "12"))
_ONI_MEM = {"t": None, "data": None}

# Ambang mengikuti definisi NOAA CPC, dan pengalinya disamakan persis dengan
# pilihan dropdown pada frontend agar mode otomatis dan manual sebanding.
def oni_kelas(anom):
    """Kelas ENSO mengikuti konvensi CPC NOAA atas nilai ONI:
         lemah    0,5 - 0,9
         moderat  1,0 - 1,4
         kuat     >= 1,5
       Versi sebelumnya menamai SEMUA ONI >= 1,0 sebagai "Kuat", sehingga
       ONI +1,39 (MJJ 2026) salah label — seharusnya moderat.
       Pengali diseragamkan dengan kedua tarik-turun di frontend.
       La Nina memakai 0,80 (keputusan 2026-09-04)."""
    if anom <= -0.5:
        return "La Nina", 0.80
    if anom < 0.5:
        return "Netral", 1.00
    if anom < 1.0:
        return "El Nino Lemah", 1.15
    if anom < 1.5:
        return "El Nino Moderat", 1.30
    return "El Nino Kuat", 1.50

def _oni_ambil():
    """Unduh & urai ONI. Mengembalikan dict, atau melempar exception."""
    req = urllib.request.Request(ONI_URL, headers={"User-Agent": "IndeksKAR/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        teks = r.read().decode("utf-8", "replace")
    baris = [b for b in teks.strip().split("\n") if b.strip()]
    terakhir = None
    for b in reversed(baris):
        p = b.split()
        if len(p) == 4:
            try:
                terakhir = {"season": p[0], "year": int(p[1]),
                            "sst": float(p[2]), "oni": float(p[3])}
                break
            except ValueError:
                continue
    if not terakhir:
        raise ValueError("format ONI tidak dikenali")

    # Tiga musim terakhir — untuk melihat arah (menguat / melemah).
    seri = []
    for b in baris:
        p = b.split()
        if len(p) == 4:
            try:
                seri.append({"season": p[0], "year": int(p[1]), "oni": float(p[3])})
            except ValueError:
                pass
    nama, mult = oni_kelas(terakhir["oni"])
    sebelum = seri[-4:-1] if len(seri) >= 4 else []
    arah = "—"
    if sebelum:
        d = terakhir["oni"] - sebelum[0]["oni"]
        arah = "menguat" if d > 0.2 else ("melemah" if d < -0.2 else "stabil")
    return {
        "ok": True, "oni": terakhir["oni"],
        "season": terakhir["season"], "year": terakhir["year"],
        "label": "%s %d" % (terakhir["season"], terakhir["year"]),
        "kelas": nama, "multiplier": mult, "arah": arah,
        "riwayat": sebelum + [{"season": terakhir["season"], "year": terakhir["year"],
                               "oni": terakhir["oni"]}],
        "source": ONI_URL, "fetched": dt.datetime.utcnow().isoformat() + "Z",
    }

def oni_get(force=False):
    """ONI dengan cache dua lapis: memori (per kontainer) lalu GCS (lintas
       kontainer). Bila keduanya gagal, kembalikan penanda ok=False supaya
       frontend jatuh ke pilihan manual — bukan menebak nilai."""
    now = dt.datetime.utcnow()
    if not force and _ONI_MEM["data"] and _ONI_MEM["t"]:
        if (now - _ONI_MEM["t"]).total_seconds() / 3600.0 < ONI_TTL_H:
            return _ONI_MEM["data"]
    if not force:
        simpan = gcs_read("enso_oni")
        if simpan and _age_hours(simpan.get("fetched", "")) < ONI_TTL_H:
            _ONI_MEM.update({"t": now, "data": simpan})
            return simpan
    try:
        d = _oni_ambil()
        _ONI_MEM.update({"t": now, "data": d})
        gcs_write("enso_oni", d)
        return d
    except Exception as e:
        lama = gcs_read("enso_oni")
        if lama:
            lama["stale"] = True
            lama["error"] = str(e)
            return lama
        return {"ok": False, "error": str(e), "multiplier": None,
                "source": ONI_URL,
                "note": "ONI tak terjangkau — pengali ENSO harus dipilih manual."}

# ─────────────────────────────────────────────────────────────
# Kecamatan: agregasi per-kecamatan + skoring IndeksKAR + riwayat harian
# ─────────────────────────────────────────────────────────────
def _sc_ndmi(v):  return 0 if v>0 else 1 if v>-0.25 else 2 if v>-0.40 else 3 if v>-0.55 else 4
def _sc_tma(v):   return 0 if v>-20 else 1 if v>-40 else 2 if v>-60 else 3 if v>-80 else 4
def _sc_sm(v):    return 0 if v>60 else 1 if v>40 else 2 if v>25 else 3 if v>15 else 4
def _sc_spi3(v):  return 0 if v>0 else 1 if v>-1.0 else 2 if v>-1.5 else 3 if v>-2.0 else 4
def _sc_spi6(v):  return 0 if v>0 else 1 if v>-0.5 else 2 if v>-1.0 else 3 if v>-1.5 else 4
def _sc_lst(v):   return 0 if v<35 else 1 if v<40 else 2 if v<45 else 3 if v<50 else 4
def _sc_rh(v):    return 0 if v>70 else 1 if v>60 else 2 if v>50 else 3 if v>40 else 4
def _sc_dry(v):   return 0 if v<7 else 1 if v<14 else 2 if v<21 else 3 if v<28 else 4
def _sc_wind(v):  return 0 if v<20 else 1 if v<35 else 2 if v<50 else 3 if v<65 else 4
def _sc_wdir(v):  return 0 if v=="nw" else 2 if v=="trans" else 4
def _sc_tsoil(v): return 0 if v<30 else 1 if v<35 else 2 if v<40 else 3 if v<45 else 4
def _sc_hot(v):   return 0 if v==0 else 1 if v<=2 else 2 if v<=5 else 3 if v<=10 else 4
def _sc_smol(v):  return int(v)
def _sc_co2(v):   return 0 if v<500 else 1 if v<700 else 2 if v<1000 else 3 if v<1500 else 4
def _sc_tsurf(v): return 0 if v<35 else 1 if v<40 else 2 if v<45 else 3 if v<50 else 4
KIND = {
 "K":[("k-ndmi",22,_sc_ndmi),("k-tma",25,_sc_tma),("k-sm",15,_sc_sm),("k-spi3",20,_sc_spi3),("k-spi6",18,_sc_spi6)],
 "T":[("t-lst",18,_sc_lst),("t-rh",20,_sc_rh),("t-dry",20,_sc_dry),("t-wind",18,_sc_wind),("t-wdir",12,_sc_wdir),("t-tsoil",12,_sc_tsoil)],
 # K3 — "p-tsurf" dikeluarkan dari model satelit: nilainya adalah t-lst yang
 # diganti nama, dari citra dan ambang yang sama persis, sehingga satu
 # pengukuran suhu permukaan tercacah dua kali (18 di T dan 18 lagi di P).
 # Bobot P kini 82; _score_dim membagi dengan bobot yang benar-benar terpakai,
 # jadi tidak perlu penyesuaian lain.
 "P":[("p-hotspot",30,_sc_hot),("p-smol",30,_sc_smol),("p-co2",22,_sc_co2)],
}
DIMW = {"K": 0.40, "T": 0.35, "P": 0.25}

def _score_dim(grp, vals):
    """Skor 0-100 satu dimensi, atau None bila tidak ada satu pun indikator.

    Mengembalikan 0 untuk dimensi kosong adalah kekeliruan: 0 berarti "sangat
    aman", padahal yang benar adalah "tidak diketahui". Kekeliruan itu yang
    membuat KHDTK ULM terbaca TERKENDALI — K dan P kosong dihitung sebagai 0
    dengan bobot penuh 40% dan 25%."""
    s = w = 0.0
    for pid, wt, fn in grp:
        v = vals.get(pid)
        if v is None: continue
        try: s += fn(v)*wt; w += wt
        except Exception: pass
    return round(s/w/4*100, 1) if w else None

def score_ik(vals, me=1.0, mm=1.0):
    """Sama persis dengan computeIK() di frontend: bobot dimensi
       dinormalisasi ulang terhadap dimensi yang benar-benar punya data."""
    K = _score_dim(KIND["K"], vals); T = _score_dim(KIND["T"], vals); P = _score_dim(KIND["P"], vals)
    raw = wsum = 0.0
    for nilai, dw in ((K, DIMW["K"]), (T, DIMW["T"]), (P, DIMW["P"])):
        if nilai is None: continue
        raw += nilai * dw; wsum += dw
    raw = raw / wsum if wsum else 0.0
    total = min(100, round(raw * me * mm))
    lv = 0 if total<25 else 1 if total<50 else 2 if total<75 else 3
    return K, T, P, total, lv
def cell_kecamatan(lng, lat, kecs):
    for k in kecs:
        for poly in k.get("polys", []):
            if poly and _pip(lng, lat, poly[0]):
                return k["name"]
    return None

def cell_desa(lng, lat, desas):
    for dsa in desas:
        for poly in dsa.get("polys", []):
            if poly and _pip(lng, lat, poly[0]):
                return dsa
    return None

def _desa_centroid(dsa):
    pts = [p for poly in dsa.get("polys", []) for ring in poly for p in ring]
    return (sum(p[0] for p in pts)/len(pts), sum(p[1] for p in pts)/len(pts)) if pts else (None, None)
def history_read(region):
    if not (_GCS and GCS_BUCKET): return {}
    try:
        bl = _GCS.bucket(GCS_BUCKET).blob("history_%s.json" % region)
        if not bl.exists(): return {}
        return json.loads(bl.download_as_text())
    except Exception as e:
        print("history_read err:", e); return {}
def history_write(region, hist):
    if not (_GCS and GCS_BUCKET): return
    try:
        bl = _GCS.bucket(GCS_BUCKET).blob("history_%s.json" % region)
        bl.cache_control = "no-cache"
        bl.upload_from_string(json.dumps(hist), content_type="application/json")
    except Exception as e:
        print("history_write err:", e)

def _compute_payload(rid):
    conf = region_conf(rid)
    KECAMATAN = conf["kecamatan"]
    DESA = conf["desa"]
    cells = build_cells(conf)
    grid = ee_grid(cells, conf)
    roi = roi_geometry(conf)
    merged: Dict[int, Dict[str, Any]] = {}
    src_meta = []
    for s in SOURCES:
        try:
            del _ROI_ISI[:]                       # jejak cadangan skala-wilayah
            data, acq = COMPUTERS[s["id"]](grid, roi)
            seragam = sorted(set(_ROI_ISI))
            for cid, vals in data.items():
                merged.setdefault(int(cid), {}).update(vals)
            age = (dt.datetime.utcnow().date() - dt.date.fromisoformat(acq)).days if acq else None
            # Sumber tanpa satu pun nilai bukan "fresh" — itu menyesatkan.
            # Frontend mengenali status "na" dan menampilkannya TIDAK TERSEDIA.
            if not data:
                status = "na"
            else:
                status = "stale" if (age is not None and age > s["revisit"]) else "fresh"
            meta = {"id": s["id"], "name": s["name"], "revisit": s["revisit"],
                    "res": s["res"], "params": s["params"], "acq": acq,
                    "ageDays": age if age is not None else -1, "status": status}
            if seragam:
                # Sel lebih kecil daripada piksel sumber: nilai diambil pada skala
                # seluruh wilayah, jadi seragam. Dinyatakan terbuka, bukan disembunyikan.
                meta["seragam"] = seragam
                meta["note"] = ("piksel sumber lebih besar daripada sel grid — "
                                + ", ".join(seragam) + " seragam se-wilayah")
            src_meta.append(meta)
        except Exception as e:
            src_meta.append({"id": s["id"], "name": s["name"], "revisit": s["revisit"],
                             "res": s["res"], "params": s["params"], "acq": None,
                             "ageDays": -1, "status": "error", "error": str(e)})
    nx = conf["nx"]
    pixels = []
    for cell in cells:
        cid = cell["r"] * nx + cell["c"]
        if not cell["in"]:
            pixels.append({"r": cell["r"], "c": cell["c"], "lat": round(cell["lat"], 5),
                           "lng": round(cell["lng"], 5), "void": True})
            continue
        vals = merged.get(cid, {})
        vals.setdefault("t-wdir", "trans")
        clean = {}
        for k, v in vals.items():
            clean[k] = round(v, 2) if isinstance(v, (int, float)) else v
        pixels.append({"r": cell["r"], "c": cell["c"], "lat": round(cell["lat"], 5),
                       "lng": round(cell["lng"], 5), "vals": clean})

    # ── Agregasi per-kecamatan + skor IndeksKAR (multiplier netral) ──
    kec_out = []
    if KECAMATAN:
        buckets = {}
        for cell in cells:
            if not cell["in"]:
                continue
            v = merged.get(cell["r"] * nx + cell["c"])
            if not v:
                continue
            kn = cell_kecamatan(cell["lng"], cell["lat"], KECAMATAN)
            if kn:
                buckets.setdefault(kn, []).append(v)
        allids = [pid for grp in KIND.values() for (pid, _w, _f) in grp]
        for name, vlist in buckets.items():
            avg = {}
            for pid in allids:
                if pid == "t-wdir":
                    ws = [x.get(pid) for x in vlist if x.get(pid)]
                    avg[pid] = max(set(ws), key=ws.count) if ws else "trans"
                    continue
                nums = [x[pid] for x in vlist if isinstance(x.get(pid), (int, float))]
                if nums:
                    avg[pid] = round(sum(nums) / len(nums), 2)
            K, T, P, total, lv = score_ik(avg)
            kec_out.append({"name": name, "n": len(vlist), "K": K, "T": T, "P": P,
                            "total": total, "lv": lv, "vals": avg})
        kec_out.sort(key=lambda x: -x["total"])

    # ── Agregasi per-desa/kelurahan ──
    desa_out = []
    if DESA:
        allids = [pid for grp in KIND.values() for (pid, _w, _f) in grp]
        dbuckets = {}
        for cell in cells:
            if not cell["in"]:
                continue
            v = merged.get(cell["r"] * nx + cell["c"])
            if not v:
                continue
            dsa = cell_desa(cell["lng"], cell["lat"], DESA)
            if dsa:
                dbuckets.setdefault(dsa["name"], {"kec": dsa.get("kecamatan", "-"), "v": []})["v"].append(v)

        def _agg(vlist):
            avg = {}
            for pid in allids:
                if pid == "t-wdir":
                    ws = [x.get(pid) for x in vlist if x.get(pid)]
                    avg[pid] = max(set(ws), key=ws.count) if ws else "trans"
                    continue
                nums = [x[pid] for x in vlist if isinstance(x.get(pid), (int, float))]
                if nums:
                    avg[pid] = round(sum(nums) / len(nums), 2)
            return avg

        for name, obj in dbuckets.items():
            avg = _agg(obj["v"])
            K, T, P, total, lv = score_ik(avg)
            desa_out.append({"name": name, "kecamatan": obj["kec"], "n": len(obj["v"]),
                             "K": K, "T": T, "P": P, "total": total, "lv": lv})
        # Desa lebih kecil dari sel grid: pakai sel terdekat ke centroid agar tetap tampil
        covered = set(dbuckets.keys())
        incells = [(cell, merged.get(cell["r"] * nx + cell["c"]))
                   for cell in cells if cell["in"] and merged.get(cell["r"] * nx + cell["c"])]
        for dsa in DESA:
            if dsa["name"] in covered:
                continue
            cx, cy = _desa_centroid(dsa)
            if cx is None or not incells:
                continue
            best = min(incells, key=lambda cv: (cv[0]["lng"] - cx) ** 2 + (cv[0]["lat"] - cy) ** 2)[1]
            K, T, P, total, lv = score_ik(dict(best))
            desa_out.append({"name": dsa["name"], "kecamatan": dsa.get("kecamatan", "-"), "n": 0,
                             "K": K, "T": T, "P": P, "total": total, "lv": lv})
        desa_out.sort(key=lambda x: -x["total"])

    # ── Riwayat harian per-kecamatan (untuk grafik tren) ──
    today = dt.date.today().isoformat()
    hist = history_read(rid)
    if kec_out:
        hist[today] = {k["name"]: {"total": k["total"], "K": k["K"], "T": k["T"], "P": k["P"]}
                       for k in kec_out}
        for d in sorted(hist)[:-60]:
            del hist[d]
        history_write(rid, hist)

    return {
        "region": conf["id"], "name": conf["name"],
        "generated": dt.datetime.utcnow().isoformat() + "Z",
        "cached": False,
        "bbox": conf["bbox"], "nx": conf["nx"], "ny": conf["ny"],
        "boundary": conf["boundary"],
        "kecamatan": kec_out,
        "desa": desa_out,
        "history": hist,
        "sources": src_meta,
        "pixels": pixels,
        "enso": oni_get(),
    }

@app.get("/api/versi")
def versi():
    """Dipakai untuk memastikan revisi mana yang sedang melayani."""
    return {"versi": VERSI,
            "fitur": {"jendela_hujan": True, "penapis_tmc": True,
                      "mjo_rmm": True, "iod_dmi": True,
                      "tambal_koleksi_kosong": True,
                      "cadangan_skala_roi": True,
                      "skor_dimensi_null": True,
                      "p_smol_dari_co": True,
                      "vals_provinsi": True},
            "co_ambang": list(CO_AMBANG)}


@app.get("/api/regions")
def regions_list():
    """Daftar wilayah yang dilayani — memudahkan diagnosis dari peramban."""
    return {"regions": REG.list_regions()}

@app.get("/api/indekskar/{region}")
def indekskar(region: str, force: int = Query(0)):
    conf = region_conf(region)
    if conf is None:
        return JSONResponse({"error": f"region '{region}' tidak dikenal",
                             "tersedia": list(REG.REGION_IDS)}, status_code=404)

    # 1) Layani dari cache GCS bila masih segar & tidak dipaksa (cepat, tanpa panggil GEE)
    if not force:
        cached = gcs_read(region)
        if cached and _age_hours(cached.get("generated", "")) < CACHE_MAX_AGE_H:
            cached["cached"] = True
            return cached

    # 2) Perlu hitung -> pastikan Earth Engine siap
    if not _INIT["done"]:
        try:
            init_ee(); _INIT["done"] = True
        except Exception as e:
            stale = gcs_read(region)          # sajikan cache lama bila ada
            if stale:
                stale["cached"] = True
                return stale
            return JSONResponse({"error": f"Earth Engine belum siap: {e}"}, status_code=503)

    # 3) Hitung dari GEE, simpan ke GCS, kirim
    try:
        payload = _compute_payload(region)
    except Exception as e:
        stale = gcs_read(region)
        if stale:
            stale["cached"] = True
            return stale
        return JSONResponse({"error": f"Komputasi gagal: {e}"}, status_code=500)
    gcs_write(region, payload)
    return payload

# ─────────────────────────────────────────────────────────────
# LEVEL PROVINSI — reaksi cepat per kecamatan se-Kalsel
# (agregasi langsung ke poligon kecamatan pada skala ≥1 km agar ringan & cepat)
# ─────────────────────────────────────────────────────────────
def _load_prov_kec():
    """Kecamatan se-provinsi. Berkas lama diutamakan; bila tidak ada, dibangun
       dari kecamatan_kalsel.geojson yang atribut kabupatennya sudah dikoreksi
       secara geometris (45 dari 147 kecamatan salah label pada data asal)."""
    p = _load_json("kalsel_kecamatan.json")
    if p:
        return p
    out = []
    for rid in REG.KAB_IDS:      # KAB_IDS, bukan REGION_IDS — KHDTK berada di
                                 # dalam Kab. Banjar; kalau ikut, tercacah dua kali
        for f in REG.kecamatan(rid):
            g = f["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            out.append({"name": f["properties"]["nama_kec"],
                        "kab": f["properties"]["kab_name"], "polys": polys})
    return out

PROV_KEC = _load_prov_kec()
if not PROV_KEC:
    print("kecamatan provinsi tidak dimuat")

def province_fc():
    feats = []
    for i, k in enumerate(PROV_KEC):
        try:
            feats.append(ee.Feature(ee.Geometry.MultiPolygon(k["polys"]), {"cid": i}))
        except Exception:
            pass
    return ee.FeatureCollection(feats)

def _compute_province():
    global _MIN_SCALE
    fc = province_fc()
    roi = ee.Geometry.Rectangle([114.3, -4.85, 116.55, -1.8])
    merged = {}
    src_meta = []
    _MIN_SCALE = 1000
    try:
        for s in SOURCES:
            try:
                del _ROI_ISI[:]
                data, acq = COMPUTERS[s["id"]](fc, roi)
                for cid, vals in data.items():
                    merged.setdefault(int(cid), {}).update(vals)
                age = (dt.datetime.utcnow().date() - dt.date.fromisoformat(acq)).days if acq else None
                if not data:
                    status = "na"
                else:
                    status = "stale" if (age is not None and age > s["revisit"]) else "fresh"
                src_meta.append({"id": s["id"], "name": s["name"], "acq": acq,
                                 "ageDays": age if age is not None else -1, "status": status})
            except Exception as e:
                src_meta.append({"id": s["id"], "name": s["name"], "status": "error", "error": str(e)})
    finally:
        _MIN_SCALE = 0
    kec = []
    for i, k in enumerate(PROV_KEC):
        vals = merged.get(i, {})
        vals.setdefault("t-wdir", "trans")
        K, T, P, total, lv = score_ik(vals)
        # vals ikut dikirim supaya panel diagnostik (Ctrl+Alt+T di frontend)
        # bisa menampilkan nilai variabel mentah pada level provinsi, bukan
        # hanya skor dimensinya. Menambah ~10 KB pada payload provinsi.
        bersih = {kk: (round(vv, 2) if isinstance(vv, (int, float)) else vv)
                  for kk, vv in vals.items()}
        kec.append({"name": k["name"], "kab": k.get("kab", "-"),
                    "K": K, "T": T, "P": P, "total": total, "lv": lv,
                    "vals": bersih})
    kec.sort(key=lambda x: -x["total"])

    # ── Agregasi ke tingkat KABUPATEN ────────────────────────────────────
    # Rata-rata sederhana antar kecamatan di dalam satu kabupaten. Sengaja
    # TIDAK ditimbang luas: tujuan grafik ini memantau arah perubahan harian,
    # bukan menghitung paparan absolut. Kecamatan dengan skor None dilewati.
    kab_map = {}
    for k in kec:
        kab_map.setdefault(k.get("kab", "-"), []).append(k)
    kab_out = []
    for nama, anggota in kab_map.items():
        def _avg(field):
            vals = [a[field] for a in anggota if a.get(field) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None
        total = _avg("total")
        if total is None:
            continue
        total = int(round(total))
        kab_out.append({
            "name": nama, "n_kec": len(anggota),
            "K": _avg("K"), "T": _avg("T"), "P": _avg("P"),
            "total": total,
            "lv": 0 if total < 25 else 1 if total < 50 else 2 if total < 75 else 3,
            "max_kec": max(anggota, key=lambda a: a["total"])["name"],
            "max_total": max(a["total"] for a in anggota),
        })
    kab_out.sort(key=lambda x: -x["total"])

    # ── Rekaman harian per kabupaten (untuk grafik garis) ────────────────
    hari = dt.date.today().isoformat()
    hist_kab = history_read("kabupaten_kalsel")
    if kab_out:
        hist_kab[hari] = {k["name"]: {"total": k["total"], "K": k["K"],
                                      "T": k["T"], "P": k["P"]} for k in kab_out}
        for d in sorted(hist_kab)[:-120]:      # simpan 120 hari terakhir
            del hist_kab[d]
        history_write("kabupaten_kalsel", hist_kab)

    return {"level": "provinsi", "name": "Kalimantan Selatan",
            "generated": dt.datetime.utcnow().isoformat() + "Z", "cached": False,
            "kecamatan": kec, "kabupaten": kab_out,
            "history_kab": hist_kab, "sources": src_meta,
            "enso": oni_get()}

# ─────────────────────────────────────────────────────────────
# ARSIP HARIAN — peta PNG + tabel tren, disimpan ke GCS
# ─────────────────────────────────────────────────────────────
# Dijalankan penjadwal tiap 07.00 WITA, dua jam setelah perhitungan
# provinsi 05.00. Endpoint ini SENGAJA tidak memaksa perhitungan GEE:
# ia hanya menggambar dari payload yang sudah ada di cache. Dengan
# begitu satu permintaan selesai dalam hitungan detik, bukan menit,
# dan tidak pernah kehabisan waktu.
import tempfile, shutil

ARSIP_PREFIX = os.environ.get("ARSIP_PREFIX", "arsip")

def _gcs_upload(local_path, blob_name, content_type):
    if not (_GCS and GCS_BUCKET):
        return None
    try:
        b = _GCS.bucket(GCS_BUCKET).blob(blob_name)
        b.upload_from_filename(local_path, content_type=content_type)
        return blob_name
    except Exception as e:
        print("arsip upload gagal:", blob_name, e)
        return None

def _tipe(nama):
    if nama.endswith(".png"):
        return "image/png"
    if nama.endswith(".csv"):
        return "text/csv; charset=utf-8"
    return "application/json"

@app.get("/api/arsip")
def arsip(tanggal: str = Query(None), force: int = Query(0)):
    """Rakit arsip harian: peta provinsi, peta tiap wilayah, tabel tren.

       ?force=1 → hitung ulang wilayah yang cache-nya kosong (lebih lama).
       ?tanggal=YYYY-MM-DD → hanya untuk penamaan folder; datanya tetap
       yang terbaru, karena payload historis per piksel tidak disimpan."""
    try:
        import arsip as AR
    except Exception as e:
        return JSONResponse({"error": "modul arsip tidak tersedia: %s" % e},
                            status_code=500)

    hari = tanggal or dt.date.today().isoformat()
    ens = oni_get()

    # 1 · payload provinsi (dari cache; hitung bila belum ada)
    prov = gcs_read("provinsi_kalsel")
    if not prov:
        if not _INIT["done"]:
            try:
                init_ee(); _INIT["done"] = True
            except Exception as e:
                return JSONResponse({"error": "EE belum siap: %s" % e}, status_code=503)
        try:
            prov = _compute_province()
            gcs_write("provinsi_kalsel", prov)
        except Exception as e:
            return JSONResponse({"error": "provinsi gagal: %s" % e}, status_code=500)

    # 2 · payload tiap wilayah — cache saja, kecuali force=1
    regions, lewat = {}, []
    for rid in REG.REGION_IDS:
        pay = gcs_read(rid)
        if not pay and force:
            try:
                pay = _compute_payload(rid)
                gcs_write(rid, pay)
            except Exception as e:
                print("arsip: %s gagal dihitung: %s" % (rid, e))
                pay = None
        if pay:
            regions[rid] = pay
        else:
            lewat.append(rid)

    hist = history_read("kabupaten_kalsel")

    # 3 · gambar ke folder sementara, lalu unggah
    tmp = tempfile.mkdtemp(prefix="arsip_")
    try:
        manifes = AR.bangun_arsip(tmp, hari, prov, REG._kecamatan(),
                                  regions, hist, score_ik, ens)
        y, m, d = hari.split("-")
        prefix = "%s/%s/%s/%s" % (ARSIP_PREFIX, y, m, d)
        terunggah = []
        for it in manifes["berkas"]:
            nama = it["berkas"]
            blob = _gcs_upload(os.path.join(tmp, nama),
                               "%s/%s" % (prefix, nama), _tipe(nama))
            if blob:
                terunggah.append(blob)
        manifes.update({"prefix": prefix, "terunggah": len(terunggah),
                        "wilayah_dilewati": lewat, "bucket": GCS_BUCKET,
                        "enso": ens.get("kelas") if ens.get("ok") else None})
        if _GCS and GCS_BUCKET:
            try:
                _GCS.bucket(GCS_BUCKET).blob("%s/manifes.json" % prefix)\
                    .upload_from_string(json.dumps(manifes, ensure_ascii=False),
                                        content_type="application/json")
            except Exception as e:
                print("manifes gagal:", e)
        return manifes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

@app.get("/api/enso")
def enso(force: int = Query(0)):
    """Konteks makro ENSO (ONI NOAA CPC) + pengali yang disarankan."""
    return oni_get(force=bool(force))

@app.get("/api/history/kabupaten")
def history_kabupaten():
    """Riwayat IndeksKAR harian per kabupaten — dibaca langsung dari GCS.
       Ringan: tidak pernah memanggil Earth Engine, jadi aman dipanggil
       setiap kali grafik dibuka."""
    hist = history_read("kabupaten_kalsel")
    hari = sorted(hist.keys())
    kabs = sorted({k for d in hist.values() for k in d.keys()})
    return {"days": hari, "kabupaten": kabs, "history": hist,
            "count_days": len(hari), "updated": hari[-1] if hari else None}

@app.get("/api/provinsi")
def provinsi(force: int = Query(0)):
    if not force:
        cached = gcs_read("provinsi_kalsel")
        if cached and _age_hours(cached.get("generated", "")) < CACHE_MAX_AGE_H:
            cached["cached"] = True
            return cached
    if not _INIT["done"]:
        try:
            init_ee(); _INIT["done"] = True
        except Exception as e:
            st = gcs_read("provinsi_kalsel")
            if st:
                st["cached"] = True; return st
            return JSONResponse({"error": f"Earth Engine belum siap: {e}"}, status_code=503)
    try:
        payload = _compute_province()
    except Exception as e:
        st = gcs_read("provinsi_kalsel")
        if st:
            st["cached"] = True; return st
        return JSONResponse({"error": f"Komputasi provinsi gagal: {e}"}, status_code=500)
    gcs_write("provinsi_kalsel", payload)
    return payload


# ═════════════════════════════════════════════════════════════════════════
#  MODUL JENDELA HUJAN & PENAPIS TMC
# ═════════════════════════════════════════════════════════════════════════
#
#  Modul ini TIDAK menyuntikkan apa pun ke IndeksKAR. Alasannya prinsip:
#  IndeksKAR menjawab "seberapa siap lahan ini terbakar HARI INI" — sebuah
#  diagnosis atas keadaan yang sudah terukur. Prakiraan hujan menjawab
#  pertanyaan lain, "kapan jendela untuk membasahi lahan", dan membawa serta
#  ketidakpastian model cuaca. Mencampurnya akan membuat skor yang sudah
#  diterbitkan ikut bergoyang mengikuti ramalan. Karena itu keluarannya
#  berdiri sendiri di /api/hujan/<wilayah>, berdampingan, bukan menyatu.
#
#  Tiga lapis waktu yang dipakai:
#    · 1-7 hari   GFS 0,25 derajat (NOAA/GFS0P25) — uap, RH, awan, hujan
#    · 1-4 minggu MJO lewat indeks RMM harian (BoM Australia)
#    · musiman    IOD lewat DMI yang DIHITUNG SENDIRI dari SST OISST
#
#  BATAS YANG HARUS DIINGAT. GFS berpiksel 27,8 km; ia tidak menyelesaikan
#  awan konvektif tropis. Keluarannya sah dibaca sebagai "jendela peluang
#  24-72 jam untuk satu kabupaten", TIDAK sah dibaca sebagai "hujan pukul
#  sekian di desa anu". Skor di bawah adalah penapis heuristik yang masih
#  harus diverifikasi terhadap hujan teramati sebelum dipercaya angkanya.

# ── MJO · indeks RMM (Wheeler & Hendon 2004) ─────────────────────────────
# Berkas teks satu baris per hari:
#   year month day RMM1 RMM2 fase amplitudo  WH04_method:_OLR_&_NCEP_wind
# Nilai hilang ditandai 999 atau 1e36.
#
# Fase 1-8 menyatakan posisi gugus konveksi yang merambat ke TIMUR dari
# Samudra Hindia. Untuk Benua Maritim (termasuk Kalimantan):
#   fase 3-4-5  gugus berada di atas kita          -> konveksi diperkuat
#   fase 7-8-1  gugus di belahan bumi seberang     -> konveksi ditekan
#   amplitudo < 1 berarti sinyalnya lemah; fase kehilangan arti.
# BERKAS RMM BoM SUDAH MATI. Diperiksa 2026-09-05: baris terakhir pada
# rmm.74toRealtime.txt bertanggal 2024-02-24, dan rmm.realtime.txt menjawab
# 404. Indeks CPC (proj_norm_order.ascii) berhenti di pentad 2026-07-27.
# Yang paling segar adalah OMI (OLR MJO Index, Kiladis dkk. 2014) di PSL.
#
# Format OMI: year month day OMI1 OMI2 amplitudo
# OMI setara RMM dengan rotasi baku:  RMM1 ~ -OMI2 ,  RMM2 ~ OMI1
# Fase dihitung dari sudut (RMM1, RMM2) memakai konvensi delapan oktan
# Wheeler-Hendon; rumusnya sudah diuji terhadap baris asli berkas BoM
# (2024-02-19 -> fase 8, 2024-02-21 -> fase 5, 2024-02-22 -> fase 6).
MJO_URL = os.environ.get("MJO_URL", "https://psl.noaa.gov/mjo/mjoindex/omi.1x.txt")
MJO_TTL_H = float(os.environ.get("MJO_TTL_H", "12"))
MJO_MAKS_UMUR_HARI = float(os.environ.get("MJO_MAKS_UMUR_HARI", "21"))
_MJO_MEM = {"t": None, "data": None}

MJO_FASE_BASAH = (3, 4, 5)
MJO_FASE_KERING = (7, 8, 1)


def mjo_fase(rmm1, rmm2):
    """Oktan Wheeler-Hendon dari (RMM1, RMM2)."""
    sudut = (math.degrees(math.atan2(rmm2, rmm1)) + 360.0) % 360.0
    return int((math.floor(sudut / 45.0) + 4) % 8) + 1


def mjo_kelas(fase, amp):
    if amp is None or amp < 1.0:
        return "lemah / tak berpola", 0
    if fase in MJO_FASE_BASAH:
        return "mendukung hujan di Benua Maritim", +1
    if fase in MJO_FASE_KERING:
        return "menekan hujan di Benua Maritim", -1
    return "peralihan", 0


def _mjo_ambil():
    req = urllib.request.Request(MJO_URL, headers={"User-Agent": "IndeksKAR/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        teks = r.read().decode("utf-8", "replace")
    sah = []
    for baris in teks.strip().split("\n"):
        p = baris.split()
        if len(p) < 6:
            continue
        try:
            th, bl, hr = int(p[0]), int(p[1]), int(p[2])
            omi1, omi2, amp = float(p[3]), float(p[4]), float(p[5])
        except ValueError:
            continue                       # baris keterangan
        if abs(omi1) > 90 or abs(omi2) > 90 or amp > 90:
            continue                       # 999 / 1e36 = nilai hilang
        rmm1, rmm2 = -omi2, omi1
        sah.append({"tanggal": "%04d-%02d-%02d" % (th, bl, hr),
                    "omi1": round(omi1, 4), "omi2": round(omi2, 4),
                    "rmm1": round(rmm1, 4), "rmm2": round(rmm2, 4),
                    "fase": mjo_fase(rmm1, rmm2), "amplitudo": round(amp, 4)})
    if not sah:
        raise ValueError("tak satu pun baris OMI yang sah")
    akhir = sah[-1]
    kelas, arah = mjo_kelas(akhir["fase"], akhir["amplitudo"])
    umur = (dt.date.today() - dt.date.fromisoformat(akhir["tanggal"])).days
    dipakai = umur <= MJO_MAKS_UMUR_HARI
    d = {"ok": True, "tanggal": akhir["tanggal"], "umur_hari": umur,
         "dipakai": dipakai, "fase": akhir["fase"], "amplitudo": akhir["amplitudo"],
         "omi1": akhir["omi1"], "omi2": akhir["omi2"],
         "rmm1": akhir["rmm1"], "rmm2": akhir["rmm2"],
         "kelas": kelas, "arah": arah if dipakai else 0,
         "riwayat": sah[-14:], "source": MJO_URL,
         "metode": "OMI (Kiladis dkk. 2014); fase via rotasi RMM1~-OMI2, RMM2~OMI1",
         "fetched": dt.datetime.utcnow().isoformat() + "Z"}
    if not dipakai:
        d["catatan"] = ("Data MJO tertinggal %d hari (batas %d). Geseran MJO "
                        "TIDAK diterapkan pada skor." % (umur, int(MJO_MAKS_UMUR_HARI)))
    return d


def mjo_get(force=False):
    """Pola cache sama dengan ONI: memori, lalu GCS, lalu unduh."""
    now = dt.datetime.utcnow()
    if not force and _MJO_MEM["data"] and _MJO_MEM["t"]:
        if (now - _MJO_MEM["t"]).total_seconds() / 3600.0 < MJO_TTL_H:
            return _MJO_MEM["data"]
    if not force:
        simpan = gcs_read("mjo_omi")
        if simpan and _age_hours(simpan.get("fetched", "")) < MJO_TTL_H:
            _MJO_MEM.update({"t": now, "data": simpan})
            return simpan
    try:
        d = _mjo_ambil()
        _MJO_MEM.update({"t": now, "data": d})
        gcs_write("mjo_omi", d)
        return d
    except Exception as e:
        lama = gcs_read("mjo_omi")
        if lama:
            lama["stale"] = True; lama["error"] = str(e)
            return lama
        return {"ok": False, "dipakai": False, "error": str(e), "source": MJO_URL,
                "note": "OMI tak terjangkau — konteks MJO tidak dipakai."}


# ── IOD · Dipole Mode Index dihitung sendiri dari SST ────────────────────
# DMI = anomali SST kotak BARAT (50-70BT, 10LS-10LU)
#     - anomali SST kotak TIMUR (90-110BT, 10LS-0)
#
# Sengaja DIHITUNG, bukan diunduh. Berkas DMI siap pakai yang beredar
# (PSL, BoM) tidak stabil dapat diakses dari lingkungan ini, sedangkan
# SST OISST sudah ada di Earth Engine. Konsekuensinya nilai kita bisa
# berbeda tipis dari DMI resmi karena beda dasar iklim dan jendela rerata;
# itu dinyatakan apa adanya di keluaran, bukan disembunyikan.
#
# IOD positif  -> uap ditarik menjauh dari Indonesia -> KERING
# IOD negatif  -> uap terkumpul di timur Samudra Hindia -> BASAH
DMI_KLIM_AWAL, DMI_KLIM_AKHIR = "1991-01-01", "2020-12-31"
DMI_JENDELA_HARI = 30


def dmi_hitung():
    """Kembalikan dict DMI. Memanggil GEE; pemanggil wajib memastikan init."""
    barat = ee.Geometry.Rectangle([50, -10, 70, 10])
    timur = ee.Geometry.Rectangle([90, -10, 110, 0])
    sst = ee.ImageCollection("NOAA/CDR/OISST/V2_1").select("sst")

    kini_akhir = ee.Date(dt.datetime.utcnow().isoformat())
    kini_awal = kini_akhir.advance(-DMI_JENDELA_HARI, "day")
    bulan = kini_akhir.get("month")

    def rerata(col, kotak):
        return ee.Image(col.mean()).multiply(0.01).reduceRegion(
            reducer=ee.Reducer.mean(), geometry=kotak, scale=27830,
            bestEffort=True, maxPixels=int(1e9)).get("sst")

    kini_col = sst.filterDate(kini_awal, kini_akhir)
    klim_col = (sst.filterDate(DMI_KLIM_AWAL, DMI_KLIM_AKHIR)
                   .filter(ee.Filter.calendarRange(bulan, bulan, "month")))

    nilai = ee.Dictionary({
        "barat_kini": rerata(kini_col, barat), "barat_klim": rerata(klim_col, barat),
        "timur_kini": rerata(kini_col, timur), "timur_klim": rerata(klim_col, timur),
        "n_kini": kini_col.size(),
    }).getInfo()

    if any(nilai.get(k) is None for k in
           ("barat_kini", "barat_klim", "timur_kini", "timur_klim")):
        raise ValueError("SST OISST belum lengkap untuk jendela ini")

    a_barat = nilai["barat_kini"] - nilai["barat_klim"]
    a_timur = nilai["timur_kini"] - nilai["timur_klim"]
    dmi = a_barat - a_timur
    if dmi >= 0.4:
        kelas, arah = "IOD positif (menekan hujan Indonesia)", -1
    elif dmi <= -0.4:
        kelas, arah = "IOD negatif (menambah hujan Indonesia)", +1
    else:
        kelas, arah = "IOD netral", 0
    return {"ok": True, "dmi": round(dmi, 3),
            "anomali_barat": round(a_barat, 3), "anomali_timur": round(a_timur, 3),
            "kelas": kelas, "arah": arah,
            "jendela_hari": DMI_JENDELA_HARI, "n_citra": nilai.get("n_kini"),
            "klimatologi": "%s..%s, bulan sama" % (DMI_KLIM_AWAL[:4], DMI_KLIM_AKHIR[:4]),
            "sumber": "NOAA/CDR/OISST/V2_1 (dihitung sendiri, bukan DMI resmi)",
            "fetched": dt.datetime.utcnow().isoformat() + "Z"}


DMI_TTL_H = float(os.environ.get("DMI_TTL_H", "24"))


def dmi_get(force=False):
    if not force:
        simpan = gcs_read("iod_dmi")
        if simpan and _age_hours(simpan.get("fetched", "")) < DMI_TTL_H:
            return simpan
    try:
        d = dmi_hitung()
        gcs_write("iod_dmi", d)
        return d
    except Exception as e:
        lama = gcs_read("iod_dmi")
        if lama:
            lama["stale"] = True; lama["error"] = str(e)
            return lama
        return {"ok": False, "error": str(e),
                "note": "DMI tak terhitung — konteks IOD tidak dipakai."}


# ── GFS · ramalan 24/48/72 jam ───────────────────────────────────────────
# Lead TIDAK lagi dipatok 24/48/72. Siklus GFS 00Z + 24 jam jatuh pukul
# 08:00 WITA — pagi, saat lapisan batas belum berkembang dan stratus masih
# menutup langit. Itu sebabnya uji "lapisan batas >= 800 m" selalu gagal
# (terukur 341 m) dan tutupan awan terbaca 100% berdampingan dengan hujan
# nol: keduanya gejala pagi hari, bukan gambaran hari itu.
#
# Baik puncak bahaya kebakaran maupun jendela penyemaian awan terjadi
# menjelang sore. Karena itu lead dihitung supaya waktu berlakunya jatuh
# pada 06 UTC = 14:00 WITA, berapa pun jam siklusnya.
GFS_JAM_TARGET_UTC = int(os.environ.get("GFS_JAM_TARGET_UTC", "6"))   # 14:00 WITA
GFS_MIN_LEAD = 6          # jangan menyajikan "ramalan" yang jaraknya < 6 jam
GFS_N_HARI = 3            # sore ini / besok / lusa
GFS_SKALA = 27830
WITA = 8                  # UTC+8


def gfs_lead_sore(jam_siklus_utc):
    """Jam ramalan yang waktu berlakunya jatuh pukul GFS_JAM_TARGET_UTC."""
    d = (GFS_JAM_TARGET_UTC - jam_siklus_utc) % 24
    if d < GFS_MIN_LEAD:
        d += 24
    return tuple(d + 24 * k for k in range(GFS_N_HARI))


def gfs_ramalan(roi):
    """Ambil satu siklus GFS yang LENGKAP dan ringkas untuk ROI.

    KEGAGALAN 2026-09-05: siklus dengan creation_time terbesar belum tentu
    sudah memuat jam ramalan yang kita minta — GFS masuk ke katalog secara
    bertahap. first() lalu mengembalikan null dan bandNames() menolak dengan
        "Image.bandNames: Parameter 'image' is required and may not be null."
    Karena itu siklus dipilih dari yang terbaru MUNDUR, sampai ditemukan yang
    memuat seluruh lead. Lead yang tetap kosong dilaporkan null, bukan
    menjatuhkan seluruh permintaan.

    Akumulasi hujan mengikuti dokumentasi GFS: band total_precipitation_surface
    menyimpan akumulasi 1-6 jam sebelumnya menurut ((F-1) %% 6) + 1. Supaya
    tidak tercacah ganda, hanya forecast_hours kelipatan 6 yang dijumlahkan."""
    col = ee.ImageCollection("NOAA/GFS0P25")
    kini = ee.Date(dt.datetime.utcnow().isoformat())
    baru_col = col.filterDate(kini.advance(-3, "day"), kini.advance(1, "day"))

    cts = baru_col.aggregate_array("creation_time").getInfo() or []
    cts = sorted({float(c) for c in cts}, reverse=True)
    if not cts:
        raise ValueError("koleksi GFS kosong untuk tiga hari terakhir")

    kandidat = cts[:8]
    lead_kandidat = [gfs_lead_sore(dt.datetime.utcfromtimestamp(c / 1000.0).hour)
                     for c in kandidat]
    jumlah = ee.List([
        baru_col.filter(ee.Filter.eq("creation_time", c))
                .filter(ee.Filter.inList("forecast_hours", list(lead))).size()
        for c, lead in zip(kandidat, lead_kandidat)
    ]).getInfo()
    pilih = next((i for i, n in enumerate(jumlah) if n >= GFS_N_HARI), 0)
    ct, LEAD = kandidat[pilih], lead_kandidat[pilih]
    siklus = baru_col.filter(ee.Filter.eq("creation_time", ct))
    t_siklus = dt.datetime.utcfromtimestamp(ct / 1000.0)

    kosong = {"hujan_mm": None, "uap_mm": None, "rh": None, "awan": None,
              "pbl_m": None, "haines": None, "angin_kmj": None, "angin_dari_deg": None}
    band = ["precipitable_water_entire_atmosphere",
            "relative_humidity_2m_above_ground",
            "total_cloud_cover_entire_atmosphere",
            "u_component_of_wind_10m_above_ground",
            "v_component_of_wind_10m_above_ground",
            "planetary_boundary_layer_height", "haines_index"]

    keluar = {"lead": list(LEAD)}
    for jam in LEAD:
        pot = siklus.filter(ee.Filter.eq("forecast_hours", jam))
        berlaku = t_siklus + dt.timedelta(hours=jam)
        cap = {"berlaku_utc": berlaku.isoformat() + "Z",
               "berlaku_wita": (berlaku + dt.timedelta(hours=WITA)).strftime("%Y-%m-%d %H:%M")}
        if pot.size().getInfo() == 0:
            keluar["j%d" % jam] = dict(kosong, catatan="jam ramalan belum tersedia", **cap)
            continue
        img = ee.Image(pot.first())
        ada = img.bandNames().getInfo() or []
        pilih = [b for b in band if b in ada]

        jam6 = list(range(6, jam + 1, 6))
        hj = siklus.filter(ee.Filter.inList("forecast_hours", jam6)) \
                   .select("total_precipitation_surface")
        citra = img.select(pilih)
        if hj.size().getInfo() > 0:
            citra = citra.addBands(hj.sum())

        d = citra.reduceRegion(reducer=ee.Reducer.mean(), geometry=roi,
                               scale=GFS_SKALA, bestEffort=True,
                               maxPixels=int(1e9)).getInfo() or {}
        u = d.get("u_component_of_wind_10m_above_ground")
        v = d.get("v_component_of_wind_10m_above_ground")
        laju = arah = None
        if u is not None and v is not None:
            laju = round(math.hypot(u, v) * 3.6, 1)                  # km/jam
            arah = round((math.degrees(math.atan2(-u, -v)) + 360) % 360, 0)
        keluar["j%d" % jam] = dict(cap, **{
            "hujan_mm": _bulat(d.get("total_precipitation_surface")),
            "uap_mm": _bulat(d.get("precipitable_water_entire_atmosphere")),
            "rh": _bulat(d.get("relative_humidity_2m_above_ground")),
            "awan": _bulat(d.get("total_cloud_cover_entire_atmosphere")),
            "pbl_m": _bulat(d.get("planetary_boundary_layer_height"), 0),
            "haines": _bulat(d.get("haines_index")),
            "angin_kmj": laju, "angin_dari_deg": arah,
        })
    keluar["siklus"] = t_siklus.isoformat() + "Z"
    keluar["jam_target_wita"] = "%02d:00" % ((GFS_JAM_TARGET_UTC + WITA) % 24)
    return keluar


def _bulat(v, n=1):
    return None if v is None else round(v, n)


# ── Skor peluang hujan ───────────────────────────────────────────────────
# Heuristik, BELUM terkalibrasi. Empat suku cuaca dijumlahkan berbobot,
# lalu digeser oleh konteks makro (MJO, IOD, ENSO). Setiap suku dibuka
# nilainya di keluaran supaya bisa diperiksa, bukan kotak hitam.
def _tangga(v, rendah, tinggi):
    """0 di bawah `rendah`, 100 di atas `tinggi`, linear di antaranya."""
    if v is None:
        return None
    if v <= rendah:
        return 0.0
    if v >= tinggi:
        return 100.0
    return (v - rendah) / (tinggi - rendah) * 100.0


HUJAN_BOBOT = {"hujan": 45, "uap": 20, "rh": 20, "awan": 15}


def skor_hujan(f, mjo=None, dmi=None, oni=None):
    komp = {
        "hujan": _tangga(f.get("hujan_mm"), 0.0, 20.0),
        "uap":   _tangga(f.get("uap_mm"), 35.0, 50.0),
        "rh":    _tangga(f.get("rh"), 50.0, 85.0),
        "awan":  _tangga(f.get("awan"), 20.0, 80.0),
    }
    s = w = 0.0
    for k, v in komp.items():
        if v is None:
            continue
        s += v * HUJAN_BOBOT[k]; w += HUJAN_BOBOT[k]
    dasar = s / w if w else None
    if dasar is None:
        return {"skor": None, "komponen": komp, "geseran": {}, "cakupan": 0}

    geser = {}
    if mjo and mjo.get("ok") and mjo.get("dipakai"):
        geser["mjo"] = 8 * mjo.get("arah", 0)
    if dmi and dmi.get("ok"):
        geser["iod"] = 8 * dmi.get("arah", 0)
    if oni and oni.get("ok") and oni.get("oni") is not None:
        o = oni["oni"]
        geser["enso"] = -8 if o >= 1.0 else (-4 if o >= 0.5 else (5 if o <= -0.5 else 0))
    total = dasar + sum(geser.values())
    return {"skor": int(round(max(0, min(100, total)))),
            "dasar": round(dasar, 1), "komponen": komp, "geseran": geser,
            "cakupan": round(w / sum(HUJAN_BOBOT.values()), 2)}


# ── Penapis TMC (hujan buatan) ───────────────────────────────────────────
# INI BUKAN LAMPU HIJAU. Keputusan menyemai adalah wewenang BMKG/BRIN dan
# menuntut radiosonde, CAPE, tinggi dasar & puncak awan dari radar — yang
# tidak dimiliki sistem ini. Yang disajikan hanyalah penapis awal: apakah
# kondisi hari itu pantas dikaji lebih lanjut.
TMC_SYARAT = [
    ("uap",   "Uap tersedia (PW ≥ 45 mm)",            lambda f: (f.get("uap_mm"), 45, "≥")),
    ("rh",    "Lapisan bawah lembap (RH ≥ 60%)",      lambda f: (f.get("rh"), 60, "≥")),
    ("awan",  "Ada awan yang bisa disemai (30–90%)",  lambda f: (f.get("awan"), (30, 90), "antara")),
    ("pbl",   "Lapisan batas cukup tebal (≥ 800 m)",  lambda f: (f.get("pbl_m"), 800, "≥")),
    ("angin", "Angin tidak kencang (≤ 25 km/j)",      lambda f: (f.get("angin_kmj"), 25, "≤")),
    ("sistem","Ada sistem hujan (≥ 0,5 mm/24j)",      lambda f: (f.get("hujan_mm"), 0.5, "≥")),
]


def skor_tmc(f):
    rinci, lulus, terukur = [], 0, 0
    for kode, teks, amb in TMC_SYARAT:
        nilai, batas, jenis = amb(f)
        if nilai is None:
            rinci.append({"kode": kode, "syarat": teks, "nilai": None, "lulus": None})
            continue
        terukur += 1
        if jenis == "≥":
            ok = nilai >= batas
        elif jenis == "≤":
            ok = nilai <= batas
        else:
            ok = batas[0] <= nilai <= batas[1]
        lulus += 1 if ok else 0
        rinci.append({"kode": kode, "syarat": teks, "nilai": nilai,
                      "batas": batas, "lulus": bool(ok)})
    if not terukur:
        return {"skor": None, "status": "data tidak cukup", "rinci": rinci}
    skor = int(round(lulus / terukur * 100))
    if skor >= 80:
        status = "kondisi mendukung — layak dikaji BMKG/BRIN"
    elif skor >= 50:
        status = "sebagian syarat terpenuhi — perlu kajian lanjut"
    else:
        status = "tidak mendukung"
    return {"skor": skor, "lulus": lulus, "terukur": terukur,
            "status": status, "rinci": rinci,
            "peringatan": ("Penapis awal, bukan keputusan operasi. Penyemaian "
                           "menuntut radiosonde, CAPE, dan pengamatan radar; "
                           "wewenangnya ada pada BMKG/BRIN.")}


# ── Endpoint ─────────────────────────────────────────────────────────────
HUJAN_TTL_H = float(os.environ.get("HUJAN_TTL_H", "6"))   # GFS terbit 4x sehari


def _hujan_payload(rid):
    conf = region_conf(rid)
    roi = roi_geometry(conf)
    ram = gfs_ramalan(roi)
    mjo = mjo_get()
    dmi = dmi_get()
    oni = oni_get()
    LEAD = ram["lead"]
    hasil = {}
    for jam in LEAD:
        f = ram["j%d" % jam]
        hasil["j%d" % jam] = {"cuaca": f,
                              "hujan": skor_hujan(f, mjo, dmi, oni),
                              "tmc": skor_tmc(f)}
    return {"region": rid, "name": conf["name"], "siklus_gfs": ram["siklus"],
            "jam_target_wita": ram.get("jam_target_wita"),
            "lead": LEAD, "ramalan": hasil,
            "makro": {"enso": oni, "mjo": mjo, "iod": dmi},
            "catatan": ("GFS berpiksel 27,8 km dan tidak menyelesaikan awan "
                        "konvektif tropis. Baca sebagai jendela peluang "
                        "harian se-kabupaten, bukan hujan titik. Jam ramalan "
                        "dipilih agar berlaku sekitar pukul 14:00 WITA, saat "
                        "lapisan batas berkembang dan penyemaian dilakukan."),
            "generated": dt.datetime.utcnow().isoformat() + "Z"}


@app.get("/api/hujan/{region}")
def hujan(region: str, force: int = Query(0)):
    conf = region_conf(region)
    if conf is None:
        return JSONResponse({"error": f"region '{region}' tidak dikenal",
                             "tersedia": list(REG.REGION_IDS)}, status_code=404)
    kunci = "hujan_%s" % region
    if not force:
        c = gcs_read(kunci)
        if c and _age_hours(c.get("generated", "")) < HUJAN_TTL_H:
            c["cached"] = True
            return c
    if not _INIT["done"]:
        try:
            init_ee(); _INIT["done"] = True
        except Exception as e:
            st = gcs_read(kunci)
            if st:
                st["cached"] = True; return st
            return JSONResponse({"error": f"Earth Engine belum siap: {e}"}, status_code=503)
    try:
        payload = _hujan_payload(region)
    except Exception as e:
        st = gcs_read(kunci)
        if st:
            st["cached"] = True; st["error"] = str(e); return st
        return JSONResponse({"error": f"Komputasi hujan gagal: {e}"}, status_code=500)
    gcs_write(kunci, payload)
    return payload


@app.get("/api/mjo")
def mjo_ep(force: int = Query(0)):
    return mjo_get(force=bool(force))


@app.get("/api/iod")
def iod_ep(force: int = Query(0)):
    if not _INIT["done"]:
        try:
            init_ee(); _INIT["done"] = True
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return dmi_get(force=bool(force))


# ── Haines · disiapkan, BELUM disambungkan ke IndeksKAR ──────────────────
# Indeks Haines (2-6) mengukur kekeringan dan ketidakstabilan lapisan bawah
# atmosfer: 2-3 rendah, 4 sedang, 5-6 tinggi. Nilainya ikut terbawa pada
# keluaran /api/hujan di atas.
#
# Fungsi skor sudah siap, tetapi indikatornya SENGAJA belum dimasukkan ke
# KIND: penempatan dimensinya masih perlu ditegaskan (NDMI & SPI berada di
# dimensi K, bukan T) dan bobotnya adalah keputusan model, bukan keputusan
# saya. Bila sudah tetap, cukup tambahkan satu baris pada KIND.
def _sc_haines(v):
    return 0 if v <= 2 else 1 if v < 4 else 2 if v < 5 else 3 if v < 6 else 4
