import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor
import psycopg2
import os
from datetime import datetime, timedelta
import logging

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== КОНФИГУРАЦИЯ ==========
VK_TOKEN = 'vk1.a.O98byUHRSkc1sBsUWF-ckq3NIXoRBPk1P81TGvrEYWhPNATTnob-2GcLBF30FgY2RNbfmzRINVdz3LXPqjghli9bQFymL81u9MkAYxzKSfrG1ht_0IZlGDOMFtao3GvQrP6F1ctojgn1SRaPfXOxHsXSEq1NVefPPcNTkv6TxOGLpJoj_13P7Wtkw8RD73Eow9vUFcldkJUI9Ig02PenJA'
GROUP_ID = 240002099


# ========== ПОДКЛЮЧЕНИЕ К POSTGRESQL ==========
def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=os.environ.get('DB_HOST', 'localhost'),
            database=os.environ.get('DB_NAME', 'fmh'),
            user=os.environ.get('DB_USER', 'postgres'),
            password=os.environ.get('DB_PASSWORD', '255zhh4j'),
            port=os.environ.get('DB_PORT', 5432)
        )
        return conn
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к БД: {e}")
        raise


# ========== БАЗА ДАННЫХ ==========
def init_db():
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                subscription_end TIMESTAMP,
                devices INTEGER DEFAULT 0,
                max_devices INTEGER DEFAULT 3,
                bonus_balance INTEGER DEFAULT 0,
                phone TEXT,
                first_time INTEGER DEFAULT 1,
                referred_by BIGINT DEFAULT NULL
            )
        ''')
        conn.commit()
        conn.close()
        logger.info("✅ Таблица users создана/проверена")
    except Exception as e:
        logger.error(f"❌ Ошибка init_db: {e}")


def get_user(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT subscription_end, devices, max_devices, bonus_balance, phone, first_time FROM users WHERE user_id = %s',
            (user_id,))
        result = c.fetchone()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка get_user: {e}")
        return None


def create_user(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO users (user_id, subscription_end, devices, max_devices, bonus_balance, phone, first_time)
            VALUES (%s, NULL, 0, 3, 0, NULL, 1)
            ON CONFLICT (user_id) DO NOTHING
        ''', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"✅ Пользователь {user_id} создан")
    except Exception as e:
        logger.error(f"❌ Ошибка create_user: {e}")


