#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fxb_viewer.pyw - KayraSouze Fx Editor (Knight Online FX gorsel onizleme/duzenleme programi, GUI)
==================================================================================================

Cift tiklayinca (.pyw -> pythonw.exe ile) konsol penceresi ACILMADAN
dogrudan bir uygulama penceresi acilir.

Ne yapar:
  - "Klasor Sec" ile fx klasorunu (C:\\...\\Desktop\\fx) gosterirsin.
  - Soldaki listede .fxb / .n3fxpart / .n3fxbundle dosyalarini gorursun,
    ustteki kutuya yazarak filtreleyebilirsin.
  - Bir dosyaya tikladiginda: icindeki texture/doku referanslarini bulur,
    bu klasordeki gercek .dxt / .tga doku dosyalarini bulup coker
    (Knight Online'in ozel .dxt konteyner formatini bu program kendi
    icinde cozuyor - harici bir programa ihtiyac yok) ve sag tarafta
    GERCEK gorseli (varsa frame'leri sirayla oynatarak) gosterir.

Gereksinimler (bilgisayarinda Python kuruluysa genelde hazir gelir):
  pip install pillow numpy

Gercek .exe yapmak istersen (opsiyonel):
  pip install pyinstaller pillow numpy
  pyinstaller --onefile --windowed fxb_viewer.pyw
  -> dist\\fxb_viewer.exe olusur, onu istedigin yere kopyalayip
     cift tiklayarak calistirabilirsin (Python kurulu olmasa bile).
"""

import os
import sys
import struct
import glob
import re
import math
import colorsys
import traceback
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

try:
    import numpy as np
except ImportError:
    print("HATA: numpy kurulu degil. Once şunu çalıştır: pip install numpy")
    sys.exit(1)

try:
    from PIL import Image, ImageTk, ImageDraw
except ImportError:
    print("HATA: Pillow kurulu degil. Once şunu çalıştır: pip install pillow")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Knight Online .dxt konteyner cozucu (BC1/DXT1, BC2/DXT3, BC3/DXT5)
# ---------------------------------------------------------------------------

def _unpack565(v):
    r = (v >> 11) & 0x1F
    g = (v >> 5) & 0x3F
    b = v & 0x1F
    r = (r << 3) | (r >> 2)
    g = (g << 2) | (g >> 4)
    b = (b << 3) | (b >> 2)
    return r, g, b


def _decode_bc1_block(data, has_alpha_mode=True):
    c0, c1 = struct.unpack_from('<HH', data, 0)
    idx = struct.unpack_from('<I', data, 4)[0]
    r0, g0, b0 = _unpack565(c0)
    r1, g1, b1 = _unpack565(c1)
    colors = np.zeros((4, 4), dtype=np.uint8)
    colors[0] = (r0, g0, b0, 255)
    colors[1] = (r1, g1, b1, 255)
    if c0 > c1 or not has_alpha_mode:
        colors[2] = ((2*r0+r1)//3, (2*g0+g1)//3, (2*b0+b1)//3, 255)
        colors[3] = ((r0+2*r1)//3, (g0+2*g1)//3, (b0+2*b1)//3, 255)
    else:
        colors[2] = ((r0+r1)//2, (g0+g1)//2, (b0+b1)//2, 255)
        colors[3] = (0, 0, 0, 0)
    out = np.zeros((4, 4, 4), dtype=np.uint8)
    for py in range(4):
        row = (idx >> (py * 8)) & 0xFF
        for px in range(4):
            sel = (row >> (px * 2)) & 0x3
            out[py, px] = colors[sel]
    return out


def _decode_bc2_alpha(data):
    out = np.zeros((4, 4), dtype=np.uint8)
    for py in range(4):
        word = struct.unpack_from('<H', data, py * 2)[0]
        for px in range(4):
            nib = (word >> (px * 4)) & 0xF
            out[py, px] = nib * 17
    return out


def _decode_bc3_alpha(data):
    a0, a1 = data[0], data[1]
    bits = int.from_bytes(data[2:8], 'little')
    alphas = [0] * 8
    alphas[0], alphas[1] = a0, a1
    if a0 > a1:
        for i in range(1, 7):
            alphas[1 + i] = ((7 - i) * a0 + i * a1) // 7
    else:
        for i in range(1, 5):
            alphas[1 + i] = ((5 - i) * a0 + i * a1) // 5
        alphas[6] = 0
        alphas[7] = 255
    out = np.zeros((4, 4), dtype=np.uint8)
    for p in range(16):
        sel = (bits >> (p * 3)) & 0x7
        py, px = divmod(p, 4)
        out[py, px] = alphas[sel]
    return out


def _decompress(pix, width, height, fourcc):
    bw, bh = (width + 3) // 4, (height + 3) // 4
    img = np.zeros((bh * 4, bw * 4, 4), dtype=np.uint8)
    off = 0
    if fourcc == b'DXT1':
        bs = 8
        for by in range(bh):
            for bx in range(bw):
                block = pix[off:off+bs]; off += bs
                img[by*4:by*4+4, bx*4:bx*4+4] = _decode_bc1_block(block, True)
    elif fourcc in (b'DXT3', b'DXT5'):
        bs = 16
        for by in range(bh):
            for bx in range(bw):
                block = pix[off:off+bs]; off += bs
                if fourcc == b'DXT3':
                    alpha = _decode_bc2_alpha(block[0:8])
                else:
                    alpha = _decode_bc3_alpha(block[0:8])
                rgb = _decode_bc1_block(block[8:16], False)
                rgb[:, :, 3] = alpha
                img[by*4:by*4+4, bx*4:bx*4+4] = rgb
    else:
        raise ValueError(f"Desteklenmeyen fourCC: {fourcc}")
    return img[:height, :width]


# D3D9 D3DFORMAT enum degerleri -- bazi eski/basit dokular DXT sikistirmasi
# kullanmiyor, ham (sikistirilmamis) piksel dizisi olarak saklaniyor ve
# "fourCC" alaninda ASCII yerine bu sayisal format kodu yer alabiliyor.
_D3DFMT_R8G8B8 = 20
_D3DFMT_A8R8G8B8 = 21
_D3DFMT_X8R8G8B8 = 22
_D3DFMT_R5G6B5 = 23
_D3DFMT_X1R5G5B5 = 24
_D3DFMT_A1R5G5B5 = 25
_D3DFMT_A4R4G4B4 = 26


def _decode_raw_format(pix, width, height, fmt_code):
    n = width * height
    if fmt_code in (_D3DFMT_A8R8G8B8, _D3DFMT_X8R8G8B8):
        raw = np.frombuffer(pix[:n * 4], dtype=np.uint8).reshape(height, width, 4)
        out = np.zeros((height, width, 4), dtype=np.uint8)
        out[..., 0] = raw[..., 2]  # R
        out[..., 1] = raw[..., 1]  # G
        out[..., 2] = raw[..., 0]  # B
        out[..., 3] = 255 if fmt_code == _D3DFMT_X8R8G8B8 else raw[..., 3]
        return out
    elif fmt_code == _D3DFMT_R8G8B8:
        raw = np.frombuffer(pix[:n * 3], dtype=np.uint8).reshape(height, width, 3)
        out = np.zeros((height, width, 4), dtype=np.uint8)
        out[..., 0] = raw[..., 2]
        out[..., 1] = raw[..., 1]
        out[..., 2] = raw[..., 0]
        out[..., 3] = 255
        return out
    elif fmt_code in (_D3DFMT_R5G6B5, _D3DFMT_X1R5G5B5, _D3DFMT_A1R5G5B5, _D3DFMT_A4R4G4B4):
        raw = np.frombuffer(pix[:n * 2], dtype='<u2').reshape(height, width).astype(np.uint32)
        out = np.zeros((height, width, 4), dtype=np.uint8)
        if fmt_code == _D3DFMT_R5G6B5:
            r5 = (raw >> 11) & 0x1F
            g6 = (raw >> 5) & 0x3F
            b5 = raw & 0x1F
            out[..., 0] = ((r5 << 3) | (r5 >> 2)).astype(np.uint8)
            out[..., 1] = ((g6 << 2) | (g6 >> 4)).astype(np.uint8)
            out[..., 2] = ((b5 << 3) | (b5 >> 2)).astype(np.uint8)
            out[..., 3] = 255
        elif fmt_code in (_D3DFMT_X1R5G5B5, _D3DFMT_A1R5G5B5):
            a1 = (raw >> 15) & 0x1
            r5 = (raw >> 10) & 0x1F
            g5 = (raw >> 5) & 0x1F
            b5 = raw & 0x1F
            out[..., 0] = ((r5 << 3) | (r5 >> 2)).astype(np.uint8)
            out[..., 1] = ((g5 << 3) | (g5 >> 2)).astype(np.uint8)
            out[..., 2] = ((b5 << 3) | (b5 >> 2)).astype(np.uint8)
            if fmt_code == _D3DFMT_X1R5G5B5:
                out[..., 3] = 255
            else:
                out[..., 3] = (a1 * 255).astype(np.uint8)
        else:  # A4R4G4B4
            a4 = (raw >> 12) & 0xF
            r4 = (raw >> 8) & 0xF
            g4 = (raw >> 4) & 0xF
            b4 = raw & 0xF
            out[..., 0] = ((r4 << 4) | r4).astype(np.uint8)
            out[..., 1] = ((g4 << 4) | g4).astype(np.uint8)
            out[..., 2] = ((b4 << 4) | b4).astype(np.uint8)
            out[..., 3] = ((a4 << 4) | a4).astype(np.uint8)
        return out
    else:
        raise ValueError(f"Desteklenmeyen ham piksel format kodu: {fmt_code}")


def load_ko_dxt(path):
    """.dxt dosyasini okuyup PIL Image (RGBA) dondurur."""
    data = open(path, 'rb').read()
    namelen = struct.unpack_from('<i', data, 0)[0]
    off = 4
    off += namelen
    magic = data[off:off+3]; off += 3
    if magic != b'NTF':
        raise ValueError(f"Beklenmeyen imza: {magic!r}")
    flags = data[off]; off += 1
    if flags & 0x04:
        raise NotImplementedError("Bu doku sifreli, bu program sifre cozemiyor.")
    width, height = struct.unpack_from('<ii', data, off); off += 8
    fourcc_raw = data[off:off+4]
    off += 8
    pix = data[off:]
    if fourcc_raw in (b'DXT1', b'DXT3', b'DXT5'):
        arr = _decompress(pix, width, height, fourcc_raw)
    else:
        fmt_code = struct.unpack('<i', fourcc_raw)[0]
        arr = _decode_raw_format(pix, width, height, fmt_code)
    return Image.fromarray(arr, 'RGBA')


def load_texture_any(path):
    """.dxt ya da .tga -> PIL Image (RGBA)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.dxt':
        return load_ko_dxt(path)
    else:
        return Image.open(path).convert('RGBA')


# ---------------------------------------------------------------------------
# RGBA -> DXT3 ENCODER (yeni/sifirdan uretilen gorseller icin).
# Basit ama dogru bir "bounding box" yontemi kullanir: her 4x4 blokta en
# parlak ve en sonuk iki rengi uc (endpoint) olarak secer, aradaki 2 rengi
# enterpolasyonla turetir, her pikseli en yakin renge atar. Kalite bir
# profesyonel sikistiriciyla ayni degil ama uretilen efekt gorselleri icin
# (fotograf degil, gradyan/parlama deseni) gorsel olarak yeterince iyi.
# ---------------------------------------------------------------------------

def _encode_bc1_block(block_rgb):
    """block_rgb: (4,4,3) uint8 -> 8 bayt (c0,c1,indices)."""
    pixels = block_rgb.reshape(16, 3).astype(np.float64)
    lum = pixels @ np.array([0.299, 0.587, 0.114])
    i_max = int(np.argmax(lum))
    i_min = int(np.argmin(lum))
    c0 = pixels[i_max]
    c1 = pixels[i_min]
    packed0 = _pack565(*c0)
    packed1 = _pack565(*c1)
    if packed0 == packed1:
        # duz renkli blok -- ikinci ucu hafifce degistir ki index hesaplamasi bozulmasin
        packed1 = packed0 - 1 if packed0 > 0 else packed0 + 1
    if packed0 <= packed1:
        packed0, packed1 = packed1, packed0
    r0, g0, b0 = _unpack565(packed0)
    r1, g1, b1 = _unpack565(packed1)
    palette = np.array([
        [r0, g0, b0],
        [r1, g1, b1],
        [(2*r0+r1)/3, (2*g0+g1)/3, (2*b0+b1)/3],
        [(r0+2*r1)/3, (g0+2*g1)/3, (b0+2*b1)/3],
    ])
    dists = ((pixels[:, None, :] - palette[None, :, :]) ** 2).sum(axis=2)
    idx = np.argmin(dists, axis=1).reshape(4, 4)
    row_bytes = bytearray(4)
    for py in range(4):
        v = 0
        for px in range(4):
            v |= int(idx[py, px]) << (px * 2)
        row_bytes[py] = v
    return struct.pack('<HH', packed0, packed1) + bytes(row_bytes)


def _encode_bc2_alpha_block(block_alpha):
    """block_alpha: (4,4) uint8 -> 8 bayt (4-bit acik alfa)."""
    out = bytearray(8)
    for py in range(4):
        word = 0
        for px in range(4):
            nib = int(round(block_alpha[py, px] / 17.0))
            nib = max(0, min(15, nib))
            word |= nib << (px * 4)
        struct.pack_into('<H', out, py * 2, word)
    return bytes(out)


def encode_rgba_to_dxt3_pixels(im_rgba):
    """PIL RGBA Image -> DXT3 sikistirilmis piksel byte'lari (genislik/yukseklik
    4'un kati olmali; degilse otomatik olarak kenarlardan tekrarlanarak
    buyutulur)."""
    w, h = im_rgba.size
    pad_w = (-w) % 4
    pad_h = (-h) % 4
    if pad_w or pad_h:
        new_im = Image.new('RGBA', (w + pad_w, h + pad_h))
        new_im.paste(im_rgba, (0, 0))
        if pad_w:
            edge = im_rgba.crop((w - 1, 0, w, h))
            for x in range(w, w + pad_w):
                new_im.paste(edge, (x, 0))
        im_rgba = new_im
        w, h = im_rgba.size
    arr = np.array(im_rgba)
    bw, bh = w // 4, h // 4
    out = bytearray(bw * bh * 16)
    pos = 0
    for by in range(bh):
        for bx in range(bw):
            block = arr[by*4:by*4+4, bx*4:bx*4+4, :]
            alpha_bytes = _encode_bc2_alpha_block(block[:, :, 3])
            color_bytes = _encode_bc1_block(block[:, :, :3])
            out[pos:pos+8] = alpha_bytes
            out[pos+8:pos+16] = color_bytes
            pos += 16
    return bytes(out), w, h


def write_ko_dxt(path, im_rgba, name=""):
    """Yeni bir PIL RGBA goruntusunu Knight Online .dxt konteynerine
    (DXT3, sifresiz) yazar."""
    pix, w, h = encode_rgba_to_dxt3_pixels(im_rgba.convert('RGBA'))
    name_bytes = name.encode('latin-1', errors='replace')
    header = struct.pack('<i', len(name_bytes)) + name_bytes
    header += b'NTF' + bytes([0])  # imza + flags (0 = sifresiz)
    header += struct.pack('<ii', w, h)
    header += b'DXT3' + b'\x00' * 4  # fourCC + rezerve
    with open(path, 'wb') as f:
        f.write(header + pix)


# ---------------------------------------------------------------------------
# Proseduerel (kod ile, matematiksel) efekt gorseli tasarimi.
# Gercek bir AI resim modeli burada YOK -- bu fonksiyonlar radyal gradyan,
# parlama, kivilcim gibi desenleri elle/matematiksel olarak kuruyor.
# ---------------------------------------------------------------------------

def _hsv_to_rgb_arr(h, s, v):
    """h,s,v: (H,W) float [0..1] -> (H,W,3) uint8"""
    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    ii = (i.astype(np.int64)) % 6
    conditions = [ii == k for k in range(6)]
    r = np.select(conditions, [v, q, p, p, t, v])
    g = np.select(conditions, [t, v, v, q, p, p])
    b = np.select(conditions, [p, p, t, v, v, q])
    return np.stack([r, g, b], axis=-1) * 255.0


def generate_effect_texture(style, hue_deg, size=128, variant=0):
    """style: 'burst' | 'sparkle' | 'ring' | 'flame'
    hue_deg: 0-360, variant: farkli rastgele varyasyonlar icin sabit tohum."""
    rng = np.random.RandomState(variant * 1000 + 1)
    n = size
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float64)
    cx, cy = (n - 1) / 2.0, (n - 1) / 2.0
    dx, dy = (xx - cx) / (n / 2.0), (yy - cy) / (n / 2.0)
    r = np.sqrt(dx * dx + dy * dy)
    ang = np.arctan2(dy, dx)
    hue = (hue_deg % 360) / 360.0

    if style == 'burst':
        core = np.clip(1.0 - r * 1.05, 0, 1) ** 2
        rays = 0.5 + 0.5 * np.cos(ang * 10 + rng.uniform(0, 6.28))
        rays = rays ** 3
        glow = np.clip(1.0 - r, 0, 1) ** 1.5
        val = np.clip(core * 1.0 + glow * rays * 0.6, 0, 1)
        sat = np.clip(1.0 - core * 0.6, 0.25, 1.0)
        alpha = np.clip(val * 1.3, 0, 1)
    elif style == 'sparkle':
        val = np.zeros((n, n))
        num_points = 7 + variant % 5
        for _ in range(num_points):
            px, py = rng.uniform(-0.8, 0.8, 2)
            pr = np.sqrt((dx - px) ** 2 + (dy - py) ** 2)
            spark = np.clip(1.0 - pr * rng.uniform(3.0, 6.0), 0, 1) ** 2
            val = np.maximum(val, spark)
        haze = np.clip(1.0 - r * 1.3, 0, 1) ** 2 * 0.35
        val = np.clip(val + haze, 0, 1)
        sat = np.clip(0.5 + val * 0.3, 0, 1)
        alpha = np.clip(val * 1.2, 0, 1)
    elif style == 'ring':
        ring_r = 0.65
        band = np.exp(-((r - ring_r) ** 2) / (2 * 0.05 ** 2))
        wobble = 1.0 + 0.15 * np.sin(ang * 8 + rng.uniform(0, 6.28))
        val = np.clip(band * wobble, 0, 1)
        inner_glow = np.clip(1.0 - r * 1.6, 0, 1) ** 3 * 0.4
        val = np.clip(val + inner_glow, 0, 1)
        sat = np.full((n, n), 0.85)
        alpha = np.clip(val * 1.4, 0, 1)
    elif style == 'flame':
        flick = rng.uniform(0.85, 1.15)
        shaped = np.clip(1.0 - (np.abs(dx) * 1.6) ** 1.5 - np.clip(dy * flick, -1, 1) * 0.5 - r * 0.3, 0, 1)
        noise = 0.5 + 0.5 * np.sin(dx * 9 + dy * 13 + rng.uniform(0, 6.28))
        val = np.clip(shaped * (0.7 + 0.3 * noise), 0, 1)
        sat = np.clip(0.6 + val * 0.3, 0, 1)
        alpha = np.clip(val * 1.2, 0, 1)
    elif style in ('ice', 'buz'):
        # sivri kristal/buz parcasi -- keskin acili isinlar + parlak beyazimsi
        # merkez (buz/kar kristaline benzemesi icin sivri, yuvarlak degil)
        n_shards = 6 + variant % 3
        spikes = np.abs(np.cos(ang * n_shards / 2.0 + rng.uniform(0, 6.28)))
        spikes = spikes ** 6
        core = np.clip(1.0 - r * 1.15, 0, 1) ** 1.5
        falloff = np.clip(1.0 - r * 0.9, 0, 1)
        val = np.clip(core * 0.9 + spikes * falloff * 0.85, 0, 1)
        # merkeze yakin daha az doygun (beyazimsi/parlak), kenarlara dogru daha doygun mavi
        sat = np.clip(0.30 + (1.0 - core) * 0.45, 0.15, 0.8)
        alpha = np.clip(val * 1.3, 0, 1)
    else:
        raise ValueError(f"Bilinmeyen stil: {style}")

    rgb = _hsv_to_rgb_arr(np.full((n, n), hue), sat, val)
    arr = np.zeros((n, n, 4), dtype=np.uint8)
    arr[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    arr[..., 3] = np.clip(alpha * 255, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, 'RGBA')


# ---------------------------------------------------------------------------
# Klasordeki GERCEK efekt dokularindan ilham alarak yeni bir varyasyon
# tasarlama. Prosedurel stillerden farki: burada saf matematik yerine,
# fx klasorunde zaten var olan (sanatci tarafindan cizilmis) bir dokuyu
# baz alip -- renk (hue), seffaflik ve ustune eklenen ekstra parlama/
# kivilcim katmanlariyla - yeni ama o gorsel dile ait bir varyant uretiyoruz.
# Bu da bir "AI resim modeli" degil (yine kod ile tasarim) ama sonuc gercek
# oyun sanatina cok daha yakin duruyor.
# ---------------------------------------------------------------------------

_SAMPLE_DIRS_PRIORITY = ('billboard', 'particle', 'ground')

# 'isim0000.dxt' / 'isim0019.tga' gibi 4 haneli frame numarali doku
# dosyalarini yakalamak icin (ayni animasyonun frame'lerini gruplamak amacli).
_FRAME_FILE_RE = re.compile(r'^(.*?)(\d{4})\.(dxt|tga)$', re.IGNORECASE)


def scan_texture_groups(fx_root):
    """fx_root altini (alt klasorler dahil, symlink/junction'lar da dahil)
    tarayip .dxt/.tga dosyalarini "ayni animasyonun frame'leri" halinde
    gruplar. Boylece bir .fxb dosyasina hic bagli olmayan, sadece doku
    barindiran klasorlerdeki (arrow/billboard/ground/javelin/particle gibi)
    gorsellere de dogrudan erisilip mevcut seffaflik/renk ayarlama ve
    kaynak-olarak-isaretle/degistir (swap) ozellikleri uygulanabilir.

    '...0000.dxt', '...0001.dxt', ... gibi 4 haneli numarayla biten
    dosyalar ayni grubun frame'leri sayilir; numarasiz tekil dosyalar
    (orn. 'arrow.dxt') kendi baslarina tek-frame'lik bir grup olusturur.
    '.dxt.orijinal_yedek' gibi yedek dosyalar (uzanti tam olarak .dxt/.tga
    ile bitmedigi icin) otomatik olarak disarida kalir.

    Donen deger: {goruntulenecek_ad: [tam_dosya_yolu, ...]} (frame'ler
    numaraya gore sirali liste)."""
    groups = {}
    display_base = {}

    def _on_err(_exc):
        pass  # bir klasorde okuma hatasi olursa o klasoru atla, digerlerine devam et

    for dirpath, _dirnames, filenames in os.walk(fx_root, onerror=_on_err, followlinks=True):
        for fn in filenames:
            low = fn.lower()
            if not (low.endswith('.dxt') or low.endswith('.tga')):
                continue
            m = _FRAME_FILE_RE.match(fn)
            full = os.path.join(dirpath, fn)
            if m:
                base, num, ext = m.group(1), m.group(2), m.group(3)
                key = (dirpath, base.lower(), ext.lower())
                groups.setdefault(key, {})[int(num)] = full
                display_base.setdefault(key, (base, ext))
            else:
                base, ext = os.path.splitext(fn)
                ext = ext.lstrip('.')
                key = (dirpath, base.lower() + '\0single', ext.lower())
                groups.setdefault(key, {})[0] = full
                display_base.setdefault(key, (base, ext))

    result = {}
    for key, frame_map in groups.items():
        dirpath = key[0]
        base, ext = display_base[key]
        rel_dir = os.path.relpath(dirpath, fx_root)
        rel_name = f"{base}.{ext}" if rel_dir == '.' else f"{rel_dir.replace(os.sep, '/')}/{base}.{ext}"
        frames_sorted = [frame_map[i] for i in sorted(frame_map.keys())]
        label = f"{rel_name}  [DOKU x{len(frames_sorted)}]"
        result[label] = frames_sorted
    return result


def list_sample_textures(fx_root, limit=500, per_dir_cap=4000):
    """fx_root altinda billboard/particle/ground klasorlerini tarayip
    (cok yavas olmasin diye adam sayisi ile sinirli) bulunan .dxt/.tga
    dosyalarinin TAM yollarini dondurur."""
    found = []
    search_dirs = []
    for d in _SAMPLE_DIRS_PRIORITY:
        p = os.path.join(fx_root, d)
        if os.path.isdir(p):
            search_dirs.append(p)
    if not search_dirs:
        search_dirs = [fx_root]
    for base in search_dirs:
        count_here = 0
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.lower().endswith(('.dxt', '.tga')) and '.orijinal_yedek' not in fn.lower():
                    found.append(os.path.join(dirpath, fn))
                    count_here += 1
                    if len(found) >= limit:
                        return found
            if count_here >= per_dir_cap:
                break
    return found


def _pick_random_texture(fx_root, sample_list, rng, exclude_path=None, max_attempts=20):
    """sample_list icinden (exclude_path haric) okunabilir bir dokuyu rastgele
    secip yukler (bozuk/desteklenmeyen olanlari atlayarak). Donus:
    (PIL Image RGBA, tam_yol) ya da (None, None) hicbiri okunamazsa.

    NOT: exclude_path once listeden cikarilir, SONRA kalanlar arasindan
    karistirilip denenir -- boylece sample_list kucuk oldugunda (orn. sadece
    2 doku varsa) "excluded olani tekrar tekrar cekip deneme hakkini bosa
    harcama" hatasina dusulmez."""
    candidates = [p for p in sample_list if p != exclude_path]
    if not candidates:
        return None, None
    idxs = list(range(len(candidates)))
    rng.shuffle(idxs)
    for idx in idxs[:max_attempts]:
        candidate = candidates[idx]
        try:
            im = load_texture_any(candidate).convert('RGBA')
            return im, candidate
        except Exception:
            continue
    return None, None


def _random_orient(im, rng):
    """Cesitlilik icin dokuyu rastgele dondurup/aynalayarak baz dokunun
    hep ayni acidan gorunmesini onler."""
    k = rng.randint(0, 4)
    if k:
        im = im.rotate(90 * k)
    if rng.randint(0, 2):
        im = im.transpose(Image.FLIP_LEFT_RIGHT)
    if rng.randint(0, 2):
        im = im.transpose(Image.FLIP_TOP_BOTTOM)
    return im


def derive_from_template(fx_root, hue_shift_deg, size=128, variant=0,
                          extra_style=None, extra_strength=55,
                          use_second_texture=False,
                          sample_list=None, chosen_path=None,
                          second_chosen_path=None, max_attempts=20):
    """Klasordeki gercek bir dokuyu (istege bagli olarak IKINCI bir gercek
    dokuyla harmanlanmis halde) baz alip yeni bir varyant uretir.
    Donus: (PIL Image RGBA, kullanilan_kaynaklarin_aciklama_metni).

    extra_strength: 0-100, ekstra prosedurel katmanin / ikinci dokunun ne
    kadar baskin olacagini kontrol eder (eskiden sabit 0.55'ti).

    NOT: fx klasorlerinde bazi .dxt dosyalari bizim cozdugumuz konteyner
    yapisiyla uyusmuyor olabilir (farkli/eski bir alt-format, bozuk dosya,
    sifreli doku vs.). Byle bir dosya rastgele secilirse sessizce atlayip
    bir sonraki adayi deniyoruz -- kullaniciya hata firlatmak yerine."""
    strength = max(0, min(100, extra_strength)) / 100.0
    rng = np.random.RandomState(variant * 7919 + 3)

    if not sample_list:
        sample_list = list_sample_textures(fx_root)
    if not sample_list and chosen_path is None:
        raise RuntimeError("fx klasorunde ornek alinacak .dxt/.tga dosyasi bulunamadi.")

    if chosen_path is not None:
        base_im = load_texture_any(chosen_path).convert('RGBA')
    else:
        base_im, chosen_path = _pick_random_texture(fx_root, sample_list, rng, max_attempts=max_attempts)
        if base_im is None:
            raise RuntimeError(
                f"Rastgele doku denendi, hicbiri okunamadi. 'Farkli Varyant' ile tekrar dene."
            )
    base_im = _random_orient(base_im, rng).resize((size, size))
    arr = np.array(base_im).astype(np.float32)

    # renk kaydirma
    if hue_shift_deg:
        arr[..., :3] = _hue_shift_array(arr[..., :3], hue_shift_deg)

    # hafif taze/parlaklik artisi -- baz dokuyu oldugu gibi kopyalamak yerine
    # biraz "yeniden yorumlanmis" hissi vermesi icin
    arr[..., :3] = np.clip(arr[..., :3] * 1.05, 0, 255)

    sources = [os.path.relpath(chosen_path, fx_root)]

    if use_second_texture and sample_list:
        second_im = None
        if second_chosen_path is not None:
            try:
                second_im = load_texture_any(second_chosen_path).convert('RGBA')
            except Exception:
                second_im = None
        else:
            second_im, second_chosen_path = _pick_random_texture(
                fx_root, sample_list, rng, exclude_path=chosen_path, max_attempts=max_attempts)
        if second_im is not None:
            second_im = _random_orient(second_im, rng).resize((size, size))
            sarr = np.array(second_im).astype(np.float32)
            if hue_shift_deg:
                sarr[..., :3] = _hue_shift_array(sarr[..., :3], hue_shift_deg)
            s_alpha = sarr[..., 3:4] / 255.0
            # 'lighten' (screen) harmanlama -- iki gercek dokuyu birbirinin
            # uzerine, kaynagin kendi seklini kaybetmeden katar
            screen = 255.0 - (255.0 - arr[..., :3]) * (255.0 - sarr[..., :3]) / 255.0
            arr[..., :3] = arr[..., :3] * (1 - strength * s_alpha) + screen * (strength * s_alpha)
            arr[..., 3] = np.clip(np.maximum(arr[..., 3], sarr[..., 3] * strength), 0, 255)
            sources.append(os.path.relpath(second_chosen_path, fx_root))

    if extra_style and extra_style != 'none':
        overlay = generate_effect_texture(extra_style, (hue_shift_deg) % 360, size=size, variant=variant)
        ov = np.array(overlay).astype(np.float32)
        # 'lighter' (additive) harmanlama -- overlay'in kendi alfasiyla olceklenmis
        ov_alpha = ov[..., 3:4] / 255.0
        arr[..., :3] = np.clip(arr[..., :3] + ov[..., :3] * ov_alpha * strength, 0, 255)
        arr[..., 3] = np.clip(np.maximum(arr[..., 3], ov[..., 3] * strength), 0, 255)

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, 'RGBA'), " + ".join(sources)


