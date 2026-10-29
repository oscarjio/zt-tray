"""
Statusikon: en Ethernet-port (RJ45) på en rundad ruta.

Rutans färg = läget på valt nätverkskort:
    grön    statisk IP
    blå     DHCP med adress
    orange  DHCP men ingen server svarade (169.254.x.x) — typiskt vid kabel direkt mot PLC
    grå     ingen kabel / länk nere
    mörkgrå inget kort valt

Lila märke nere till höger med "Z" = ZeroTier-plats ansluten (VPN).
Under ett IP-byte visas tre punkter i stället för porten.

När ett uttag delas byts ikonen:
    orange + paus  delningen är startad men länken är inte uppe
    grön + bock    någon är inkopplad (nät, USB eller COM)
"""

from PIL import Image, ImageDraw

COLORS = {
    "static": (22, 163, 74),
    "dhcp": (37, 99, 235),
    "apipa": (217, 119, 6),
    "down": (107, 114, 128),
    "none": (107, 114, 128),
    "unknown": (55, 65, 81),
}
VPN_COLOR = (124, 58, 237)
SHARE_WAIT = (224, 96, 24)
SHARE_LIVE = (8, 150, 96)
S = 256  # ritas stort och skalas ner för mjuka kanter
_WHITE = (255, 255, 255, 255)

_cache = {}


def _lighten(c, f):
    return tuple(int(v + (255 - v) * f) for v in c)


def _darken(c, f):
    return tuple(int(v * (1 - f)) for v in c)


