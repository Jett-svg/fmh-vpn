import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()

# ВАЖНО: для MySQL 8.0 строка подключения должна использовать драйвер pymysql
# (нужно установить: pip install pymysql cryptography — cryptography нужна
# для дефолтного в MySQL 8 метода авторизации caching_sha2_password).
#
# Формат DATABASE_URL в .env:
#   mysql+pymysql://user:password@host:3306/dbname?charset=utf8mb4
#
# Если в .env всё ещё указан postgresql:// URL — приложение упадёт с понятной
# ошибкой при старте (нет установленного psycopg2), а не будет молча писать
# не туда, куда нужно.
DATABASE_URL = os.environ['DATABASE_URL']

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # перед каждым использованием проверяет "живо" ли
                           # соединение однострочным SELECT 1; если мертво —
                           # тихо пересоздаёт вместо падения на реальном запросе.
                           # У MySQL есть свой wait_timeout (по умолчанию 8 часов,
                           # но на многих хостингах меньше), который тихо рвёт
                           # неактивные соединения — без pre_ping это давало бы
                           # ту же ошибку "MySQL server has gone away".
    pool_recycle=280,     # принудительно пересоздавать соединения до того, как
                           # их закроет сам MySQL-сервер по wait_timeout
    pool_size=5,
    max_overflow=10,
    connect_args={
        "connect_timeout": 10,  # без этого при заблокированном/недоступном хосте
        # pymysql виснет на TCP-таймауте ОС (может быть много дольше 60 секунд) —
        # именно так рождается загадочный 504 Gateway Timeout от nginx вместо
        # внятной ошибки в логах. Теперь падать будет максимум через 10 секунд
        # с ясным сообщением об ошибке подключения.
    },
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection() -> bool:
    """
    Используется в main.py (lifespan) при старте, чтобы убедиться, что БД
    реально доступна, прежде чем создавать таблицы через Base.metadata.create_all.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        print(f"❌ Ошибка подключения к БД: {e}")
        return False
