import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from contextlib import suppress
from pathlib import Path



from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    FSInputFile,
)
from openai import OpenAI

from dotenv import load_dotenv

load_dotenv()


# =========================================================
# ЛОГИ
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


# =========================================================
# КЛЮЧИ
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================================================
# БОТ
# =========================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bot.db"
AUDIO_DIR = BASE_DIR / "generated_audio"
AUDIO_DIR.mkdir(exist_ok=True)

CARDS_PER_BLOCK = 5


# =========================================================
# ТЕМЫ
# =========================================================

TOPICS = {
    "food": {
        "title": "Кухня, кулинария, еда",
        "file": "SENTENCES_FOOD.json",
        "enabled": True,
    },
    "emotions": {
        "title": "Характер, эмоции, чувства",
        "file": "SENTENCES_EMOTIONS.json",
        "enabled": True,
    },
    "it_hiring": {
        "title": "IT, найм, поиск работы",
        "file": "SENTENCES_IT_HIRING.json",
        "enabled": True,
    },
    "random": {
        "title": "Разное",
        "file": "SENTENCES_RANDOM.json",
        "enabled": True,
    },
}


def load_json_file(filename: str):
    with open(BASE_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def load_topic_data() -> dict[str, list]:
    data = {}
    for topic_key, topic_info in TOPICS.items():
        filename = topic_info.get("file")
        if filename and (BASE_DIR / filename).exists():
            data[topic_key] = load_json_file(filename)
        else:
            data[topic_key] = []
    return data


TOPIC_DATA = load_topic_data()


# =========================================================
# БАЗА ДАННЫХ
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            state TEXT DEFAULT 'idle',
            selected_level TEXT,
            selected_topic TEXT,
            current_block INTEGER DEFAULT 0,
            current_card INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()


def get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id, name, state, selected_level, selected_topic, current_block, current_card
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    )
    row = cur.fetchone()

    if not row:
        cur.execute(
            """
            INSERT INTO users (user_id, state, current_block, current_card)
            VALUES (?, 'idle', 0, 0)
            """,
            (user_id,),
        )
        conn.commit()
        cur.execute(
            """
            SELECT user_id, name, state, selected_level, selected_topic, current_block, current_card
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()

    conn.close()

    return {
        "user_id": row[0],
        "name": row[1],
        "state": row[2],
        "selected_level": row[3],
        "selected_topic": row[4],
        "current_block": row[5],
        "current_card": row[6],
    }


def update_user(user_id: int, **kwargs):
    if not kwargs:
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    fields = []
    values = []

    for key, value in kwargs.items():
        fields.append(f"{key} = ?")
        values.append(value)

    values.append(user_id)
    sql = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?"
    cur.execute(sql, values)
    conn.commit()
    conn.close()


# =========================================================
# ВСПОМОГАТЕЛЬНОЕ
# =========================================================

def bold_once(text: str, needle: str) -> str:
    if not needle:
        return text
    pattern = re.escape(needle)
    return re.sub(pattern, f"<b>{needle}</b>", text, count=1, flags=re.IGNORECASE)


def get_global_index(block_index: int, card_index: int) -> int:
    return block_index * CARDS_PER_BLOCK + card_index


def get_topic_info(topic_key: str | None) -> dict:
    if topic_key in TOPICS:
        return TOPICS[topic_key]
    return TOPICS["food"]


def get_topic_sentences(topic_key: str | None) -> list:
    key = topic_key if topic_key in TOPIC_DATA else "food"
    return TOPIC_DATA.get(key, [])


def get_sentence(topic_key: str | None, global_index: int) -> dict:
    sentences = get_topic_sentences(topic_key)
    return sentences[global_index]


def get_total_blocks(topic_key: str | None) -> int:
    sentences = get_topic_sentences(topic_key)
    return len(sentences) // CARDS_PER_BLOCK


def level_keyboard(selected: str | None = None) -> InlineKeyboardMarkup:
    beginner_text = "Beginner"
    intermediate_text = "Intermediate"
    advanced_text = "Advanced"

    if selected == "Beginner":
        beginner_text = "Beginner 😇"
    if selected == "Intermediate":
        intermediate_text = "Intermediate 😊"
    if selected == "Advanced":
        advanced_text = "Advanced 😎"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=beginner_text, callback_data="level_beginner")],
            [InlineKeyboardButton(text=intermediate_text, callback_data="level_intermediate")],
            [InlineKeyboardButton(text=advanced_text, callback_data="level_advanced")],
        ]
    )


def topic_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for topic_key, topic_info in TOPICS.items():
        if topic_info.get("enabled"):
            callback_data = f"topic_select:{topic_key}"
        else:
            callback_data = f"topic_disabled:{topic_key}"

        rows.append(
            [InlineKeyboardButton(text=topic_info["title"], callback_data=callback_data)]
        )

    return InlineKeyboardMarkup(inline_keyboard=rows)


def block_actions_keyboard(topic_key: str, block_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Повторить блок 🔁", callback_data=f"repeat_block:{topic_key}:{block_index}")],
            [InlineKeyboardButton(text="Следующий блок ➡️", callback_data=f"next_block:{topic_key}:{block_index}")],
            [InlineKeyboardButton(text="Сменить тему 🎯", callback_data="change_topic")],
        ]
    )


def score_text(score: int) -> str:
    if score >= 10:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, супер! 🎉 10/10"
    if score == 9:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, отлично! 🤩 9/10"
    if score == 8:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, очень хорошо! 😎 8/10"
    if score == 7:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, вполне! 👍 7/10"
    if score == 6:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, но немного странно! 🧐 6/10"
    if score == 5:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, но будут вопросы! 🤔 5/10"
    if score == 4:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Да, но скорее всего переспросит! 😅 4/10"
    if score == 3:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Скорее нет, чем да. 😇 3/10"
    if score == 2:
        return "<b>🇬🇧 Поймёт ли нейтив:</b> Нет, вообще не поймёт. 😜 2/10"
    return "<b>🇬🇧 Поймёт ли нейтив:</b> Нет, не поймёт. 🤷‍♂️ 1/10"


def hash_text(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def get_tts_cache_path(text: str, prefix: str = "tts") -> Path:
    return AUDIO_DIR / f"{prefix}_{hash_text(text)}.mp3"


# =========================================================
# OPENAI: TTS / STT / GPT
# =========================================================

def text_to_speech(text: str, file_path: Path):
    audio = client.audio.speech.create(
        model="gpt-4o-mini-tts",
        voice="nova",
        input=text,
    )
    with open(file_path, "wb") as f:
        f.write(audio.content)


def speech_to_text(file_path: str) -> str:
    with open(file_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=audio_file,
        )
    return transcript.text.strip()


def extract_name_and_reply(user_text: str) -> dict:
    prompt = f"""
You are helping a Telegram bot for learning English.

The user has just introduced themselves after this message:
"Hi, I’m Taya. I live in Bristol. I’m 27 years old. I’m an English teacher.
I have a lovely cat called Richie. Do you like him?
Tell me about yourself. Where are you from? How long have you been learning English?"

Here is the user's message:
{user_text}

Do 2 things:
1. Speak English. Try to extract the user's name. If there is no name, return null.
2. Speak English. Write a short, warm, natural reply, like a real person making friendly small talk.
The reply must clearly react to details from the user's message.
The user should feel that the reply is personal, not generic.

IMPORTANT:
- Do NOT ask a new question
- Keep it short and natural
- Add "Let's begin. Choose your English level."

Return strict JSON:
{{
  "name": "name or null",
  "reply": "ready English reply"
}}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.choices[0].message.content.strip()

    try:
        return json.loads(raw)
    except Exception:
        return {
            "name": None,
            "reply": "It’s really nice to meet you! Let’s begin. Choose your English level."
        }


def evaluate_translation(user_answer: str, correct_en: str, keyword: str) -> dict:
    prompt = f"""
Ты проверяешь ответ ученика в Telegram-боте по английскому.

Правильная фраза:
{correct_en}

Ответ ученика:
{user_answer}

Ключевое слово карточки:
{keyword}

Правила оценки:
1. Самое важное — передан ли правильный смысл фразы.
2. Если ученик использует синонимы или близкие по смыслу слова, это не ошибка.
3. Если фраза звучит естественно и смысл сохранён, не занижай балл только из-за другой формулировки.
4. Нормальные замены:
   - stew / braise
   - add some salt / add salt
   - enjoy / like
   - family / relatives
5. Если ключевое слово карточки заменено синонимом, но смысл тот же, это допустимо.
6. Если ответ почти полностью правильный по смыслу и звучит естественно, можно ставить 9-10.
7. Если ответ сокращён, но смысл в целом понятен, обычно 6-8.
8. Если ответ частично по смыслу, но с заметными потерями, обычно 4-6.
9. Если ответ почти не связан с заданием, это "almost".
10. Если ответ не на английском, бессвязный, оффтоп или плохое распознавание голоса, это "almost".
11. Не будь слишком строгим к мелким артиклям и небольшим грамматическим шероховатостям, если общий смысл понятен.
12. Если ответ звучит не как дословный перевод, но естественно и по смыслу совпадает, это всё равно хороший ответ.

Верни строго JSON:
{{
  "type": "score" или "almost",
  "score": число от 1 до 10 или null,
  "reason": "коротко, одно предложение"
}}
"""
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.choices[0].message.content.strip()

    try:
        parsed = json.loads(raw)
        if parsed.get("type") == "almost":
            return {"type": "almost", "score": None}
        score = int(parsed.get("score", 1))
        score = max(1, min(10, score))
        return {"type": "score", "score": score}
    except Exception:
        return {"type": "almost", "score": None}


# =========================================================
# ЛОГИ / CHAT ACTION / ASYNC ОБЁРТКИ
# =========================================================

class StepTimer:
    def __init__(self, label: str):
        self.label = label
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        logger.info("START | %s", self.label)
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = time.perf_counter() - self.started
        logger.info("DONE  | %s | %.3fs", self.label, elapsed)


async def _send_chat_action_loop(chat_id: int, action: ChatAction):
    while True:
        try:
            await bot.send_chat_action(chat_id, action)
        except Exception as e:
            logger.warning("Chat action error (%s): %s", action, e)
        await asyncio.sleep(4)


class chat_action:
    def __init__(self, chat_id: int, action: ChatAction):
        self.chat_id = chat_id
        self.action = action
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(_send_chat_action_loop(self.chat_id, self.action))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


async def run_stt(file_path: str) -> str:
    with StepTimer(f"STT | {Path(file_path).name}"):
        return await asyncio.to_thread(speech_to_text, file_path)


async def run_extract_name_and_reply(user_text: str) -> dict:
    with StepTimer("GPT onboarding reply"):
        return await asyncio.to_thread(extract_name_and_reply, user_text)


async def run_evaluate_translation(user_answer: str, correct_en: str, keyword: str) -> dict:
    with StepTimer("GPT evaluate translation"):
        return await asyncio.to_thread(
            evaluate_translation,
            user_answer,
            correct_en,
            keyword,
        )


async def ensure_tts_file(text: str, prefix: str = "tts") -> Path:
    file_path = get_tts_cache_path(text, prefix=prefix)

    if file_path.exists() and file_path.stat().st_size > 0:
        logger.info("TTS cache hit | %s", file_path.name)
        return file_path

    with StepTimer(f"TTS generate | {prefix}"):
        await asyncio.to_thread(text_to_speech, text, file_path)

    logger.info("TTS cache saved | %s", file_path.name)
    return file_path


# =========================================================
# ОТПРАВКА СООБЩЕНИЙ
# =========================================================

async def send_onboarding(chat_id: int):
    photo_path = BASE_DIR / "taya_cat_1.png"

    text_ru = (
        "Привет, я Тая! 🤓\n"
        "Я живу в Бристоле 🇬🇧\n"
        "Мне 27 лет. Я учитель английского языка.\n\n"
        "У меня есть классный котик по имени Риччи. Тебе нравится? 🐈‍⬛\n\n"
        "Расскажи о себе! Откуда ты? Как давно занимаешься английским?\n\n"
        "<i>Ответь текстом или голосом 💬🎤</i>"
    )

    text_en_audio = (
        "Hi! I’m Taya. I live in Bristol. I’m twenty-seven years old, and I’m an English teacher. "
        "I’ve got a lovely cat called Richie. Do you like him? "
        "Tell me about yourself. Where are you from? How long have you been learning English?"
    )

    await bot.send_photo(
        chat_id,
        FSInputFile(photo_path),
        caption=text_ru,
        parse_mode="HTML"
    )

    try:
        async with chat_action(chat_id, ChatAction.RECORD_VOICE):
            audio_path = await ensure_tts_file(text_en_audio, prefix="onboarding")
        await bot.send_voice(chat_id, FSInputFile(audio_path))
    except Exception as e:
        logger.exception("TTS error in send_onboarding: %s", e)


async def send_level_selection(chat_id: int):
    await bot.send_message(
        chat_id,
        "Выбери уровень:",
        reply_markup=level_keyboard(),
    )


async def send_topic_selection(chat_id: int):
    await bot.send_message(
        chat_id,
        "<b>Intermediate!</b> ✅ Отлично! Теперь выбери тему:",
        reply_markup=topic_keyboard(),
        parse_mode="HTML",
    )


async def send_card(chat_id: int, user_id: int):
    user = get_user(user_id)
    topic_key = user["selected_topic"] or "food"
    global_index = get_global_index(user["current_block"], user["current_card"])
    sentence = get_sentence(topic_key, global_index)

    block_num = user["current_block"] + 1
    card_num = user["current_card"] + 1

    ru_text = bold_once(sentence["ru"], sentence["target_ru"])
    en_text = bold_once(sentence["en"], sentence["target_en"])

    text = (
        f"<b>{block_num} блок ({card_num}/5)</b>\n"
        f"<i>Запиши голос или текст на англ 🎤💬</i>\n\n"
        f"{ru_text}\n\n"
        f"💡 Подсказка: "
        f"<tg-spoiler>{en_text} Произносится: <b>[{sentence['pronunciation']}]</b></tg-spoiler>"
    )

    await bot.send_message(chat_id, text, parse_mode="HTML")


async def send_feedback_and_audio(chat_id: int, sentence: dict, evaluation: dict):
    en_text = bold_once(sentence["en"], sentence["target_en"])

    if evaluation["type"] == "almost":
        text = (
            "🇬🇧 Поймёт ли нейтив: Ну почти… 👀\n"
            f"<b>👩🏼Как бы сказал нейтив:</b> {en_text}"
        )
    else:
        text = (
            f"{score_text(evaluation['score'])}\n"
            f"<b>👩🏼Как бы сказал нейтив:</b> {en_text}"
        )

    await bot.send_message(chat_id, text, parse_mode="HTML")

    try:
        async with chat_action(chat_id, ChatAction.RECORD_VOICE):
            audio_path = await ensure_tts_file(sentence["en"], prefix="answer")
        await bot.send_voice(chat_id, FSInputFile(audio_path))
    except Exception as e:
        logger.exception("TTS error in send_feedback_and_audio: %s", e)


async def send_block_completed(chat_id: int, topic_key: str, block_index: int):
    sentences = get_topic_sentences(topic_key)

    start = block_index * CARDS_PER_BLOCK
    end = start + CARDS_PER_BLOCK
    block_items = sentences[start:end]

    lines = [
        f"<b>Блок №{block_index + 1} пройден</b> 🎉🌟",
        "",
        "<b>Ключевые слова блока:</b>",
        "",
    ]

    for item in block_items:
        lines.append(
            f"✅ <b>{item['target_en']}</b> - {item['gloss_ru']} [{item['pronunciation']}]"
        )

    await bot.send_message(
        chat_id,
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=block_actions_keyboard(topic_key, block_index),
    )


# =========================================================
# /start
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    get_user(message.from_user.id)

    update_user(
        message.from_user.id,
        name=None,
        state="onboarding",
        selected_level=None,
        selected_topic=None,
        current_block=0,
        current_card=0,
    )

    logger.info("/start | user_id=%s chat_id=%s", message.from_user.id, message.chat.id)
    await send_onboarding(message.chat.id)


# =========================================================
# CALLBACK: УРОВНИ
# =========================================================

@dp.callback_query(F.data == "level_beginner")
async def cb_level_beginner(callback: types.CallbackQuery):
    await callback.answer("Этот уровень пока недоступен.", show_alert=False)


@dp.callback_query(F.data == "level_advanced")
async def cb_level_advanced(callback: types.CallbackQuery):
    await callback.answer("Этот уровень пока недоступен.", show_alert=False)


@dp.callback_query(F.data == "level_intermediate")
async def cb_level_intermediate(callback: types.CallbackQuery):
    update_user(
        callback.from_user.id,
        selected_level="Intermediate",
        state="topic_select",
    )

    try:
        await callback.message.edit_reply_markup(
            reply_markup=level_keyboard(selected="Intermediate")
        )
    except Exception:
        pass

    await callback.answer()
    await send_topic_selection(callback.message.chat.id)


# =========================================================
# CALLBACK: ТЕМЫ
# =========================================================

@dp.callback_query(F.data.startswith("topic_disabled:"))
async def cb_topic_disabled(callback: types.CallbackQuery):
    await callback.answer("Эта тема пока недоступна.", show_alert=False)


@dp.callback_query(F.data.startswith("topic_select:"))
async def cb_topic_select(callback: types.CallbackQuery):
    try:
        topic_key = callback.data.split(":", 1)[1]
    except Exception:
        await callback.answer("Не удалось определить тему.", show_alert=False)
        return

    topic_info = TOPICS.get(topic_key)
    if not topic_info or not topic_info.get("enabled"):
        await callback.answer("Эта тема пока недоступна.", show_alert=False)
        return

    sentences = get_topic_sentences(topic_key)
    if not sentences:
        await callback.answer("Для этой темы пока нет карточек.", show_alert=False)
        return

    update_user(
        callback.from_user.id,
        selected_topic=topic_key,
        current_block=0,
        current_card=0,
        state="waiting_answer",
    )
    await callback.answer()
    await send_card(callback.message.chat.id, callback.from_user.id)


# =========================================================
# CALLBACK: ДЕЙСТВИЯ ПОСЛЕ БЛОКА
# =========================================================

@dp.callback_query(F.data.startswith("repeat_block:"))
async def cb_repeat_block(callback: types.CallbackQuery):
    try:
        _, topic_key, block_str = callback.data.split(":")
        block_index = int(block_str)
    except Exception:
        await callback.answer("Не удалось определить блок.", show_alert=False)
        return

    if topic_key not in TOPICS:
        await callback.answer("Не удалось определить тему.", show_alert=False)
        return

    update_user(
        callback.from_user.id,
        state="waiting_answer",
        selected_topic=topic_key,
        current_block=block_index,
        current_card=0,
    )
    await callback.answer()
    await send_card(callback.message.chat.id, callback.from_user.id)


@dp.callback_query(F.data.startswith("next_block:"))
async def cb_next_block(callback: types.CallbackQuery):
    try:
        _, topic_key, block_str = callback.data.split(":")
        current_block = int(block_str)
    except Exception:
        await callback.answer("Не удалось определить блок.", show_alert=False)
        return

    total_blocks = get_total_blocks(topic_key)
    next_block = current_block + 1

    if next_block >= total_blocks:
        await callback.answer("Это был последний блок этой темы.", show_alert=False)
        await bot.send_message(
            callback.message.chat.id,
            "Все блоки этой темы пройдены 🎉\nТеперь выбери тему:",
            reply_markup=topic_keyboard(),
        )
        update_user(
            callback.from_user.id,
            state="topic_select",
            selected_topic=None,
            current_block=0,
            current_card=0,
        )
        return

    update_user(
        callback.from_user.id,
        selected_topic=topic_key,
        current_block=next_block,
        current_card=0,
        state="waiting_answer",
    )
    await callback.answer()
    await send_card(callback.message.chat.id, callback.from_user.id)


@dp.callback_query(F.data == "change_topic")
async def cb_change_topic(callback: types.CallbackQuery):
    update_user(
        callback.from_user.id,
        state="topic_select",
        selected_topic=None,
        current_block=0,
        current_card=0,
    )
    await callback.answer()
    await bot.send_message(
        callback.message.chat.id,
        "Теперь выбери тему:",
        reply_markup=topic_keyboard(),
    )


# =========================================================
# ОБРАБОТКА ГОЛОСА И ТЕКСТА
# =========================================================

async def get_message_text(message: types.Message) -> tuple[str | None, bool]:
    if message.text:
        logger.info("Incoming text | user_id=%s", message.from_user.id)
        return message.text.strip(), True

    if message.voice:
        logger.info("Incoming voice | user_id=%s", message.from_user.id)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as temp_file:
            temp_path = temp_file.name

        try:
            with StepTimer("Telegram get_file"):
                telegram_file = await bot.get_file(message.voice.file_id)

            with StepTimer("Telegram download voice"):
                await bot.download_file(telegram_file.file_path, destination=temp_path)

            async with chat_action(message.chat.id, ChatAction.TYPING):
                text = await run_stt(temp_path)

            logger.info("STT text | user_id=%s | text=%r", message.from_user.id, text)
            return text, True
        except Exception as e:
            logger.exception("STT error: %s", e)
            return None, False
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    return None, False


@dp.message()
async def handle_all_messages(message: types.Message):
    total_started = time.perf_counter()
    user = get_user(message.from_user.id)
    text, ok = await get_message_text(message)

    if user["state"] == "onboarding":
        source_text = text if ok and text else ""

        if not source_text:
            parsed = {
                "name": None,
                "reply": "It’s really nice to meet you!"
            }
        else:
            try:
                async with chat_action(message.chat.id, ChatAction.TYPING):
                    parsed = await run_extract_name_and_reply(source_text)
            except Exception as e:
                logger.exception("GPT onboarding reply error: %s", e)
                parsed = {
                    "name": None,
                    "reply": "It’s really nice to meet you!"
                }

        name = parsed.get("name")
        reply = parsed.get("reply") or "It’s really nice to meet you!"

        if name:
            update_user(message.from_user.id, name=name)

        full_voice_text = f"{reply} Let’s begin. Choose your English level."

        try:
            async with chat_action(message.chat.id, ChatAction.RECORD_VOICE):
                audio_path = await ensure_tts_file(full_voice_text, prefix="level_intro")
            await message.answer_voice(FSInputFile(audio_path))
        except Exception as e:
            logger.exception("TTS error in onboarding reply: %s", e)

        update_user(message.from_user.id, state="level_select")
        await send_level_selection(message.chat.id)

        logger.info(
            "Handled onboarding | user_id=%s | total=%.3fs",
            message.from_user.id,
            time.perf_counter() - total_started,
        )
        return

    if user["state"] == "waiting_answer":
        topic_key = user["selected_topic"] or "food"
        global_index = get_global_index(user["current_block"], user["current_card"])
        sentence = get_sentence(topic_key, global_index)

        if not ok or not text:
            evaluation = {"type": "almost", "score": None}
        else:
            try:
                async with chat_action(message.chat.id, ChatAction.TYPING):
                    evaluation = await run_evaluate_translation(
                        user_answer=text,
                        correct_en=sentence["en"],
                        keyword=sentence["target_en"],
                    )
            except Exception as e:
                logger.exception("Evaluation error: %s", e)
                evaluation = {"type": "almost", "score": None}

        await send_feedback_and_audio(message.chat.id, sentence, evaluation)

        current_block = user["current_block"]
        current_card = user["current_card"]

        if current_card < CARDS_PER_BLOCK - 1:
            update_user(
                message.from_user.id,
                current_card=current_card + 1,
                state="waiting_answer",
            )
            await send_card(message.chat.id, message.from_user.id)
        else:
            update_user(
                message.from_user.id,
                state="block_done",
            )
            await send_block_completed(message.chat.id, topic_key, current_block)

        logger.info(
            "Handled waiting_answer | user_id=%s | total=%.3fs",
            message.from_user.id,
            time.perf_counter() - total_started,
        )
        return

    if user["state"] in ("level_select", "topic_select", "block_done"):
        await message.answer("Нажми кнопку ниже.")
        logger.info(
            "Handled passive state | user_id=%s | state=%s | total=%.3fs",
            message.from_user.id,
            user["state"],
            time.perf_counter() - total_started,
        )
        return


# =========================================================
# ЗАПУСК
# =========================================================

async def main():
    init_db()
    logger.info("Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
