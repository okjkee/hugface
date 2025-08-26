# commentator_telegram_hf_ready.py
import os
import logging
import asyncio
import csv
from datetime import datetime
from typing import Tuple, Optional
import aiohttp
from pyrogram import Client, errors
from pyrogram.types import Message
from pyrogram.enums import ChatType
from pyrogram.raw.functions import updates
from pyrogram.raw.types import updates as raw_updates

# ---------- ВШИТЫЕ КРЕДЕНШИЛЫ (тут твои данные) ----------
API_ID = 25794511
API_HASH = "bb5432d6f6f30b7f9cdb113e6282d2f4"
PHONE_NUMBER = "+380631873460"
BOT_TOKEN = "8494933940:AAG65QMzv-b2Os1JZOqk2MY0hmyA5FIEjrg"
HF_API_TOKEN = "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"
HF_MODEL = "distilbert/distilgpt2"
# ----------------------------------------------------------------

# Параметры пауз/таймингов
COMMENT_DELAY = 600  # пауза между комментами в канале (сек)
GLOBAL_COMMENT_DELAY = 300
RECONNECT_DELAY = 30
MAX_RECONNECT_ATTEMPTS = 5

# Логи и файлы
BLACKLIST_FILE = "blacklist.txt"
PROCESSED_POSTS_FILE = "processed_posts.txt"
REPORTS_FILE = "reports.csv"

# Разрешённые типы постов (можешь настроить)
ALLOWED_POST_TYPES = {
    "text": True,
    "text_photo": True,
    "text_video": True,
    "text_document": True,
    "text_audio": True,
    "photo": False,
    "video": False,
    "document": False,
    "audio": False
}

BAD_KEYWORDS = [
    "gore", "porn", "porno", "violence", "death", "murder",
    "rape", "terror", "bomb", "explosion", "child", "suicide"
]

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)

# Проверка минимального наличия данных (на случай случайной правки)
if not API_ID or not API_HASH or not PHONE_NUMBER:
    logger.error("🚨 Telegram credentials missing. Проверь API_ID/API_HASH/PHONE_NUMBER в файле.")
    raise SystemExit(1)
if not HF_API_TOKEN:
    logger.error("🚨 HF_API_TOKEN не найден. Проверь HF_API_TOKEN в файле.")
    raise SystemExit(1)

# ---------- Pyrogram client ----------
app = Client(
    "my_account",
    api_id=API_ID,
    api_hash=API_HASH,
    phone_number=PHONE_NUMBER,
    device_model="PC",
    system_version="Unknown",
    app_version="1.0",
    lang_code="en"
)

# Состояние
last_message_ids = {}
last_comment_times = {}
blacklist = set()
processed_posts = set()
state = None
start_time = datetime.now()
reconnect_attempts = 0

