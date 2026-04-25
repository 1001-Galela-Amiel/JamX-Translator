import requests
import html
import urllib.parse
import deepl

GOOGLE_TRANSLATE_URL = "https://translate.google.com/m"
_translation_cache = {}

LANG_MAP = {
    "auto": "Auto",
    #"zh": "zh-CN",
    #"en": "en",
    #"ja": "ja","""
    "af": "Afrikaans",
    "ar": "Arabic",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fa": "Persian",
    "fi": "Finnish",
    "fr": "French",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "jw": "Javanese",
    "km": "Khmer",
    "kn": "Kannada",
    "ko": "Korean",
    "la": "Latin",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "ml": "Malayalam",
    "mr": "Marathi",
    "ms": "Malay",
    "mt": "Maltese",
    "ne": "Nepali",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "sq": "Albanian",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "zh-CN": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "zu": "Zulu",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}

def google_translate(text, src="auto", dst="en"):
    """
    Sends the given text to the Google Translate mobile web endpoint and parses the response.
    It builds a query URL, fetches the HTML, extracts the translated string from specific tags, and safely falls back when parsing fails.
    """
    if not text.strip():
        return ""

    src = src.lower()
    dst = dst.lower()
    
    params = {"sl": src, "tl": dst, "q": text}
    url = GOOGLE_TRANSLATE_URL + "?" + urllib.parse.urlencode(params)

    r = requests.get(url, headers=headers, timeout=5)
    if r.status_code != 200:
        return text

    content = r.text
    try:
        start = content.index('result-container">') + 18
        end = content.index("<", start)
        return html.unescape(content[start:end])
    except:
        return text

# Uses deepl library to send some text of x language to deepL and translate to y language
def deepl_translate(text, source_lang="auto", target_lang="en"):
    
    # DeepL requires api key via sign-up - currently using my own (Brent)
    # Since free version anyways (and for a translator), not too much of a risk to keep out (I think) - making secret seems too much of a hassle for use-case
    # Try not to use DeepL too much if possible, lest I have to make another account
    api_key = "f563ad68-c166-4e2d-b532-9dfc5cd2df97:fx"
    # Language map for DeepL specificially, based on given options w/ Google Translate
    lang_map = {
        "EN": "EN-US",  # Since EN not normally accepted (specify american or british(?))
        "PT": "PT-BR",  # One for portuguese too (though not used)
        "NO": "NB", # Norwegian
        "ZH-CN": "ZH-HANS", # Simplified chinese
        "ZH-TW": "ZH_HANT", # Traditional Chinese
    }
    # Normalize language codes to uppercase
    source = source_lang.upper()
    target = target_lang.upper()
    # Alter language based on above lang_map, if needed
    source = lang_map.get(source, source)
    # Since deepL has no auto for target language, defaulting to English
    if target == "AUTO":
        target = "EN-US"
    else:
        target = lang_map.get(target, target)

    translator = deepl.Translator(api_key)
    # Try statement since not all languages supported by Google Translate supported by DeepL
    try:
        result = translator.translate_text(
            text,
            source_lang=None if source_lang.lower() == "auto" else source_lang.upper(),
            target_lang=target.upper()
        )
        # Print statements left in commented out if you want to check the difference between DeepL and Google Translate
        # print(f"DeepL returned: {result.text}")
        # result2 = google_translate(text, source_lang, target_lang)
        # print("Google Translate would return: " + result2)
        return result.text
    
    # Default to Google Translate if above doesn't work
    except Exception as e:
        print(f"DeepL error: {e}")
        result = google_translate(text, source_lang, target_lang)
        print(f"Google fallback returned: {result}")
        return result

def translate_text(src_lang, dst_lang, text, translator="Google Translate", translation_cache=None):
    """
    Provides a cached translation helper between two language codes.
    It looks up the text in an in memory cache and only calls google_translate when there is no cached result, storing the new translation afterward.
    """
    key = f"{src_lang}|{dst_lang}|{text}"
    if translation_cache and translation_cache.get(key):
        return translation_cache[key]

    if translator == "Google Translate":
        result = google_translate(text, src_lang, dst_lang)
    elif translator == "DeepL":
        # print(f"using deepl | src: {src_lang} | dst: {dst_lang} | text: {text}")
        result = deepl_translate(text, src_lang, dst_lang)
        # print(result)
    else:
        result = google_translate(text, src_lang, dst_lang)
        raise ValueError(f"Unknown translator: {translator}")
    return result
