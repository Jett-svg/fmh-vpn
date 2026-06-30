from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters, ConversationHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
import os
import sqlite3
from datetime import datetime, timedelta

BOT_TOKEN = '8934659898:AAFjnr0OwI5gV3eV05drid5EnsBrCWGV67c'

ADMIN_CHAT_ID = -5119832795

# Состояния для ConversationHandler
PHONE = 1

# ========== БАЗА ДАННЫХ ==========
def init_db():
    conn = sqlite3.connect('users.db')
    c = conn.cursor()

    # Создаём таблицу, если её нет
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_end TEXT,
            devices INTEGER DEFAULT 0,
            max_devices INTEGER DEFAULT 3,
            bonus_balance INTEGER DEFAULT 0,
            phone TEXT
        )
    ''')

    # Добавляем колонку first_time, если её нет
    try:
        c.execute('ALTER TABLE users ADD COLUMN first_time INTEGER DEFAULT 1')
    except sqlite3.OperationalError:
        pass  # Колонка уже существует

    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT subscription_end, devices, max_devices, bonus_balance, phone, first_time FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def mark_user_as_old(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET first_time = 0 WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def create_user(user_id):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, subscription_end, devices, max_devices, bonus_balance, phone) VALUES (?, ?, ?, ?, ?, ?)',
              (user_id, None, 0, 3, 0, None))
    conn.commit()
    conn.close()

def update_user_phone(user_id, phone):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('UPDATE users SET phone = ? WHERE user_id = ?', (phone, user_id))
    conn.commit()
    conn.close()

def update_user_subscription(user_id, days):
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    end_date = (datetime.now() + timedelta(days=days)).isoformat()
    c.execute('UPDATE users SET subscription_end = ? WHERE user_id = ?', (end_date, user_id))
    conn.commit()
    conn.close()

# Инициализируем базу при запуске
init_db()

# ========== КНОПКИ И МЕНЮ ==========

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
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

def welcome_menu():
    keyboard = [
        [InlineKeyboardButton("🎁 Активировать пробный период", callback_data="activate_trial")]
    ]
    return InlineKeyboardMarkup(keyboard)

def trial_activated_menu():
    keyboard = [
        [InlineKeyboardButton("✅ Активировать", callback_data="activate")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== МЕНЮ ОПЛАТЫ ==========

def payment_tariff_menu():
    keyboard = [
        [InlineKeyboardButton("📱 Simple — 249 ₽", callback_data="pay_tariff_simple")],
        [InlineKeyboardButton("🚀 Pro — 499 ₽", callback_data="pay_tariff_pro")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)

def payment_plan_menu(tariff):
    prices = {
        "simple": {"1": 249, "3": 599, "6": 999},
        "pro": {"1": 499, "3": 999, "6": 1499}
    }
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
        "sbp": "🏦 **Оплата через СБП**\n\n"
               "Переведите сумму на номер телефона:\n"
               "`+7 999 123-45-67`\n\n"
               "После оплаты нажмите кнопку «Проверить оплату».",
        "crypto": "₿ **Оплата криптовалютой**\n\n"
                  "Отправьте USDT (TRC20) на адрес:\n"
                  "`TXYZ...`\n\n"
                  "После оплаты нажмите кнопку «Проверить оплату».",
        "card": "💳 **Оплата банковской картой**\n\n"
                "Перейдите по ссылке для оплаты:\n"
                "🔗 [Оплатить картой](https://example.com/pay)\n\n"
                "После оплаты нажмите кнопку «Проверить оплату»."
    }
    keyboard = [
        [InlineKeyboardButton("✅ Проверить оплату", callback_data="check_payment")],
        [InlineKeyboardButton("🔙 Назад", callback_data="payment_methods_back")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
    ]
    return texts.get(method, "Способ оплаты не найден"), InlineKeyboardMarkup(keyboard)

# ========== ФУНКЦИИ ОТПРАВКИ ==========

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message=None):
    image_path = 'FMH-VPN.jpg'
    caption = "👋 Привет! Это FMH_VPN.\n\nВыбери действие:"

    if os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            await update.effective_chat.send_photo(
                photo=InputFile(photo),
                caption=caption,
                reply_markup=main_menu()
            )
    else:
        await update.effective_chat.send_message(
            text=caption,
            reply_markup=main_menu()
        )

async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    image_path = 'FMH-VPN.jpg'
    caption = (
        "👋 Привет!\n\n"
        "Если вы устали от лагающих и не работающих VPN — тогда вы по адресу.\n\n"
        "Чтобы проверить, насколько мы хороши, дарим тебе пробный период на 3 дня."
    )
    create_user(update.effective_user.id)
    if os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            await update.message.reply_photo(
                photo=InputFile(photo),
                caption=caption,
                reply_markup=welcome_menu()
            )
    else:
        await update.message.reply_text(
            text=caption,
            reply_markup=welcome_menu()
        )

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
        "• 399 ₽/месяц (Pro подписка)"
    )
    await update.effective_chat.send_message(
        text=text,
        reply_markup=trial_activated_menu()
    )

async def send_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data is None:
        await update.effective_chat.send_message(
            "⚠️ Вы не зарегистрированы. Напишите /start для регистрации.",
            reply_markup=back_button()
        )
        return

    # Теперь 6 значений: subscription_end, devices, max_devices, bonus_balance, phone, first_time
    subscription_end, devices, max_devices, bonus_balance, phone, first_time = user_data

    # ... остальной код без изменений

    if subscription_end:
        end_date = datetime.fromisoformat(subscription_end)
        if end_date > datetime.now():
            status = "✅ Активна"
            days_left = (end_date - datetime.now()).days
            end_text = f"до {end_date.strftime('%d.%m.%Y')} (осталось {days_left} дн.)"
        else:
            status = "❌ Истекла"
            end_text = f"истекла {end_date.strftime('%d.%m.%Y')}"
    else:
        status = "❌ Не активна"
        end_text = "нет активной подписки"

    devices_used = devices
    devices_left = max_devices - devices

    text = (
        "👤 **Личный кабинет**\n\n"
        f"📅 **Статус подписки:** {status}\n"
        f"📆 **Окончание:** {end_text}\n\n"
        f"📱 **Устройства:** {devices_used} из {max_devices} использовано\n"
        f"✅ **Свободно:** {devices_left} устройств\n\n"
        f"💰 **Бонусный счёт:** {bonus_balance} ₽"
        f"📞 **Телефон:** {phone or 'Не указан'}"
    )
    await update.effective_chat.send_message(
        text=text,
        parse_mode='Markdown',
        reply_markup=back_button()
    )

# ========== РЕФЕРАЛЬНАЯ ПРОГРАММА ==========

async def referral_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data and user_data[4]:
        await update.effective_chat.send_message(
            "👥 Реферальная программа:\n\n"
            "С каждого приобретения или продления подписки приглашенного пользователя Вы получаете 10% на ваш бонусный счет.\n\n"
            "📨 Поделитесь партнёрской ссылкой:\nhttps://t.me/...\n\n"
            "Например, если вы пригласили 10 пользователей, и каждый из них оформил подписку на 249 рублей, то вы получите 20% от их платежей.\n"
            "Денежные средства на бонусном счете доступны к выводу каждое первое число следующего месяца.\n\n"
            "Приглашайте только реальных пользователей, боты будут отфильтрованы.\n\n"
            "Вы можете вывести денежные средства с бонусного счета на личную карту или же истратить их в нашем сервисе.\n\n"
            'Вывод на карту доступен от 1000р',
            reply_markup=back_button()
        )
        return ConversationHandler.END

    await update.effective_chat.send_message(
        "📱 Для участия в реферальной программе укажите ваш номер телефона.\n"
        "Отправьте его в формате: +7XXXXXXXXXX",
        reply_markup=back_button()
    )
    return PHONE

async def referral_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user_id = update.effective_user.id

    if not phone.startswith('+') or len(phone) < 10:
        await update.message.reply_text(
            "❌ Неверный формат номера. Отправьте номер в формате: +7XXXXXXXXXX"
        )
        return PHONE

    update_user_phone(user_id, phone)

    await update.message.reply_text(
        "✅ Номер сохранён!\n\n"
        "👥 Реферальная программа:\n\n"
        "С каждого приобретения или продления подписки приглашенного пользователя Вы получаете 10% на ваш бонусный счет.\n\n"
        "📨 Поделитесь партнёрской ссылкой:\nhttps://t.me/...\n\n"
        "Например, если вы пригласили 10 пользователей, и каждый из них оформил подписку на 249 рублей, то вы получите 10% от их платежей.\n"
        "Денежные средства на бонусном счете обновляются каждое первое число следующего месяца.\n\n"
        "Приглашайте только реальных пользователей, боты будут отфильтрованы.\n"
        "Вы можете вывести денежные средства с бонусного счета на личную карту или же истратить их в нашем сервисе.",
        reply_markup=back_button()
    )
    return ConversationHandler.END

async def referral_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Ввод номера отменён.",
        reply_markup=back_button()
    )
    return ConversationHandler.END

# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)

    # Получаем данные пользователя из БД
    user_data = get_user(user_id)
    first_time = user_data[5] if user_data else 1  # 5-й индекс — first_time

    if first_time == 1:
        # Первый раз — показываем приветствие
        mark_user_as_old(user_id)  # помечаем, что уже не новый
        await send_welcome(update, context)
    else:
        # Старый пользователь — сразу в меню
        await send_main_menu(update, context)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "help":
        context.user_data['support_mode'] = True
        await update.effective_chat.send_message(
            "📞 Напишите сообщение поддержке, постараемся ответить оперативно",
            reply_markup=back_button()
        )

    elif query.data == "payment":
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu()
        )

    elif query.data == "payment_methods_back":
        # Возвращаемся к выбору способов оплаты (сохраняем выбранный тариф)
        tariff = context.user_data.get('selected_tariff', 'simple')
        await update.effective_chat.send_message(
            f"💳 **Выберите способ оплаты для тарифа {tariff.capitalize()}:**",
            parse_mode='Markdown',
            reply_markup=payment_method_menu()
        )

    elif query.data == "payment_tariff_back":
        await update.effective_chat.send_message(
            "💸 **Выберите тариф:**",
            parse_mode='Markdown',
            reply_markup=payment_tariff_menu()
        )

    elif query.data == "pay_tariff_simple":
        context.user_data['selected_tariff'] = 'simple'
        await update.effective_chat.send_message(
            "📱 **Тариф Simple**\n\nВыберите срок подписки:",
            parse_mode='Markdown',
            reply_markup=payment_plan_menu('simple')
        )

    elif query.data == "pay_tariff_pro":
        context.user_data['selected_tariff'] = 'pro'
        await update.effective_chat.send_message(
            "🚀 **Тариф Pro**\n\nВыберите срок подписки:",
            parse_mode='Markdown',
            reply_markup=payment_plan_menu('pro')
        )

    elif query.data.startswith("pay_plan_"):
        parts = query.data.split("_")
        tariff = parts[2]
        months = parts[3]
        prices = {"simple": {"1": 249, "3": 599, "6": 999}, "pro": {"1": 499, "3": 999, "6": 1499}}
        price = prices[tariff][months]
        context.user_data['selected_plan'] = int(months)

        await update.effective_chat.send_message(
            f"✅ Вы выбрали **{tariff.capitalize()}** на {months} месяц(ев).\n"
            f"💰 Сумма: {price} ₽\n\nВыберите способ оплаты:",
            parse_mode='Markdown',
            reply_markup=payment_method_menu()
        )

    elif query.data.startswith("pay_method_"):
        method = query.data.split("_")[2]
        text, keyboard = payment_processing_menu(method)
        await update.effective_chat.send_message(
            text,
            parse_mode='Markdown',
            reply_markup=keyboard
        )

    elif query.data == "check_payment":
        await update.effective_chat.send_message(
            "⏳ Проверяем оплату...\n\nЕсли оплата прошла, подписка будет активирована в течение 5 минут.",
            reply_markup=back_button()
        )

    elif query.data == "connect":
        await update.effective_chat.send_message(
            "🌐 Подключение временно недоступно.\nПожалуйста, оплатите подписку через кнопку «💸 Оплата».",
            reply_markup=back_button()
        )

    elif query.data == "activate_trial":
        update_user_subscription(update.effective_user.id, 3)
        await send_service_info(update, context)

    elif query.data == "activate":
        await update.effective_chat.send_message(
            "✅ Поздравляем! Вы активировали пробный период на 3 дня.\n\n"
            "Теперь вы можете пользоваться нашим VPN без ограничений.\n"
            "Наслаждайтесь! 🚀",
            reply_markup=main_menu()
        )

    elif query.data == 'show_channel':
        await update.effective_chat.send_message(
            'Ссылка на канал:\nhttps://t.me/FMH_VPN'
        )

    elif query.data == "profile":
        await send_profile(update, context)

    elif query.data == "main_menu":
        await send_main_menu(update, context)

# Обработчик текстовых сообщений (поддержка)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('support_mode'):
        user = update.effective_user
        text = update.message.text

        admin_text = f"📩 Новое обращение от {user.first_name} (@{user.username or 'нет username'}):\n\n{text}"
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text)

        await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Мы ответим вам в ближайшее время.")
        context.user_data['support_mode'] = False

# ========== ЗАПУСК ==========
app = ApplicationBuilder().token(BOT_TOKEN).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(referral_start, pattern="^referral")],
    states={
        PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, referral_phone)],
    },
    fallbacks=[CommandHandler("cancel", referral_cancel)],
)

app.add_handler(CommandHandler("start", start))
app.add_handler(conv_handler)
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Бот запущен...")
app.run_polling()