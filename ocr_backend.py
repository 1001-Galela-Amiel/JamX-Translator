import os
import time
import numpy as np
from PIL import Image
from image_preprocessor import removeBackground

from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR

import json

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Associated language map for each language in conjunction with rapidOCR
LANG_TO_LANGREC = {
    # Chinese
    "zh-CN": LangRec.CH,
    "zh-TW": LangRec.CHINESE_CHT,

    # CJK
    "ja":    LangRec.JAPAN,
    "ko":    LangRec.KOREAN,

    # English
    "en":    LangRec.EN,

    # Latin script
    "fr":    LangRec.LATIN,
    "es":    LangRec.LATIN,
    "de":    LangRec.LATIN,
    "it":    LangRec.LATIN,
    "pt":    LangRec.LATIN,
    "nl":    LangRec.LATIN,
    "pl":    LangRec.LATIN,
    "ro":    LangRec.LATIN,
    "sv":    LangRec.LATIN,
    "da":    LangRec.LATIN,
    "no":    LangRec.LATIN,
    "fi":    LangRec.LATIN,
    "cs":    LangRec.LATIN,
    "sk":    LangRec.LATIN,
    "sl":    LangRec.LATIN,
    "hr":    LangRec.LATIN,
    "hu":    LangRec.LATIN,
    "et":    LangRec.LATIN,
    "lv":    LangRec.LATIN,
    "lt":    LangRec.LATIN,
    "sq":    LangRec.LATIN,
    "af":    LangRec.LATIN,
    "sw":    LangRec.LATIN,
    "ms":    LangRec.LATIN,
    "tl":    LangRec.LATIN,
    "id":    LangRec.LATIN,
    "vi":    LangRec.LATIN,
    "tr":    LangRec.LATIN,
    "mt":    LangRec.LATIN,
    "la":    LangRec.LATIN,

    # Cyrillic
    "ru":    LangRec.CYRILLIC,
    "uk":    LangRec.CYRILLIC,
    "bg":    LangRec.CYRILLIC,

    # Arabic script
    "ar":    LangRec.ARABIC,
    "ur":    LangRec.ARABIC,
    "fa":    LangRec.ARABIC,

    # Devanagari
    "hi":    LangRec.DEVANAGARI,
    "mr":    LangRec.DEVANAGARI,
    "ne":    LangRec.DEVANAGARI,

    # Other
    "ta":    LangRec.TA,
    "te":    LangRec.TE,
    "th":    LangRec.TH,
    "el":    LangRec.EL,
}

_last_debug_save = 0.0

# Newest version (v5) for OCR, but only supports CH and EN
PPOCRV5_LANGS = {LangRec.CH, LangRec.EN}

# Cache of created RapidOCR model engines
engine_cache: dict[str, RapidOCR] = {}

def get_ocr_engine(lang_code: str) -> RapidOCR:
    if lang_code in engine_cache:
        return engine_cache[lang_code]
    
    lang_rec = LANG_TO_LANGREC.get(lang_code, LangRec.CH)

    # Use v5 if CH or EN. Else, use v4
    ocr_version = OCRVersion.PPOCRV5 if lang_rec in PPOCRV5_LANGS else OCRVersion.PPOCRV4
    engine = RapidOCR(
        params={
            "Rec.lang_type": lang_rec,
            "Rec.model_type": ModelType.MOBILE,
            "Rec.ocr_version": ocr_version,
        }
    )

    engine_cache[lang_code] = engine
    return engine

def ocr_image_data(pil_image, prefer_lang_code="auto", enable_preprocessing=False):
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
    """If preprocessing enabled, apply to ocr image"""
    if(enable_preprocessing == True):
        img = removeBackground(img)
    
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

    engine = get_ocr_engine(prefer_lang_code)
    try:
        result = engine(img_bgr)
    except Exception as e:
        print(f"Engine error: {e}")

    entries = []
    processed_img = Image.fromarray(img_bgr)

    if result is None or result.txts is None:
        return entries
    
    for bbox, text, score in zip(result.boxes, result.txts, result.scores):
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
            "lang": prefer_lang_code,
        })

    return entries, processed_img
