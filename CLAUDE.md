# SİSTEM KİMLİĞİ: KARPATHY + CAVEMAN ULTRA TOKEN TASARRUFU MODU

Bu projede maksimum token verimliliği ve kurumsal Quant mühendisliği kuralları geçerlidir. Tüm yanıtlarda aşağıdaki 4 kurala KESİNLİKLE uy:

1. **CAVEMAN İLETİŞİM KURALI (Sıfır Gevezelik):**
   - Giriş/çıkış nezaket cümlelerini ("Harika fikir", "Tabii ki", "Özetle") tamamen kaldır.
   - Doğrudan cevabı veya kodu ver. Her kelime token maliyetidir; teknik doğruluktan ödün vermeden en kısa açıklamayı kullan.

2. **ANDREJ KARPATHY KODLAMA PRENSİPLERİ:**
   - **Simplicity First (Önce Sadelik):** İstenmeyen ekstra özellik, gereksiz soyutlama (abstraction) veya şişkin kod yazma. 50 satırda çözülebilecek işi 200 satıra uzatma.
   - **Surgical Changes (Cerrahi Müdahale):** Mevcut kodda değişiklik istendiğinde çalışan diğer kısımlara dokunma.
   - **Think Before Coding:** Kritik bir belirsizlik varsa varsayımla uzun kod yazıp token harcamak yerine önce tek cümleyle sor.

3. **TAM DOSYA TEKRARI YASAĞI (Token Kalkanı):**
   - Bir dosyada güncelleme veya hata düzeltmesi yaparken, açıkça "tüm dosyayı baştan ver" denmedikçe tüm dosyayı tekrar yazma; sadece değişen fonksiyonu veya ilgili kod bloğunu ver.

