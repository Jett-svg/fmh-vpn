import logging
import os
import random
import secrets
import string
import time
from collections import defaultdict
from datetime import datetime, timedelta

import bcrypt
import psycopg2
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler,
    MessageHandler, filters, ConversationHandler,
)
from yookassa import Configuration, Payment
import asyncio
from flask import Flask
import threading

# ========== ОБЩАЯ ЛОГИКА (3x-ui, подписки, платежи) — единый источник правды ==========
from vpn_core import (
    init_shared_schema,
    get_db_connection,
    SubscriptionRecord,
    get_active_subscription_by_user,
    build_subscription_link,
    apply_subscription_payment,
    mark_payment_processed_if_new,
    merge_user_by_phone,
)

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

# ========== ТРОТТЛИНГ СООБЩЕНИЙ/КНОПОК ==========
user_last_message = defaultdict(float)
MIN_INTERVAL = 1.0  # секунда между сообщениями/нажатиями от одного пользователя

# ========== FLASK ДЛЯ RENDER (health-check) ==========
flask_app = Flask(__name__)


@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "Bot is running!", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, debug=False)


threading.Thread(target=run_web, daemon=True).start()

# ========== СХЕМА БД (общая часть — из vpn_core) ==========
init_shared_schema()


# ========== ФУНКЦИИ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ (специфичны для бота) ==========
def get_user(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT subscription_end, devices, max_devices, bonus_balance, phone, first_time '
            'FROM users WHERE user_id = %s',
            (user_id,)
        )
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


def hash_password(password: str) -> str:
    """Хэширует пароль bcrypt'ом — единый формат для бота, сайта и FastAPI-сервиса"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def generate_site_credentials(user_id: int):
    """Генерирует email и пароль для входа на сайт."""
    email = f"tg_{user_id}@fmh.local"
    alphabet = string.ascii_letters + string.digits
    password = ''.join(secrets.choice(alphabet) for _ in range(12))
    return email, password


def save_site_credentials(user_id: int, email: str, password: str) -> bool:
    try:
        conn = get_db_connection()
        c = conn.cursor()
        password_hash = hash_password(password)
        c.execute('''
            UPDATE users
            SET email = %s, password_hash = %s, email_verified = TRUE
            WHERE user_id = %s
        ''', (email, password_hash, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ Учётные данные сайта сохранены для user_id={user_id}: email={email}")
        return True
    except Exception as e:
        logger.error(f"❌ Ошибка save_site_credentials для {user_id}: {e}")
        return False


def get_site_credentials(user_id: int):
    """Получает email пользователя (пароль не возвращаем!)"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT email FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"❌ Ошибка get_site_credentials для {user_id}: {e}")
        return None


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


def mark_captcha_passed(user_id):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET captcha_passed = TRUE WHERE user_id = %s', (user_id,))
        conn.commit()
        conn.close()
        logger.info(f"✅ КАПЧА ОТМЕЧЕНА ДЛЯ {user_id}")
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


# ========== СЕКРЕТЫ ОКРУЖЕНИЯ ==========
BOT_TOKEN = os.environ['BOT_TOKEN']
ADMIN_CHAT_ID = int(os.environ['ADMIN_CHAT_ID'])
PAYMENT_PHONE = 1

# ========== НАСТРОЙКА ЮKASSA ==========
YOOKASSA_SHOP_ID = os.environ['YOOKASSA_SHOP_ID']
YOOKASSA_SECRET_KEY = os.environ['YOOKASSA_SECRET_KEY']
YOOKASSA_TEST_MODE = os.environ.get('YOOKASSA_TEST_MODE', 'true').lower() == 'true'

Configuration.account_id = YOOKASSA_SHOP_ID
Configuration.secret_key = YOOKASSA_SECRET_KEY


