# Model routing & fallback — taşınabilir kurulum

Bu doküman, "DeepSeek ile kod yazdırma + kota dolunca IDE/model devri" kurulumunu
**sıfırdan başka bir makinede** (ör. iş yerindeki bilgisayarda) yeniden kurmak için
gereken her şeyi anlatır. Bir AI ajanına bu dosyayı verip "uygula" demen yeterli.

---

## 1. Ne kuruyoruz?

| Bileşen | Ne yapar |
|---|---|
| `scripts/model-fallback.py` | Üç sağlayıcının kotasını okur, eşik aşılınca devir kararı verir |
| `scripts/antigravity-quota.py` | Antigravity language server'ından canlı model kotası |
| `scripts/codex-quota.py` | Codex app-server RPC'sinden canlı 5s/haftalık kota |
| `scripts/opencode-quota.py` | OpenCode Go kotası (`GET /zen/go/v1/usage`: rolling/haftalık/aylık) |
| `scripts/handoff.py` | `.auto-orchestrator/handoffs/` altına makine okunur devir kaydı |
| `scripts/codex-opencode.sh` | Codex'i OpenCode Go (deepseek) provider'ıyla çalıştırır |
| `scripts/codex-handoff.sh` | Codex devrini başlatır + sohbeti projeye bağlar |
| `scripts/codex-link-project.py` | Codex CLI sohbetini uygulamada projeye bağlar |
| `scripts/deepseek-ask.sh` | Tek soruluk metin cevabı (curl) |
| `scripts/deepseek-code.py` | Dosya + talimat → değişmiş dosya (diff, yedek, sözdizimi kontrolü) |
| `skills/model-fallback/` | Politika + prosedür (eşikler, karar tablosu, devir) |
| `skills/deepseek-coder/` | Toplu/paralel DeepSeek worker aracı (Evren → OpenCode fallback) |

**Temel kural:** kod yazdırmak için deepseek'i **ajan** olarak çalıştır
(`opencode run`); curl sadece tek soruluk metin içindir.

---

## 2. Dosyalar nereye gider?

Tüm yollar macOS içindir.

| Kaynak (bu repo) | Hedef |
|---|---|
| `skills/model-fallback/` | `~/.config/opencode/skills/model-fallback/` |
| `skills/deepseek-coder/` | `~/.gemini/config/skills/deepseek-coder/` |
| `scripts/*` (bu dokümandaki 9 script) | `~/.config/opencode/scripts/` |
| — (symlink) | `~/.agents/skills/model-fallback` → OpenCode skill dizini |

Kurallar (her IDE ayrı okur):