def _tile(base):
    """Rundad ruta med samma gradient som den vanliga ikonen."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bot = _lighten(base, 0.18), _darken(base, 0.12)
    for y in range(S):
        t = y / (S - 1)
        gd.line([(0, y), (S, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)) + (255,))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((6, 6, S - 6, S - 6), radius=58, fill=255)
    img.paste(grad, (0, 0), mask)
    return img


def _stroke(d, a, b, width, fill):
    d.line([a, b], fill=fill, width=width)
    r = width / 2
    d.ellipse((a[0] - r, a[1] - r, a[0] + r, a[1] + r), fill=fill)
    d.ellipse((b[0] - r, b[1] - r, b[0] + r, b[1] + r), fill=fill)


def _glyph_net(d, hole):
    L, R, T, B = 58, 188, 62, 148
    d.rounded_rectangle((L, T, R, B), radius=12, fill=_WHITE)
    d.rectangle((96, B - 12, 150, 176), fill=_WHITE)
    n, pw = 4, 14
    span = (R - L) - 48
    for i in range(n):
        x = L + 24 + i * (span - pw) / (n - 1)
        d.rounded_rectangle((x, T + 14, x + pw, T + 52), radius=3, fill=hole)


def _glyph_usb(d, hole):
    cx, cy = 118, 118
    _stroke(d, (cx, cy + 58), (cx, cy - 8), 18, _WHITE)
    d.polygon([(cx, cy - 62), (cx - 26, cy - 6), (cx + 26, cy - 6)], fill=_WHITE)
    _stroke(d, (cx, cy + 6), (cx - 48, cy - 36), 16, _WHITE)
    _stroke(d, (cx, cy + 6), (cx + 48, cy - 36), 16, _WHITE)
    d.ellipse((cx - 70, cy - 58, cx - 30, cy - 18), fill=_WHITE)
    d.ellipse((cx - 58, cy - 46, cx - 42, cy - 30), fill=hole)
    d.rounded_rectangle((cx + 28, cy - 58, cx + 68, cy - 18), radius=4, fill=_WHITE)
    d.rounded_rectangle((cx + 40, cy - 46, cx + 56, cy - 30), radius=2, fill=hole)


def _glyph_com(d, hole):
    x, y, w, h = 52, 78, 132, 96
    r = h / 2
    d.rectangle((x, y, x + w - r, y + h), fill=_WHITE)
    d.ellipse((x + w - h, y, x + w, y + h), fill=_WHITE)
    for i, px in enumerate((78, 102, 126, 150)):
        d.ellipse((px, 100, px + 16, 116), fill=hole)
        if i < 3:
            d.ellipse((px + 12, 132, px + 28, 148), fill=hole)


def _share_badge(d, base, live):
    cx, cy, r = 186, 186, 58
    d.ellipse((cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10), fill=_darken(base, 0.28) + (255,))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_WHITE)
    ink = _darken(base, 0.05) + (255,)
    if live:
        d.line([(cx - 26, cy + 2), (cx - 8, cy + 22), (cx + 28, cy - 20)],
               fill=ink, width=14, joint="curve")
    else:
        d.rounded_rectangle((cx - 22, cy - 18, cx - 8, cy + 18), radius=4, fill=ink)
        d.rounded_rectangle((cx + 8, cy - 18, cx + 22, cy + 18), radius=4, fill=ink)


def _share_image(kind, live, busy=False):
    base = SHARE_LIVE if live else SHARE_WAIT
    img = _tile(base)
    d = ImageDraw.Draw(img)
    if busy:
        for cx in (78, 128, 178):
            d.ellipse((cx - 17, 128 - 17, cx + 17, 128 + 17), fill=_WHITE)
        return img
    hole = _darken(base, 0.08) + (255,)
    {"net": _glyph_net, "usb": _glyph_usb, "com": _glyph_com}[kind](d, hole)
    _share_badge(d, base, live)
    return img


def render(mode="unknown", vpn=False, busy=False, size=64, share="", live=False):
    share = share if share in ("net", "usb", "com") else ""
    key = (mode, vpn, busy, size, share, bool(live))
    if key in _cache:
        return _cache[key]

    if share:
        img = _share_image(share, bool(live), busy=bool(busy))
        out = img.resize((size, size), Image.LANCZOS)
        _cache[key] = out
        return out

    base = COLORS.get(mode, COLORS["unknown"])
    img = _tile(base)
    d = ImageDraw.Draw(img)
    white = _WHITE

    if busy:
        for cx in (78, 128, 178):
            d.ellipse((cx - 17, 128 - 17, cx + 17, 128 + 17), fill=white)
    else:
        # RJ45-port: kropp + flik nedtill
        L, R, T, B = 52, 204, 58, 160
        TL, TR, TB = 96, 160, 196
        d.rounded_rectangle((L, T, R, B), radius=14, fill=white)
        d.rectangle((TL, B - 14, TR, TB), fill=white)
        # Kontaktstift (urfrästa i rutans färg)
        pin_col = _darken(base, 0.05) + (255,)
        n, pw = 4, 16
        span = (R - L) - 56
        for i in range(n):
            x = L + 28 + i * (span - pw) / (n - 1)
            d.rounded_rectangle((x, T + 16, x + pw, T + 62), radius=4, fill=pin_col)
        if mode == "down":
            # Snedstreck = ingen länk
            d.line([(44, 212), (212, 44)], fill=_darken(base, 0.45) + (255,), width=22)
            d.line([(44, 212), (212, 44)], fill=white, width=10)

    if vpn:
        cx, cy, r = 186, 186, 64
        d.ellipse((cx - r - 12, cy - r - 12, cx + r + 12, cy + r + 12), fill=white)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=VPN_COLOR + (255,))
        z = [(cx - 28, cy - 28), (cx + 28, cy - 28), (cx - 28, cy + 28), (cx + 28, cy + 28)]
        d.line(z, fill=white, width=17, joint="curve")

    out = img.resize((size, size), Image.LANCZOS)
    _cache[key] = out
    return out


def save_ico(path, mode="dhcp"):
    big = render(mode, size=256)
    big.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    import sys
    save_ico(sys.argv[1] if len(sys.argv) > 1 else "app.ico")
