sanzh, [29 окт. 2026 в 22:43]
import os, re, time, asyncio, tempfile, threading, queue, uuid
import edge_tts
import pygame
import speech_recognition as sr
from google import genai
from google.genai import types

# ---------- Настройки ----------
MODEL = "gemini-3.8-flash"
VOICE = "ru-RU-SvetlanaNeural"
PITCH = "-2Hz"     # выше = "+10Hz", ниже = "-10Hz"
RATE = "+5%"       # быстрее = "+15%", медленнее = "-10%"

client = genai.Client(
    api_key="API_KEY",
    http_options=types.HttpOptions(timeout=20000),
)

system_instruction = (
    "Ты Карен из мультфильма «Губка Боб Квадратные Штаны»: умный компьютер, "
    "жена Планктона. Ты холодная, остроумная и саркастичная, говоришь ровно и по-деловому, "
    "но в глубине души заботишься о собеседнике. Обращайся к собеседнику «Планктон». "
    "Отвечай по-русски, строго 1-2 коротких предложения. "
    "Не используй смайлики, списки, звёздочки и заголовки: твой ответ озвучивается голосом."
)

config = types.GenerateContentConfig(
    system_instruction=system_instruction,
    max_output_tokens=500,
    thinking_config=types.ThinkingConfig(thinking_level="low"),
)
chat = client.chats.create(model=MODEL, config=config)

# ---------- Голос ----------
pygame.mixer.init()
text_q = queue.Queue()    # предложения, которые надо озвучить
audio_q = queue.Queue()   # готовые аудиофайлы

def synthesize(text, path):
    async def go():
        await edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH).save(path)
    asyncio.run(go())

def synth_worker():
    while True:
        text = text_q.get()
        path = os.path.join(tempfile.gettempdir(), f"karen_{uuid.uuid4().hex}.mp3")
        try:
            synthesize(text, path)
            audio_q.put(path)
        except Exception as e:
            print("Ошибка озвучки:", e)
            audio_q.put(None)
        text_q.task_done()

def play_worker():
    while True:
        path = audio_q.get()
        if path:
            try:
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    time.sleep(0.05)
                pygame.mixer.music.unload()
                os.remove(path)
            except Exception as e:
                print("Ошибка воспроизведения:", e)
        audio_q.task_done()

threading.Thread(target=synth_worker, daemon=True).start()
threading.Thread(target=play_worker, daemon=True).start()

def say(text):
    text = re.sub(r"[*_#`]", "", text).strip()
    if text:
        text_q.put(text)

def wait_until_spoken():
    text_q.join()
    audio_q.join()

# ---------- Gemini: говорим по предложениям, пока она ещё пишет ----------
def ask_and_speak(user_text):
    buffer = ""
    for chunk in chat.send_message_stream(user_text):
        buffer += chunk.text or ""
        parts = re.split(r"(?<=[.!?])\s+", buffer)
        for sentence in parts[:-1]:
            say(sentence)
        buffer = parts[-1]
    say(buffer)

# ---------- Микрофон ----------
recognizer = sr.Recognizer()
recognizer.pause_threshold = 0.7

with sr.Microphone() as source:
    print("Настраиваю микрофон, помолчи секунду...")
    recognizer.adjust_for_ambient_noise(source, duration=1)

print("--- Карен запущена. Говори. Чтобы выйти, скажи «стоп». ---")
say("Карен на связи.")
wait_until_spoken()

STOP_PHRASES = {"стоп", "выход", "пока", "до свидания"}

while True:
    try:
        with sr.Microphone() as source:
            print("\nСлушаю...")
            audio = recognizer.listen(source, timeout=10, phrase_time_limit=15)

        print("Распознаю...")
        user_text = recognizer.recognize_google(audio, language="ru-RU")
        print(f"Вы: {user_text}")

        if user_text.lower().strip(" .!?,") in STOP_PHRASES:
            say("До свидания, Планктон.")
            wait_until_spoken()
            break

        t = time.time()
        ask_and_speak(user_text)
        wait_until_spoken()   # пока она говорит, микрофон не слушает
        print(f"(ответ занял {time.time() - t:.1f} сек)")
