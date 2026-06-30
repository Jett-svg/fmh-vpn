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
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            subscription_end TEXT,
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
    conn = sqlite3.connect('users.db')
    c = conn.cursor()
    c.execute('SELECT subscription_end, devices, max_devices, bonus_balance, phone FROM users WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result

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

# Главное меню (кнопки)
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📞 Поддержка", callback_data="help")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data="info")],
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="referral")],
        [InlineKeyboardButton("💸 Оплата", callback_data="payment")],
        [InlineKeyboardButton("🌐 Подключиться", callback_data="connect")],
        [InlineKeyboardButton("👤 Личный кабинет", callback_data="profile")],
        [InlineKeyboardButton("⌯⌲ Наш канал", callback_data="show_channel")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Кнопка "Назад"
def back_button():
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

# Приветственное меню для новых пользователей
def welcome_menu():
    keyboard = [
        [InlineKeyboardButton("🎁 Активировать пробный период", callback_data="activate_trial")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Меню после активации пробного периода
def trial_activated_menu():
    keyboard = [
        [InlineKeyboardButton("✅ Активировать", callback_data="activate")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ========== МЕНЮ ОПЛАТЫ ==========

def payment_plan_menu():
    """Меню выбора срока подписки"""
    keyboard = [
        [InlineKeyboardButton("1 месяц — 249 ₽", callback_data="pay_plan_1")],
        [InlineKeyboardButton("3 месяца — 599 ₽", callback_data="pay_plan_3")],
        [InlineKeyboardButton("6 месяцев — 999 ₽", callback_data="pay_plan_6")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def payment_method_menu():
    """Меню выбора способа оплаты"""
    keyboard = [
        [InlineKeyboardButton("🏦 СБП", callback_data="pay_method_sbp")],
        [InlineKeyboardButton("₿ Криптовалюта", callback_data="pay_method_crypto")],
        [InlineKeyboardButton("💳 Банковская карта", callback_data="pay_method_card")],
        [InlineKeyboardButton("🔙 Назад", callback_data="payment_plans")]
    ]
    return InlineKeyboardMarkup(keyboard)

def payment_processing_menu(method):
    """Сообщение после выбора способа оплаты"""
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
        [InlineKeyboardButton("🔙 Назад", callback_data="payment_methods")]
    ]
    return texts.get(method, "Способ оплаты не найден"), InlineKeyboardMarkup(keyboard)

# ========== ФУНКЦИИ ОТПРАВКИ СООБЩЕНИЙ ==========

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

    subscription_end, devices, max_devices, bonus_balance, phone = user_data

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

# ========== РЕФЕРАЛЬНАЯ ПРОГРАММА С ЗАПРОСОМ ТЕЛЕФОНА ==========

async def referral_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user(user_id)

    if user_data and user_data[4]:  # phone уже есть
        await update.effective_chat.send_message(
            "👥 Реферальная программа:\n\n"
            "С каждого приобретения или продления подписки приглашенного пользователя Вы получаете 10% на ваш бонусный счет.\n\n"
            "📨 Поделитесь партнёрской ссылкой:\nhttps://t.me/...\n\n"
            "Например, если вы пригласили 10 пользователей, и каждый из них оформил подписку на 249 рублей, то вы получите 10% от их платежей.\n"
            "Денежные средства на бонусном счете обновляются каждое первое число следующего месяца.\n\n"
            "Приглашайте только реальных пользователей, боты будут отфильтрованы.\n"
            "Вы можете вывести денежные средства с бонусного счета на личную карту или же истратить их в нашем сервисе.",
            reply_markup=back_button()
        )
        return

    await update.effective_chat.send_message(
        "📱 Для участия в реферальной программе укажите ваш номер телефона.\n"
        "Отправьте его в формате: +7XXXXXXXXXX",
        reply_markup=back_button()
    )
    return PHONE

async def referral_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user_id = update.effective_user.id

    # Простая валидация номера
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

# ========== ОБРАБОТЧИКИ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    create_user(user_id)

    if not context.user_data.get('first_time'):
        context.user_data['first_time'] = True
        await send_welcome(update, context)
    else:
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

    elif query.data == "info":
        await update.effective_chat.send_message(
            "ℹ️ Чтобы узнать информацию посетите наш сайт:\nhttps://example.com",
            reply_markup=back_button()
        )


    elif query.data == "payment":
        await update.effective_chat.send_message(
            "💸 **Выберите срок подписки:**",
            parse_mode='Markdown',
            reply_markup=payment_plan_menu()
        )

    elif query.data == "payment_plans":
        await update.effective_chat.send_message(
            "💸 **Выберите срок подписки:**",
            parse_mode='Markdown',
            reply_markup=payment_plan_menu()
        )

    elif query.data == "payment_methods":
        await update.effective_chat.send_message(
            "💳 **Выберите способ оплаты:**",
            parse_mode='Markdown',
            reply_markup=payment_method_menu()
        )

    elif query.data.startswith("pay_plan_"):
        plan = query.data.split("_")[2]
        plan_names = {"1": "1 месяц (249 ₽)", "3": "3 месяца (599 ₽)", "6": "6 месяцев (999 ₽)"}
        context.user_data['selected_plan'] = int(plan)
        await update.effective_chat.send_message(
            f"✅ Вы выбрали **{plan_names[plan]}**.\n\nТеперь выберите способ оплаты:",
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
            "⏳ Проверяем оплату...\n\n"
            "Если оплата прошла, подписка будет активирована в течение 5 минут.",
            reply_markup=back_button()
        )

    elif query.data == "connect":
        await update.effective_chat.send_message(
            "🌐 Подключение недоступно",
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
            'Ссылка на канал:\n'
            'https://t.me/FMH_VPN'
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

# Регистрируем ConversationHandler для реферальной программы
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