# ---------------------------------------------------------------------------
# Seffaflik (%) ve renk (hue kaydirma) duzenleme
#
# Onemli tasarim karari: DXT/BC blok sikistirmasi iki "uc renk" (endpoint)
# ve onlar arasinda 4x4'luk piksel basina 2-bit (renk) / 3-4 bit (alfa)
# interpolasyon indeksinden olusur. Biz SADECE uc renkleri / alfa referans
# degerlerini donusturuyoruz, indeks/desen bitlerine hic dokunmuyoruz. Bu
# sayede gorseldeki tum detay/gradyan korunur, sadece geneldeki renk/seffaflik
# kayar -- hem hizli hem de guvenli (yanlis coz/yeniden-sikistirma riski yok).
# ---------------------------------------------------------------------------

def _pack565(r, g, b):
    r = max(0, min(255, int(round(r))))
    g = max(0, min(255, int(round(g))))
    b = max(0, min(255, int(round(b))))
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _hue_shift_rgb(r, g, b, hue_shift_deg):
    if hue_shift_deg == 0:
        return r, g, b
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    h = (h + hue_shift_deg / 360.0) % 1.0
    r2, g2, b2 = colorsys.hsv_to_rgb(h, s, v)
    return r2 * 255.0, g2 * 255.0, b2 * 255.0


