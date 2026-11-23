"""Blog artwork, drawn with Pillow: the Recruence mark (the robot on an orange tile) and article covers in the blog's
house style (near-black, an orange floor glow and grid, a monospace category label, a bold white title, and a diagram
of glowing outlined cards joined by connectors around a central block on a pedestal). Each cover's diagram is about its
article, never a stock picture. Used by build.py."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent
ORANGE = (255, 107, 0)
ORANGE_HI = (255, 150, 70)
S = 2  # drawn at twice the size, then scaled down for smooth edges
W, H = 1600, 900


def font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for name in names:
        for folder in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu"), Path("/System/Library/Fonts/Supplemental"),
                       Path("/Library/Fonts")):
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size)


BOLD = ["segoeuib.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]
SEMI = ["seguisb.ttf", "segoeuisb.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"]
MONO = ["consolab.ttf", "consola.ttf", "DejaVuSansMono-Bold.ttf", "Menlo.ttc"]


def robot_mask(size: int) -> Image.Image:
    """The robot's silhouette (from assets/robot.png) as an alpha mask, keeping its face and tie as holes."""
    robot = Image.open(HERE / "assets" / "robot.png").convert("RGBA").resize((size, size), Image.LANCZOS)
    alpha = robot.getchannel("A")
    # The white visor and tie become holes, so the glyph reads as a silhouette with a face.
    r, g, b, _ = robot.split()
    light = ImageChops.multiply(ImageChops.multiply(r.point(lambda v: 255 if v > 200 else 0), g.point(lambda v: 255 if v > 200 else 0)),
                                b.point(lambda v: 255 if v > 200 else 0))
    return ImageChops.subtract(alpha, light)


def mark(size: int) -> Image.Image:
    """The Recruence mark for the blog: a white robot on an orange rounded tile."""
    big = size * 4
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    d.rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.24), fill=ORANGE + (255,))
    glyph = robot_mask(int(big * 0.74))
    white = Image.new("RGBA", glyph.size, (255, 255, 255, 255))
    off = (big - glyph.size[0]) // 2
    tile.paste(white, (off, off + int(big * 0.02)), glyph)
    return tile.resize((size, size), Image.LANCZOS)


# ---------- drawing helpers (all coordinates at the final 1600x900 size) ----------

