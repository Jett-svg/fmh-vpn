"""
Общая логика для бота и сайта: работа с БД (users/subscriptions/payments)
и интеграция с панелями 3x-ui. Раньше этот код был продублирован в bot.py
и app.py по отдельности — из-за этого версии расходились и чинить баги
приходилось в двух местах. Теперь это единственный источник правды.

MySQL 8.0: подключение к БД переиспользует SQLAlchemy engine из database.py
(mysql+pymysql), а не создаёт своё собственное — так у бота и сайта гарантированно
одинаковые настройки пула/таймаутов и один и тот же connection string.
"""

import json
import logging
import os
import secrets
import traceback
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
import pymysql
from dotenv import load_dotenv

from database import engine

logger = logging.getLogger(__name__)
load_dotenv()


# ========== ПОДКЛЮЧЕНИЕ К БД ==========
def get_db_connection():
    """
    Возвращает "сырое" DBAPI-соединение (pymysql) из пула, настроенного
    в database.py (pool_pre_ping, pool_recycle, connect_timeout=10 и т.д.).
    По API (.cursor(), .commit(), .close()) полностью взаимозаменяемо
    с тем, что раньше возвращал psycopg2.connect(...).
    """
    return engine.raw_connection()


# ========== СХЕМА (идемпотентно, можно звать из обоих сервисов при старте) ==========
def _safe_execute(cursor, sql, ignore_errno=None, label=""):
    """
    Выполняет DDL и молча игнорирует ошибку "уже существует" (используется
    для ALTER TABLE ADD COLUMN / CREATE INDEX, у которых в MySQL 8.0 не всегда
    можно надёжно использовать IF NOT EXISTS).
    ignore_errno: код ошибки MySQL, который считаем нормальным (объект уже есть).
      1060 = Duplicate column name
      1061 = Duplicate key name
    """
    try:
        cursor.execute(sql)
    except pymysql.err.InternalError as e:
        errno = e.args[0] if e.args else None
        if ignore_errno and errno == ignore_errno:
            logger.info(f"ℹ️ Пропущено (уже существует): {label}")
        else:
            raise
    except pymysql.err.OperationalError as e:
        errno = e.args[0] if e.args else None
        if ignore_errno and errno == ignore_errno:
            logger.info(f"ℹ️ Пропущено (уже существует): {label}")
        else:
            raise


