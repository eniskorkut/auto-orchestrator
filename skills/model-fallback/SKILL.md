---
name: model-fallback
description: Antigravity, Codex ve OpenCode arasında limit bazlı iş devri. Use when Antigravity (Gemini) veya Codex kotası azalıyor/bitiyor, kullanıcı "limit kontrol et", "kota", "devret", "fallback", "iş yarım kalmasın" diyor; ya da uzun bir göreve başlamadan önce hangi modelin kullanılacağına karar vermek gerekiyor.
---

# Model fallback — limit dolmadan devir

Amaç: bir sağlayıcının kotası bitmek üzereyken işi **başka bir modele** devretmek,
böylece görev yarım kalmasın. Kontrol tamamen yerel: hiçbir bulut API'sine
kota sorgusu atılmaz, kimlik bilgisi kullanılmaz.

## Eşikler

| Pencere | Kalan eşiği | Rol |
|---|---|---|
| 5 saatlik | **≤ %10** | **ana tetik** — devir ve IDE seçimi burada yapılır |
| Haftalık | **≤ %5** | emniyet kemeri — 5 saatlik taze olsa bile hafta sonuna dayandıysan erken devreder |

Tek eşikle çalıştırmak istersen: `--weekly-low 0` (haftalık kontrolü kapatır).

**Reset kredileri asla otomatik harcanmaz.** `codex-quota.py` sadece sayısını
raporlar; harcama kararı kullanıcınındır. Limit dolduğunda önce devir yapılır,
kredi harcama teklif edilmez.

## Paralel yürütme (zorunlu)

Bağımsız işler **her zaman paralel** yürütülür; birbirinin çıktısını beklemeyen işleri
sıraya dizmek yasaktır.

- **Wave-0:** İşin başında bağımlılık haritasını çıkar. Bağımsız olan her şeyi aynı
  anda başlat (Scaffolding, doküman, test, UI, ayrı katmanlar). "Önce şu bitsin"
  sadece gerçek bir veri/API bağımlılığı varsa geçerlidir.
- **Eşzamanlılık:** Aynı anda en fazla **3** alt ajan. Daha fazlası bağlam/çakışma
  maliyeti getirir; sıraya al, boşalan yuvaya yenisini ver.
- **Bağımlılık:** Sadece gerçek bağımlılık bekler (şema, migration, paylaşılan arayüz).
  "Aynı dosya" bağımlılık değildir; dosya sahipliğini ayır ve paralel koş.
- **Milestone sonrası:** Biten iş grubundan sonra kalan işi **yeniden** böl;
  ilk turda bölüp sonra her şeyi kendin yapma.
- **Sözleşme:** Her alt ajana hedef, dosya kapsamı, kabul kriteri ve dönüş formatı ver.
  Çakışan dosya sahipliği verme; sonucu doğrulamadan entegre etme.
- **Doğrulama:** Paralel yürütme hız içindir; entegrasyon ve testler tek elden
  (orkestratörde) yapılır.

## Davranış kuralı (eşik aşıldığında)

1. **Yeni task başlatma.** O an çalışan adımı bitir (yarım bırakma).
2. Handoff kaydını yaz (`handoffs/<run>-H<ts>.v1.json`): yapılmış / in-flight / kalan.
3. Hedefi seç ve **IDE'yi de değiştir**: Codex CLI, Antigravity veya OpenCode.
4. Devret, sonucu doğrula, eski sahipliği bırak.

Eşik %10 iken tipik olarak ~30 dakikalık kullanım payı kalır; handoff yazmak ve
geçiş yapmak için yeterli. %0'ı beklemek, geçiş sırasında kota hatası alma riski
demektir.

## Komutlar

```sh
~/.config/opencode/scripts/model-fallback.py --status      # üç sağlayıcının durumu
~/.config/opencode/scripts/model-fallback.py --from antigravity
~/.config/opencode/scripts/model-fallback.py --from codex
~/.config/opencode/scripts/model-fallback.py --from codex --json
```

