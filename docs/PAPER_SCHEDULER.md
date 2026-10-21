# BT-007C — Kontrollü PAPER Spot scheduler (varsayılan KAPALI)

**Bu paket gerçek Binance işlemi yapmaz.** `paper_scheduler.py` yalnızca `paper_worker.run_paper_worker_once` çağrısının güvenli, engellenebilir ve kontrollü bir zaman döngüsünü ekler. `main`, production domain, live exchange order, Futures, özel Binance API anahtarı ve Tam Kare değişmez.

## Çalışma ve üçlü izin kapısı

1. Kodun sadece import edilmesi, database migration, varsayılan Compose veya `bot_enabled=True` olması worker başlatmaz.
2. Yerel `compose.yaml` içinde `paper-scheduler` ancak `--profile paper-scheduler` ile açıkça çağrılan isteğe bağlı ayrı hizmettir. Bir portu yoktur, web/API servisine yönetici token'ı verilmez, otomatik `restart` kapalıdır.
3. Ek olarak ortam `PAPER_SCHEDULER_OPT_IN=I_UNDERSTAND_PAPER_ONLY` ve `TRADING_MODE=paper` gerektirir. Komut satırı `--enable-paper-scheduler` parametresi gerektirir. Daha önce oluşturulmuş PostgreSQL PAPER hesabının kalıcı `bot_enabled=True`, `manual_approval=False`, izinli Spot sembolü ve ayar/hesap sürümü eşleşmesi olmadan tek bir public HTTP isteği veya mum/sanal işlem veritabanı yazımı olmaz. Güvenlik için manuel onay gerekiyorsa worker durur; manuel onay üretilmez.

## Yerel test ortamı kullanımı (gerçek kullanıcı verisi veya production değil)

Önce API ortamında ve PostgreSQL migrasyonları uygulanmış izole test veritabanında PAPER ayarlarını ayrı bir güvenli yöntemle inceleyin. Yayın ve herhangi bir gerçek hesap için **bu Compose komutunu kullanmayın**. Ortamda iki opt-in koşulu yoksa komut hata vererek durur:

```bash
# Yalnız açık kullanıcı onayı ve ayrı PAPER test veritabanı ile:
export PAPER_SCHEDULER_OPT_IN=I_UNDERSTAND_PAPER_ONLY
docker compose --profile paper-scheduler up --build paper-scheduler

# Durdurmak için Ctrl+C veya:
docker compose --profile paper-scheduler stop paper-scheduler
```

Windows PowerShell veya farklı Docker dağıtımlarında ortam değişkenlerini bulunduğunuz kabuğun sözdizimine göre ayarlayın. Başka testlerde mevcut API/PostgreSQL kurulumu üzerinde çalışmak için veritabanını ayrıca yönetmek gerekir. Varsayılan Compose hiçbir zaman scheduler başlatmaz.

## Saat, hız limiti, dayanıklılık

- Her tur sonra en az 60 saniye beklenir. Yeni kapalı mumun bir sonraki UTC aralık sınırından önce tekrar koşulmaz: `max(now+60s, next_close_boundary+1ms, durable_next_allowed_at_ms)`. BTCUSDT/ETHUSDT; 1m/5m/15m dışında istek yok.
- `paper_worker` korunmaları aynen devam eder: session-scoped PostgreSQL advisory lock (diğer işçiler yazamaz), `0005_paper_worker_guard` üzerinde geçmiş deneme zamanı, kalıcı cooldown, Binance 418/429 Retry-After, kademeli bekleme ve jitter. 503, timeout, stale/invalid/gaps fail-closed; anlık aynı mumun geleceğine bakılarak sinyal üretilmez.
- `SIGTERM` ve `SIGINT` için durdurma olayı set edilir, bekleme hemen kırılır, mevcut tek turu bozacak zorlamalı yarım transaction iptali yapılmaz; tur tamamlanınca süreç kapanır. PostgreSQL bağlantı kaybında worker hata döndürür ve scheduler durur, kendiliğinden sık retry yoktur.
- Kalıcı `PaperWorkerGuardRow` son güvenli `last_status`, `last_attempt_at_ms`, `next_allowed_at_ms` kayıtlarını tutar. Scheduler'ın özeti `disabled/stopped/failed/limit_reached`, işçi raporu `disabled/ready/no_signal/paper_recorded/rate_limited/source_blocked/error` şeklindedir. İleride UI bu güvenli durumları ayrı entegrasyonla okuyacak. Raw DB URL, ADMIN_TOKEN, secret, exception payload veya gerçek müşteri verisi loglanmaz.
- `ticker/price` exchange event timestamp vermez; kullanılan yerel REST gözlem fiyatı, gerçek eşleşmiş Binance emri/fill değildir. Sadece varsayımsal PAPER muhasebe ve explicit taker fee/slippage kuralları vardır.

## Kabul ve doğrulama

`tests/test_paper_scheduler.py` saf fake clock ile opt-in olmadan sıfır I/O, bir sonraki kapanmış mum sınırı, 60s minimum, Retry-After bekleme, SIGTERM-like stop, UTC geri gidiş ve CLI güvenlik kapıları testlerini yapar. GitHub Actions sıfırdan lokal `trader_scheduler_test` PostgreSQL veritabanını migrasyonla kurup `tests/test_paper_scheduler_postgres.py` ve yalnız MockTransport public HTTP ile iki çevrim (NO_SIGNAL -> PAPER_RECORDED), journal/quote tekilleştirme, 429, restart sonrası cooldown ve izin iptalini doğrular. Bağımsız Docker Persistence ve Full Stack Paper E2E PASS olmadan `dev` merge yapılmaz.

**Bu bir prod deploy değildir.** Devam eden sıradaki işler: dashboard/UI gerçek worker rapor entegrasyonu, erişim yetkisi/Cloudflare güvenliği, kalıcı backuplar, izleme/operasyon ve kapalı beta. Strix aktif tarama bu sürümde NOT RUN.
