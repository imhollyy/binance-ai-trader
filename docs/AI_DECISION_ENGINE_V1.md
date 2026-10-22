# TradePilot — AI Constitution v1 teknik uygulama

**Asıl anayasa:** [AI_TRADING_CONSTITUTION_V1.md](AI_TRADING_CONSTITUTION_V1.md).
Bu dosya kullanıcının 23.043 baytlık orijinal anayasa metnidir. Normatif
olan kullanıcı metnidir, bu açıklama onun yerine geçmez. İş kaydı: #88.

## Mevcut (BT-012 ilk geliştirme dilimi)

- src/binance_ai_trader/ai_constitution.py: Pydantic AI öneri sözleşmesi ve
  deterministik, yan etkisiz IndependentRiskPolicy / risk değerlendirmesi.
- Yalnız SPOT + PAPER. Strict JSON: signal_id, schema version, model version,
  market regime, kapalı mumun kaynağa bağlı zamanı, geçerlilik sonu,
  BUY/SELL/HOLD/NO_TRADE, reason_codes, opsiyonel olasılık / EV bileşenleri.
- AI isteği kullanıcı hard limitlerini belirleyemez. Fazla alan reddedilir;
  gerçek para / Futures / kaldıraç sözleşmede desteklenmez.
- Güvenilir veri snapshot'ı ve maliyetler yalnız sunucu tarafından sağlanır;
  AI'ın kendi verified, confidence, pozitif EV iddiası bu kayıtları oluşturmaz.
- Risk kontrol sırası: user hard limits, bot state, exchange, portfolio,
  source quality, maliyet, sinyal, maliyet sonrası beklenen değer.
- Karar uygun olsa bile PROPOSAL_ELIGIBLE **işlem emri değildir**.
  API/worker ve PostgreSQL ledger'e yeni AI işlem bağlantısı yapılmadı.
  Eski SMA worker, compose preview ve kullanıcının PAPER bakiyesi değişmedi.

## Henüz uygulanmayan anayasa bölümleri

- LLM/API sağlayıcısı, gerçek model kalibrasyonu ve güven skoru ölçümü;
  model_version sunucu kalibrasyon listesinde yoksa MODEL_NOT_CALIBRATED.
- Çoklu zaman dilimi veri sağlayıcısı, market-regime classifier, orderbook,
  trade flow, haber, sentiment, derivatives, Futures risk/işlem motoru.
- AI SELL ve PAUSED/STOPPED/EMERGENCY_FLATTEN sırasında açık pozisyon yönetimi;
  yeni çıkış motoru bitmeden EXIT_ENGINE_NOT_INTEGRATED.
- Stop-loss seviyesi otomatik çalışan bir stop emri değildir; fiyat garantisi yok.
- Risk modu 1-5 haritalaması, beklenen değer ve maliyet marjı eşikleri
  kullanıcı tarafından ayrıca belirlenecek; burada kullanıcının tercihi
  gibi varsayılan yüzde/bütçe kullanılmadı.
- Otomatik PAPER BUY→SELL gerçek sahibin veritabanında test edilmedi.
  İzole PostgreSQL ile AI karar çıkışlarından tam sanal işlemlere geçiş
  sonraki ayrı doğrulama ve onay gerektiriyor.

## Veri ve güvenlik sözleşmesi

- AiSpotProposal untrusted model text'inden strict schema validation ile üretilir.
- IndependentRiskPolicy sahibi/sunucu tarafından, gerçek PAP​ER defterinin
  tutarlı snapshot'ından, doğru ayar sürümüyle hazırlanmalıdır.
- TrustedSpotEvidence ancak deterministic source validator / verified market
  collector tarafından üretilmelidir; model bu alanları tedarik edemez.
- Bayt cinsinden fiyatlar ve bps Decimal. Decimal alanları JSON'da metin
  olarak taşınır; binary floating point kabul edilmez.
- P(win)+P(loss)+P(flat)=1; tahmin modeli kalibre edilmemişse beklenen
  değer hesaplandı/öneri onaylandı diye gösterilmez.