Alt scriptler tek başına da çalışır:

```sh
~/.config/opencode/scripts/antigravity-quota.py [--json]
~/.config/opencode/scripts/codex-quota.py [--json]
```

Sohbetten tek adımda kontrol: **`/quota`** (global komut: `commands/quota.md`).
Codex verisi, Codex her çalıştığında tazelenir; `--status` çıktısındaki
"veri yasi" satırına bak.

## Karar tablosu

| Kaynak | Durum | Hedef |
|---|---|---|
| Antigravity | kalan > eşik | devam (devir yok) |
| Antigravity | tükendi + Codex müsait | **Codex** `gpt-6-luna`, effort `max` |
| Antigravity | tükendi + Codex de tükendi | **OpenCode** `opencode-go/deepseek-v4.1-flash#max` |
| Codex | müsait | devam |
| Codex | tükendi + Antigravity müsait | **Antigravity** `Gemini 3.8 Flash (Medium)` |
| Codex | tükendi + Antigravity de tükendi | **OpenCode** `opencode-go/deepseek-v4.1-flash#max` |
| OpenCode | Codex penceresi sıfırlandıysa | **Codex**'e geri devir |
| OpenCode | Codex yok, Antigravity sıfırlandıysa | **Antigravity**'ye geri devir |
| OpenCode | ikisi de tükendi | burada kal (son çare) |

### İşçi (subagent) modeli — önce v4.1-flash, erişim yoksa yerel

Kural: **alt ajanlar önce `opencode-go/deepseek-v4.1-flash#max` kullanmayı dener.**
O sağlayıcıya erişimi olmayan araç, kendi direksiyon modelini kullanır.

| Orkestratör | Alt ajan (1. tercih) | Erişim yoksa (fallback) |
|---|---|---|
| OpenCode | `opencode-go/deepseek-v4.1-flash#max` | — (erişim var) |
| Codex | `opencode-go/deepseek-v4.1-flash#max` ✗ | `gpt-6-luna`, effort `max` |
| Antigravity | `opencode-go/deepseek-v4.1-flash#max` ✗ | `Gemini 3.8 Flash (Medium)` |

**Direksiyon (orkestratör) modeli de aynı:**

| Araç | Orkestratör modeli |
|---|---|
| OpenCode | `opencode-go/deepseek-v4.1-flash#max` |
| Codex | `gpt-6-luna` + `model_reasoning_effort = "max"` (`~/.codex/config.toml`) |
| Antigravity | `Gemini 3.8 Flash (Medium)` (`last_selected_agent_model = MODEL_PLACEHOLDER_M319`) |

Kanıt (Codex): `~/.codex/config.toml` içinde provider tanımı yok — yalnızca OpenAI
aile­si. Alt ajan thread'lerinin tamamı `gpt-6-luna` / `gpt-5.6-terra` /
`gpt-6.1-terra`; hiçbiri deepseek değil.

### Codex'i OpenCode Go'ya bağlama (evren DEĞİL)

OpenCode Go OpenAI-uyumlu bir uç sunuyor:

```
base_url : https://opencode.ai/zen/go/v1
model    : deepseek-v4.1-flash
header   : x-opencode-session: <herhangi bir oturum kimliği>   (zorunlu)
auth     : OPENCODE_API_KEY  (güncel anahtar proje .env'inde; auth.json'daki eski olabilir)
```

**Anahtar kaynağı:** `~/Desktop/english-work/.env` içindeki `OPENCODE_API_KEY`
(51 karakter). `~/.local/share/opencode/auth.json` içindeki `opencode-go` kaydı
**eski** olabilir ve `GoUsageLimitError` döndürür — önce `.env` okunur.

Doğrulama (2026-11-08): aynı anahtarla
`POST https://opencode.ai/zen/go/v1/responses` → **200**,
`/chat/completions` → **200**. Yani uç ve anahtar çalışıyor.