def _adjust_color_block(data, pos, hue_shift_deg, preserve_order):
    if hue_shift_deg == 0:
        return
    c0, c1 = struct.unpack_from('<HH', data, pos)
    r0, g0, b0 = _unpack565(c0)
    r1, g1, b1 = _unpack565(c1)
    r0, g0, b0 = _hue_shift_rgb(r0, g0, b0, hue_shift_deg)
    r1, g1, b1 = _hue_shift_rgb(r1, g1, b1, hue_shift_deg)
    nc0 = _pack565(r0, g0, b0)
    nc1 = _pack565(r1, g1, b1)
    if preserve_order:
        was_gt = c0 > c1
        now_gt = nc0 > nc1
        if was_gt != now_gt and nc0 != nc1:
            nc0, nc1 = nc1, nc0
    struct.pack_into('<HH', data, pos, nc0, nc1)


def _adjust_bc2_alpha_block(data, pos, alpha_pct):
    if alpha_pct == 100:
        return
    factor = max(0.0, alpha_pct / 100.0)
    for i in range(8):
        b = data[pos + i]
        lo = b & 0xF
        hi = (b >> 4) & 0xF
        lo = max(0, min(15, round(lo * factor)))
        hi = max(0, min(15, round(hi * factor)))
        data[pos + i] = (hi << 4) | lo


def _adjust_bc3_alpha_block(data, pos, alpha_pct):
    if alpha_pct == 100:
        return
    factor = max(0.0, alpha_pct / 100.0)
    data[pos] = max(0, min(255, round(data[pos] * factor)))
    data[pos + 1] = max(0, min(255, round(data[pos + 1] * factor)))


def adjust_dxt_bytes(data, alpha_pct=100, hue_shift_deg=0):
    """Bir .dxt dosyasinin ham byte'larini okuyup seffaflik/renk ayarini
    uyguladiktan sonra yeni byte dizisini dondurur (dosya boyutu degismez)."""
    data = bytearray(data)
    namelen = struct.unpack_from('<i', data, 0)[0]
    off = 4
    off += namelen
    magic = bytes(data[off:off + 3]); off += 3
    if magic != b'NTF':
        raise ValueError(f"Beklenmeyen imza: {magic!r}")
    flags = data[off]
    if flags & 0x04:
        raise NotImplementedError("Bu doku sifreli (encrypted), degistirilemiyor.")
    off += 1
    width, height = struct.unpack_from('<ii', data, off); off += 8
    fourcc = bytes(data[off:off + 4])
    off += 8
    pix_off = off

    bw, bh = (width + 3) // 4, (height + 3) // 4
    pos = pix_off
    if fourcc == b'DXT1':
        for _ in range(bw * bh):
            _adjust_color_block(data, pos, hue_shift_deg, preserve_order=True)
            pos += 8
    elif fourcc in (b'DXT3', b'DXT5'):
        for _ in range(bw * bh):
            if fourcc == b'DXT3':
                _adjust_bc2_alpha_block(data, pos, alpha_pct)
            else:
                _adjust_bc3_alpha_block(data, pos, alpha_pct)
            _adjust_color_block(data, pos + 8, hue_shift_deg, preserve_order=False)
            pos += 16
    else:
        raise ValueError(f"Desteklenmeyen fourCC: {fourcc}")
    return bytes(data)


def save_adjusted_texture(path, alpha_pct=100, hue_shift_deg=0):
    """Dosyayi (once .orijinal_yedek olarak yedekleyip) seffaflik/renk
    ayari uygulanmis haliyle DISKE YAZAR. Sadece degisiklik varsa yazar."""
    if alpha_pct == 100 and hue_shift_deg == 0:
        return False
    ext = os.path.splitext(path)[1].lower()
    backup_path = path + '.orijinal_yedek'
    data = open(path, 'rb').read()
    if ext == '.dxt':
        new_data = adjust_dxt_bytes(data, alpha_pct, hue_shift_deg)
    elif ext == '.tga':
        im = Image.open(path).convert('RGBA')
        arr = np.array(im).astype(np.float32)
        if hue_shift_deg:
            arr[..., :3] = _hue_shift_array(arr[..., :3], hue_shift_deg)
        if alpha_pct != 100:
            arr[..., 3] = np.clip(arr[..., 3] * (alpha_pct / 100.0), 0, 255)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        out_im = Image.fromarray(arr, 'RGBA')
        import io
        buf = io.BytesIO()
        out_im.save(buf, format='TGA')
        new_data = buf.getvalue()
    else:
        raise ValueError(f"Desteklenmeyen dosya turu: {ext}")
    if not os.path.exists(backup_path):
        with open(backup_path, 'wb') as f:
            f.write(data)
    with open(path, 'wb') as f:
        f.write(new_data)
    return True


def replace_effect_texture(target_paths, source_images):
    """target_paths uzerindeki her dosyayi (once yedekleyip) source_images
    listesinden (gerekirse dongusel olarak) alinan bir gorselle degistirir.
    Hedefin ORIJINAL boyutuna yeniden olceklenir (ayni .n3fxpart/.fxb
    ayarlariyla uyumlu kalsin diye). Donus: (basarili_sayisi, hata_listesi)."""
    done = 0
    failed = []
    for i, tpath in enumerate(target_paths):
        try:
            src_im = source_images[i % len(source_images)]
            try:
                target_size = load_texture_any(tpath).size
            except Exception:
                target_size = src_im.size
            resized = src_im.convert('RGBA').resize(target_size)
            ext = os.path.splitext(tpath)[1].lower()
            backup_path = tpath + '.orijinal_yedek'
            if not os.path.exists(backup_path):
                with open(backup_path, 'wb') as f:
                    f.write(open(tpath, 'rb').read())
            if ext == '.dxt':
                write_ko_dxt(tpath, resized, name="")
            elif ext == '.tga':
                resized.save(tpath, format='TGA')
            else:
                raise ValueError(f"Desteklenmeyen dosya turu: {ext}")
            done += 1
        except Exception as e:
            failed.append(f"{os.path.basename(tpath)}: {e}")
    return done, failed


def _hue_shift_array(rgb, hue_shift_deg):
    """rgb: (...,3) float32 0-255 -> hue kaydirilmis (...,3) float32 0-255 (numpy vektorize)."""
    a = rgb / 255.0
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    v = maxc
    delta = maxc - minc
    safe_delta = np.where(delta == 0, 1, delta)
    s = np.where(maxc == 0, 0, delta / np.where(maxc == 0, 1, maxc))
    rc = (maxc - r) / safe_delta
    gc = (maxc - g) / safe_delta
    bc = (maxc - b) / safe_delta
    h = np.zeros_like(maxc)
    h = np.where(maxc == r, bc - gc, h)
    h = np.where(maxc == g, 2.0 + rc - bc, h)
    h = np.where(maxc == b, 4.0 + gc - rc, h)
    h = (h / 6.0) % 1.0
    h = np.where(delta == 0, 0, h)
    h = (h + hue_shift_deg / 360.0) % 1.0

    i = np.floor(h * 6.0)
    f = h * 6.0 - i
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    ii = (i.astype(np.int64)) % 6
    conditions = [ii == k for k in range(6)]
    r2 = np.select(conditions, [v, q, p, p, t, v])
    g2 = np.select(conditions, [t, v, v, q, p, p])
    b2 = np.select(conditions, [p, p, t, v, v, q])
    out = np.stack([r2, g2, b2], axis=-1) * 255.0
    return out


def apply_visual_transform(im, alpha_pct=100, hue_shift_deg=0):
    """Sadece ONIZLEME icin: bir PIL RGBA goruntusune seffaflik/renk
    ayarini uygular (diske yazmadan), donusturulmus yeni PIL Image dondurur."""
    if alpha_pct == 100 and hue_shift_deg == 0:
        return im
    arr = np.array(im.convert('RGBA')).astype(np.float32)
    if hue_shift_deg:
        arr[..., :3] = _hue_shift_array(arr[..., :3], hue_shift_deg)
    if alpha_pct != 100:
        arr[..., 3] = np.clip(arr[..., 3] * (alpha_pct / 100.0), 0, 255)
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, 'RGBA')


# ---------------------------------------------------------------------------
# .fxb (binary) / .n3fxpart, .n3fxbundle (metin) icinden texture yollarini cikar
# ---------------------------------------------------------------------------

def is_text_fx(data):
    return data[:20].startswith(b"<N3FXPART>") or data[:20].startswith(b"<N3FXBUNDLE>")


def find_ascii_strings(data, min_len=6):
    out = []
    run_start = None
    for i, b in enumerate(data):
        if 32 <= b < 127:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and i - run_start >= min_len:
                out.append(data[run_start:i].decode('ascii', errors='replace'))
            run_start = None
    if run_start is not None and len(data) - run_start >= min_len:
        out.append(data[run_start:].decode('ascii', errors='replace'))
    return out


def extract_texture_refs(path):
    """Bir .fxb/.n3fxpart/.n3fxbundle dosyasindan (path, numtex_or_None) listesi cikarir."""
    data = open(path, 'rb').read()
    refs = []
    if is_text_fx(data):
        text = data.decode('latin-1')
        if text.startswith('<N3FXBUNDLE>'):
            # Bagli PART dosyalarini bul, onlari da oku
            base_dir = os.path.dirname(path)
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('<PART>'):
                    rel = line[len('<PART>'):].strip().split()[0]
                    rel_norm = rel.replace('/', os.sep).replace('\\', os.sep)
                    # 'fx/...' onekini at, root zaten fx klasoru
                    parts = [p for p in rel_norm.split(os.sep) if p]
                    if parts and parts[0].lower() == 'fx':
                        parts = parts[1:]
                    if parts:
                        *dparts, fname = parts
                        ddir = _resolve_dir_ci(base_dir, dparts) if dparts else base_dir
                        real_fname = _find_child_ci(ddir, fname) if ddir else None
                        part_path = os.path.join(ddir, real_fname) if (ddir and real_fname) else None
                        if part_path and os.path.isfile(part_path):
                            refs.extend(extract_texture_refs(part_path))
            return refs
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('<texture>'):
                val = line[len('<texture>'):].strip()
                toks = val.rsplit(' ', 1)
                if len(toks) == 2 and toks[1].isdigit():
                    refs.append((toks[0], int(toks[1])))
                else:
                    refs.append((val, None))
        return refs
    else:
        # binary .fxb: okunabilir path benzeri string'leri ara.
        # NOT: binary alanlarda string'in hemen onunde/sonunda komsu alanlardan
        # "sizan" 1-2 garbage bayt olabiliyor (orn. "pAfx\\billboard\\..." gibi),
        # bu yuzden "fx\\" ya da "fx/" gecen ilk yerden itibaren kirpiyoruz.
        seen = set()
        mesh_refs = []
        tex_refs = []
        for s in find_ascii_strings(data):
            if len(s) > 220:
                continue
            m = re.search(r'fx[\\/]', s, flags=re.IGNORECASE)
            if not m:
                continue
            cleaned = s[m.start():]
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            if cleaned.lower().endswith('.n3shape'):
                mesh_refs.append((cleaned, None))
            else:
                tex_refs.append((cleaned, None))
        # once 2D texture referanslarini dene (billboard/particle/ground),
        # bulunamazsa mesh (3D) referanslari da listeye ekle (yine de
        # goruntulenemeyecek ama bilgi olarak dursun)
        refs.extend(tex_refs)
        refs.extend(mesh_refs)
        return refs


