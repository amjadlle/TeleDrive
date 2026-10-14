# TeleDrive — proje durumu, P0/P1 kabulü ve P2 planı

**Tek güncel proje/acceptance kaynağı.** Sürüm kanıtı, P0, P1 geliştirme, production operator adımları ve P2 kalanları burada toplanır. CI workflow'ları, Docker kurulumu ve 13 DEV testinin **teknik ayrıntısı** [CI ve test rehberinde](CI_WORKFLOWS.md). Güncel TODO: [TODO.md](../TODO.md). Geçmiş çalışma notları Git tarihinde kalır.

## 1. Sürüm ve kabul sınırı

| Aşama | Durum | Ne kanıtlandı? |
| --- | --- | --- |
| **P0 — desteklenen uploader/REST/S3 alt kümesi** | **DEV kabul edildi** | Temel transfer, doğrulama, kimlik/izin testleri; tam AWS S3 paritesi **değil** |
| **P1 — Docker geliştirme kabulü** | **DEV kabul edildi** | İki WebUI + gerçek PostgreSQL, 13 arıza/restore/temizlik kapısı; Telegram **sentetik** |
| **P1 — gerçek test kanalı** | **DEV kabul edildi** | Bot, User MTProto, üç bağımsız worker + PostgreSQL E2E |
| **P1 — gerçek production/operator** | **ONAYLANMADI** | Fiziksel bağımsız sunucu, orijinal kanal DR ve sahip onayı olmadan verilemez |
| **P2 — gelişmiş kurtarma/GC/S3** | **DEV sürüyor** | Aşağıda yapılanlar ve kalan güvenlik kapıları ayrılmıştır |

