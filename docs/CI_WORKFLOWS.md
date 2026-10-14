# TeleDrive — CI ve geliştirme testleri

Bu dosya **GitHub Actions iş akışlarının, Docker arıza testinin ve P1 geliştirme kabulünün tek kaynağıdır**. Projenin hangi aşamada olduğu, production onayları ve P2 backlog'u için [Proje durumu ve kabul rehberi](IMPLEMENTATION_STATUS.md) kullanılır.

## GitHub Actions grupları

GitHub Actions kenar çubuğu gerçek klasör/grup desteklemediği için adların başındaki sayılar birer görsel gruptur. İş akışı dosya yolları, job ID'leri, secret gereksinimleri ve concurrency kuralları görünüm için değiştirilmez.

| Grup | Workflow | Test kapsamı |
| --- | --- | --- |
| 01 Quality | [Security & Regression](../.github/workflows/quality-security.yml) | Regresyon, güvenlik, bağımlılık ve container smoke testleri |
| 02 Integration | [Databases](../.github/workflows/integration-databases.yml) | Gerçek PostgreSQL/MySQL, native yedek ve izole restore |
| 02 Integration | [REST & S3](../.github/workflows/integration-storage.yml) | REST, SigV4/S3, boto3, AWS CLI ve rclone uyumluluk testleri |
| 02 Integration | [Desktop](../.github/workflows/integration-desktop.yml) | Linux ve Windows desktop/CLI/paketleme kontrolleri |
| 03 Resilience | [Docker Fault Injection](../.github/workflows/resilience-docker.yml) | Gerçek PostgreSQL, iki WebUI, arıza enjeksiyonu, geri yükleme, 13 DEV kapısı |
| 04 Live Telegram | [Bot API E2E](../.github/workflows/live-telegram-bot.yml) | Özel Telegram test kanalında gerçek Bot aktarımı ve readback |
| 04 Live Telegram | [User MTProto E2E](../.github/workflows/live-telegram-user.yml) | Gerçek Telethon oturumu, SHA ve Range |
| 04 Live Telegram | [Multiworker PostgreSQL E2E](../.github/workflows/live-telegram-multiworker.yml) | Ortak PostgreSQL kullanan üç bağımsız worker ve gerçek Telegram |
| 05 Delivery | [Release Gate & Packages](../.github/workflows/delivery-release.yml) | Release regresyonları, Linux/Windows paketleri, sürüm yayını |
| 06 Endurance | [Offline 60m Capacity Soak](../.github/workflows/endurance-capacity.yml) | **Manuel** 60–110 dk: gerçek SQLite/engine, diskte sentetik Telegram, SQL restart ve p95 |

**Dokuz ana workflow** her `main` push'unda çalışır. `06 Endurance` manuel `workflow_dispatch` testidir ve her push'ta bir saatlik iş başlatmaz. Her job, `GITHUB_STEP_SUMMARY` içine sonucunu yazar. Gerçek Telegram E2E'leri test kanalı secret'larına bağımlıdır; Docker DEV testinin secret ihtiyacı yoktur.

### CI sonucunun anlamı

- **PASS:** Belirtilen SHA üzerindeki ilgili testler geçti; başka bir SHA veya production kabulü için geçerli değildir.
- **FAIL:** Log ve artifact incelenmeli, neden çözülüp **yeni SHA** üzerinde yeniden denenmelidir.
- **CANCELLED / SKIPPED / QUEUED:** Zorunlu kabul testlerinde başarı kanıtı sayılamaz.
- **Release:** `main` üzerinde doğrulama yapılır; paket yayınlama sürüm tag'i veya açık `[publish-release]` işaretiyle tetiklenir. Yayın yolu aynı SHA'nın bağımsız CI matrisini doğrular ve mevcut sürümü yanlışlıkla ezmez.
- **06 Endurance:** Telegram kullanıcısı/production host testi değildir. Tek başına fiziksel multi-host veya doğal Telegram 429 kanıtı vermez.

## P1 geliştirme kabulü — Docker fault injection

