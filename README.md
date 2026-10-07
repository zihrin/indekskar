# IndeksKAR Kalsel — versi gratis (GitHub Actions + GitHub Pages)

Pengganti backend Cloud Run `indekskar-gee` (project `indekskar-kalsel`, billing mati sejak 3 Okt 2026).
Logika perhitungan **sama persis**: `backend/app.py` adalah app.py revisi Cloud Run terakhir
(`VERSI 2026-09-05c`). Bedanya hanya cara menjalankan dan tempat menyimpan hasil:

| Dulu (berbayar)                                   | Sekarang (gratis)                                          |
|---------------------------------------------------|------------------------------------------------------------|
| Cloud Run FastAPI, dipanggil Cloud Scheduler      | GitHub Actions menjalankan `build_static.py` sesuai jadwal |
| Cache & riwayat di bucket GCS                     | Folder `store/` di cabang `gh-pages`                       |
| `https://indekskar-gee-…run.app/api/indekskar/…`  | `https://<user>.github.io/indekskar-kalsel/api/indekskar/…`|
| Earth Engine project `indekskar-kalsel`           | Earth Engine project `indekskar` (noncommercial, Community)|

## Jadwal

- **05:00 WITA**: perhitungan penuh (provinsi, 13 kab/kota + KHDTK, jendela hujan, ENSO/MJO/IOD, arsip PNG harian)
- **11:00 WITA**: jendela hujan (GFS) saja
- Manual: tab **Actions → IndeksKAR harian → Run workflow**

Catatan: jadwal GitHub bisa terlambat 5–30 menit pada jam sibuk.

## Alamat hasil

- Dashboard Kalsel: `https://<user>.github.io/indekskar-kalsel/`
- API per wilayah: `…/api/indekskar/banjarbaru` (juga `banjarbaru.json`)
- Provinsi: `…/api/provinsi` · Hujan: `…/api/hujan/<wilayah>` · Riwayat: `…/api/history/kabupaten`
- Arsip PNG harian (30 hari terakhir): `…/store/arsip/YYYY/MM/DD/`
- Ringkasan jalannya pekerjaan terakhir: `…/status.json`

Untuk dashboard yang di-hosting di tempat lain, cukup ganti satu baris:

```js
window.KD_API = "https://<user>.github.io/indekskar-kalsel/api/indekskar";
```

## Pengaturan (sekali saja)

1. Secret repo `EE_KEY_JSON` = isi kunci service account `indekskar-bot@indekskar.iam.gserviceaccount.com`.
2. Project `indekskar` terdaftar di Earth Engine sebagai noncommercial (tier Community, tanpa billing).
3. Settings → Pages → Source: *Deploy from a branch* → `gh-pages` / `(root)`.
   (Cabang `gh-pages` baru muncul setelah workflow pertama selesai.)

## Batasan dibanding Cloud Run

- Tombol "Perbarui"/`?force=1` di dashboard tidak lagi memicu perhitungan seketika; data diperbarui sesuai jadwal.
  Untuk memaksa, jalankan workflow secara manual dari tab Actions.
- Kuota EE Community: 150 EECU-jam per bulan. Pantau di Cloud Console → Earth Engine → Quotas.
