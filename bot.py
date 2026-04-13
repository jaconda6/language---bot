import io
import os
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from groq import Groq
from gtts import gTTS

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TARGET_LANGUAGE = os.getenv("TARGET_LANGUAGE", "en")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "mysecret")

if not TELEGRAM_TOKEN or not GROQ_API_KEY:
    raise ValueError("Нет токенов в переменных окружения")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
groq_client = Groq(api_key=GROQ_API_KEY)
user_contexts = {}

async def get_ai_response(user_id, user_text):
    if user_id not in user_contexts:
        user_contexts[user_id] = []
    user_contexts[user_id].append({"role": "user", "content": user_text})
    system_prompt = {
        "role": "system",
        "content": f"You are a helpful language tutor for a Russian speaker learning {TARGET_LANGUAGE}. Reply in {TARGET_LANGUAGE} only. If user makes a mistake, provide correction in *italics*. Ask a follow-up question. Keep under 3 sentences."
    }
    messages = [system_prompt] + user_contexts[user_id][-10:]
    try:
        chat_completion = groq_client.chat.completions.create(
            messages=messages,
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=300,
        )
        ai_text = chat_completion.choices[0].message.content
        user_contexts[user_id].append({"role": "assistant", "content": ai_text})
        if len(user_contexts[user_id]) > 20:
            user_contexts[user_id] = user_contexts[user_id][-10:]
        return ai_text
    except Exception as e:
        logging.error(f"Groq error: {e}")
        return "Sorry, I had a problem thinking."

async def text_to_speech(text):
    mp3_fp = io.BytesIO()
    tts = gTTS(text=text, lang=TARGET_LANGUAGE, slow=False)
    tts.write_to_fp(mp3_fp)
    mp3_fp.seek(0)
    return mp3_fp

@dp.message(Command("start", "new"))
async def start_handler(message: types.Message):
    user_contexts.pop(message.from_user.id, None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Новый диалог", callback_data="new_chat")]
    ])
    await message.answer(
        f"👋 Привет! Я учитель {TARGET_LANGUAGE}. Напиши или пришли голосовое.\n/new — начать заново\n/explain — объяснить по-русски",
        reply_markup=kb
    )

@dp.message(F.text)
async def text_message_handler(message: types.Message):
    await bot.send_chat_action(message.chat.id, "typing")
    ai_reply = await get_ai_response(message.from_user.id, message.text)
    await message.answer(ai_reply)
    await bot.send_chat_action(message.chat.id, "record_voice")
    voice_fp = await text_to_speech(ai_reply)
    await message.answer_voice(BufferedInputFile(voice_fp.read(), filename="voice.mp3"))

@dp.message(F.voice)
async def voice_message_handler(message: types.Message):
    user_id = message.from_user.id
    await bot.send_chat_action(message.chat.id, "typing")
    file_id = message.voice.file_id
    file = await bot.get_file(file_id)
    voice_data = io.BytesIO()
    await bot.download_file(file.file_path, voice_data)
    voice_data.seek(0)
    voice_data.name = "voice.ogg"
    await message.reply("🎧 Слушаю...")
    try:
        transcription = groq_client.audio.transcriptions.create(
            file=("voice.ogg", voice_data.read()),
            model="whisper-large-v3-turbo",
            response_format="text",
            language=TARGET_LANGUAGE,
        )
        user_text = transcription
        await message.reply(f"🗣️ Вы сказали: <i>{user_text}</i>")
        ai_reply = await get_ai_response(user_id, user_text)
        await message.answer(ai_reply)
        voice_fp = await text_to_speech(ai_reply)
        await message.answer_voice(BufferedInputFile(voice_fp.read(), filename="reply.mp3"))
    except Exception as e:
        logging.error(f"Voice error: {e}")
        await message.reply("😕 Не смог разобрать голосовое.")

@dp.message(Command("explain"))
async def explain_handler(message: types.Message):
    user_id = message.from_user.id
    if user_id in user_contexts and len(user_contexts[user_id]) >= 2:
        last_ai = user_contexts[user_id][-1]['content']
        last_user = user_contexts[user_id][-2]['content']
        prompt = f"User asked: '{last_user}'. You answered: '{last_ai}'. Explain in Russian why you answered that way, what grammar or vocabulary was used."
        explanation = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="llama-3.3-70b-versatile",
            max_tokens=500,
        )
        await message.answer(f"📖 Объяснение:\n{explanation.choices[0].message.content}")
    else:
        await message.answer("Сначала напиши что-нибудь.")

@dp.callback_query(F.data == "new_chat")
async def new_chat_callback(call: types.CallbackQuery):
    user_contexts.pop(call.from_user.id, None)
    await call.message.edit_text("🆕 Контекст очищен.")
    await call.answer()

async def on_startup(bot: Bot):
    webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(webhook_url, secret_token=WEBHOOK_SECRET)

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
