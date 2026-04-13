import os
import time
import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_ocr_engine = RapidOCR()
_last_debug_save = 0.0


def ocr_image_data(pil_image, prefer_lang_code="auto"):
    """
    Runs OCR on the given image bytes and returns detected text entries.
    It decodes the bytes into an image, passes it through RapidOCR, converts polygon boxes into simple rectangles, and builds a list of text and bounding box dictionaries.
    """
    global _last_debug_save

    now = time.time()
    if now - _last_debug_save > 1.0:
        try:
            pil_image.save(os.path.join(LOG_DIR, "debug_frame.png"))
        except Exception:
            pass
        _last_debug_save = now

    img = np.array(pil_image)

    if img.ndim == 3 and img.shape[1] > 1600:
        scale = 1600.0 / img.shape[1]
        new_h = int(img.shape[0] * scale)
        import cv2
        img = cv2.resize(img, (1600, new_h), interpolation=cv2.INTER_LINEAR)

    if img.ndim == 3 and img.shape[2] == 3:
        import cv2
        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    else:
        img_bgr = img

    result, _ = _ocr_engine(img_bgr)
    entries = []

    if not result:
        return entries

    for item in result:
        try:
            bbox, text, score = item
        except Exception:
            continue

        if not text or not str(text).strip():
            continue

        try:
            xs = [pt[0] for pt in bbox]
            ys = [pt[1] for pt in bbox]
            x, y = int(min(xs)), int(min(ys))
            w, h = int(max(xs) - x), int(max(ys) - y)
        except Exception:
            continue

        if w < 5 or h < 5:
            continue

        entries.append({
            "text": str(text).strip(),
            "bbox": (x, y, w, h),
            "lang": "unknown",
        })

    return entries