def _find_child_ci(parent_dir, name):
    """parent_dir icinde 'name' ile BUYUK/kucuk harf farkini gormezden gelerek
    eslesen ilk dosya/klasor adini bulur (Windows zaten case-insensitive ama
    bu fonksiyon Linux/mac uzerinde test ederken de dogru calismasini saglar)."""
    try:
        entries = os.listdir(parent_dir)
    except OSError:
        return None
    target = name.lower()
    for e in entries:
        if e.lower() == target:
            return e
    return None


def _resolve_dir_ci(fx_root, dirparts):
    cur = fx_root
    for part in dirparts:
        child = _find_child_ci(cur, part)
        if child is None:
            return None
        cur = os.path.join(cur, child)
    return cur


def resolve_texture_frames(fx_root, tex_path, numtex, max_frames=40):
    """Bir texture referansini (orn. 'fx\\ground\\30430dm_light_g\\dm_g.tga')
    fx_root altinda gercek dosyalara cozer, bulunan frame dosya yollarini dondurur.
    Windows zaten case-insensitive dosya sistemi kullanir ama bu fonksiyon
    kucuk/buyuk harf farkli olsa bile (orn. klasorde 'Fx' referansta 'fx')
    dogru dosyayi bulabilsin diye elle case-insensitive arama yapar."""
    rel = tex_path.replace('/', os.sep).replace('\\', os.sep)
    parts = [p for p in rel.split(os.sep) if p]
    if parts and parts[0].lower() == 'fx':
        parts = parts[1:]
    if not parts:
        return []
    *dirparts, filename = parts
    base, _ext = os.path.splitext(filename)
    dirpath = _resolve_dir_ci(fx_root, dirparts) if dirparts else fx_root
    if not dirpath or not os.path.isdir(dirpath):
        return []

    try:
        entries = os.listdir(dirpath)
    except OSError:
        return []
    entries_lower = {e.lower(): e for e in entries}

    frames = []
    n = numtex if numtex else max_frames
    for i in range(n):
        found = None
        for ext in ('.dxt', '.tga'):
            key = f"{base}{i:04d}{ext}".lower()
            if key in entries_lower:
                found = os.path.join(dirpath, entries_lower[key])
                break
        if found:
            frames.append(found)
        elif numtex:
            continue
        else:
            if i > 0:
                break
    if not frames:
        # frame numarasiz tek dosya olabilir (orn. base.tga / base.dxt)
        for ext in ('.dxt', '.tga'):
            key = f"{base}{ext}".lower()
            if key in entries_lower:
                frames.append(os.path.join(dirpath, entries_lower[key]))
                break
    return frames


def _resolve_file_ci(fx_root, rel_path):
    """rel_path (orn. 'object\\arrow.n3pmesh' ya da 'fx\\object\\arrow.n3pmesh')
    yolunu fx_root altinda BUYUK/kucuk harf farkini gormezden gelerek gercek
    dosyaya cozer. Bulamazsa None dondurur."""
    rel = rel_path.replace('/', os.sep).replace('\\', os.sep)
    parts = [p for p in rel.split(os.sep) if p]
    if parts and parts[0].lower() == 'fx':
        parts = parts[1:]
    if not parts:
        return None
    *dirparts, filename = parts
    dirpath = _resolve_dir_ci(fx_root, dirparts) if dirparts else fx_root
    if not dirpath or not os.path.isdir(dirpath):
        return None
    real_name = _find_child_ci(dirpath, filename)
    if not real_name:
        return None
    full = os.path.join(dirpath, real_name)
    return full if os.path.isfile(full) else None


# ---------------------------------------------------------------------------
# 3D MESH (.n3shape / .n3pmesh) DESTEGI
#
# Format kesfi: kullanicinin yukledigi GERCEK .n3shape/.n3pmesh dosyalarini
# byte-byte inceleyerek yapildi (herhangi bir resmi/genel dokumantasyon
# bulunamadi -- ayni .dxt formatinda oldugu gibi ampirik reverse-engineering).
#
# .n3pmesh (tek bir alt-mesh'in gercek geometri verisi):
#   int32 namelen + name
#   6x int32 header -- gozlemlenen alanlar: [?, ?, vertex_count, index_count, ?, ?]
#     (bazi dosyalarda son iki alan vertex_count/index_count ile ayni, bazilarinda
#     0 -- anlami tam netlesmedi ama parse icin sadece [2] ve [3]. alanlar yeterli)
#   vertex_count x 32 bayt vertex (8x float32: pos.xyz, normal.xyz, uv.xy)
#   index_count x uint16 (ucgen listesi -- her 3 indeks bir ucgen)
#   (opsiyonel) degisken boyutlu "footer" -- materyal/LOD/ikinci-alt-parca verisi
#     olabilir, bu surum SADECE ilk/ana ucgen listesini render ediyor (bazi
#     karmasik/coklu-parcali mesh'lerde -- orn. bir kilicin kabza+agiz gibi ayri
#     parcalari -- sadece BIR parca gorunebilir, TUMU degil)
#
# .n3shape (bir efektin/objenin "kabugu" -- referans ettigi .n3pmesh + .dxt
# dosyalarinin YOLLARINI, uzunluk-onekli (int32 len + ascii) string olarak
# iceriyor, tipki .dxt/.n3pmesh header'larindaki isim alani gibi):
#   ...  (bounding box / transform / skin verisi -- bu surumde kullanilmiyor)
#   int32 namelen + "object\\xxx.n3pmesh" (baz mesh referansi)
#   ...
#   int32 namelen + "object\\xxx.dxt"     (baz texture referansi, bu surumde
#                                          henuz gorsele uygulanmiyor)
# ---------------------------------------------------------------------------

def extract_n3shape_refs(path):
    """'.n3shape' dosyasindan referans edilen .n3pmesh ve .dxt/.tga yollarini
    cikarir. Uzunluk-onekli (int32 length + ascii text) string'leri tarayarak
    bulur -- bu N3 formatinin genel konvansiyonu (dosya adi/texture/mesh
    referanslari hep boyle saklaniyor). Donus: (mesh_ref, texture_ref) --
    bulunamayanlar None."""
    data = open(path, 'rb').read()
    n = len(data)
    mesh_ref = None
    tex_ref = None
    off = 0
    while off + 4 <= n and (mesh_ref is None or tex_ref is None):
        (slen,) = struct.unpack_from('<i', data, off)
        if 0 < slen <= 260 and off + 4 + slen <= n:
            chunk = data[off + 4:off + 4 + slen]
            if all(32 <= b < 127 for b in chunk):
                s = chunk.decode('ascii')
                low = s.lower()
                if mesh_ref is None and low.endswith('.n3pmesh'):
                    mesh_ref = s
                elif tex_ref is None and low.endswith(('.dxt', '.tga')):
                    tex_ref = s
        off += 1
    return mesh_ref, tex_ref


def parse_n3pmesh(path):
    """Basit (tek ucgen-listesi) .n3pmesh dosyalarini parse eder.
    Donus: (verts, faces) -- verts: [(x,y,z), ...], faces: [(i0,i1,i2), ...].

    NOT: bazi karmasik/coklu-parcali mesh dosyalari (orn. birden fazla
    materyal/alt-parcadan olusan silahlar) bu basit varsayimla TAM uyusmuyor
    olabilir -- boyle durumda RuntimeError firlatilir, program cokmez,
    kullaniciya net bir mesaj gosterilir."""
    data = open(path, 'rb').read()
    if len(data) < 8:
        raise RuntimeError("dosya cok kucuk")
    namelen = struct.unpack_from('<i', data, 0)[0]
    off = 4 + namelen
    if namelen < 0 or off + 24 > len(data):
        raise RuntimeError("n3pmesh header okunamadi (namelen gecersiz)")
    h = struct.unpack_from('<6i', data, off)
    vcount, icount = h[2], h[3]
    off += 24
    if vcount <= 0 or icount <= 0 or icount % 3 != 0:
        raise RuntimeError(
            f"bu .n3pmesh dosyasi basit (tek parcali ucgen listesi) formatla uyusmuyor "
            f"(vertex_count={vcount}, index_count={icount})")
    vbytes = vcount * 32
    if off + vbytes > len(data):
        raise RuntimeError("vertex verisi dosya boyutuyla uyusmuyor")
    verts = []
    for i in range(vcount):
        vals = struct.unpack_from('<8f', data, off + i * 32)
        verts.append(vals[:3])
    ioff = off + vbytes
    ibytes = icount * 2
    if ioff + ibytes > len(data):
        raise RuntimeError("index verisi dosya boyutuyla uyusmuyor")
    idx = struct.unpack_from(f'<{icount}h', data, ioff)
    if max(idx) >= vcount or min(idx) < 0:
        raise RuntimeError("index degerleri gecersiz araliktaki (format varsayimi bu dosya icin uymuyor)")
    faces = [tuple(idx[i:i + 3]) for i in range(0, icount, 3)]
    return verts, faces


def _normalize3(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


def render_mesh_frame(verts, faces, angle_deg, size=300, tilt_deg=20):
    """verts/faces'i basit bir yazilim (software) 3D render ile tek bir
    aciya gore duzlem goruntusune (PIL RGBA Image) cevirir -- gercek
    texture/animasyon YOK, sadece duz golgeli (flat-shaded) statik model
    onizlemesi (bir isik yonune gore ucgen basina parlaklik hesabi ve
    painter's algorithm ile derinlik siralamasi)."""
    xs = [v[0] for v in verts]; ys = [v[1] for v in verts]; zs = [v[2] for v in verts]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    cz = (min(zs) + max(zs)) / 2.0
    extent = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs), 1e-6)
    scale = (size * 0.38) / (extent / 2.0)
    a = math.radians(angle_deg)
    ca, sa = math.cos(a), math.sin(a)
    t = math.radians(tilt_deg)
    ct, st = math.cos(t), math.sin(t)
    proj = []
    for v in verts:
        x, y, z = v[0] - cx, v[1] - cy, v[2] - cz
        x, z = x * ca + z * sa, -x * sa + z * ca   # Y ekseni etrafinda dondur
        y, z = y * ct - z * st, y * st + z * ct    # hafif X tilt (3 boyutlu his icin)
        proj.append((x, y, z))
    light = _normalize3((0.4, 0.6, 1.0))
    tris = []
    for (i0, i1, i2) in faces:
        p0, p1, p2 = proj[i0], proj[i1], proj[i2]
        ux, uy, uz = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        vx, vy, vz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        nlen = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / nlen, ny / nlen, nz / nlen
        intensity = max(0.15, nx * light[0] + ny * light[1] + nz * light[2])
        avg_z = (p0[2] + p1[2] + p2[2]) / 3.0
        tris.append((avg_z, intensity, p0, p1, p2))
    tris.sort(key=lambda tr: tr[0])  # painter's algorithm: once uzaktakiler
    im = Image.new('RGBA', (size, size), (30, 32, 40, 255))
    draw = ImageDraw.Draw(im)
    cxp, cyp = size / 2.0, size / 2.0
    for avg_z, intensity, p0, p1, p2 in tris:
        pts = []
        for (x, y, z) in (p0, p1, p2):
            pts.append((cxp + x * scale, cyp - y * scale))
        base = int(60 + 150 * intensity)
        color = (base, base, min(255, int(base * 1.05) + 20), 255)
        draw.polygon(pts, fill=color, outline=(20, 20, 25, 255))
    return im


def render_mesh_frames(verts, faces, n_frames=24, size=300, tilt_deg=20):
    """360 derece etrafinda donen n_frames adet kare (PIL Image listesi)
    uretir -- var olan sprite-animasyon altyapisiyla (start_anim/_tick_anim)
    dogrudan uyumlu olsun diye."""
    frames = []
    for i in range(n_frames):
        angle = 360.0 * i / n_frames
        frames.append(render_mesh_frame(verts, faces, angle, size=size, tilt_deg=tilt_deg))
    return frames


def _resolve_and_parse_shape(fx_root, shape_ref):
    """Bir .n3shape referansini (orn. 'fx\\object\\xxx\\yyy.n3shape') coz,
    icindeki .n3pmesh'i bul ve parse et. Donus: (verts, faces, mesh_path).
    Basarisiz olursa RuntimeError firlatir (mesaji kullaniciya gosterilmeye
    hazir, Turkce ve aciklayici)."""
    shape_path = _resolve_file_ci(fx_root, shape_ref)
    if not shape_path:
        raise RuntimeError(".n3shape dosyasi diskte bulunamadi.")
    try:
        mesh_ref, tex_ref = extract_n3shape_refs(shape_path)
    except Exception as e:
        raise RuntimeError(f".n3shape okunamadi: {e}")
    if not mesh_ref:
        raise RuntimeError(".n3shape icinde bir .n3pmesh referansi bulunamadi -- bu dosya format varsayimimizla uyusmuyor olabilir.")
    # NOT: .n3shape icindeki yol ('object\\xxx.n3pmesh') her zaman fx_root'a
    # gore dogru klasoru göstermeyebiliyor (orn. .n3shape bir alt klasorde
    # olabilir ama ic referans sadece 'object\\...' diyor). Gercekte .n3pmesh
    # neredeyse hep .n3shape ile AYNI ADI tasiyan bir "kardes" (sibling) dosya
    # olarak AYNI KLASORDE duruyor -- once onu dene, sonra tam yolu dene.
    shape_dir = os.path.dirname(shape_path)
    mesh_basename = os.path.basename(mesh_ref.replace('/', os.sep).replace('\\', os.sep))
    sibling_name = _find_child_ci(shape_dir, mesh_basename)
    mesh_path = os.path.join(shape_dir, sibling_name) if sibling_name else None
    if not mesh_path:
        mesh_path = _resolve_file_ci(fx_root, mesh_ref)
    if not mesh_path:
        raise RuntimeError(f"Referans edilen mesh dosyasi diskte bulunamadi: {mesh_ref}")
    try:
        verts, faces = parse_n3pmesh(mesh_path)
    except Exception as e:
        raise RuntimeError(
            f"3D model dosyasi ({os.path.relpath(mesh_path, fx_root)}) su anki basit "
            f"parser ile acilamadi -- muhtemelen birden fazla parcali/karmasik bir mesh: {e}")
    return verts, faces, mesh_path


