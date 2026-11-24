import os, re, time, asyncio, tempfile, threading, queue, uuid
from array import array
import edge_tts
import pygame
import speech_recognition as sr
from google import genai
from google.genai import types

from face import Face, EMOTIONS

# ---------- Настройки ----------
MODEL = "gemini-3.8-flash"
VOICE = "ru-RU-SvetlanaNeural"
PITCH = "-2Hz"     # выше = "+10Hz", ниже = "-10Hz"
RATE = "+5%"       # быстрее = "+15%", медленнее = "-10%"

BORED_AFTER = 6.0      # сек речи собеседника, после которых Карен скучает (лицо 3)
PHRASE_LIMIT = 30      # максимум секунд одной фразы
EMOTION_HOLD = 3.0     # сколько держать эмоцию после ответа, потом снова волна

client = genai.Client(
    api_key=os.environ.get("GEMINI_API_KEY", "API_KEY"),
    http_options=types.HttpOptions(timeout=20000),
)

system_instruction = (
    "Ты Карен из мультфильма «Губка Боб Квадратные Штаны»: умный компьютер, "
    "жена Планктона. Ты холодная, остроумная и саркастичная, говоришь ровно и по-деловому, "
    "но в глубине души заботишься о собеседнике. Обращайся к собеседнику «Планктон». "
    "Отвечай по-русски, строго 1-2 коротких предложения. "
    "Не используй смайлики, списки, звёздочки и заголовки: твой ответ озвучивается голосом. "
    "Начинай ответ с тега своей эмоции, он не озвучивается, а показывается на твоём экране: "
    "[happy] — рада, довольна, смешно; [angry] — злишься, раздражена, тебя оскорбили; "
    "[surprised] — удивлена, шокирована; [sad] — грустно, обидно, сочувствуешь; "
    "[bored] — скучно, собеседник занудствует или говорит слишком долго; "
    "[neutral] — спокойна, просто отвечаешь. "
    "Если эмоция меняется посреди ответа, ставь новый тег перед предложением. "
    "Будь живой: не отвечай всё время [neutral]."
)

config = types.GenerateContentConfig(
    system_instruction=system_instruction,
    max_output_tokens=500,
    thinking_config=types.ThinkingConfig(thinking_level="low"),
)
chat = client.chats.create(model=MODEL, config=config)

face = Face(title="Karen")

# ---------- Эмоции из ответа ----------
TAG_RE = re.compile(r"\[\s*(" + "|".join(EMOTIONS) + r")\s*\]", re.IGNORECASE)

def split_emotion(text, current):
    """Достаёт последний тег эмоции из текста и возвращает (эмоция, текст без тегов)."""
    tags = TAG_RE.findall(text)
    emotion = tags[-1].lower() if tags else current
    return emotion, TAG_RE.sub("", text).strip()