- Tüm onaylar ilerideki bağımsız executor tarafından atomik DB ve güncel
  account/source/quote kontrollerinden tekrar geçirilmelidir.
- Varsayılan worker KAPALI. Real Binance API keys, live orders ve Futures YOK.

## Anayasanın kaynak bütünlüğü

Original UTF-8 kaynak 1.161 metin satırı, CRLF ve son satır yeni satır
ile 23.043 bayttır. Testler bu kaynak dosyanın değişmediğini kontrol eder.
Anayasa değişiklikleri yalnız kullanıcının açık kararıyla v1.1 / v2 olabilir.

## BT-013 izole PAPER karar ve defter provası

- `ai_market_regime.py` yalnız kullanıcı/uygulama tarafından AÇIKÇA seçilen üç ayrı
  zaman dilimini (şu an public veri adaptörünün desteklediği 1m/5m/15m) kullanır.
  Kapalı, kesintisiz, güncel en az 30 mum olmadan rejim hazır sayılmaz.
  Direction, hacim ve gözlenen göreceli range expansion betimseldir; bilimsel
  kalibre edilmiş yön tahmini veya kesin kâr olasılığı değildir.
- Üç zaman dilimi çelişirse `UNCERTAIN`; veri hatasında `DATA_QUALITY_FAILURE`.
  BUY için yalnız doğrulanmış UPTREND/STRONG_UPTREND uyumu kabul edilir.
- `ai_paper_dry_run.py`, önceden bağımsız risk kontrolü yapılmış BUY sinyalini
  immutable ve **kalıcı olmayan** `PaperSpotBook` simülatörüne geçirir.
  Sembole, kapalı muma, rejime ve ücrete ait veriler modelin JSON'undan alınmaz.
- İki yönlü taker fee + slippage ve iki tarafta yarım spread baz alınarak
  `paid_fees_usdt`, `realized_pnl_usdt` ve gerçek net nakit doğrulanır.
  Mevcut simülatör tam sayı bps işlem ücreti ve kayma kabul ettiği için
  doğrulanmış fractional bps ücreti düşürerek yuvarlamak yerine giriş reddedilir.
- AI SELL yalnız defterde gerçekten var olan bir pozisyonun tam miktarını azaltabilir.
  RUNNING, PAUSED ve EMERGENCY_FLATTEN sırasında doğrulanmış fiyatta sanal kapanış
  test edilir. STOPPED sırasında henüz tanımlanmamış `stop_behavior` için
  otomatik çıkış varsayımı yapılmaz; bu bir üretim stop emri değildir.
- Testler sentetik OHLCV/risksiz bakiyede çalışır; burada *kalibrasyon testi veya
  finansal getiri üstünlüğü* iddiası yoktur.

**Sınırlar:** Yeni fonksiyonlar herhangi bir HTTP endpoint, worker, PostgreSQL
ledger, gerçek Binance emri veya Cloudflare erişimine BAĞLI DEĞİLDİR.
Sanal BUY→SELL yalnız fonksiyon çağrısı içindeki in-memory defterde gösterilir.
Kalıcı veritabanı bağlama, atomik tekrar kontrol, gerçek stop/TP motoru,
kaynak veri provenance'ı, fee precision genişletmesi, model calibration,
çok daha zengin regime/flow analizi ve ileri walk-forward testler ayrı
güvenlik-kritik işlerdir. Canlı işlem ve Futures kesinlikle kapalıdır.

## BT-014: kapalı, tek kullanımlık PostgreSQL atomik işlem provası

- `ai_paper_atomic_test.py` yeni bir motor veya otomatik worker DEĞİLDİR.
  Yalnız `AI_ATOMIC_TEST_ISOLATED=1` ve `localhost/trader_ai_atomic_test`
  koşulları **birlikte** sağlanıyorsa aynı mevcut `open_paper_spot` /
  `close_paper_spot` atomik defterini çağırır. GitHub CI ayrı veritabanı
  oluşturur, Alembic uygular, eşzamanlı ve başarısız işlemleri test eder.
