# fxb_viewer

Knight Online FX görüntüleme ve düzenleme aracı (Python GUI).

Kaynak dosya: `fxb_viewer.pyw` — konsol penceresi açmadan çalışan tek dosyalık uygulama.

## Ne işe yarar?

- FX klasöründeki `.fxb`, `.n3fxpart`, `.n3fxbundle` dosyalarını listeler
- İçlerindeki texture referanslarını bulur; `.dxt` / `.tga` dokularını açıp önizler
- Knight Online’ın özel `.dxt` konteynerini (DXT1/DXT3/DXT5) kendi içinde çözer
- Şeffaflık ve renk (hue) ayarı yapıp dosyaya kaydeder (ilk kayıtta `.orijinal_yedek` bırakır)
- Basit prosedürel efekt dokusu tasarlayıp `.dxt` olarak kaydedebilir
- Deneysel: `.n3shape` / `.n3pmesh` 3D mesh önizleme ve basit mesh üretimi

## Gereksinimler

- Windows
- Python 3.8+ (test edildi: 3.10)
- Paketler: `pillow`, `numpy` (`tkinter` genelde Python ile gelir)

## Kurulum

```powershell
# 1) Repoyu indir
git clone https://github.com/Bymuhh/fxb_viewer.git
cd fxb_viewer

# 2) Bağımlılıkları kur
pip install -r requirements.txt

# 3) Çalıştır (konsolsuz pencere)
pythonw fxb_viewer.pyw
```

Alternatif: `fxb_viewer.pyw` dosyasına çift tıkla (`.pyw` → `pythonw.exe`).

Pillow yoksa:

```powershell
pip install pillow numpy
```

## Kullanım

1. **Klasör Seç (fx)** ile oyun FX klasörünü seç
2. Soldaki listeden bir efekt / doku seç → sağda önizleme
3. İstersen şeffaflık / hue ayarla → **Kaydet**
4. İsteğe bağlı: **Yeni Efekt Tasarla**, **Efekt Galerisi**, **3D Model Tasarla**

> Dosyaya yazma işlemleri orijinali değiştirir. İlk yazmada `.orijinal_yedek` kopyası oluşturulur; yine de FX klasörünün yedeğini alman önerilir.

## İsteğe bağlı: .exe yapmak

```powershell
pip install pyinstaller pillow numpy
pyinstaller --onefile --windowed fxb_viewer.pyw
```

Çıktı: `dist\fxb_viewer.exe`

## Notlar

- Oyun dosyalarını değiştirmek ToS / ban riski taşıyabilir; kendi sorumluluğunda kullan
- 3D mesh yazma özelliği deneyseldir; oyunda her zaman çalışması garanti değildir
- Orijinal araç açıklaması: KayraSouze Fx Editor

## Lisans

Bu depodaki kod, orijinal yazarın koşullarına tabidir. Ticari kullanım / yeniden dağıtım için orijinal kaynağı kontrol et.
