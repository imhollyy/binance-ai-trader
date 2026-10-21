# BT-009A — Kapalı beta HTTPS oturum güvenliği (yerel hazırlık)

**Durum:** Geliştirme amaçlı, otomatik etkinleşmeyen staging taslağı. Gerçek Cloudflare yayını, domain/DNS, sunucu kurulumları, özel kullanıcı/parolalar ve gerçek kullanıcı verileri bu PR kapsamında **oluşturulmaz veya değiştirilmez**. Varsayılan `API_AUTH_MODE=local` ile eski localhost geliştirme paneli aynı şekilde çalışır; bu modun `X-Admin-Token` değeri **internette ASLA paylaşılmaz**.

## İki birbirinden ayrı çalışma modu

| | LOCAL (varsayılan) | STAGING (ancak açıkça seçilirse) |
|---|---|---|
| Kimlik doğrulama | Uzun `X-Admin-Token` | PostgreSQL'de yalnız SHA-256 özeti saklanan rastgele 256-bit session token |
| API host | Sadece localhost/127.0.0.1/testserver | Tek, açıkça belirtilmiş HTTPS hostname (`STAGING_API_HOST`) |
| İzinli browser origin | Yerel Next.js | Bir adet kesin HTTPS origin (`STAGING_WEB_ORIGIN`) |
| Cookie | Yok | `__Host-tradepilot`, Secure, HttpOnly, SameSite=Strict, Path=/, 15 dakika |
| Yetkiler | Yerel geliştirme | Viewer READ; operator READ ve ayrıca CSRF + `X-Paper-Settings-Approval: PAPER_SETTINGS_ONLY` ile yalnız PAPER settings PUT |
| API token fallback | Local | **Kesinlikle yok**: `ADMIN_TOKEN` varsa startup fail-closed |
| Binance | PUBLIC Spot read + PAPER muhasebe | Aynı; gerçek Binance emirleri/private API kesinlikle yok |

### Nasıl kurulmalı? Henüz nasıl kurulmamalı?

1. Ayrı bir **izole staging PostgreSQL** sağlayın; kullanıcı canlı PAPER/üretim DB'sine işaret etmeyin. `0006_staging_sessions` migration **additive** ve login gate row'u seed eder; önceki hesap/journal/mumları sıfırlamaz. Dolu `staging_sessions` varken downgrade reddedilir.
2. HTTPS için web origin ve API DNS host'u kesin olarak eşleştirin. Örnek (gerçek hostname değil): `https://app.staging.example` ve `api.staging.example`. **Her iki uçta TLS** veya güvenilir terminate+upstream tasarımı gerekir; API HTTPS olmayan request'i reddeder. Arbitrary `X-Forwarded-Proto` ve geniş `TrustedHost` kabul edilmemeli.
3. İki ayrı, uzun gizli geçiş cümlesi için yalnız interaktif geliştirme konsolunda aşağıdaki hash yardımcı kodunu çalıştırabilirsiniz. **Asıl parolayı CLI parametresi, GitHub secrets olmayan log, PR, .env.example, rapor veya kaynak koda yazmayın.** Scrypt salted hash'lerini güvenli staging secret manager'a koyun.

```python
from getpass import getpass
from binance_ai_trader.staging_auth import password_hash
print(password_hash(getpass("Viewer or operator secret (never logged): ")))
```

Gerekli staging environment değerleri:

```text
TRADING_MODE=paper
API_AUTH_MODE=staging
DATABASE_URL=postgresql+psycopg://<staging-only-credentials>@<private-db-host>/<staging-db>
STAGING_API_HOST=api.staging.example
STAGING_WEB_ORIGIN=https://app.staging.example
STAGING_VIEWER_PASSWORD_HASH=<a distinct salted scrypt hash>
STAGING_OPERATOR_PASSWORD_HASH=<another distinct salted scrypt hash>
```

`ADMIN_TOKEN` staging modunda **mevcut olmamalıdır**. Web önizleme ayrı **build-time opt-in** `NEXT_PUBLIC_TP_STAGING_PREVIEW=enabled` ve `NEXT_PUBLIC_STAGING_API_ORIGIN=https://api.staging.example` ile görünür. Bu değişkenlere yalnız **public API origin** yazılabilir, gizli hash/şifre yazılmaz. Default Next.js build localhost UI'yı korur. HTTPS iki subdomain **same-site** olmadıkça Strict cookie API isteğine eklenmez.

4. API reverse proxy ve Cloudflare Access ağ politikası, IP koruması, TLS/host/origin, DNS ve gerçek staging launch **BT-009B'nin ayrı açık onay konusudur**. Bu PR ile herhangi bir `docker compose up` veya otomatik servis yayını yapılmaz; default Compose bind'i `127.0.0.1:8000` kalır. Backend staging kodunda Exchange keys, order routes, scheduler remote start/stop yok.

