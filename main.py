import os
import random
import psycopg2
import threading
from telebot import TeleBot, types
from time import sleep
from collections import defaultdict

# Объявление команд
COMMAND_NEXT_WORD = 'Следующее слово ⏭'
COMMAND_ADD_WORD = 'Добавить слово ➕'
COMMAND_DELETE_WORD = 'Удалить слово 🔙'

# Получение токена из переменной окружения
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("Переменная окружения 'BOT_TOKEN' не установлена!")

# Параметры подключения к базе данных
DB_PARAMS = {
    'dbname': 'words_db',
    'user': 'postgres',
    'password': 'postgres',
    'host': 'localhost',
    'port': '5432',
}

bot = TeleBot(BOT_TOKEN)

# Глобальный словарь для хранения состояния пользователя
user_states = {}

STATE_NONE = 0
STATE_AWAITING_RU = 1
STATE_AWAITING_EN = 2
STATE_AWAITING_DELETE = 3

# Словарь для хранения последнего выбранного слова
last_word = {}

# Перечень последних сообщений для очистки экрана
last_messages = {}

# Словарь для отслеживания использованных слов за сессию
used_words = defaultdict(set)


# Функция для получения соединения с базой данных
def get_connection():
    """Возвращает соединение с базой данных."""
    return psycopg2.connect(**DB_PARAMS)


# Инициализация структуры баз данных и добавление начальных слов
def init_db():
    """Инициализирует структуру баз данных и добавляет начальные слова."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Таблица пользователей
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id SERIAL PRIMARY KEY,
                        chat_id BIGINT UNIQUE NOT NULL
                    );
                """)

                # Таблица общих слов
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS words (
                        id SERIAL PRIMARY KEY,
                        ru TEXT NOT NULL UNIQUE,
                        en TEXT NOT NULL
                    );
                """)

                # Таблица персональных слов
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_words (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        ru TEXT NOT NULL,
                        en TEXT NOT NULL,
                        UNIQUE (user_id, ru)
                    );
                """)

                # Таблица журнала ответов
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS answer_log (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id),
                        ru TEXT NOT NULL,
                        correct_en TEXT NOT NULL,
                        chosen_en TEXT NOT NULL,
                        is_correct BOOLEAN NOT NULL,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Вставка начальных слов
                cur.execute("""
                    INSERT INTO words (ru, en)
                    VALUES
                        ('мир', 'peace'),
                        ('зеленый', 'green'),
                        ('красный', 'red'),
                        ('синий', 'blue'),
                        ('белый', 'white'),
                        ('черный', 'black'),
                        ('хороший', 'good'),
                        ('плохой', 'bad'),
                        ('привет', 'hello'),
                        ('пока', 'goodbye')
                    ON CONFLICT (ru) DO NOTHING;
                """)

            conn.commit()
    except Exception as e:
        print(f"Ошибка при инициализации базы данных: {e}")


# Получение или создание ID пользователя
def get_or_create_user_id(chat_id):
    """Получает ID пользователя из таблицы users или создает новую запись."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE chat_id=%s;", (chat_id,))
            row = cur.fetchone()
            if row:
                return row[0]
            cur.execute(
                "INSERT INTO users (chat_id) VALUES (%s) RETURNING id;",
                (chat_id,)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            return user_id


# Добавление слова в персональные слова пользователя
def add_word_to_user_words(user_id, ru, en):
    """Добавляет слово в персональные слова пользователя."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_words (user_id, ru, en)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, ru) DO NOTHING;
                """,
                (user_id, ru, en)
            )
            conn.commit()


# Выбор случайного слова с вариантами перевода
def select_random_word_with_options(user_id, chat_id):
    """Выбирает случайное слово и варианты перевода одним запросом."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Получаем все доступные слова для пользователя
            cur.execute("""
                SELECT ru, en FROM words 
                UNION ALL
                SELECT ru, en FROM user_words WHERE user_id = %s
            """, (user_id,))
            all_words = cur.fetchall()

            if not all_words:
                return None

            # Исключаем уже использованные слова за эту сессию
            unused_words = [
                (ru, en) for ru, en in all_words
                if ru not in used_words[chat_id]
            ]

            # Если все слова уже использованы, очищаем историю и начинаем заново
            if not unused_words:
                used_words[chat_id].clear()
                unused_words = all_words

            # Выбираем случайное слово из неиспользованных
            ru, correct_en = random.choice(unused_words)

            # Добавляем слово в использованные
            used_words[chat_id].add(ru)

            # Генерируем дополнительные неправильные варианты
            # Получаем случайные английские слова из всех доступных
            # Исправленный запрос: используем подзапрос без DISTINCT в ORDER BY
            cur.execute("""
                SELECT en FROM (
                    SELECT en FROM words 
                    UNION 
                    SELECT en FROM user_words WHERE user_id = %s
                ) AS all_translations 
                WHERE en != %s 
                ORDER BY RANDOM() 
                LIMIT 3;
            """, (user_id, correct_en))
            wrong_options = [row[0] for row in cur.fetchall()]

            # Если недостаточно неправильных вариантов, дополняем из общей таблицы
            if len(wrong_options) < 3:
                cur.execute("""
                    SELECT en 
                    FROM words 
                    WHERE en != %s 
                    ORDER BY RANDOM() 
                    LIMIT %s;
                """, (correct_en, 3 - len(wrong_options)))
                additional_options = [row[0] for row in cur.fetchall()]
                wrong_options.extend(additional_options)

            # Удаляем возможные дубликаты и оставляем максимум 3 варианта
            wrong_options = list(set(wrong_options))[:3]

            # Добавляем правильный вариант
            options = wrong_options + [correct_en]
            random.shuffle(options)

            return ru, correct_en, options