def init_shared_schema():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY AUTO_INCREMENT,
            subscription_end DATETIME DEFAULT NULL,
            tariff_type VARCHAR(50) DEFAULT NULL,
            devices INT DEFAULT 0,
            max_devices INT DEFAULT 3,
            bonus_balance INT DEFAULT 0,
            phone VARCHAR(32) DEFAULT NULL,
            first_time INT DEFAULT 1,
            referred_by BIGINT DEFAULT NULL,
            captcha_passed BOOLEAN DEFAULT FALSE,
            bonus_paid BOOLEAN DEFAULT FALSE,
            start_date DATETIME DEFAULT NULL,
            reminder_sent INT DEFAULT 0,
            email VARCHAR(255) DEFAULT NULL,
            password_hash VARCHAR(255) DEFAULT NULL,
            email_verified BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY users_email_unique (email),
            UNIQUE KEY users_phone_unique (phone)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ''')

    # Пользователи сайта получают ID из очень большого диапазона, чтобы никогда
    # не пересечься с настоящими Telegram ID (у Postgres для этого была
    # отдельная убывающая SEQUENCE — в MySQL аналога нет, поэтому используем
    # обычный AUTO_INCREMENT, но стартующий с гигантского числа).
    # Выполняется один раз — если счётчик уже сдвинут выше, ALTER TABLE его не понизит.
    # ВАЖНО: значение должно быть намного больше любого реального Telegram ID
    # (сейчас они уже доходят до нескольких миллиардов) и согласовано с проверкой
    # `tg_user_id < 1_000_000_000_000` в build_client_payload() — иначе сайтовый
    # ID может случайно совпасть с чьим-то настоящим Telegram ID.
    try:
        c.execute('ALTER TABLE users AUTO_INCREMENT = 9000000000000')
    except (pymysql.err.InternalError, pymysql.err.OperationalError) as e:
        logger.warning(f"⚠️ Не удалось выставить AUTO_INCREMENT для users: {e}")

    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INT PRIMARY KEY AUTO_INCREMENT,
            user_id BIGINT NOT NULL,
            payment_id VARCHAR(255) NOT NULL,
            amount INT NOT NULL,
            plan VARCHAR(50) NOT NULL,
            duration VARCHAR(50) NOT NULL,
            status VARCHAR(50) DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY payments_payment_id_uidx (payment_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            sub_id VARCHAR(255) PRIMARY KEY,
            user_id BIGINT NOT NULL,
            client_uuid VARCHAR(255) NOT NULL,
            tariff VARCHAR(50) NOT NULL,
            expiry_ms BIGINT NOT NULL,
            email VARCHAR(255) DEFAULT NULL,
            client_password VARCHAR(255) DEFAULT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX subscriptions_user_id_idx (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS telegram_auth_tokens (
            token VARCHAR(255) PRIMARY KEY,
            telegram_user_id BIGINT,
            used BOOLEAN DEFAULT FALSE,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    ''')

    # На случай апгрейда с более старой версии таблицы (без этих колонок/индексов) —
    # добавляем недостающее и молча пропускаем, если уже есть.
    _safe_execute(c, 'ALTER TABLE subscriptions ADD COLUMN email VARCHAR(255) DEFAULT NULL',
                  ignore_errno=1060, label="subscriptions.email")
    _safe_execute(c, 'ALTER TABLE subscriptions ADD COLUMN client_password VARCHAR(255) DEFAULT NULL',
                  ignore_errno=1060, label="subscriptions.client_password")
    _safe_execute(c, 'ALTER TABLE subscriptions ADD COLUMN is_active BOOLEAN DEFAULT TRUE',
                  ignore_errno=1060, label="subscriptions.is_active")
    _safe_execute(c, 'ALTER TABLE users ADD COLUMN email VARCHAR(255) DEFAULT NULL',
                  ignore_errno=1060, label="users.email")
    _safe_execute(c, 'ALTER TABLE users ADD COLUMN password_hash VARCHAR(255) DEFAULT NULL',
                  ignore_errno=1060, label="users.password_hash")
    _safe_execute(c, 'ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT FALSE',
                  ignore_errno=1060, label="users.email_verified")
    _safe_execute(c, 'CREATE UNIQUE INDEX users_email_unique ON users(email)',
                  ignore_errno=1061, label="users_email_unique")
    _safe_execute(c, 'CREATE UNIQUE INDEX users_phone_unique ON users(phone)',
                  ignore_errno=1061, label="users_phone_unique")
    _safe_execute(c, 'CREATE UNIQUE INDEX payments_payment_id_uidx ON payments(payment_id)',
                  ignore_errno=1061, label="payments_payment_id_uidx")

    conn.commit()
    conn.close()
    logger.info("✅ Общая схема БД проверена/создана (vpn_core, MySQL)")


# ========== СЕРВЕРА 3x-ui ==========
SERVERS = {
    "simple": {
        "panel_url": os.environ['SIMPLE_PANEL_URL'],
        "sub_base_url": os.environ['SIMPLE_SUB_URL'],
        "login": os.environ['SIMPLE_LOGIN'],
        "password": os.environ['SIMPLE_PASSWORD'],
        "inbound_ids": [6, 7, 8, 9, 20, 21],
    },
    "pro": {
        "panel_url": os.environ['PRO_PANEL_URL'],
        "sub_base_url": os.environ['PRO_SUB_URL'],
        "login": os.environ['PRO_LOGIN'],
        "password": os.environ['PRO_PASSWORD'],
        "inbound_ids": [1, 2, 18, 19, 20, 21],
    },
}


def build_subscription_link(tariff: str, sub_id: str) -> str:
    server = SERVERS[tariff]
    return f"{server['sub_base_url']}/fmh1/{sub_id}"


def build_client_payload(tg_user_id, tariff, client_uuid, client_password, sub_id, expiry_ms):
    limit_ip = 5 if tariff == "pro" else 3
    # tg_user_id для пользователей, зарегистрированных через сайт, — это огромное
    # AUTO_INCREMENT-число (см. init_shared_schema), а не настоящий Telegram chat ID.
    # Панели такой ID не нужен, передаём 0, чтобы не ломать встроенные уведомления 3x-ui.
    panel_tg_id = tg_user_id if tg_user_id < 1_000_000_000_000 else 0
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

    def get_client_ips(self, email: str) -> list:
        """
        Возвращает список УНИКАЛЬНЫХ IP-адресов, с которых подключался клиент
        (журнал IP, который панель также использует для контроля limitIp).
        Именно из этого можно получить настоящее "количество устройств".

        ВАЖНО: правильный путь для 3x-ui (Clients-раздел) —
        POST /panel/api/clients/ips/{email}, а НЕ /panel/api/inbounds/clientIps/{email}
        (последнего на этой панели просто нет — отсюда были 404).

        Формат ответа: {"success": true, "obj": ["1.2.3.4 (2026-07-29 12:00:00)", ...]}
        — то есть каждая запись это "IP (таймстамп подключения)", а не голый IP,
        и один и тот же IP может встречаться несколько раз (по одной записи на
        каждое подключение). Поэтому обязательно дедуплицируем сам IP.
        """
        r = self.session.post(
            f"{self.base_url}/panel/api/clients/ips/{email}",
            headers={"X-Csrf-Token": self.csrf_token} if self.csrf_token else None,
        )
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(f"❌ 3x-ui clients/ips failed [{r.status_code}] {self.base_url} email={email}: {r.text[:500]}")
            raise
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"3x-ui error: {data}")

        obj = data.get("obj")
        if not obj or obj == "No IP Record":
            return []

        # На случай если где-то отдаётся JSON-строкой, а не массивом
        if isinstance(obj, str):
            try:
                obj = json.loads(obj)
            except (ValueError, TypeError):
                return []

        if not isinstance(obj, list):
            return []

        unique_ips = set()
        for entry in obj:
            if not isinstance(entry, str):
                continue
            # "1.2.3.4 (2026-07-29 12:00:00)" → берём часть до первого пробела
            ip = entry.split(" ")[0].strip()
            if ip:
                unique_ips.add(ip)

        return list(unique_ips)


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
        ON DUPLICATE KEY UPDATE
            client_uuid = VALUES(client_uuid),
            client_password = VALUES(client_password),
            email = VALUES(email),
            tariff = VALUES(tariff),
            expiry_ms = VALUES(expiry_ms),
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