## Rol ve istek kuralları

- `POST /api/v1/auth/login`: yalnız `https://app.staging.example` Origin ve HTTPS. PostgreSQL singleton `staging_login_gate` her denemeyi aynı DB kilidinde değerlendirir. 5 yanlış deneme global 5 dakika kilit; doğru parola kilit esnasında dahi geçerli değildir. Bu basit uygulama bir servis-kötüye-kullanım korumasıdır, **Cloudflare rate limiting/WAF'ın yerini almaz**.
- Parola hash: memory-hard salted `scrypt_16384` ayrı 16-byte random salt ve 64-byte çıktı. Parola plaintext'i kalıcı olarak yazılmaz. Test fixture'ları **sentetik örneklerdir**.
- Login sonucu `Secure HttpOnly SameSite=Strict` cookie ve anlık JSON içinde CSRF token döner; CSRF yalnız tarayıcı çalışma belleğinde tutulur, veritabanında hash saklanır. Session 15 dakika içinde sona erer; verilmiş bir cookie token'ı direkt veritabanında bulunmaz. `GET /api/v1/auth/session` yalnız rol/son kullanımı verir, CSRF token'ı geri döndürmez.
- `POST /api/v1/auth/logout` aynı exact Origin ve session/CSRF ister; DB'deki oturumu `revoked=true` yapar, cookie siler. Eski cookie + CSRF tekrarını server reddeder. Süre aşımında server ayrıca 401 döner.
- Yerel `X-Admin-Token` header staging isteğinde her durumda 403. Bütün `GET /api/v1/*` endpoints session gerektirir; host/origin/HTTPS kapıları korunur; API docs/OpenAPI staging'de devre dışıdır.
- Yalnız operator `PUT /api/v1/settings` kullanabilir. Her istek explicit same Origin, güçlü session, geçerli `X-CSRF-Token` ve `X-Paper-Settings-Approval=PAPER_SETTINGS_ONLY` gerektirir. Bu PAPER ayar kaydıdır, worker'ı başlatmaz. Viewer/anonim/yanlış role/CSRF/Origin reddedilir. Web staging önizlemesi **sadece okur**; ayar PUT düğmesi yoktur.

## Tehdit modeli ve açık operasyonel blokajlar

- Kimlik doğrulama hataları generic; login gate ve session kayıtları replay/restart sırasında PostgreSQL'de kalır. Hiçbir cookie, CSRF, parola, host DB credential veya `DATABASE_URL` log mesajında/HTTP hata ayrıntısında yazılmamalıdır. Geçersiz DB veya origin/Host/HTTP/ayar kombinasyonları fail-closed.
- CDN/proxy'den `X-Forwarded-Host/Proto` alanları **asla sıradan tarayıcının yazdığı şekilde güvenilir sayılmamalıdır**. Staging API HTTPS olarak algılamadığı durumda bilinçli olarak 403 üretir; staging deploy öncesi güvenilir ingress mimarisi kurulmalı.
- Secure cookie ve Strict SameSite için web+API hostları gerçek TLS ve aynı registered site'da olmalıdır. Farklı site veya yerel HTTP üzerinde tam oturum doğrulaması yapılmış sayılmaz.
- Bugünkü staging preview yalnız read-only; bağımsız kullanıcı kaydı/recovery/2FA/CSP, Cloudflare Access tenant ve gerçek staging browser-to-API full HTTPS roundtrip, ayrıntılı request/audit observability ve load/DR/backup-restore **henüz yapılmadı**. Özellikle global login throttle bir saldırgan tarafından kısa süreli erişim engeline dönüşebilir; Cloudflare WAF ile desteklenmeden public erişim açılamaz.
- `staging_sessions` süre geçmiş kayıtların periyodik silinmesi, backup-restore, TLS secret rotation, kullanıcı devre dışı bırakma ve granular operator elevation sonraki operasyon aşamasıdır.
- Playwright yerel UI giriş formları mock HTTPS yanıtlarıyla doğrulansa bile, bu **gerçek HTTPS ile cookie'nin Chrome'da roundtrip kanıtı değildir**; gerçek staging ayrı güvenlik kabulüne tabidir.
- Strix aktif güvenlik taraması **NOT RUN**; izole, açıkça izinli hedef sonrası ayrı kayıt gerekir. `main`, Tam Kare ve canlı Binance emirleri değişmeyecek.

## Test ve release gate

Ruff lint/format, mypy strict, pytest, disposable PostgreSQL migration/auth session/logout/role/CSRF, Web TypeScript/Next + 360/390/768/1280 Playwright, Docker persistence, Full Stack Paper E2E. Bunlar `dev` PR final HEAD üzerinde PASS olmadan merge/release yoktur. Sonra post-merge dev CI de PASS olmalıdır.