# Проверка правильности ответа
def check_answer(selected, correct):
    """Проверяет правильность ответа."""
    return selected.strip().lower() == correct.strip().lower()


# Обработка команды /start
@bot.message_handler(commands=['start'])
def handle_start(message):
    """Обработчик команды /start."""
    # Очищаем историю использованных слов при старте новой сессии
    used_words[message.chat.id].clear()

    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row(COMMAND_NEXT_WORD)
    keyboard.row(COMMAND_ADD_WORD, COMMAND_DELETE_WORD)
    bot.send_message(
        message.chat.id,
        "Добро пожаловать! Начнем изучение английских слов.",
        reply_markup=keyboard
    )
    handle_next_word(message)


# Основной обработчик сообщений
@bot.message_handler(func=lambda m: True)
def handle_message(m):
    """Основной обработчик сообщений."""
    state = user_states.get(m.chat.id, {'state': STATE_NONE})
    if state['state'] == STATE_AWAITING_RU:
        handle_ru_message(m)
    elif state['state'] == STATE_AWAITING_EN:
        handle_en_message(m)
    elif state['state'] == STATE_AWAITING_DELETE:
        handle_delete_word_confirm(m)
    else:
        text = m.text.strip()
        if text == COMMAND_NEXT_WORD:
            handle_next_word(m)
        elif text == COMMAND_ADD_WORD:
            handle_add_word(m)
        elif text == COMMAND_DELETE_WORD:
            handle_delete_word(m)


# Обработка нажатия кнопок
@bot.callback_query_handler(func=lambda c: True)
def handle_callback(c):
    """Обработчик нажатия кнопок."""
    data = c.data
    if data == 'next_word':
        handle_next_word(c.message)
    elif data == 'add_word':
        handle_add_word(c.message)
    elif data == 'delete_word':
        handle_delete_word(c.message)
    elif data.startswith('answer_'):
        handle_answer(c)


# Отправка следующего слова для изучения
def handle_next_word(message):
    """Отправляет следующее слово для изучения."""
    user_id = get_or_create_user_id(message.chat.id)
    word_info = select_random_word_with_options(user_id, message.chat.id)
    if not word_info:
        bot.send_message(message.chat.id, "Нет слов в базе данных.")
        return
    ru, correct_en, options = word_info
    last_word[message.chat.id] = (ru, correct_en)
    keyboard = types.InlineKeyboardMarkup()
    buttons = [
        types.InlineKeyboardButton(text=opt, callback_data=f"answer_{opt}")
        for opt in options
    ]
    # Расположение кнопок по две
    for i in range(0, len(buttons), 2):
        keyboard.row(*buttons[i:i + 2])
    msg = bot.send_message(
        message.chat.id,
        f"Переведите слово: 🇷🇺 {ru}",
        reply_markup=keyboard
    )
    last_messages[message.chat.id] = msg


