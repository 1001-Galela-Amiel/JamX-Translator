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
    # Requires api key via sign-up - currently using my own (Brent)
    print("switched to deepl")
    api_key = "BTM1s6VGuuy6NJi"
    translator = deepl.Translator(api_key)
    result = translator.translate_text(
        text,
        source_lang=source_lang.upper(),
        target_lang=target_lang.upper()
    )
    return result.text

def translate_text(src_lang, dst_lang, text, translator="Google Translate"):
    """
    Provides a cached translation helper between two language codes.
    It looks up the text in an in memory cache and only calls google_translate when there is no cached result, storing the new translation afterward.
    """
    print("current translator is: " + translator)
    key = f"{src_lang}|{dst_lang}|{text}"
    if key in _translation_cache:
        return _translation_cache[key]
    if translator == "Google Translate":
        print("using google translate")
        result = google_translate(text, src_lang, dst_lang)
    elif translator == "DeepL":
        print("using deepl")
        result = deepl_translate(text, src_lang, dst_lang)
    else:
        print("there was an error")
        raise ValueError(f"Unknown translator: {translator}")
    _translation_cache[key] = result
    return result
