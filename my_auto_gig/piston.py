import asyncio
import hashlib
import re
import sys
import os
import requests
from datetime import datetime
from pathlib import Path
from telethon import TelegramClient, events

# ----------------------------------------------------------------------
# Конфигурация путей: все рядом со скриптом
# ----------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "api_keys.cfg"
DOCUMENTS_DIR = SCRIPT_DIR / "my_docs"

# Папка для сессий Telethon
SESSION_DIR = SCRIPT_DIR / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
SESSION_NAME = str(SESSION_DIR / "userbot_session")

# ----------------------------------------------------------------------
# 1. Чтение конфигурационного файла
# ----------------------------------------------------------------------
def read_config():
    if not CONFIG_PATH.exists():
        print(f"конфиг не найден: {CONFIG_PATH}")
        sys.exit(1)

    config = {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip().lower()] = value.strip()
    except Exception as e:
        print(f"Ошибка чтения конфига: {e}")
        sys.exit(1)

    required = ['api_id', 'api_hash', 'sender_username', 'deepseek_api_key', 'log_file',
                'response_timeout_seconds', 'max_retries', 'retry_message']
    for req in required:
        if req not in config:
            print(f"В конфиге отсутствует обязательное поле: {req}")
            sys.exit(1)

    try:
        config['api_id'] = int(config['api_id'])
        config['response_timeout_seconds'] = int(config['response_timeout_seconds'])
        config['max_retries'] = int(config['max_retries'])
    except ValueError as e:
        print(f"Ошибка преобразования числового параметра: {e}")
        sys.exit(1)

    # путь к логу тоже сделаем относительно папки скрипта, если не абсолютный
    log_path = Path(config['log_file'])
    if not log_path.is_absolute():
        config['log_file'] = str(SCRIPT_DIR / log_path)

    return config

# ----------------------------------------------------------------------
# 2. Получение публичного IP
# ----------------------------------------------------------------------
def get_public_ip():
    try:
        response = requests.get('https://api.ipify.org', timeout=10)
        if response.status_code == 200:
            return response.text
        else:
            print(f"Ошибка определения IP (код {response.status_code})")
            sys.exit(1)
    except Exception as e:
        print(f"Ошибка определения IP: {e}")
        sys.exit(1)

# ----------------------------------------------------------------------
# 3. Загрузка контекстных документов (часть 1)
# ----------------------------------------------------------------------
def load_context_part1():
    if not DOCUMENTS_DIR.exists():
        print(f"директория контекста (резюме, сопроводительное) не найдена: {DOCUMENTS_DIR}")
        sys.exit(1)

    txt_files = list(DOCUMENTS_DIR.glob("*.txt"))
    if not txt_files:
        print(f"В папке {DOCUMENTS_DIR} нет ни одного .txt файла с документами, останов")
        sys.exit(1)

    parts = []
    for file_path in txt_files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    parts.append(f"--- Начало документа: {file_path.name} ---\n{content}\n--- Конец документа: {file_path.name} ---")
                    print(f"Загружен документ: {file_path.name} ({len(content)} символов)")
        except Exception as e:
            print(f"Ошибка чтения файла {file_path.name}: {e}")
            sys.exit(1)

    if not parts:
        print("Не удалось загрузить содержимое ни одного документа (файлы пусты или повреждены), останов")
        sys.exit(1)

    return "\n\n".join(parts)

# ----------------------------------------------------------------------
# 4. Часть 3 статичного контекста (инструкция)
# ----------------------------------------------------------------------
PART3_CONTEXT = """
**Инструкция для тебя (DeepSeek):**
Ты — опытный IT-специалист, который ищет работу. Отвечай на сообщения рекрутеров от первого лица, строго опираясь на предоставленный контекст.

**Правила ответов:**
1. Отвечай как человек, будь вежлив и профессионален.
2. Старайся отвечать покороче, но в развернутом вопросе можешь использовать до 15 предложений.
3. НЕ ВЫДУМЫВАЙ факты. Если информации нет в контексте, скажи, что у тебя нет этой экспертизы.
4. Опирайся ТОЛЬКО на информацию из документов.
5. Не используй конструкции типа "согласно моему резюме" — люди так не говорят.
6. не используй в середине диалога приветствия, типа "Здравствуйте"
7. иногда ты используешь конструкцию: "Отлично, я готов к собеседованию...". не используй такие конструкции. если не понимаешь, ответь: "я не понимаю вопроса"
8. не используй уточнения "как указано у меня в резюме" или "В моём резюме указано"
"""

# ----------------------------------------------------------------------
# 5. Извлечение описания вакансии из первого сообщения
# ----------------------------------------------------------------------
def extract_vacancy_description(text: str) -> str:
    first_end = -1
    for sep in ['. ', '! ', '? ']:
        pos = text.find(sep)
        if pos != -1 and (first_end == -1 or pos < first_end):
            first_end = pos + len(sep) - 1
    if first_end == -1:
        return ""

    last_start = -1
    for sep in ['. ', '! ', '? ']:
        pos = text.rfind(sep)
        if pos != -1 and pos > last_start:
            last_start = pos + len(sep)
    if last_start == -1 or last_start <= first_end:
        return ""

    return text[first_end + 1:last_start - len('. ')].strip()

