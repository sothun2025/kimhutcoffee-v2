# import os
#
# class Config:
#     # Core
#     SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")
#
#     # Flask-Mail
#     MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
#     MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
#     MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True").lower() == "true"
#     MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "False").lower() == "true"
#     MAIL_USERNAME = os.getenv("MAIL_USERNAME")
#     MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
#     MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", MAIL_USERNAME)
#
#     # Telegram
#     TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
#     TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# config.py
import os
from dotenv import load_dotenv

load_dotenv()  # read .env once, here

def _to_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "t", "yes", "y", "on"}

def _to_int(val: str | None, default: int) -> int:
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default

class BaseConfig:
    # Core
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret")

    # Flask-Mail
    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = _to_int(os.getenv("MAIL_PORT"), 587)
    MAIL_USE_TLS = _to_bool(os.getenv("MAIL_USE_TLS", "True"), True)
    MAIL_USE_SSL = _to_bool(os.getenv("MAIL_USE_SSL", "False"), False)
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", MAIL_USERNAME)

    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # Flask specifics
    DEBUG = False
    TESTING = False

class DevelopmentConfig(BaseConfig):
    DEBUG = True

class ProductionConfig(BaseConfig):
    DEBUG = False

class TestingConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