def create_yookassa_payment(user_id, amount, tariff_type, months,
                             description="Оплата подписки на медиа контент", payment_type="bank_card"):
    try:
        idempotence_key = str(secrets.token_hex(16))

        payment_data = {
            "amount": {"value": str(amount), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/fmh_vpn_bot"},
            "description": description,
            "metadata": {
                "user_id": str(user_id),
                "tariff_type": tariff_type,
                'months': str(months),
            },
            "capture": True,
        }

        if payment_type == "sbp":
            payment_data["payment_method_data"] = {"type": "sbp"}

        if YOOKASSA_TEST_MODE:
            payment_data["test"] = True

        payment = Payment.create(payment_data, idempotence_key)
        logger.info(f"💰 Платеж создан: {payment.id} для user_id={user_id}")

        return {
            'payment_id': payment.id,
            'confirmation_url': payment.confirmation.confirmation_url,
            'status': payment.status,
        }
    except Exception as e:
        logger.error(f"❌ Ошибка создания платежа: {e}")
        return None


def check_payment_status(payment_id):
    try:
        payment = Payment.find_one(payment_id)
        return {'status': payment.status, 'paid': payment.paid, 'amount': payment.amount.value}
    except Exception as e:
        logger.error(f"❌ Ошибка проверки платежа: {e}")
        return None


def process_payment(user_id, amount):
    """Начисление бонусов рефереру (только за первую оплату) — специфично для бота,
    на сайте реферальной программы нет."""
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('SELECT referred_by FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()

        if result and result[0]:
            referrer_id = result[0]

            c.execute('SELECT bonus_paid FROM users WHERE user_id = %s', (user_id,))
            bonus_paid = c.fetchone()[0]

            if not bonus_paid:
                bonus = int(amount * 0.2)
                c.execute('UPDATE users SET bonus_balance = bonus_balance + %s WHERE user_id = %s',
                          (bonus, referrer_id))
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


def process_successful_payment(user_id, payment_id, amount, tariff_type, months):
    """Тонкая обёртка над общей логикой из vpn_core — добавляет реферальный бонус,
    которого нет на сайте, и идемпотентно защищена от двойной обработки."""
    try:
        if not mark_payment_processed_if_new(payment_id, user_id, amount, tariff_type, months):
            logger.info(f"ℹ️ Платёж {payment_id} уже обработан — пропускаем")
            record = get_active_subscription_by_user(user_id)
            return build_subscription_link(record.tariff, record.sub_id) if record else None

        sub_link = apply_subscription_payment(user_id, tariff_type, days=30 * months)
        process_payment(user_id, amount)  # реферальный бонус — своя логика бота
        logger.info(f"✅ Платеж {payment_id} обработан для user_id={user_id}, ссылка: {sub_link}")
        return sub_link
    except Exception as e:
        logger.error(f"❌ Ошибка обработки платежа: {e}")
        return None


# ========== ВЕБХУК ЮKASSA ==========
YOOKASSA_ALLOWED_IPS = {
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11",
    "77.75.156.35",
    "77.75.154.128/25",
    "2a02:5180::/32",
}
# ⚠️ Проверь актуальный список в доке YooKassa перед продом, они его обновляют

import ipaddress


def is_yookassa_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    for net in YOOKASSA_ALLOWED_IPS:
        if addr in ipaddress.ip_network(net):
            return True
    return False


from flask import request, jsonify


@flask_app.route('/yookassa/webhook', methods=['POST'])
def yookassa_webhook():
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    if not is_yookassa_ip(client_ip):
        logger.warning(f"⚠️ Вебхук с недоверенного IP: {client_ip}")
        return jsonify({"status": "forbidden"}), 403

    data = request.json
    if data.get('event') == 'payment.succeeded':
        obj = data['object']
        user_id = int(obj['metadata']['user_id'])
        amount = float(obj['amount']['value'])
        payment_id = obj['id']
        tariff_type = obj['metadata'].get('tariff_type', 'simple')
        months = int(obj['metadata'].get('months', 1))

        check = check_payment_status(payment_id)
        if not check or not check['paid']:
            return jsonify({"status": "not confirmed"}), 400

        process_successful_payment(user_id, payment_id, amount, tariff_type, months)

    return jsonify({"status": "ok"}), 200


# ========== КНОПКИ ==========
def main_menu():
    keyboard = [
        [InlineKeyboardButton("👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton("💸 Оплата", callback_data="payment")],
        [InlineKeyboardButton("🌐 Подключиться", callback_data="connect")],
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="referral")],
        [InlineKeyboardButton("📞 Поддержка", callback_data="help")],
        [InlineKeyboardButton("⌯⌲ Наш канал", callback_data="show_channel")],
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
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить", callback_data="skip_phone")]])


def payment_tariff_menu_with_bonus(user_id, selected_tariff=None, selected_plan=None):
    user_data = get_user(user_id)
    bonus = user_data[3] if user_data else 0

    prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}

    if selected_tariff and selected_plan:
        price = prices[selected_tariff][str(selected_plan)]
        keyboard = []

        if bonus >= price:
            keyboard.append([InlineKeyboardButton(
                f"💰 Оплатить ВСЕ бонусами ({price} ₽)",
                callback_data=f"bonus_all_{selected_tariff}_{selected_plan}"
            )])
        if bonus > 0:
            keyboard.append([InlineKeyboardButton(
                f"🔄 Частично бонусами (есть {bonus} ₽)",
                callback_data=f"bonus_partial_input_{selected_tariff}_{selected_plan}"
            )])
        keyboard.append([InlineKeyboardButton(
            f"💳 Полностью деньгами — {price} ₽",
            callback_data=f"pay_full_{selected_tariff}_{selected_plan}"
        )])
        keyboard.append([InlineKeyboardButton("🔙 К выбору срока", callback_data=f"back_to_plan_{selected_tariff}")])
        keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
        return InlineKeyboardMarkup(keyboard)

    keyboard = [
        [InlineKeyboardButton("❓ Не знаю что выбрать", callback_data="unknown_choice")],
        [InlineKeyboardButton(f"📱 Simple — от 249 ₽ (бонусов: {bonus} ₽)", callback_data="tariff_select_simple")],
        [InlineKeyboardButton(f"🚀 Pro — от 499 ₽ (бонусов: {bonus} ₽)", callback_data="tariff_select_pro")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


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
    caption = (
        "👋 Привет!\n\n"
        "Если вы устали от лагающих и не работающих VPN — тогда вы по адресу.\n\n"
        "Чтобы проверить, насколько мы хороши, дарим тебе пробный период на 3 дня."
    )
    if os.path.exists('FMH-VPN.jpg'):
        with open('FMH-VPN.jpg', 'rb') as photo:
            await update.message.reply_photo(photo=InputFile(photo), caption=caption, reply_markup=welcome_menu())
    else:
        await update.message.reply_text(text=caption, reply_markup=welcome_menu())


async def send_service_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📌 О нашем сервисе:\n\n"
        "✅ Неограниченная скорость и безлимитный трафик\n"
        "✅ До 3-х подключаемых устройств в одной подписке\n"
        "✅ Совместимость со всеми устройствами\n"
        "✅ Возможность заходить в российские приложения и банки даже с выключенным VPN, никаких ограничений\n"
        "✅ Имеем резервные сервера на случай сбоя основных — а это значит, вы никогда не останетесь без VPN\n\n"
        "Гарантируем вам работу на всей территории России и со всеми операторами мобильной связи.\n\n"
        "💸 Стоимость после пробного периода:\n"
        "• 249 ₽/месяц (Simple подписка)\n"
        "• 499 ₽/месяц (Pro подписка)"
    )
    await update.effective_chat.send_message(text=text, reply_markup=trial_activated_menu())


async def send_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = get_user(update.effective_user.id)
    if not user_data:
        await update.effective_chat.send_message(
            "⚠️ Вы не зарегистрированы. Напишите /start для регистрации.",
            reply_markup=back_button()
        )
        return

    subscription_end, devices, max_devices, bonus_balance, phone, _ = user_data

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT tariff_type FROM users WHERE user_id = %s', (update.effective_user.id,))
    result = c.fetchone()
    tariff_type = result[0] if result else None
    conn.close()

    if subscription_end:
        end_date = subscription_end if isinstance(subscription_end, datetime) else None
        if end_date and end_date > datetime.now():
            days_left = (end_date - datetime.now()).days
            hours_left = (end_date - datetime.now()).seconds // 3600
            status = "✅ Активна"
            end_text = (
                f"до {end_date.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
                if days_left > 0 else
                f"до {end_date.strftime('%d.%m.%Y')} (осталось {hours_left} ч.)"
            )
        else:
            status = "❌ Истекла"
            end_text = f"истекла {end_date.strftime('%d.%m.%Y') if end_date else 'давно'}"
    else:
        status = "❌ Не активна"
        end_text = "нет активной подписки"

    if tariff_type == 'simple':
        tariff_name = "📱 Simple (3 устройства)"
    elif tariff_type == 'pro':
        tariff_name = "🚀 Pro (5 устройств)"
    elif tariff_type is None and subscription_end and subscription_end > datetime.now():
        tariff_name = "📱 Simple (3 устройства)"
    else:
        tariff_name = "❌ Нет подписки"

    record = get_active_subscription_by_user(update.effective_user.id)
    sub_link_text = (
        f"\n🔑 **Ссылка на подписку:**\n`{build_subscription_link(record.tariff, record.sub_id)}`"
        if record else ""
    )

    text = (
        f"👤 **Личный кабинет**\n\n"
        f"📅 **Статус подписки:** {status}\n"
        f"📆 **Окончание:** {end_text}\n"
        f"📦 **Тариф:** {tariff_name}\n\n"
        f"📱 **Устройства:** {devices} из {max_devices} использовано\n"
        f"✅ **Свободно:** {max_devices - devices} устройств\n\n"
        f"💰 **Бонусный счёт:** {bonus_balance} ₽\n"
        f"📞 **Телефон:** {phone or 'Не указан'}"
        f"{sub_link_text}"
    )
    await update.effective_chat.send_message(text=text, parse_mode='Markdown', reply_markup=back_button())


# ========== ОПЛАТА ==========
async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data and user_data[4]:
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu_with_bonus(user_id)
        )
        return ConversationHandler.END

    await update.effective_chat.send_message(
        "📱 Для оформления подписки укажите ваш номер телефона.\n"
        "Это необязательно, но поможет нам связаться с вами.\n\n"
        "Отправьте номер в формате: +7XXXXXXXXXX\n"
        "Или нажмите «Пропустить».",
        reply_markup=skip_button()
    )
    return PAYMENT_PHONE


async def payment_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user_id = update.effective_user.id

    if not phone.startswith('+') or len(phone) < 10:
        await update.message.reply_text(
            "❌ Неверный формат номера. Отправьте номер в формате: +7XXXXXXXXXX\n"
            "Или нажмите «Пропустить».",
            reply_markup=skip_button()
        )
        return PAYMENT_PHONE

    merge_user_by_phone(user_id, phone)
    success = update_user_phone(user_id, phone)

    if success:
        await update.message.reply_text("✅ Номер сохранён!")
        user_data = get_user(user_id)
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu_with_bonus(user_id)
        )
    else:
        await update.message.reply_text(
            "❌ Ошибка сохранения номера. Попробуйте позже или нажмите «Пропустить».",
            reply_markup=skip_button()
        )
        return PAYMENT_PHONE

    return ConversationHandler.END


async def skip_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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


# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # ===== 1. АВТОРИЗАЦИЯ С САЙТА =====
    if context.args and len(context.args) > 0 and context.args[0].startswith('auth_'):
        token = context.args[0].replace('auth_', '')

        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('''
                INSERT INTO telegram_auth_tokens (token, telegram_user_id, used, created_at)
                VALUES (%s, %s, FALSE, NOW())
                ON CONFLICT (token) DO UPDATE
                SET telegram_user_id = %s, used = FALSE, created_at = NOW()
            ''', (token, user_id, user_id))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"❌ Ошибка обновления токена в БД: {e}")
            await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")
            return

        try:
            await update.message.reply_text(
                f"✅ **Авторизация успешна!**\n\n"
                f"Вы вошли как пользователь с ID: `{user_id}`\n"
                f"⏳ **Пожалуйста, просто вернитесь на вкладку браузера.**\n"
                f"Вход на сайт произойдет автоматически через 1-2 секунды.",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения в Telegram: {e}")

        return

    # ===== 2. ОБЫЧНЫЙ ЗАПУСК БОТА =====
    existing_user = get_user(user_id)

    referrer_id = None
    if context.args:
        try:
            if context.args[0].startswith('ref_'):
                referrer_id = int(context.args[0].split('_')[1])
        except Exception:
            pass

    if existing_user is None:
        create_user(user_id)

        conn = get_db_connection()
        c = conn.cursor()
        c.execute('UPDATE users SET start_date = %s WHERE user_id = %s', (datetime.now(), user_id))
        conn.commit()
        conn.close()

        if referrer_id and referrer_id != user_id:
            try:
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('SELECT user_id, phone FROM users WHERE user_id = %s', (referrer_id,))
                referrer_data = c.fetchone()
                if referrer_data and referrer_data[1] and referrer_data[1].strip():
                    c.execute('UPDATE users SET referred_by = %s WHERE user_id = %s', (referrer_id, user_id))
                    conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"❌ Ошибка сохранения реферера: {e}")

        question, answer = generate_captcha()
        user_captcha[user_id] = {'answer': answer, 'attempts': 0}
        await update.message.reply_text(
            f"🤖 **Привет! Для продолжения решите пример:**\n\n**{question} = ?**\n\nВведите ответ числом.",
            parse_mode='HTML'
        )
        return

    if not is_captcha_passed(user_id):
        question, answer = generate_captcha()
        user_captcha[user_id] = {'answer': answer, 'attempts': 0}
        await update.message.reply_text(
            f"🤖 **Для продолжения решите пример:**\n\n**{question} = ?**\n\nВведите ответ числом.",
            parse_mode='HTML'
        )
        return

    site_email = get_site_credentials(user_id)
    if not site_email:
        email, password = generate_site_credentials(user_id)
        if save_site_credentials(user_id, email, password):
            site_url = "http://217.60.39.78:8080"
            credentials_message = (
                f"🔐 **Ваши данные для входа на сайт:**\n\n"
                f"📧 **Логин (email):**\n`{email}`\n\n"
                f"🔑 **Пароль:**\n`{password}`\n\n"
                f"⚠️ **Сохраните эти данные!**\n\n"
                f"🌐 **Перейти на сайт:**\n{site_url}"
            )
            await update.message.reply_text(credentials_message, parse_mode='Markdown')
        else:
            await update.message.reply_text(
                "⚠️ Не удалось сохранить данные для входа на сайт. Напишите /mylogin позже."
            )

    if existing_user[5] == 1:
        mark_user_as_old(user_id)
        await send_welcome(update, context)
    else:
        await send_main_menu(update, context)


