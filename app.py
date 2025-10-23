import os
import json
import copy
import threading
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from flask import (
    Flask, render_template, session, redirect, url_for,
    request, flash, jsonify
)
from flask_mail import Mail, Message
import requests
from dotenv import load_dotenv
load_dotenv()

from bakong_khqr import KHQR
import qrcode

# Optional Redis (for server-side orders + idempotency)
try:
    import redis
except Exception:  # redis lib not installed
    redis = None

from config import DevelopmentConfig, ProductionConfig, TestingConfig

# ----------------- Redis helpers (optional) -----------------
_r = None
if os.getenv("REDIS_URL") and redis is not None:
    _r = redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)

def orders_save(md5: str, order: dict, ttl_sec: int = 3600):
    """Persist order (Redis if available, else session)."""
    if _r:
        _r.setex(f"order:{md5}", ttl_sec, json.dumps(order))
    else:
        pending = session.get("pending_orders", {})
        pending[md5] = order
        session["pending_orders"] = pending
        session.modified = True

def orders_get(md5: str):
    if _r:
        val = _r.get(f"order:{md5}")
        return json.loads(val) if val else None
    else:
        return session.get("pending_orders", {}).get(md5)

def orders_update(md5: str, updater):
    """Read-modify-write helper."""
    o = orders_get(md5)
    if not o:
        return None
    o2 = updater(o) or o
    orders_save(md5, o2)
    return o2

# -------- idempotency lock (Redis SETNX -> fallback to process lock) --------
_locks = {}
_locks_guard = threading.Lock()

def acquire_notify_lock(md5: str, ttl: int = 60) -> bool:
    if _r:
        return _r.set(f"notify_lock:{md5}", "1", nx=True, ex=ttl) is True
    # fallback (per-process, not multi-instance safe)
    with _locks_guard:
        lock = _locks.get(md5)
        if not lock:
            lock = threading.Lock()
            _locks[md5] = lock
    return lock.acquire(blocking=False)

def release_notify_lock(md5: str):
    if _r:
        _r.delete(f"notify_lock:{md5}")
    else:
        try:
            _locks.get(md5).release()
        except Exception:
            pass

# ----------------- TG message builder -----------------
def build_tg_lines(order: dict) -> str:
    from html import escape
    cust = order.get("customer", {}) or {}
    items = order.get("items", []) or []
    lines = [
        "<b>New Paid Order</b>",
        f"Name: {escape(cust.get('name',''))}",
        f"Email: {escape(cust.get('email',''))}",
        f"Phone: {escape(cust.get('phone',''))}",
        f"Address: {escape(cust.get('address',''))}",
        "",
        "Items:",
    ]
    for it in items:
        name = escape(str(it.get("name","")))
        qty = it.get("qty", 1)
        line_total = it.get("line_total", "0.00")
        lines.append(f"- {qty} x {name} (${line_total})")
    lines.append(f"\nSubtotal: ${order.get('subtotal','0.00')} {order.get('currency','USD')}")
    lines.append(f"Time: {datetime.now():%Y-%m-%d %H:%M}")
    return "\n".join(lines)

