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