- AI'nın JSON'undaki sermaye/risk limitleri kabul edilmez. Güncel settings,
  hesap/sürüm, açık pozisyon ve journal hesap mutabakatı PostgreSQL kilidi
  altından yeniden okunur. Üç zaman dilimi kayıtlı OHLCV'si de aynı işlemde
  yeniden doğrulanır. AI BUY risk değerlendirmesini tekrar geçemezse bütün
  işlem rollback olur. Idempotency anahtarı bir sinyal kimliğine bağlıdır.
- SELL yalnız var olan açık pozisyonu kapatır; yeni risk açamaz. STOPPED
  otomatik kapanış davranışı henüz tanımlanmadığından reddedilir.
- Fiyat örneği en son kayıtlı kapalı mumun kapanışıdır; bunun üzerinden
  half-spread ve adverse slippage eklenir. **Bu fiyat o anda gerçekleşebilir
  gerçek emir fiyatı değildir.** Decimal kesinlik sınırında aşağı yuvarlanmış
  0.001 adımlı miktar kullanılır; korunan notional tavan aşılmaz.
- İzole test ortamında policy, sahte kalibrasyon listesi ve kaynak örneği
  elle sağlanır. Depodaki mevcut SpotCandle tablosu kriptografik kaynak
  doğrulaması yapmadığından testlerin geçmesi güvenilir piyasa veri
  provenance'ının veya AI modelinin gerçek kalibrasyonunun kanıtı OLAMAZ.

**Üretime aktivasyon yok:** Kullanıcının gerçek PAPER ledger'ı, scheduler,
API, Cloudflare, Binance account, Futures ve LIVE emir yolu değiştirilmez.
PAPER üretim hesabına bağlamadan önce doğrulanmış source adapter, gerçek
fee/slippage konfigürasyonu, stop/TP/PAUSED/EMERGENCY davranış motoru,
ayrı gate yönetimi, model kalibrasyonu, tekrar/çakışma/saat testleri ve
kullanıcının kontrollü test onayı zorunludur.

## BT-015: açık pozisyon için veri doğrulamalı, işlem yapmayan koruma

- `ai_position_protection.py`, **yalnız salt-okunur karar/uyarıdır**. Ne
  `stop_price` alanı ne de stop seviyesinin aşılması gerçek veya PAPER bir
  emrin gerçekleştiğini gösterir. Bu modül DB'ye, API'ye, scheduler'a, worker'a,
  Futures'a veya Binance private endpoints'e yazmaz; mevcut PAPER owner ledger
  ve `main` değişmez.
- `BotControlSnapshot` sürümlü **hafızadaki** durum geçişlerini test eder;
  kalıcı ayar tablosu veya eşzamanlı DB CAS değildir. Durumlar RUNNING, PAUSED,
  STOPPED, EMERGENCY_FLATTEN. STOPPED'dan doğrudan RUNNING yasak; PAUSED'dan
  RUNNING'e ancak kullanıcı açıkça yetkilendirirse ve PAPER settings uygun
  ise geçilir. EMERGENCY_FLATTEN adı kendi başına pozisyon kapatmaz.
- Operatörün açıkça seçebileceği `MONITOR_ONLY` ve `REVIEW_REDUCE_ONLY`
  davranışları bile emir göndermez. `REDUCE_ONLY_REVIEW` ancak kayıtlı
  açık pozisyon için inceleme talebidir; **otomatik reduce-only icra değildir**.
  Kullanıcı henüz stop_behavior tercihinde bulunmadığından otomatik
  emir varsayılmaz.
- Bir kerelik `observe_public_paper_position_once`, mevcut
  `PublicSpotObservationClient` ile Binance PUBLIC Spot 1m mumları ve
  `ticker/price` verisini GET olarak alır, mevcut kalite doğrulayıcısından
  geçirir. 3 saniyeyi aşan lokal alıntı, geçersiz OHLCV, 429, fiyat
  uyuşmazlığı veya config versiyon uyuşmazlığı halinde işlem veya
  pozitif koruma sonucu üretmez. Bu ticker fiyatı **borsanın emir
  gerçekleştirme fiyatı veya emir defteri spread ölçümü DEĞİLDİR**.