# ---------------------------------------------------------------------------
# PROSEDUREL 3D MODEL URETIMI (deneysel!)
#
# 2D dokularda yaptigimiz "Yeni Efekt Tasarla" ozelliginin 3D karsiligi --
# sifirdan (kod/matematik ile) basit geometrik sekiller (kristal, dikenli
# yildiz, sivri parca kumesi, dusuk-poligon kure) uretir. parse_n3pmesh ile
# AYNI (verts, faces) formatinda dondugu icin render_mesh_frame/frames
# dogrudan kullanilabiliyor.
#
# write_n3pmesh ile bunlar GERCEK bir .n3pmesh dosyasi olarak diske de
# yazilabiliyor -- ANCAK bu DENEYSEL bir ozellik: .n3pmesh formatinin bazi
# alanlarinin (ozellikle degisken boyutlu "footer") gercek anlami hala
# cozulmedi, o yuzden yazdigimiz dosyanin GERCEK OYUN ICINDE dogru
# calisacagi GARANTI DEGIL. Bizim kendi onizlememizde her zaman doğru
# gorunur (cunku footer'i zaten okurken yok sayiyoruz). Riski azaltmak icin
# bu ozellik YENI bir .n3shape yazmiyor -- bunun yerine VAR OLAN, calisan
# bir .n3shape'in referans ettigi .n3pmesh dosyasinin YERINE (ayni dosya
# adiyla, yedekleyerek) gecirilmek uzere tasarlandi; boylece .n3shape'in
# kendisi (hangi alanlari tasidigini tam cozemedigimiz) hic degismiyor.
# ---------------------------------------------------------------------------

def _tri_normal(a, b, c):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / length, ny / length, nz / length)


def _emit_tri(verts, faces, a, b, c, uv_a=(0.0, 0.0), uv_b=(1.0, 0.0), uv_c=(0.5, 1.0)):
    n = _tri_normal(a, b, c)
    base = len(verts)
    for p, uv in ((a, uv_a), (b, uv_b), (c, uv_c)):
        verts.append((p[0], p[1], p[2], n[0], n[1], n[2], uv[0], uv[1]))
    faces.append((base, base + 1, base + 2))


def generate_mesh_crystal(n_sides=6, height=1.0, radius=0.45, variant=0):
    """Iki apex (uc) + ortada n_sides koseli bir halka -- kristal/mucevher
    seklinde bir bipiramit."""
    rng = np.random.RandomState(variant * 131 + 7)
    ring = []
    for i in range(n_sides):
        ang = 2 * math.pi * i / n_sides
        r = radius * (1.0 + rng.uniform(-0.15, 0.15))
        ring.append((r * math.cos(ang), 0.0, r * math.sin(ang)))
    top = (0.0, height, 0.0)
    bottom = (0.0, -height * 0.55, 0.0)
    verts, faces = [], []
    for i in range(n_sides):
        a, b = ring[i], ring[(i + 1) % n_sides]
        _emit_tri(verts, faces, top, a, b)
        _emit_tri(verts, faces, bottom, b, a)
    return verts, faces