def rms16(raw):
    """Громкость 16-битного PCM (без audioop, его убрали из Python 3.13)."""
    a = array("h", raw)
    if not a:
        return 0.0
    step = max(1, len(a) // 2000)
    s = a[::step]
    return (sum(x * x for x in s) / len(s)) ** 0.5

# ---------- Голос ----------
pygame.mixer.init()
text_q = queue.Queue()    # (эмоция, предложение) для озвучки
audio_q = queue.Queue()   # (эмоция, путь к mp3)

def synthesize(text, path):
    async def go():
        await edge_tts.Communicate(text, VOICE, rate=RATE, pitch=PITCH).save(path)
    asyncio.run(go())

def synth_worker():
    while True:
        emotion, text = text_q.get()
        path = os.path.join(tempfile.gettempdir(), f"karen_{uuid.uuid4().hex}.mp3")
        try:
            synthesize(text, path)
            audio_q.put((emotion, path))
        except Exception as e:
            print("Ошибка озвучки:", e)
            audio_q.put((emotion, None))
        text_q.task_done()

ENV_FPS = 30

def envelope(sound):
    """Громкость звука по кадрам (30 в секунду), 0..1 — чтобы волна шла в такт голосу."""
    freq, size, channels = pygame.mixer.get_init()
    if abs(size) != 16:
        return []
    a = array("h", sound.get_raw())
    win = max(1, int(freq / ENV_FPS) * channels)
    env = []
    for i in range(0, len(a), win):
        s = a[i:i + win:4]
        env.append((sum(x * x for x in s) / len(s)) ** 0.5 if s else 0.0)
    peak = max(env, default=0) or 1.0
    return [min(1.0, e / peak * 1.1) for e in env]

def play_worker():
    while True:
        emotion, path = audio_q.get()
        if path:
            try:
                face.set_emotion(emotion)        # лицо меняется вместе с началом фразы
                face.set_mode("speaking")
                sound = pygame.mixer.Sound(path)
                env = envelope(sound)
                ch = sound.play()
                t0 = time.time()
                while ch.get_busy():
                    i = int((time.time() - t0) * ENV_FPS)
                    face.set_level(env[i] if i < len(env) else 0.0)
                    time.sleep(0.02)
                face.set_level(0.0)
                os.remove(path)
            except Exception as e:
                print("Ошибка воспроизведения:", e)
        audio_q.task_done()

threading.Thread(target=synth_worker, daemon=True).start()
threading.Thread(target=play_worker, daemon=True).start()

def say(text, emotion="neutral"):
    text = re.sub(r"[*_#`]", "", text).strip()
    if text:
        text_q.put((emotion, text))

def wait_until_spoken():
    text_q.join()
    audio_q.join()
    face.set_mode("idle")
    face.relax(EMOTION_HOLD)

# ---------- Gemini: говорим по предложениям, пока она ещё пишет ----------
def ask_and_speak(user_text):
    face.set_mode("thinking")
    emotion = "neutral"
    spoken = []
    buffer = ""

    def flush(sentence):
        nonlocal emotion
        emotion, clean = split_emotion(sentence, emotion)
        if clean:
            spoken.append(f"[{emotion}] {clean}")
            say(clean, emotion)

    for chunk in chat.send_message_stream(user_text):
        buffer += chunk.text or ""
        parts = re.split(r"(?<=[.!?])\s+", buffer)
        for sentence in parts[:-1]:
            flush(sentence)
        buffer = parts[-1]
    flush(buffer)
    print("Карен:", " ".join(spoken))

# ---------- Микрофон ----------
recognizer = sr.Recognizer()
recognizer.pause_threshold = 0.7

def listen_phrase(source):
    """Слушает фразу кусками: волна реагирует на голос, после BORED_AFTER сек — скучающее лицо."""
    face.set_mode("listening")
    chunks, bored = [], False
    bytes_per_sec = source.SAMPLE_RATE * source.SAMPLE_WIDTH
    for chunk in recognizer.listen(source, timeout=10, phrase_time_limit=PHRASE_LIMIT, stream=True):
        raw = chunk.get_raw_data()
        chunks.append(raw)
        if source.SAMPLE_WIDTH == 2:
            face.set_level(rms16(raw) / 3000)
        talked = sum(map(len, chunks)) / bytes_per_sec
        if not bored and talked > BORED_AFTER:
            face.set_emotion("bored")
            bored = True
    face.set_level(0.0)
    data = b"".join(chunks)
    return sr.AudioData(data, source.SAMPLE_RATE, source.SAMPLE_WIDTH), len(data) / bytes_per_sec

STOP_PHRASES = {"стоп", "выход", "пока", "до свидания"}

def voice_loop():
    with sr.Microphone() as source:
        print("Настраиваю микрофон, помолчи секунду...")
        recognizer.adjust_for_ambient_noise(source, duration=1)

    print("--- Карен запущена. Говори. Чтобы выйти, скажи «стоп» или закрой окно. ---")
    say("Карен на связи.", "happy")
    wait_until_spoken()

    while True:
        try:
            with sr.Microphone() as source:
                print("\nСлушаю...")
                audio, seconds = listen_phrase(source)

            face.set_mode("thinking")
            print("Распознаю...")
            user_text = recognizer.recognize_google(audio, language="ru-RU")
            print(f"Вы ({seconds:.0f} сек): {user_text}")

            if user_text.lower().strip(" .!?,") in STOP_PHRASES:
                say("До свидания, Планктон.", "sad")
                wait_until_spoken()
                break

            if seconds > BORED_AFTER:
                user_text += f"\n(Планктон говорил без остановки {seconds:.0f} секунд.)"

            t = time.time()
            ask_and_speak(user_text)
            wait_until_spoken()   # пока она говорит, микрофон не слушает
            print(f"(ответ занял {time.time() - t:.1f} сек)")
        except sr.WaitTimeoutError:
            face.set_mode("idle")
        except sr.UnknownValueError:
            print("Не расслышала.")
            face.set_mode("idle")
            face.relax(0)
        except sr.RequestError as e:
            print("Ошибка распознавания:", e)
            face.set_mode("idle")
        except Exception as e:
            print("Ошибка:", e)
            face.set_emotion("surprised", hold=2)
            face.set_mode("idle")
    face.close()

# Окно pygame должно жить в главном потоке (иначе macOS падает), голос — в фоне.
threading.Thread(target=voice_loop, daemon=True).start()
face.run()