**Geliştirme aşamasında gerekli kabul kapısı budur.** Önceden kurulmuş TeleDrive, gerçek legacy dosyaları, Telegram API kimliği, Bot token, production SQL veritabanı veya operatör imzası **gerekmez**.

### Tek komut

Docker Engine + Docker Compose v2 gerekir. GitHub Actions bunu kendi izole runner'ında kurar; yerel makinede kurulum mümkün değilse [Docker P1 workflow'unun](../.github/workflows/resilience-docker.yml) `success` sonucu ve artifact'i kullanılır.

~~~bash
bash scripts/run_docker_fault_injection.sh
# Eşdeğer varsayılan geliştirme kabul sihirbazı:
python scripts/acceptance.py
~~~

Komut, rastgele isimli Compose projesi ve özel çalışma alanında şu servisleri oluşturur: **PostgreSQL 17**, ayrı iki gerçek TeleDrive WebUI/API container'ı ve gerçek SQLAlchemy/TelegramObjectStorage motorunu kullanan senaryo container'ı. Telegram'ın yerine **diske yazan sentetik taşıma** geçer; gerçek Telegram'a bağlanılmaz. Dış dünyaya servis portu açılmaz.

### DEV-01–13 makine tarafından doğrulanan kriterler

| Kapı | Sınanan arıza / davranış | Geçme şartı |
| --- | --- | --- |
| DEV-01 | Temiz PostgreSQL + iki gerçek WebUI süreci | Her iki Basic Auth health endpoint'i başarılı; anonim erişim reddedilir |
| DEV-02 | 0B, 1B, 4097B, 65537B ve 5MiB+7B veri | Kalıcı SQL HEAD/manifest ve disk-backed chunk eşleşir |
| DEV-03 | Yükleme sonrası yerel kaynak kaldırılır | Tam SHA-256 ve chunk sınırı aşan Range readback geçer |
| DEV-04 | Telegram send sonrası belirsiz ACK / SQL crash | Önceden yazılmış send intent kalır; kör çift gönderim engellenir |
| DEV-05 | Eski FAILED nesne ve GC koruması | Belirsiz ACK korunur; fiziksel silme etkinleştirilmez |
| DEV-06 | Sentetik Telegram 429 | Önce SQL gönderim slotu ayrılır; ortak cooldown/ölçüm iki node'da görünür |
| DEV-07 | Gerçek `pg_dump` + bağımsız `pg_restore` | Object ID, SHA-256, chunk offset ve Range korunur |
| DEV-08 | Bozulmuş SQL arşivi | Native restore/`pg_restore --list` hasarlı kopyayı reddeder |
| DEV-09 | Bozuk uzak medya | Checksum hatası kısmi çıktı yayınlamaz; onarım sonrası readback geçer |
| DEV-10 | İzole yaşam döngüsü | Yalnızca rastgele Compose projesi ve volume'leri kaldırılır |
| DEV-11 | Gerçek Web `SIGKILL` + PostgreSQL restart | HEAD, send fence, cooldown ve medya yeniden okunabilir |
| DEV-12 | Eksik chunk ve ikinci chunk'ta timeout | Sağlam mevcut hedef değişmez; onarım sonrası SHA geçer |
| DEV-13 | Birleşik kabul / teardown | Dört doğrulanmış stage, 13/13 kontrol ve başarılı scoped cleanup |

### Test akışı ve kanıt

1. Gerçek Web konfigürasyon yükleyicisiyle iki node doğrulanır; anonim erişim reddedilir.
2. Sentetik boyutlarda kaynaklar yüklenir, SQL manifest/HEAD yazılır, kaynaklar silinir; byte ve Range doğrulaması yapılır.
3. Lost ACK ve **önce slot ayrılmış** sentetik 429 enjekte edilir; send fence / cooldown ortak SQL'de yeniden okunur.
4. `web-a` öldürülür, PostgreSQL yeniden başlatılır; yeniden bağlanan node'lar kalıcı kayıtları okur.
5. Gerçek `pg_dump` alınır, bozuk kopya reddedilir, ayrı restore veritabanında readback tekrarlanır.
6. Uzak chunk bozulması, eksik medya ve ikinci chunk timeout'u sırasında sağlam hedef korunur.
7. `seed.json`, `restarted.json`, `restored.json` ve `acceptance.json` birleştirilir; **13 kapı ve cleanup** geçmeden PASS raporlanmaz.

