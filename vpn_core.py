"""
Общая логика для бота и сайта: работа с БД (users/subscriptions/payments)
и интеграция с панелями 3x-ui. Раньше этот код был продублирован в bot.py
и app.py по отдельности — из-за этого версии расходились и чинить баги
приходилось в двух местах. Теперь это единственный источник правды.
"""

import os
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
import psycopg2
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv()

# ========== ПОДКЛЮЧЕНИЕ К БД ==========
def get_db_connection():
    return psycopg2.connect(os.environ['DATABASE_URL'])


# ========== СХЕМА (идемпотентно, можно звать из обоих сервисов при старте) ==========
def init_shared_schema():
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
            bonus_paid BOOLEAN DEFAULT FALSE,
            start_date TIMESTAMP DEFAULT NULL,
            reminder_sent INTEGER DEFAULT 0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            payment_id VARCHAR(255) NOT NULL,
            amount INTEGER NOT NULL,
            plan VARCHAR(50) NOT NULL,
            duration VARCHAR(50) NOT NULL,
            status VARCHAR(50) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            sub_id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            client_uuid TEXT NOT NULL,
            tariff TEXT NOT NULL,
            expiry_ms BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS telegram_auth_tokens (
            token VARCHAR(255) PRIMARY KEY,
            telegram_user_id BIGINT,
            used BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    c.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS email TEXT')
    c.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS client_password TEXT')
    c.execute('ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE')
    c.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT')
    c.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT')
    c.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE')

    c.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique_idx
        ON users (email) WHERE email IS NOT NULL AND email <> ''
    ''')
    c.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS users_phone_unique_idx
        ON users (phone) WHERE phone IS NOT NULL AND phone <> ''
    ''')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS payments_payment_id_uidx ON payments(payment_id)')

    c.execute('''
        CREATE SEQUENCE IF NOT EXISTS site_user_id_seq
        MINVALUE -9223372036854775000
        INCREMENT -1
        START -1
    ''')

    conn.commit()
    conn.close()
    logger.info("✅ Общая схема БД проверена/создана (vpn_core)")


# ========== СЕРВЕРА 3x-ui ==========
SERVERS = {
    "simple": {
        "panel_url": os.environ['SIMPLE_PANEL_URL'],
        "sub_base_url": os.environ['SIMPLE_SUB_URL'],
        "login": os.environ['SIMPLE_LOGIN'],
        "password": os.environ['SIMPLE_PASSWORD'],
        "inbound_ids": [1, 2, 4, 5],
    },
    "pro": {
        "panel_url": os.environ['PRO_PANEL_URL'],
        "sub_base_url": os.environ['PRO_SUB_URL'],
        "login": os.environ['PRO_LOGIN'],
        "password": os.environ['PRO_PASSWORD'],
        "inbound_ids": [1, 2, 18, 19],
    },
}


def build_subscription_link(tariff: str, sub_id: str) -> str:
    server = SERVERS[tariff]
    return f"{server['sub_base_url']}/fmh1/{sub_id}"


def build_client_payload(tg_user_id, tariff, client_uuid, client_password, sub_id, expiry_ms):
    limit_ip = 5 if tariff == "pro" else 3
    # tg_user_id отрицателен для пользователей, зарегистрированных через сайт
    # (см. site_user_id_seq) — это не настоящий Telegram chat ID, панели он не нужен
    # в таком виде, передаём 0, чтобы не ломать встроенные уведомления 3x-ui.
    panel_tg_id = tg_user_id if tg_user_id > 0 else 0
    return {
        "id": client_uuid,
        "password": client_password,          # auth/пароль для Hysteria/Trojan-инбаунда
        "email": f"u{abs(tg_user_id)}_{tariff}_{client_uuid[:6]}",
        "flow": "xtls-rprx-vision",            # актуально только для VLESS
        "limitIp": limit_ip,
        "totalGB": 0,
        "expiryTime": expiry_ms,
        "enable": True,
        "tgId": panel_tg_id,
        "subId": sub_id,
        "reset": 0,
    }


