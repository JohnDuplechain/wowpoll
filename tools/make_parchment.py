"""Generate WowPoll's seamless background textures.

Built from FFT-filtered noise, which tiles perfectly with no seams.
Run:  python tools/make_parchment.py [--color R,G,B] [--out static/name.jpg] [--preview out.png]
  default:  warm brown parchment -> static/parchment.jpg
  blue:     python tools/make_parchment.py --color 12,112,140 --out static/texture-blue.jpg
Needs numpy + pillow (dev only; not needed by the app).
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


N = 1024                      # tile size in px (bigger = repeats less noticeably)
BASE = np.array([float(v) for v in arg("--color", "112,92,60").split(",")])
SEED = 11

rng = np.random.default_rng(SEED)
fy = np.fft.fftfreq(N)[:, None]
fx = np.fft.fftfreq(N)[None, :]
radius = np.sqrt(fx ** 2 + fy ** 2)
radius[0, 0] = np.inf


def norm(a):
    a = a - a.mean()
    return a / a.std()


def fractal(beta):
    """Periodic 1/f^beta noise: bigger beta = softer, larger blotches."""
    spec = np.fft.fft2(rng.normal(size=(N, N))) / radius ** (beta / 2)
    return norm(np.real(np.fft.ifft2(spec)))


def fibers():
    """Faint long horizontal-ish streaks, like pulp fibers."""
    filt = np.exp(-(fx / 0.004) ** 2 - (fy / 0.08) ** 2)
    filt[0, 0] = 0
    return norm(np.real(np.fft.ifft2(np.fft.fft2(rng.normal(size=(N, N))) * filt)))


mottle = fractal(3.6)        # very soft large clouds
mid = fractal(2.2)           # medium blotches
speckle = fractal(1.3)       # fine cloudy speckle (the "leathery" look)
grain = norm(rng.normal(size=(N, N)))

# Luminance variation: subtle, mostly fine-scale, like aged paper
lum = 0.030 * mottle + 0.030 * mid + 0.038 * speckle + 0.016 * grain

# Same hue everywhere (no green/red cast); only brightness varies
rgb = BASE[None, None, :] * (1 + lum[..., None])
img = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")

root = Path(__file__).resolve().parent.parent
out = root / arg("--out", "static/parchment.jpg")
img.save(out, quality=82, optimize=True, progressive=True)
print(f"wrote {out} ({out.stat().st_size // 1024} KB)")

if "--preview" in sys.argv:
    dest = sys.argv[sys.argv.index("--preview") + 1]
    W, H = 1600, 900
    canvas = Image.new("RGB", (W, H))
    for y in range(0, H, N):
        for x in range(0, W, N):
            canvas.paste(img, (x, y))
    canvas.save(dest)
    print(f"preview {dest}")
