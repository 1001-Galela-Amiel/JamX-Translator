# Jam-X Translator
JamX is a software tool designed to let users translate computer games, allowing in-game text to be edited from one language to another and shared with others.

## Key Feature: 
Supports 30+ languages, helping make games accessible to players regardless of language barriers. It also includes interactive font customization features and text-to-speech functionality to improve accessibility and user experience.

# Overview
This tool provides:
1. Real-Time Window Capture – Captures and processes frames directly from a selected game or application window
2. OCR-Based Text Detection – Extracts in-game text from captured content using OCR technology
3. In-Game Translation – Translates detected text into 30+ supported languages in real time
4. Memory Hooking Support – Retrieves in-game text directly from memory for improved extraction accuracy
5. Text-to-Speech (TTS) – Converts translated text into speech for accessibility and hands-free interaction
6. Interactive Display Features – Includes customizable fonts, overlays, and translation display settings for improved user experience

# Usage
1. Clone and setup
   ```bash
   git clone "copy and paste our code link here"
   cd Jam-X Translator
   python3 -m venv venv
   ```

3. Install all required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run the program:
   ```bash
   python main.py
   ```
5. Select or capture a game/application window.
6. Choose the source and target language.
7. Start OCR and translation to view translated in-game text in real time.
8. Optionally enable text-to-speech (TTS) for audio output.
   
# Configuration
## .env file
```bash
Contains TTS(text to speech) API key
```

## Directory Structure
```text
Jam-X Translator/
├── README.md (this file)
├── app/
│   ├── main.py                       # JamX Translator Software
│   ├── capture.py                    # captures screen for ocr processing 
│   ├── image_processor.py             
│   ├── luna_helper32.py             # Memory Hooking
│   ├── lunaworker.py                 # Memory Hooking 
│   ├── memory_patch_worker.py                 
│   ├── ocr_backend.py                    # Ocr integration
│   ├── ocr_overlay.py                    
│   └── textspeech.py                      # Translated text speech
│   │── preprocessing.py
│   │── translate_backend.py           #Translator
│   │── translation_worker.py
│   │── snipper.py
│   └── website/
│       └── html/css/js files
└── requirements.txt
```
## Limitations
1.Some games may restrict memory hooking or window capture functionality.

2.Real-time translation speed may vary depending on system performance and internet connection.

3.Certain languages may produce less accurate translations than others.

4.Text overlapping or fast-moving subtitles can reduce detection accuracy.

5.Performance may decrease when running high-resource games alongside the application.

## Future Improvements
1. Add support for additional translation APIs and AI translation models.
2. Improve OCR accuracy for stylized and animated game text.
3. Enhance memory hooking compatibility across more games and platforms.
4. Add customizable overlay themes and UI personalization features.
5. Implement offline translation support.
6. Improve real-time processing speed and optimization.
7. Add user translation sharing and community translation features.

# Contributors
[Johnnie](https://github.com/johnniemorrisSC)

[Mary-Ann](https://github.com/1107-Mary-Ann-Affo)

[Brent](https://github.com/1001-Galela-Amiel)

# Project Website
[JamX Translator](https://1107-mary-ann-affo.github.io/jamxweb-translator/index.html)
