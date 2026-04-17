from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
import tempfile
import threading
from queue import Queue
import os
import time
import miniaudio

load_dotenv()
'''Simple text-to-speech module using ElevenLabs API. It manages a queue of text to be spoken and processes it in a separate thread. T
he generated audio is played using miniaudio, and temporary files are cleaned up after use.'''
client = ElevenLabs(api_key=os.getenv("ELEVENLABS_API_KEY"))

speech_queue = Queue()
voice_enabled = False
speech_thread = None


def set_voice_enabled(enabled):
    global voice_enabled
    voice_enabled = enabled


def _should_speak(text):
    t = str(text or "").strip()
    if not t:
        return False
    if len(t) < 10:
        return False

    digit_count = sum(ch.isdigit() for ch in t)
    if len(t) > 0 and digit_count / len(t) > 0.30:
        return False

    if len(t.split()) < 3:
        return False

    return True


def speak(text):
    if voice_enabled and _should_speak(text):
        while not speech_queue.empty():
            try:
                speech_queue.get_nowait()
            except Exception:
                break
        print("Queued:", text)
        speech_queue.put(text)


def _play_mp3_file(path):
    info = miniaudio.get_file_info(path)
    stream = miniaudio.stream_file(path)
    device = miniaudio.PlaybackDevice()

    try:
        device.start(stream)
        time.sleep(float(info.duration) + 0.25)
    finally:
        try:
            device.close()
        except Exception:
            pass


def process_speech_queue():
    print("Speech worker started")

    while True:
        text = speech_queue.get()
        print("Dequeued:", text)

        if text is None:
            print("Speech worker stopping")
            break

        tmp_path = None

        try:
            print("Generating audio for:", text)

            audio = client.text_to_speech.convert(
                text=text,
                voice_id="JBFqnCBsd6RMkjVDRZzb",
                model_id="eleven_multilingual_v2",
                output_format="mp3_44100_128"
            )

            audio_bytes = b"".join(audio)

            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            print("Playing file:", tmp_path)
            _play_mp3_file(tmp_path)
            print("Finished:", text)

        except Exception as e:
            print(f"Error occurred while processing speech: {e}")

        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass


def start_speech_thread():
    global speech_thread
    if speech_thread is None or not speech_thread.is_alive():
        speech_thread = threading.Thread(target=process_speech_queue)
        speech_thread.start()


def stop_speech_thread():
    global speech_thread
    if speech_thread is not None and speech_thread.is_alive():
        speech_queue.put(None)
        speech_thread.join(timeout=5)


def cleanup_speech():
    stop_speech_thread()
    
    