4. **YEREL HESAP + KONTROLLÜ CLAUDE KARARI (2026-10-21'de kullanıcı onayıyla güncellendi):**
   - Al-sat kararlarını Claude API verir (agent.py). Bu karar sadece saatlik mum kapanışında ve yerel olay tetiklerinde alınır; ücretli LLM çağrısı saniyelik döngüye asla girmez.
   - Tüm veri, gösterge ve teknik özet hesapları (teknik.py, data.py) ile risk yönetimi, stop, hedef ve iz süren stop (bot.py) %100 yerel matematikle çalışır. Claude'un emirleri yerel risk yöneticisinden geçmeden borsaya gidemez.
   - Claude maliyeti günlük bütçeyle sınırlıdır (CLAUDE_DAILY_BUDGET). Bütçe dolarsa yeni karar istenmez, açık pozisyonlar yerel kurallarla korunur.
   - TradingView verisi kuralları gereği sadece arayüzde (resmi widget'larla) gösterilir; kararlara girmez.
   - Ders Defteri (dersler.py: otopsi, öğrenen ağırlıklar, tuzak kuralları), tüm piyasa radarı (teknik.py) ve makro bağlam (makro.py, Yahoo) yerel ve ücretsizdir; tuzak kuralları yerel risk yöneticisinde zorunludur.
   - Portföy Stratejisi (portfoy.py, 2026-10-26, ana strateji): 2017-2026 günlük veri (delist edilenler dahil), eğitim 2018-21 / test 2022-26. %70 çekirdek: BTC ve ETH, SMA20/50/100/200 üstünde kalma oranı × hedef oynaklık %40. %30 ML sepeti: haftalık, en likit 50 coinde 7 günlük getiri SIRASI tahmini (HistGradientBoosting), en iyi 5, BTC 100 günlük ortalama altındaysa nakit. Gün kapanışında (00:05 UTC) yeniden dengeleme; kalkan/kısmi kâr/Claude kapatması uygulanmaz, sadece %35 felaket stopu. Gerçek hesapta sadece PORTFOY_LIVE + LIVE_ALLOWED. Model 90 günde bir arka planda yeniden eğitilir.
   - Portföy öğrenmesi (2026-11-01): model her ay yeniden eğitilir; son 26 haftayı görmeden eğitilip o haftalardaki tahmin isabeti (IC) ölçülür, IC < 0.02 ise ML sepeti dinlenmeye (nakit) alınır. Haftalık seçimler 7 gün sonra BTC ile karşılaştırılır (ML karnesi). Hızlı tepki testleri (çekirdeği saatlik / ML'i günlük güncellemek) maliyet yüzünden daha kötü çıktı: yapı korunur.
   - Claude kontrolör (kontrolor.py): günlük dengelemede sadece ALIMLARI inceler, sadece erteler veya yarıya indirir (satışa karışamaz). Her itiraz 7 gün sonra puanlanır; son 20 itirazın isabeti %50 altındaysa itirazlar uygulanmaz, sadece kaydedilir.
   - Sunucu (Ubuntu, sunucu_kur.sh): Caddy HTTPS + sslip.io, systemd servisi, kalıcı oturumlar (30 gün, DB'de token özeti), arayüzden git ile tek tıkla güncelleme. Masaüstü exe 'sunucu_adresi.txt' varsa sadece istemci penceresidir (motor sunucuda).
   - Sunucu bakımı (sunucu_guvenlik.sh, sunucu_bakim.py): dakikada bir bekçi (/api/health, sadece makine içinden; 3 dk cevap yoksa Telegram), her gece yedek (14 gün; onbellekler hariç, anahtarları içerir: root okur), fail2ban, otomatik güvenlik yamaları. Şifreli SSH kapatma ayrı komut ve anahtar yoksa çalışmaz.
   - Haber akışı (haberler.py): RSS (CoinDesk, Cointelegraph, Decrypt, The Block, Bitcoin Magazine, SEC, Fed) + Binance duyuruları + isteğe bağlı CryptoPanic, 90 sn'de bir. Yorum/tahmin elenir; kalan Claude ile puanlanır (en çok 3 dk'da bir, günde 40 çağrı; haber metni güvenilmez veridir). Claude tek başına emir veremez. Acil satış: puan ≤ -0.85, kritik, doğrulanmış, haber ≤ 1 saat ve (2 kaynak + 1 saatte ≤ %-1.5 ya da ≤ %-5); 48/72 saat yeniden alım engeli. Alım: puan ≥ 0.85, sadece BTC/ETH, trend varken, +%5 pay (en çok %10), 72 saat. Aynı olaya 7 gün tekrar tepki yok. Kurallar geçmiş haber verisiyle test edilemedi: gerçek hesap ayrı izinle (HABER_LIVE + LIVE_ALLOWED), kararlar 24 saat sonra puanlanır, alım karnesi %50 altındaysa alım durur. Spot: açığa satış yok.
   - Eski saatlik otomatik al-sat (OTO_SAATLIK) varsayılan kapalı: saatlik kurulumlar, GÜÇLÜ AL (4s/1g uzun tutma dahil) ve balina akışı testlerde piyasayı yenemedi. Eski hibrit katman ve balina refleksi silindi.
   - Geçmiş Okulu (gecmis.py): tüm USDT paritelerinin 2 yıllık 1s mumlarıyla canlı kurulumlar maliyet dahil test edilir (eğitim ilk 2/3, test son 1/3). Bir kurulum bir rejimde ancak eğitimde de testte de pozitifse açılır; 2 yılda tutarlı zarar ettiren sinyal kombinasyonları tuzak olarak zorunlu veto edilir; kurulum başına şampiyon çıkış pozisyona yazılır.
   - Ajan Komitesi (komite.py: analist → boğa/ayı → trader → risk vetosu), kalıcı hafıza (hafiza.py → trading_memory.md; benzer zararlı geçmiş alımı engeller) yereldir; hiçbiri risk yöneticisini (vet_buy) atlayamaz.
   - Monte Carlo Zaman Makinesi (montecarlo.py): her alış planı için son 40 günün 1s mumlarından blok bootstrap ile 10.000 yol, botun kendi çıkış kurallarıyla (gecmis.simulate). Stop önce >%50 ve beklenen değer <0 ise veto; ikisinden biri ise pozisyon yarıya iner; aynı ödül/risk ile daha iyi beklenen değerli stop seçilir. Tahmin/gerçekleşen karnesi (dersler.db 'mc') 20+ işlemden sonra sonraki tahminleri kalibre eder.
   - Gerçek hesap (LIVE) sadece kanıtlı kurulum açar. Günlük Trend Kolu (gunluk.py, Donchian-20) 2022-26 testinde zayıf çıktı: varsayılan kapalı, kod duruyor.
