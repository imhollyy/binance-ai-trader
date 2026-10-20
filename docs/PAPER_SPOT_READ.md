# BT-004D3 — Yerel salt-okunur PAPER Spot defteri

Bu özellik yalnızca **localhost** üzerindeki geliştirme API'sine bağlanan
TradePilot panelinin **Pozisyonlar** sayfasında kalıcı sanal Spot hesap
bilgisini, pozisyonları ve append-only işlem günlüğünü okur.

## API ve güvenlik

`GET /api/v1/paper/spot?positions_limit=30&events_limit=30`

- `X-Admin-Token` zorunludur; CORS ve TrustedHost sınırları mevcut yerel API ile aynıdır.
- 1–100 aralığında son pozisyonları ve journal olaylarını, ayrıca tam sayımları döndürür.
- PostgreSQL'de `REPEATABLE READ READ ONLY` tek snapshot üzerinden okuma yapar.
- Yanıt `Cache-Control: no-store, private` içerir.
- Decimal/NUMERIC parasal alanları JSON **string** biçimindedir; float'a çevrilmez.
- `initialized=false`, `account=null`, boş listeler: hesap henüz oluşturulmamış.
  GET hesabı veya sahte işlem verisini **asla oluşturmaz**.
- `initial_capital_usdt`, `cash_usdt`, `paid_fees_usdt`,
  `realized_pnl_usdt`: yalnızca kaydedilmiş **sanal** değerlerdir.
- `unrealized PnL`, `equity`, canlı fiyat veya Binance bakiyesi hesaplanmaz.
- HTTP alış/satış endpoint'i, işçi süreç, scheduler, gerçek Binance bağlantısı yoktur.

Örnek yerel sorgu (gerçek token veya adres paylaşılan loglara yazılmaz):

```sh
curl -H "X-Admin-Token: <local-secret>" http://127.0.0.1:8000/api/v1/paper/spot
```

## Kontroller

- `tests/test_paper_read_api.py`: yetkisiz erişim, origin/host, sınırlar,
  bootstrap yapılmaması, boş/oluşturulmuş hesap, Decimal JSON.
- `tests/test_paper_execution_postgres.py`: yalnızca bağımsız
  `trader_exec_test` PostgreSQL'de gerçek al/kapat sonrası tutarlı snapshot,
  journal/pozisyon ve read-only değişmezlik kontrolleri.
- `web/e2e/paper-ledger.spec.ts`: bağlantı yokken 360/390/768/1280px,
  izole Docker E2E'de bağlantı sonrası gerçekte boş hesabı dürüst gösterme.
- `.github/workflows/fullstack-e2e.yml` bunları disposable DB üzerinde çalıştırır.
- Strix aktif tarama, public site güvenliği ve üretim kimlik doğrulaması bu paket
  kapsamında yapılmadı; yerel admin token internete açılmamalıdır.

**Veri güvenliği:** testleri kullanıcı DB'sinde çalıştırma; bazı diğer testler
veritabanını temizler. `main`/production/Tam Kare değiştirilmez.
