from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, \
    filters, ConversationHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
import psycopg2
import os
from datetime import datetime, timedelta
from flask import Flask
import threading

# ========== FLASK ДЛЯ RENDER ==========
flask_app = Flask(__name__)


@flask_app.route('/')
@flask_app.route('/health')
def health_check():
    return "Bot is running!", 200


def run_web():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)


threading.Thread(target=run_web, daemon=True).start()


# ========== ПОДКЛЮЧЕНИЕ К POSTGRESQL ==========
def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        database=os.environ.get('DB_NAME', 'fmh'),
        user=os.environ.get('DB_USER', 'postgres'),
        password=os.environ.get('DB_PASSWORD', '255zhh4j'),
        port=os.environ.get('DB_PORT', 5432)
    )


# ========== БАЗА ДАННЫХ ==========
def init_db():
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
            first_time INTEGER DEFAULT 1
        )
    ''')
    conn.commit()
    conn.close()


def get_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        'SELECT subscription_end, devices, max_devices, bonus_balance, phone, first_time FROM users WHERE user_id = %s',
        (user_id,))
    return c.fetchone()


def create_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO users (user_id, subscription_end, devices, max_devices, bonus_balance, phone, first_time)
        VALUES (%s, NULL, 0, 3, 0, NULL, 1)
        ON CONFLICT (user_id) DO NOTHING
    ''', (user_id,))
    conn.commit()
    conn.close()


def mark_user_as_old(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET first_time = 0 WHERE user_id = %s', (user_id,))
    conn.commit()
    conn.close()


def update_user_phone(user_id, phone):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET phone = %s WHERE user_id = %s', (phone, user_id))
    conn.commit()
    conn.close()
    print(f"📝 БД обновлена: user_id={user_id}, phone={phone}")


def update_user_subscription(user_id, days):
    conn = get_db_connection()
    c = conn.cursor()
    end_date = (datetime.now() + timedelta(days=days)).isoformat()
    c.execute('UPDATE users SET subscription_end = %s WHERE user_id = %s', (end_date, user_id))
    conn.commit()
    conn.close()


init_db()

BOT_TOKEN = os.environ.get('BOT_TOKEN', '8934659898:AAFjnr0OwI5gV3eV05drid5EnsBrCWGV67c')
ADMIN_CHAT_ID = int(os.environ.get('ADMIN_CHAT_ID', '-5119832795'))
PHONE = 1
PAYMENT_PHONE = 2


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


# ========== МЕНЮ ОПЛАТЫ ==========
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

    if subscription_end and isinstance(subscription_end, str):
        end_date = datetime.fromisoformat(subscription_end)
        if end_date > datetime.now():
            status, days_left = "✅ Активна", (end_date - datetime.now()).days
            end_text = f"до {end_date.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
        else:
            status, end_text = "❌ Истекла", f"истекла {end_date.strftime('%d.%m.%Y')}"
    else:
        status, end_text = "❌ Не активна", "нет активной подписки"

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
async def payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_data = get_user(update.effective_user.id)
    if user_data and user_data[4]:  # номер уже есть
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu()
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

    print(f"📞 Получен номер: {phone} от user_id: {user_id}")

    if not phone.startswith('+') or len(phone) < 10:
        await update.message.reply_text(
            "❌ Неверный формат номера. Отправьте номер в формате: +7XXXXXXXXXX\n"
            "Или нажмите «Пропустить».",
            reply_markup=skip_button()
        )
        return PAYMENT_PHONE

    update_user_phone(user_id, phone)
    await update.message.reply_text("✅ Номер сохранён!")

    await update.effective_chat.send_message(
        "💸 **Выберите тариф:**",
        parse_mode='Markdown',
        reply_markup=payment_tariff_menu()
    )
    return ConversationHandler.END


async def skip_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.delete()

    await update.effective_chat.send_message(
        "💸 **Выберите тариф:**",
        parse_mode='Markdown',
        reply_markup=payment_tariff_menu()
    )
    return ConversationHandler.END


# ========== РЕФЕРАЛКА (БЕЗ ЗАПРОСА НОМЕРА) ==========
async def referral_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_data = get_user(update.effective_user.id)

    if user_data and user_data[4]:
        await update.effective_chat.send_message(
            "👥 Реферальная программа:\n\n"
            "С каждого приобретения или продления подписки приглашенного пользователя Вы получаете 20% на ваш бонусный счет.\n\n"
            "📨 Поделитесь партнёрской ссылкой:\nhttps://t.me/...\n\n"
            "Например, если вы пригласили 10 пользователей, и каждый из них оформил подписку на 249 рублей, то вы получите 20% от их платежей.\n"
            "Денежные средства на бонусном счете доступны к выводу каждое первое число следующего месяца.\n\n"
            "Приглашайте только реальных пользователей, боты будут отфильтрованы.\n\n"
            "Вы можете вывести денежные средства с бонусного счета на личную карту или же истратить их в нашем сервисе.\n\n"
            "Вывод на карту доступен от 1000р",
            reply_markup=back_button()
        )
        return ConversationHandler.END
    else:
        await update.effective_chat.send_message(
            "👥 Реферальная программа:\n\n"
            "Для участия в реферальной программе необходимо указать номер телефона.\n"
            "Вы можете сделать это при оформлении подписки через кнопку «💸 Оплата».\n\n"
            "После указания номера вам станут доступны реферальные бонусы.",
            reply_markup=back_button()
        )
        return ConversationHandler.END


# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)
    user_data = get_user(user_id)
    if user_data and user_data[5] == 1:
        mark_user_as_old(user_id)
        await send_welcome(update, context)
    else:
        await send_main_menu(update, context)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "help":
        context.user_data['support_mode'] = True
        await update.effective_chat.send_message("📞 Напишите сообщение поддержке, постараемся ответить оперативно",
                                                 reply_markup=back_button())

    elif data == "payment":
        return await payment_start(update, context)

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
    if context.user_data.get('support_mode'):
        user = update.effective_user
        text = update.message.text
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID,
                                       text=f"📩 Новое обращение от {user.first_name} (@{user.username or 'нет username'}):\n\n{text}")
        await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Мы ответим вам в ближайшее время.")
        context.user_data['support_mode'] = False


# ========== ЗАПУСК ==========
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))

# ConversationHandler для оплаты с запросом номера
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
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Бот запущен...")
app.run_polling()