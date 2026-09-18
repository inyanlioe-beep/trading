# Trading

Analisa teknikal Binance + screener Indodax + bot auto-trading (paper mode default).

Heuristik EMA/RSI/MACD. **Bukan prediksi. DYOR.**

## Isi

| File | Fungsi |
|---|---|
| `main.py` | FastAPI. Ambil klines Binance, hitung indikator, keluarkan sinyal. Serve `static/index.html`. |
| `static/index.html` | Chart candlestick (lightweight-charts) + panel sinyal. |
| `screener.py` | Scan pasangan IDR Indodax, skor tiap pair, cetak peringkat. |
| `bot.py` | Bot auto-trading Indodax. Paper mode default. |
| `paper_state.json` | State bot: posisi, PnL, riwayat trade. |

## Sinyal

| Komponen | Parameter |
|---|---|
| EMA | 20, 50 |
| MACD | 12 / 26 / 9 |
| RSI | 14 (Wilder) |

BUY: EMA20 > EMA50 + MACD bull cross, RSI < 70. SELL: kebalikannya.
Skor screener = bobot heuristik (EMA, MACD, RSI, rasio volume). `>= SCAN_MIN_SCORE` → BUY.

## Instalasi

```bash
pip install -r requirements.txt
```

## Dashboard

```bash
python main.py                      # http://127.0.0.1:8000
python main.py --selftest           # cek indikator
```

API: `GET /api/klines?symbol=BTCUSDT&interval=1h&limit=300`
(interval: `1m 5m 15m 1h 4h 1d`)

## Screener

```bash
python screener.py                  # top 15 pair IDR
python screener.py --min-vol 5e9    # naikkan ambang likuiditas (IDR 24 jam)
python screener.py --min-vr 1.0     # hanya volume x1.0+ vs rata-rata 20 bar
python screener.py --interval 4h
python screener.py --selftest
```

## Bot

Paper mode default — tidak ada order nyata.

```bash
python bot.py              # loop
python bot.py --once       # satu siklus
python bot.py --selftest
```

Config dari `.env` (lihat `.env.example`). Env OS menang atas `.env`.

| Var | Default | Arti |
|---|---|---|
| `PAPER` | `true` | `false` = order nyata (perlu `INDODAX_KEY`/`INDODAX_SECRET`) |
| `PAIR` | `btc_idr` | Market Indodax |
| `PAIR_MODE` | `manual` | `auto` = ikut pair teratas screener |
| `INTERVAL` | `1h` | Timeframe sinyal |
| `CUT_LOSS_PCT` | `3.0` | Jual paksa di bawah entry |
| `TAKE_PROFIT_PCT` | `6.0` | Jual paksa di atas entry; `0` = mati |
| `BUDGET_IDR` | `1000000` | Ukuran order per beli |
| `POLL_SECONDS` | `30` | Jeda antar siklus |
| `STATE_FILE` | `paper_state.json` | File state |
| `SCAN_MIN_SCORE` | `6` | Skor `>=` ini → BUY |
| `SCAN_SELL_SCORE` | `0` | Skor `<=` ini → SELL |
| `SCAN_MIN_VR` | `1.0` | Rasio volume minimum agar pair lolos |
| `SCAN_MIN_VOL` | `300000000` | Volume IDR 24 jam minimum |
| `SCAN_REFRESH` | `3600` | Detik antar re-scan |

## Keamanan

- `false` untuk `PAPER` = order nyata. Uang bisa hilang.
- Bot berhenti saat pair dalam maintenance/suspended.
- Beli ditolak bila saldo IDR kurang atau order di bawah minimum pair.
- Mode `auto` hanya pindah pair saat posisi kosong.
- Sinyal gagal ambil data → NEUTRAL, bot tidak menebak.

## Catatan

- Data Binance dari `data-api.binance.vision` (mirror publik).
- Sinyal dari klines Binance USDT; eksekusi di Indodax IDR.
- Skor = heuristik teknikal, bukan prediksi.
