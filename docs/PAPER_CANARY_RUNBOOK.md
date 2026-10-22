# BT-011 — İlk otomatik PAPER pilotu: 3-turlu yerel deneme

**Yalnız sanal hesap, gerçek Binance emri YOK.** GitHub CI testleri kullanıcı bilgisayarındaki servisleri başlatmaz.
Bu yönergeyi kullanıcı yalnız mevcut Docker preview'a yeni `dev` imajlarını çekip web ve API'yi güncellediğinde uygulamalıdır.
**DB volume asla silinmez.** `docker compose down -v` yasak; eski sanal emirler korunur.

## Başlangıç koşulları
- Cloudflare oturumu çalışıyor; eski PAPER işlemleri ve bakiye görüntüleniyor.
- `preview-api` ve `preview-web` yeni `dev` koduna göre rebuild edildi.
- İsteğe bağlı ancak önerilen: PAPER PostgreSQL için ayrı güvenli yedek/geri yükleme provası.
- Ön kontrol *tüm ledger tutarlılığını* doğrular. Açık sanal pozisyon varsa kullanıcı ancak bilinçli manuel satışıyla kapatabilir; pilot kendi başına satmaz.

Komutlar **Windows PowerShell**, repo preview kök klasöründe. Env dosyası başka isimdeyse mevcut dosya adını kullan. Bunlar **kullanıcı bilgisayarında** çalışacak; GitHub CI bu komutların çalıştırıldığını kanıtlamaz.

```powershell
$P = @('-p','tradepilot_quick','--env-file','.env.preview.quick','-f','compose.preview.yaml')
docker compose @P --profile preview run --rm preview-api python -m binance_ai_trader.preview_autopilot --action preflight
```

Beklenen: `PAPER_PREFLIGHT READY_MANUAL_PAPER_MODE`. `BLOCKED_OPEN_PAPER_POSITION` gelirse **açık sanal pozisyon varken pilotu başlatma**. Düzeltme amacıyla hiçbir otomatik satış tetiklenmez.

## Kullanıcının ayrıca onaylaması halinde

```powershell
$env:PAPER_AUTOPILOT_CONTROL_OPT_IN = 'I_APPROVE_PAPER_AUTOMATION_ONLY'
docker compose @P --profile preview run --rm -e PAPER_AUTOPILOT_CONTROL_OPT_IN preview-api python -m binance_ai_trader.preview_autopilot --action enable --confirm-paper-only
Remove-Item Env:PAPER_AUTOPILOT_CONTROL_OPT_IN

$env:PAPER_SCHEDULER_OPT_IN = 'I_UNDERSTAND_PAPER_ONLY'
docker compose @P --profile paper-pilot up --build preview-paper-pilot
Remove-Item Env:PAPER_SCHEDULER_OPT_IN
```

Pilot en çok **3 kapalı mum çevrimi** çalışır (`--max-ticks 3`). Normal bekleme en az 60 saniyedir. İşlem oluşmayabilir: `no_signal` başarılı güvenli sonuçtur. Terminalde yalnız sabit işçi durum kodları çıkar; gerçek Binance emri oluşturulamaz.

## Durdurma ve manuel moda geri dönüş

```powershell
docker compose @P --profile paper-pilot stop preview-paper-pilot
$env:PAPER_AUTOPILOT_CONTROL_OPT_IN = 'I_APPROVE_PAPER_AUTOMATION_ONLY'
docker compose @P --profile preview run --rm -e PAPER_AUTOPILOT_CONTROL_OPT_IN preview-api python -m binance_ai_trader.preview_autopilot --action disable --confirm-paper-only
Remove-Item Env:PAPER_AUTOPILOT_CONTROL_OPT_IN
docker compose @P --profile preview run --rm preview-api python -m binance_ai_trader.preview_autopilot --action preflight
```

UI Bot Yönetimi → Verileri yenile: son kayıtlı worker sonucu ve bir sonraki izinli deneme zamanını gösterir. **Çalışıyor** statüsü hâlâ doğrulanmaz; yalnız saklanan tarihsel deneme kanıtıdır. Gerçek 7/24 işletim/heartbeat daha sonraki kapsamdır.

İşlem öncesi/sonrası gerçek PAPER nakit, pozisyon ve ledger event sayısını karşılaştır. Pilotun ana amaçları: izin kapıları, tekrarsızlık, cooldown, hata halinde kapanış ve muhasebe. Yalnız birkaç turla strateji kârlılığı kanıtlanmaz.