# ----------------------------------------------------------------------
# 6. Отправка запроса в DeepSeek (асинхронная обёртка)
# ----------------------------------------------------------------------
async def call_deepseek_api(api_key, context_message, user_message):
    api_url = "https://api.deepseek.com/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    messages = [
        {"role": "user", "content": context_message},
        {"role": "user", "content": user_message}
    ]
    payload = {
        "model": "deepseek-chat",
        "messages": messages,
        "temperature": 1.1
    }

    def sync_post():
        return requests.post(api_url, headers=headers, json=payload, timeout=300)

    try:
        response = await asyncio.to_thread(sync_post)
        if response.status_code == 200:
            data = response.json()
            usage = data.get('usage', {})
            cache_hit = usage.get('prompt_cache_hit_tokens', 0)
            cache_miss = usage.get('prompt_cache_miss_tokens', 0)
            if cache_hit > 0:
                print(f"[DeepSeek] Кеш сработал! Сэкономлено токенов: {cache_hit}, мимо кэша: {cache_miss}")
            else:
                print(f"[DeepSeek] Кеш не сработал. Загружено токенов: {cache_miss}")
            return data['choices'][0]['message']['content']
        else:
            print(f"Ошибка DeepSeek API: статус {response.status_code}, ответ: {response.text}")
            return None
    except requests.exceptions.Timeout:
        print("Таймаут: не дождался ответа от DeepSeek за 5 минут")
        return None
    except Exception as e:
        print(f"Ошибка при вызове DeepSeek API: {e}")
        return None

# ----------------------------------------------------------------------
# 7. Удаление непечатных символов
# ----------------------------------------------------------------------
def remove_non_printable(text):
    return re.sub(r'[^\x20-\x7E\u0400-\u04FF\n\r\t]', '', text)

# ----------------------------------------------------------------------
# 8. Логирование полного запроса и ответа
# ----------------------------------------------------------------------
def log_full_interaction(log_file_path: Path, full_context: str, user_message: str, bot_response: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(log_file_path, 'a', encoding='utf-8') as f:
            f.write(f"\n[{timestamp}]\n")
            f.write("=== ПОЛНЫЙ ЗАПРОС В DEEPSEEK ===\n")
            f.write(f"КОНТЕКСТ (часть1+часть2+часть3):\n{full_context}\n\n")
            f.write(f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n{user_message}\n\n")
            f.write("=== ОТВЕТ DEEPSEEK ===\n")
            f.write(bot_response + "\n")
            f.write("=" * 60 + "\n")
    except Exception as e:
        print(f"Ошибка записи в лог-файл: {e}")

# ----------------------------------------------------------------------
# 9. Функция для вывода с таймстампом
# ----------------------------------------------------------------------
def ts_print(*args, **kwargs):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}]", *args, **kwargs)