`~/.codex/config.toml` içine eklenen blok:

```toml
[model_providers.opencode-go]
name = "OpenCode Go"
base_url = "https://opencode.ai/zen/go/v1"
env_key = "OPENCODE_API_KEY"
wire_api = "responses"          # Codex 0.164 "chat" desteklemiyor
http_headers = { "x-opencode-session" = "codex-handoff" }
```

Kullanım (anahtar dosyadan okunur, kabuğa yazılmaz):

```sh
~/.config/opencode/scripts/codex-opencode.sh exec -C <proje> - < brief.md
```

`codex-opencode.sh` anahtarı sırayla `$OPENCODE_ENV_FILE`,
`~/Desktop/english-work/.env`, `auth.json` üzerinden bulur.

## Durum (2026-11-08 01:06): **çalışıyor**

Yanlış anahtar (auth.json'daki eski kayıt) `GoUsageLimitError` veriyordu. Güncel
anahtar `english-work/.env` içinde; doğru base URL `https://opencode.ai/zen/go/v1`.

Doğrulama:
- `POST /responses` → **200**
- `POST /chat/completions` → **200**
- `codex-opencode.sh exec … "Sadece OK yaz"` → **OK** (uçtan uca)

Kota sınırı yok; Codex artık deepseek-v4.1-flash ile çalışabilir ve alt ajanları
bunu miras alır.

Kalan uyarı: Codex `deepseek-v4.1-flash` için model metaverisi bulamıyor
("Model metadata not found, defaulting to fallback"). Uzun görevlerde context
penceresi/araç davranışı bozulursa `model_context_window` / `model_max_output_tokens`
veya `model_catalog_json` ile metaveri ver — ama bunu gerçek bir çağrıyla test et.

Devirde kullanım:

```sh
HANDOFF_MODEL=opencode ~/.config/opencode/scripts/codex-handoff.sh <proje> <brief> "<başlık>"
```

`HANDOFF_MODEL` verilmezse Luna kullanılır.

### DeepSeek'i her IDE'den çalıştırma — önce `opencode run`

Kod yazdırmak, dosya değiştirtmek veya araç çalıştırmak gerekiyorsa deepseek'i
**ajan** olarak çalıştır. Bunun taşınabilir yolu OpenCode CLI'dır; **curl gerekmez.**

```sh
opencode run --model opencode-go/deepseek-v4.1-flash#max --auto \
  -f <brief.md> "<talimat>"
```

| Parametre | İş |
|---|---|
| `--model` | `opencode-go/deepseek-v4.1-flash#max` |
| `--auto` | izinleri otomatik onaylar (dikkatli kullan) |
| `-f` | brief/dosya bağlar |
| `--session <id>` | aynı oturumda devam eder |
| `--agent` | belirli bir ajan profili |

Bu gerçek bir ajandır: dosya okur/yazar, komut çalıştırır, test koşar.

Doğrulama (2026-11-08): geçici bir `greet.py` üzerinde
`opencode run --standalone --model opencode-go/deepseek-v4.1-flash#max --auto "..."`
çalıştırıldı; dosya doğru şekilde düzenlendi (boş isim ve sıfıra bölme guard'ları
eklendi) ve ajan kendi doğrulama çıktısını üretti.

**Hangi IDE'de nasıl:**

| IDE | Yöntem | Kod yazabilir |
|---|---|---|
| OpenCode | doğrudan model / subagent | ✅ |
| Codex | native provider (`-c model_provider=opencode-go`) **veya** `opencode run` | ✅ |
| Antigravity | terminalden `opencode run` | ✅ |
| Antigravity | curl (`deepseek-ask.sh`) | ❌ sadece metin |

Codex'te native provider tercih edilir; oturum Codex uygulamasında sohbet olarak
görünür. Diğer her yerde `opencode run` yeterlidir.

### Curl ne zaman hâlâ işe yarar (opsiyonel)

Ajan açmaya değmeyecek **tek soruluk** işler için:

```sh
~/.config/opencode/scripts/deepseek-ask.sh "Bu hata mesajı ne demek?"
echo "uzun baglam" | ~/.config/opencode/scripts/deepseek-ask.sh "Bunu ozetle"
```

**Kritik sınır:** curl çağrısı deepseek'i **ajan yapmaz**. Araç çağıramaz, dosya
okuyamaz/yazamaz, kod çalıştıramaz — sadece metin üretir. Kod değişikliği
gerekiyorsa `opencode run` (veya Codex provider) kullanılır, curl değil.

Curl'ün kaldığı tek yer: Antigravity'de ajan açmadan hızlı bir metin cevabı almak
veya kota/gecikme açısından tek istek atmak.

### Dosya değiştirtme (kod yazdırma) — `deepseek-code.py`

"Şu dosya + şu talimat → değişmiş dosya" akışı. Model dosyayı görür, talimatı alır,
**tamamını** değiştirilmiş olarak döndürür; script ayıklar ve farkı gösterir.

```sh
~/.config/opencode/scripts/deepseek-code.py <dosya> -i "talimat"
~/.config/opencode/scripts/deepseek-code.py <dosya> -i "talimat" --apply
~/.config/opencode/scripts/deepseek-code.py <dosya> -i "talimat" --out /tmp/yeni.py
~/.config/opencode/scripts/deepseek-code.py <dosya> -i "talimat" --json
```

Davranış:

1. Dosyayı okur, modele "sadece değiştirilmiş dosyanın tamamını tek kod bloğunda ver"
   der (açıklama/refactor yasak).
2. Cevabı `<dosya>.deepseek.new` olarak yazar, `unified diff` basar.
3. `--apply` yoksa **orijinale dokunmaz**.
4. `--apply`: `<> .bak-<zaman>` yedeği alır, sonra yazar.

Güvenlik kontrolleri (script içinde):

- Boş cevap → reddedilir
- Orijinalin %25'inden kısa / 4 katından uzun cevap → reddedilir
- `.py` dosyasında `compile()` ile sözdizimi kontrolü; hata varsa yazılmaz
- Kod bloğu ayıklanamazsa ham metin kullanılır (yine kontrollerden geçer)

Kullanım deseni:

1. Tek dosya seç (çok dosyalı iş için dosya başına ayrı çağrı yap).
2. Talimatı **dar** tut: "şu fonksiyonu düzelt", "şu hatayı gider" — "refactor et" değil.
3. Diff'i oku; beklenmeyen değişiklik varsa `--apply` yapma.
4. Uyguladıktan sonra **testleri kendi alt ajanınla** çalıştır. Model kodu çalıştırmaz,
   sadece metin üretir.

Örnek (gerçek test):

```sh
$ deepseek-code.py math_utils.py -i "topla hatasını düzelt; ortalama boş listede 0.0 döndürsün"
OK: math_utils.py.deepseek.new
DEĞİŞİM: +3 / -1 satır
-    return a - b
+    return a + b
+    if not values:
+        return 0.0
```

### Antigravity sınırı

Antigravity model listesi kapalı bir Gemini ailesi; özel/OpenAI-uyumlu sağlayıcı
tanımlama alanı yok. MCP araçları eklenebilir ama **model** olarak deepseek
kullanılamaz. Bu yüzden Antigravity'de doğru davranış her zaman
`Gemini 3.8 Flash (Medium)`.


Antigravity de kendi Gemini ailesine bağlı; `agentapi`/UI üzerinden seçilen model
ne ise alt ajanlar onu miras alır.



Antigravity'de pencere sınıfı reset süresinden türetilir: ≤6 saat → 5 saatlik
(eşik %10), >6 saat → haftalık (eşik %5). Varsayılan izlenen model
`Gemini 3.8 Flash (Medium)`; `--antigravity-model` ile değiştirilir.

## Ne zaman kontrol et

1. **Uzun bir göreve başlamadan önce** (birden fazla dosya, >15 dk sürecek iş).
2. Kullanıcı "limit/kota/devret" dediğinde.
3. Bir model hatası `RESOURCE_EXHAUSTED`, 429, "quota exceeded", "rate limit"
   içerdiğinde — tekrar denemeden önce `--status` çalıştır.
4. 20+ araç çağrısı süren işlerde arada bir (ör. her büyük aşama sonunda).

## Devir prosedürü

Her devirde önce **brief** yaz, sonra hedefe ver. Brief olmadan devretme:
bağlam kaybı işi yarım bırakır.

### Devir işini de v4.1-flash yapar (kota koruması)

Eşik %10'a indiğinde elimizde sınırlı kota kalır. Brief yazmak, repo taramak,
handoff kaydı üretmek dakikalar sürebilir; bu işi direksiyondaki pahalı model
yaparsa %10 tükenir ve devir yarım kalır. Bu yüzden **devir hazırlığı da v4.1-flash
alt ajanına verilir**:

1. **Prep (v4.1-flash)** — orkestratör `handoff_prep` ajanını çağırır:
   `subagent(agent="handoff_prep", model="opencode-go/deepseek-v4.1-flash#max", prompt="<proje, hedef, sebep>")`.
   Ajan brief'i yazar, `handoff.py --dry-run` çalıştırır, kopyala-çalıştır komutu döner.
2. **Onay (orkestratör)** — 3 maddelik kontrol: doğru proje mi, kalan işler planla
   tutarlı mı, kabul kriterleri gerçek komutlar mı. Onay veya tek revizyon.
3. **Yürütme (aynı ajan)** — prep ajanına "handoff kaydını yaz ve komutu üret"
   denir (dry-run kaldırılır). Kayıt diske düşer, komut orkestratöre döner.
4. **Devir (orkestratör)** — tek mesajla hedefi başlatır (`codex exec ...` veya
   kullanıcıya Antigravity'de model değiştirtme).

Böylece pahalı model yalnızca ~2 kısa mesaj harcar; tarama/yazma işi flash'ta kalır.
Kota yine de biterse brief ve kayıt **diskte** olduğu için yeni sahip oradan devam eder.

`brief` dosyası: `~/.config/opencode/handoffs/<YYYYMMDD-HHMM>-<proje>-brief.md`
(proje repo'suna yazma; devir artefaktı repo'ya ait değil)

### 0. Makine okunur devir kaydı

```sh
~/.config/opencode/scripts/handoff.py --project <proje> --to <codex|antigravity|opencode> \
  --reason "quota: <kaynak> <pencere> %<kalan> <= %<esik>" --dry-run
```

`--dry-run` hiçbir dosya yazmaz; JSON'u gösterir. Onaylanınca aynı komutu
`--dry-run` olmadan çalıştır: `.auto-orchestrator/handoffs/<run>-H<ts>.v1.json`
atomik olarak yazılır, var olan dosya ezilmez.

Not: kayıt `plans/<run>.json` içeriğine dayanır. Plan eskiyse
`completed/in_flight/next` alanları da eskiyi yansıtır — devirden önce planın
güncel olduğunu doğrula.

```md
# Görev
<tek paragraf hedef>

# Bağlam
- Proje: <yol>
- İlgili dosyalar: <yollar>
- Şimdiye kadar yapılanlar: <özet + commit>

# Kabul kriterleri
- [ ] <ölçülebilir madde>
- [ ] <test/komut>

# Kısıtlar
- <dokunulmayacak dosyalar, stil, sürüm>
```

### Hedef: Codex (`gpt-6-luna`, effort max)

> `--project` **zorunlu**. Verilmezse script devretmez: varsayılan olarak çalışma
> dizinini kullanmak yanlış projeye devretmeye yol açar. Script ayrıca projenin
> gerçekten bir proje olduğunu (`.git`/`.auto-orchestrator`/`PROJECT.md` vb.) ve
> brief'in o proje yolundan bahsettiğini doğrular.

```sh
~/.config/opencode/scripts/model-fallback.py --from antigravity \
  --project /Users/eniskorkut/Desktop/<proje> \
  --brief ~/.config/opencode/handoffs/<ts>-<proje>-brief.md
```

Çıktıdaki `KOMUT` satırı doğrudan çalıştırılabilir:

```sh
codex exec -m gpt-6-luna -c model_reasoning_effort="max" \
  -C "<proje>" -s workspace-write --skip-git-repo-check \
  - < <brief.md>
```

**Uygulamada sohbet olarak görünmesi için** `codex-handoff.sh` kullan (önerilen):

```sh
~/.config/opencode/scripts/codex-handoff.sh <proje> <brief> "<sohbet başlığı>"
```

Sarmalayıcı `codex exec --json` çalıştırır, `thread.started` id'sini yakalar ve
`codex-link-project.py` ile thread'i projeye bağlar. Bağlanmazsa Codex CLI
oturumları `threads.project_id = NULL` kalır; uygulama onları "Projeler"
altında değil yalnızca "Yakın zamanlar"da gösterir.

Elle bağlama / başlık düzeltme:

```sh
~/.config/opencode/scripts/codex-link-project.py --thread <id> \
  --project <proje> --title "<başlık>"
```

- Uzun çıktıyı dosyaya al: `... > .opencode/handoff/<ts>-result.md 2>&1`
- Codex oturumu da kota tüketir; devri **tek** kez yap, parça parça gönderme.

### Hedef: Antigravity (`Gemini 3.8 Flash (Medium)`)

Antigravity'de model seçimi kullanıcı arayüzünden yapılır:

1. Kullanıcıya brief'i ver ve Antigravity'de modeli `Gemini 3.8 Flash (Medium)` yapmasını söyle.
2. Alternatif (deneysel): `~/.gemini/antigravity/bin/agentapi new-conversation --model=flash "<brief>"`.
   Bu yeni bir konuşma açar; sonucu otomatik geri getirmez, kullanıcıya bildir.

### Hedef: OpenCode (`opencode-go/deepseek-v4.1-flash#max`)

Alt görev aracıyla devret:

```
subagent(agent="general", model="opencode-go/deepseek-v4.1-flash#max",
         prompt="<brief içeriği + kabul kriterleri>")
```

OpenCode oturumunun kendi modelini değiştirmek mümkün değil; bu yüzden devir
**subagent** ile yapılır. Kullanıcı ana oturumu da taşımak isterse `/model`
ile `opencode-go/deepseek-v4.1-flash` (variant `max`) seçmeli.

## Sınırlar (dürüst notlar)

- **Oturum modeli değiştirilemez.** OpenCode bir skill içinden kendi modelini
  değiştiremez; devir ancak subagent, `codex exec` veya kullanıcının `/model`
  seçimiyle olur.
- **Codex verisi canlı.** `codex app-server` üzerinden `account/rateLimits/read`
  RPC'si ~1 saniyede anlık değeri verir (`--cached` ile dosyadan okunur). Ayrıca
  varsa "reset kredisi" sayısını da döndürür; limit dolduğunda devretmek yerine
  kredi harcamak bir seçenek olabilir.
- **Antigravity'de tükenmiş model** `remainingFraction` alanını hiç göndermez;
  script bunu %0 sayar (panel de öyle yapıyor).
- **Antigravity verisi canlı.** Language server açık olmalı; kapalıysa script hata verir.
- **Codex kotası harcanır.** `codex exec` gerçek bir istek atar; sadece kontrol
  için çağırma, `codex-quota.py` yeterli.
- Eşikler kullanıcı tercihidir; değiştirmek için `model-fallback.py` başındaki
  `WEEKLY_LOW` / `FIVE_HOUR_LOW` sabitlerini düzenle.