class Canvas:
    def __init__(self):
        self.img = Image.new("RGB", (W * S, H * S), (9, 9, 11))
        self.glow = Image.new("RGB", (W * S, H * S), (0, 0, 0))  # added on top, blurred: the neon look

    def background(self):
        d = ImageDraw.Draw(self.img)
        for y in range(0, H * S, 4):  # a slight vertical shade
            t = y / (H * S)
            d.line([(0, y), (W * S, y)], fill=(int(9 + 6 * t), int(9 + 4 * t), int(11 + 2 * t)), width=4)
        floor = Image.new("RGB", (W * S, H * S), (0, 0, 0))
        f = ImageDraw.Draw(floor)
        f.ellipse((560 * S, 640 * S, 1700 * S, 1180 * S), fill=(150, 55, 0))
        f.ellipse((200 * S, 760 * S, 1100 * S, 1150 * S), fill=(70, 25, 0))
        self.img = ImageChops.add(self.img, floor.filter(ImageFilter.GaussianBlur(120 * S)))
        grid = Image.new("L", (W * S, H * S), 0)
        g = ImageDraw.Draw(grid)
        for x in range(700, W, 56):
            g.line([(x * S, 0), (x * S, H * S)], fill=34, width=S)
        for y in range(0, 620, 56):
            g.line([(700 * S, y * S), (W * S, y * S)], fill=34, width=S)
        fade = Image.linear_gradient("L").resize((W * S, H * S)).point(lambda v: max(0, 255 - v * 2))
        grid = ImageChops.multiply(grid, fade)
        self.img.paste((60, 42, 30), (0, 0), grid)

    def text(self, xy, text, fnt, fill):
        ImageDraw.Draw(self.img).text((xy[0] * S, xy[1] * S), text, font=fnt, fill=fill)

    def card(self, box, radius=18, fill=(22, 17, 14), outline=ORANGE, width=3, glow=True):
        x0, y0, x1, y1 = (v * S for v in box)
        ImageDraw.Draw(self.img).rounded_rectangle((x0, y0, x1, y1), radius=radius * S, fill=fill, outline=outline, width=width * S)
        if glow:
            ImageDraw.Draw(self.glow).rounded_rectangle((x0, y0, x1, y1), radius=radius * S, outline=(255, 110, 20), width=6 * S)

    def line(self, pts, width=3, dot_ends=True):
        p = [(x * S, y * S) for x, y in pts]
        ImageDraw.Draw(self.img).line(p, fill=ORANGE_HI, width=width * S, joint="curve")
        ImageDraw.Draw(self.glow).line(p, fill=(255, 100, 10), width=(width + 4) * S)
        if dot_ends:
            for x, y in (p[0], p[-1]):
                r = 6 * S
                ImageDraw.Draw(self.img).ellipse((x - r, y - r, x + r, y + r), fill=(255, 190, 130))
                ImageDraw.Draw(self.glow).ellipse((x - 2 * r, y - 2 * r, x + 2 * r, y + 2 * r), fill=(255, 110, 20))

    def arrow(self, x0, y, x1):
        self.line([(x0, y), (x1, y)], dot_ends=False)
        d = ImageDraw.Draw(self.img)
        d.polygon([(x1 * S, (y - 10) * S), ((x1 + 16) * S, y * S), (x1 * S, (y + 10) * S)], fill=ORANGE_HI)

    def rows(self, x, y, widths, gap=22, width=6, color=(255, 140, 60)):
        d = ImageDraw.Draw(self.img)
        for i, w in enumerate(widths):
            yy = (y + i * gap) * S
            d.rounded_rectangle((x * S, yy, (x + w) * S, yy + width * S), radius=3 * S, fill=color)

    def bullets(self, x, y, widths, gap=26):
        d = ImageDraw.Draw(self.img)
        for i, w in enumerate(widths):
            cy = y + i * gap
            d.ellipse(((x) * S, (cy - 5) * S, (x + 10) * S, (cy + 5) * S), fill=ORANGE_HI)
            d.rounded_rectangle(((x + 22) * S, (cy - 3) * S, (x + 22 + w) * S, (cy + 3) * S), radius=3 * S, fill=(255, 140, 60))

    def person(self, cx, cy, r=16):
        d = ImageDraw.Draw(self.img)
        d.ellipse(((cx - r) * S, (cy - r - 8) * S, (cx + r) * S, (cy + r - 8) * S), fill=(255, 140, 60))
        d.pieslice(((cx - r * 1.9) * S, (cy + r - 2) * S, (cx + r * 1.9) * S, (cy + r * 4.4) * S), 180, 360, fill=(255, 140, 60))

    def check(self, cx, cy, r=22):
        d = ImageDraw.Draw(self.img)
        d.ellipse(((cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S), fill=ORANGE)
        ImageDraw.Draw(self.glow).ellipse(((cx - r - 8) * S, (cy - r - 8) * S, (cx + r + 8) * S, (cy + r + 8) * S), fill=(255, 110, 20))
        d.line([((cx - 10) * S, cy * S), ((cx - 3) * S, (cy + 8) * S), ((cx + 11) * S, (cy - 8) * S)], fill=(255, 255, 255), width=5 * S, joint="curve")

    def wave(self, x, y, heights, step=14):
        d = ImageDraw.Draw(self.img)
        for i, h in enumerate(heights):
            xx = (x + i * step) * S
            d.rounded_rectangle((xx, (y - h / 2) * S, xx + 6 * S, (y + h / 2) * S), radius=3 * S, fill=(255, 140, 60))

    def pedestal(self, cx, cy, w=230):
        d, g = ImageDraw.Draw(self.img), ImageDraw.Draw(self.glow)
        for i, (rw, rh) in enumerate(((w, 46), (w * 0.8, 34), (w * 0.62, 24))):
            box = ((cx - rw / 2) * S, (cy - rh / 2 + i * 6) * S, (cx + rw / 2) * S, (cy + rh / 2 + i * 6) * S)
            d.ellipse(box, outline=(255, 120, 30), width=3 * S)
            g.ellipse(box, outline=(255, 110, 20), width=10 * S)
        g.ellipse(((cx - w * 0.7) * S, (cy - 30) * S, (cx + w * 0.7) * S, (cy + 60) * S), fill=(120, 45, 0))

    def chip(self, box, label: str):
        """The central block: a dark rounded cube with the robot glyph in orange and a label."""
        self.card(box, radius=26, fill=(28, 20, 15), width=4)
        x0, y0, x1, y1 = box
        size = int((x1 - x0) * 0.46)
        glyph = robot_mask(size * S)
        colour = Image.new("RGB", glyph.size, ORANGE)
        self.img.paste(colour, (int((x0 + x1) / 2 * S - glyph.size[0] / 2), int((y0 + 22) * S)), glyph)
        self.glow.paste((180, 70, 0), (int((x0 + x1) / 2 * S - glyph.size[0] / 2), int((y0 + 22) * S)), glyph)
        f = font(BOLD, 34 * S)
        d = ImageDraw.Draw(self.img)
        tw = d.textlength(label, font=f)
        d.text(((x0 + x1) / 2 * S - tw / 2, (y1 - 62) * S), label, font=f, fill=(255, 255, 255))

    def finish(self) -> Image.Image:
        glow = self.glow.filter(ImageFilter.GaussianBlur(14 * S))
        img = ImageChops.add(self.img, ImageChops.multiply(glow, Image.new("RGB", glow.size, (200, 200, 200))))
        return img.resize((W, H), Image.LANCZOS)


# ---------- the diagrams, one per article ----------

def diagram_interviewer(c: Canvas):
    """Approved questions released one at a time into the AI interviewer, out comes a report."""
    for i, (y, label) in enumerate(((210, "Q1"), (345, "Q2"), (480, "Q3"))):
        c.card((850, y, 1030, y + 100))
        c.text((872, y + 20), label, font(MONO, 22 * S), (255, 160, 90))
        c.rows(872, y + 58, [120, 80], gap=18)
        c.line([(1030, y + 50), (1080, y + 50), (1080, 410), (1110, 410)], dot_ends=i == 1)
    c.pedestal(1200, 545)
    c.chip((1110, 320, 1290, 520), "AI")
    c.arrow(1290, 420, 1350)
    c.card((1370, 290, 1540, 560))
    c.rows(1395, 330, [100, 120, 80, 110, 70], gap=26)
    c.check(1505, 525)


def diagram_scorecard(c: Canvas):
    """A transcript with one quoted line highlighted, checked into a scorecard with ratings and a score."""
    c.card((840, 230, 1120, 560))
    c.text((866, 252), "TRANSCRIPT", font(MONO, 20 * S), (255, 160, 90))
    c.rows(866, 300, [210, 180, 200], gap=24, color=(120, 70, 40))
    d = ImageDraw.Draw(c.img)
    d.rounded_rectangle((858 * S, 368 * S, 1102 * S, 404 * S), radius=8 * S, fill=(70, 32, 8), outline=ORANGE, width=2 * S)
    c.text((870, 368), "\u201c", font(BOLD, 34 * S), (255, 170, 100))
    c.rows(894, 383, [180], gap=0)
    c.rows(866, 430, [190, 150, 205, 120], gap=24, color=(120, 70, 40))
    c.arrow(1120, 395, 1170)
    c.pedestal(1360, 585, w=300)
    c.card((1190, 250, 1530, 560), radius=22, fill=(28, 20, 15), width=4)
    c.text((1216, 272), "SCORECARD", font(MONO, 20 * S), (255, 160, 90))
    for row, filled in enumerate((5, 4, 4)):
        y = 330 + row * 56
        c.rows(1216, y, [90], gap=0, color=(200, 120, 70))
        for k in range(5):
            x = 1340 + k * 30
            fill = ORANGE if k < filled else (60, 40, 30)
            d.ellipse((x * S, (y - 8) * S, (x + 18) * S, (y + 10) * S), fill=fill)
    c.text((1216, 482), "87", font(BOLD, 52 * S), (255, 255, 255))
    c.text((1290, 505), "/100", font(MONO, 22 * S), (255, 160, 90))
    c.check(1490, 510)


def diagram_meeting(c: Canvas):
    """Three meeting tiles (Meet, Zoom, Teams) wired to the AI interviewer, which streams speech back."""
    for i, y in enumerate((200, 330, 460)):
        c.card((840, y, 1010, y + 105))
        c.person(925, y + 50, r=15)
        c.line([(1010, y + 52), (1060, y + 52), (1060, 395), (1100, 395)], dot_ends=i == 1)
    c.pedestal(1195, 525)
    c.chip((1100, 300, 1290, 500), "LIVE")
    c.arrow(1290, 400, 1345)
    c.card((1365, 300, 1540, 500))
    c.wave(1388, 360, [18, 34, 52, 30, 60, 40, 22, 46, 28, 14])
    c.rows(1388, 420, [120, 90], gap=22)


DIAGRAMS = {"ai-interviewer": diagram_interviewer, "evidence-based-interview-scorecard": diagram_scorecard,
            "ai-interview-google-meet-zoom-teams": diagram_meeting}


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, width: int) -> list[str]:
    lines, line = [], ""
    for w in text.split():
        trial = (line + " " + w).strip()
        if draw.textlength(trial, font=fnt) > width * S and line:
            lines.append(line)
            line = w
        else:
            line = trial
    return lines + [line]