def mark_user_as_old(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET first_time = 0 WHERE user_id = %s', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"✅ user_id={user_id} помечен как старый")
    except Exception as e:
        logger.error(f"❌ Ошибка mark_user_as_old: {e}")


def update_user_subscription(user_id, days):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        end_date = datetime.now() + timedelta(days=days)
        c.execute('UPDATE users SET subscription_end = %s WHERE user_id = %s', (end_date, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Подписка обновлена для {user_id} на {days} дней")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка update_user_subscription: {e}")
        return False


def update_user_phone(user_id, phone):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET phone = %s WHERE user_id = %s', (phone, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ БД обновлена: user_id={user_id}, phone={phone}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка update_user_phone: {e}")
        return False


def get_referral_count(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users WHERE referred_by = %s', (user_id,))
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        logger.error(f"❌ Ошибка get_referral_count: {e}")
        return 0


init_db()


# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('👤 Личный кабинет', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('💸 Оплата', color=VkKeyboardColor.POSITIVE)
    keyboard.add_line()
    keyboard.add_button('🌐 Подключиться', color=VkKeyboardColor.SECONDARY)
    keyboard.add_button('🤝 Реферальная программа', color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button('📞 Поддержка', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('⌯⌲ Наш канал', color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


def get_back_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.PRIMARY)
    return keyboard.get_keyboard()


def get_welcome_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('🎁 Активировать пробный период', color=VkKeyboardColor.POSITIVE)
    return keyboard.get_keyboard()


def get_tariff_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('📱 Simple — 249 ₽', color=VkKeyboardColor.PRIMARY)
    keyboard.add_button('🚀 Pro — 499 ₽', color=VkKeyboardColor.PRIMARY)
    keyboard.add_line()
    keyboard.add_button('🔙 Назад', color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


# ========== ОТПРАВКА СООБЩЕНИЙ ==========
def send_message(vk, user_id, text, keyboard=None):
    """Отправляет сообщение пользователю"""
    try:
        params = {
            'user_id': user_id,
            'message': text,
            'random_id': 0
        }
        if keyboard:
            params['keyboard'] = keyboard

        vk.messages.send(**params)  # ← ИЗМЕНЕНО!
        logger.info(f"✅ Сообщение отправлено пользователю {user_id}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки сообщения: {e}")


# ========== ОБРАБОТЧИКИ ==========
def handle_start(vk, user_id, message_text):
    logger.info(f"🚀 start от user_id={user_id}")

    referrer_id = None
    if ' ' in message_text:
        parts = message_text.split(' ', 1)
        if parts[1].startswith('ref_'):
            try:
                referrer_id = int(parts[1].split('_')[1])
                logger.info(f"🔗 Реферальная ссылка от {referrer_id} для {user_id}")
            except:
                pass

    existing_user = get_user(user_id)

    if existing_user is None:
        create_user(user_id)

        if referrer_id and referrer_id != user_id:
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('SELECT user_id, phone FROM users WHERE user_id = %s', (referrer_id,))
                referrer_data = c.fetchone()
                if referrer_data and referrer_data[1] and referrer_data[1].strip():
                    c.execute('UPDATE users SET referred_by = %s WHERE user_id = %s', (referrer_id, user_id))
                    conn.commit()
                    logger.info(f"✅ Пользователь {user_id} приглашен {referrer_id}")
                conn.close()
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения реферера: {e}")

        user_data = get_user(user_id)
        if user_data and user_data[5] == 1:
            mark_user_as_old(user_id)
            text = (
                "👋 Привет!\n\n"
                "Если вы устали от лагающих и не работающих VPN — тогда вы по адресу.\n\n"
                "Чтобы проверить, насколько мы хороши, дарим тебе пробный период на 3 дня."
            )
            send_message(vk, user_id, text, get_welcome_keyboard())
    else:
        logger.info(f"ℹ️ Пользователь {user_id} уже существует")
        if existing_user[5] == 1:
            mark_user_as_old(user_id)
            text = (
                "👋 Привет!\n\n"
                "Если вы устали от лагающих и не работающих VPN — тогда вы по адресу.\n\n"
                "Чтобы проверить, насколько мы хороши, дарим тебе пробный период на 3 дня."
            )
            send_message(vk, user_id, text, get_welcome_keyboard())
        else:
            send_main_menu(vk, user_id)


def send_main_menu(vk, user_id):
    text = "👋 Привет! Это FMH_VPN.\n\nВыбери действие:"
    send_message(vk, user_id, text, get_main_keyboard())


def handle_profile(vk, user_id):
    user_data = get_user(user_id)
    if not user_data:
        send_message(vk, user_id, "⚠️ Вы не зарегистрированы. Напишите /start для регистрации.", get_back_keyboard())
        return

    subscription_end, devices, max_devices, bonus_balance, phone, _ = user_data

    if subscription_end:
        if isinstance(subscription_end, datetime):
            end_date = subscription_end
        elif isinstance(subscription_end, str):
            end_date = datetime.fromisoformat(subscription_end)
        else:
            end_date = None

        if end_date and end_date > datetime.now():
            days_left = (end_date - datetime.now()).days
            status = "✅ Активна"
            if days_left > 0:
                end_text = f"до {end_date.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
            else:
                hours_left = (end_date - datetime.now()).seconds // 3600
                end_text = f"до {end_date.strftime('%d.%m.%Y')} (осталось {hours_left} ч.)"
        else:
            status = "❌ Истекла"
            end_text = f"истекла {end_date.strftime('%d.%m.%Y') if end_date else 'давно'}"
    else:
        status = "❌ Не активна"
        end_text = "нет активной подписки"

    text = (
        f"👤 Личный кабинет\n\n"
        f"📅 Статус подписки: {status}\n"
        f"📆 Окончание: {end_text}\n\n"
        f"📱 Устройства: {devices} из {max_devices} использовано\n"
        f"✅ Свободно: {max_devices - devices} устройств\n\n"
        f"💰 Бонусный счёт: {bonus_balance} ₽\n"
        f"📞 Телефон: {phone or 'Не указан'}"
    )
    send_message(vk, user_id, text, get_back_keyboard())


def handle_payment_start(vk, user_id):
    user_data = get_user(user_id)

    if user_data and user_data[4]:
        text = "💸 Выберите тариф:"
        send_message(vk, user_id, text, get_tariff_keyboard())
        return

    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button('⏭️ Пропустить', color=VkKeyboardColor.SECONDARY)

    text = (
        "📱 Для оформления подписки укажите ваш номер телефона.\n"
        "Это необязательно, но поможет нам связаться с вами.\n\n"
        "Отправьте номер в формате: +7XXXXXXXXXX\n"
        "Или нажмите «Пропустить»."
    )
    send_message(vk, user_id, text, keyboard.get_keyboard())


def handle_phone_input(vk, user_id, phone):
    phone = phone.strip()

    if not phone.startswith('+') or len(phone) < 10:
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('⏭️ Пропустить', color=VkKeyboardColor.SECONDARY)
        text = "❌ Неверный формат номера. Отправьте номер в формате: +7XXXXXXXXXX\nИли нажмите «Пропустить»."
        send_message(vk, user_id, text, keyboard.get_keyboard())
        return

    success = update_user_phone(user_id, phone)

    if success:
        send_message(vk, user_id, "✅ Номер сохранён!")
        text = "💸 Выберите тариф:"
        send_message(vk, user_id, text, get_tariff_keyboard())
    else:
        text = "❌ Ошибка сохранения номера. Попробуйте позже."
        send_message(vk, user_id, text)


def handle_referral(vk, user_id):
    user_data = get_user(user_id)

    if user_data and user_data[4]:
        ref_link = f"https://vk.com/clip{GROUP_ID}?ref=ref_{user_id}"
        text = (
            f"👥 Реферальная программа\n\n"
            f"💰 Ваш бонусный счет: {user_data[3]} ₽\n\n"
            f"📨 Ваша реферальная ссылка:\n{ref_link}\n\n"
            f"🔥 Как это работает:\n"
            f"• Приглашайте друзей по вашей ссылке\n"
            f"• Когда друг оформит подписку, вы получите 20% от его платежа\n"
            f"• Бонусы можно тратить на подписку или выводить\n\n"
            f"💸 Вывод бонусов:\n"
            f"• Минимальная сумма вывода: 500 ₽\n\n"
            f"📊 Статистика:\n"
            f"• Приглашено: {get_referral_count(user_id)} человек"
        )
        send_message(vk, user_id, text, get_back_keyboard())
    else:
        text = (
            "👥 Реферальная программа\n\n"
            "Для участия в реферальной программе необходимо указать номер телефона.\n"
            "Вы можете сделать это при оформлении подписки через кнопку «💸 Оплата».\n\n"
            "После указания номера вам станут доступны реферальные бонусы."
        )
        send_message(vk, user_id, text, get_back_keyboard())


def handle_activate_trial(vk, user_id):
    success = update_user_subscription(user_id, 3)
    if success:
        text = (
            "📌 О нашем сервисе:\n\n"
            "✅ Неограниченная скорость и безлимитный трафик\n"
            "✅ До 3-х подключаемых устройств в одной подписке\n"
            "✅ Совместимость со всеми устройствами\n"
            "✅ Возможность заходить в российские приложения и банки даже с выключенным VPN\n"
            "✅ Имеем резервные сервера на случай сбоя основных\n\n"
            "💸 Стоимость после пробного периода:\n"
            "• 249 ₽/месяц (Simple подписка)\n"
            "• 499 ₽/месяц (Pro подписка)"
        )
        keyboard = VkKeyboard(one_time=False)
        keyboard.add_button('✅ Активировать', color=VkKeyboardColor.POSITIVE)
        send_message(vk, user_id, text, keyboard.get_keyboard())


def handle_activate(vk, user_id):
    text = (
        "✅ Поздравляем! Вы активировали пробный период на 3 дня.\n\n"
        "Теперь вы можете пользоваться нашим VPN без ограничений.\nНаслаждайтесь! 🚀"
    )
    send_message(vk, user_id, text, get_main_keyboard())


def handle_connect(vk, user_id):
    text = "🌐 Подключение временно недоступно.\nПожалуйста, оплатите подписку через кнопку «💸 Оплата»."
    send_message(vk, user_id, text, get_back_keyboard())


def handle_help(vk, user_id):
    text = "📞 Напишите сообщение поддержке, постараемся ответить оперативно"
    send_message(vk, user_id, text, get_back_keyboard())


def handle_channel(vk, user_id):
    text = "Ссылка на канал:\nhttps://vk.com/fmh_vpn"
    send_message(vk, user_id, text, get_back_keyboard())


# ========== ОСНОВНАЯ ЛОГИКА ==========
def main():
    logger.info("🚀 Запуск бота ВКонтакте...")

    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()

    longpoll = VkBotLongPoll(vk_session, GROUP_ID)

    logger.info("✅ Бот готов к работе!")

    for event in longpoll.listen():
        if event.type == VkBotEventType.MESSAGE_NEW:
            user_id = event.object.message['from_id']
            text = event.object.message.get('text', '').strip().lower()

            logger.info(f"📨 Получено сообщение: {text} от user_id={user_id}")

            if text == '/start' or text == 'начать':
                handle_start(vk, user_id, text)
                continue

            if text == '👤 личный кабинет':
                handle_profile(vk, user_id)
            elif text == '💸 оплата':
                handle_payment_start(vk, user_id)
            elif text == '🌐 подключиться':
                handle_connect(vk, user_id)
            elif text == '🤝 реферальная программа':
                handle_referral(vk, user_id)
            elif text == '📞 поддержка':
                handle_help(vk, user_id)
            elif text == '⌯⌲ наш канал':
                handle_channel(vk, user_id)
            elif text == '🔙 назад':
                send_main_menu(vk, user_id)
            elif text == '🎁 активировать пробный период':
                handle_activate_trial(vk, user_id)
            elif text == '✅ активировать':
                handle_activate(vk, user_id)
            elif text in ['📱 simple — 249 ₽', '🚀 pro — 499 ₽']:
                text = (
                    "✅ Вы выбрали тариф.\n"
                    "💰 Сумма: 249 ₽\n\n"
                    "Оплата временно осуществляется через поддержку.\n"
                    "Напишите в поддержку для оформления."
                )
                send_message(vk, user_id, text, get_back_keyboard())
            elif text.startswith('+') and len(text) >= 10:
                handle_phone_input(vk, user_id, text)
            elif text == '⏭️ пропустить':
                text = "💸 Выберите тариф:"
                send_message(vk, user_id, text, get_tariff_keyboard())
            else:
                send_main_menu(vk, user_id)


if __name__ == '__main__':
    main()