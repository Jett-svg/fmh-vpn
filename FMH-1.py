from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
import os

BOT_TOKEN = '8934659898:AAFjnr0OwI5gV3eV05drid5EnsBrCWGV67c'

ADMIN_CHAT_ID = -5119832795

# Главное меню (кнопки)
def main_menu():
    keyboard = [
        [InlineKeyboardButton("📞 Поддержка", callback_data="help")],
        [InlineKeyboardButton("ℹ️ Информация", callback_data="info")],
        [InlineKeyboardButton("🤝 Реферальная программа", callback_data="referral")],
        [InlineKeyboardButton("💸 Оплата", callback_data="payment")],
        [InlineKeyboardButton("🌐 Подключиться", callback_data="connect")]
    ]
    return InlineKeyboardMarkup(keyboard)

# Кнопка "Назад"
def back_button():
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]]
    return InlineKeyboardMarkup(keyboard)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    image_path = 'FMH-VPN.jpg'

    if os.path.exists(image_path):
        with open(image_path, 'rb') as photo:
            await update.message.reply_photo(
                photo=InputFile(photo),
                caption="👋 Привет! Это FMH_VPN.\n\nВыбери действие:",
                reply_markup=main_menu()
            )
    else:
        await update.message.reply_text(
            "👋 Привет! Это FMH_VPN.\n\nВыбери действие:",
            reply_markup=main_menu()
        )

# Обработчик текстовых сообщений (поддержка)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('support_mode'):
        user = update.effective_user
        text = update.message.text

        admin_text = f"📩 Новое обращение от {user.first_name} (@{user.username or 'нет username'}):\n\n{text}"
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text)

        await update.message.reply_text("✅ Ваше сообщение отправлено в поддержку. Мы ответим вам в ближайшее время.")
        context.user_data['support_mode'] = False

# Обработчик кнопок
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "help":
        context.user_data['support_mode'] = True
        await query.message.reply_text(
            "📞 Напишите сообщение поддержке, постараемся ответить оперативно",
            reply_markup=back_button()
        )

    elif query.data == "info":
        await query.message.reply_text(
            "ℹ️ Чтобы узнать информацию посетите наш сайт:\nhttps://example.com",
            reply_markup=back_button()
        )

    elif query.data == "referral":
        await query.message.reply_text(
            "👥 Реферальная программа:\n\n"
            "За каждого приглашенного друга вы и друг получаете по 3 дня подписки.\n\n"
            "С каждой покупки или продления подписки приглашенного пользователя Вы получаете 30% на ваш бонусный счет.\n\n"
            "📨 Поделитесь партнёрской ссылкой:\nhttps://t.me/...\n\n"
            "Например, если вы пригласили 10 пользователей, и каждый из них оформил подписку на 249 рублей, то вы получите 30% от их платежей. Каждый месяц.\n\n"
            "Приглашайте только реальных пользователей, боты будут отфильтрованы",
            reply_markup=back_button()
        )

    elif query.data == "payment":
        await query.message.reply_text(
            "💸 Оплата временно недоступна. Следите за обновлениями.",
            reply_markup=back_button()
        )

    elif query.data == "connect":
        await query.message.reply_text(
            "🌐 Чтобы подключиться, выберите тариф:\n\n"
            "1 месяц — 249 ₽\n"
            "3 месяца — 599 ₽\n"
            "6 месяцев — 999 ₽",
            reply_markup=back_button()
        )

    elif query.data == "main_menu":
        await query.message.reply_text(
            "👋 Главное меню:",
            reply_markup=main_menu()
        )

# Создаём приложение
app = ApplicationBuilder().token(BOT_TOKEN).build()

# Добавляем обработчики
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

print("Бот запущен...")
app.run_polling()