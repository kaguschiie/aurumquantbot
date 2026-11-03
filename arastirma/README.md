# Strateji araştırması

`portfoy.py` içindeki `BACKTEST` tablosunun ve strateji kararlarının kodu. Bot bu klasörü kullanmaz. Eğitim 2018-21, test 2022-26; veriye delist edilen coinler dahil; tek yön maliyet %0,15 (komisyon + kayma).

## Yeniden üretme

```bash
cd arastirma
pip install numpy pandas scikit-learn requests
python indir_1d.py
python canli_kopya.py
```

`indir_1d.py` yaklaşık 52 MB günlük veriyi `d1/` klasörüne indirir (depoya girmez). `canli_kopya.py` canlı yapıyı (30 günde bir eğitim, canlı bantlar) kopyalar. 2026-10 sonuna kadarki veriyle test: yıllık %22,9, en derin düşüş -%23,8, Sharpe 0,92. Sepet günü seçilirken test görüldü; 7 günün ortalaması %17,7. Eski %25,2 (`limit_test.py`, `ml.py`) yıllık eğitim ve Pazartesi mumu kullanır, canlıyla aynı değildir. Veri uzadıkça sayılar değişir.

Bu sayılar geçmiş testtir, gerçek para sonucu değildir. Test canlı sistemin korumalarını (kill switch, molalar, %35 stop, IC kapısı, Claude kontrolör) simüle etmez ve denenen varyant sayısına göre düzeltilmemiştir.

## Dosyalar

| Dosya | Ne yapar |
|---|---|
| `indir_1d.py` | Günlük mumları indirir |
| `panel.py` | Veri yükleme, backtest motoru, istatistikler |
| `arastir.py` | Basit kural taraması (`sonuc.csv`) |
| `saglam.py` | Seçilen kuralların sağlamlık testi |
| `ml.py`, `ml_denetim.py` | ML sepeti ve sızıntı denetimi (`ml_sonuc.json`) |
| `karisim.py` | Çekirdek ve ML karışımı (70/30 buradan) |
| `koruma.py` | Düşüşte zararı azaltma fikirleri |
| `hiz.py`, `hiz_saat.py` | Daha sık işlem varyantları |
| `guclu_uzun.py` | GÜÇLÜ AL sinyalini uzun tutma testi |
| `canli_kopya.py` | Canlı yapının kopyası, 7 gün karşılaştırması, rastgele sepet sıfır testi (`BACKTEST` buradan) |
| `limit_test.py` | Dengelemede 24 saatlik zarar limiti |
| `hack_replay.py` | Haber kurallarının 29 Ekim Binance hack'inde geri oynatılması (çıkan haberleri ve Binance'i canlı okur) |

## Veriye bağlı kısıtlar

- `hiz_saat.py` uygulamanın 1 saatlik mum önbelleğini okur: `KQB_SAATLIK_VERI` ile klasörü göster.
- `guclu_uzun.py` içindeki 4 saatlik test için `veri4h/` verisi depoda yok; indirme kodu da burada değil.
- `hack_replay.py` canlı RSS okuduğu için aynı başlıkları sonsuza kadar bulamayabilir.
- `sonuclar/` ilk çalıştırmaların çıktılarıdır (`ml`, `ml_denetim`, `hiz`, `limit_test`). `arastir` ve `guclu_uzun` günlükleri yerel yollar içerdiği için eklenmedi; özetleri `sonuc.csv` ve `guclu_uzun.json` içinde.
