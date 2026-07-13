import logging
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, \
    filters, ConversationHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
import psycopg2
import os
from datetime import datetime, timedelta
from flask import Flask
import threading
from yookassa import Configuration, Payment
import uuid
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timedelta
from telegram import Bot
import random
load_dotenv()

user_captcha = {}

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

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
    flask_app.run(host="0.0.0.0", port=port, debug=False)


threading.Thread(target=run_web, daemon=True).start()


# ========== ПОДКЛЮЧЕНИЕ К POSTGRESQL ==========
def get_db_connection():
    try:
        # Берем готовую строку подключения из переменной окружения DATABASE_URL
        conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
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
                tariff_type TEXT DEFAULT NULL,
                devices INTEGER DEFAULT 0,
                max_devices INTEGER DEFAULT 3,
                bonus_balance INTEGER DEFAULT 0,
                phone TEXT,
                first_time INTEGER DEFAULT 1,
                referred_by BIGINT DEFAULT NULL,
                captcha_passed BOOLEAN DEFAULT FALSE,
                bonus_paid BOOLEAN DEFAULT FALSE
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


def update_user_subscription(user_id, days, new_tariff):
    """
    Обновляет подписку пользователя.
    - Если тариф совпадает с текущим → продлеваем.
    - Если тариф отличается → заменяем (сгорает старая подписка).
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Получаем текущий тариф и дату окончания
        c.execute('SELECT tariff_type, subscription_end FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()

        current_tariff = result[0] if result else None
        current_end = result[1] if result else None

        # Проверяем, активна ли подписка
        is_active = current_end and current_end > datetime.now()

        if is_active and current_tariff == new_tariff:
            # 1. ТАРИФ СОВПАДАЕТ → продлеваем
            new_end_date = current_end + timedelta(days=days)
            logger.info(f"🔄 Продлеваем {new_tariff}: {current_end} + {days} дней = {new_end_date}")
        else:
            # 2. ТАРИФ ОТЛИЧАЕТСЯ ИЛИ ПОДПИСКА НЕАКТИВНА → заменяем
            new_end_date = datetime.now() + timedelta(days=days)
            logger.info(f"🆕 Заменяем {current_tariff or 'нет'} на {new_tariff} до {new_end_date}")

        # Обновляем БД
        c.execute('''
            UPDATE users 
            SET subscription_end = %s, tariff_type = %s 
            WHERE user_id = %s
        ''', (new_end_date, new_tariff, user_id))

        conn.commit()
        conn.close()
        logger.info(f"✅ Подписка обновлена для {user_id}: тариф={new_tariff}, до={new_end_date}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка update_user_subscription: {e}")
        return False


init_db()

BOT_TOKEN = os.environ.get('BOT_TOKEN', '8934659898:AAFjnr0OwI5gV3eV05drid5EnsBrCWGV67c')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '-5119832795'))
PAYMENT_PHONE = 1

# ========== НАСТРОЙКА ЮKASSA ==========
YOOKASSA_SHOP_ID = os.environ.get('YOOKASSA_SHOP_ID', '1401068')
YOOKASSA_SECRET_KEY = os.environ.get('YOOKASSA_SECRET_KEY', 'test_14IMRPYpmtUV9warhcIWTCwi_WkEOoqYV7_aVvv4njw')
YOOKASSA_TEST_MODE = True

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY


def create_yookassa_payment(user_id, amount, description="Оплата подписки на медиа контент", payment_type="bank_card"):
    try:
        idempotence_key = str(uuid.uuid4())

        payment_data = {
            "amount": {
                "value": str(amount),
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/fmh_vpn_bot"
            },
            "description": description,
            "metadata": {
                "user_id": str(user_id)
            },
            "capture": True  # ← ДОБАВЬ ЭТУ СТРОКУ! Это включает мгновенное списание
        }

        # Если СБП - добавляем payment_method_data
        if payment_type == "sbp":
            payment_data["payment_method_data"] = {
                "type": "sbp"
            }

        if YOOKASSA_TEST_MODE:
            payment_data["test"] = True

        payment = Payment.create(payment_data, idempotence_key)
        logger.info(f"💰 Платеж создан: {payment.id} для user_id={user_id}")

        return {
            'payment_id': payment.id,
            'confirmation_url': payment.confirmation.confirmation_url,
            'status': payment.status
        }
    except Exception as e:
        logger.error(f"❌ Ошибка создания платежа: {e}")
        return None


def check_payment_status(payment_id):
    try:
        payment = Payment.find_one(payment_id)
        return {
            'status': payment.status,
            'paid': payment.paid,
            'amount': payment.amount.value
        }
    except Exception as e:
        logger.error(f"❌ Ошибка проверки платежа: {e}")
        return None


def process_payment(user_id, amount):
    """Обработка оплаты и начисление бонусов рефереру (только за первую оплату)"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # 1. Проверяем, есть ли у пользователя реферер
        c.execute('SELECT referred_by FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()

        if result and result[0]:
            referrer_id = result[0]

            # 2. Проверяем, не начислялись ли уже бонусы этому пользователю
            c.execute('SELECT bonus_paid FROM users WHERE user_id = %s', (user_id,))
            bonus_paid = c.fetchone()[0]

            # 3. Если бонус ещё не начислен — начисляем
            if not bonus_paid:
                bonus = int(amount * 0.2)
                c.execute('UPDATE users SET bonus_balance = bonus_balance + %s WHERE user_id = %s',
                          (bonus, referrer_id))
                # 4. Помечаем, что бонус начислен
                c.execute('UPDATE users SET bonus_paid = TRUE WHERE user_id = %s', (user_id,))
                conn.commit()
                logger.info(f"💰 Начислено {bonus} бонусов рефереру {referrer_id} за первую оплату {user_id}")
            else:
                logger.info(f"ℹ️ Бонусы за {user_id} уже начислены рефереру {referrer_id}")

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка process_payment: {e}")
        return False


def process_successful_payment(user_id, payment_id, amount, tariff_type):
    """Обработка успешного платежа - активация подписки + бонусы рефереру"""
    try:
        # 30 дней для любого тарифа
        update_user_subscription(user_id, 30, tariff_type)
        process_payment(user_id, amount)
        logger.info(f"✅ Платеж {payment_id} обработан для user_id={user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка обработки платежа: {e}")
        return False

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


def payment_tariff_menu_with_bonus(user_id, selected_tariff=None, selected_plan=None):
    """Меню тарифов с учетом бонусов и частичной оплаты"""
    user_data = get_user(user_id)
    bonus = user_data[3] if user_data else 0

    prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}

    # Если тариф и план выбраны - показываем варианты оплаты
    if selected_tariff and selected_plan:
        price = prices[selected_tariff][str(selected_plan)]

        keyboard = []

        # Кнопка 1: Оплатить ВСЕ бонусами (если хватает)
        if bonus >= price:
            keyboard.append([InlineKeyboardButton(
                f"💰 Оплатить ВСЕ бонусами ({price} ₽)",
                callback_data=f"bonus_all_{selected_tariff}_{selected_plan}"
            )])

        # Кнопка 2: Частичная оплата (ввод суммы) - ВСЕГДА ЕСТЬ!
        if bonus > 0:
            keyboard.append([InlineKeyboardButton(
                f"🔄 Частично бонусами (есть {bonus} ₽)",
                callback_data=f"bonus_partial_input_{selected_tariff}_{selected_plan}"
            )])

        # Кнопка 3: Полностью деньгами
        keyboard.append([InlineKeyboardButton(
            f"💳 Полностью деньгами — {price} ₽",
            callback_data=f"pay_full_{selected_tariff}_{selected_plan}"
        )])

        keyboard.append([InlineKeyboardButton("🔙 К выбору срока", callback_data=f"back_to_plan_{selected_tariff}")])
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

        return InlineKeyboardMarkup(keyboard)

    # Если тариф не выбран - показываем выбор тарифа
    keyboard = [
        [InlineKeyboardButton(f"📱 Simple — от 249 ₽ (бонусов: {bonus} ₽)", callback_data="tariff_select_simple")],
        [InlineKeyboardButton(f"🚀 Pro — от 499 ₽ (бонусов: {bonus} ₽)", callback_data="tariff_select_pro")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def mark_captcha_passed(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET captcha_passed = TRUE WHERE user_id = %s', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"✅ КАПЧА ОТМЕЧЕНА ДЛЯ {user_id}")  # ← ДОБАВЬ ЭТУ СТРОКУ!
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка mark_captcha_passed: {e}")
        return False

def is_captcha_passed(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT captcha_passed FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        conn.close()
        return result and result[0]
    except Exception as e:
        logger.error(f"❌ Ошибка is_captcha_passed: {e}")
        return False

def generate_captcha():
    a = random.randint(1, 10)
    b = random.randint(1, 10)
    if random.choice([True, False]):
        return f"{a} + {b}", a + b
    a, b = max(a, b), min(a, b)
    return f"{a} - {b}", a - b


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

    if subscription_end:
        if isinstance(subscription_end, datetime):
            end_date = subscription_end
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


# ========== ОПЛАТА ==========
async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("🔴🔴🔴 НАЖАТА КНОПКА ОПЛАТА!")
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    logger.info(f"🔴 user_id={user_id}")

    user_data = get_user(user_id)

    if user_data and user_data[4]:
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu_with_bonus(user_id)
        )
        return ConversationHandler.END

    logger.info("❌ Номера нет, запрашиваем ввод")
    await update.effective_chat.send_message(
        "📱 Для оформления подписки укажите ваш номер телефона.\n"
        "Это необязательно, но поможет нам связаться с вами.\n\n"
        "Отправьте номер в формате: +7XXXXXXXXXX\n"
        "Или нажмите «Пропустить».",
        reply_markup=skip_button()
    )
    return PAYMENT_PHONE


async def payment_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("🔴🔴🔴🔴🔴 PAYMENT_PHONE ВЫЗВАН!")
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
        user_data = get_user(user_id)
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu_with_bonus(user_id)
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

    user_id = update.effective_user.id
    user_data = get_user(user_id)
    await update.effective_chat.send_message(
        "💸 **Выберите тариф:**\n\n"
        f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
        parse_mode='Markdown',
        reply_markup=payment_tariff_menu_with_bonus(user_id)
    )
    return ConversationHandler.END


# ========== РЕФЕРАЛКА ==========
async def referral_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data and user_data[4]:
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

        text = (
            f"👥 **Реферальная программа**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**\n\n"
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
    user = get_user(user_id)
    return user[3] if user else 0


# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    existing_user = get_user(user_id)

    # 1. Сначала обрабатываем реферальную ссылку (если есть)
    referrer_id = None
    if context.args:
        try:
            if context.args[0].startswith('ref_'):
                referrer_id = int(context.args[0].split('_')[1])
                logger.info(f"🔗 Реферальная ссылка от {referrer_id} для {user_id}")
        except:
            pass

    # 2. Если пользователь НОВЫЙ — создаем и сохраняем реферера
    if existing_user is None:
        create_user(user_id)

        # Сохраняем реферера (если есть)
        if referrer_id and referrer_id != user_id:
            try:
                conn = get_db_connection()
                c = conn.cursor()
                # Проверяем, что реферер существует
                c.execute('SELECT user_id, phone FROM users WHERE user_id = %s', (referrer_id,))
                referrer_data = c.fetchone()
                if referrer_data and referrer_data[1] and referrer_data[1].strip():
                    c.execute('UPDATE users SET referred_by = %s WHERE user_id = %s', (referrer_id, user_id))
                    conn.commit()
                    logger.info(f"✅ Пользователь {user_id} приглашен {referrer_id}")
                conn.close()
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения реферера: {e}")

        # Показываем капчу новому пользователю
        question, answer = generate_captcha()
        user_captcha[user_id] = {'answer': answer, 'attempts': 0}
        await update.message.reply_text(
            f"🤖 **Привет! Для продолжения решите пример:**\n\n**{question} = ?**\n\nВведите ответ числом.",
            parse_mode='HTML'
        )
        return

    # 3. Если пользователь уже есть, проверяем капчу
    if not is_captcha_passed(user_id):
        question, answer = generate_captcha()
        user_captcha[user_id] = {'answer': answer, 'attempts': 0}
        await update.message.reply_text(
            f"🤖 **Для продолжения решите пример:**\n\n**{question} = ?**\n\nВведите ответ числом.",
            parse_mode='HTML'
        )
        return

    # 4. Если капча пройдена — показываем меню
    if existing_user[5] == 1:
        mark_user_as_old(user_id)
        await send_welcome(update, context)
    else:
        await send_main_menu(update, context)


async def send_subscription_reminder(bot: Bot, user_id: int, days_left: int, end_date: datetime):
    """Отправляет напоминание пользователю"""
    try:
        # Сообщение для 3 дней
        if days_left == 3:
            text = (
                f"⚠️ **Напоминание!**\n\n"
                f"Ваша подписка на FMH_VPN закончится через **3 дня**.\n"
                f"📅 Дата окончания: {end_date.strftime('%d.%m.%Y')}\n\n"
                f"💸 Чтобы продлить подписку, нажмите кнопку «💸 Оплата» в меню бота.\n\n"
                f"Не дайте своему VPN уснуть! 🚀"
            )
        # Сообщение для 2 дней
        elif days_left == 2:
            text = (
                f"🔥 **Внимание!**\n\n"
                f"Ваша подписка на FMH_VPN закончится через **2 дня**!\n"
                f"📅 Дата окончания: {end_date.strftime('%d.%m.%Y')}\n\n"
                f"💸 Продлите подписку сейчас, чтобы не остаться без VPN.\n"
                f"Нажмите «💸 Оплата» в меню бота."
            )
        # Сообщение для 1 дня
        elif days_left == 1:
            text = (
                f"🚨 **Последний день!**\n\n"
                f"Ваша подписка на FMH_VPN заканчивается **ЗАВТРА**!\n"
                f"📅 Дата окончания: {end_date.strftime('%d.%m.%Y')}\n\n"
                f"💸 **Срочно продлите подписку!**\n"
                f"Нажмите «💸 Оплата» в меню бота.\n\n"
                f"Если не продлите, VPN перестанет работать. 😱"
            )
        else:
            return

        # Отправляем сообщение (бот может писать пользователю, даже если он не писал ему)
        await bot.send_message(chat_id=user_id, text=text, parse_mode='HTML')
        logger.info(f"✅ Напоминание ({days_left} дн.) отправлено пользователю {user_id}")

    except Exception as e:
        # Если пользователь заблокировал бота или его нет
        logger.error(f"❌ Ошибка отправки напоминания для {user_id}: {e}")


async def check_expiring_subscriptions(bot: Bot):
    """Проверяет подписки и отправляет напоминания за 3, 2 и 1 день до окончания"""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Находим пользователей, у которых подписка заканчивается через 1, 2 или 3 дня
        now = datetime.now()

        c.execute('''
            SELECT user_id, subscription_end FROM users 
            WHERE subscription_end IS NOT NULL 
            AND subscription_end > NOW()
            AND DATE_PART('day', subscription_end - NOW()) IN (1, 2, 3)
        ''')

        users = c.fetchall()
        conn.close()

        for user_id, end_date in users:
            days_left = (end_date - now).days

            # Отправляем только если осталось 1, 2 или 3 дня
            if days_left in [1, 2, 3]:
                await send_subscription_reminder(bot, user_id, days_left, end_date)
                await asyncio.sleep(0.5)  # Небольшая задержка, чтобы не спамить

    except Exception as e:
        logger.error(f"❌ Ошибка проверки подписок: {e}")


async def scheduled_check(bot: Bot):
    """Запускает проверку подписок каждый день в 10:00 и 18:00"""
    while True:
        try:
            # Проверяем подписки
            await check_expiring_subscriptions(bot)

            # Ждем до следующей проверки (12 часов)
            await asyncio.sleep(12 * 60 * 60)  # 12 часов

        except Exception as e:
            logger.error(f"❌ Ошибка в scheduled_check: {e}")
            await asyncio.sleep(60)  # Если ошибка, ждем минуту и пробуем снова


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    logger.info(f"🔄 Нажата кнопка: {data}")

    # ===== ПОДДЕРЖКА =====
    if data == "help":
        context.user_data['support_mode'] = True
        await update.effective_chat.send_message("📞 Если у вас появились вопросы, напишите нам! Мы ответим вам в ближайщее время. Аккаунт для связи - @FMHHELP",
                                                 reply_markup=back_button())

    # ===== ПРОПУСТИТЬ НОМЕР =====
    elif data == "skip_phone":
        return await skip_phone_handler(update, context)

        # ===== ОПЛАТА =====
    elif data == "payment":
        return await payment_start(update, context)


    # ===== НАЗАД К ВЫБОРУ ТАРИФА =====
    elif data == "payment_tariff_back":
        user_id = update.effective_user.id
        user_data = get_user(user_id)
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu_with_bonus(user_id)
        )

    # ===== ВЫБОР ТАРИФА =====
    elif data.startswith("tariff_select_"):
        tariff = data.split("_")[2]
        user_id = update.effective_user.id
        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        context.user_data['selected_tariff'] = tariff

        prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}

        keyboard = [
            [InlineKeyboardButton(
                f"1 месяц — {prices[tariff]['1']} ₽ (бонусов: {min(bonus, prices[tariff]['1'])} ₽)",
                callback_data=f"plan_with_bonus_{tariff}_1"
            )],
            [InlineKeyboardButton(
                f"3 месяца — {prices[tariff]['3']} ₽ (бонусов: {min(bonus, prices[tariff]['3'])} ₽)",
                callback_data=f"plan_with_bonus_{tariff}_3"
            )],
            [InlineKeyboardButton(
                f"6 месяцев — {prices[tariff]['6']} ₽ (бонусов: {min(bonus, prices[tariff]['6'])} ₽)",
                callback_data=f"plan_with_bonus_{tariff}_6"
            )],
            [InlineKeyboardButton("🔙 Выбор тарифа", callback_data="payment_tariff_back")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]

        await update.effective_chat.send_message(
            f"📱 **Тариф {tariff.capitalize()}**\n\n"
            f"💰 Ваш бонусный счет: **{bonus} ₽**\n\n"
            f"Выберите срок подписки:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ===== ВЫБОР ПЛАНА (СРОКА) =====
    elif data.startswith("plan_with_bonus_"):
        parts = data.split("_")
        tariff, months = parts[3], parts[4]
        months = int(months)

        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        user_id = update.effective_user.id
        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['payment_amount'] = price

        # ===== МЕНЮ СПОСОБОВ ОПЛАТЫ (ДОБАВИЛИ СБП) =====
        keyboard = [
            [InlineKeyboardButton(
                f"💰 Оплатить ВСЕ бонусами ({min(bonus, price)} ₽)" if bonus >= price else f"💰 Не хватает бонусов ({bonus} из {price} ₽)",
                callback_data=f"bonus_all_{tariff}_{months}" if bonus >= price else "no_bonus"
            )],
            [InlineKeyboardButton(
                f"🔄 Частично бонусами (есть {bonus} ₽)",
                callback_data=f"bonus_partial_input_{tariff}_{months}"
            )] if bonus > 0 else [],
            [InlineKeyboardButton(
                f"💳 Банковская карта — {price} ₽",
                callback_data=f"pay_full_card_{tariff}_{months}"
            )],
            [InlineKeyboardButton(
                f"🏦 СБП — {price} ₽",
                callback_data=f"pay_full_sbp_{tariff}_{months}"
            )],
            [InlineKeyboardButton("🔙 К выбору срока", callback_data=f"back_to_plan_{tariff}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]
        # Убираем пустые кнопки
        keyboard = [k for k in keyboard if k]

        await update.effective_chat.send_message(
            f"✅ **Вы выбрали {tariff.capitalize()} на {months} месяц(ев)**\n\n"
            f"💰 Стоимость: **{price} ₽**\n"
            f"💎 Ваши бонусы: **{bonus} ₽**\n\n"
            f"💡 **Выберите способ оплаты:**",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ===== ОПЛАТА ВСЕМИ БОНУСАМИ =====
    elif data.startswith("bonus_all_"):
        parts = data.split("_")
        tariff, months = parts[2], parts[3]
        months = int(months)

        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        user_id = update.effective_user.id
        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        if bonus >= price:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('''
                UPDATE users 
                SET bonus_balance = bonus_balance - %s, 
                    subscription_end = %s 
                WHERE user_id = %s
            ''', (price, datetime.now() + timedelta(days=30 * months), user_id))
            conn.commit()
            conn.close()

            await update.effective_chat.send_message(
                f"✅ **Подписка активирована за бонусы!** 🎉\n\n"
                f"📱 Тариф: {tariff.capitalize()}\n"
                f"📆 Период: {months} месяц(ев)\n"
                f"💰 Списано бонусов: **{price} ₽**\n"
                f"📊 Остаток бонусов: **{bonus - price} ₽**",
                parse_mode='Markdown',
                reply_markup=main_menu()
            )
        else:
            await update.effective_chat.send_message(
                f"❌ **Недостаточно бонусов!**\n\n"
                f"💎 У вас: **{bonus}** бонусов\n"
                f"💸 Нужно: **{price}** бонусов",
                parse_mode='Markdown',
                reply_markup=back_button()
            )

    # ===== ЧАСТИЧНАЯ ОПЛАТА (ВВОД СУММЫ) =====
    elif data.startswith("bonus_partial_input_"):
        parts = data.split("_")
        tariff, months = parts[3], parts[4]
        months = int(months)

        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        user_id = update.effective_user.id
        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        if bonus <= 0:
            await update.effective_chat.send_message(
                "❌ У вас нет бонусов для частичной оплаты.",
                reply_markup=back_button()
            )
            return

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['full_price'] = price
        context.user_data['awaiting_bonus_input'] = True
        context.user_data['bonus_tariff'] = tariff
        context.user_data['bonus_plan'] = months
        context.user_data['bonus_max'] = min(bonus, price)

        await update.effective_chat.send_message(
            f"💎 **Частичная оплата бонусами**\n\n"
            f"💰 Стоимость: **{price} ₽**\n"
            f"💎 Ваши бонусы: **{bonus} ₽**\n"
            f"📊 Максимум можно использовать: **{min(bonus, price)} ₽**\n\n"
            f"✏️ **Введите сумму бонусов, которую хотите потратить:**\n"
            f"(от 1 до {min(bonus, price)} ₽)",
            parse_mode='Markdown',
            reply_markup=back_button()
        )

    # ===== ПОЛНАЯ ОПЛАТА КАРТОЙ =====
    elif data.startswith("pay_full_card_"):
        parts = data.split("_")
        tariff, months = parts[3], parts[4]
        months = int(months)

        price = {"simple": {"1": 249, "3": 599, "6": 999},
                 "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['payment_amount'] = price
        context.user_data['bonus_to_use'] = 0

        # Создаем платеж через карту
        payment_data = create_yookassa_payment(
            user_id=update.effective_user.id,
            amount=price,
            description=f"Подписка на медиа контент - {tariff.capitalize()} - {months} мес",
            payment_type="bank_card"
        )

        if payment_data:
            context.user_data['payment_id'] = payment_data['payment_id']

            keyboard = [
                [InlineKeyboardButton("💳 Перейти к оплате картой", url=payment_data['confirmation_url'])],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
                [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]

            await update.effective_chat.send_message(
                f"💳 **Оплата банковской картой**\n\n"
                f"💰 Сумма: **{price} ₽**\n"
                f"📱 Тариф: {tariff.capitalize()}\n"
                f"📆 Период: {months} месяц(ев)\n\n"
                f"🔗 [Оплатить картой]({payment_data['confirmation_url']})",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.effective_chat.send_message(
                "❌ Ошибка создания платежа. Попробуйте позже.",
                reply_markup=back_button()
            )

    # ===== ПОЛНАЯ ОПЛАТА СБП =====
    elif data.startswith("pay_full_sbp_"):
        parts = data.split("_")
        tariff, months = parts[3], parts[4]
        months = int(months)

        price = {"simple": {"1": 249, "3": 599, "6": 999},
                 "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['payment_amount'] = price
        context.user_data['bonus_to_use'] = 0

        payment_data = create_yookassa_payment(
            user_id=update.effective_user.id,
            amount=price,
            description=f"Подписка на медиа контент - {tariff.capitalize()} - {months} мес",
            payment_type="sbp"
        )

        if payment_data:
            context.user_data['payment_id'] = payment_data['payment_id']
            keyboard = [
                [InlineKeyboardButton("🏦 Перейти к оплате через СБП", url=payment_data['confirmation_url'])],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
                [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]
            await update.effective_chat.send_message(
                f"🏦 **Оплата через СБП**\n\n"
                f"💰 Сумма: **{price} ₽**\n"
                f"📱 Тариф: {tariff.capitalize()}\n"
                f"📆 Период: {months} месяц(ев)\n\n"
                f"🔗 [Оплатить через СБП]({payment_data['confirmation_url']})",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.effective_chat.send_message(
                "❌ Ошибка создания платежа. Попробуйте позже.",
                reply_markup=back_button()
            )

    # ===== НАЗАД К ВЫБОРУ СРОКА =====
    elif data.startswith("back_to_plan_"):
        tariff = data.split("_")[3]
        user_id = update.effective_user.id
        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}

        keyboard = [
            [InlineKeyboardButton(
                f"1 месяц — {prices[tariff]['1']} ₽ (бонусов: {min(bonus, prices[tariff]['1'])} ₽)",
                callback_data=f"plan_with_bonus_{tariff}_1"
            )],
            [InlineKeyboardButton(
                f"3 месяца — {prices[tariff]['3']} ₽ (бонусов: {min(bonus, prices[tariff]['3'])} ₽)",
                callback_data=f"plan_with_bonus_{tariff}_3"
            )],
            [InlineKeyboardButton(
                f"6 месяцев — {prices[tariff]['6']} ₽ (бонусов: {min(bonus, prices[tariff]['6'])} ₽)",
                callback_data=f"plan_with_bonus_{tariff}_6"
            )],
            [InlineKeyboardButton("🔙 Выбор тарифа", callback_data="payment_tariff_back")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        ]

        await update.effective_chat.send_message(
            f"📱 **Тариф {tariff.capitalize()}**\n\n"
            f"💰 Ваш бонусный счет: **{bonus} ₽**\n\n"
            f"Выберите срок подписки:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # ===== ПРОВЕРКА ПЛАТЕЖА =====
    elif data == "check_payment":
        user_id = update.effective_user.id
        payment_id = context.user_data.get('payment_id')
        tariff = context.user_data.get('selected_tariff', 'simple')

        if not payment_id:
            await update.effective_chat.send_message(
                "❌ Платеж не найден. Попробуйте создать новый.",
                reply_markup=back_button()
            )
            return

        payment_status = check_payment_status(payment_id)

        if payment_status and payment_status['paid'] and payment_status['status'] == 'succeeded':
            amount = context.user_data.get('payment_amount', 0)
            bonus_used = context.user_data.get('bonus_to_use', 0)
            full_price = context.user_data.get('full_price', amount + bonus_used)

            if process_successful_payment(user_id, payment_id, amount, tariff):
                months = context.user_data.get('selected_plan', 1)
                tariff = context.user_data.get('selected_tariff', 'Simple')

                if bonus_used > 0 and amount > 0:
                    await update.effective_chat.send_message(
                        f"✅ **Оплата подтверждена!** 🎉\n\n"
                        f"📱 Тариф: {tariff.capitalize()}\n"
                        f"📆 Период: {months} месяц(ев)\n"
                        f"💰 Полная стоимость: **{full_price} ₽**\n"
                        f"💎 Оплачено бонусами: **{bonus_used} ₽**\n"
                        f"💳 Оплачено деньгами: **{amount} ₽**\n"
                        f"🎁 Реферер получил **{int(amount * 0.2)}** бонусов!\n\n"
                        f"Теперь вы можете пользоваться VPN без ограничений 🚀",
                        parse_mode='Markdown',
                        reply_markup=main_menu()
                    )
                elif bonus_used > 0 and amount == 0:
                    await update.effective_chat.send_message(
                        f"✅ **Подписка активирована за бонусы!** 🎉\n\n"
                        f"📱 Тариф: {tariff.capitalize()}\n"
                        f"📆 Период: {months} месяц(ев)\n"
                        f"💰 Списано бонусов: **{full_price} ₽**",
                        parse_mode='Markdown',
                        reply_markup=main_menu()
                    )
                else:
                    await update.effective_chat.send_message(
                        f"✅ **Оплата подтверждена!** 🎉\n\n"
                        f"📱 Тариф: {tariff.capitalize()}\n"
                        f"📆 Период: {months} месяц(ев)\n"
                        f"💰 Сумма: **{amount} ₽**\n"
                        f"🎁 Реферер получил **{int(amount * 0.2)}** бонусов!\n\n"
                        f"Теперь вы можете пользоваться VPN без ограничений 🚀",
                        parse_mode='Markdown',
                        reply_markup=main_menu()
                    )
            else:
                await update.effective_chat.send_message(
                    "❌ Ошибка активации подписки. Обратитесь в поддержку.",
                    reply_markup=main_menu()
                )
        else:
            await update.effective_chat.send_message(
                f"⏳ Платеж еще не оплачен.\n"
                f"Статус: {payment_status['status'] if payment_status else 'неизвестен'}\n\n"
                f"Оплатите счет и нажмите «Проверить оплату» снова.",
                reply_markup=back_button()
            )

    # ===== ПОДКЛЮЧЕНИЕ =====
    elif data == "connect":
        await update.effective_chat.send_message(
            "🌐 Подключение временно недоступно.\nПожалуйста, оплатите подписку через кнопку «💸 Оплата».\n\n<a href='https://quaintly-ornate-basil.tilda.ws/'>Ссылка на инструкцию по подключению</a>",
            reply_markup=back_button(),
            parse_mode='HTML'
        )

    # ===== ПРОБНЫЙ ПЕРИОД =====
    elif data == "activate_trial":
        update_user_subscription(update.effective_user.id, 3)
        await send_service_info(update, context)

    # ===== АКТИВАЦИЯ ПРОБНОГО =====
    elif data == "activate":
        await update.effective_chat.send_message(
            "✅ Поздравляем! Вы активировали пробный период на 3 дня.\n\nТеперь вы можете пользоваться нашим VPN без ограничений.\nНаслаждайтесь! 🚀",
            reply_markup=main_menu()
        )

    # ===== НАШ КАНАЛ =====
    elif data == 'show_channel':
        await update.effective_chat.send_message('Ссылка на канал:\nhttps://t.me/FMH_VPN')

    # ===== ЛИЧНЫЙ КАБИНЕТ =====
    elif data == "profile":
        await send_profile(update, context)

    # ===== РЕФЕРАЛКА =====
    elif data == "referral":
        await referral_start(update, context)

    # ===== ГЛАВНОЕ МЕНЮ =====
    elif data == "main_menu":
        await send_main_menu(update, context)


# ========== ПОДДЕРЖКА ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"📨 Получено сообщение: {update.message.text}")
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # ===== ПРОВЕРКА КАПЧИ =====
    if user_id in user_captcha and 'answer' in user_captcha[user_id]:
        try:
            user_answer = int(text)
            correct_answer = user_captcha[user_id]['answer']

            if user_answer == correct_answer:
                del user_captcha[user_id]
                mark_captcha_passed(user_id)
                await update.message.reply_text("✅ Отлично! Теперь вы можете пользоваться ботом.")
                # ← ВЫЗЫВАЕМ START, А НЕ send_main_menu!
                await start(update, context)
                return
            else:
                user_captcha[user_id]['attempts'] += 1
                if user_captcha[user_id]['attempts'] >= 3:
                    del user_captcha[user_id]
                    await update.message.reply_text(
                        "❌ Вы превысили количество попыток. Напишите /start, чтобы попробовать снова."
                    )
                    return
                question, answer = generate_captcha()
                user_captcha[user_id] = {'answer': answer, 'attempts': user_captcha[user_id]['attempts']}
                await update.message.reply_text(
                    f"❌ Неправильно! Попробуйте ещё раз:\n\n**{question} = ?**\n\nОсталось попыток: {3 - user_captcha[user_id]['attempts']}",
                    parse_mode='HTML'
                )
                return
        except ValueError:
            await update.message.reply_text("❌ Введите ЧИСЛО, а не текст.")
            return

    # ===== ОБРАБОТКА ВВОДА СУММЫ БОНУСОВ =====
    if context.user_data.get('awaiting_bonus_input'):
        try:
            bonus_amount = int(update.message.text.strip())
            max_bonus = context.user_data.get('bonus_max', 0)
            tariff = context.user_data.get('bonus_tariff')
            months = context.user_data.get('bonus_plan')
            full_price = context.user_data.get('full_price', 0)

            if bonus_amount <= 0 or bonus_amount > max_bonus:
                await update.message.reply_text(
                    f"❌ Введите число от 1 до {max_bonus}",
                    reply_markup=back_button()
                )
                return

            # Сохраняем сумму бонусов
            context.user_data['bonus_to_use'] = bonus_amount
            context.user_data['payment_amount'] = full_price - bonus_amount
            context.user_data['awaiting_bonus_input'] = False

            # Списываем бонусы
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('UPDATE users SET bonus_balance = bonus_balance - %s WHERE user_id = %s',
                      (bonus_amount, user_id))
            conn.commit()
            conn.close()

            remaining = full_price - bonus_amount

            if remaining > 0:
                # Создаем платеж на остаток
                payment_data = create_yookassa_payment(
                    user_id=user_id,
                    amount=remaining,
                    description=f"Подписка на медиа контент (остаток) - {tariff.capitalize()} - {months} мес"
                )

                if payment_data:
                    context.user_data['payment_id'] = payment_data['payment_id']

                    keyboard = [
                        [InlineKeyboardButton("💳 Перейти к оплате", url=payment_data['confirmation_url'])],
                        [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
                        [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
                        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
                    ]

                    await update.message.reply_text(
                        f"✅ **Частичная оплата**\n\n"
                        f"💰 Полная стоимость: **{full_price} ₽**\n"
                        f"💎 Списано бонусов: **{bonus_amount} ₽**\n"
                        f"💳 Остаток к оплате: **{remaining} ₽**\n\n"
                        f"🔗 [Оплатить остаток]({payment_data['confirmation_url']})",
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    # Возвращаем бонусы при ошибке
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('UPDATE users SET bonus_balance = bonus_balance + %s WHERE user_id = %s',
                              (bonus_amount, user_id))
                    conn.commit()
                    conn.close()

                    await update.message.reply_text(
                        "❌ Ошибка создания платежа. Бонусы возвращены.",
                        reply_markup=back_button()
                    )
            else:
                # Остатка нет - активируем подписку
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('UPDATE users SET subscription_end = %s WHERE user_id = %s',
                          (datetime.now() + timedelta(days=30 * months), user_id))
                conn.commit()
                conn.close()

                await update.message.reply_text(
                    f"✅ **Подписка полностью оплачена бонусами!** 🎉\n\n"
                    f"📱 Тариф: {tariff.capitalize()}\n"
                    f"📆 Период: {months} месяц(ев)\n"
                    f"💰 Списано бонусов: **{bonus_amount} ₽**",
                    parse_mode='Markdown',
                    reply_markup=main_menu()
                )

        except ValueError:
            await update.message.reply_text(
                "❌ Введите ЧИСЛО. Например: 100",
                reply_markup=back_button()
            )
        return

    # ===== ДИАЛОГ ОПЛАТЫ =====
    if context.user_data.get('state') == PAYMENT_PHONE:
        logger.info("🔴 Сообщение перенаправляется в payment_phone")
        return await payment_phone(update, context)

    # ===== ПОДДЕРЖКА =====
    if context.user_data.get('support_mode'):
        user = update.effective_user
        text = update.message.text
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"📩 Новое обращение от {user.first_name} (@{user.username or 'нет username'}):\n\n{text}"
        )
        await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Мы ответим вам в ближайшее время.")
        context.user_data['support_mode'] = False


# ========== ЗАПУСК ==========
def main():
    logger.info("🚀 Запуск бота...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Получаем объект бота для отправки сообщений
    bot = app.bot

    # Добавляем обработчики
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

    # Запускаем фоновую задачу для проверки подписок
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(scheduled_check(bot))

    logger.info("✅ Бот готов!")
    app.run_polling()


if __name__ == '__main__':
    main()