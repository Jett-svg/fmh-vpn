import logging
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, \
    filters, ConversationHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
import psycopg2
import os
from datetime import datetime, timedelta
from flask import Flask
import threading

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ (УБРАЛ ДЕБАГ) ==========
logging.basicConfig(
    level=logging.INFO,  # ← ИЗМЕНИЛ С DEBUG НА INFO
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Отключаем лишние логи от библиотек
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
logging.getLogger('telegram').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ========== FLASK ДЛЯ RENDER ==========
flask_app = Flask(__name__)


@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "Bot is running!", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False)  # ← ВЫКЛЮЧИЛ ДЕБАГ


threading.Thread(target=run_web, daemon=True).start()


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
        logger.info("✅ Подключение к БД успешно")
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
        logger.info(f"📊 Получены данные для user_id={user_id}")
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка get_user для {user_id}: {e}")
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
        logger.error(f"❌ Ошибка create_user для {user_id}: {e}")


def mark_user_as_old(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET first_time = 0 WHERE user_id = %s', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"✅ user_id={user_id} помечен как старый")
    except Exception as e:
        logger.error(f"❌ Ошибка mark_user_as_old для {user_id}: {e}")


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
        logger.error(f"❌ Ошибка update_user_phone для {user_id}: {e}")
        return False


def update_user_subscription(user_id, days):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        end_date = datetime.now() + timedelta(days=days)
        c.execute('UPDATE users SET subscription_end = %s WHERE user_id = %s', (end_date, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Подписка обновлена для {user_id} на {days} дней до {end_date}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка update_user_subscription: {e}")
        return False


init_db()

BOT_TOKEN = os.environ.get('BOT_TOKEN', '8934659898:AAFjnr0OwI5gV3eV05drid5EnsBrCWGV67c')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '-5119832795'))
PAYMENT_PHONE = 1

def add_bonus(user_id, amount):
    """Начисляет бонусы пользователю"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET bonus_balance = bonus_balance + %s WHERE user_id = %s', (amount, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Начислено {amount} бонусов пользователю {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка начисления бонусов: {e}")
        return False

def get_user_by_id(user_id):
    """Получает пользователя по ID"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        conn.close()
        return result
    except Exception as e:
        logger.error(f"❌ Ошибка get_user_by_id: {e}")
        return None


def process_payment(user_id, amount):
    """Обработка оплаты и начисление бонусов рефереру"""
    try:
        # Начисляем бонусы рефереру (20%)
        conn = get_db_connection()
        c = conn.cursor()

        # Получаем реферера
        c.execute('SELECT referred_by FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()

        if result and result[0]:
            referrer_id = result[0]
            bonus = int(amount * 0.2)  # 20% от платежа
            c.execute('UPDATE users SET bonus_balance = bonus_balance + %s WHERE user_id = %s', (bonus, referrer_id))
            logger.info(f"💰 Начислено {bonus} бонусов рефереру {referrer_id} за платеж {user_id}")

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка process_payment: {e}")
        return False


def payment_tariff_menu_with_bonus(user_id):
    """Меню тарифов с учетом бонусов"""
    user_data = get_user(user_id)
    bonus = user_data[3] if user_data else 0

    keyboard = [
        [InlineKeyboardButton(f"📱 Simple — 249 ₽ (с бонусами: {max(0, 249 - bonus)} ₽)",
                              callback_data="pay_tariff_simple")],
        [InlineKeyboardButton("🚀 Pro — 499 ₽", callback_data="pay_tariff_pro")],
        [InlineKeyboardButton("💸 Использовать бонусы", callback_data="use_bonus")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def use_bonus_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик использования бонусов"""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user_data = get_user(user_id)
    bonus = user_data[3] if user_data else 0

    if bonus >= 249:
        # Списываем бонусы и активируем подписку
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET bonus_balance = bonus_balance - 249, subscription_end = %s WHERE user_id = %s',
                  (datetime.now() + timedelta(days=30), user_id))
        conn.commit()
        conn.close()

        await update.effective_chat.send_message(
            f"✅ Подписка активирована за бонусы! Списано 249 бонусов.\n"
            f"Остаток бонусов: {bonus - 249} ₽",
            reply_markup=main_menu()
        )
    else:
        await update.effective_chat.send_message(
            f"❌ Недостаточно бонусов. У вас {bonus} ₽, нужно 249 ₽",
            reply_markup=back_button()
        )


# ========== КНОПКИ ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton("💸 Оплата", callback_data="payment")],
        [InlineKeyboardButton("🌐 Подключиться", callback_data="connect")],
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="referral")],
        [InlineKeyboardButton("📞 Поддержка", callback_data="help")],
        [InlineKeyboardButton("⌯⌲ Наш канал", callback_data="show_channel")]
    ]
    return InlineKeyboardMarkup(keyboard)


def back_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]])


def welcome_menu():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁 Активировать пробный период", callback_data="activate_trial")]])


def trial_activated_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Активировать", callback_data="activate")]])


def skip_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭️ Пропустить", callback_data="skip_phone")]
    ])


def payment_tariff_menu():
    keyboard = [
        [InlineKeyboardButton("📱 Simple — 249 ₽", callback_data="pay_tariff_simple")],
        [InlineKeyboardButton("🚀 Pro — 499 ₽", callback_data="pay_tariff_pro")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def payment_plan_menu(tariff):
    prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}
    keyboard = [
        [InlineKeyboardButton(f"1 месяц — {prices[tariff]['1']} ₽", callback_data=f"pay_plan_{tariff}_1")],
        [InlineKeyboardButton(f"3 месяца — {prices[tariff]['3']} ₽", callback_data=f"pay_plan_{tariff}_3")],
        [InlineKeyboardButton(f"6 месяцев — {prices[tariff]['6']} ₽", callback_data=f"pay_plan_{tariff}_6")],
        [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def payment_method_menu():
    keyboard = [
        [InlineKeyboardButton("🏦 СБП", callback_data="pay_method_sbp")],
        [InlineKeyboardButton("₿ Криптовалюта", callback_data="pay_method_crypto")],
        [InlineKeyboardButton("💳 Банковская карта", callback_data="pay_method_card")],
        [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def payment_processing_menu(method):
    texts = {
        "sbp": "🏦 **Оплата через СБП**\n\nПереведите сумму на номер телефона:\n`+7 999 123-45-67`\n\nПосле оплаты нажмите кнопку «Проверить оплату».",
        "crypto": "₿ **Оплата криптовалютой**\n\nОтправьте USDT (TRC20) на адрес:\n`TXYZ...`\n\nПосле оплаты нажмите кнопку «Проверить оплату».",
        "card": "💳 **Оплата банковской картой**\n\nПерейдите по ссылке для оплаты:\n🔗 [Оплатить картой](https://example.com/pay)\n\nПосле оплаты нажмите кнопку «Проверить оплату»."
    }
    keyboard = [
        [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton("🔙 Назад", callback_data="payment_methods_back")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return texts.get(method, "Способ оплаты не найден"), InlineKeyboardMarkup(keyboard)


# ========== ОТПРАВКА СООБЩЕНИЙ ==========
async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message=None):
    image_path = 'FMH-VPN.jpg'
    caption = "👋 Привет! Это FMH_VPN.\n\nВыбери действие:"
    if message:
        await message.delete()
    if os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            await update.effective_chat.send_photo(photo=InputFile(photo), caption=caption, reply_markup=main_menu())
    else:
        await update.effective_chat.send_message(text=caption, reply_markup=main_menu())


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    create_user(update.effective_user.id)
    caption = "👋 Привет!\n\nЕсли вы устали от лагающих и не работающих VPN — тогда вы по адресу.\n\nЧтобы проверить, насколько мы хороши, дарим тебе пробный период на 3 дня."
    if os.path.exists('FMH-VPN.jpg'):
        with open('FMH-VPN.jpg', 'rb') as photo:
            await update.message.reply_photo(photo=InputFile(photo), caption=caption, reply_markup=welcome_menu())
    else:
        await update.message.reply_text(text=caption, reply_markup=welcome_menu())


async def send_service_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "📌 О нашем сервисе:\n\n✅ Неограниченная скорость и безлимитный трафик\n✅ До 3-х подключаемых устройств в одной подписке\n✅ Совместимость со всеми устройствами\n✅ Возможность заходить в российские приложения и банки даже с выключенным VPN, никаких ограничений\n✅ Имеем резервные сервера на случай сбоя основных — а это значит, вы никогда не останетесь без VPN\n\nГарантируем вам работу на всей территории России и со всеми операторами мобильной связи.\n\n💸 Стоимость после пробного периода:\n• 249 ₽/месяц (Simple подписка)\n• 499 ₽/месяц (Pro подписка)"
    await update.effective_chat.send_message(text=text, reply_markup=trial_activated_menu())


async def send_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = get_user(update.effective_user.id)
    if not user_data:
        await update.effective_chat.send_message("⚠️ Вы не зарегистрированы. Напишите /start для регистрации.",
                                                 reply_markup=back_button())
        return

    subscription_end, devices, max_devices, bonus_balance, phone, _ = user_data

    # ПРОВЕРЯЕМ ПРАВИЛЬНО
    if subscription_end:
        # Если subscription_end - это datetime объект
        if isinstance(subscription_end, datetime):
            end_date = subscription_end
        # Если это строка - конвертируем
        elif isinstance(subscription_end, str):
            end_date = datetime.fromisoformat(subscription_end)
        else:
            end_date = None

        if end_date and end_date > datetime.now():
            days_left = (end_date - datetime.now()).days
            hours_left = (end_date - datetime.now()).seconds // 3600
            status = "✅ Активна"
            if days_left > 0:
                end_text = f"до {end_date.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
            else:
                end_text = f"до {end_date.strftime('%d.%m.%Y')} (осталось {hours_left} ч.)"
        else:
            status = "❌ Истекла"
            end_text = f"истекла {end_date.strftime('%d.%m.%Y') if end_date else 'давно'}"
    else:
        status = "❌ Не активна"
        end_text = "нет активной подписки"

    text = (
        f"👤 **Личный кабинет**\n\n"
        f"📅 **Статус подписки:** {status}\n"
        f"📆 **Окончание:** {end_text}\n\n"
        f"📱 **Устройства:** {devices} из {max_devices} использовано\n"
        f"✅ **Свободно:** {max_devices - devices} устройств\n\n"
        f"💰 **Бонусный счёт:** {bonus_balance} ₽\n"
        f"📞 **Телефон:** {phone or 'Не указан'}"
    )

    await update.effective_chat.send_message(text=text, parse_mode='Markdown', reply_markup=back_button())


# ========== ОПЛАТА С ЗАПРОСОМ НОМЕРА ==========
# ========== ОПЛАТА С ЗАПРОСОМ НОМЕРА ==========
async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("🔴🔴🔴 НАЖАТА КНОПКА ОПЛАТА!")
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    logger.info(f"🔴 user_id={user_id}")

    user_data = get_user(user_id)

    if user_data and user_data[4]:  # номер уже есть
        # УБИРАЕМ УДАЛЕНИЕ СООБЩЕНИЯ
        # await query.message.delete()  ← ЗАКОММЕНТИЛИ

        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu()
        )
        return ConversationHandler.END

    logger.info("❌ Номера нет, запрашиваем ввод")

    # УБИРАЕМ УДАЛЕНИЕ СООБЩЕНИЯ
    # await query.message.delete()  ← ЗАКОММЕНТИЛИ

    await update.effective_chat.send_message(
        "📱 Для оформления подписки укажите ваш номер телефона.\n"
        "Это необязательно, но поможет нам связаться с вами.\n\n"
        "Отправьте номер в формате: +7XXXXXXXXXX\n"
        "Или нажмите «Пропустить».",
        reply_markup=skip_button()
    )
    return PAYMENT_PHONE


async def skip_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("⏭️ ПРОПУСТИТЬ НОМЕР")
    query = update.callback_query
    await query.answer()

    # УБИРАЕМ УДАЛЕНИЕ СООБЩЕНИЯ
    # await query.message.delete()  ← ЗАКОММЕНТИЛИ

    await update.effective_chat.send_message(
        "💸 **Выберите тариф:**",
        parse_mode='Markdown',
        reply_markup=payment_tariff_menu()
    )
    return ConversationHandler.END


async def payment_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("🔴🔴🔴🔴🔴 PAYMENT_PHONE ВЫЗВАН! 🔴🔴🔴🔴🔴")
    phone = update.message.text.strip()
    user_id = update.effective_user.id

    logger.info(f"📞 Получен номер: {phone} от user_id: {user_id}")


    if not phone.startswith('+') or len(phone) < 10:
        logger.warning(f"❌ Неверный формат номера: {phone}")
        await update.message.reply_text(
            "❌ Неверный формат номера. Отправьте номер в формате: +7XXXXXXXXXX\n"
            "Или нажмите «Пропустить».",
            reply_markup=skip_button()
        )
        return PAYMENT_PHONE

    logger.info(f"💾 Сохраняем номер {phone} для user_id={user_id}")
    success = update_user_phone(user_id, phone)

    if success:
        logger.info("✅ Номер успешно сохранен в БД")
        await update.message.reply_text("✅ Номер сохранён!")

        logger.info("📋 Показываем меню тарифов")
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu()
        )
    else:
        logger.error("❌ Ошибка сохранения номера в БД")
        await update.message.reply_text(
            "❌ Ошибка сохранения номера. Попробуйте позже или нажмите «Пропустить».",
            reply_markup=skip_button()
        )
        return PAYMENT_PHONE

    logger.info("🏁 Завершаем диалог")
    return ConversationHandler.END


async def skip_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("⏭️ ПРОПУСТИТЬ НОМЕР")
    query = update.callback_query
    await query.answer()
    await query.message.delete()

    await update.effective_chat.send_message(
        "💸 **Выберите тариф:**",
        parse_mode='Markdown',
        reply_markup=payment_tariff_menu()
    )
    return ConversationHandler.END


# ========== РЕФЕРАЛКА ==========
async def referral_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data and user_data[4]:  # номер есть
        # Создаем реферальную ссылку
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

        text = (
            f"👥 **Реферальная программа**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**\n\n"  # user_data[3] - bonus_balance
            f"📨 **Ваша реферальная ссылка:**\n`{ref_link}`\n\n"
            f"🔥 **Как это работает:**\n"
            f"• Приглашайте друзей по вашей ссылке\n"
            f"• Когда друг оформит подписку, вы получите **20%** от его платежа\n"
            f"• Бонусы можно тратить на подписку или выводить\n\n"
            f"💸 **Вывод бонусов:**\n"
            f"• Минимальная сумма вывода: **500 ₽**\n"
            f"• Вывод осуществляется каждый месяц\n\n"
            f"📊 **Статистика:**\n"
            f"• Приглашено: {get_referral_count(user_id)} человек\n"
            f"• Заработано: {get_total_earnings(user_id)} ₽"
        )
        await update.effective_chat.send_message(text, parse_mode='Markdown', reply_markup=back_button())
    else:
        await update.effective_chat.send_message(
            "👥 **Реферальная программа**\n\n"
            "Для участия в реферальной программе необходимо указать номер телефона.\n"
            "Вы можете сделать это при оформлении подписки через кнопку «💸 Оплата».\n\n"
            "После указания номера вам станут доступны реферальные бонусы.",
            parse_mode='Markdown',
            reply_markup=back_button()
        )


def get_referral_count(user_id):
    """Считает сколько людей пригласил пользователь"""
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


def get_total_earnings(user_id):
    """Считает сколько заработал пользователь (нужно добавить таблицу транзакций)"""
    # Пока просто возвращаем бонусный баланс
    user = get_user(user_id)
    return user[3] if user else 0


# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Проверяем, есть ли реферальный параметр
    referrer_id = None
    if context.args:
        try:
            # Формат: /start ref_123456789
            if context.args[0].startswith('ref_'):
                referrer_id = int(context.args[0].split('_')[1])
                logger.info(f"🔗 Реферальная ссылка от {referrer_id} для {user_id}")
        except:
            pass

    # Проверяем, существует ли пользователь в БД
    existing_user = get_user(user_id)

    # ЕСЛИ ПОЛЬЗОВАТЕЛЯ НЕТ В БД - создаем и проверяем рефералку
    if existing_user is None:
        # 1. Создаем пользователя
        create_user(user_id)

        # 2. Проверяем реферера
        if referrer_id and referrer_id != user_id:
            try:
                conn = get_db_connection()
                c = conn.cursor()

                # Проверяем, существует ли реферер в БД и есть ли у него номер
                c.execute('SELECT user_id, phone FROM users WHERE user_id = %s', (referrer_id,))
                referrer_data = c.fetchone()

                if referrer_data:
                    referrer_phone = referrer_data[1]

                    # Проверяем, есть ли у реферера номер телефона
                    if referrer_phone and referrer_phone.strip():
                        # Все проверки пройдены - сохраняем реферера
                        c.execute('UPDATE users SET referred_by = %s WHERE user_id = %s',
                                  (referrer_id, user_id))
                        conn.commit()
                        logger.info(f"✅ Пользователь {user_id} приглашен {referrer_id}")
                    else:
                        logger.warning(f"⚠️ Реферер {referrer_id} не указал номер телефона")
                else:
                    logger.warning(f"⚠️ Реферер {referrer_id} не найден в БД")

                conn.close()
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения реферера: {e}")

        # Пользователь новый - показываем приветствие
        user_data = get_user(user_id)
        if user_data and user_data[5] == 1:
            mark_user_as_old(user_id)
            await send_welcome(update, context)

    else:
        # ПОЛЬЗОВАТЕЛЬ УЖЕ СУЩЕСТВУЕТ
        logger.info(f"ℹ️ Пользователь {user_id} уже существует")

        # Если пользователь уже есть, но first_time = 1 (почему-то не помечен старым)
        if existing_user[5] == 1:
            mark_user_as_old(user_id)
            await send_welcome(update, context)

        # ЕСЛИ ПОДПИСКА АКТИВНА
        elif existing_user[0] and existing_user[0] > datetime.now():
            await send_main_menu(update, context)  # ПРОСТО ГЛАВНОЕ МЕНЮ

        # ИНАЧЕ - ГЛАВНОЕ МЕНЮ
        else:
            await send_main_menu(update, context)  # ПРОСТО ГЛАВНОЕ МЕНЮ


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    logger.info(f"🔄 Нажата кнопка: {data}")

    if data == "help":
        context.user_data['support_mode'] = True
        await update.effective_chat.send_message("📞 Напишите сообщение поддержке, постараемся ответить оперативно",
                                                 reply_markup=back_button())

    elif data == "skip_phone":
        return await skip_phone_handler(update, context)

    elif data == "payment_tariff_back":
        await update.effective_chat.send_message("💸 **Выберите тариф:**", parse_mode='Markdown',
                                                 reply_markup=payment_tariff_menu())

    elif data == "payment_methods_back":
        tariff = context.user_data.get('selected_tariff', 'simple')
        await update.effective_chat.send_message(f"💳 **Выберите способ оплаты для тарифа {tariff.capitalize()}:**",
                                                 parse_mode='Markdown', reply_markup=payment_method_menu())

    elif data == "pay_tariff_simple":
        context.user_data['selected_tariff'] = 'simple'
        await update.effective_chat.send_message("📱 **Тариф Simple**\n\nВыберите срок подписки:", parse_mode='Markdown',
                                                 reply_markup=payment_plan_menu('simple'))

    elif data == "pay_tariff_pro":
        await update.effective_chat.send_message(
            "🚀 **Тариф Pro**\n\n"
            "К сожалению, этот тариф пока находится в разработке.\n"
            "Следите за обновлениями!",
            parse_mode='Markdown',
            reply_markup=back_button()
        )

    elif data.startswith("pay_plan_"):
        parts = data.split("_")
        tariff, months = parts[2], parts[3]
        prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}
        price = prices[tariff][months]
        context.user_data['selected_plan'] = int(months)
        await update.effective_chat.send_message(
            f"✅ Вы выбрали **{tariff.capitalize()}** на {months} месяц(ев).\n💰 Сумма: {price} ₽\n\nВыберите способ оплаты:",
            parse_mode='Markdown', reply_markup=payment_method_menu())

    elif data.startswith("pay_method_"):
        text, keyboard = payment_processing_menu(data.split("_")[2])
        await update.effective_chat.send_message(text, parse_mode='Markdown', reply_markup=keyboard)

    elif data == "check_payment":
        await update.effective_chat.send_message(
            "⏳ Проверяем оплату...\n\nЕсли оплата прошла, подписка будет активирована в течение 5 минут.",
            reply_markup=back_button())

    elif data == "connect":
        await update.effective_chat.send_message(
            "🌐 Подключение временно недоступно.\nПожалуйста, оплатите подписку через кнопку «💸 Оплата».",
            reply_markup=back_button())

    elif data == "activate_trial":
        update_user_subscription(update.effective_user.id, 3)
        await send_service_info(update, context)

    elif data == "activate":
        await update.effective_chat.send_message(
            "✅ Поздравляем! Вы активировали пробный период на 3 дня.\n\nТеперь вы можете пользоваться нашим VPN без ограничений.\nНаслаждайтесь! 🚀",
            reply_markup=main_menu())

    elif data == 'show_channel':
        await update.effective_chat.send_message('Ссылка на канал:\nhttps://t.me/FMH_VPN')

    elif data == "profile":
        await send_profile(update, context)

    elif data == "referral":
        await referral_start(update, context)

    elif data == "main_menu":
        await send_main_menu(update, context)


# ========== ПОДДЕРЖКА ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📨 Получено сообщение: {update.message.text}")

    # Проверяем, не находимся ли мы в диалоге оплаты
    if context.user_data.get('state') == PAYMENT_PHONE:
        logger.info("🔴 Сообщение перенаправляется в payment_phone")
        return await payment_phone(update, context)

    if context.user_data.get('support_mode'):
        user = update.effective_user
        text = update.message.text
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID,
                                       text=f"📩 Новое обращение от {user.first_name} (@{user.username or 'нет username'}):\n\n{text}")
        await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Мы ответим вам в ближайшее время.")
        context.user_data['support_mode'] = False


# ========== ЗАПУСК ==========
def main():
    logger.info("🚀 Запуск бота...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    payment_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(payment_start, pattern="^payment$")],
        states={
            PAYMENT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_phone),
                CallbackQueryHandler(skip_phone_handler, pattern="^skip_phone$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
    )
    app.add_handler(payment_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Бот готов! Теперь ТЫКНИ кнопку в телеграме и смотри что в консоли")
    app.run_polling()


if __name__ == '__main__':
    main()