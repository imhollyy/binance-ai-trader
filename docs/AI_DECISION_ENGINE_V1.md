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
