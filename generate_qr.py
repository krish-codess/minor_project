"""
generate_qr.py
Generates QR codes for each parking slot and an overview sheet.
Usage:  python generate_qr.py [base_url]
Default base_url: http://localhost:5000
"""

import sys
import os

try:
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Installing dependencies...")
    os.system("pip install qrcode[pil] pillow --break-system-packages -q")
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000"
OUT_DIR  = "qr_codes"
os.makedirs(OUT_DIR, exist_ok=True)

NUM_SLOTS = 10

print(f"Generating QR codes → {OUT_DIR}/  (base: {BASE_URL})")

qr_images = []

for slot_id in range(1, NUM_SLOTS + 1):
    # ── Entry QR ──
    entry_url = f"{BASE_URL}/scan/entry/{slot_id}"
    qr = qrcode.QRCode(version=1, box_size=8, border=3,
                       error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(entry_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    # Add label
    W, H = img.size
    canvas = Image.new("RGB", (W, H + 50), "white")
    canvas.paste(img, (0, 0))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except Exception:
        font = ImageFont.load_default()

    label = f"SLOT P{slot_id} — ENTRY"
    bbox  = draw.textbbox((0, 0), label, font=font)
    tw    = bbox[2] - bbox[0]
    draw.text(((W - tw) // 2, H + 12), label, fill="black", font=font)

    path = os.path.join(OUT_DIR, f"slot_{slot_id:02d}_entry.png")
    canvas.save(path)
    qr_images.append((canvas, f"P{slot_id}"))
    print(f"  ✓ slot_{slot_id:02d}_entry.png → {entry_url}")

# ── Overview sheet ──
COLS = 5
ROWS = (NUM_SLOTS + COLS - 1) // COLS
W0, H0 = qr_images[0][0].size
PAD = 20
sheet_w = COLS * W0 + (COLS + 1) * PAD
sheet_h = ROWS * H0 + (ROWS + 1) * PAD + 60

sheet = Image.new("RGB", (sheet_w, sheet_h), "#f5f5f5")
draw  = ImageDraw.Draw(sheet)

try:
    title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
except Exception:
    title_font = ImageFont.load_default()

title = "RFID Parking System — Slot QR Codes"
tb = draw.textbbox((0,0), title, font=title_font)
draw.text(((sheet_w - (tb[2]-tb[0]))//2, 14), title, fill="#1a1d2e", font=title_font)

for i, (img, _) in enumerate(qr_images):
    row = i // COLS
    col = i %  COLS
    x = PAD + col * (W0 + PAD)
    y = 60 + PAD + row * (H0 + PAD)
    sheet.paste(img, (x, y))

sheet_path = os.path.join(OUT_DIR, "ALL_SLOTS_OVERVIEW.png")
sheet.save(sheet_path)
print(f"\n✅ Overview sheet → {sheet_path}")
print("Print these and place them at each physical parking slot.")
