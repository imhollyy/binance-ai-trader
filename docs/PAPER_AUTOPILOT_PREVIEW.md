# BT-010B — Kontrollü PAPER otomasyon pilotu (gerçek para YOK)

Bu modül **gerçekten kendi kendine kapalı Spot mumlarını değerlendirip yalnız SANAL
PAPER muhasebe kaydı yazabilen** mevcut \`paper_worker\` + \`paper_scheduler\`
zincirini, ilk özel beta için **ayrı ve varsayılan kapalı** bir Docker servisi
olarak hazırlar. Başlangıç örnek stratejisi deterministik SMA geçişidir; henüz
AI seçme motoru, kârlılık garantisi veya gerçek Binance emir sistemi yoktur.

## Otomatik başlamamasını sağlayan izinler

1. Normal \`--profile preview\` komutu **preview-paper-pilot** servisini başlatmaz.
2. Pilot açıkça \`--profile paper-pilot\` ile hedeflenmeli; \`restart: "no"\`.
3. Bağımsız ortam değeri \`PAPER_SCHEDULER_OPT_IN=I_UNDERSTAND_PAPER_ONLY\` şart.
4. Pilot CLI \`--action run --confirm-paper-only\` olmadan dönemez.
5. PostgreSQL hesabı tek ve izole \`preview-db/trader_preview\` olmalı.
6. Hesap ayarı atomik ve açık yerel \`--action enable\` ile
   \`bot_enabled=true, manual_approval=false\` moda geçmeli; manuel mod varsayılan.
7. \`run_paper_worker_once\` her çevrimde veriyi, hesap/ayar sürümünü, riskleri
   ve veritabanı kilidini kontrol eder. 418/429, eksik/gecikmiş veri veya
   hesap uyuşmazlığında yeni işlem oluşturmaz.

**Bu kod gerçek API anahtarı, Futures emri, para çekme, live trading veya
borsa emir adaptörü içermez.** İsim benzerliğiyle yanıltıcı şekilde canlı
Binance bota dönüşmez. Gerçek para ayrı kapsam, güvenlik incelemesi ve onaydır.

## Kullanıcının PC'sinde henüz çalıştırılmadı

Bu paket sadece GitHub / geçici CI veritabanında kabul edildiğinde
\`dev\` içine girecek. Kullanıcı PC'sindeki mevcut 1.000 USDT PAPER defteri,
Cloudflare Access, şifreler ve Docker volume'leri **hiç değiştirilmemeli**.
En azından manuel açık pozisyon kapalı olmalı, bağımsız yedek/geri yükleme
denetlenmeli ve son web sürümü gözle görülmeli.

Onay sonrası, mevcut isolated preview env kullanılarak yalnız PC sahibinin
yerel terminalinde şu **kontrol amaçlı** işlem gerçekleştirilebilir:

- \`preview-paper-pilot\` için \`--action status\`: sadece PAPER modu okur;
  "AUTO_PAPER_PERMITTED_NOT_RUNNING_VERIFIED" bir **çalışan süreç kanıtı DEĞİL**.
- \`PAPER_AUTOPILOT_CONTROL_OPT_IN=I_APPROVE_PAPER_AUTOMATION_ONLY\` ile
  \`--action enable --confirm-paper-only\`: mevcut ayar/manual izin durumunu,
  hesap versiyonunu, sabit 1000 sanal USDT sermayeyi ve immutable ledger'ı
  kontrol eder. Açık sanal pozisyon varsa reddeder; yalnız ayar, account version,
  ayar audit satırlarını tek işlemle günceller. Nakit/komisyon/PnL değiştirmez.
- Bağımsız \`PAPER_SCHEDULER_OPT_IN=I_UNDERSTAND_PAPER_ONLY\` ve pilot profiliyle
  \`--action run --confirm-paper-only\` komutu kullanıcının **ayrı** başlatma
  eylemidir. Komut hiçbir port veya özel Binance credential kullanmaz.
- **Acil durdurma**: önce \`preview-paper-pilot\` container'ını durdur,
  sonra \`--action disable --confirm-paper-only\` ile manuel moda dön.
  Disable açık sanal pozisyon varken de çalışır; nakit/journal korur. Var olan
  sanal pozisyon için manuel PAPER satış gerekir, gerçek stop emri YOK.

Doğrudan \`docker compose down -v\`, veritabanı temizleme, yeni PAPER bootstrap,
gerçek Binance API anahtarı yükleme veya güvenlik duvarında yeni port açma YOK.

## Test kabulü

- \`trader_manual_test\` disposable PostgreSQL + MockTransport: 1000 sanal
  USDT; limit bozuk/429 fiyat reddi, BUY/SELL, komisyon, gerçekleşmiş PnL,
  işlem/quote/audit sayımı, retry idempotency, restart ve çakışan nonce.
- Viewer PUT yasak, operator Origin/CSRF şart, izole pilot staging
  sadece risk alanlarına izin verir; hesap sürümü aynı SQL transaction içinde
  taşınır, bakiye ve immutable journal korunur; açık pozisyonda risk değişimi ret.
- Isolated pilot enable/disable, no-open-position, start opt-in ve
  \`run_paper_scheduler(enabled=False)\` sıfır işlem garantisi.
- Mevcut \`trader_scheduler_test\` PG suite: kapalı mum SMA sinyali,
  tek mükerrer kayıtsız BUY, Retry-After/429, cooldown restart ve izin iptali.
- İlgili CI PASS olmadan hiçbiri tamamlanmış sayılmaz.

Sanal ledger testlerinin geçmesi, gerçek Binance hesabına erişildiğini veya
bir stratejinin pozitif beklenen getirisini göstermez.
