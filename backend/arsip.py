# -*- coding: utf-8 -*-
"""
arsip.py — penggambar arsip harian IndeksKAR

Menghasilkan, untuk satu tanggal:
  · 1 PNG peta provinsi   — choropleth 147 kecamatan
  · N PNG peta wilayah    — grid piksel per kabupaten/kota + KHDTK
  · 1 CSV tabel tren      — riwayat harian per kabupaten
  · 1 PNG tabel tren      — 14 hari terakhir, siap tempel ke laporan

Modul ini murni penggambar: ia TIDAK memanggil Earth Engine dan tidak
menyentuh jaringan. Semua masukan berupa payload yang sudah dihitung
app.py. Dengan begitu ia cepat, dapat diuji tanpa GEE, dan tidak pernah
menjadi penyebab kegagalan perhitungan.

Ketergantungan: matplotlib (sudah membawa font DejaVu, jadi tidak perlu
font sistem di dalam container).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
from typing import Any, Dict, List, Optional, Sequence

import matplotlib
matplotlib.use("Agg")                    # tanpa layar — wajib di Cloud Run
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon, Rectangle
from matplotlib.collections import PatchCollection
from matplotlib.lines import Line2D

# Warna tingkat — SAMA persis dengan LV pada frontend, supaya arsip dan
# tampilan web tidak pernah berbeda arti.
LV_WARNA = ["#16a34a", "#d97706", "#ea580c", "#dc2626"]
LV_NAMA = ["TERKENDALI", "RENTAN", "KRITIS", "KOLAPS"]
WARNA_KOSONG = "#e5e7eb"


def _lv(total: Optional[float]) -> Optional[int]:
    if total is None:
        return None
    return 0 if total < 25 else 1 if total < 50 else 2 if total < 75 else 3


def _polys(geom: Dict[str, Any]) -> List[Any]:
    if not geom:
        return []
    return [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]


def _kop(ax, judul: str, sub: str, tanggal: str, enso: Optional[Dict[str, Any]]):
    """Kepala gambar yang seragam untuk semua peta.

    Judul dan sub-judul digambar sebagai teks biasa, bukan set_title(),
    supaya jaraknya tetap dalam satuan sumbu. Dengan set_title() jaraknya
    dalam poin, sehingga pada gambar tinggi ia menempel dan menumpuk."""
    ax.text(0, 1.052, judul, transform=ax.transAxes, fontsize=13,
            fontweight="bold", va="bottom", ha="left", color="#111827")
    baris = sub
    if enso and enso.get("ok"):
        baris += "  ·  ENSO %s (ONI %+.2f, ×%.2f)" % (
            enso.get("kelas", "-"), enso.get("oni", 0), enso.get("multiplier", 1))
    ax.text(0, 1.016, baris, transform=ax.transAxes, fontsize=8, color="#5b6675",
            va="bottom", ha="left")
    ax.text(1, 1.016, tanggal, transform=ax.transAxes, fontsize=8, color="#94a0b0",
            va="bottom", ha="right")


def _legenda(ax, tambah_kosong=True):
    item = [Line2D([0], [0], marker="s", linestyle="", markersize=9,
                   markerfacecolor=LV_WARNA[i], markeredgecolor="none",
                   label="%s (%s)" % (LV_NAMA[i], ["<25", "25–49", "50–74", "≥75"][i]))
            for i in range(4)]
    if tambah_kosong:
        item.append(Line2D([0], [0], marker="s", linestyle="", markersize=9,
                           markerfacecolor=WARNA_KOSONG, markeredgecolor="none",
                           label="tanpa data"))
    ax.legend(handles=item, loc="lower left", bbox_to_anchor=(0, -0.10),
              ncol=5, frameon=False, fontsize=8, handletextpad=0.4,
              columnspacing=1.4)


# ─────────────────────────────────────────────────────────────────────────
# 1 · Peta provinsi — choropleth per kecamatan
# ─────────────────────────────────────────────────────────────────────────

def peta_provinsi(prov_payload: Dict[str, Any],
                  kec_fitur: Sequence[Dict[str, Any]],
                  path: str, tanggal: str,
                  enso: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """kec_fitur: daftar fitur GeoJSON kecamatan (punya properties.nama_kec
       & properties.kab_name). Dicocokkan ke payload berdasarkan nama."""
    kecs = prov_payload.get("kecamatan") or []
    if not kecs or not kec_fitur:
        return None

    # Kunci ganda (nama, kabupaten) — nama kecamatan tidak unik se-provinsi.
    skor = {}
    for k in kecs:
        skor[(str(k.get("name", "")).strip().lower(),
              str(k.get("kab", "")).strip().lower())] = k
    hanya_nama = {}
    for k in kecs:
        hanya_nama.setdefault(str(k.get("name", "")).strip().lower(), k)

    fig, ax = plt.subplots(figsize=(10, 11), dpi=150)
    patch, warna = [], []
    cocok = 0
    for f in kec_fitur:
        p = f.get("properties", {})
        kunci = (str(p.get("nama_kec", "")).strip().lower(),
                 str(p.get("kab_name", "")).strip().lower())
        rec = skor.get(kunci) or hanya_nama.get(kunci[0])
        lv = _lv(rec.get("total")) if rec else None
        if rec:
            cocok += 1
        for rings in _polys(f.get("geometry")):
            if not rings or len(rings[0]) < 3:
                continue
            patch.append(MplPolygon(rings[0], closed=True))
            warna.append(LV_WARNA[lv] if lv is not None else WARNA_KOSONG)

    if not patch:
        plt.close(fig)
        return None
    ax.add_collection(PatchCollection(patch, facecolors=warna,
                                      edgecolors="#ffffff", linewidths=0.35))
    ax.autoscale_view()
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)

    n_kritis = sum(1 for k in kecs if (_lv(k.get("total")) or 0) >= 2)
    _kop(ax, "IndeksKAR Kalimantan Selatan",
         "%d kecamatan  ·  %d pada tingkat KRITIS atau lebih  ·  %d tercocokkan"
         % (len(kecs), n_kritis, cocok), tanggal, enso)
    _legenda(ax)
    ax.text(0, -0.055, "Nilai per kecamatan sebelum pengali ENSO diterapkan.",
            transform=ax.transAxes, fontsize=7.5, color="#94a0b0")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


# ─────────────────────────────────────────────────────────────────────────
# 2 · Peta wilayah — grid piksel
# ─────────────────────────────────────────────────────────────────────────

def peta_wilayah(payload: Dict[str, Any], path: str, tanggal: str,
                 skor_fn, enso: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """skor_fn(vals) -> (K, T, P, total, lv) — dioper dari app.score_ik agar
       arsip memakai rumus yang sama persis dengan API."""
    piks = payload.get("pixels") or []
    bb = payload.get("bbox") or {}
    nx, ny = payload.get("nx"), payload.get("ny")
    if not piks or not nx or not ny or not bb:
        return None

    dlat = (bb["n"] - bb["s"]) / ny
    dlng = (bb["e"] - bb["w"]) / nx
    lebar = (bb["e"] - bb["w"]); tinggi = (bb["n"] - bb["s"])
    rasio = max(0.45, min(2.2, lebar / tinggi if tinggi else 1))
    fig, ax = plt.subplots(figsize=(8.2, 8.2 / rasio), dpi=150)

    isi = 0
    cacah = [0, 0, 0, 0]
    for p in piks:
        if p.get("void"):
            continue
        vals = p.get("vals") or {}
        if not vals:
            continue
        try:
            _K, _T, _P, total, lv = skor_fn(vals)
        except Exception:
            continue
        isi += 1
        cacah[lv] += 1
        s = bb["n"] - (p["r"] + 1) * dlat
        w = bb["w"] + p["c"] * dlng
        ax.add_patch(Rectangle((w, s), dlng, dlat,
                               facecolor=LV_WARNA[lv], edgecolor="none"))

    if not isi:
        plt.close(fig)
        return None

    # Garis batas wilayah di atas grid
    for rings in (payload.get("boundary") or []):
        if rings and len(rings[0]) >= 3:
            ax.add_patch(MplPolygon(rings[0], closed=True, fill=False,
                                    edgecolor="#111827", linewidth=0.9))

    ax.set_xlim(bb["w"], bb["e"]); ax.set_ylim(bb["s"], bb["n"])
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)

    km = dlat * 111
    ukur = ("%.0f m" % (km * 1000)) if km < 1 else ("%.1f km" % km).replace(".", ",")
    mode = "GEE real-time" if payload.get("cached") is not None else "pra-hitung"
    _kop(ax, "IndeksKAR — %s" % payload.get("name", payload.get("region", "-")),
         "%d piksel  ·  grid %d×%d (sel ≈ %s)  ·  %s"
         % (isi, nx, ny, ukur, mode), tanggal, enso)
    _legenda(ax, tambah_kosong=False)
    ax.text(0, -0.055,
            "Sebaran: %s" % "  ".join("%s %d" % (LV_NAMA[i], cacah[i])
                                      for i in range(4) if cacah[i]),
            transform=ax.transAxes, fontsize=7.5, color="#94a0b0")
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


# ─────────────────────────────────────────────────────────────────────────
# 3 · Tabel tren — CSV & PNG
# ─────────────────────────────────────────────────────────────────────────

def tren_csv(history_kab: Dict[str, Any], path: str) -> str:
    hari = sorted(history_kab.keys())
    kabs = sorted({k for d in history_kab.values() for k in d.keys()})
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["tanggal", "kabupaten", "total", "K", "T", "P", "tingkat"])
        for d in hari:
            for k in kabs:
                e = history_kab.get(d, {}).get(k)
                if not e:
                    continue
                lv = _lv(e.get("total"))
                w.writerow([d, k, e.get("total"), e.get("K"), e.get("T"),
                            e.get("P"), LV_NAMA[lv] if lv is not None else ""])
    return path


def tren_png(history_kab: Dict[str, Any], path: str, tanggal: str,
             n_hari: int = 14) -> Optional[str]:
    hari = sorted(history_kab.keys())[-n_hari:]
    if not hari:
        return None
    kabs = sorted({k for d in history_kab.values() for k in d.keys()})
    if not kabs:
        return None
    # Urut menurun berdasarkan hari terakhir
    akhir = history_kab.get(hari[-1], {})
    kabs.sort(key=lambda k: -(akhir.get(k, {}).get("total") or 0))

    tinggi = max(3.0, 0.42 * len(kabs) + 1.9)
    lebar = max(7.0, 0.86 * len(hari) + 3.6)
    fig, ax = plt.subplots(figsize=(lebar, tinggi), dpi=150)
    ax.set_xlim(0, len(hari)); ax.set_ylim(0, len(kabs))
    ax.invert_yaxis()

    for j, k in enumerate(kabs):
        for i, d in enumerate(hari):
            e = history_kab.get(d, {}).get(k)
            v = e.get("total") if e else None
            lv = _lv(v)
            ax.add_patch(Rectangle((i, j), 1, 1,
                                   facecolor=LV_WARNA[lv] if lv is not None else WARNA_KOSONG,
                                   edgecolor="white", linewidth=1.2))
            if v is not None:
                ax.text(i + 0.5, j + 0.5, "%d" % v, ha="center", va="center",
                        fontsize=8.5, color="white", fontweight="bold")

    ax.set_yticks([j + 0.5 for j in range(len(kabs))])
    ax.set_yticklabels(kabs, fontsize=8.5)
    ax.set_xticks([i + 0.5 for i in range(len(hari))])
    ax.set_xticklabels([d[5:] for d in hari], fontsize=8, rotation=0)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)

    _kop(ax, "Tren IndeksKAR Harian per Kabupaten",
         "%d hari terakhir  ·  %d kabupaten/kota  ·  nilai sebelum pengali ENSO"
         % (len(hari), len(kabs)), tanggal, None)
    _legenda(ax)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


# ─────────────────────────────────────────────────────────────────────────
# 4 · Perakit arsip harian
# ─────────────────────────────────────────────────────────────────────────

def bangun_arsip(dirpath: str, tanggal: str, prov_payload, kec_fitur,
                 region_payloads: Dict[str, Any], history_kab, skor_fn,
                 enso=None) -> Dict[str, Any]:
    """Menulis seluruh berkas arsip ke dirpath. Mengembalikan manifes.

    Setiap penggambar dibungkus try/except sendiri: satu gambar gagal tidak
    boleh membatalkan arsip hari itu."""
    os.makedirs(dirpath, exist_ok=True)
    hasil, galat = [], []

    def coba(nama, fn):
        try:
            p = fn()
            if p:
                hasil.append({"berkas": os.path.basename(p), "jenis": nama,
                              "ukuran": os.path.getsize(p)})
        except Exception as e:                       # noqa: BLE001
            galat.append({"jenis": nama, "pesan": str(e)[:200]})

    coba("peta_provinsi", lambda: peta_provinsi(
        prov_payload, kec_fitur, os.path.join(dirpath, "peta_provinsi.png"),
        tanggal, enso))

    for rid, pay in (region_payloads or {}).items():
        coba("peta_%s" % rid, (lambda r=rid, p=pay: peta_wilayah(
            p, os.path.join(dirpath, "peta_%s.png" % r), tanggal, skor_fn, enso)))

    coba("tren_csv", lambda: tren_csv(
        history_kab, os.path.join(dirpath, "tren_kabupaten.csv")))
    coba("tren_png", lambda: tren_png(
        history_kab, os.path.join(dirpath, "tren_kabupaten.png"), tanggal))

    return {"tanggal": tanggal, "berkas": hasil, "jumlah": len(hasil),
            "galat": galat, "dibuat": dt.datetime.utcnow().isoformat() + "Z"}