# ========== КЛИЕНТ ПАНЕЛИ 3x-ui ==========
class ThreeXUIClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        _parsed = urlsplit(self.base_url)
        self.origin = f"{_parsed.scheme}://{_parsed.netloc}"
        self.csrf_token = None
        self.session = httpx.Client(
            timeout=15,
            verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def login(self):
        r_get = self.session.get(f"{self.base_url}/")
        try:
            r_get.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"❌ 3x-ui GET перед логином не удался [{r_get.status_code}] {self.base_url}: {r_get.text[:500]}")
            raise

        csrf_token = None
        try:
            r_csrf = self.session.get(f"{self.base_url}/csrf-token")
            if r_csrf.status_code == 200:
                body = r_csrf.text.strip()
                try:
                    data = r_csrf.json()
                    csrf_token = (
                        data.get("token") or data.get("csrfToken")
                        or data.get("obj") or data.get("data")
                        if isinstance(data, dict) else data
                    )
                except ValueError:
                    csrf_token = body
            else:
                logger.warning(f"⚠️ 3x-ui csrf-token эндпоинт вернул {r_csrf.status_code} — пробуем без него")
        except httpx.HTTPError as e:
            logger.warning(f"⚠️ Не удалось получить csrf-token у {self.base_url}: {e}")

        login_headers = {
            "Origin": self.origin,
            "Referer": f"{self.base_url}/",
        }
        if csrf_token:
            login_headers["X-Csrf-Token"] = csrf_token
        self.csrf_token = csrf_token

        r = self.session.post(
            f"{self.base_url}/login",
            data={"username": self.username, "password": self.password},
            headers=login_headers,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"❌ 3x-ui login failed [{r.status_code}] {self.base_url}: {r.text[:500]}")
            raise

    def add_client(self, inbound_ids: list, client: dict):
        payload = {"client": client, "inboundIds": inbound_ids}
        r = self.session.post(
            f"{self.base_url}/panel/api/clients/add",
            json=payload,
            headers={"X-Csrf-Token": self.csrf_token} if self.csrf_token else None,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"❌ 3x-ui clients/add failed [{r.status_code}] {self.base_url} inbounds={inbound_ids}: {r.text[:500]}")
            raise
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"3x-ui error: {data}")
        return data

    def update_client(self, email: str, client: dict):
        r = self.session.post(
            f"{self.base_url}/panel/api/clients/update/{email}",
            json=client,
            headers={"X-Csrf-Token": self.csrf_token} if self.csrf_token else None,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"❌ 3x-ui clients/update failed [{r.status_code}] {self.base_url} email={email}: {r.text[:500]}")
            raise
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"3x-ui error: {data}")
        return data

    def delete_client(self, email: str):
        r = self.session.post(
            f"{self.base_url}/panel/api/clients/del/{email}",
            headers={"X-Csrf-Token": self.csrf_token} if self.csrf_token else None,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"❌ 3x-ui clients/del failed [{r.status_code}] {self.base_url} email={email}: {r.text[:500]}")
            raise
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"3x-ui error: {data}")
        return data


# ========== ЗАПИСЬ ПОДПИСКИ ==========
class SubscriptionRecord:
    def __init__(self, sub_id, user_id, client_uuid, client_password, email, tariff, expiry_ms):
        self.sub_id = sub_id
        self.user_id = user_id
        self.client_uuid = client_uuid
        self.client_password = client_password
        self.email = email
        self.tariff = tariff
        self.expiry_ms = expiry_ms


def save_subscription_to_db(user_id, sub_id, client_uuid, client_password, email, tariff, expiry_ms):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE subscriptions SET is_active = FALSE WHERE user_id = %s', (user_id,))
    c.execute('''
        INSERT INTO subscriptions (sub_id, user_id, client_uuid, client_password, email, tariff, expiry_ms, is_active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
        ON CONFLICT (sub_id) DO UPDATE
        SET client_uuid = EXCLUDED.client_uuid,
            client_password = EXCLUDED.client_password,
            email = EXCLUDED.email,
            tariff = EXCLUDED.tariff,
            expiry_ms = EXCLUDED.expiry_ms,
            is_active = TRUE
    ''', (sub_id, user_id, client_uuid, client_password, email, tariff, expiry_ms))
    conn.commit()
    conn.close()
    logger.info(f"✅ Подписка {sub_id} сохранена для user_id={user_id} (тариф={tariff})")


