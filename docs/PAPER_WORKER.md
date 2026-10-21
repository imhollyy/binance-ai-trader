# BT-007B — Varsayılan kapalı, tek tur PAPER Spot worker

**Önemli:** Bu paket bir servis/scheduler başlatmaz. `run_paper_worker_once(...)` yalnızca çağrıldığında bir kez çalışır. Varsayılan `enabled=False` ve veritabanında `bot_enabled=False` olduğu için normal kurulumda hiçbir piyasa talebi, veritabanı değişikliği veya sanal alış/satış olmaz. `enabled=True` ve kalıcı PAPER Spot ayarlarında `bot_enabled=True`, `manual_approval=False` şartları birlikte gerekir. Bu çağrı henüz web paneline, arka plan hizmetine veya Cloudflare'e bağlanmamıştır.

## Tek tur akışı

1. Yalnızca PostgreSQL ve BTCUSDT/ETHUSDT için 1m/5m/15m desteklenir. Önce READ ONLY snapshot'ta PAPER hesap/ayar/version/onay/izinler kontrol edilir. Hesap yoksa otomatik oluşturulmaz.
2. Aynı veritabanında tek sabit session-scoped PostgreSQL advisory lock alınır. Kilit başka worker'daysa ağ çağrısı veya trade yerine `ready:another_worker_holds_lock` döner. Kilit bir PostgreSQL bağlantısı açık tutularak tüm kısa tur boyunca elde tutulur; bağlantı kopunca DB kilidi bırakır.
3. Kalıcı `paper_worker_guard` tablosundaki `next_allowed_at_ms` okunur. Önceki denemeden daha erkense hiçbir REST/DB trade çağrısı yoktur. Sistem saati geri gittiğinde çalışma engellenir. Yeni tur başlamadan **önce** güvenli en az bir mum aralığı kadar (minimum 60 saniye) soğuma süresi diske yazılır; süreç aniden kapanırsa yeniden başlatmada aşırı tekrar deneme olmaz.
4. `market_ingest.ingest_once(..., require_ready=True)` veriyi yalnız public Spot API'den alır; 50–1000 tamamen kapanmış, contiguous mumun `feed_quality.READY` doğrulanması **öncesinde herhangi bir candle insert yapmaz**. PostgreSQL'e eşzamanlı güvenli ON CONFLICT ile kaydeder, DB içinde tespit edilen boşluk varsa PAPER işlemini engeller.
5. `run_observed_paper_spot_once(...)` yeniden public kapanmış mum geçmişi ve public ticker fiyatını alır. Ticker Binance exchange event zamanı taşımaz: yalnız yerel UTC request/receive gözlem zamanıdır. En fazla üç saniyelik fiyat gözlemi, son kapanışa göre sapma, fiyat geçerliliği, aynı stored candle dizisi ve risk/approval denetlenir. Geçerli kapanış SMA sinyali varsa sanal PAPER ledger içinde mevcut transaction lock/journal idempotency ve append-only quote audit ile kayıt yapılır. **Canlı Binance emri, private API, exchange fill ya da gerçek kâr yoktur.**
6. Sonuç tek durum kaydıdır: `disabled`, `ready` (kilit meşgul/cooldown), `no_signal`, `paper_recorded`, `source_blocked`, `rate_limited`, `error`. Ayrıca güvenli neden, UTC tur zamanı, bir sonraki izinli kontrol zamanı ve yalnız simülasyon journal key'i dönebilir. Token, PII, fiyat ya da key secret loglanmaz.

## 418/429 ve dayanıklılık

418/429 kaynak kısıtlamasında otomatik tekrar çağrı yapılmaz. `Retry-After` ve üstel backoff ile, en fazla bir saatlik, 418 için en az 15 dakika olacak şekilde duraklama hesabı kullanılır. Deterministik küçük jitter aynı anda çok sayıda yeniden denemeyi azaltır. 503, malformed, gap, stale, network timeout, risk reddi ve güvenilmeyen hesap durumu da fail-closed sonuç verir; bu durumlarda da soğuma uygulanır. `0005_paper_worker_guard` yalnız yeni koruma tablosunu ekler ve **hiçbir worker başlatmaz, mevcut PAPER hesaplarını sıfırlamaz**. Boş olmayan tablonun downgrade ile silinmesi reddedilir.

Önemli sınırlamalar: Web/API'de otomatik enable endpoint'i yoktur; 7/24 scheduler, uptime/watchdog, alarm gönderimi, gerçek auth/session ve production internet yayını ayrı güvenlik aşamalarıdır. `PaperWorkerStatus.PAPER_RECORDED` yalnız sanal bakiye/günlükte kayıt demektir. Bot kapalı varsayılır, maliyetler ve stop tahminidir.

## Örnek kullanım (yalnız izole geliştirme)

```python
from binance_ai_trader.paper_worker import run_paper_worker_once

# Varsayılan, güvenli ve hiçbir I/O yapmayan çağrı:
report = run_paper_worker_once(engine)
assert report.status == "disabled"

# Yalnız izole test ortamında, açık sanal onayla ve önceden
# yapılandırılmış PAPER hesabı/izinlerle tek deneme:
# report = run_paper_worker_once(engine, symbol='BTCUSDT', interval='1m', enabled=True)
```

## Testler

`tests/test_paper_worker.py` dış ağa çıkmadan varsayılan kapalı durum, parametre hatası, backoff/418/429 sınırlarını doğrular. `tests/test_paper_worker_postgres.py` yalnız CI'ın sıfırdan kurduğu localhost `trader_worker_test` disposable PostgreSQL veritabanında çalışır; sahte public Binance HTTP ile opt-in, no-signal, concurrent worker, journal/quote idempotency, restart cooldown ve onay iptalini test eder. Final PR HEAD Ruff, mypy, pytest, izolasyonlu PostgreSQL, Docker Persistence ve Full Stack E2E PASS olmadan merge edilemez. Strix active security scan NOT RUN.