def robot_render(size: int) -> Image.Image:
    """The robot as a dark, glossy object lit from below by orange: a matte body, an orange rim, a glowing face."""
    src = Image.open(HERE / "assets" / "robot.png").convert("RGBA").resize((size, size), Image.LANCZOS)
    alpha = src.getchannel("A")
    r, g, b, _ = src.split()
    face = ImageChops.multiply(ImageChops.multiply(r.point(lambda v: 255 if v > 200 else 0), g.point(lambda v: 255 if v > 200 else 0)),
                               b.point(lambda v: 255 if v > 200 else 0))
    face = ImageChops.multiply(face, alpha)
    body = ImageChops.subtract(alpha, face)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    # Body: dark charcoal, a little lighter at the top (a soft key light from above).
    shade = Image.linear_gradient("L").resize((size, size)).point(lambda v: 255 - v)
    dark = Image.merge("RGB", [shade.point(lambda v: 14 + v * 34 // 255), shade.point(lambda v: 12 + v * 28 // 255), shade.point(lambda v: 11 + v * 24 // 255)])
    out.paste(dark, (0, 0), body)
    # Orange rim light: the silhouette's edge, strongest at the bottom where the glow comes from.
    inner = alpha.filter(ImageFilter.MinFilter(max(3, size // 60 * 2 + 1)))
    edge = ImageChops.subtract(alpha, inner).filter(ImageFilter.GaussianBlur(size / 220))
    below = Image.linear_gradient("L").resize((size, size)).point(lambda v: 90 + v * 165 // 255)
    edge = ImageChops.multiply(edge, below)
    out.paste((255, 128, 40), (0, 0), edge)
    # The face and tie glow orange, as if lit from inside.
    out.paste((255, 132, 44), (0, 0), face)
    # A soft highlight on the upper left of the head.
    spec = Image.new("L", (size, size), 0)
    ImageDraw.Draw(spec).ellipse((size * 0.22, size * 0.12, size * 0.5, size * 0.3), fill=70)
    spec = ImageChops.multiply(spec.filter(ImageFilter.GaussianBlur(size / 30)), body)
    out.paste((255, 255, 255), (0, 0), spec)
    return out, face


def meetstream_mark(size: int) -> Image.Image:
    """MeetStream's square mark, cut from its public blog logo (scratch copy passed in MEETSTREAM_LOGO)."""
    import os
    logo = Image.open(os.environ["MEETSTREAM_LOGO"]).convert("RGBA")
    alpha = logo.getchannel("A")
    # The mark is the first solid shape from the left: stop at the first fully transparent column after it.
    cols = [any(alpha.getpixel((x, y)) > 40 for y in range(logo.size[1])) for x in range(logo.size[0])]
    start = cols.index(True)
    end = start + cols[start:].index(False)
    piece = logo.crop((start, 0, end, logo.size[1]))
    piece = piece.crop(piece.getchannel("A").getbbox())
    side = max(piece.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(piece, ((side - piece.size[0]) // 2, (side - piece.size[1]) // 2), piece)
    return square.resize((size, size), Image.LANCZOS)


def _coeffs(pa, pb):
    # Solve the 8 perspective coefficients without numpy (Gaussian elimination).
    rows, rhs = [], []
    for (x, y), (u, v) in zip(pa, pb):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y]); rhs.append(v)
    n = 8
    a = [r[:] + [b] for r, b in zip(rows, rhs)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(a[r][i]))
        a[i], a[piv] = a[piv], a[i]
        for r in range(n):
            if r != i:
                f = a[r][i] / a[i][i]
                a[r] = [x - f * y for x, y in zip(a[r], a[i])]
    return [a[i][n] / a[i][i] for i in range(n)]


def tilt(img: Image.Image, facing: str, amount: float = 0.1) -> Image.Image:
    """A slight perspective turn: 'right' makes the left edge recede, 'left' the right edge."""
    w, h = img.size
    k = h * amount / 2
    if facing == "right":
        quad = [(0, k), (w, 0), (w, h), (0, h - k)]
    else:
        quad = [(0, 0), (w, k), (w, h - k), (0, h)]
    coeffs = _coeffs(quad, [(0, 0), (w, 0), (w, h), (0, h)])
    return img.transform((w, h), Image.PERSPECTIVE, coeffs, Image.BICUBIC)


def slab(w: int, h: int, depth: int, radius: int, draw_face, up: bool = False) -> Image.Image:
    """A rounded slab with visible thickness. The extrusion goes down-right (a card seen from slightly above-left)
    or, with up=True, up-right (a block whose top face shows). The face is top-lit charcoal with an orange edge;
    draw_face(draw, x, y) adds the content on the face. All sizes are already at drawing scale."""
    pad = depth + 8
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fx, fy = pad, pad + (depth if up else 0)
    for i in range(depth, 0, -1):
        dx, dy = (i * 0.75, -i) if up else (i * 0.6, i)
        shade = int(14 + 22 * (1 - i / depth))
        warm = (min(255, shade + (40 if not up else 10)), shade + (12 if not up else 8), shade + 4)
        d.rounded_rectangle((fx + dx, fy + dy, fx + dx + w, fy + dy + h), radius=radius, fill=warm + (255,))
    if up:  # the top face catches the light
        for i in range(depth, 0, -1):
            dx, dy = i * 0.75, -i
            d.rounded_rectangle((fx + dx, fy + dy, fx + dx + w, fy + dy + radius * 2), radius=radius, fill=(58 - i, 52 - i, 48 - i, 255))
    face = Image.linear_gradient("L").resize((w, h)).point(lambda v: 255 - v)
    face = Image.merge("RGB", [face.point(lambda v: 18 + v * 30 // 255), face.point(lambda v: 16 + v * 26 // 255), face.point(lambda v: 15 + v * 24 // 255)])
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    img.paste(face, (fx, fy), mask)
    d.rounded_rectangle((fx, fy, fx + w, fy + h), radius=radius, outline=(255, 118, 26, 255), width=max(2, radius // 8))
    d.arc((fx + 3, fy + 3, fx + radius * 2, fy + radius * 2), 180, 270, fill=(255, 190, 140, 255), width=max(2, radius // 10))
    d.line([(fx + radius, fy + 3), (fx + w * 0.55, fy + 3)], fill=(255, 190, 140, 255), width=max(2, radius // 10))
    draw_face(d, fx, fy)
    return img


def cylinder(c, cx: int, top: int, rx: int, ry: int, height: int) -> None:
    """A two-tier metallic pedestal: dark brushed steel, light catching the rims, an orange rim light at the base
    and a warm pool of light on the floor around it."""
    floor = Image.new("RGB", c.img.size, (0, 0, 0))
    ImageDraw.Draw(floor).ellipse(((cx - rx * 2.0) * S, (top + height + 10) * S, (cx + rx * 2.0) * S, (top + height + 150) * S), fill=(150, 56, 0))
    c.img = ImageChops.add(c.img, floor.filter(ImageFilter.GaussianBlur(46 * S)))
    d, g = ImageDraw.Draw(c.img), ImageDraw.Draw(c.glow)
    tiers = ((rx * 1.18, ry * 1.18, top + height * 0.55, height * 0.75), (rx, ry, top, height * 0.6))
    for i, (trx, tryy, ttop, th) in enumerate(tiers):
        th = int(th)
        for y in range(th, -1, -1):  # the side: lit from the top-left, darker toward the bottom
            v = int(26 + 34 * (1 - y / max(1, th)))
            d.ellipse(((cx - trx) * S, (ttop + y - tryy) * S, (cx + trx) * S, (ttop + y + tryy) * S), fill=(v, v, v + 3))
        if i == 0:  # orange rim light along the base
            box = ((cx - trx) * S, (ttop + th - tryy) * S, (cx + trx) * S, (ttop + th + tryy) * S)
            d.arc(box, 10, 170, fill=(255, 128, 40), width=3 * S)
            g.arc(box, 10, 170, fill=(230, 90, 10), width=12 * S)
        top_box = ((cx - trx) * S, (ttop - tryy) * S, (cx + trx) * S, (ttop + tryy) * S)
        d.ellipse(top_box, fill=(44, 44, 48))
        d.ellipse(((cx - trx * 0.86) * S, (ttop - tryy * 0.86) * S, (cx + trx * 0.86) * S, (ttop + tryy * 0.86) * S), fill=(30, 30, 33), outline=(64, 64, 68), width=2 * S)
        d.arc(top_box, 190, 350, fill=(130, 130, 136), width=2 * S)
    g.ellipse(((cx - rx * 0.8) * S, (top - ry * 0.7) * S, (cx + rx * 0.8) * S, (top + ry * 0.7) * S), fill=(90, 34, 0))


def place(c, piece: Image.Image, x: int, y: int) -> None:
    """Paste a rendered 3D piece and let its orange edges glow."""
    c.img.paste(piece, (x * S, y * S), piece)
    r, gch, b, a = piece.split()
    hot = ImageChops.multiply(r.point(lambda v: 255 if v > 200 else 0), gch.point(lambda v: 255 if v < 170 else 0))
    hot = ImageChops.multiply(hot, a)
    glow = Image.new("RGB", c.glow.size, (0, 0, 0))
    glow.paste((200, 80, 0), (x * S, y * S), hot)
    c.glow = ImageChops.add(c.glow, glow)


def soft_background() -> Image.Image:
    """Near-black; a soft grey halo behind the subject, and a warm orange glow rising from the bottom edge."""
    img = Image.new("RGB", (W * S, H * S), (9, 9, 10))
    d = ImageDraw.Draw(img)
    for y in range(0, H * S, 4):
        v = int(9 + 5 * y / (H * S))
        d.line([(0, y), (W * S, y)], fill=(v, v, v), width=4)
    halo = Image.new("RGB", (W * S, H * S), (0, 0, 0))
    ImageDraw.Draw(halo).ellipse((1000 * S, 90 * S, 1480 * S, 560 * S), fill=(40, 40, 44))
    img = ImageChops.add(img, halo.filter(ImageFilter.GaussianBlur(60 * S)))
    wide = Image.new("RGB", (W * S, H * S), (0, 0, 0))
    ImageDraw.Draw(wide).ellipse((760 * S, 0, 1700 * S, 760 * S), fill=(16, 16, 18))
    img = ImageChops.add(img, wide.filter(ImageFilter.GaussianBlur(150 * S)))
    warm = Image.new("RGB", (W * S, H * S), (0, 0, 0))
    w = ImageDraw.Draw(warm)
    w.ellipse((60 * S, 800 * S, 1760 * S, 1260 * S), fill=(90, 32, 0))
    w.ellipse((300 * S, 1035 * S, 1640 * S, 1150 * S), fill=(255, 112, 12))
    return ImageChops.add(img, warm.filter(ImageFilter.GaussianBlur(58 * S)))


def tile(img: Image.Image, box, radius=26) -> None:
    """A matte dark 3D tile: soft drop shadow, a top-lit gradient face, a thin edge and a highlight along the top."""
    x0, y0, x1, y1 = (int(v * S) for v in box)
    w, h, r = x1 - x0, y1 - y0, radius * S
    shadow = Image.new("L", img.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle((x0 + 10 * S, y0 + 26 * S, x1 + 10 * S, y1 + 30 * S), radius=r, fill=170)
    img.paste((0, 0, 0), (0, 0), shadow.filter(ImageFilter.GaussianBlur(26 * S)))
    face = Image.linear_gradient("L").resize((w, h)).point(lambda v: 255 - v)
    face = Image.merge("RGB", [face.point(lambda v: 17 + v * 26 // 255), face.point(lambda v: 16 + v * 24 // 255), face.point(lambda v: 16 + v * 23 // 255)])
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=r, fill=255)
    img.paste(face, (x0, y0), mask)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0, y0, x1, y1), radius=r, outline=(54, 52, 50), width=2 * S)
    d.arc((x0 + 2 * S, y0 + 2 * S, x0 + 2 * r, y0 + 2 * r), 180, 270, fill=(92, 90, 88), width=2 * S)
    d.line([(x0 + r, y0 + 2 * S), (x1 - r, y0 + 2 * S)], fill=(92, 90, 88), width=2 * S)
    d.arc((x1 - 2 * r, y0 + 2 * S, x1 - 2 * S, y0 + 2 * r), 270, 360, fill=(92, 90, 88), width=2 * S)


def cover_object(post: dict, brand: str = "recruence") -> Image.Image:
    """The build-guide cover: copy on the left; on the right a candidate passes through Recruence (the robot) and
    comes out as an analysis score, as matte 3D tiles on charcoal with a soft haze and a little orange."""
    c = Canvas()
    c.img = soft_background()
    d = ImageDraw.Draw(c.img)

    # The pedestal, then the Recruence block on it (a cube whose top face shows).
    cylinder(c, 1180, 452, 150, 30, 34)

    def block_face(dd, fx, fy):
        glyph = robot_mask(96 * S)
        tile = Image.new("RGBA", glyph.size, (255, 112, 10, 255))
        dd.bitmap((fx + (200 * S - glyph.size[0]) // 2, fy + 22 * S), glyph, fill=(255, 112, 10, 255))
        f = font(BOLD, 30 * S)
        tw = dd.textlength("Recruence", font=f)
        dd.text((fx + (200 * S - tw) / 2, fy + 132 * S), "Recruence", font=f, fill=(255, 255, 255, 255))
    block = slab(200 * S, 186 * S, 30 * S, 30 * S, block_face, up=True)
    place(c, block, 1080 - 38, 236)

    # Input: the candidate's live call, as a card tilted toward the block.
    def call_face(dd, fx, fy):
        dd.rounded_rectangle((fx + 104 * S, fy + 12 * S, fx + 168 * S, fy + 40 * S), radius=14 * S, fill=(40, 24, 18, 255), outline=(255, 118, 26, 255), width=2 * S)
        dd.ellipse((fx + 114 * S, fy + 21 * S, fx + 124 * S, fy + 31 * S), fill=(255, 70, 50, 255))
        dd.text((fx + 130 * S, fy + 15 * S), "LIVE", font=font(SEMI, 15 * S), fill=(255, 255, 255, 255))
        for k in range(3):
            cx, cy = fx + (40 + k * 40) * S, fy + 70 * S
            dd.ellipse((cx - 11 * S, cy - 19 * S, cx + 11 * S, cy + 3 * S), fill=(255, 140, 60, 255))
            dd.pieslice((cx - 19 * S, cy + 6 * S, cx + 19 * S, cy + 46 * S), 180, 360, fill=(255, 140, 60, 255))
        for k, hgt in enumerate((10, 20, 30, 16, 26, 34, 20, 12, 26, 16)):
            x = fx + (22 + k * 14) * S
            dd.rounded_rectangle((x, fy + (118 - hgt / 2) * S, x + 6 * S, fy + (118 + hgt / 2) * S), radius=3 * S, fill=(255, 140, 60, 255))
    call = tilt(slab(180 * S, 140 * S, 16 * S, 22 * S, call_face), "right", 0.12)
    place(c, call, 820 - 24, 286)
    c.line([(1004, 368), (1072, 368)], width=3)

    # Outputs: score, report, check, as cards tilted toward the block.
    def out_face(kind):
        def draw(dd, fx, fy):
            sq = (fx + 14 * S, fy + 14 * S, fx + 66 * S, fy + 66 * S)
            dd.rounded_rectangle(sq, radius=12 * S, fill=(38, 26, 20, 255), outline=(255, 118, 26, 255), width=2 * S)
            if kind == "score":
                f = font(BOLD, 24 * S)
                tw = dd.textlength("87", font=f)
                dd.text(((sq[0] + sq[2]) / 2 - tw / 2, sq[1] + 11 * S), "87", font=f, fill=(255, 255, 255, 255))
            elif kind == "report":
                for k, w in enumerate((28, 22, 26)):
                    dd.rounded_rectangle((sq[0] + 12 * S, sq[1] + (15 + k * 10) * S, sq[0] + (12 + w) * S, sq[1] + (19 + k * 10) * S), radius=2 * S, fill=(255, 150, 70, 255))
            else:
                dd.ellipse((sq[0] + 10 * S, sq[1] + 10 * S, sq[2] - 10 * S, sq[3] - 10 * S), fill=(255, 112, 10, 255))
                dd.line([(sq[0] + 18 * S, sq[1] + 27 * S), (sq[0] + 24 * S, sq[1] + 33 * S), (sq[0] + 35 * S, sq[1] + 20 * S)], fill=(255, 255, 255, 255), width=4 * S, joint="curve")
            for k, w in enumerate((96, 70)):
                dd.rounded_rectangle((fx + 84 * S, fy + (28 + k * 20) * S, fx + (84 + w) * S, fy + (35 + k * 20) * S), radius=3 * S, fill=(255, 140, 60, 255))
        return draw
    for oy, kind in ((196, "score"), (306, "report"), (416, "check")):
        card = tilt(slab(200 * S, 80 * S, 12 * S, 16 * S, out_face(kind)), "left", 0.1)
        place(c, card, 1360 - 20, oy - 20)
        midy = oy + 40
        c.line([(1290, 368), (1320, 368), (1320, midy), (1356, midy)], width=3, dot_ends=False)
        d.ellipse((1350 * S, (midy - 6) * S, 1362 * S, (midy + 6) * S), fill=(255, 190, 130))

    # Copy, as on the reference covers.
    d = ImageDraw.Draw(c.img)
    if brand == "meetstream":  # MeetStream's own logo, as on its blog covers
        ms = meetstream_mark(44 * S)
        c.img.paste(ms, (80 * S, 70 * S), ms)
        c.text((136, 76), "meetstream.ai", font(SEMI, 31 * S), (255, 255, 255))
    else:
        m = mark(44 * S)
        c.img.paste(m, (80 * S, 70 * S), m)
        c.text((136, 76), "Recruence", font(SEMI, 31 * S), (255, 255, 255))
    c.text((84, 226), post["category"].upper(), font(MONO, 21 * S), (255, 122, 40))
    title_font = font(BOLD, 56 * S)
    y = 260
    for ln in wrap(d, post["cover_title"], title_font, 680)[:3]:
        c.text((80, y), ln, title_font, (255, 255, 255))
        y += 66
    sub_font = font(["segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf"], 24 * S)
    y += 14
    for ln in wrap(d, post["cover_subtitle"], sub_font, 640)[:3]:
        c.text((82, y), ln, sub_font, (168, 168, 176))
        y += 34
    chip_font = font(SEMI, 19 * S)
    x, y = 82, y + 24
    for label, dot in (("Google Meet", (52, 168, 83)), ("Zoom", (45, 140, 255)), ("Microsoft Teams", (98, 100, 167))):
        w = int(d.textlength(label, font=chip_font) / S) + 48
        d.rounded_rectangle((x * S, y * S, (x + w) * S, (y + 38) * S), radius=19 * S, fill=(26, 25, 25), outline=(50, 48, 47), width=2 * S)
        d.ellipse(((x + 15) * S, (y + 13) * S, (x + 26) * S, (y + 24) * S), fill=dot)
        c.text((x + 34, y + 7), label, chip_font, (228, 228, 232))
        x += w + 12
    foot = font(MONO, 19 * S)
    c.text((82, H - 70), "meetstream.ai/blog" if brand == "meetstream" else "recruence/blog", foot, (120, 120, 128))
    right = ([("from $0.35/hr", (255, 122, 40)), ("  ·  $5 free credit", (120, 120, 128))] if brand == "meetstream"
             else [("free & open source", (255, 122, 40)), ("  ·  MIT licensed", (120, 120, 128))])
    total = sum(d.textlength(s, font=foot) for s, _ in right) / S
    x = W - 80 - total
    for s, col in right:
        c.text((x, H - 70), s, foot, col)
        x += d.textlength(s, font=foot) / S
    return c.finish()


def cover(post: dict) -> Image.Image:
    if post.get("cover_style") == "object":
        return cover_object(post)
    c = Canvas()
    c.background()
    DIAGRAMS.get(post["slug"], diagram_interviewer)(c)
    m = mark(60 * S)
    c.img.paste(m, (96 * S, 96 * S), m)
    c.text((172, 104), "Recruence", font(SEMI, 40 * S), (255, 255, 255))
    c.text((100, 300), post["category"].upper(), font(MONO, 26 * S), (255, 122, 40))
    title_font = font(BOLD, 70 * S)
    d = ImageDraw.Draw(c.img)
    words, lines, line = post["cover_title"].split(), [], ""
    for w in words:
        trial = (line + " " + w).strip()
        if d.textlength(trial, font=title_font) > 640 * S and line:
            lines.append(line)
            line = w
        else:
            line = trial
    lines.append(line)
    y = 350
    for ln in lines[:4]:
        c.text((96, y), ln, title_font, (255, 255, 255))
        y += 84
    c.text((100, H - 112), "recruence / blog", font(MONO, 24 * S), (150, 150, 155))
    return c.finish()


def draw_all(posts: list[dict]) -> None:
    out = HERE / "covers"
    out.mkdir(exist_ok=True)
    for p in posts:
        img = cover(p)
        img.save(out / f"{p['slug']}.webp", "WEBP", quality=84, method=6)
        img.save(out / f"{p['slug']}.jpg", "JPEG", quality=86, optimize=True, progressive=True)
    for size, name in ((64, "mark-64.png"), (256, "mark.png")):
        mark(size).save(HERE / "assets" / name, optimize=True)
    print(f"covers: {len(posts)} drawn")