- STOPPED sırasında açık pozisyon `ALERT_STOPPED_POSITION_OPEN` uyarısı
  verir. PAUSED sırasında stop aşılırsa `ALERT_STOP_BREACH` veya açık
  yetkiyle `REDUCE_ONLY_REVIEW` alınır. EMERGENCY_FLATTEN sırasında
  `EMERGENCY_NOT_EXECUTED` açıkça bildirilir. İsteğe bağlı kullanıcı
  tanımlı take-profit seviyesi sadece `ALERT_TAKE_PROFIT` verir.
- Testler tamamen sentetik `httpx.MockTransport` ile çalışır; Binance
  işleminin sonucunu veya canlı veri doğruluğunu kanıtlamaz.

**Sonraki güvenlik kapısı:** BotState ile stop_behavior'ın yetkili kullanıcı
CAS sürümüyle kalıcı PostgreSQL'de tutulması, gerçek server-owned quote
kanıtı, birden çok pozisyonda bağımsız koruma, fee/spread doğrulaması,
stop/TP emir motoru, cron/worker idempotency/rate-limit, kontrollü owner
PAPER kabulü ve gerçek model kalibrasyonu. Bunlar tamamlanmadan bot
aktif/korumalı gibi tanıtılamaz.

## BT-016: PostgreSQL'de sürümlü operatör durum provası (yalnız CI)

- `0007_paper_bot_controls` migration'ı sadece **boş** durum ve
  append-only audit tabloları ekler. RUNNING satırı oluşturmaz, botu açmaz,
  mevcut PAPER hesap, pozisyon ve journal verilerini değiştirmez.
- `ai_paper_bot_controls_test.py` yazma erişimini özellikle yalnız
  `BOT_CONTROLS_TEST_ISOLATED=1` ve
  `localhost/trader_bot_controls_test` kombinasyonuyla sınırlar.
  Bu kodun üretim API'si, takvim/worker girişi veya Binance private
  emir erişimi bulunmaz.
- İlk bootstrap ancak açıkça çağrılırsa `STOPPED`,
  `MONITOR_ONLY`, `revision=1` oluşturur; tekrar çağrı var olan
  durumu sıfırlamaz. Her değişiklikte kilitli `TradingSettingsRow`
  ve `PaperBotControlRow` altından sürüm kontrolü ve canlı
  `StagingSessionRow` operatör rolü / iptal / süre kontrolü yapılır.
  Revizyon karşılaştırması aynı işlemde atomiktir; audit yeni
  revizyonla aynı transaction'da eklenir.
- **Güvenlik sınırı:** İç servise verilen oturum HASH'i tek başına
  cookie/CSRF sahipliğinin kanıtı değildir. Üretim HTTP yetkilendirme
  katmanı bu ham cookie, HTTPS Origin, revocation, CSRF, özel approval
  ve kullanıcı niyetini ayrıca doğrulamadıkça bu modül **kullanıcı
  durum güncelleme yetkisi** sağlamaz. CI testleri sentetik oturumlarla
  çalışır ve ana PAPER işleyicileri bu tabloyu okumaz.
- `REVIEW_REDUCE_ONLY` seçimi ve `operator_confirmed_reduce_only`
  halen sadece **inceleme sonucu** üretmeye yöneliktir. Stop/TP,
  emergency veya başka otomatik emir için izin değildir.

**Sonraki faz:** staging RBAC+CSRF ile güvenli API'ye bağlama,
kalıcı worker'ın bu durumu kilit altında fail-closed okuması, fiyat
kaynağının doğrulanması, ayrı audit/journal ve sonra kontrollü
PAPER stop/TP reduce-only icra testi. Kullanıcı hesabında
otomatik işlem başlatılmadı, piyasa kârlılığı kanıtlanmadı.

## BT-017: authenticated isolated bot-control API rehearsal

- Normal LOCAL and normal STAGING modes **do not register** any bot-control
  endpoint. The HTTPS route is registered only when an explicitly opted-in,
  local disposable `trader_bot_controls_http_test` PostgreSQL instance is
  used with `BOT_CONTROLS_HTTP_TEST_ISOLATED=1`.