**Yayınlanmış kilometre taşı:** [TeleDrive v1.0.7](https://github.com/babico/TeleDrive/releases/tag/v1.0.7), kaynak SHA `747747652cd1ca36878453d49daf627d8c0a60ad`. Linux ve Windows paketleri mevcut. Release kaynak SHA'sı ile daha yeni `main` üzerindeki P2 geliştirme kodu **aynı sürüm değildir**. Yeni release kabulü için tüm zorunlu CI testleri **aynı yeni SHA** üzerinde başarıyla bitmelidir; iptal/atlanan test sayılmaz.

### P0 geliştirme kabul kriterleri ve kanıtlar

- **Kalıcı transfer:** SQLAlchemy (SQLite/PostgreSQL/MySQL), immutable manifest/chunk, SHA-256 tam/Ranges, dayanıklı job journal, fencing, kaynak değişimi ve belirsiz Bot ACK'de fail-closed tutum.
- **HTTP/API güvenliği:** Ayrı REST ve S3 yetkileri, süre/iptal koşulları, erişim kontrolleri, SigV4 doğrulaması, idempotent/koşullu işlemler; desteklenmeyen davranışların sessiz başarı sayılmaması.
- **Uyumlu alt küme:** REST ve S3 gateway; CRUD, multipart, conditional, copy, sürüm/etiket/CORS ve sınırlı presigned işlemler; boto3, AWS CLI, rclone için gerçek süreç testleri. Cyberduck ve AWS'nin bütün lifecycle/policy/SSE davranışları onaylı değildir.
- **Orijinal test-kanalı Telegram:** Gerçek Bot ve User E2E ile PostgreSQL'e bağlı üç bağımsız worker; bunlar **production kaynak medyası** değildir.
- **Arıza/restore:** PostgreSQL/MySQL native yedek/geri yükleme, Docker fault injection ve Linux/Windows masaüstü testleri.

Tek-SHA başarılı DEV taban çizgisi: `a7e3adf`. Kanıt koşuları: [Security](https://github.com/babico/TeleDrive/actions/runs/38565237975), [REST/S3](https://github.com/babico/TeleDrive/actions/runs/38565238070), [Database](https://github.com/babico/TeleDrive/actions/runs/38565238030), [Desktop](https://github.com/babico/TeleDrive/actions/runs/38565237852), [Bot](https://github.com/babico/TeleDrive/actions/runs/38565237802), [User](https://github.com/babico/TeleDrive/actions/runs/38565237851), [Multiworker](https://github.com/babico/TeleDrive/actions/runs/38565237918), [Docker P1](https://github.com/babico/TeleDrive/actions/runs/38565237956) ve [Release gate](https://github.com/babico/TeleDrive/actions/runs/38565237938). [Tüm güncel Actions](https://github.com/babico/TeleDrive/actions).

### P1 geliştirme kabulü

P1 DEV için Docker senaryosunda **DEV-01–DEV-13'ün tamamı**, dört stage kanıtı ve cleanup zorunludur. **Gerçek Bot/User/Multiworker workflow'ları** da kendi test kanalında çalışmalıdır. Bunun tek komutu ve kontrol matrisi [CI rehberinde](CI_WORKFLOWS.md#p1-geliştirme-kabulü--docker-fault-injection) tutulur; burada ikinci, zamanla farklılaşacak 13 maddelik kopya yoktur.

Production ortamı henüz kurulmadıysa orijinal legacy folder, bir saatlik **production** Telegram 429, production kanal medya geri yüklemesi veya operatör imzası **DEV kriteri değildir**.

## 2. Production/operator kabulü — henüz yapılmadı

Bu bölüm ancak gerçek host'lar, sahibine ait Telegram hesabı ve saklanan orijinal SQL/medya var olduğunda uygulanır. **CI PASS production imzası değildir.** Token, session, DB URL veya gerçek orijinal kanal verilerini GitHub'a yüklemeyin.

### A. Fiziksel bağımsız host ve doğal 429 soak

En az iki **fiziksel olarak ayrı** uygulama host'u ve ortak PostgreSQL/MySQL kurulur. Her host'un bağımsız gözlemle elde edilmiş, SHA-256 ile saklanan makine kimliği ile ortak SQL kimliği kayıt altına alınır. İki farklı URL'nin aynı makineye çıkması kabul edilmez. Doğal Telegram 429 trafik altında görülür; Telegram limitlerini zorlamak için flood yapılmaz.

Korunan ortamda `TELEDRIVE_P1_HEALTH_HOSTS` (iki–sekiz farklı HTTPS endpoint'i içeren `label`/`url` JSON), `TELEDRIVE_P1_BASIC_USERNAME` ve `TELEDRIVE_P1_BASIC_PASSWORD` sağlandıktan sonra:

~~~bash
python scripts/monitor_p1_soak.py \
  --output /secure/acceptance/soak.json \
  --duration-seconds 3600 \
  --interval-seconds 60
~~~

Rapor `telemetry_gate_passed=true`, en az bir saat, her host için gerçek pozitif 429 gözlemi, ortak counter uyumu, unresolved ACK olmaması ve boşalan kuyruk göstermelidir. Bu **yalnızca telemetri** kanıtıdır; fiziksel bağımsızlık ayrı doğrulanır.

### B. Orijinal Telegram medyası ile DR

Kaynak SQL snapshot ve Telegram credential ayrı korunan alanlarda saklanır. Geri yükleme **ayrı staging veritabanında** yapılır. SQLite için [Backup & Restore](BACKUP_RESTORE.md) rehberindeki `scripts/backup_metadata.py` ve `scripts/verify_metadata_backup.py` araçları kullanılır. Kaynağın **orijinal Bot hesabı ve kanalıyla**:

~~~bash
python scripts/rehearse_telegram_dr.py \
  --snapshot /secure/backups/sealed.db \
  --sha256 EXPECTED_ARCHIVE_SHA256 \
  --bucket ORIGINAL_BUCKET --key ORIGINAL_KEY \
  --account ORIGINAL_ACCOUNT --chat-id ORIGINAL_CHAT_ID
~~~

Araç mühürlü arşiv SHA'sını, izole SQL verisini, gerçek Telegram'dan tam SHA/size ve cross-chunk Range'i doğrular; upload/delete yapmaz. PostgreSQL/MySQL için native dump ve ayrı restore yapılır; yalnızca SQL'in açılması **orijinal Telegram verisinin kurtarıldığını kanıtlamaz**.

### C. Gerçek legacy varsa cutover (yoksa atlanır)

Legacy uploader durdurulur, kaynak dizin yazmaya kapatılır, lease güvenceye alınır, onaylı kaynak köküne sınırlandırılır. Gerçek legacy kaynak **yoksa** sahte test veya cutover üretmeyin: bağımsız owner kanıtıyla `no_legacy_source_attested=true`, cutover `{"not_applicable":true}` kaydedilebilir.

~~~bash
python scripts/verify_legacy_cutover.py \
  --database /secure/isolated.db \
  --source-root /approved/legacy --allowed-root /approved \
  --scratch-dir /secure/scratch --bucket ORIGINAL_BUCKET \
  --account ORIGINAL_ACCOUNT --chat-id ORIGINAL_CHAT_ID
~~~

`verified=true`, eksiksiz tarama, sıfır queued/inconsistent, bütün kaynak inode/revizyon/SHA değerlerinin güncel HEAD ve orijinal Telegram bytes ile eşleşmesi gereklidir. Operatör onayı olmadan canlı yönlendirme değiştirilmez.

### D. İki imzalı production kanıtı

`scripts/verify_production_bundle.py`; `release_sha`, `topology`, `soak`, `dr`, `cutover`, `original_telegram_data_attested` ve iki bağımsız imzayı kabul eder. `topology.hosts` her host için `label`, `machine_id_sha256` ve ortak `shared_database_sha256` taşır; `independent_hosts_owner_attested=true` gerekir. `soak` / `dr` / `cutover` alanları **gerçek araçların çıktısı** olmalıdır.

Aynı kanonik JSON (imza `approvals` alanı hariç) iki farklı kişi tarafından ayrı Ed25519 private key ile imzalanır; `approvals.owner` ve `approvals.operator` base64 imzadır. 32 baytlık public key dosyaları bağımsız elde edilir:

~~~bash
python scripts/verify_production_bundle.py \
  --bundle /secure/acceptance/production-bundle.json \
  --release-sha REVIEWED_40_HEX_COMMIT_SHA \
  --owner-public-key /secure/keys/owner.pub \
  --operator-public-key /secure/keys/operator.pub
~~~

Yanlış SHA, eksik koşu, aynı fiziksel host, hatalı mesaj/medya ve eksik imza **fail-closed** reddedilir. Bu denetimin başarılı olması **otomatik deployment onayı değildir**: `production_acceptance_signed_off=false` ve `physical_telegram_deletion_enabled=false` kalır. Son karar orijinal sistem sahibine ve ayrı change-control'a aittir.

## 3. P2 — güvenli GC, kurtarma, kapasite ve S3

### Fiziksel Telegram GC: DEV güvenlik koruması var, silme izni **yok**

**Var:** `StorageGCInventory` retention/FAILED ve HEAD/alias/published-version/multipart/Bot ACK referanslarını tarar. `RecoveryManager.cleanup` silme yolunda SQL HEAD, yayınlanmış/removed sürümler, copy alias, multipart part, pre-send intent ve Bot receipt gibi referansları tekrar kontrol eder. Telethon probe hesap/kanal/marker eşleşmesi arar.

**Henüz yok:** Bütün sürümleri kapsayan transactional mark/sweep; eşzamanlı writer bariyeri; uzaktan silme öncesi bağımsız tam sahiplik taraması ve **kurtarılabilir orijinal medya yedeği**; eksiksiz audit/iki-personel izni. Telegram'dan silinen **orijinal message ID geri alınamaz**; sıradan SQL backup geri yüklemesi rollback sayılmaz. Bu nedenle WebUI/API üzerinde otomatik fiziksel GC **kapalıdır**. Riskli durumları “test geçsin” diye açmayın.

Güvenli yayın şartı: imzalı dry-run planı, complete scan, HEAD/sürüm/alias/retention doğrulaması, bağımsız payload snapshot + kurtarma provası, writer fencing ve iki kişinin açık yetkilendirmesi. Belirsiz Bot ACK nesneleri korunur.

### Telegram'dan metadata rebuild: kanıtlanmış nesnelerde destekleniyor

`teledrive.storage.rebuild.restore_isolated_catalog_batch` ve `scripts/rebuild_from_telegram.py`; orijinal Telethon oturumuyla Telegram mesajını, tam UUID/ordinal/SHA-prefix marker'ını, account/chat/message kimliğini ve **bağımsız verilen** tam chunk ve nesne SHA-256 değerlerini kontrol eder. Çıktı **mevcut production SQL'in üzerine değil, yepyeni izole SQLite veritabanına** alınır.

Tek nesnelik plan alanları: `object_id`, `bucket`, `key`, `sha256`, `size` ve `chunks` dizisi; her chunk'ta `ordinal`, `account`, `chat_id`, `message_id`, `size`, `sha256` zorunludur. Çoklu plan biçimi `{"objects":[ ...tam nesne planları... ]}`. 1–128 nesne, en fazla 4.096 chunk ve toplam 1 GiB sınırı vardır. Tüm uzaktan medya doğrulanmadan SQL yayınlanmaz; geçici DB `PRAGMA integrity_check` ile kontrol edilir, `0600` izniyle mevcut hedef ezilmeden atomik yayınlanır. Bir nesnenin bozulması **kısmi katalog bırakmaz**.

~~~bash
# TELEDRIVE_REBUILD_API_ID / TELEDRIVE_REBUILD_API_HASH /
# TELEDRIVE_REBUILD_SESSION korunan ortamdan sağlanır.
python scripts/rebuild_from_telegram.py \
  --plan /secure/verified-plan.json \
  --new-database /secure/isolated-rebuilt.db \
  --account original-user \
  --target ORIGINAL_TELEGRAM_CHANNEL
~~~

**Sınır:** Telegram mesaj caption'ı tek başına bucket/key, kayıp sürüm geçmişi veya bilinmeyen bütün mesajların güvenilir eşlemesini kanıtlamaz. Orijinal metadata yoksa otomatik tam keşif **açık P2 işidir**. Bot unknown-ACK için bağımsız güvenilir `file_id` gerekir. Geri kurma komutu remote upload/delete yapmaz.

### P2 kapasite: sentetik ve gerçek host testleri ayrıdır

[Manuel 60–110 dk dayanıklılık workflow'u](../.github/workflows/p2-capacity-soak.yml) gerçek SQLite/engine, diskte kalıcı sentetik Telegram, periyodik SQL restart, geçmiş nesne readback, SHA ve p50/p95 ölçer. Normal `main` push'unda bir saatlik iş tüketmez. **Gerçek Telegram, doğal 429, fiziksel host, production soak değil.**

Gerçek production sunucusu olduğunda en az 1 saatlik host telemetrisi ve uzun-dönem kapasite/latency profili ayrıca toplanacaktır.

### P2 S3 ve istemci kapsamı

AWS lifecycle, bucket policies, SSE, ileri checksum/SDK davranışları, eksik S3 varyantları ve kanıtlanmamış üçüncü taraf istemciler (örn. Cyberduck) halen açığa alınmamış özelliklerdir. Desteklenen P0/P1 alt kümesiyle karıştırmayın.

## 4. Açık işler ve kapanış politikası

- [x] P0/P1 **geliştirme** kanıtlarının bağlı olduğu v1.0.7 milestone yayımlandı.
- [x] Docker 13 kapılı DEV, ayrı gerçek test-kanalı E2E ve release matrix kontrolü eklendi.
- [x] Korumalı kurtarma, tek/çok nesneli izole Telegram rebuild ve iki imzalı offline production rapor denetimi geliştirildi.
- [ ] Sonraki release SHA'sının bütün zorunlu workflow'ları tek SHA'da başarılı olmalı (başka SHA'nın yeşili geçmez).
- [ ] Production owner fiziksel host, orijinal medya DR ve varsa legacy cutover kanıtını sağlamalı.
- [ ] Fiziksel GC için bağımsız restore kanıtı + writer bariyeri + onay tamamlanmadan deletion açılamaz.
- [ ] Otomatik bilinmeyen Telegram geçmişi keşfi, P2 advanced S3 ve production ölçek testleri geliştirilmeli.

Operatörlerin toplu iş listesi: [TODO.md](../TODO.md). Sürüm günlüğü için ayrı `docs/RELEASE_NOTES_*` dosyaları tutulmaz; [GitHub Releases](https://github.com/babico/TeleDrive/releases) sayfası notları üretir.
