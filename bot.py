import io
import os
import logging
import asyncio
import tempfile
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from openai import OpenAI
import edge_tts  # Библиотека для озвучки
from pydub import AudioSegment

# ---------- 🔑 ТВОИ ТОКЕНЫ (НЕ ДЕЛИСЬ ЭТИМ ФАЙЛОМ!) ----------
TELEGRAM_TOKEN = "8734154801:AAF6W_yIkfPBL3T5f6nmdzc3jptnQPJd16A"
OPENROUTER_API_KEY = "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"
# ---------------------------------------------------------

# ---------- НАСТРОЙКИ ----------
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret")

# Доступные языки (коды для edge-tts)
LANGUAGES = {
    "🇩🇰 Dansk": "da",
    "🇳🇱 Nederlands": "nl",
    "🇬🇧 English": "en",
    "🇨🇳 中文 (简体)": "zh",
    "🇪🇸 Español": "es"
}

# Голоса для edge-tts (можно будет расширить)
VOICES = {
    "da": "da-DK-ChristelNeural",
    "nl": "nl-NL-ColetteNeural",
    "en": "en-US-JennyNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "es": "es-ES-ElviraNeural"
}

user_languages = {}
user_contexts = {}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Клиент OpenRouter
openrouter_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------
def language_keyboard():
    buttons = []
    for name, code in LANGUAGES.items():
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"lang_{code}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_ai_response(user_id: int, user_text: str) -> str:
    lang_code = user_languages.get(user_id, "en")
    
    if user_id not in user_contexts:
        user_contexts[user_id] = []
    user_contexts[user_id].append({"role": "user", "content": user_text})

    system_prompt = {
        "role": "system",
        "content": f"""You are a helpful language tutor for a Russian speaker learning {lang_code}. 
        Reply in {lang_code} only. Keep responses under 3 sentences. 
        If the user makes a mistake, provide a short correction in *italics* at the end.
        Ask a follow-up question to continue the conversation."""
    }
    messages = [system_prompt] + user_contexts[user_id][-10:]

    try:
        chat_completion = openrouter_client.chat.completions.create(
            messages=messages,
            model="deepseek/deepseek-chat-v3-0324:free",
            temperature=0.7,
            max_tokens=300,
        )
        ai_text = chat_completion.choices[0].message.content
        user_contexts[user_id].append({"role": "assistant", "content": ai_text})
        if len(user_contexts[user_id]) > 20:
            user_contexts[user_id] = user_contexts[user_id][-10:]
        return ai_text
    except Exception as e:
        logging.error(f"OpenRouter error: {e}")
        return "Sorry, I had a problem thinking. Try again."

async def text_to_speech(text: str, lang_code: str) -> io.BytesIO:
    voice = VOICES.get(lang_code, VOICES["en"])
    communicate = edge_tts.Communicate(text, voice)
    mp3_fp = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3_fp.write(chunk["data"])
    mp3_fp.seek(0)
    return mp3_fp

# ---------- ОБРАБОТЧИКИ СООБЩЕНИЙ ----------
@dp.message(Command("start", "new"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    user_contexts.pop(user_id, None)
    
    if user_id not in user_languages:
        await message.answer(
            "👋 Привет! Я твой мультиязычный репетитор.\n\n"
            "Сначала выбери язык, который хочешь изучать:",
            reply_markup=language_keyboard()
        )
    else:
        lang_name = [k for k, v in LANGUAGES.items() if v == user_languages[user_id]][0]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Новый диалог", callback_data="new_chat")],
            [InlineKeyboardButton(text="🌐 Сменить язык", callback_data="change_lang")]
        ])
        await message.answer(
            f"👋 Ты изучаешь {lang_name}.\n"
            "Напиши мне что-нибудь на этом языке.\n"
            "Я отвечу текстом и голосом!\n\n"
            "/new — начать заново\n"
            "/explain — объяснить по-русски\n"
            "/language — сменить язык",
            reply_markup=kb
        )

@dp.message(Command("language"))
async def language_command(message: types.Message):
    await message.answer("Выбери язык для изучения:", reply_markup=language_keyboard())