def update_subscription_expiry_in_db(sub_id, expiry_ms):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE subscriptions SET expiry_ms = %s WHERE sub_id = %s', (expiry_ms, sub_id))
    conn.commit()
    conn.close()


def mark_subscription_inactive(sub_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE subscriptions SET is_active = FALSE WHERE sub_id = %s', (sub_id,))
    conn.commit()
    conn.close()


def get_subscription_from_db(sub_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT sub_id, user_id, client_uuid, client_password, email, tariff, expiry_ms
        FROM subscriptions WHERE sub_id = %s
    ''', (sub_id,))
    row = c.fetchone()
    conn.close()
    return SubscriptionRecord(*row) if row else None


def get_active_subscription_by_user(user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        SELECT sub_id, user_id, client_uuid, client_password, email, tariff, expiry_ms
        FROM subscriptions
        WHERE user_id = %s AND is_active = TRUE
        ORDER BY created_at DESC
        LIMIT 1
    ''', (user_id,))
    row = c.fetchone()
    conn.close()
    return SubscriptionRecord(*row) if row else None


# ========== ВЫДАЧА / ПРОДЛЕНИЕ / УДАЛЕНИЕ КЛИЕНТА В 3x-ui ==========
def issue_subscription(tg_user_id: int, tariff: str, expiry_ms: int) -> str:
    client_uuid = str(uuid.uuid4())
    client_password = secrets.token_hex(12)
    sub_id = secrets.token_hex(8)

    client_payload = build_client_payload(tg_user_id, tariff, client_uuid, client_password, sub_id, expiry_ms)
    email = client_payload["email"]

    server = SERVERS[tariff]
    panel = ThreeXUIClient(server["panel_url"], server["login"], server["password"])
    panel.login()
    panel.add_client(server["inbound_ids"], client_payload)

    save_subscription_to_db(tg_user_id, sub_id, client_uuid, client_password, email, tariff, expiry_ms)
    return build_subscription_link(tariff, sub_id)


def extend_subscription(record: SubscriptionRecord, new_expiry_ms: int):
    client_payload = build_client_payload(
        record.user_id, record.tariff, record.client_uuid, record.client_password,
        record.sub_id, new_expiry_ms,
    )
    server = SERVERS[record.tariff]
    panel = ThreeXUIClient(server["panel_url"], server["login"], server["password"])
    panel.login()
    panel.update_client(record.email, client_payload)
    update_subscription_expiry_in_db(record.sub_id, new_expiry_ms)


def delete_subscription(record: SubscriptionRecord):
    server = SERVERS[record.tariff]
    panel = ThreeXUIClient(server["panel_url"], server["login"], server["password"])
    try:
        panel.login()
        panel.delete_client(record.email)
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить {record.email} на {server['panel_url']}: {e}")


# ========== ГЛАВНАЯ ФУНКЦИЯ: АКТИВАЦИЯ / ПРОДЛЕНИЕ / СМЕНА ТАРИФА ==========
def apply_subscription_payment(user_id: int, tariff: str, days: int):
    """
    - Тот же тариф активен → продлеваем существующего клиента в 3x-ui (дни суммируются).
    - Тариф другой (или подписки не было/истекла) → удаляем старого клиента (если был),
      создаём нового.

    Порядок операций: сначала синхронизируемся с 3x-ui, и только при подтверждённом
    успехе фиксируем изменения в users. Раньше было наоборот — users коммитился первым,
    и при сбое связи с 3x-ui личный кабинет показывал "активная подписка" без реального
    ключа. Теперь такого разрыва между БД и реальным состоянием панели быть не должно.

    Возвращает ссылку на подписку или None при ошибке (тогда состояние users не меняется).
    """
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT tariff_type, subscription_end FROM users WHERE user_id = %s', (user_id,))
        result = c.fetchone()
        conn.close()

        current_tariff = result[0] if result else None
        current_end = result[1] if result else None
        is_active = current_end and current_end > datetime.now()
        same_tariff = is_active and current_tariff == tariff and tariff is not None

        new_end_date = (current_end + timedelta(days=days)) if same_tariff else (datetime.now() + timedelta(days=days))
        max_devices = 5 if tariff == 'pro' else 3
        expiry_ms = int(new_end_date.timestamp() * 1000)

        # 1. Синхронизация с 3x-ui — ДО записи в users
        if same_tariff:
            record = get_active_subscription_by_user(user_id)
            if record and record.tariff == tariff:
                try:
                    extend_subscription(record, expiry_ms)
                    sub_link = build_subscription_link(record.tariff, record.sub_id)
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось продлить {record.email} в 3x-ui ({e}), пересоздаю клиента")
                    mark_subscription_inactive(record.sub_id)
                    sub_link = issue_subscription(user_id, tariff, expiry_ms)
            else:
                logger.warning(f"⚠️ Не найдена активная запись subscriptions для {user_id}, создаю новую")
                sub_link = issue_subscription(user_id, tariff, expiry_ms)
        else:
            old_record = get_active_subscription_by_user(user_id)
            if old_record:
                try:
                    delete_subscription(old_record)
                    logger.info(f"🗑️ Удалён клиент старого тарифа {old_record.tariff} для {user_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Не удалось удалить старого клиента {old_record.email} ({e})")
                mark_subscription_inactive(old_record.sub_id)
            sub_link = issue_subscription(user_id, tariff, expiry_ms)

        # 2. Только теперь фиксируем users — 3x-ui подтвердил успех
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE users
            SET subscription_end = %s, tariff_type = %s, max_devices = %s
            WHERE user_id = %s
        ''', (new_end_date, tariff, max_devices, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ БД users обновлена: {user_id} → {tariff}, до {new_end_date}")

        return sub_link

    except Exception as e:
        logger.error(f"❌ Ошибка apply_subscription_payment: {e}")
        return None


# ========== ИДЕМПОТЕНТНАЯ ОБРАБОТКА ПЛАТЕЖЕЙ ==========
def mark_payment_processed_if_new(payment_id, user_id, amount, tariff, months) -> bool:
    """True только для ПЕРВОГО успешного вызова с этим payment_id — защищает от двойного
    продления, если платёж будет обработан дважды (вебхук + опрос статуса с фронта/бота)."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO payments (user_id, payment_id, amount, plan, duration, status)
        VALUES (%s, %s, %s, %s, %s, 'succeeded')
        ON CONFLICT (payment_id) DO NOTHING
    ''', (user_id, payment_id, int(amount), tariff, str(months)))
    conn.commit()
    is_first_time = c.rowcount == 1
    conn.close()
    return is_first_time


