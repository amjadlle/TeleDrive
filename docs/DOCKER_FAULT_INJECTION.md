# Docker fault injection — sıfırdan kur, boz, kurtar

**Bu test altyapısı ilerideki bütün özellikler için yeniden kullanılır.** Geliştirme geliştirme kabulü için mevcut bir
production sunucusu, gerçek legacy dosyası, operatör imzası, bir saatlik
production soak veya production Telegram kanalı **gerekli değildir**.
Bunlar dağıtım öncesi sonraki aşamanın ayrı
[`Geliştirme_PRODUCTION_ACCEPTANCE.md`](Geliştirme_PRODUCTION_ACCEPTANCE.md) kapılarıdır.

## Tek komut

Bilgisayarda Docker Engine + Docker Compose v2 + Python 3 gerekir.
TeleDrive kaynak ağacında:

```bash
git switch main
git pull --ff-only origin main
bash scripts/run_docker_fault_injection.sh
```

Mevcut `python scripts/p1_acceptance_wizard.py` komutu da varsayılan
olarak aynı Docker DEV testini çağırır; eski production soruları
yalnızca `--production` ile açıkça seçildiğinde görüntülenir.
Test **hiçbir API ID/hash, StringSession, Bot token, kanal ID, legacy
source path veya üretim DB bilgisi sormaz**.

Test tamamen yeni, rastgele adlandırılmış bir Docker Compose projesi
oluşturur. İçinde:

- PostgreSQL 17 (dış dünyaya hiçbir port açılmaz)
- İki ayrı gerçek TeleDrive WebUI/API container'ı (HTTP yalnızca izole
  container ağı içinde, Basic Auth ve farklı süreçler)
- Gerçek `TelegramObjectStorage` / SQLAlchemy manifest ve chunk
  motorunu kullanan bağımsız Python test container'ı
- Telegram ağının yerini alan, kendi Docker test workspace'ine yazan
  **disk-backed sahte Telegram** (gerçek Telegram hesabına bağlanmaz)

## Geliştirme geliştirme kabul matrisi

| ID | Otomatik test | Başarı kanıtı |
| --- | --- | --- |
| DEV-01 | Sıfırdan PostgreSQL ve 2 TeleDrive HTTP süreci | İki gerçek Basic Auth health endpoint'i, anonim isteğe 401/403 |
| DEV-02 | 0B, 1B, 4097B, 65537B ve 5MiB+7B yükleme | SQL HEAD/manifest, gerçek disk chunk, indirme SHA-256, multipart toplam boyut |
| DEV-03 | İstemcinin kaynak dosyası silindikten sonra readback | Tam eşit byte dizisi, Range read |
| DEV-04 | Simüle Telegram ACK/SQL crash | Persisted pre-send intent, otomatik çift gönderimin reddi |
| DEV-05 | Sürüm/retention ve GC güvenliği | Unresolved intent GC tarafından korunur; fiziksel silme asla etkin değil |
| DEV-06 | Sentetik Telegram 429 | Paylaşılan SQL cooldown sayacı yeni SQL bağlantısı ve iki web node'unda tutarlı |
| DEV-07 | Native PostgreSQL 17 dump + **ayrı** restored DB | pg_dump, pg_restore, SHA-256, Range ve referans kimlikleri |
| DEV-08 | Kopuk/bozuk SQL yedeği | Kopyası kasıtlı bozulan arşiv native pg_restore tarafından reddedilir |
| DEV-09 | Bozuk Telegram media chunk | Download fail-closed, yarım çıktı yayımlanmaz, medya onarılınca tekrar okunur |
| DEV-10 | İzinli yaşam döngüsü | Projeye özel Docker volume ve container'lar temizlenir; üretim ortamı değiştirilmez |

Bu testler *Docker motoru, gerçek PostgreSQL ve gerçek Web API* kullanır;
ancak Telegram upload/download **simüle edilir**. Gerçek Telegram
Bot/User/multiworker doğrulaması ayrı opt-in GitHub E2E'de koşar.
Sentetik 429 ve crash senaryoları doğal Telegram flood/429 kanıtı değildir.

## Rapor ve sınırlar

Rapor varsayılan `.docker-fault-evidence/docker-fault-<random>.json`
altında mode 0600 saklanır (Git tarafından izlenmez). İsteğe bağlı
`TELEDRIVE_FAULT_REPORT_DIR` ayrı özel bir dizini gösterebilir. Parola,
token, API hash, session, özel kanal kimliği veya DB URL'si rapora
yazılmaz. CLI yalnızca kendi `mktemp` workspace'ini ve benzersiz
Compose projesini `down --volumes` ile temizler; `docker system prune`
ve global volume temizleme kesinlikle kullanılmaz.

GitHub üzerinden aynı test: **TeleDrive Docker Fault Injection CI**
workflow'u. Bu workflow secret kullanmaz. İş başarılıysa DEV-01–10
geliştirme kabulü tamamlanabilir. GERÇEK Telegram/production kabulü
ayrı kanıt ister.

Kontrol edilmemiş ortamda `production_acceptance_signed_off` alanı
daima `false` kalır. `legacy_cutover_required` geliştirme modu
için `false` olmak zorundadır; *bu* legacy migration'ın test
edildiği anlamına gelmez.
