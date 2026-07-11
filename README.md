# Po3_Range_Scanner
# Po3 Monitor v3 — Kullanım (Telegram / Mail alarmlı)

## Hızlı kullanım

    # 1) Dosyanın başındaki CONFIG bölümünü doldur (aşağıda anlatılıyor)

    # 2) Alarm kanalını test et (Telegram'a/maile test mesajı atar)
    python3 po3_monitor_v3.py --test-alert

    # 3) Tek tarama yapıp çıksın (deneme için)
    python3 po3_monitor_v3.py --once

    # 4) Sürekli çalışsın (SSH kapansa bile arkada devam eder)
    nohup python3 po3_monitor_v3.py > po3.log 2>&1 &

    # Logu izle:            tail -f po3.log
    # Durdurmak için:       pkill -f po3_monitor_v3

Varsayılan olarak 15m, 30m, 1h ve 4h'ün DÖRDÜNÜ birden izler,
her 5 dakikada bir tarar, hem long hem short arar.

## Ne zaman alarm atar?

v2'deki Po3 taramasının üstüne RETEST şartı eklendi. Alarm ancak
şu sıra tamamlanınca gelir:

1. Akümülasyon — dar bantta sıkışma (range)
2. Manipülasyon — range dibinin altına sweep (short'ta: tepenin üstüne)
3. Reclaim — en az bir mum range İÇİNE geri kapanmış
4. RETEST — fiyat şu an geri dönüp range dibini (short'ta tepesini)
   yeniden test ediyor ve range içinde kapanıyor  <- ALARM BURADA

Yani alarm geldiğinde fiyat tam giriş bölgesinde olur; expansion
başladıysa (range tepesi kırıldıysa) artık alarm atmaz, geç kalmışsındır.

Aynı sembol/TF/yön için tekrar tekrar alarm atmaz: varsayılan olarak
8 bar boyunca susar (15m'de 2 saat, 4h'te ~1.5 gün). Bu bilgiyi
po3_alert_state.json dosyasında tutar, script yeniden başlasa da hatırlar.

## Telegram kurulumu (önerilen — 2 dakika)

1. Telegram'da @BotFather'a yaz: /newbot -> bota bir isim ver
   -> sana TOKEN verir (123456789:AAH4x... gibi)
2. Oluşan bota Telegram'dan herhangi bir mesaj at ("selam" yeterli)
3. Tarayıcıda şunu aç (TOKEN'ı kendi tokenınla değiştir):
   https://api.telegram.org/botTOKEN/getUpdates
   Çıkan JSON içinde "chat":{"id":987654321 ...} -> bu senin CHAT_ID'in
4. Script dosyasının başındaki CONFIG'e yaz:
   "TELEGRAM_BOT_TOKEN": "123456789:AAH4x...",
   "TELEGRAM_CHAT_ID": "987654321",
5. Test: python3 po3_monitor_v3.py --test-alert

Not: Makinenin api.telegram.org'a çıkışı olmalı. Test:
    curl -s https://api.telegram.org > /dev/null && echo ACIK || echo KAPALI

## Mail kurulumu (SMTP)

CONFIG'de:

    "EMAIL_ENABLED": True,
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": 587,
    "SMTP_USER": "seninmailin@gmail.com",
    "SMTP_PASS": "uygulama-sifresi",
    "MAIL_TO": "alarmlarin-gidecegi@mail.com",

Gmail kullanıyorsan normal şifren ÇALIŞMAZ: Google Hesabı ->
Güvenlik -> 2 Adımlı Doğrulama açık olmalı -> "Uygulama Şifreleri"nden
16 haneli şifre üret, SMTP_PASS'e onu yaz. Kurumsal mail kullanacaksan
kendi SMTP host/port bilgilerinizi gir (ör. port 587 + STARTTLS).

## WhatsApp?

Dürüst cevap: pratik değil. WhatsApp'ın botlar için resmi yolu
Meta WhatsApp Business API (veya Twilio gibi aracılar) — başvuru,
onaylı numara ve çoğu durumda ücret gerektirir. Gayriresmi
kütüphaneler ise hesabını banlatma riski taşır. Telegram botu
2 dakikada kuruluyor ve tamamen ücretsiz; onu öneririm.
İlla WhatsApp istersen Twilio hesabı açıp API bilgilerini
getirirsen scripti ona uyarlarız.

## Çıktı / alarm örneği

    Po3 RETEST SOLUSDT 1h [LONG]
    Symbol : SOLUSDT
    TF     : 1h
    Side   : LONG
    Range  : 142.10 - 151.80
    Sweep  : 139.95
    Close  : 143.20
    Setup  : retest of range low 142.10
    Target (-0.272): 154.44

## Parametreler

    --tfs            İzlenecek TF'ler (boşlukla ayır)            (varsayılan: 15m 30m 1h 4h)
                     Ör: --tfs 1h 4h  -> sadece 1h ve 4h izler
    --interval       Tarama döngüleri arası saniye               (varsayılan 300)
    --top / yok      Monitor modunda top yok; şartı sağlayan HERKESE alarm atar
    --min-vol        Min 24s hacim (USDT)                        (varsayılan 20000000)
    --max-symbols    En fazla kaç sembol                         (varsayılan 120)
    --acc-len        Akümülasyon penceresi (mum)                 (varsayılan 40)
    --manip-len      Sweep son kaç mumda aransın                 (varsayılan 20)
    --width-mult     Range genişlik toleransı                    (varsayılan 20)
    --drift          Range eğim toleransı 0-1                    (varsayılan 0.70)
    --retest-tol     Retest bölgesi genişliği (range'in oranı)   (varsayılan 0.15)
                     0.15 = range dibinin %15 üstüne kadar olan bölgeye
                     dönüş "retest" sayılır. Büyütürsen daha erken/gevşek alarm.
    --cooldown-bars  Aynı sembol/TF/yön için kaç bar sussun      (varsayılan 8)
    --once           Tek tarama yap ve çık
    --test-alert     Kanallara test mesajı at ve çık

## Notlar

- pip gerekmez, Python 3.6+ yeterli.
- 4 TF x 120 sembol bir tam tur ~4-5 dk sürer; --interval 300 ile
  pratikte kesintisiz döner. Yükü azaltmak için --tfs veya
  --max-symbols'u kıs.
- QRadar test makinesinde çalıştırıyorsan: script hafiftir ama
  üretim appliance'ında değil test makinesinde tutman doğru olur.
- Bu bir filtre/alarm aracıdır, sinyal servisi değil: alarm gelince
  grafiği açıp yapıyı kendi gözünle teyit et.