Çıktı: varsayılan `.docker-fault-evidence/docker-fault-<random>.json`; isteğe bağlı `TELEDRIVE_FAULT_REPORT_DIR`. Kanıt dosyaları mode `0600`; parola/token/session/private chat/DB URL rapora girmez. Temizlik sadece benzersiz Compose projesini `down -v` ile kapsar; **`docker system prune` veya global volume silme yoktur**.

Örnek zorunlu sonuç alanları:

~~~json
{
  "mode": "disposable-docker-development",
  "result": "PASS",
  "docker_project_removed": true,
  "production_data_used": false,
  "real_telegram_tested": false,
  "legacy_cutover_required": false,
  "production_acceptance_signed_off": false
}
~~~

Bu sadece **beklenen şema örneğidir**, gerçekleşmiş koşu çıktısı değildir. Gerçek raporda dört stage, `stages.acceptance.checks_count == 13` ve `all_checks_verified == true` bulunmalı; arıza/temizlik başarısızsa CI kırmızı olmalıdır.

### Korunması gereken regresyonlar

- Ortak cooldown, SQL `reserve_send_slot` çağrısı **önce** yapılmadan dayatılamaz. Senaryolar güvenlik protokolünü gevşetmez.
- Container'ın `/api/health` yanıtı tek başına Web yapılandırmasının geçerli olduğuna kanıt değildir. Fault harness'ı gerçek `load_config` ayrıştırıcısını iki node'da da kullanır; zorunlu `sleep_min_seconds` / `sleep_max_seconds` / `max_files_per_run` / `max_files_per_day` alanları atlanmamalıdır.
- Gerçek Bot/User/üç-worker testleri **ayrı** workflow'lardır. Geliştirme Docker'daki sentetik 429 doğal production 429 kanıtı değildir.

## Kabul sınırı ve ilgili belgeler

P0/P1 DEV geliştirme kapanışı ve bunun production'dan farkı, operatörün gerçek host/DR/cutover prosedürü, Telegram GC sınırı ve P2 backlog'u tek yerde: **[Proje durumu, kabul ve P2](IMPLEMENTATION_STATUS.md)**.

SQL backup'ın genel uygulaması: [Backup & Restore](BACKUP_RESTORE.md). Test altyapısı: [Docker Compose tanımı](../.github/ci/fault-injection/compose.yml), [senaryolar](../scripts/docker_fault_scenarios.py) ve [çalıştırıcı](../scripts/run_docker_fault_injection.sh).

## Dosya düzeni ve çalışma profilleri

- GitHub Actions tanımları yalnızca [`.github/workflows/`](../.github/workflows/) içindedir. Resilience testinin Docker build bağlamı ve yardımcı Dockerfile'ı [`.github/ci/fault-injection/`](../.github/ci/fault-injection/) altında tutulur; GitHub Actions bu dosyaları ayrıca workflow olarak çalıştırmaz.
- Bağımlılıklar [`requirements/server.txt`](../requirements/server.txt) (başsız Web/S3) ve [`requirements/desktop.txt`](../requirements/desktop.txt) (PySide6 masaüstü) olarak ayrılmıştır. GUI paketlerini server image'ına yüklemeyiz. Ortak Python çekirdeğinin metadata'sı `pyproject.toml` içindedir.
- [`docker-compose.yml`](../docker-compose.yml) normalde sadece `teledrive` Web servisini açar. İkinci `s3` ASGI servisi `profiles: [s3]` altında isteğe bağlıdır; `docker compose --profile s3 up -d --build` ile açılır ve aynı image/metadata'yı kullanır. Farklı giriş noktalarının ayrı proses olması güvenlik ve port sınırlarını korur.
- Docker arıza senaryosunda **gerçek** PostgreSQL ve iki ayrı Web container'ı kasıtlıdır: çoklu servis crash/restart testi bunu gerektirir. Bunlar production container'ları değildir.