| IDE | Dosya |
|---|---|
| OpenCode | `~/.config/opencode/AGENTS.md` |
| Codex | `~/.codex/AGENTS.md` |
| Antigravity | `~/.agents/rules/model-routing.md` + `~/.gemini/GEMINI.md` |
| Ortak skill görünürlüğü | `~/.agents/skills/` (symlink'ler) |

---

## 3. Kurulum adımları

### 3.1 Anahtar

`OPENCODE_API_KEY` gerekir (OpenCode Go / zen).

```sh
# Proje .env dosyasına koy (repo'ya commit etme!)
echo 'OPENCODE_API_KEY=<anahtar>' >> ~/Desktop/<proje>/.env
```

Scriptler anahtarı şu sırayla arar:
`$OPENCODE_ENV_FILE` → `~/Desktop/english-work/.env` → `~/Desktop/trading-system/tlab/.env` → `~/.local/share/opencode/auth.json`

> Not: `auth.json` içindeki kayıt **eski** olabilir; `GoUsageLimitError` alırsan
> güncel anahtarı `.env` dosyasına koy.

### 3.2 Scriptler

```sh
mkdir -p ~/.config/opencode/scripts
cp scripts/*.py scripts/*.sh ~/.config/opencode/scripts/
chmod +x ~/.config/opencode/scripts/*
```

### 3.3 Skill'ler

```sh
mkdir -p ~/.config/opencode/skills ~/.gemini/config/skills ~/.agents/skills
cp -R skills/model-fallback ~/.config/opencode/skills/
cp -R skills/deepseek-coder ~/.gemini/config/skills/
ln -sfn ~/.config/opencode/skills/model-fallback ~/.agents/skills/model-fallback
```

### 3.4 Codex provider

`~/.codex/config.toml` içine:

```toml
[model_providers.opencode-go]
name = "OpenCode Go"
base_url = "https://opencode.ai/zen/go/v1"
env_key = "OPENCODE_API_KEY"
wire_api = "responses"          # Codex 0.164 "chat" desteklemiyor
http_headers = { "x-opencode-session" = "codex-handoff" }
```

### 3.5 Kurallar (her IDE'ye kısa blok)

- **OpenCode:** `~/.config/opencode/AGENTS.md` → model-fallback + orkestrasyon blokları
- **Codex:** `~/.codex/AGENTS.md` → kota tetiği + "curl değil, opencode run" kuralı
- **Antigravity:** `~/.agents/rules/model-routing.md` (tam metin) + `~/.gemini/GEMINI.md` (kısa özet)

Bu dosyaların içeriği `skills/model-fallback/SKILL.md` ile aynı politika olmalı;
tek kaynak skill'dir, diğerleri kısa işaretçidir.

### 3.6 Antigravity notu

Antigravity model listesi kapalı bir Gemini ailesidir; özel/OpenAI-uyumlu sağlayıcı
**tanımlanamaz**. Orada deepseek yalnızca araç olarak kullanılır:

```sh
opencode run --model opencode-go/deepseek-v4.1-flash#max --auto -f <brief> "<talimat>"
```

---

## 4. Doğrulama (kurulumdan sonra şart)

```sh
# 1) Kota okuma (üç sağlayıcı: Antigravity + Codex + OpenCode)
~/.config/opencode/scripts/model-fallback.py --status
~/.config/opencode/scripts/opencode-quota.py

# 2) DeepSeek ucu
~/.config/opencode/scripts/deepseek-ask.sh "Sadece OK yaz."

# 3) Codex + deepseek uçtan uca
~/.config/opencode/scripts/codex-opencode.sh exec -C /tmp -s read-only \
  --skip-git-repo-check "Sadece OK yaz."

# 4) OpenCode ajanı (dosya değiştirebiliyor mu)
mkdir -p /tmp/oc-test && cd /tmp/oc-test
printf 'def f(x):\n    return x -\n' > t.py
opencode run --standalone --model opencode-go/deepseek-v4.1-flash#max --auto \
  "t.py dosyasindaki sozdizimi hatasini duzelt."
cat t.py
```

Beklenen: 1) üç sağlayıcı tablosu, 2) `OK`, 3) `OK`, 4) düzeltilmiş dosya.

---

## 5. Günlük kullanım

| Durum | Komut |
|---|---|
| Kod yazdırma (her IDE) | `opencode run --model opencode-go/deepseek-v4.1-flash#max --auto -f brief.md "..."` |
| Kota kontrolü | `model-fallback.py --from <antigravity\|codex\|opencode> --project <proje>` |
| Codex'e devir | `HANDOFF_MODEL=opencode codex-handoff.sh <proje> <brief> "<başlık>"` |
| Tek dosya düzeltme | `deepseek-code.py <dosya> -i "talimat" [--apply]` |
| Tek soru | `deepseek-ask.sh "soru"` |

Eşikler: **5 saatlik ≤ %10** (ana tetik) veya **haftalık ≤ %5** (emniyet) → devret.
**Reset kredileri asla otomatik harcanmaz.**

---

## 6. AI ajana verilecek talimat (kopyala-yapıştır)

```
Bu repodaki docs/MODEL-ROUTING-SETUP.md dosyasını oku ve sırayla uygula.
Kurallar:
- Anahtarı asla repoya commit etme; .env veya ortam değişkeni kullan.
- Adımları atlama; her adımdan sonra "Doğrulama" bölümündeki ilgili komutu çalıştır.
- Bir komut hata verirse dur, hatayı raporla, tahminle devam etme.
- Antigravity'ye özel sağlayıcı eklemeye çalışma; orada sadece `opencode run` kullanılır.
- Bitince: hangi dosyaları oluşturduğunu, hangi doğrulamaların geçtiğini ve
  geçmeyenleri tek tek yaz.
```

---

## 7. Bilinen sınırlar

- Codex `deepseek-v4.1-flash` metaverisini tanımaz ("fallback metadata" uyarısı).
  Uzun oturumlarda sorun çıkarsa `model_context_window` / `model_catalog_json` ile
  metaveri ver — gerçek çağrıyla test etmeden "düzeldi" deme.
- Antigravity'de deepseek **model** olarak kullanılamaz.
- `opencode run` ön planda çalışır; uzun işleri arka planda başlat.
- Aynı thread üzerinde iki Codex süreci çalıştırma: `thread-store conflict` alırsın.
  Bayat `~/.codex/thread-writer-locks/*.lock` dosyalarını temizle.