def _icosahedron_dirs():
    """Standart ikosahedron'un 12 tepe noktasi, birim vektor (yon) olarak."""
    phi = (1.0 + math.sqrt(5.0)) / 2.0
    raw = [
        (-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
        (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
        (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1),
    ]
    out = []
    for (x, y, z) in raw:
        length = math.sqrt(x * x + y * y + z * z)
        out.append((x / length, y / length, z / length))
    return out


def generate_mesh_spikes(directions, spike_len=1.0, base_r=0.12, variant=0):
    """Her yon icin merkezden disariya dogru sivri, 4 yuzlu ince bir dikenli
    piramit uretir (dikenli yildiz / sivri parca kumesi efektleri icin)."""
    rng = np.random.RandomState(variant * 977 + 13)
    verts, faces = [], []
    for (dx, dy, dz) in directions:
        length = spike_len * (1.0 + rng.uniform(-0.2, 0.2))
        tip = (dx * length, dy * length, dz * length)
        up = (0.0, 1.0, 0.0) if abs(dy) < 0.9 else (1.0, 0.0, 0.0)
        ax = (up[1] * dz - up[2] * dy, up[2] * dx - up[0] * dz, up[0] * dy - up[1] * dx)
        al = math.sqrt(sum(c * c for c in ax)) or 1.0
        ax = tuple(c / al for c in ax)
        ay = (dy * ax[2] - dz * ax[1], dz * ax[0] - dx * ax[2], dx * ax[1] - dy * ax[0])
        base_center = (dx * base_r * 0.3, dy * base_r * 0.3, dz * base_r * 0.3)
        ring = []
        for k in range(4):
            ang = 2 * math.pi * k / 4 + rng.uniform(0, 0.3)
            px = base_center[0] + ax[0] * base_r * math.cos(ang) + ay[0] * base_r * math.sin(ang)
            py = base_center[1] + ax[1] * base_r * math.cos(ang) + ay[1] * base_r * math.sin(ang)
            pz = base_center[2] + ax[2] * base_r * math.cos(ang) + ay[2] * base_r * math.sin(ang)
            ring.append((px, py, pz))
        for k in range(4):
            a, b = ring[k], ring[(k + 1) % 4]
            _emit_tri(verts, faces, tip, a, b)
    return verts, faces


_ICOSAHEDRON_FACES = [
    (0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
    (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
    (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
    (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1),
]


def generate_mesh(style, size=1.0, variant=0):
    """Sifirdan (prosedurel/matematiksel) basit bir 3D model uretir.
    style: 'crystal' | 'star3d' | 'shard' | 'orb'
    Donus: (verts, faces) -- parse_n3pmesh ile AYNI format (render_mesh_frame
    ve write_n3pmesh ile dogrudan uyumlu)."""
    if style == 'crystal':
        return generate_mesh_crystal(n_sides=6 + variant % 3, height=size, radius=size * 0.45, variant=variant)
    elif style == 'star3d':
        dirs = _icosahedron_dirs()
        return generate_mesh_spikes(dirs, spike_len=size, base_r=size * 0.14, variant=variant)
    elif style == 'shard':
        rng = np.random.RandomState(variant * 311 + 5)
        n_shards = 8 + variant % 5
        dirs = []
        for _ in range(n_shards):
            v = rng.normal(size=3)
            vl = float(np.linalg.norm(v)) or 1.0
            dirs.append(tuple((v / vl).tolist()))
        return generate_mesh_spikes(dirs, spike_len=size * 1.1, base_r=size * 0.10, variant=variant)
    elif style == 'orb':
        dirs = _icosahedron_dirs()
        verts, faces = [], []
        for (ia, ib, ic) in _ICOSAHEDRON_FACES:
            a = tuple(c * size for c in dirs[ia])
            b = tuple(c * size for c in dirs[ib])
            c = tuple(c * size for c in dirs[ic])
            _emit_tri(verts, faces, a, b, c)
        return verts, faces
    else:
        raise ValueError(f"Bilinmeyen 3D model stili: {style}")


def write_n3pmesh(path, verts, faces, name=""):
    """Prosedurel uretilen (verts, faces) veriyi GERCEK bir .n3pmesh dosyasi
    olarak yazar (reverse-engineering ile cozulen format ile). verts: her biri
    8 float (pos.xyz, normal.xyz, uv.xy); faces: her biri 3 vertex indeksi.

    UYARI (deneysel): footer/materyal blogunun tam anlami cozulmedigi icin bu
    dosyanin GERCEK OYUNDA dogru calisacagi garanti degil -- sadece bu
    programin KENDI onizlemesinde kesinlikle dogru calisir (okurken footer'i
    zaten yok sayiyoruz). En basit/kucuk gozlemlenen footer (4 bayt sifir,
    ornegin karuflags/housetop gibi TEK PARCALI temiz dosyalarda gorulen)
    kullanilir."""
    name_b = name.encode('ascii', errors='replace')
    vcount = len(verts)
    icount = len(faces) * 3
    if vcount == 0 or icount == 0:
        raise ValueError("bos mesh (vertex/face yok) yazilamaz")
    out = struct.pack('<i', len(name_b)) + name_b
    out += struct.pack('<6i', 0, 0, vcount, icount, vcount, icount)
    for v in verts:
        out += struct.pack('<8f', *v)
    flat_idx = []
    for f in faces:
        flat_idx.extend(f)
    out += struct.pack(f'<{icount}h', *flat_idx)
    out += struct.pack('<i', 0)  # en basit/guvenli gozlemlenen footer
    with open(path, 'wb') as f:
        f.write(out)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

APP_NAME = "KayraSouze Fx Editor"


def _make_app_icon(size=64):
    """Uygulamanin kendi ikonunu programin KENDI efekt jeneratoruyle uretir
    (harici bir resim dosyasina ihtiyac yok -- .pyw tek dosya olarak kalir).
    Varsayilan Tk 'tuy' (feather) ikonunun yerini alsin diye kullaniliyor."""
    core = generate_effect_texture('burst', hue_deg=195, size=size, variant=2)
    # koyu, hafif yuvarlak bir arka plan katmani -- ikon seffaf pencere/taskbar
    # zeminlerinde de secilebilir olsun diye
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    cx = cy = (size - 1) / 2.0
    r = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (size / 2.0)
    bg_alpha = np.clip((1.0 - r) * 1.3, 0, 1) * 255
    bg = np.zeros((size, size, 4), dtype=np.uint8)
    bg[..., 0] = 20
    bg[..., 1] = 22
    bg[..., 2] = 30
    bg[..., 3] = bg_alpha.astype(np.uint8)
    bg_im = Image.fromarray(bg, 'RGBA')
    icon = Image.alpha_composite(bg_im, core)
    return icon


class FxbViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self._app_icon_img = None
        try:
            icon_im = _make_app_icon(64)
            self._app_icon_img = ImageTk.PhotoImage(icon_im)
            self.iconphoto(True, self._app_icon_img)
        except Exception:
            pass  # ikon uretimi basarisiz olursa sessizce varsayilana dus
        self.geometry("980x620")
        self.fx_root = None
        self.all_files = []
        self.texture_groups = {}
        self.anim_frames = []
        self.anim_index = 0
        self.anim_job = None

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=6, pady=6)
        ttk.Button(top, text="Klasor Sec (fx)", command=self.choose_folder).pack(side=tk.LEFT)
        self.folder_label = ttk.Label(top, text="(henuz klasor secilmedi)")
        self.folder_label.pack(side=tk.LEFT, padx=8)
        ttk.Button(top, text="Yeni Efekt Tasarla...", command=self.open_design_dialog).pack(side=tk.RIGHT)
        ttk.Button(top, text="3D Model Tasarla...", command=self.open_3d_design_dialog).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(top, text="Efekt Galerisi...", command=self.open_gallery).pack(side=tk.RIGHT, padx=(0, 6))

        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text="Filtrele:").pack(anchor=tk.W)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add('write', lambda *a: self.refresh_list())
        ttk.Entry(left, textvariable=self.filter_var, width=34).pack(fill=tk.X)

        self.listbox = tk.Listbox(left, width=45, height=32)
        self.listbox.pack(fill=tk.Y, expand=True, pady=(4, 0))
        self.listbox.bind('<<ListboxSelect>>', self.on_select)
        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.config(yscrollcommand=sb.set)

        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0))

        self.canvas = tk.Canvas(right, width=340, height=340, bg="#222222")
        self.canvas.pack(pady=4)
        self.canvas_image_id = None
        self.tk_img = None

        adjust = ttk.Frame(right)
        adjust.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(adjust, text="Seffaflik %:").grid(row=0, column=0, padx=(0, 4))
        self.alpha_var = tk.IntVar(value=100)
        ttk.Spinbox(adjust, from_=0, to=100, textvariable=self.alpha_var, width=6).grid(row=0, column=1)
        ttk.Label(adjust, text="   Renk (hue derece):").grid(row=0, column=2, padx=(12, 4))
        self.hue_var = tk.IntVar(value=0)
        ttk.Spinbox(adjust, from_=-180, to=180, textvariable=self.hue_var, width=6).grid(row=0, column=3)
        ttk.Button(adjust, text="Onizle", command=self.preview_adjust).grid(row=0, column=4, padx=(12, 4))
        ttk.Button(adjust, text="Kaydet (dosyaya yaz)", command=self.save_adjust).grid(row=0, column=5, padx=4)
        ttk.Button(adjust, text="Sifirla", command=self.reset_adjust).grid(row=0, column=6, padx=4)

        swap = ttk.Frame(right)
        swap.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(swap, text="Begendim -> Kaynak Olarak Isaretle", command=self.mark_as_source).pack(side=tk.LEFT)
        ttk.Button(swap, text="Bu Efekti Isaretliyle Degistir", command=self.apply_swap).pack(side=tk.LEFT, padx=6)
        self.marked_label_var = tk.StringVar(value="(kaynak isaretlenmedi)")
        ttk.Label(swap, textvariable=self.marked_label_var).pack(side=tk.LEFT, padx=6)

        self.info_text = tk.Text(right, width=70, height=18, wrap=tk.WORD)
        self.info_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        self.current_frame_paths = []
        self.current_rel_path = None
        self.current_is_mesh = False
        self.current_mesh_path = None
        self.current_mesh_rel = None
        self.marked_source_paths = []
        self.marked_source_rel = None

    def choose_folder(self):
        d = filedialog.askdirectory(title="fx klasorunu sec")
        if not d:
            return
        self.fx_root = d
        self.folder_label.config(text=d)
        self.all_files = []
        walk_errors = []

        def _on_walk_error(exc):
            # os.walk varsayilan olarak bir klasorde hata olunca (izin,
            # bozuk junction, vs.) sessizce o klasoru atlar ama bazen tum
            # dongu yarida kesilebiliyordu; burada hatayi kaydedip devam
            # ediyoruz ki bir klasordeki sorun digerlerini engellemesin.
            walk_errors.append(getattr(exc, 'filename', str(exc)))

        try:
            # followlinks=True: bazi fx kurulumlarinda alt klasorler
            # (arrow/billboard/ground/... ) NTFS junction/symlink olarak
            # baglaniyor; varsayilan followlinks=False bu klasorlerin
            # ICINE inmedigi icin oradaki .fxb dosyalari hic gorunmuyordu.
            for dirpath, _dirnames, filenames in os.walk(d, onerror=_on_walk_error, followlinks=True):
                for fn in filenames:
                    if fn.lower().endswith(('.fxb', '.n3fxpart', '.n3fxbundle')):
                        full = os.path.join(dirpath, fn)
                        rel = os.path.relpath(full, d)
                        self.all_files.append(rel)
        except Exception:
            messagebox.showerror(
                "Klasor Tarama Hatasi",
                "Klasor taranirken beklenmeyen bir hata olustu:\n\n" + traceback.format_exc()
            )

        # arrow/billboard/ground/javelin/object/particle gibi hicbir .fxb
        # icermeyen, sadece doku (.dxt/.tga) barindiran klasorlerdeki
        # gorsellere de dogrudan erisebilmek icin bunlari da listeye ekliyoruz.
        self.texture_groups = {}
        try:
            self.texture_groups = scan_texture_groups(d)
        except Exception:
            messagebox.showwarning(
                "Doku Tarama Hatasi",
                "Doku (.dxt/.tga) dosyalari taranirken hata olustu, "
                "sadece .fxb listesiyle devam ediliyor:\n\n" + traceback.format_exc()
            )
        self.all_files.extend(self.texture_groups.keys())

        self.all_files.sort()
        self.refresh_list()

        if walk_errors:
            preview = "\n".join(walk_errors[:15])
            more = f"\n... (+{len(walk_errors) - 15} tane daha)" if len(walk_errors) > 15 else ""
            messagebox.showwarning(
                "Bazi Klasorler Okunamadi",
                f"{len(walk_errors)} klasor/dosyaya erisilemedi, atlandi:\n\n{preview}{more}"
            )

        if not self.all_files:
            messagebox.showinfo(
                "Sonuc Yok",
                "Secilen klasorde (alt klasorler dahil) ne .fxb/.n3fxpart/.n3fxbundle "
                "ne de .dxt/.tga dosyasi bulunamadi."
            )

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        filt = self.filter_var.get().lower()
        self._visible = [f for f in self.all_files if filt in f.lower()]
        for f in self._visible[:5000]:
            self.listbox.insert(tk.END, f)

    def on_select(self, _evt):
        sel = self.listbox.curselection()
        if not sel:
            return
        rel = self._visible[sel[0]]
        if rel in self.texture_groups:
            self.show_texture_group(rel)
            return
        full = os.path.join(self.fx_root, rel)
        self.show_file(full, rel)

    def show_texture_group(self, rel):
        """Bir .fxb dosyasina bagli olmadan, dogrudan secilen bir doku
        (.dxt/.tga) frame grubunu gosterir -- arrow/billboard/ground/
        javelin/particle gibi klasorlerdeki gorsellere de fxb'siz erisim
        saglar. current_frame_paths/current_rel_path'i doldurdugu icin
        seffaflik/renk ayarlama, kaydetme ve kaynak-olarak-isaretle/
        degistir (swap) ozellikleri buradan secilen gorsellerde de aynen
        calisir."""
        self.stop_anim()
        self.info_text.delete('1.0', tk.END)
        frames = self.texture_groups.get(rel, [])
        self.current_frame_paths = list(frames)
        self.current_rel_path = rel
        self.current_is_mesh = False
        self.current_mesh_path = None
        self.current_mesh_rel = None
        self.reset_adjust(redraw=False)
        info_lines = [f"Dosya: {rel}\n", "Format: DOGRUDAN DOKU (.dxt/.tga) -- bir .fxb'ye bagli degil\n"]
        info_lines.append(f"\n{len(frames)} frame:")
        for fpath in frames:
            info_lines.append(f"   {os.path.relpath(fpath, self.fx_root)}")
        self.info_text.insert(tk.END, "\n".join(info_lines))
        if frames:
            self.start_anim(frames)
        else:
            self.canvas.delete("all")

    def show_file(self, full_path, rel_path):
        self.stop_anim()
        self.info_text.delete('1.0', tk.END)
        self.current_frame_paths = []
        self.current_rel_path = rel_path
        self.current_is_mesh = False
        self.current_mesh_path = None
        self.current_mesh_rel = None
        self.reset_adjust(redraw=False)
        info_lines = [f"Dosya: {rel_path}\n"]
        try:
            self._show_file_inner(full_path, rel_path, info_lines)
        except Exception:
            info_lines.append("\n\nBEKLENMEYEN HATA (lutfen bu mesaji bana gonder):\n")
            info_lines.append(traceback.format_exc())
            self.canvas.delete("all")
        self.info_text.insert(tk.END, "\n".join(info_lines))

    def _show_file_inner(self, full_path, rel_path, info_lines):
        data = open(full_path, 'rb').read()
        if is_text_fx(data):
            info_lines.append("Format: DUZ METIN (n3fxpart/n3fxbundle)\n")
            text = data.decode('latin-1')
            for line in text.splitlines():
                line = line.strip()
                if line.startswith('<') and '>' in line and len(line) < 120:
                    info_lines.append(line)
        else:
            info_lines.append("Format: BINARY .fxb (sayisal alanlar dogrulanmamis, sadece gorsel/texture cikariliyor)\n")

        refs = []
        try:
            refs = extract_texture_refs(full_path)
        except Exception as e:
            info_lines.append(f"Texture cikarma hatasi: {e}")

        frames = []
        used_ref = None
        for tex_path, numtex in refs:
            fr = resolve_texture_frames(self.fx_root, tex_path, numtex)
            if fr:
                frames = fr
                used_ref = tex_path
                break

        if frames:
            info_lines.append(f"\nBulunan doku: {used_ref}  ({len(frames)} frame)")
            for fpath in frames:
                info_lines.append(f"   {os.path.relpath(fpath, self.fx_root)}")
            mesh_only = [r for r, _ in refs if r.lower().endswith('.n3shape')]
            if mesh_only:
                info_lines.append(f"\n(Ayrica {len(mesh_only)} adet 3D mesh (.n3shape) referansi var, bu program onlari gosteremiyor -- sadece 2D texture kismi gosteriliyor.)")
            self.current_frame_paths = frames
            self.start_anim(frames)
        else:
            mesh_only = [r for r, _ in refs if r.lower().endswith('.n3shape')]
            if mesh_only:
                shown = self._try_render_mesh_preview(mesh_only[0], info_lines)
                if not shown:
                    self.canvas.delete("all")
            elif refs:
                info_lines.append(f"\n(Texture referanslari bulundu ama diskte karsilik gelen .dxt/.tga dosyalari bulunamadi: {refs[0][0]})")
                self.canvas.delete("all")
            else:
                info_lines.append("\n(Bu dosyada okunabilir bir texture/mesh yolu bulunamadi.)")
                self.canvas.delete("all")

    def _try_render_mesh_preview(self, shape_ref, info_lines):
        """Bir .n3shape referansini (orn. 'fx\\object\\xxx\\yyy.n3shape') coz,
        icindeki .n3pmesh'i bulup basit bir donen 3D onizleme goster. Basarili
        olursa True, olamazsa (dosya bulunamadi / format uyusmadi) bilgi
        mesajini info_lines'a ekleyip False dondurur -- program hicbir zaman
        cokmez, sadece daha az detayli bir mesaj gosterir."""
        info_lines.append(f"\n3D mesh (.n3shape) efekti: {shape_ref}")
        if not self.fx_root:
            info_lines.append("(Onizleme icin once fx klasoru secilmeli.)")
            return False
        try:
            verts, faces, mesh_path = _resolve_and_parse_shape(self.fx_root, shape_ref)
        except Exception as e:
            info_lines.append(f"({e})")
            return False
        try:
            frames = render_mesh_frames(verts, faces, n_frames=24, size=320)
        except Exception as e:
            info_lines.append(f"(3D onizleme render edilirken hata olustu: {e})")
            return False
        mesh_rel = os.path.relpath(mesh_path, self.fx_root)
        info_lines.append(
            f"3D model bulundu: {mesh_rel}  "
            f"({len(verts)} vertex, {len(faces)} ucgen)\n"
            "(NOT: bu SADECE geometri onizlemesi -- texture/renk/animasyon/iskelet "
            "henuz uygulanmiyor, bazi coklu-parcali modellerin sadece bir parcasi "
            "gorunebilir. Duz golgeli, donen basit bir 3D onizleme. Alttaki '3D Model "
            "Tasarla' butonuyla bu mesh'in GEOMETRISINI degistirebilirsin.)")
        self.current_frame_paths = []  # 3D mesh dosyaya-yazma islemlerini desteklemiyor
        self.current_is_mesh = True
        self.current_mesh_path = mesh_path
        self.current_mesh_rel = mesh_rel
        self.start_anim_frames_direct(frames)
        return True

    def start_anim_frames_direct(self, pil_frames):
        """start_anim'in dosyadan-yukleme kismini atlayip, HAZIR PIL Image
        karelerini (orn. 3D mesh render cikisini) dogrudan animasyon
        altyapisina (_tick_anim) besler."""
        self.anim_frames = []
        for im in pil_frames:
            disp = im.copy()
            disp.thumbnail((320, 320))
            self.anim_frames.append(ImageTk.PhotoImage(disp))
        self.anim_index = 0
        self._tick_anim()

    def get_effect_thumbnail(self, rel_path, size=80):
        """rel_path (.fxb/.n3fxpart/.n3fxbundle) icin kucuk bir onizleme
        (PIL RGBA Image) uretir -- Efekt Galerisi'nde kullanmak icin. 2D
        texture'li efektlerde ilk frame'i, 3D mesh-only efektlerde basit bir
        statik 3D render'i dondurur. Hicbir sey bulunamazsa None dondurur
        (galeri bu durumda o efekti sessizce atlar)."""
        if not self.fx_root:
            return None
        full = os.path.join(self.fx_root, rel_path)
        try:
            refs = extract_texture_refs(full)
        except Exception:
            return None
        for tex_path, numtex in refs:
            try:
                frames = resolve_texture_frames(self.fx_root, tex_path, numtex)
                if frames:
                    im = load_texture_any(frames[0]).convert('RGBA')
                    im.thumbnail((size, size))
                    return im
            except Exception:
                continue
        mesh_only = [r for r, _ in refs if r.lower().endswith('.n3shape')]
        for shape_ref in mesh_only:
            try:
                verts, faces, _mesh_path = _resolve_and_parse_shape(self.fx_root, shape_ref)
                im = render_mesh_frame(verts, faces, angle_deg=35, size=size)
                return im
            except Exception:
                continue
        return None

    def start_anim(self, frame_paths, alpha_pct=100, hue_shift_deg=0):
        self.anim_frames = []
        errors = []
        for p in frame_paths:
            try:
                im = load_texture_any(p)
                im = apply_visual_transform(im, alpha_pct, hue_shift_deg)
                im.thumbnail((320, 320))
                self.anim_frames.append(ImageTk.PhotoImage(im))
            except Exception as e:
                errors.append(f"{os.path.basename(p)}: {e}")
        if errors:
            self.info_text.insert(tk.END, "\n\nUYARI -- bazi frame'ler yuklenemedi:\n" + "\n".join(errors))
        self.anim_index = 0
        self._tick_anim()

    def preview_adjust(self):
        if not self.current_frame_paths:
            return
        self.stop_anim()
        self.start_anim(self.current_frame_paths, self.alpha_var.get(), self.hue_var.get())

    def reset_adjust(self, redraw=True):
        self.alpha_var.set(100)
        self.hue_var.set(0)
        if redraw and self.current_frame_paths:
            self.preview_adjust()

    def _no_texture_message(self):
        """current_frame_paths bos oldugunda gosterilecek mesaj -- sebep 3D
        mesh oldugunda (2D texture-duzenleme ozellikleri 3D geometriye
        uygulanamiyor) ayri, daha aciklayici bir metin dondurur."""
        if getattr(self, 'current_is_mesh', False):
            return ("Bu efekt 3D mesh (geometri) kullaniyor -- seffaflik/renk "
                     "degistirme, kaynak isaretleme ve efekt degistirme (swap) "
                     "ozellikleri su an SADECE 2D texture'li (billboard/particle/"
                     "ground) efektlerde calisiyor, 3D modelleri henuz "
                     "degistiremiyoruz.")
        return "Once listeden gorseli olan (2D texture'lu) bir dosya sec."

    def save_adjust(self):
        if not self.current_frame_paths:
            messagebox.showinfo("Kaydet", self._no_texture_message())
            return
        alpha_pct = self.alpha_var.get()
        hue_deg = self.hue_var.get()
        if alpha_pct == 100 and hue_deg == 0:
            messagebox.showinfo("Kaydet", "Seffaflik/renk degeri degistirilmedi (100% / 0 derece) -- kaydedilecek bir sey yok.")
            return
        ok = messagebox.askyesno(
            "Emin misin?",
            f"{len(self.current_frame_paths)} dosya UZERINE YAZILACAK "
            f"(seffaflik={alpha_pct}%, hue={hue_deg} derece).\n\n"
            "Ilk kez kaydedince yaninda '.orijinal_yedek' uzantili bir "
            "yedek kopya birakilacak, istersen oradan geri donebilirsin.\n\n"
            "Devam edilsin mi?")
        if not ok:
            return
        done, failed = 0, []
        for p in self.current_frame_paths:
            try:
                save_adjusted_texture(p, alpha_pct, hue_deg)
                done += 1
            except Exception as e:
                failed.append(f"{os.path.basename(p)}: {e}")
        msg = f"{done} dosya kaydedildi."
        if failed:
            msg += "\n\nBasarisiz olanlar:\n" + "\n".join(failed)
        messagebox.showinfo("Kaydet", msg)
        # kaydedilen sonucu diskten tekrar okuyup goster (gercek sonucu dogrula)
        self.reset_adjust(redraw=False)
        self.start_anim(self.current_frame_paths)

    def mark_as_source(self):
        if not self.current_frame_paths:
            messagebox.showinfo("Isaretle", self._no_texture_message())
            return
        self.marked_source_paths = list(self.current_frame_paths)
        self.marked_source_rel = self.current_rel_path
        self.marked_label_var.set(f"Kaynak: {self.current_rel_path}")

    def apply_swap(self):
        if not self.marked_source_paths:
            messagebox.showinfo("Degistir", "Once bir efekti 'Begendim -> Kaynak Olarak Isaretle' ile isaretle.")
            return
        if not self.current_frame_paths:
            messagebox.showinfo("Degistir", self._no_texture_message())
            return
        if self.current_frame_paths == self.marked_source_paths:
            messagebox.showinfo("Degistir", "Kaynak ile hedef ayni efekt gibi gorunuyor, iptal edildi.")
            return
        ok = messagebox.askyesno(
            "Emin misin?",
            f"'{self.current_rel_path}' efektinin gorseli, "
            f"'{self.marked_source_rel}' efektinin gorseliyle DEGISTIRILECEK.\n\n"
            f"{len(self.current_frame_paths)} dosya uzerine yazilacak "
            "(ilk kez kaydedince '.orijinal_yedek' yedegi birakilir).\n\n"
            "Devam edilsin mi?")
        if not ok:
            return
        try:
            source_images = [load_texture_any(p) for p in self.marked_source_paths]
        except Exception:
            messagebox.showerror("Hata", "Kaynak gorseller okunamadi:\n" + traceback.format_exc())
            return
        done, failed = replace_effect_texture(self.current_frame_paths, source_images)
        msg = f"{done} dosya degistirildi."
        if failed:
            msg += "\n\nBasarisiz olanlar:\n" + "\n".join(failed)
        messagebox.showinfo("Degistir", msg)
        self.start_anim(self.current_frame_paths)

    def _tick_anim(self):
        if not self.anim_frames:
            return
        self.canvas.delete("all")
        img = self.anim_frames[self.anim_index % len(self.anim_frames)]
        self.canvas.create_image(170, 170, image=img)
        self.anim_index += 1
        self.anim_job = self.after(150, self._tick_anim)

    def stop_anim(self):
        if self.anim_job:
            self.after_cancel(self.anim_job)
            self.anim_job = None
        self.anim_frames = []

    def open_design_dialog(self, initial_chosen_path=None):
        DesignEffectDialog(self, initial_chosen_path=initial_chosen_path)

    def open_gallery(self):
        if not self.fx_root:
            messagebox.showinfo("Galeri", "Once 'Klasor Sec (fx)' ile fx klasorunu sec.")
            return
        EffectGalleryDialog(self)

    def open_3d_design_dialog(self):
        Design3DMeshDialog(self)


class Design3DMeshDialog(tk.Toplevel):
    """'3D Model Tasarla' penceresi -- DENEYSEL. 2D 'Yeni Efekt Tasarla'
    ozelliginin 3D karsiligi: sifirdan (kod/matematik ile) basit bir 3D model
    (kristal, dikenli yildiz, sivri parca kumesi, dusuk-poligon kure) uretip
    onizler. Kaydetme: YENI bir .n3shape yazmiyor (o formatin bircok alani
    hala cozulmedi, riskli olur) -- bunun yerine ana pencerede su an secili
    olan (onizlemesi basarili gosterilen) bir 3D mesh efektinin .n3pmesh
    GEOMETRISININ UZERINE yazar, boylece o efektin .n3shape'i (ve butun
    bilinmeyen alanlari) hic degismeden kalir -- ayni 2D texture swap
    ozelligimizle ayni guvenlik mantigi."""

    STYLES = [
        ("Kristal / Mucevher", "crystal"),
        ("Yildiz (dikenli kure)", "star3d"),
        ("Sivri parca kumesi", "shard"),
        ("Dusuk-poligon kure", "orb"),
    ]

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("3D Model Tasarla (deneysel)")
        self.geometry("400x560")
        self.variant = 0
        self.verts = None
        self.faces = None
        self.anim_frames = []
        self.anim_index = 0
        self.anim_job = None

        ttk.Label(self, text="DENEYSEL -- prosedurel/matematiksel olarak sifirdan\n"
                              "3D model uretiliyor (AI resim modeli DEGIL). Bu\n"
                              "programin KENDI onizlemesinde her zaman dogru gorunur,\n"
                              "ama GERCEK OYUNDA calisacagi GARANTI DEGIL -- bazi\n"
                              ".n3pmesh format alanlarinin anlami hala cozulmedi.",
                  justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(10, 6))

        form = ttk.Frame(self)
        form.pack(fill=tk.X, padx=10)
        ttk.Label(form, text="Sekil:").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.style_var = tk.StringVar(value=self.STYLES[0][1])
        self.style_combo = ttk.Combobox(form, values=[s[0] for s in self.STYLES], state='readonly', width=22)
        self.style_combo.current(0)
        self.style_combo.grid(row=0, column=1, pady=4)
        self.style_combo.bind('<<ComboboxSelected>>', lambda e: self._on_style_change(self.style_combo.current()))

        ttk.Label(form, text="Boyut:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.size_var = tk.DoubleVar(value=1.0)
        ttk.Spinbox(form, from_=0.2, to=5.0, increment=0.1, textvariable=self.size_var, width=8).grid(row=1, column=1, sticky=tk.W, pady=4)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=(8, 4))
        ttk.Button(btns, text="Onizle", command=self.render_preview).pack(side=tk.LEFT)
        ttk.Button(btns, text="Farkli Varyant", command=self.new_variant).pack(side=tk.LEFT, padx=6)

        self.canvas = tk.Canvas(self, width=300, height=300, bg="#222222")
        self.canvas.pack(pady=10)

        self.target_label_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.target_label_var, wraplength=380, justify=tk.LEFT).pack(padx=10)

        ttk.Button(self, text="Bu Mesh'in Uzerine Yaz (secili 3D efekt)...",
                   command=self.save_to_selected_mesh).pack(pady=(6, 10))
        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=380, justify=tk.LEFT).pack(padx=10, pady=(0, 10))

        self._update_target_label()
        self.render_preview()

    def _update_target_label(self):
        mesh_rel = getattr(self.parent, 'current_mesh_rel', None)
        if mesh_rel:
            self.target_label_var.set(f"Hedef (ana pencerede secili): {mesh_rel}")
        else:
            self.target_label_var.set(
                "Hedef yok -- ana penceredeki listeden onizlemesi basarili "
                "gosterilen bir 3D mesh efekti sec, sonra buraya don.")

    def _on_style_change(self, idx):
        self.style_var.set(self.STYLES[idx][1])
        self.render_preview()

    def new_variant(self):
        self.variant += 1
        self.render_preview()

    def render_preview(self):
        style = self.style_var.get()
        size = self.size_var.get()
        try:
            self.verts, self.faces = generate_mesh(style, size=size, variant=self.variant)
            frames = render_mesh_frames(self.verts, self.faces, n_frames=16, size=300)
        except Exception:
            messagebox.showerror("Hata", traceback.format_exc())
            return
        self._stop_anim()
        self.anim_frames = [ImageTk.PhotoImage(im) for im in frames]
        self.anim_index = 0
        self._tick()

    def _tick(self):
        if not self.anim_frames:
            return
        self.canvas.delete("all")
        img = self.anim_frames[self.anim_index % len(self.anim_frames)]
        self.canvas.create_image(150, 150, image=img)
        self.anim_index += 1
        self.anim_job = self.after(150, self._tick)

    def _stop_anim(self):
        if self.anim_job:
            self.after_cancel(self.anim_job)
            self.anim_job = None
        self.anim_frames = []

    def save_to_selected_mesh(self):
        if self.verts is None or self.faces is None:
            return
        self._update_target_label()
        mesh_path = getattr(self.parent, 'current_mesh_path', None)
        mesh_rel = getattr(self.parent, 'current_mesh_rel', None)
        if not mesh_path:
            messagebox.showinfo(
                "Hedef yok",
                "Once ana penceredeki listeden onizlemesi basarili gosterilen "
                "bir 3D mesh (.n3shape) efekti sec, sonra bu pencereye geri don.")
            return
        ok = messagebox.askyesno(
            "Emin misin?",
            f"'{mesh_rel}' dosyasinin GEOMETRISI, burada tasarladigin yeni "
            "sekille UZERINE YAZILACAK.\n\n"
            "Ilk kez kaydedince yaninda '.orijinal_yedek' uzantili bir yedek "
            "kopya birakilacak.\n\n"
            "UYARI: Bu DENEYSEL bir ozellik. Format bazi alanlari (footer/"
            "materyal blogu) tam cozulmedigi icin bu efektin GERCEK OYUNDA "
            "dogru gorunecegi/yuklenecegi garanti degil -- sadece bu "
            "PROGRAMDAKI onizleme kesinlikle dogru calisir. Bir seyler "
            "ters giderse yedekten geri donebilirsin.\n\n"
            "Devam edilsin mi?")
        if not ok:
            return
        try:
            backup_path = mesh_path + '.orijinal_yedek'
            if not os.path.exists(backup_path):
                with open(backup_path, 'wb') as f:
                    f.write(open(mesh_path, 'rb').read())
            write_n3pmesh(mesh_path, self.verts, self.faces, name=os.path.splitext(os.path.basename(mesh_path))[0])
        except Exception:
            messagebox.showerror("Hata", traceback.format_exc())
            return
        self.status_var.set(f"Kaydedildi: {mesh_rel}  (yedek: {os.path.basename(backup_path)})")
        messagebox.showinfo(
            "Kaydedildi",
            f"'{mesh_rel}' dosyasinin mesh'i degistirildi.\n\n"
            "Ana penceredeki efekti tekrar secip onizlemeyi yenileyebilirsin. "
            "Oyunda test etmeden once yedegin durdugundan emin ol.")


