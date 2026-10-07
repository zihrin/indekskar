# -*- coding: utf-8 -*-
"""
build_static.py — menjalankan backend IndeksKAR (app.py) sebagai skrip batch
untuk GitHub Actions, lalu menulis hasil tiap endpoint sebagai file JSON
statis yang disajikan GitHub Pages. Pengganti Cloud Run + GCS, tanpa biaya.

Struktur keluaran (OUT_DIR, default ../site) meniru URL API lama, sehingga
frontend cukup mengganti KD_API:

  api/indekskar/<wilayah>     (dulu GET /api/indekskar/<wilayah>)
  api/provinsi                (dulu GET /api/provinsi)
  api/hujan/<wilayah>         (dulu GET /api/hujan/<wilayah>)
  api/enso, api/mjo, api/iod, api/regions, api/versi, api/history/kabupaten
  health, status.json         (ringkasan sukses/gagal tiap langkah)
  store/                      (pengganti bucket GCS: cache, riwayat, arsip PNG)

Mode (argumen pertama atau env MODE):
  full   — semua: provinsi, 14 wilayah, hujan, ENSO/MJO/IOD, arsip harian
  hujan  — hanya jendela hujan (GFS) + MJO/IOD, untuk pembaruan siang hari

Variabel lingkungan:
  EE_PROJECT, EE_SERVICE_ACCOUNT, EE_KEY_FILE  — kredensial Earth Engine
  OUT_DIR           — folder situs (berisi store/ dari jalankan sebelumnya)
  ARSIP_KEEP_DAYS   — berapa hari arsip PNG disimpan (default 30)
  ONLY              — opsional, daftar wilayah dipisah koma (uji coba)
"""
import os, sys, json, time, shutil, datetime as dt, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.abspath(os.environ.get("OUT_DIR", os.path.join(HERE, "..", "site")))
STORE = os.path.join(OUT, "store")
MODE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "full")).strip().lower()
KEEP = int(os.environ.get("ARSIP_KEEP_DAYS", "30"))

os.environ.setdefault("GCS_BUCKET", "local")


# ── Pengganti google.cloud.storage: bucket = folder lokal STORE ─────────────
class _Blob:
    def __init__(self, path):
        self.path = path
        self.cache_control = None

    def exists(self):
        return os.path.exists(self.path)

    def download_as_text(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def upload_from_string(self, data, content_type=None):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        mode = "wb" if isinstance(data, bytes) else "w"
        with open(self.path, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as f:
            f.write(data)

    def upload_from_filename(self, src, content_type=None):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        shutil.copyfile(src, self.path)


class _Bucket:
    def blob(self, name):
        return _Blob(os.path.join(STORE, name))


class LocalStorageClient:
    def bucket(self, name):
        return _Bucket()


# ── Muat backend dan pasang penyimpanan lokal ───────────────────────────────
import app as A  # noqa: E402

A._GCS = LocalStorageClient()
A.GCS_BUCKET = "local"
os.makedirs(STORE, exist_ok=True)

STATUS = {"mode": MODE, "mulai": dt.datetime.utcnow().isoformat() + "Z",
          "versi_backend": A.VERSI, "langkah": []}


def _body(res):
    """dict/list biasa, atau JSONResponse FastAPI → (status_code, data)."""
    if hasattr(res, "status_code") and hasattr(res, "body"):
        return res.status_code, json.loads(res.body.decode("utf-8"))
    return 200, res


def tulis(rel, data):
    p = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)
    # Salinan berekstensi .json untuk alat lain (spreadsheet, skrip laporan).
    shutil.copyfile(p, p + ".json")


def langkah(rel, fn):
    """Jalankan satu endpoint; tulis hanya bila sukses. Bila gagal, file
       versi sebelumnya di situs dibiarkan (dashboard tetap menampilkan
       data terakhir yang sah, ditandai oleh 'generated')."""
    t0 = time.time()
    try:
        code, data = _body(fn())
        ok = code == 200 and not (isinstance(data, dict) and data.get("error") and not data.get("cached"))
        if ok:
            tulis(rel, data)
        info = {"path": rel, "ok": ok, "http": code, "detik": round(time.time() - t0, 1)}
        if isinstance(data, dict):
            if data.get("cached"):
                info["catatan"] = "pakai cache lama (komputasi gagal)"
            if data.get("error"):
                info["error"] = str(data["error"])[:300]
    except Exception as e:
        traceback.print_exc()
        info = {"path": rel, "ok": False, "error": str(e)[:300], "detik": round(time.time() - t0, 1)}
    STATUS["langkah"].append(info)
    print(("OK  " if info["ok"] else "GAGAL ") + rel, "(%.1fs)" % info["detik"], info.get("error", ""), flush=True)


def pangkas_arsip():
    root = os.path.join(STORE, os.environ.get("ARSIP_PREFIX", "arsip"))
    if not os.path.isdir(root):
        return
    batas = dt.date.today() - dt.timedelta(days=KEEP)
    for y in os.listdir(root):
        for m in os.listdir(os.path.join(root, y)):
            for d in os.listdir(os.path.join(root, y, m)):
                try:
                    if dt.date(int(y), int(m), int(d)) < batas:
                        shutil.rmtree(os.path.join(root, y, m, d), ignore_errors=True)
                except ValueError:
                    pass


def main():
    A.init_ee()
    A._INIT["done"] = True
    print("Earth Engine siap · proyek", os.environ.get("EE_PROJECT"), "· mode", MODE, flush=True)

    rids = list(A.REG.REGION_IDS)
    if os.environ.get("ONLY"):
        rids = [r.strip() for r in os.environ["ONLY"].split(",") if r.strip()]

    if MODE == "full":
        langkah("api/enso", lambda: A.enso(force=1))
        langkah("api/provinsi", lambda: A.provinsi(force=1))
        for rid in rids:
            langkah("api/indekskar/" + rid, lambda rid=rid: A.indekskar(rid, force=1))

    for rid in rids:
        langkah("api/hujan/" + rid, lambda rid=rid: A.hujan(rid, force=1))
    langkah("api/mjo", lambda: A.mjo_ep(force=1))
    langkah("api/iod", lambda: A.iod_ep(force=1))

    if MODE == "full":
        langkah("api/arsip", lambda: A.arsip(tanggal=dt.date.today().isoformat(), force=0))
        pangkas_arsip()

    langkah("api/history/kabupaten", A.history_kabupaten)
    langkah("api/regions", A.regions_list)
    langkah("api/versi", A.versi)
    tulis("health", {"ok": True, "ee": True, "static": True,
                     "generated": dt.datetime.utcnow().isoformat() + "Z"})

    STATUS["selesai"] = dt.datetime.utcnow().isoformat() + "Z"
    STATUS["gagal"] = sum(1 for s in STATUS["langkah"] if not s["ok"])
    with open(os.path.join(OUT, "status.json"), "w", encoding="utf-8") as f:
        json.dump(STATUS, f, ensure_ascii=False, indent=1)
    print("Selesai: %d langkah, %d gagal" % (len(STATUS["langkah"]), STATUS["gagal"]))
    # Gagal total (tidak ada satu pun yang berhasil) → tandai job merah.
    if STATUS["langkah"] and STATUS["gagal"] == len(STATUS["langkah"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
