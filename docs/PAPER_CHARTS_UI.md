# BT-008B — Kayıtlı PAPER veri grafikleri ve mobil UI

Bu paket TradePilot'in *yerel geliştirme* panelindeki mevcut kayıtlı PAPER veri ekranlarını okunabilir hale getirir. Binance private REST, gerçek emir/transfer, Futures yürütme, Cloudflare internet erişimi ve PAPER scheduler'ın otomatik açılması **bu paketin kapsamında değildir**.

## Grafiklerin doğruluk kapısı

- **Spot kapanış grafiği** yalnız daha önce PostgreSQL'e kaydedilen ve `GET /api/v1/market/candles` endpoint'inden okunabilen 2–100 kapalı mum için gösterilir. Görselleştirmeden önce açık/kapanış zamanları izinli 1m/5m/15m aralığına tam uyumlu, kapalı, pozitif ve OHLC'de tutarlı olmalı; tüm mumlar birbiri ardına bitişik olmalıdır. Eksik, tekrar, bozuk veya açık mum varsa çizgi **hiç oluşturulmaz**; boşluk üstüne yanıltıcı çizgi çekilmez. Son kapanış fiyatı **gerçek zamanlı ticker değildir**, yerel saklama zamanından sonra piyasa hareket etmiş olabilir.
- **Gerçekleşmiş sanal PnL grafiği** yalnız bounded son PAPER journal kayıtlarındaki `spot_sell` olaylarının `realized_pnl_delta_usdt` alanlarını kullanır. Her çubuk ayrı satış olayının sonuç değeridir; toplam balance, kümülatif PnL veya unrealized open-position mark-to-market olarak etiketlenmez. Alış/satış geçmişi yoksa grafik **yoktur**. Bar uzunlukları yaklaşık görselleştirmedir; kesin `Decimal` kaynak dizeleri, hesaplanıp yuvarlanmadan çubuğun yanında yazılır.
- Fiyat kaynağı ve quote yerel UTC receive metadata BT-008A'nın zaten korunmuş read-only API'sinden gelir. Gerçek Binance execution fill iddiası YOK. `bot_enabled` tercihi, aktif worker heartbeat olmadığında `çalışıyor` diye etiketlenmez.
- Eksik backend bağlantısı/hatalar, boş durum ve yükleniyor durumları grafik veya kâr uydurmaz. Grafikleri kullanıcı yalnız bağlantı kurup veri okuduktan sonra görebilir; gerçek exchange price polling eklenmez.

## Mobil ve erişilebilirlik

360, 390, 768 ve 1280 piksel tarayıcı testleri yeniden düzenlenmiş menü, navigasyon, dokunma hedefleri, min 44 piksel kontrol yüksekliği, grafik etiketleri ve overflow'u kontrol eder. Tam görünüm ekran görüntüsü aynı test turunda Playwright ekine alınır (`web/e2e/recorded-charts.spec.ts`), ancak bağımsız manuel görsel onaydan geçtiği anlamına gelmez. Grafik SVG'sinde erişilebilir `title`, `desc` ve `role=img` bulunur; bar grafiğinde her sonuç erişilebilir metinle de sunulur.

Tarayıcı testi fixture'ları yalnızca izole Playwright testlerinin `page.route` sahte yanıtlarıdır; **üretim/demo bakiyesi değildir**. Gerçek API ve PostgreSQL round-trip ayrı Full Stack Paper E2E korumasında çalışır. Github final PR HEAD 4 pipeline (Python CI+PG, Web UI CI, Docker Persistence, Full Stack E2E) ve `dev` post-merge bütün PASS olmadan release edilmez.

## Sonraki aşama

[#8](https://github.com/imhollyy/binance-ai-trader/issues/8) — gerçek oturum/kimlik doğrulama, Cloudflare HTTPS, worker/API/DB servis hosting ve yedekleme; `X-Admin-Token` internette paylaşıma AÇILMAYACAK. Sonrasında 7/24 PAPER kapalı beta güvenlik ve geri yükleme testleri. Tam Kare ve `main` değişmez.
