# TradePilot — local kurulum ve inceleme (BT-004A)

Bu rehber **yalnızca kendi bilgisayarında geliştirme/test içindir**. Gerçek Binance
hesabına bağlanmaz, işlem yapmaz, kaldıraçlı emir göndermez. Panel henüz
internete açılmamıştır. `main` yerine geliştirilmekte olan `dev` dalını kullan.

## Gerekenler

- Git, Docker Desktop (Compose dahil), Node.js 22 ve çalışır bir internet bağlantısı.
- Visual Studio Code önerilir. Python yalnızca Docker dışında komut çalıştıracaksan gerekir.
- Tüm kurulum **bağımsız** `imhollyy/binance-ai-trader` deposundadır. Tam Kare
  reposuna veya production Supabase'e hiçbir işlem yapma.

## 1. Repo

Yeni klon için:

```powershell
git clone --branch dev https://github.com/imhollyy/binance-ai-trader.git
cd binance-ai-trader
```

Repo daha önce klonlandıysa:

```powershell
git switch dev
git pull --ff-only origin dev
```

## 2. Yerel gizli değerler

PowerShell:

```powershell
Copy-Item .env.example .env
py -3 -c "import secrets; print(secrets.token_hex(24))"
py -3 -c "import secrets; print(secrets.token_hex(24))"
```

İki farklı çıktıyı **yalnızca bilgisayarındaki .env dosyasına** yaz:
- `POSTGRES_PASSWORD=` birinci hex çıktı
- `ADMIN_TOKEN=` ikinci hex çıktı (en az 32 karakter)
- `TRADING_MODE=paper`

Python yüklü değilse rastgele güçlü, en az 32 karakterli **yalnızca
harf/rakam** içeren iki ayrı değer üret. PostgreSQL şifresi mevcut Compose
URL'sine yerleştirildiği için `@`, `/`, `:`, `#` gibi özel karakterleri
bu geliştirme kurulumunda kullanma.

**.env, şifre, token veya Docker logundaki gizli bilgileri sohbet, issue,
PR, ekran görüntüsü veya GitHub'a yapıştırma.**

macOS/Linux'ta `cp .env.example .env` ve
`python3 -c 'import secrets; print(secrets.token_hex(24))'` kullan.

## 3. Backend ve veritabanı

Proje kök klasöründeyken:

```powershell
docker compose up --build -d
docker compose ps
```

Tarayıcıda:
- http://127.0.0.1:8000/health/live — `alive`
- http://127.0.0.1:8000/health/ready — `ready`

Sistem bu adreslere yanıt vermiyorsa API başlatılamamıştır; önce
`docker compose logs --tail 60 api` ile genel hatayı incele. Günlüklerde
gizli bilgi bulunabileceğini unutma.

## 4. Paneli aç

İkinci VS Code terminalinde:

```powershell
cd web
npm install
npm run dev
```

http://127.0.0.1:3000 adresini aç. Sağ üstteki **API Bağlan** düğmesinden:
- API adresi: `http://127.0.0.1:8000`
- Yerel yönetici anahtarı: **kendi .env dosyandaki ADMIN_TOKEN**

Risk Yönetimi ve Bot Yönetimi sayfalarındaki **sanal** ayarlar artık
PostgreSQL'e kaydedilir. Geçersiz limitler API tarafından reddedilir.
Bot etkinleştirme tercihi tek başına bot veya emir servisi başlatmaz.

## 5. Spot geçmişini doldur

Ayrı bir terminalde, proje **kök klasöründe**:

```powershell
docker compose exec api python -m binance_ai_trader.market_cli --symbol BTCUSDT --interval 1m --limit 100
docker compose exec api python -m binance_ai_trader.market_cli --symbol ETHUSDT --interval 5m --limit 100
```

Bu sadece Binance Global'in **herkese açık Spot mum geçmişini** indirir.
API anahtarı gerekmez ve emir gönderilmez. Tekrar çalıştırırsan zaten
kaydedilmiş mumlar çoğaltılmaz. HTTP 418/429 alırsan durur; hemen yeniden
deneyerek hız sınırını zorlamamalısın.

Panelde **Piyasa Verileri** sekmesinde parite/zaman aralığını seç,
**Geçmişi Göster** düğmesine bas. Buradaki veriler geçmiş kapanmış mumlardır;
anlık fiyat veya işlem tavsiyesi değildir.

## 6. Sağlıklı kapat

```powershell
docker compose down
```

Bu komut veritabanı volume'ünü **korur**. `docker compose down -v`
veritabanını siler; veri kaybını istemiyorsan kullanma.

## Hâlâ yapılmayanlar

- Fiziksel iPhone'dan güvenli uzak erişim / HTTPS / gerçek oturum yönetimi
- Zamanlanmış 7/24 veri akışı ve WebSocket
- Paper emir yürütme ve backtest
- Binance API anahtarları / gerçek Spot veya Futures emirleri

Gerçek sermaye bağlamadan önce bunlar ayrıca geliştirilecek, test edilecek
ve açık onayla etkinleştirilecek.