class EffectGalleryDialog(tk.Toplevel):
    """Klasordeki BIRCOK efekti kucuk onizleme (thumbnail) halinde YAN YANA
    gosteren bir galeri. Bir alan/skil efektini degistirmek isterken tek tek
    tiklayip gozden gecirmek yerine, once burada gorsel olarak goz gezdirip
    begenilen efekti secmek icin var. Secilen efekt iki sekilde kullanilabilir:
      - "Kaynak Olarak Isaretle": ana penceredeki swap ozelligiyle ayni
        (baska bir efektin uzerine bunun gorselini yazmak icin).
      - "Tasarla'da Baz Al": Yeni Efekt Tasarla penceresini ACIP, sablon
        modunda dogrudan BU efektin dokusunu baz alarak acar."""

    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.title("Efekt Galerisi")
        self.geometry("720x560")
        self.selected_rel = None
        self.selected_thumb_path = None  # secilen efektin ilk frame'inin TAM yolu (varsa)
        self._thumb_imgs = []  # ImageTk referanslarini canli tutmak icin

        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=8, pady=8)
        ttk.Label(top, text="Filtrele:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(value=self.parent.filter_var.get())
        ttk.Entry(top, textvariable=self.filter_var, width=30).pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Yenile", command=self.reload_thumbnails).pack(side=tk.LEFT)
        self.status_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.status_var).pack(side=tk.LEFT, padx=10)

        # kaydirilabilir (scrollable) grid alani
        outer = ttk.Frame(self)
        outer.pack(fill=tk.BOTH, expand=True, padx=8)
        self.canvas = tk.Canvas(outer, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.grid_frame = ttk.Frame(self.canvas)
        self._grid_window = self.canvas.create_window((0, 0), window=self.grid_frame, anchor=tk.NW)
        self.grid_frame.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfig(self._grid_window, width=e.width))

        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=8, pady=8)
        self.selected_label_var = tk.StringVar(value="(henuz secim yok -- bir thumbnail'e tikla)")
        ttk.Label(bottom, textvariable=self.selected_label_var, wraplength=460, justify=tk.LEFT).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(bottom, text="Kaynak Olarak Isaretle", command=self.mark_selected_as_source).pack(side=tk.LEFT, padx=4)
        ttk.Button(bottom, text="Tasarla'da Baz Al", command=self.use_selected_in_design).pack(side=tk.LEFT, padx=4)

        self.reload_thumbnails()

    def reload_thumbnails(self):
        for child in list(self.grid_frame.children.values()):
            child.destroy()
        self._thumb_imgs = []
        filt = self.filter_var.get().lower()
        candidates = [f for f in self.parent.all_files if filt in f.lower()]
        MAX_SHOWN = 60
        shown = candidates[:MAX_SHOWN]
        if len(candidates) > MAX_SHOWN:
            self.status_var.set(f"{len(shown)}/{len(candidates)} gosteriliyor (filtrele daralt)")
        else:
            self.status_var.set(f"{len(shown)} efekt")
        cols = 6
        row = col = 0
        found_any = False
        for rel in shown:
            thumb = self.parent.get_effect_thumbnail(rel, size=80)
            if thumb is None:
                continue  # gorseli cikartilamayan efektler galeride sessizce atlanir
            found_any = True
            tkimg = ImageTk.PhotoImage(thumb)
            self._thumb_imgs.append(tkimg)
            cell = ttk.Frame(self.grid_frame)
            cell.grid(row=row, column=col, padx=4, pady=4)
            btn = tk.Button(cell, image=tkimg, relief=tk.FLAT,
                             command=lambda r=rel: self.on_pick(r))
            btn.pack()
            name = os.path.basename(rel)
            if len(name) > 16:
                name = name[:14] + '...'
            ttk.Label(cell, text=name, wraplength=90, justify=tk.CENTER).pack()
            col += 1
            if col >= cols:
                col = 0
                row += 1
        if not found_any:
            ttk.Label(self.grid_frame, text="(gorsel cikartilabilen efekt bulunamadi -- filtreyi degistirmeyi dene)").grid(row=0, column=0, padx=10, pady=10)

    def on_pick(self, rel):
        self.selected_rel = rel
        full = os.path.join(self.parent.fx_root, rel)
        try:
            refs = extract_texture_refs(full)
        except Exception:
            refs = []
        thumb_path = None
        for tex_path, numtex in refs:
            frames = resolve_texture_frames(self.parent.fx_root, tex_path, numtex)
            if frames:
                thumb_path = frames[0]
                break
        self.selected_thumb_path = thumb_path
        self.selected_label_var.set(f"Secili: {rel}")

    def mark_selected_as_source(self):
        if not self.selected_rel:
            messagebox.showinfo("Isaretle", "Once bir efekt sec (thumbnail'e tikla).")
            return
        full = os.path.join(self.parent.fx_root, self.selected_rel)
        try:
            refs = extract_texture_refs(full)
        except Exception as e:
            messagebox.showerror("Hata", f"Efekt okunamadi: {e}")
            return
        frames = []
        for tex_path, numtex in refs:
            frames = resolve_texture_frames(self.parent.fx_root, tex_path, numtex)
            if frames:
                break
        if not frames:
            messagebox.showinfo("Isaretle", "Bu efektin 2D dokusu bulunamadi (muhtemelen sadece 3D mesh) -- kaynak olarak isaretlenemez.")
            return
        self.parent.marked_source_paths = frames
        self.parent.marked_source_rel = self.selected_rel
        self.parent.marked_label_var.set(f"Kaynak: {self.selected_rel}")
        messagebox.showinfo("Isaretlendi", f"'{self.selected_rel}' kaynak olarak isaretlendi.\n\nAna pencerede degistirmek istedigin efekti sec, sonra 'Bu Efekti Isaretliyle Degistir' butonuna bas.")

    def use_selected_in_design(self):
        if not self.selected_thumb_path:
            messagebox.showinfo(
                "Tasarla'da Baz Al",
                "Bu efekt icin kullanilabilir bir 2D doku bulunamadi (muhtemelen sadece 3D mesh) "
                "-- Tasarla penceresi sadece 2D dokulardan ilham alabiliyor.")
            return
        self.parent.open_design_dialog(initial_chosen_path=self.selected_thumb_path)