# ----------------- App factory -----------------
def create_app():
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # KHQR
    BAKONG_TOKEN = os.getenv("BAKONG_TOKEN", "")
    khqr = KHQR(BAKONG_TOKEN) if BAKONG_TOKEN else None

    # Config by APP_ENV
    env_name = (os.getenv("APP_ENV") or "development").lower()
    cfg = {
        "development": DevelopmentConfig,
        "production": ProductionConfig,
        "testing": TestingConfig,
    }.get(env_name, DevelopmentConfig)
    app.config.from_object(cfg)

    # Good cookie defaults for prod
    app.config.setdefault("SESSION_COOKIE_SECURE", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    app.config.setdefault("SECRET_KEY", os.getenv("SECRET_KEY", "change-me"))

    mail = Mail(app)

    # Load products
    with open(os.path.join(app.root_path, "products.json"), "r", encoding="utf-8") as f:
        PRODUCTS = json.load(f)

    # Helpers
    def _money(x):
        return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def cart_items():
        cart = session.get("cart", {})
        items = []
        for pid, qty in cart.items():
            product = next((p for p in PRODUCTS if p["id"] == int(pid)), None)
            if product:
                line_total = _money(Decimal(product["price"]) * Decimal(qty))
                items.append({
                    "id": product["id"],
                    "name": product["name"],
                    "price": _money(product["price"]),
                    "qty": int(qty),
                    "line_total": line_total
                })
        subtotal = _money(sum(i["line_total"] for i in items)) if items else _money(0)
        return items, subtotal

    def send_telegram(text: str) -> bool:
        token = app.config.get("TELEGRAM_BOT_TOKEN")
        chat_id = app.config.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            app.logger.error("[Telegram] Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        try:
            r = requests.post(url, json=payload, timeout=15)
            app.logger.info("[Telegram] status=%s body=%s", r.status_code, r.text[:300])
            try:
                j = r.json()
            except Exception:
                j = {}
            return r.ok and j.get("ok") is True
        except Exception:
            app.logger.exception("[Telegram] send failed")
            return False

    def send_invoice_email(customer: dict, items: list, subtotal) -> bool:
        if not app.config.get("MAIL_USERNAME") or not app.config.get("MAIL_DEFAULT_SENDER"):
            app.logger.error("[Mail] MAIL_* not configured correctly")
            return False
        lines = [f"- {i['qty']} x {i['name']} (${i['line_total']})" for i in items]
        body = "\n".join(lines) + f"\n\nSubtotal: ${subtotal}\nTime: {datetime.now():%Y-%m-%d %H:%M}"
        msg = Message(
            subject="Your Kimhut Café Invoice",
            recipients=[customer.get("email") or app.config["MAIL_DEFAULT_SENDER"]],
            body=(f"Hello {customer.get('name', 'Customer')},\n\n"
                  f"Thanks for your order!\n\n{body}\n\n"
                  f"Delivery to:\n{customer.get('address', '(none)')}\n"
                  f"Phone: {customer.get('phone', '(none)')}\n\n— Kimhut Café"),
            sender=app.config["MAIL_DEFAULT_SENDER"],
        )
        try:
            mail.send(msg)
            app.logger.info("[Mail] sent to %s", msg.recipients)
            return True
        except Exception:
            app.logger.exception("[Mail] send failed")
            return False

    # ---------- Routes ----------
    @app.route("/")
    def home():
        return render_template("home.html")

    @app.route("/about")
    def about():
        return render_template("about.html")

    # Contact
    def send_contact_ack(to_email: str, name: str, original_message: str):
        if not to_email or not app.config.get("MAIL_USERNAME"):
            return False
        msg = Message(
            subject="We received your message — Kimhut Café",
            recipients=[to_email],
            body=(f"Hi {name},\n\n"
                  "Thanks for reaching out to Kimhut Café. We’ve received your message:\n\n"
                  f"\"{original_message}\"\n\n"
                  "We’ll get back to you as soon as possible.\n\n— Kimhut Café"),
        )
        try:
            mail.send(msg)
            return True
        except Exception as e:
            app.logger.exception("[Mail] contact ack failed: %s", e)
            return False

    @app.route("/contact", methods=["GET", "POST"])
    def contact():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip()
            message = request.form.get("message", "").strip()

            from html import escape
            tg_text = (
                "<b>Contact</b>\n"
                f"From: {escape(name)} &lt;{escape(email)}&gt;\n\n"
                f"{escape(message)}"
            )
            tg_ok = send_telegram(tg_text)
            ack_ok = send_contact_ack(email, name, message)

            if tg_ok or ack_ok:
                flash("Thanks! Your message was sent.", "success")
            else:
                flash("Message received, but notifications are not configured.", "success")
            return redirect(url_for("contact"))
        return render_template("contact.html")

    @app.context_processor
    def inject_nav_categories():
        cats = sorted(set(p["category"] for p in PRODUCTS))
        sel = (request.args.get("category") or "All")
        q = request.args.get("q", "")
        return {"categories_nav": cats, "selected_nav_category": sel, "search_q": q}

    @app.route("/products")
    def products():
        selected = request.args.get("category", "All")
        q = (request.args.get("q") or "").strip().lower()
        items = PRODUCTS
        if selected != "All":
            items = [p for p in items if p["category"].lower() == selected.lower()]
        if q:
            items = [p for p in items if q in p["name"].lower()]
        categories = ["All"] + sorted(set(p["category"] for p in PRODUCTS))
        return render_template("products.html", products=items, categories=categories, selected=selected)

    @app.route("/cart")
    def cart():
        items, subtotal = cart_items()
        return render_template("cart.html", items=items, subtotal=subtotal)

    @app.context_processor
    def inject_cart_count():
        cart = session.get("cart", {})
        try:
            count = sum(int(q) for q in cart.values())
        except Exception:
            count = 0
        return {"cart_count": count}

    @app.route("/add-to-cart", methods=["POST"])
    def add_to_cart():
        pid = request.form.get("product_id")
        qty = int(request.form.get("qty", "1"))
        cart = session.get("cart", {})
        cart[pid] = cart.get(pid, 0) + qty
        session["cart"] = cart
        cart_count = sum(int(q) for q in cart.values() if str(q).isdigit())
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.accept_json:
            return jsonify({"ok": True, "cart_count": cart_count})
        flash("Item added to cart.", "success")
        return redirect(url_for("products"))

    @app.route("/update-cart", methods=["POST"])
    def update_cart():
        cart = {}
        for key, val in request.form.items():
            if key.startswith("qty_"):
                pid = key.replace("qty_", "")
                try:
                    q = max(0, int(val))
                    if q > 0:
                        cart[pid] = q
                except Exception:
                    pass
        session["cart"] = cart
        flash("Cart updated.", "success")
        return redirect(url_for("cart"))

    @app.route("/checkout/success")
    def checkout_success():
        return render_template("checkout_success.html")

    @app.route("/checkout", methods=["GET", "POST"])
    def checkout():
        items, subtotal = cart_items()
        if request.method == "POST":
            customer = {
                "name": request.form.get("name", ""),
                "address": request.form.get("address", ""),
                "email": request.form.get("email", ""),
                "phone": request.form.get("phone", ""),
            }

            # KHQR
            amount_str = str(subtotal)
            currency = "USD"
            if not khqr:
                flash("BAKONG_TOKEN មិនកំណត់ទេ (.env)", "danger")
                return redirect(url_for("checkout"))

            qr_payload = khqr.create_qr(
                bank_account='sothun_thoeun@aclb',
                merchant_name='SOTHUN THOEUN',
                merchant_city='Phnom Penh',
                amount=amount_str,
                currency=currency,
                store_label='CHAI-Shop',
                phone_number='855888356210',
                bill_number='INV-00001',
                terminal_label='Cashier-01',
                static=False
            )
            md5 = khqr.generate_md5(qr_payload)

            # Save QR image
            qr_object = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4
            )
            qr_object.add_data(qr_payload)
            qr_object.make(fit=True)
            img = qr_object.make_image(fill_color="black", back_color="white")
            img.save(os.path.join(app.static_folder, "qrcode.png"))

            # Save pending order (server-side; deep copy items!)
            order = {
                "customer": copy.deepcopy(customer),
                "items": copy.deepcopy(items),
                "subtotal": str(amount_str),
                "currency": currency,
                "notified": False,
                "created_at": datetime.utcnow().isoformat(),
            }
            orders_save(md5, order)

            return render_template(
                "payment.html",
                amount=amount_str, currency=currency,
                md5=md5, name="SOTHUN THOEUN", seconds=60
            )

        return render_template("checkout.html", items=items, subtotal=subtotal)

    @app.route('/check-payment', methods=['POST'])
    def check_payment():
        data = request.get_json(silent=True) or {}
        md5 = data.get('md5')
        if not md5:
            return {'error': 'missing md5'}, 400
        if not os.getenv("BAKONG_TOKEN"):
            return {'error': 'BAKONG_TOKEN not set'}, 500

        try:
            res = requests.post(
                'https://api-bakong.nbc.gov.kh/v1/check_transaction_by_md5',
                json={'md5': md5},
                headers={
                    'authorization': f'Bearer {os.getenv("BAKONG_TOKEN")}',
                    'Content-Type': 'application/json'
                },
                timeout=10,
            )
        except Exception as e:
            app.logger.exception("[Bakong] request failed")
            return {'error': 'bakong_request_failed', 'detail': str(e)}, 502

        if res.status_code != 200:
            app.logger.error("[Bakong] HTTP %s %s", res.status_code, res.text[:400])
            return {"error": "bakong_http_error", "code": res.status_code}, 502

        try:
            payload = res.json() or {}
        except Exception:
            app.logger.error("[Bakong] non-JSON response: %s %s", res.status_code, res.text[:400])
            payload = {}

        p_data = payload.get("data") or {}
        status_text = (
            (payload.get("transaction_status")
             or p_data.get("transaction_status")
             or p_data.get("trackingStatus")
             or "")
            .upper()
        )
        success = (status_text == "SUCCESS") or (
            payload.get("responseCode") == 0 and (p_data.get("acknowledgedDateMs") or p_data.get("createdDateMs"))
        )
        app.logger.info("[Bakong] md5=%s success=%s", md5, success)

        if not success:
            return {"success": False, "message": "Waiting for payment..."}

        # Paid -> fetch order (server-side store or session)
        order = orders_get(md5)
        if not order:
            app.logger.warning("[Order] not found for md5=%s (check storage/REDIS_URL)", md5)
            # payment is confirmed anyway
            return {"success": True, "message": "Payment Success (order not found)"}

        # idempotency lock
        if not acquire_notify_lock(md5):
            app.logger.info("[Notify] duplicate suppressed md5=%s", md5)
            return {"success": True, "message": "Payment Success"}

        try:
            if not order.get("notified"):
                try:
                    tg_ok = send_telegram(build_tg_lines(order))
                    app.logger.info("[Telegram] sent=%s", tg_ok)
                except Exception:
                    app.logger.exception("[Telegram] build/send failed")

                try:
                    mail_ok = send_invoice_email(order["customer"], order["items"], order["subtotal"])
                    app.logger.info("[Mail] sent=%s", mail_ok)
                except Exception:
                    app.logger.exception("[Mail] send failed")

                # mark notified
                order["notified"] = True
                orders_save(md5, order)

                # clear cart (best effort)
                session["cart"] = {}
                session.modified = True
        finally:
            release_notify_lock(md5)

        return {"success": True, "message": "Payment Success"}

    return app

app = create_app()

if __name__ == "__main__":
    # Use gunicorn/uwsgi in production; this is for local run.
    app.run()