# ========== РЕАЛЬНОЕ КОЛИЧЕСТВО УСТРОЙСТВ ==========
def get_devices_used(record: "SubscriptionRecord") -> int:
    """
    Спрашивает у панели 3x-ui, с скольких разных IP реально подключался
    клиент — это и есть "количество подключённых устройств".

    ВАЖНО: это живой сетевой запрос к панели на каждый вызов (как и остальные
    функции в этом файле — login() перед каждым действием). Если панель
    недоступна или отвечает с ошибкой, исключение пробрасывается наверх —
    вызывающий код (например, /api/user в api.py) должен сам решить,
    что показать пользователю при сбое (например, последнее известное
    значение из БД вместо падения запроса целиком).
    """
    server = SERVERS[record.tariff]
    panel = ThreeXUIClient(server["panel_url"], server["login"], server["password"])
    panel.login()
    ips = panel.get_client_ips(record.email)
    return len(ips)


def sync_devices_used_to_db(user_id: int, record: "SubscriptionRecord") -> int:
    """
    То же самое, что get_devices_used(), но дополнительно сохраняет
    результат в users.devices — чтобы у этого числа было "последнее
    известное" значение на случай, если панель временно недоступна.
    Возвращает актуальное количество устройств.
    """
    devices_used = get_devices_used(record)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE users SET devices = %s WHERE user_id = %s', (devices_used, user_id))
    conn.commit()
    conn.close()

    return devices_used


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
            SET subscription_end = %s, tariff_type = %s, max_devices = %s, devices = 0
            WHERE user_id = %s
        ''', (new_end_date, tariff, max_devices, user_id))
        conn.commit()
        conn.close()
        logger.info(f"✅ БД users обновлена: {user_id} → {tariff}, до {new_end_date}")

        return sub_link

    except Exception as e:
        error_msg = f"❌ Ошибка apply_subscription_payment: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)

        # Опционально дублируем ошибку в отдельный файл — только если путь задан
        # явно через .env (APP_LOG_PATH). Так один и тот же vpn_core.py безопасно
        # работает и на сервере сайта, и на сервере бота: если переменная не задана
        # (или указанной директории не существует), просто ничего не пишем в файл,
        # вместо падения с FileNotFoundError.
        app_log_path = os.environ.get('APP_LOG_PATH')
        if app_log_path:
            try:
                with open(app_log_path, "a") as f:
                    f.write(f"[{datetime.now().isoformat()}] {error_msg}\n")
            except OSError as log_err:
                logger.warning(f"⚠️ Не удалось записать в APP_LOG_PATH={app_log_path}: {log_err}")

        return None


# ========== ИДЕМПОТЕНТНАЯ ОБРАБОТКА ПЛАТЕЖЕЙ ==========
def mark_payment_processed_if_new(payment_id, user_id, amount, tariff, months) -> bool:
    """True только для ПЕРВОГО успешного вызова с этим payment_id — защищает от двойного
    продления, если платёж будет обработан дважды (вебхук + опрос статуса с фронта/бота).
    В MySQL нет ON CONFLICT ... DO NOTHING — аналог: INSERT IGNORE, а факт "была ли
    вставка" смотрим по cursor.rowcount (0 — проигнорировано, 1 — вставлено)."""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT IGNORE INTO payments (user_id, payment_id, amount, plan, duration, status)
        VALUES (%s, %s, %s, %s, %s, 'succeeded')
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
