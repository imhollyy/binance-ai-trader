# BT-004B · Paper Spot muhasebe / simülasyon çekirdeği

`paper_spot` modülü **yalnızca bellekte, ağsız ve immutable** muhasebe yapar.

- `new_paper_spot_book(settings)`: ayrı Spot bütçesi için sanal nakit defteri
- `simulate_spot_buy(...)`: saf, varsayılan olarak onay gerektiren alış simülasyonu
- `simulate_spot_sell(...)`: mevcut sanal long pozisyonun tam kapanışı
- Komisyon ve kötü yönlü kayma (slippage) **açık varsayımlar** olarak girişte ve çıkışta uygulanır.
- Pozisyon kimlikleri tekrar kullanılamaz; reddedilen giriş defteri değiştirmez.
- `paper_risk.evaluate_paper_risk` üzerinden bot kilidi, günlük zarar, maksimum düşüş, bütçe ve açık pozisyon sayısı doğrulanır.
- Tahmini stop zararı, giderleri de hesaba katar. Stop fiyatı kaydedilir fakat otomatik stop yürütme **yoktur**.
- `PaperRiskState` dışarıdan sağlanan sanal değerleme snapshot'ıdır; pozisyon sayısı ve rezerve sermayenin defterle tutarlı olması zorunlu.

## Kritik sınırlar

**Gerçek veya simüle edilmiş emir icrası, API endpoint'i, worker, webhook, private
Binance API, Futures, otomatik strateji, DB transaction / crash recovery yok.**
Bu çekirdek sahte işlem matematiğini hesaplar, bir borsa emrini veya gerçek stop-loss'u
temsil etmez. Mark-to-market, equity/günlük PnL, kalıcılık, tekrar oynatma ve
idempotent transaction **ayrı paketlerde** tamamlanmalıdır.

Slipaj/komisyon tahminlerdir, gerçekleşecek fiyat ya da maksimum zarar garantisi değildir.