# Обработка выбора варианта перевода
def handle_answer(c):
    """Обрабатывает выбранный вариант перевода."""
    selected_en = c.data.split('_')[1]
    ru, correct_en = last_word.get(c.message.chat.id, ("", ""))
    is_correct = check_answer(selected_en, correct_en)
    user_id = get_or_create_user_id(c.message.chat.id)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO answer_log (user_id, ru, correct_en,
                                        chosen_en, is_correct)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (user_id, ru, correct_en, selected_en, is_correct)
            )
            conn.commit()

    # Подготовка текста с результатом
    result_text = f"Переведите слово: 🇷🇺 {ru}\n\n"
    if is_correct:
        result_text += "✅ Правильно! ❤️"
    else:
        result_text += f"❌ Неправильно! 🔥🔥🔥\nПравильный ответ: {correct_en}"

    # Обновление сообщения, убираем клавиатуру
    try:
        bot.edit_message_text(
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            text=result_text,
            reply_markup=None
        )
    except Exception as e:
        print(f"Ошибка при изменении текста сообщения: {e}")
        # Отправляем отдельное сообщение, если обновление не сработало
        bot.send_message(c.message.chat.id, result_text)

    # Сообщение автоматически удаляется через 1 секунду
    def delete_message_after_delay():
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")

    # Запускаем таймер удаления сообщения через 1 секунду
    timer = threading.Timer(1.0, delete_message_after_delay)
    timer.start()

    # Переходим к новому слову через 1.5 секунды
    sleep(1.5)
    handle_next_word(c.message)


# Запрос ввода нового русского слова
def handle_add_word(message):
    """Запрашивает ввод нового русского слова."""
    user_states[message.chat.id] = {'state': STATE_AWAITING_RU}
    bot.send_message(message.chat.id, "Введите русское слово:")


# Запоминает введенное русское слово и запрашивает перевод
def handle_ru_message(message):
    """Запоминает введённое русское слово и запрашивает перевод."""
    user_states[message.chat.id]['ru'] = message.text.strip()
    user_states[message.chat.id]['state'] = STATE_AWAITING_EN
    bot.send_message(message.chat.id, "Введите английский перевод слова:")


# Добавление нового слова в базу данных
def handle_en_message(message):
    """Добавляет новое слово в базу данных."""
    state = user_states.get(message.chat.id, {})
    ru_word = state.get('ru')
    en_word = message.text.strip()
    if not ru_word or not en_word:
        bot.send_message(
            message.chat.id,
            "Ошибка: слово не может быть пустым."
        )
        user_states.pop(message.chat.id, None)
        return
    user_id = get_or_create_user_id(message.chat.id)
    add_word_to_user_words(user_id, ru_word, en_word)
    bot.send_message(
        message.chat.id,
        f"Слово '{ru_word}' с переводом '{en_word}' "
        "добавлено в ваши персональные слова!"
    )
    user_states.pop(message.chat.id, None)
    clear_screen(message.chat.id)


# Предложение удалить слово из списка персональных слов
def handle_delete_word(message):
    """Предлагает выбрать слово для удаления из списка персональных слов."""
    user_id = get_or_create_user_id(message.chat.id)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ru FROM user_words WHERE user_id=%s;",
                (user_id,)
            )
            rows = cur.fetchall()
            if rows:
                words_list = "\n".join(row[0] for row in rows)
                bot.send_message(
                    message.chat.id,
                    f"Ваши слова:\n{words_list}\n\n"
                    "Введите слово для удаления:",
                )
                user_states[message.chat.id] = {
                    'state': STATE_AWAITING_DELETE
                }
            else:
                bot.send_message(
                    message.chat.id,
                    "У вас нет слов для удаления."
                )


# Удаление указанного слова из личной коллекции пользователя
def handle_delete_word_confirm(message):
    """Подтверждает удаление указанного слова из личной коллекции."""
    user_id = get_or_create_user_id(message.chat.id)
    word_to_delete = message.text.strip()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_words WHERE user_id=%s AND ru=%s;",
                (user_id, word_to_delete)
            )
            deleted = cur.rowcount
            conn.commit()
    if deleted > 0:
        bot.send_message(
            message.chat.id,
            f"Слово '{word_to_delete}' удалено."
        )
    else:
        bot.send_message(message.chat.id, "Слово не найдено.")
    user_states.pop(message.chat.id, None)
    clear_screen(message.chat.id)


# Очистка экрана путем удаления последнего сообщения
def clear_screen(chat_id):
    """Удаляет последнее сообщение пользователя."""
    old_msg = last_messages.get(chat_id)
    if old_msg:
        try:
            bot.delete_message(chat_id, old_msg.message_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщения: {e}")
        del last_messages[chat_id]


# Основная точка входа
if __name__ == '__main__':
    init_db()
    print("Бот запущен")
    bot.infinity_polling()