def process_successful_payment(user_id, payment_id, amount, tariff_type, months):
    """Базовая версия без реферальных бонусов — используется сайтом.
    Бот оборачивает эту функцию и дополнительно начисляет бонус рефереру."""
    if not mark_payment_processed_if_new(payment_id, user_id, amount, tariff_type, months):
        logger.info(f"ℹ️ Платёж {payment_id} уже был обработан ранее — пропускаем повторное продление")
        record = get_active_subscription_by_user(user_id)
        return build_subscription_link(record.tariff, record.sub_id) if record else None

    return apply_subscription_payment(user_id, tariff_type, days=30 * months)


# ========== СВЯЗКА АККАУНТОВ ПО ТЕЛЕФОНУ (бот-регистрация ⇄ более старая сайт-запись) ==========
def merge_user_by_phone(canonical_user_id, phone):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        'SELECT user_id, subscription_end, tariff_type, max_devices FROM users WHERE phone = %s AND user_id != %s',
        (phone, canonical_user_id)
    )
    other = c.fetchone()
    if not other:
        conn.close()
        return False

    other_id, other_end, other_tariff, other_max = other
    c.execute('SELECT subscription_end FROM users WHERE user_id = %s', (canonical_user_id,))
    mine = c.fetchone()
    my_end = mine[0] if mine else None

    if other_end and (not my_end or other_end > my_end):
        c.execute('''
            UPDATE users SET subscription_end = %s, tariff_type = %s, max_devices = %s
            WHERE user_id = %s
        ''', (other_end, other_tariff, other_max, canonical_user_id))
        c.execute('UPDATE subscriptions SET user_id = %s WHERE user_id = %s AND is_active = TRUE',
                  (canonical_user_id, other_id))
        logger.info(f"🔗 Слияние: подписка перенесена с {other_id} на {canonical_user_id} (телефон {phone})")

    c.execute('UPDATE users SET phone = NULL WHERE user_id = %s', (other_id,))
    conn.commit()
    conn.close()
    return True