# ---------- Утилиты для загрузки/сохранения ----------
def load_blacklist_and_posts():
    global blacklist, processed_posts
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s:
                        try:
                            blacklist.add(int(s))
                        except Exception:
                            pass
            logger.info(f"ℹ️ Загружен blacklist: {len(blacklist)} записей")
        except Exception as e:
            logger.error(f"Ошибка чтения {BLACKLIST_FILE}: {e}")
    else:
        open(BLACKLIST_FILE, "a").close()

    if os.path.exists(PROCESSED_POSTS_FILE):
        try:
            with open(PROCESSED_POSTS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s:
                        processed_posts.add(s)
            logger.info(f"ℹ️ Загружен processed_posts: {len(processed_posts)} записей")
        except Exception as e:
            logger.error(f"Ошибка чтения {PROCESSED_POSTS_FILE}: {e}")
    else:
        open(PROCESSED_POSTS_FILE, "a").close()

def save_blacklist(chat_id: int):
    if chat_id not in blacklist:
        blacklist.add(chat_id)
        with open(BLACKLIST_FILE, "a", encoding="utf-8") as f:
            f.write(f"{chat_id}\n")
        logger.info(f"ℹ️ Канал {chat_id} добавлен в blacklist")

def save_processed_post(chat_id: int, msg_id: int):
    key = f"{chat_id}:{msg_id}"
    if key not in processed_posts:
        processed_posts.add(key)
        with open(PROCESSED_POSTS_FILE, "a", encoding="utf-8") as f:
            f.write(f"{key}\n")

def save_report(chat_id: int, linked_chat_id: int, message: Message, comment: str, tokens_in: int, tokens_out: int):
    report = {
        "date": datetime.now().isoformat(),
        "chat_id": chat_id,
        "linked_chat_id": linked_chat_id,
        "account_id": PHONE_NUMBER,
        "channel_title": message.chat.title,
        "post_text": message.text or message.caption or "",
        "post_type": get_post_type(message),
        "comment_text": comment,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out
    }
    file_exists = os.path.exists(REPORTS_FILE)
    with open(REPORTS_FILE, "a", newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=report.keys())
        if not file_exists:
            writer.writeheader()
        writer.writerow(report)

# ---------- Взаимодействие с Hugging Face ----------
async def generate_comment(post_text: str) -> Tuple[str, int, int]:
    """
    Возвращает (comment, tokens_in, tokens_out).
    Если пост содержит запрещёнку — возвращает ("REJECT",0,0).
    Пытается HF_MODEL, затем список fallback моделей.
    """
    if not post_text or not post_text.strip():
        return "", 0, 0

    lower = post_text.lower()
    for kw in BAD_KEYWORDS:
        if kw in lower:
            logger.info("🔒 Обнаружен запрещённый контент — отклоняем")
            return "REJECT", 0, 0

    prompt = (
        "You are a witty assistant creating comments for Telegram posts.\n"
        "If the post is gore, extreme violence, sexual content involving minors, or other explicit criminal content — reply exactly REJECT.\n"
        "Otherwise, write a cheerful concise comment (10–30 words) in the same language as the post. Do not include emojis or mention AI.\n\n"
        f"Post: {post_text}\n\nComment:"
    )

    models_to_try = [HF_MODEL] if HF_MODEL else []
    models_to_try += ["distilgpt2", "gpt2", "EleutherAI/gpt-neo-125M"]

    payload = {
        "inputs": prompt,
        "parameters": {
            "max_new_tokens": 80,
            "temperature": 0.7,
            "return_full_text": False
        }
    }

    headers = {
        "Authorization": f"Bearer {HF_API_TOKEN}",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        last_error = None
        for model_name in models_to_try:
            if not model_name:
                continue
            url = f"https://api-inference.huggingface.co/models/{model_name}"
            try:
                async with session.post(url, headers=headers, json=payload, timeout=60) as resp:
                    status = resp.status
                    text = await resp.text()
                    logger.debug(f"HF ({model_name}) status {status}; body (first 300 chars): {text[:300]}")

                    if status == 200:
                        try:
                            data = await resp.json()
                        except Exception:
                            data = text
                        generated = ""
                        if isinstance(data, list) and data:
                            if isinstance(data[0], dict) and "generated_text" in data[0]:
                                generated = data[0]["generated_text"]
                            else:
                                generated = str(data[0])
                        elif isinstance(data, dict):
                            if "error" in data:
                                last_error = data.get("error")
                                logger.error(f"HF returned error for {model_name}: {last_error}")
                                continue
                            for k in ("generated_text", "text", "response"):
                                if k in data:
                                    generated = data[k]
                                    break
                            if not generated:
                                generated = str(data)
                        else:
                            generated = str(data)

                        if generated.startswith(prompt):
                            generated = generated[len(prompt):].strip()

                        words = generated.split()
                        if len(words) > 40:
                            generated = " ".join(words[:40])

                        tokens_in = len(post_text.split())
                        tokens_out = len(generated.split())
                        return generated.strip(), tokens_in, tokens_out

                    elif status == 404:
                        logger.warning(f"⚠️ Model '{model_name}' not found (404). Trying fallback.")
                        last_error = text
                        continue
                    elif status in (401, 403):
                        logger.error(f"🚨 Auth error {status} for HF model {model_name}. Check HF_API_TOKEN and model permissions.")
                        last_error = text
                        break
                    else:
                        logger.error(f"🚨 HF returned {status} for model {model_name}: {text[:300]}")
                        last_error = text
                        continue

            except asyncio.TimeoutError:
                logger.error(f"🚨 Timeout calling HF model {model_name}")
                last_error = "timeout"
                continue
            except Exception as e:
                logger.error(f"🚨 Exception calling HF model {model_name}: {e}")
                last_error = str(e)
                continue

    logger.error(f"🚨 Не удалось получить ответ от HF. Последняя ошибка: {last_error}")
    return "", 0, 0

# ---------- Вспомогательные функции ----------
def get_post_type(message: Message) -> str:
    has_text = bool(message.text or message.caption)
    has_photo = bool(message.photo)
    has_video = bool(message.video)
    has_document = bool(message.document)
    has_audio = bool(message.audio)
    if has_text:
        if has_photo: return "text_photo"
        if has_video: return "text_video"
        if has_document: return "text_document"
        if has_audio: return "text_audio"
        return "text"
    if has_photo: return "photo"
    if has_video: return "video"
    if has_document: return "document"
    if has_audio: return "audio"
    return "unknown"

async def can_comment(client: Client, chat_id: int) -> Tuple[bool, Optional[int]]:
    if chat_id in blacklist:
        return False, None
    try:
        chat_info = await client.get_chat(chat_id)
        if chat_info.linked_chat:
            return True, chat_info.linked_chat.id
        save_blacklist(chat_id)
        return False, None
    except errors.FloodWait as fw:
        logger.warning(f"Flood wait {fw.value}s for {chat_id}")
        await asyncio.sleep(fw.value)
        return False, None
    except Exception as e:
        logger.error(f"Error checking comment capability for {chat_id}: {e}")
        return False, None

async def send_notification(chat_title: str, comment: str):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": BOT_TOKEN and str(os.getenv("NOTIFY_USER_ID", "")) or "", "text": f"Commented in '{chat_title}': {comment}"}
    async with aiohttp.ClientSession() as session:
        try:
            await session.post(url, json=payload, ssl=False)
        except Exception:
            pass

# ---------- Основной процесс обработки сообщений ----------
async def process_message(client: Client, message: Message):
    chat_id = message.chat.id
    message_id = message.id
    post_key = f"{chat_id}:{message_id}"
    if post_key in processed_posts:
        return
    if message.date < start_time:
        save_processed_post(chat_id, message_id)
        return

    now = datetime.now()
    last_time = last_comment_times.get(chat_id)
    if last_time and (now - last_time).total_seconds() < COMMENT_DELAY:
        save_processed_post(chat_id, message_id)
        return

    post_type = get_post_type(message)
    if not ALLOWED_POST_TYPES.get(post_type, False):
        save_processed_post(chat_id, message_id)
        return

    logger.info(f"🔔 Новый пост в '{message.chat.title or chat_id}' (ID: {chat_id})")

    ok, linked_chat_id = await can_comment(client, chat_id)
    if not ok or not linked_chat_id:
        save_processed_post(chat_id, message_id)
        return

    # Ищем соответствующий пересланный/связанный пост в linked_chat
    target_message = None
    try:
        async for linked_msg in client.get_chat_history(linked_chat_id, limit=50):
            if (linked_msg.forward_from_chat and linked_msg.forward_from_chat.id == chat_id
                and hasattr(linked_msg, "forward_from_message_id") and linked_msg.forward_from_message_id == message_id):
                target_message = linked_msg
                break
            if (linked_msg.from_user is None and linked_msg.sender_chat and linked_msg.sender_chat.id == chat_id
                and (linked_msg.text or linked_msg.caption) == (message.text or message.caption)):
                target_message = linked_msg
                break
    except Exception as e:
        logger.warning(f"Ошибка при поиске связанного сообщения: {e}")

    if not target_message:
        save_processed_post(chat_id, message_id)
        return

    # Сгенерировать комментарий
    comment, tokens_in, tokens_out = await generate_comment(message.text or message.caption or "")
    if comment == "REJECT" or not comment:
        save_processed_post(chat_id, message_id)
        return

    comment = comment.strip('"\' ')
    try:
        await client.send_message(linked_chat_id, comment, reply_to_message_id=target_message.id)
        logger.info(f"✅ Комментарий отправлен: {comment}")
        save_report(chat_id, linked_chat_id, message, comment, tokens_in, tokens_out)
        last_comment_times[chat_id] = datetime.now()
        last_message_ids[chat_id] = message_id
        await send_notification(message.chat.title or str(chat_id), comment)
        await asyncio.sleep(GLOBAL_COMMENT_DELAY)
    except (errors.Forbidden, errors.ChatWriteForbidden):
        logger.info("Нет прав на отправку — добавляем в blacklist")
        save_blacklist(chat_id)
    except errors.FloodWait as fw:
        logger.warning(f"Flood wait при отправке: {fw.value}s")
        await asyncio.sleep(fw.value)
    except Exception as e:
        logger.error(f"Ошибка отправки комментария: {e}")

    save_processed_post(chat_id, message_id)

# ---------- Обработка обновлений (GetDifference) и polling ----------
async def fetch_updates(client: Client):
    global state, reconnect_attempts
    while True:
        try:
            if state is None:
                state = await client.invoke(updates.GetState())
                reconnect_attempts = 0
                logger.info("📡 Инициализировано состояние обновлений")
            difference = await client.invoke(
                updates.GetDifference(pts=state.pts, qts=state.qts, date=state.date)
            )

            if isinstance(difference, raw_updates.DifferenceEmpty):
                pass
            elif isinstance(difference, raw_updates.Difference) or isinstance(difference, raw_updates.DifferenceSlice):
                msgs = difference.new_messages
                for msg in msgs:
                    chat_id = getattr(msg.peer_id, 'channel_id', None)
                    if chat_id and chat_id not in blacklist:
                        message = await client.get_messages(chat_id, msg.id)
                        if message and message.chat.type == ChatType.CHANNEL:
                            await process_message(client, message)
                # обновляем state
                try:
                    st = difference.state if isinstance(difference, raw_updates.Difference) else difference.intermediate_state
                    state.pts = st.pts
                    state.qts = st.qts
                    state.date = st.date
                except Exception:
                    state = await client.invoke(updates.GetState())
            else:
                state = await client.invoke(updates.GetState())
            reconnect_attempts = 0
        except errors.FloodWait as fw:
            logger.warning(f"Flood wait in updates: {fw.value}s")
            await asyncio.sleep(fw.value)
        except (ConnectionResetError, OSError) as e:
            reconnect_attempts += 1
            if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error("Превышено число попыток переподключения")
                raise
            delay = RECONNECT_DELAY * (2 ** reconnect_attempts)
            logger.warning(f"Сетевая ошибка {e}. Ждём {delay}s")
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Ошибка при получении обновлений: {e}")
        await asyncio.sleep(10)

async def poll_channels(client: Client):
    global reconnect_attempts
    while True:
        try:
            async for dialog in client.get_dialogs():
                chat = dialog.chat
                if chat.type != ChatType.CHANNEL or chat.id in blacklist:
                    continue
                async for msg in client.get_chat_history(chat.id, limit=1):
                    key = f"{chat.id}:{msg.id}"
                    if key in processed_posts:
                        continue
                    if chat.id not in last_message_ids or msg.id > last_message_ids[chat.id]:
                        await process_message(client, msg)
            reconnect_attempts = 0
        except errors.FloodWait as fw:
            logger.warning(f"Flood wait in polling: {fw.value}s")
            await asyncio.sleep(fw.value)
        except (ConnectionResetError, OSError) as e:
            reconnect_attempts += 1
            if reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                logger.error("Превышено число попыток переподключения")
                raise
            delay = RECONNECT_DELAY * (2 ** reconnect_attempts)
            logger.warning(f"Сетевая ошибка {e}. Ждём {delay}s")
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error(f"Ошибка в polling: {e}")
        await asyncio.sleep(30)

# ---------- Хуки и запуск ----------
@app.on_raw_update()
async def on_client_start(client: Client, update, users, chats):
    if not hasattr(app, "_updates_task"):
        logger.info("🚀 Клиент запущен")
        load_blacklist_and_posts()
        app._updates_task = asyncio.create_task(fetch_updates(client))
        app._polling_task = asyncio.create_task(poll_channels(client))

if __name__ == "__main__":
    logger.info("🚀 Запуск Telegram-комментатора (Hugging Face)")
    try:
        app.run()
    except errors.AuthKeyUnregistered:
        logger.error("Auth key unregistered — удалите session файл и перезапустите")
    except errors.UserDeactivatedBan:
        logger.error("Аккаунт заблокирован Telegram")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при запуске: {e}")