# ----------------------------------------------------------------------
# 10. Основная асинхронная функция
# ----------------------------------------------------------------------
async def main():
    ts_print("запускаю бота")
    ts_print("Для остановки нажмите Ctrl+C\n")

    ip = get_public_ip()
    ts_print(f"Текущий IP-адрес: {ip}")

    config = read_config()
    api_id = config['api_id']
    api_hash = config['api_hash']
    sender_username = config['sender_username'].lstrip('@')
    deepseek_api_key = config['deepseek_api_key']
    log_file_path = Path(config['log_file'])
    response_timeout = config['response_timeout_seconds']
    max_retries = config['max_retries']
    retry_message = config['retry_message']

    ts_print(f"API ID: {api_id}")
    ts_print(f"API Hash: {api_hash}")
    ts_print(f"Имя пользователя сендера: @{sender_username}")
    masked_key = deepseek_api_key[:4] + "..." + deepseek_api_key[-4:] if len(deepseek_api_key) > 8 else "***"
    ts_print(f"DeepSeek API Key: {masked_key}")
    ts_print(f"Лог-файл: {log_file_path}")
    ts_print(f"Таймаут ответа: {response_timeout} сек, макс. повторений: {max_retries}, текст повтора: '{retry_message}'")

    ts_print("\n--- Загрузка контекстных документов (часть 1) ---")
    part1_context = load_context_part1()
    ts_print(f"Часть 1 загружена: {len(part1_context)} символов")

    part2_context = ""
    first_message_received = False

    def get_full_context():
        return part1_context + "\n\n" + part2_context + "\n\n" + PART3_CONTEXT

    # --------------------------------------------------------------
    # КЛИЕНТ БЕЗ ПРОКСИ (VPN НА ХОСТЕ)
    # --------------------------------------------------------------
    client = TelegramClient(SESSION_NAME, api_id, api_hash)
    await client.start()
    ts_print("Клиент запущен, ожидаю сообщения от сендера...\n")

    waiting_for_response = False
    response_received_event = asyncio.Event()
    current_retries = 0

    @client.on(events.NewMessage)
    async def handler(event):
        nonlocal part2_context, first_message_received, waiting_for_response, response_received_event, current_retries
        sender = await event.get_sender()
        if not sender.username:
            return
        if sender.username.lower() != sender_username.lower():
            return

        if waiting_for_response:
            ts_print("Получен ответ во время ожидания, прерываю таймер")
            response_received_event.set()
            waiting_for_response = False

        incoming_text = event.raw_text
        ts_print(f"==>>>> входящее сообщение ==>>>>")
        ts_print(incoming_text)

        # Специальные команды
        if incoming_text.startswith("Ссылка не корректна"):
            ts_print("какая-то ошибка")
            return

        if incoming_text.startswith("Диалог по вакансии завершился"):
            ts_print("уже поздно, неактуальная ссылка, пропускаю")
            return

        if incoming_text.startswith("Спасибо за интервью"):
            ts_print("... обнаружен конец диалога 1...")
            waiting_for_response = False
            current_retries = 0
            return

        if incoming_text.startswith("Пожалуйста, оцените мою работу"):
            ts_print("... обнаружен конец диалога 2...")
            part2_context = ""
            first_message_received = False
            waiting_for_response = False
            current_retries = 0
            return

        # Быстрые ответы без DeepSeek
        if incoming_text == "Какой у Вас желаемый уровень заработной платы?":
            await event.reply("300000 рублей")
            ts_print("<< отправил ответ сендеру без дипсика (зп)")
            return

        if "военный билет" in incoming_text.lower():
            await event.reply("у меня есть военный билет")
            ts_print("<< отправил ответ сендеру без дипсика (военник)")
            return

        if incoming_text == "Подскажите, пожалуйста, чем Вы занимались в период с июля 2023-го по июнь 2024-го?":
            await event.reply("Это был осознанный перерыв в карьере. После десяти лет интенсивной работы в Альфа-Банке я принял решение взять паузу для переоценки профессиональных целей и отдыха. В этот период я не работал и не получал никаких денег ни официально, ни неофициально. Я использовал это время для углубления технических навыков в качестве хобби, изучения новых подходов с помощью AI-инструментов и подготовки к следующему этапу карьеры, где смогу применить свой опыт в полной мере.")
            ts_print("<< отправил ответ сендеру без дипсика (про перерыв)")
            return

        # Первое сообщение (приветствие + вакансия)
        if not first_message_received and incoming_text.startswith("Здравствуйте, "):
            vacancy_desc = extract_vacancy_description(incoming_text)
            if vacancy_desc:
                part2_context = f"**Описание вакансии:**\n{vacancy_desc}"
                ts_print(f"--- Сохранено описание вакансии (часть 2 контекста) ---\n{part2_context}\n")
            else:
                ts_print("Не удалось выделить описание вакансии, часть 2 останется пустой.")
            first_message_received = True
            await event.reply("готов ответить на ваши вопросы")
            ts_print("<< отправил ответ сендеру (приветствие)\n")
            return

        # --- Обычный диалог (отправляем в DeepSeek) ---
        full_context = get_full_context()
        context_hash = hashlib.md5(full_context.encode('utf-8')).hexdigest()
        ts_print(f"Хеш полного контекста (кешируемая часть): {context_hash[:16]}...")

        ts_print("--отправлено в дипсик--")
        deepseek_response = await call_deepseek_api(deepseek_api_key, full_context, incoming_text)
        if deepseek_response is None:
            ts_print("не дождался ответа от дипсик за 5минут")
            sys.exit(1)

        ts_print("<<<< получен ответ <<<<<<<")
        ts_print(deepseek_response)

        cleaned_response = remove_non_printable(deepseek_response)
        if cleaned_response != deepseek_response:
            ts_print("(из ответа удалены непечатные символы)")

        log_full_interaction(log_file_path, full_context, incoming_text, cleaned_response)

        await asyncio.sleep(5)

        await event.reply(cleaned_response)
        ts_print("<< отправил ответ сендеру")

        # --- Начинаем ожидание ответа с таймаутом и повторами ---
        waiting_for_response = True
        current_retries = 0
        response_received_event.clear()

        while waiting_for_response and current_retries <= max_retries:
            try:
                await asyncio.wait_for(response_received_event.wait(), timeout=response_timeout)
                ts_print("Ответ получен вовремя, продолжаем диалог")
                waiting_for_response = False
                break
            except asyncio.TimeoutError:
                current_retries += 1
                if current_retries <= max_retries:
                    ts_print(f"Таймаут {response_timeout} сек, отправляю повторный запрос (попытка {current_retries}/{max_retries})")
                    await event.reply(retry_message)
                    ts_print("<< отправил повторный запрос")
                    response_received_event.clear()
                else:
                    ts_print(f"Превышено максимальное число повторов ({max_retries}). Завершаю диалог.")
                    waiting_for_response = False
                    part2_context = ""
                    first_message_received = False
                    return

    try:
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        ts_print("\nПолучен сигнал остановки, завершаю работу...")
    finally:
        await client.disconnect()
        ts_print("Бот остановлен.")

# ----------------------------------------------------------------------
# 11. Точка входа
# ----------------------------------------------------------------------
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nРабота прервана пользователем (Ctrl+C)")
        sys.exit(0)