class DesignEffectDialog(tk.Toplevel):
    """'Yeni Efekt Tasarla' penceresi. NOT: burada gercek bir AI resim
    modeli YOK. Iki mod var:
      - Prosedurel: goruntu sifirdan matematiksel (radyal gradyan, parlama,
        kivilcim) olarak kod ile tasarlanir.
      - Klasorden ilham al (onerilen): fx klasorunde zaten var olan GERCEK
        bir doku baz alinir, renk/seffaflik degistirilir ve istege bagli
        ekstra bir parlama/kivilcim katmani eklenir -- sonuc gercek oyun
        sanatina daha yakin duruyor.
    Her iki modda da sonuc gercek Knight Online .dxt (DXT3) formatina
    kodlanip kaydediliyor."""

    STYLES = [
        ("Patlama / parlama", "burst"),
        ("Kivilcim kumesi", "sparkle"),
        ("Halka / aura", "ring"),
        ("Alev", "flame"),
        ("Buz / Kristal", "ice"),
    ]
    EXTRA_STYLES = [
        ("(yok)", "none"),
        ("+ Patlama/parlama", "burst"),
        ("+ Kivilcim", "sparkle"),
        ("+ Halka", "ring"),
        ("+ Alev", "flame"),
        ("+ Buz/Kristal", "ice"),
    ]

    def __init__(self, parent, initial_chosen_path=None):
        super().__init__(parent)
        self.parent = parent
        self.title("Yeni Efekt Tasarla")
        self.geometry("440x760")
        self.current_image = None
        self.variant = 0
        self.sample_list = None
        self.last_source_rel = None
        self.second_texture_var = None
        # Efekt Galerisi'nden "Tasarla'da Baz Al" ile acildiysa, ilk onizleme
        # rastgele bir doku yerine DOGRUDAN bu dosyayi baz alsin -- sadece
        # ilk render'da kullanilir, sonrasinda (Farkli Varyant vs.) normal
        # rastgele akisa doner.
        self.forced_chosen_path = initial_chosen_path

        ttk.Label(self, text="Bu bir AI resim modeli DEGIL -- goruntu kod ile\n"
                              "tasarlaniyor (ya sifirdan matematiksel, ya da\n"
                              "klasordeki gercek bir dokudan turetilerek), sonra\n"
                              "oyunun .dxt formatina cevriliyor.",
                  justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(10, 6))

        mode_frame = ttk.Frame(self)
        mode_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.mode_var = tk.StringVar(value='template')
        ttk.Radiobutton(mode_frame, text="Klasorden ilham al (onerilen)", value='template',
                        variable=self.mode_var, command=self._on_mode_change).pack(anchor=tk.W)
        ttk.Radiobutton(mode_frame, text="Prosedurel (sifirdan)", value='procedural',
                        variable=self.mode_var, command=self._on_mode_change).pack(anchor=tk.W)

        form = ttk.Frame(self)
        form.pack(fill=tk.X, padx=10)

        self.style_label = ttk.Label(form, text="Stil:")
        self.style_label.grid(row=0, column=0, sticky=tk.W, pady=4)
        self.style_var = tk.StringVar(value=self.STYLES[0][1])
        self.style_combo = ttk.Combobox(form, values=[s[0] for s in self.STYLES], state='readonly', width=22)
        self.style_combo.current(0)
        self.style_combo.grid(row=0, column=1, pady=4)
        self.style_combo.bind('<<ComboboxSelected>>', lambda e: self._on_style_change(self.style_combo.current()))

        self.extra_label = ttk.Label(form, text="Ekstra katman:")
        self.extra_label.grid(row=1, column=0, sticky=tk.W, pady=4)
        self.extra_var = tk.StringVar(value=self.EXTRA_STYLES[2][1])
        self.extra_combo = ttk.Combobox(form, values=[s[0] for s in self.EXTRA_STYLES], state='readonly', width=22)
        self.extra_combo.current(2)
        self.extra_combo.grid(row=1, column=1, pady=4)
        self.extra_combo.bind('<<ComboboxSelected>>', lambda e: self._on_extra_change(self.extra_combo.current()))

        ttk.Label(form, text="Renk (hue, 0-360):").grid(row=2, column=0, sticky=tk.W, pady=4)
        self.hue_var = tk.IntVar(value=30)
        ttk.Spinbox(form, from_=0, to=360, textvariable=self.hue_var, width=8).grid(row=2, column=1, sticky=tk.W, pady=4)

        ttk.Label(form, text="Boyut (piksel):").grid(row=3, column=0, sticky=tk.W, pady=4)
        self.size_var = tk.IntVar(value=128)
        size_combo = ttk.Combobox(form, values=[64, 128, 256], textvariable=self.size_var, state='readonly', width=8)
        size_combo.grid(row=3, column=1, sticky=tk.W, pady=4)

        self.strength_label = ttk.Label(form, text="Yogunluk (ekstra katman) %:")
        self.strength_label.grid(row=4, column=0, sticky=tk.W, pady=4)
        self.strength_var = tk.IntVar(value=55)
        self.strength_spin = ttk.Spinbox(form, from_=0, to=100, textvariable=self.strength_var, width=8,
                                          command=self.render_preview)
        self.strength_spin.grid(row=4, column=1, sticky=tk.W, pady=4)

        self.second_texture_var = tk.BooleanVar(value=True)
        self.second_tex_check = ttk.Checkbutton(
            form, text="Ikinci gercek dokuyu da harmanla (daha zengin sonuc)",
            variable=self.second_texture_var, command=self._on_second_tex_toggle)
        self.second_tex_check.grid(row=5, column=0, columnspan=2, sticky=tk.W, pady=4)

        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, padx=10, pady=(8, 4))
        ttk.Button(btns, text="Onizle", command=self.render_preview).pack(side=tk.LEFT)
        ttk.Button(btns, text="Farkli Varyant / Farkli Doku", command=self.new_variant).pack(side=tk.LEFT, padx=6)

        self.canvas = tk.Canvas(self, width=280, height=280, bg="#222222")
        self.canvas.pack(pady=10)

        self.source_label_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.source_label_var, wraplength=400, justify=tk.LEFT).pack(padx=10)

        ttk.Button(self, text="Farkli Kaydet (.dxt olarak)...", command=self.save_as_dxt).pack(pady=(6, 4))
        ttk.Button(self, text="Ana listede secili olan efektin uzerine kaydet",
                   command=self.save_to_selected_effect).pack(pady=(0, 10))

        self._on_mode_change()
        self.render_preview()

    def _on_second_tex_toggle(self):
        self.render_preview()

    def _on_mode_change(self):
        is_template = self.mode_var.get() == 'template'
        self.style_label.config(text="Ekstra katman:" if is_template else "Stil:")
        # sablon modunda "Stil" secimi yerine sadece extra katman kullanilir
        self.style_combo.grid_remove() if is_template else self.style_combo.grid()
        self.extra_label.grid() if is_template else self.extra_label.grid_remove()
        self.extra_combo.grid() if is_template else self.extra_combo.grid_remove()
        # yogunluk ve ikinci-doku secenekleri sadece "klasorden ilham al" modunda anlamli
        self.strength_label.grid() if is_template else self.strength_label.grid_remove()
        self.strength_spin.grid() if is_template else self.strength_spin.grid_remove()
        self.second_tex_check.grid() if is_template else self.second_tex_check.grid_remove()
        self.render_preview()

    def _on_style_change(self, idx):
        self.style_var.set(self.STYLES[idx][1])
        self.render_preview()

    def _on_extra_change(self, idx):
        self.extra_var.set(self.EXTRA_STYLES[idx][1])
        self.render_preview()

    def new_variant(self):
        self.variant += 1
        self.render_preview()

    def render_preview(self):
        hue = self.hue_var.get()
        size = self.size_var.get()
        mode = self.mode_var.get()
        try:
            if mode == 'template':
                if not self.parent.fx_root:
                    messagebox.showinfo("Klasor gerekli", "Once ana pencereden 'Klasor Sec (fx)' ile fx klasorunu sec.")
                    self.mode_var.set('procedural')
                    self._on_mode_change()
                    return
                if self.sample_list is None:
                    self.sample_list = list_sample_textures(self.parent.fx_root)
                extra = self.extra_var.get()
                strength = self.strength_var.get()
                use_second = bool(self.second_texture_var.get())
                chosen = self.forced_chosen_path
                im, src = derive_from_template(
                    self.parent.fx_root, hue, size=size, variant=self.variant,
                    extra_style=extra, extra_strength=strength,
                    use_second_texture=use_second, sample_list=self.sample_list,
                    chosen_path=chosen)
                self.forced_chosen_path = None  # sadece ilk onizlemede zorla, sonra normal rastgele akis
                self.last_source_rel = src
                self.source_label_var.set(f"Baz alinan gercek doku(lar): {src}")
            else:
                style = self.style_var.get()
                im = generate_effect_texture(style, hue, size=size, variant=self.variant)
                self.last_source_rel = None
                self.source_label_var.set("")
        except Exception:
            messagebox.showerror("Hata", traceback.format_exc())
            return
        self.current_image = im
        disp = im.copy()
        disp.thumbnail((280, 280))
        self._tkimg = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(140, 140, image=self._tkimg)

    def save_as_dxt(self):
        if self.current_image is None:
            return
        initial_dir = self.parent.fx_root if self.parent.fx_root else None
        path = filedialog.asksaveasfilename(
            title="Yeni efekt dokusunu kaydet",
            defaultextension=".dxt",
            filetypes=[("Knight Online texture", "*.dxt")],
            initialdir=initial_dir,
            initialfile="yeni_efekt0000.dxt",
        )
        if not path:
            return
        try:
            write_ko_dxt(path, self.current_image, name="")
        except Exception:
            messagebox.showerror("Hata", traceback.format_exc())
            return
        messagebox.showinfo(
            "Kaydedildi",
            f"{os.path.basename(path)} yazildi.\n\n"
            "Bunu bir efektte kullanmak icin: bir .n3fxpart dosyasinda "
            "<texture> satirini bu dosyayi gosterecek sekilde duzenlemen "
            "(ya da yeni bir .n3fxpart/.fxb olusturman) gerekir -- frame "
            "numarasi (orn. 0000) dosya adinin sonunda olmali.")

    def save_to_selected_effect(self):
        """Tasarlanan gorseli, ANA PENCEREDE su an listede secili olan
        efektin dosyalarinin UZERINE yazar (Farkli Kaydet gibi yeni bir
        dosya olusturmak yerine, dogrudan o efektin orijinal dosyalarini
        degistirir). Ana pencerede zaten var olan swap altyapisini
        (replace_effect_texture) kullanir, yani ilk kez yazarken otomatik
        '.orijinal_yedek' yedegi birakilir."""
        if self.current_image is None:
            return
        target_paths = getattr(self.parent, 'current_frame_paths', None)
        target_rel = getattr(self.parent, 'current_rel_path', None)
        if not target_paths:
            messagebox.showinfo(
                "Secili efekt yok",
                "Once ana penceredeki listeden UZERINE YAZILACAK efekti sec "
                "(gorseli goruntulenebilen bir dosya olmali).")
            return
        ok = messagebox.askyesno(
            "Emin misin?",
            f"'{target_rel}' efektinin {len(target_paths)} dosyasi, "
            "burada tasarladigin YENI gorselle UZERINE YAZILACAK.\n\n"
            "Ilk kez kaydedince yaninda '.orijinal_yedek' uzantili bir "
            "yedek kopya birakilacak, istersen oradan geri donebilirsin.\n\n"
            "Devam edilsin mi?")
        if not ok:
            return
        done, failed = replace_effect_texture(target_paths, [self.current_image])
        msg = f"{done} dosya '{target_rel}' uzerine kaydedildi."
        if failed:
            msg += "\n\nBasarisiz olanlar:\n" + "\n".join(failed)
        messagebox.showinfo("Kaydedildi", msg)
        # ana penceredeki animasyonu, diskten yeniden okuyarak tazele
        if hasattr(self.parent, 'start_anim'):
            self.parent.stop_anim()
            self.parent.start_anim(target_paths)


if __name__ == '__main__':
    app = FxbViewer()
    app.mainloop()