@dp.callback_query(F.data.startswith("lang_"))
async def set_language(call: types.CallbackQuery):
    user_id = call.from_user.id
    lang_code = call.data.split("_")[1]
    user_languages[user_id] = lang_code
    user_contexts.pop(user_id, None)
    
    lang_name = [k for k, v in LANGUAGES.items() if v == lang_code][0]
    await call.message.edit_text(f"✅ Язык изменён на {lang_name}")
    await call.answer()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новый диалог", callback_data="new_chat")],
        [InlineKeyboardButton(text="🌐 Сменить язык", callback_data="change_lang")]
    ])
    await call.message.answer(
        f"Теперь ты изучаешь {lang_name}.\nНапиши мне что-нибудь!",
        reply_markup=kb
    )

@dp.callback_query(F.data == "change_lang")
async def change_lang_callback(call: types.CallbackQuery):
    await call.message.edit_text("Выбери новый язык:", reply_markup=language_keyboard())
    await call.answer()

@dp.callback_query(F.data == "new_chat")
async def new_chat_callback(call: types.CallbackQuery):
    user_id = call.from_user.id
    user_contexts.pop(user_id, None)
    await call.message.edit_text("🆕 Контекст очищен. Начинаем заново!")
    await call.answer()

@dp.message(F.text)
async def text_message_handler(message: types.Message):
    user_id = message.from_user.id
    lang_code = user_languages.get(user_id, "en")
    
    await bot.send_chat_action(message.chat.id, "typing")
    ai_reply = await get_ai_response(user_id, message.text)
    await message.answer(ai_reply)
    
    await bot.send_chat_action(message.chat.id, "record_voice")
    try:
        voice_fp = await text_to_speech(ai_reply, lang_code)
        await message.answer_voice(BufferedInputFile(voice_fp.read(), filename="voice.mp3"))
    except Exception as e:
        logging.error(f"TTS error: {e}")
        await message.answer("⚠️ Не удалось создать озвучку для этого языка.")

@dp.message(F.voice)
async def voice_message_handler(message: types.Message):
    user_id = message.from_user.id
    lang_code = user_languages.get(user_id, "en")
    await bot.send_chat_action(message.chat.id, "typing")
    await message.reply("🎧 Распознаю речь...")
    
    try:
        # Скачиваем и конвертируем аудио
        file_id = message.voice.file_id
        file = await bot.get_file(file_id)
        voice_data = io.BytesIO()
        await bot.download_file(file.file_path, voice_data)
        voice_data.seek(0)
        
        with tempfile.NamedTemporaryFile(suffix='.ogg', delete=False) as tmp_ogg:
            tmp_ogg.write(voice_data.read())
            tmp_ogg.flush()
            audio = AudioSegment.from_ogg(tmp_ogg.name)
            mp3_data = io.BytesIO()
            audio.export(mp3_data, format="mp3")
            mp3_data.seek(0)
            mp3_data.name = "audio.mp3"
        os.unlink(tmp_ogg.name)
        
        # Отправляем на распознавание в OpenRouter
        transcription = openrouter_client.audio.transcriptions.create(
            model="openai/whisper-large-v3", # Можно также "openai/whisper-large-v2"
            file=mp3_data,
            language=lang_code
        )
        user_text = transcription.text
        await message.reply(f"🗣️ Вы сказали: <i>{user_text}</i>")
        
        # Обрабатываем как обычный текст
        ai_reply = await get_ai_response(user_id, user_text)
        await message.answer(ai_reply)
        
        voice_fp = await text_to_speech(ai_reply, lang_code)
        await message.answer_voice(BufferedInputFile(voice_fp.read(), filename="reply.mp3"))
        
    except Exception as e:
        logging.error(f"Voice processing error: {e}")
        await message.reply("😕 Не смог разобрать голосовое. Попробуй ещё раз.")

@dp.message(Command("explain"))
async def explain_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_contexts and len(user_contexts[user_id]) >= 2:
        last_ai = user_contexts[user_id][-1]['content']
        last_user = user_contexts[user_id][-2]['content']
        prompt = f"User asked: '{last_user}'. You answered: '{last_ai}'. Explain in Russian why you answered that way, what grammar or vocabulary was used. Keep it short and educational."
        try:
            explanation = openrouter_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="deepseek/deepseek-chat-v3-0324:free",
                max_tokens=500,
            )
            await message.answer(f"📖 Объяснение:\n{explanation.choices[0].message.content}")
        except Exception as e:
            await message.answer("Не удалось получить объяснение.")
    else:
        await message.answer("Сначала напиши что-нибудь, чтобы было что объяснять.")

# ---------- WEBHOOK ----------
async def on_startup(bot: Bot):
    webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET)
    logging.info(f"Webhook установлен на {webhook_url}")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook()

def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

if __name__ == "__main__":
    main()
