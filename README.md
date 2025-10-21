# Kimhut Café – Flask

A simple Coffee Shop website built with Flask, featuring:
- Pages: Home, Menu (Products), About, Contact
- Cart + Checkout
- Telegram notification to shop owner
- Email invoice to customer (Flask-Mail)

## Quick Start (PyCharm / local)

1. **Create a virtual environment** and install deps:
   ```bash
   pip install -r requirements.txt
   ```

2. **Copy** `.env.example` to `.env` and fill in real values:
   - `FLASK_SECRET_KEY`
   - `MAIL_*` (use Gmail SMTP or your provider; for Gmail, create an App Password)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

3. **Run**:
   ```bash
   python app.py
   ```
   Visit http://127.0.0.1:5000

## Notes
- Telegram requires you to create a Bot via @BotFather and obtain your `chat_id`.
- Emails will log errors to console if `MAIL_*` values are not configured.