- Isolated `GET/PUT /api/v1/paper/bot-control` accepts the existing
  server-verifiable HTTPS cookie session. PUT additionally requires exact
  Origin, CSRF, operator role and separate
  `X-Paper-Bot-Control-Approval: ISOLATED_BOT_CONTROL_REVIEW_ONLY`.
  The caller cannot supply an operator token hash. The independently
  locked DB writer revalidates the unrevoked, unexpired operator session
  and config revision before an atomic CAS + append-only audit.
- Read-only GET is permitted to authenticated viewers and operators.
  Missing/stale control state, wrong version, invalid stop behavior and
  absent permissions fail closed. `Cache-Control: no-store, private`.
- All tests are synthetic and use separate disposable PostgreSQL plus
  FastAPI TestClient; no actual owner account, scheduler or exchange
  order endpoint. RUNNING in this *test-only* table **never** runs a bot.
- This remains insufficient for production: the controlled test route
  intentionally cannot target production PostgreSQL, existing workers
  do not use the state and stop-loss/TP execution is not installed.
  Operator risk settings and fees still require separate acceptance.

## BT-018: izolasyonlu, senaryolu PAPER stop / take-profit kapanışı

- `ai_protective_paper_test.py` **sadece** PostgreSQL
  `localhost/trader_protective_close_test` ve
  `PROTECTIVE_CLOSE_TEST_ISOLATED=1` halinde çalışabilir; gerçek
  kullanıcı PAPER veritabanına, runtime worker'a, otomatik scheduler'a,
  HTTP uç noktasına, Futures'a veya canlı Binance emrine BAĞLANMAZ.
- Piyasa kaynağı şartı gerçek `PublicSpotObservationClient` arayüzüdür,
  ancak bu prova ayrıca `httpx.MockTransport` gerektirir. Bu nedenle
  fiyat/1m kline verileri kesinlikle **SENTETİKTİR**: canlı borsa
  fiyatı veya gerçekleşebilir emir fiyatı değildir.
- Onaydan bağımsız `RUNNING`, `STOPPED` veya `MONITOR_ONLY`
  otomatik pozisyon kapatamaz. Operatör tarafından önceden seçilmiş
  `REVIEW_REDUCE_ONLY`, `operator_confirmed_reduce_only=true`,
  `PAUSED` veya `EMERGENCY_FLATTEN`, doğru control revision ve
  ayrıca her simülasyonda `explicit_test_exit_approval=true`
  gerekir. Stop altı veya tanımlı TP üstü trigger'ı gösterilmeden
  kapanış olmaz.
- `close_paper_spot` içindeki account/settings/positions/journal
  mutabakatı ve `_signal_preflight` kilit altında tekrarlanır;
  kontrol revizyonu, stored 1m OHLCV, kaynak kalitesi ve tazeliği
  tekrar doğrulanır. SELL reduce-only'dir, tek mevcut açık
  pozisyonun tamamı kapatılır; anahtarlı idempotency ve
  `PaperSpotObservedQuoteRow` append-only alıntı kaydı aynı
  transaction'dadır.
- Rakamlar mevcut PAPER defterinin komisyon ve olumsuz fiyat kayması
  varsayımlarını kullanır. **Gerçek emir defteri spread verisi ve
  exchange taker ücret katmanı henüz bulunmuyor**; net sonuçlar
  gerçekte garantili kâr/zarar tahmini sayılmaz.
- İzole CI: eşzamanlı stop kapanışı, gereksiz/yanlış tetikleyiciler,
  yetkisiz revizyon, 429, kaynak aykırılığı, taker fee/slippage
  hesabi, pozitif TP net P&L, negatif stop net P&L ve
  audit flush hatasında tam rollback.

**Sıradaki üretim kapıları:** Gerçek Spot best bid/ask
kaynağı doğrulaması, gerçek ücret kademesi ve piyasa filtreleri,
birden çok paralel sembolde açık risk, restart/kill-switch,
kurumsal state worker entegrasyonu, manuel operatör onayı ve
uzun süreli paper forward-run; bunlar tamamlanmadan otomatik
koruma etkinleştirilemez.
