# Docker fault injection — sıfırdan kur, boz, kurtar

**Bu test altyapısı ilerideki bütün özellikler için yeniden kullanılır.** Geliştirme kabulü için mevcut bir
production sunucusu, gerçek legacy dosyası, operatör imzası, bir saatlik
production soak veya production Telegram kanalı **gerekli değildir**.
Bunlar dağıtım öncesi sonraki aşamanın ayrı
[`P1_PRODUCTION_ACCEPTANCE.md`](P1_PRODUCTION_ACCEPTANCE.md) kapılarıdır.

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
| DEV-11 | Gerçek SIGKILL + PostgreSQL restart | web-a gerçek container SIGKILL, PostgreSQL restart, HEAD/429/ACK/readback tekrar kontrol edilir |
| DEV-12 | Eksik media + yarıda kopan indirme | Chunk kaybolur veya ikinci chunk'ta timeout olur; sağlam hedef korunur, sonra remote restore ile SHA geçer |
| DEV-13 | Birleşik P1 DEV kabul raporu | Dört kanıt dosyası ve 13 kapı doğrulanmadan PASS olmaz; Compose cleanup sonucu doğrulanır |

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


## CI regresyonu: gönderim slotundan önce sentetik 429 üretme

14 Ekim 2026, `fc255a86` üzerinde Docker kurulumu, iki WebUI health
kontrolü ve SQL manifest seed aşaması başarılıydı. Test senaryosu,
`BotFileIndex.impose_shared_cooldown()` çağrısına gönderim slotunu
`reserve_send_slot()` ile önceden ayırmadan girdiği için
`BotApiError: Bot API shared cooldown row missing` ile durdu.
Bu **üretim protokolünün fail-closed korumasıdır**, Bot API/storage
motorundaki bir hata değildir.

Docker senaryosu artık sırasıyla (1) `interval_seconds=0` ile ortak
SQL gönderim slotunu kalıcı ayırır, (2) sentetik ve kesin 429'u uygular,
(3) bekleme süresinin 7 saniyeye yükseldiğini doğrular, (4) yeni SQL
bağlantısından cooldown ve durable 429 sayacını tekrar doğrular ve
(5) aynı SQL'i iki ayrı Web node üzerinden okur. Testler gerçek Telegram'a
gitmez; doğal üretim 429 kanıtı sayılmaz. Ayrı offline regresyon testi,
eksik slotun reddedilmeye devam etmesini ve doğru sıra sonrası cooldown
başarısını doğrular. Bu kontrol güvenli protokolü gevşetmeden CI hatasını
giderir.


### Docker Web yönetici API'si konfigürasyon yükleme koruması

`1f32159` sonrası test, eksik `upload.sleep_min_seconds` alanı yüzünden
`/api/v1/admin/storage/health` endpoint'inde HTTP 500 verdi. Docker
`/api/health` liveness endpoint'i, konfigürasyon ayrıştırıcısını çağırmadığı
için iki Web container sağlıklı görünüyordu. Sentetik/dev `config.yaml`
artık üretim `load_config` ayrıştırıcısının zorunlu `sleep_min_seconds`,
`sleep_max_seconds`, `max_files_per_run` ve `max_files_per_day`
alanlarını içerir. Fault senaryosu başlamadan **iki Web container'ında**
gerçek yükleyiciyle erken doğrulama yapılır. Offline CI kontrat testi,
script şablonunu `teledrive/uploader.py` içindeki zorunlu alanlarla
karşılaştırır; değişen şema ile senaryonun sessizce ayrışmasını önler.

Bu düzeltmeler hiçbir üretim servisini başlatmaz veya gerçek Telegram
kanalına bağlanmaz; tamamı rastgele adlandırılmış Docker DEV ortamındadır.

### DEV-11, DEV-12 ve DEV-13: otomatik sıfır kurulum kabulü

Yeni gerçek Docker aşamaları: seed -> web-a SIGKILL + PostgreSQL restart ->
restarted -> native PostgreSQL dump/corruption/restore -> restored ->
acceptance -> izole Compose down -v. Bunlar yalnızca benzersiz test projesini
etkiler. Host makinesindeki production container/volume temizlenmez.

DEV-11: iki gerçek TeleDrive Web prosesinin ortak SQL bağlantısı, önceki
manifest HEAD, kayıp ACK fence, sentetik 429 ve medya checksum bilgileri
SIGKILL/SQL restart sonrası yeni test prosesinden tekrar okunur.

DEV-12: bozuk uzak media, eksik Telegram-simülasyon mesajı ve ikinci
parçada transport timeout kontrol edilir. Her arızada mevcut sağlam hedef
korunur ve remote media onarılınca SHA/Range tekrar geçer.

DEV-13: seed.json, restarted.json, restored.json ve acceptance.json
makine tarafından denetlenir. checks_count=13; silme devre dışı.
Teardown başarısız olursa rapor PASS olamaz.

Çalıştırma: python scripts/p1_acceptance_wizard.py
Alternatif: bash scripts/run_docker_fault_injection.sh
Rapor: .docker-fault-evidence/docker-fault-<random>.json (0600).

Github Actions: TeleDrive Docker Fault Injection CI; başarı için
workflow result=success ve artifact'te dört DEV kanıtı bulunmalı.
Bu simüle testlerin hiçbiri gerçek Telegram/üretim multi-host kabulü
veya legacy cutover imzası değildir.