async def mylogin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    email = get_site_credentials(user_id)

    if not email:
        email, password = generate_site_credentials(user_id)
        if not save_site_credentials(user_id, email, password):
            await update.message.reply_text("⚠️ Не удалось создать данные для входа. Попробуйте ещё раз позже.")
            return

        site_url = "http://217.60.39.78:8080"
        text = (
            f"🔐 **Ваши данные для входа на сайт:**\n\n"
            f"📧 **Логин (email):**\n`{email}`\n\n"
            f"🔑 **Пароль:**\n`{password}`\n\n"
            f"⚠️ **Сохраните эти данные!**\n\n"
            f"🌐 **Перейти на сайт:**\n{site_url}"
        )
    else:
        site_url = "http://217.60.39.78:8080"
        text = (
            f"🔐 **Ваши данные для входа на сайт:**\n\n"
            f"📧 **Логин (email):**\n`{email}`\n\n"
            f"🔑 **Пароль:** скрыт в целях безопасности\n\n"
            f"💡 Если вы забыли пароль, напишите в поддержку @FMHHELP — мы поможем восстановить доступ.\n\n"
            f"🌐 **Перейти на сайт:**\n{site_url}"
        )

    await update.message.reply_text(text, parse_mode='Markdown')


# ========== ФОНОВЫЕ ПРОВЕРКИ ==========
async def check_inactive_users(bot: Bot):
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT user_id, start_date, reminder_sent
            FROM users
            WHERE start_date IS NOT NULL
            AND subscription_end IS NULL
            AND reminder_sent < 5
        ''')
        users = c.fetchall()
        conn.close()

        now = datetime.now()

        for user_id, start_date, reminder_sent in users:
            days_passed = (now - start_date).days

            if days_passed == reminder_sent:
                user_data = get_user(user_id)
                if user_data and user_data[0] is not None:
                    continue

                text = (
                    f"👋 **Привет!**\n\n"
                    f"Вы зарегистрировались в FMH_VPN, но ещё не активировали пробный период.\n\n"
                    f"🎁 Мы дарим вам **3 дня** бесплатного доступа к VPN.\n"
                    f"✅ Быстро, надёжно, без ограничений.\n\n"
                    f"💡 **Чтобы активировать пробный период,** просто нажмите /start и следуйте инструкциям.\n\n"
                    f"Не упустите возможность попробовать! 🚀"
                )

                try:
                    await bot.send_message(chat_id=user_id, text=text, parse_mode='Markdown')

                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('UPDATE users SET reminder_sent = reminder_sent + 1 WHERE user_id = %s', (user_id,))
                    conn.commit()
                    conn.close()

                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки напоминания для {user_id}: {e}")

    except Exception as e:
        logger.error(f"❌ Ошибка check_inactive_users: {e}")


async def send_subscription_reminder(bot: Bot, user_id: int, days_left: int, end_date: datetime):
    try:
        if days_left == 3:
            text = (
                f"⚠️ **Напоминание!**\n\nВаша подписка на FMH_VPN закончится через **3 дня**.\n"
                f"📅 Дата окончания: {end_date.strftime('%d.%m.%Y')}\n\n"
                f"💸 Продлите подписку сейчас, чтобы не остаться без VPN!"
            )
        elif days_left == 2:
            text = (
                f"🔥 **Внимание!**\n\nВаша подписка на FMH_VPN закончится через **2 дня**!\n"
                f"📅 Дата окончания: {end_date.strftime('%d.%m.%Y')}\n\n"
                f"💸 Продлите подписку сейчас, чтобы не остаться без VPN."
            )
        elif days_left == 1:
            text = (
                f"🚨 **Последний день!**\n\nВаша подписка на FMH_VPN заканчивается **ЗАВТРА**!\n"
                f"📅 Дата окончания: {end_date.strftime('%d.%m.%Y')}\n\n"
                f"💸 **Срочно продлите подписку!**\nЕсли не продлите, VPN перестанет работать. 😱"
            )
        else:
            return

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💸 Оплатить", callback_data="payment")]])
        await bot.send_message(chat_id=user_id, text=text, parse_mode='HTML', reply_markup=keyboard)
    except Exception as e:
        logger.error(f"❌ Ошибка отправки напоминания для {user_id}: {e}")


async def check_expiring_subscriptions(bot: Bot):
    try:
        conn = get_db_connection()
        c = conn.cursor()
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
            if days_left in [1, 2, 3]:
                await send_subscription_reminder(bot, user_id, days_left, end_date)
                await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"❌ Ошибка проверки подписок: {e}")


async def scheduled_check(bot: Bot):
    while True:
        try:
            now = datetime.now()
            if now.hour in [10, 18] and now.minute == 0:
                await check_expiring_subscriptions(bot)
            if now.hour == 8 and now.minute == 0:
                await check_inactive_users(bot)
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"❌ Ошибка в scheduled_check: {e}")
            await asyncio.sleep(60)


# ========== КНОПКИ ==========
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = time.time()
    if now - user_last_message[user_id] < MIN_INTERVAL:
        return
    user_last_message[user_id] = now

    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "help":
        context.user_data['support_mode'] = True
        await update.effective_chat.send_message(
            "📞 Если у вас появились вопросы, напишите нам! Мы ответим вам в ближайщее время. "
            "Аккаунт для связи - @FMHHELP",
            reply_markup=back_button()
        )

    elif data == "skip_phone":
        return await skip_phone_handler(update, context)

    elif data == "payment":
        return await payment_start(update, context)

    elif data == "payment_tariff_back":
        user_data = get_user(user_id)
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**\n\n"
            f"💰 Ваш бонусный счет: **{user_data[3]} ₽**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu_with_bonus(user_id)
        )

    elif data.startswith("tariff_select_"):
        tariff = data.split("_")[2]
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
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        await update.effective_chat.send_message(
            f"📱 **Тариф {tariff.capitalize()}**\n\n"
            f"💰 Ваш бонусный счет: **{bonus} ₽**\n\n"
            f"Выберите срок подписки:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("plan_with_bonus_"):
        parts = data.split("_")
        tariff, months = parts[3], int(parts[4])
        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['payment_amount'] = price

        keyboard = [
            [InlineKeyboardButton(
                f"💰 Оплатить ВСЕ бонусами ({min(bonus, price)} ₽)" if bonus >= price
                else f"💰 Не хватает бонусов ({bonus} из {price} ₽)",
                callback_data=f"bonus_all_{tariff}_{months}" if bonus >= price else "no_bonus"
            )],
            [InlineKeyboardButton(
                f"🔄 Частично бонусами (есть {bonus} ₽)",
                callback_data=f"bonus_partial_input_{tariff}_{months}"
            )] if bonus > 0 else [],
            [InlineKeyboardButton(f"💳 Банковская карта — {price} ₽", callback_data=f"pay_full_card_{tariff}_{months}")],
            [InlineKeyboardButton(f"🏦 СБП — {price} ₽", callback_data=f"pay_full_sbp_{tariff}_{months}")],
            [InlineKeyboardButton("🔙 К выбору срока", callback_data=f"back_to_plan_{tariff}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        keyboard = [k for k in keyboard if k]

        await update.effective_chat.send_message(
            f"✅ **Вы выбрали {tariff.capitalize()} на {months} месяц(ев)**\n\n"
            f"💰 Стоимость: **{price} ₽**\n"
            f"💎 Ваши бонусы: **{bonus} ₽**\n\n"
            f"💡 **Выберите способ оплаты:**",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("bonus_all_"):
        parts = data.split("_")
        tariff, months = parts[2], int(parts[3])
        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        if bonus >= price:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute('UPDATE users SET bonus_balance = bonus_balance - %s WHERE user_id = %s', (price, user_id))
            conn.commit()
            conn.close()

            sub_link = apply_subscription_payment(user_id, tariff, days=30 * months)

            text = (
                f"✅ **Подписка активирована за бонусы!** 🎉\n\n"
                f"📱 Тариф: {tariff.capitalize()}\n"
                f"📆 Период: {months} месяц(ев)\n"
                f"💰 Списано бонусов: **{price} ₽**\n"
                f"📊 Остаток бонусов: **{bonus - price} ₽**"
            )
            text += (
                f"\n\n🔑 **Ваша ссылка на подписку:**\n`{sub_link}`" if sub_link else
                "\n\n⚠️ Не удалось выдать ключ автоматически, напишите в поддержку."
            )
            await update.effective_chat.send_message(text, parse_mode='Markdown', reply_markup=main_menu())
        else:
            await update.effective_chat.send_message(
                f"❌ **Недостаточно бонусов!**\n\n💎 У вас: **{bonus}** бонусов\n💸 Нужно: **{price}** бонусов",
                parse_mode='Markdown', reply_markup=back_button()
            )

    elif data.startswith("bonus_partial_input_"):
        parts = data.split("_")
        tariff, months = parts[3], int(parts[4])
        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        user_data = get_user(user_id)
        bonus = user_data[3] if user_data else 0

        if bonus <= 0:
            await update.effective_chat.send_message(
                "❌ У вас нет бонусов для частичной оплаты.", reply_markup=back_button()
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

    elif data.startswith("pay_full_card_"):
        parts = data.split("_")
        tariff, months = parts[3], int(parts[4])
        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['payment_amount'] = price
        context.user_data['bonus_to_use'] = 0

        payment_data = create_yookassa_payment(
            user_id=user_id, amount=price, tariff_type=tariff, months=months,
            description=f"Подписка на медиа контент - {tariff.capitalize()} - {months} мес",
            payment_type="bank_card"
        )

        if payment_data:
            context.user_data['payment_id'] = payment_data['payment_id']
            keyboard = [
                [InlineKeyboardButton("💳 Перейти к оплате картой", url=payment_data['confirmation_url'])],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
                [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
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
            await update.effective_chat.send_message("❌ Ошибка создания платежа. Попробуйте позже.", reply_markup=back_button())

    elif data.startswith("pay_full_sbp_"):
        parts = data.split("_")
        tariff, months = parts[3], int(parts[4])
        price = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 1399, "6": 2399}}[tariff][str(months)]

        context.user_data['selected_tariff'] = tariff
        context.user_data['selected_plan'] = months
        context.user_data['payment_amount'] = price
        context.user_data['bonus_to_use'] = 0

        payment_data = create_yookassa_payment(
            user_id=user_id, amount=price, tariff_type=tariff, months=months,
            description=f"Подписка на медиа контент - {tariff.capitalize()} - {months} мес",
            payment_type="sbp"
        )

        if payment_data:
            context.user_data['payment_id'] = payment_data['payment_id']
            keyboard = [
                [InlineKeyboardButton("🏦 Перейти к оплате через СБП", url=payment_data['confirmation_url'])],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
                [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
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
            await update.effective_chat.send_message("❌ Ошибка создания платежа. Попробуйте позже.", reply_markup=back_button())

    elif data.startswith("back_to_plan_"):
        tariff = data.split("_")[3]
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
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        await update.effective_chat.send_message(
            f"📱 **Тариф {tariff.capitalize()}**\n\n"
            f"💰 Ваш бонусный счет: **{bonus} ₽**\n\n"
            f"Выберите срок подписки:",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "check_payment":
        payment_id = context.user_data.get('payment_id')
        tariff = context.user_data.get('selected_tariff', 'simple')
        months = context.user_data.get('selected_plan', 1)

        if not payment_id:
            await update.effective_chat.send_message("❌ Платеж не найден. Попробуйте создать новый.", reply_markup=back_button())
            return

        payment_status = check_payment_status(payment_id)

        if payment_status and payment_status['paid'] and payment_status['status'] == 'succeeded':
            amount = context.user_data.get('payment_amount', 0)
            bonus_used = context.user_data.get('bonus_to_use', 0)
            full_price = context.user_data.get('full_price', amount + bonus_used)

            sub_link = process_successful_payment(user_id, payment_id, amount, tariff, months)

            if sub_link is not None:
                link_text = f"\n\n🔑 **Ваша ссылка на подписку:**\n`{sub_link}`"
                if bonus_used > 0 and amount > 0:
                    await update.effective_chat.send_message(
                        f"✅ **Оплата подтверждена!** 🎉\n\n"
                        f"📱 Тариф: {tariff.capitalize()}\n"
                        f"📆 Период: {months} месяц(ев)\n"
                        f"💰 Полная стоимость: **{full_price} ₽**\n"
                        f"💎 Оплачено бонусами: **{bonus_used} ₽**\n"
                        f"💳 Оплачено деньгами: **{amount} ₽**\n"
                        f"🎁 Реферер получил **{int(amount * 0.2)}** бонусов!"
                        f"{link_text}",
                        parse_mode='Markdown',
                        reply_markup=main_menu()
                    )
                else:
                    await update.effective_chat.send_message(
                        f"✅ **Оплата подтверждена!** 🎉\n\n"
                        f"📱 Тариф: {tariff.capitalize()}\n"
                        f"📆 Период: {months} месяц(ев)\n"
                        f"💰 Сумма: **{amount} ₽**\n"
                        f"🎁 Реферер получил **{int(amount * 0.2)}** бонусов!"
                        f"{link_text}",
                        parse_mode='Markdown',
                        reply_markup=main_menu()
                    )
            else:
                await update.effective_chat.send_message(
                    "⚠️ Оплата прошла, но возникла ошибка выдачи ключа. Напишите в поддержку — мы выдадим ключ вручную.",
                    reply_markup=main_menu()
                )
        else:
            await update.effective_chat.send_message(
                f"⏳ Платеж еще не оплачен.\n"
                f"Статус: {payment_status['status'] if payment_status else 'неизвестен'}\n\n"
                f"Оплатите счет и нажмите «Проверить оплату» снова.",
                reply_markup=back_button()
            )

    elif data == "connect":
        record = get_active_subscription_by_user(user_id)
        if record:
            text = (
                f"🌐 **Ваша ссылка на подписку:**\n{build_subscription_link(record.tariff, record.sub_id)}\n\n"
                f"<a href='https://quaintly-ornate-basil.tilda.ws/'>Инструкция по подключению</a>"
            )
        else:
            text = (
                "🌐 У вас пока нет активной подписки.\n"
                "Оформите её через кнопку «💸 Оплата».\n\n"
                "<a href='https://quaintly-ornate-basil.tilda.ws/'>Ссылка на инструкцию по подключению</a>"
            )
        await update.effective_chat.send_message(text, reply_markup=back_button(), parse_mode='HTML')

    elif data == "activate_trial":
        user_data = get_user(user_id)
        if user_data and user_data[0] and user_data[0] > datetime.now():
            await update.effective_chat.send_message("✅ У вас уже есть активная подписка!", reply_markup=main_menu())
        else:
            await send_service_info(update, context)

    elif data == "activate":
        user_data = get_user(user_id)
        if user_data and user_data[0] and user_data[0] > datetime.now():
            await update.effective_chat.send_message("✅ У вас уже есть активная подписка!", reply_markup=main_menu())
        else:
            sub_link = apply_subscription_payment(user_id, 'simple', 3)
            if sub_link:
                await update.effective_chat.send_message(
                    f"✅ Поздравляем! Вы активировали пробный период на 3 дня.\n\n"
                    f"Теперь вы можете пользоваться нашим VPN без ограничений.\n"
                    f"Наслаждайтесь! 🚀\n\n"
                    f"🔑 **Ваша ссылка на подписку:**\n{sub_link}",
                    parse_mode='Markdown',
                    reply_markup=main_menu()
                )
            else:
                await update.effective_chat.send_message(
                    "❌ Ошибка активации пробного периода. Попробуйте позже или обратитесь в поддержку.",
                    reply_markup=main_menu()
                )

    elif data == "unknown_choice":
        text = (
            "❓ **Не знаете, что выбрать?**\n\n"
            "📱 **Simple** — 249 ₽/месяц\n"
            "• 3 устройства\n"
            "• Безлимитный трафик\n"
            "• Стандартная скорость и резервные серверы\n\n"
            "🚀 **Pro** — 499 ₽/месяц\n"
            "• 5 устройств\n"
            "• Безлимитный трафик\n"
            "• Максимальная скорость и резервные серверы\n"
            "• Возможность не отключать впн когда необходимо зайти в Российские приложения или сайты\n"
            "• С включенным впн работает навигатор и сотовая связь (нет надоедливого - ОТКЛЮЧИТЕ ВПН)\n"
            "• Интернет работает всегда, даже когда у других нет)\n\n"
            "💡 **Совет:** Если вы планируете использовать VPN на нескольких устройствах (телефон, ноутбук, планшет) — выбирайте Pro.\n"
            "Если только на одном-двух устройствах — Simple вам подойдёт.\n\n"
            "🔙 Нажмите «Назад», чтобы вернуться к выбору тарифа."
        )
        keyboard = [
            [InlineKeyboardButton("🔙 Назад к тарифам", callback_data="payment_tariff_back")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
        await update.effective_chat.send_message(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == 'show_channel':
        await update.effective_chat.send_message('Ссылка на канал:\nhttps://t.me/FMH_VPN')

    elif data == "profile":
        await send_profile(update, context)

    elif data == "referral":
        await referral_start(update, context)

    elif data == "main_menu":
        await send_main_menu(update, context)


# ========== ОБРАБОТКА ТЕКСТОВЫХ СООБЩЕНИЙ ==========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = time.time()
    if now - user_last_message[user_id] < MIN_INTERVAL:
        return
    user_last_message[user_id] = now

    text = update.message.text.strip()

    # ===== КАПЧА =====
    if user_id in user_captcha and 'answer' in user_captcha[user_id]:
        try:
            user_answer = int(text)
            correct_answer = user_captcha[user_id]['answer']

            if user_answer == correct_answer:
                del user_captcha[user_id]
                mark_captcha_passed(user_id)

                email, password = generate_site_credentials(user_id)
                if save_site_credentials(user_id, email, password):
                    site_url = "http://217.60.39.78:8080"
                    success_message = (
                        f"✅ **Капча успешно пройдена!**\n\n"
                        f"🎉 Добро пожаловать в FMH-VPN!\n\n"
                        f"🔐 **Ваши данные для входа на сайт:**\n\n"
                        f"📧 **Логин (email):**\n`{email}`\n\n"
                        f"🔑 **Пароль:**\n`{password}`\n\n"
                        f"⚠️ **Сохраните эти данные!** Они понадобятся для входа в личный кабинет на сайте.\n\n"
                        f"🌐 **Перейти на сайт:**\n{site_url}"
                    )
                    await update.message.reply_text(success_message, parse_mode='Markdown')
                else:
                    await update.message.reply_text(
                        "✅ Капча пройдена! ⚠️ Не удалось сразу создать данные для входа на сайт — "
                        "напишите /mylogin позже."
                    )

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
                    f"❌ Неправильно! Попробуйте ещё раз:\n\n**{question} = ?**\n\n"
                    f"Осталось попыток: {3 - user_captcha[user_id]['attempts']}",
                    parse_mode='HTML'
                )
                return
        except ValueError:
            await update.message.reply_text("❌ Введите ЧИСЛО, а не текст.")
            return

    # ===== ВВОД СУММЫ БОНУСОВ ДЛЯ ЧАСТИЧНОЙ ОПЛАТЫ =====
    if context.user_data.get('awaiting_bonus_input'):
        try:
            bonus_amount = int(text)
            max_bonus = context.user_data.get('bonus_max', 0)
            tariff = context.user_data.get('bonus_tariff')
            months = context.user_data.get('bonus_plan')
            full_price = context.user_data.get('full_price', 0)

            if bonus_amount <= 0 or bonus_amount > max_bonus:
                await update.message.reply_text(f"❌ Введите число от 1 до {max_bonus}", reply_markup=back_button())
                return

            context.user_data['bonus_to_use'] = bonus_amount
            context.user_data['payment_amount'] = full_price - bonus_amount
            context.user_data['awaiting_bonus_input'] = False

            conn = get_db_connection()
            c = conn.cursor()
            c.execute('UPDATE users SET bonus_balance = bonus_balance - %s WHERE user_id = %s', (bonus_amount, user_id))
            conn.commit()
            conn.close()

            remaining = full_price - bonus_amount

            if remaining > 0:
                payment_data = create_yookassa_payment(
                    user_id=user_id, amount=remaining, tariff_type=tariff, months=months,
                    description=f"Подписка на медиа контент (остаток) - {tariff.capitalize()} - {months} мес"
                )

                if payment_data:
                    context.user_data['payment_id'] = payment_data['payment_id']
                    keyboard = [
                        [InlineKeyboardButton("💳 Перейти к оплате", url=payment_data['confirmation_url'])],
                        [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
                        [InlineKeyboardButton("🔙 Назад", callback_data="payment_tariff_back")],
                        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
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
                    conn = get_db_connection()
                    c = conn.cursor()
                    c.execute('UPDATE users SET bonus_balance = bonus_balance + %s WHERE user_id = %s',
                              (bonus_amount, user_id))
                    conn.commit()
                    conn.close()
                    await update.message.reply_text("❌ Ошибка создания платежа. Бонусы возвращены.", reply_markup=back_button())
            else:
                sub_link = apply_subscription_payment(user_id, tariff, days=30 * months)
                text = (
                    f"✅ **Подписка полностью оплачена бонусами!** 🎉\n\n"
                    f"📱 Тариф: {tariff.capitalize()}\n"
                    f"📆 Период: {months} месяц(ев)\n"
                    f"💰 Списано бонусов: **{bonus_amount} ₽**"
                )
                if sub_link:
                    text += f"\n\n🔑 **Ваша ссылка на подписку:**\n`{sub_link}`"
                else:
                    text += "\n\n⚠️ Не удалось выдать ключ автоматически, напишите в поддержку."
                await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_menu())

        except ValueError:
            await update.message.reply_text("❌ Введите ЧИСЛО. Например: 100", reply_markup=back_button())
        return

    # ===== ПОДДЕРЖКА =====
    if context.user_data.get('support_mode'):
        user = update.effective_user
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
    bot = app.bot

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mylogin", mylogin_command))

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

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(scheduled_check(bot))

    logger.info("✅ Бот готов!")
    app.run_polling()


if __name__ == '__main__':
    main()