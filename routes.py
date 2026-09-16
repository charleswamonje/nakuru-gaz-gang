from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib, secrets, smtplib
from email.message import EmailMessage
from flask import Blueprint, current_app, jsonify, render_template, request, session, abort, redirect, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from . import db, limiter
from .models import User, Product, Service, Order, OrderItem, ServiceRequest

main = Blueprint("main", __name__)


def email_public():
    try:
        start = date.fromisoformat(current_app.config["BUSINESS_START_DATE"])
        return (date.today() - start).days >= int(current_app.config.get("EMAIL_PUBLIC_AFTER_DAYS", 30))
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_ok(password):
    return (isinstance(password, str) and len(password) >= 12 and
            any(c.isupper() for c in password) and any(c.islower() for c in password) and
            any(c.isdigit() for c in password) and any(not c.isalnum() for c in password))


def send_email(to, subject, body):
    host = current_app.config.get("SMTP_HOST")
    if not host:
        current_app.logger.warning("SMTP not configured. Email for %s: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"] = current_app.config["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(host, int(current_app.config.get("SMTP_PORT", 587)), timeout=15) as smtp:
        smtp.starttls()
        smtp.login(current_app.config["SMTP_USERNAME"], current_app.config["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return True


def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


@main.get("/")
def index():
    return render_template("index.html", products=Product.query.filter_by(active=True).all(),
                           services=Service.query.filter_by(active=True).all(), email_public=email_public(),
                           user=current_user())


@main.get("/auth")
def auth_page():
    return render_template("auth.html", user=current_user())


@main.post("/auth/register")
@limiter.limit("5 per minute")
def register():
    data = request.form
    email = str(data.get("email", "")).strip().lower()
    password = data.get("password", "")
    if "@" not in email or len(email) > 320 or not password_ok(password):
        return jsonify(error="Use a valid email and a password of at least 12 characters containing upper/lowercase letters, a number and a symbol."), 400
    if User.query.filter_by(email=email).first():
        return jsonify(error="An account with that email already exists."), 409
    raw = secrets.token_urlsafe(32)
    user = User(email=email, password_hash=generate_password_hash(password),
                verification_token_hash=token_hash(raw),
                verification_expires_at=datetime.now(timezone.utc) + timedelta(hours=24))
    db.session.add(user); db.session.commit()
    link = url_for("main.verify_email", token=raw, _external=True)
    sent = send_email(email, "Verify your Nakuru Gaz Gang account", f"Verify your email within 24 hours:\n\n{link}")
    response = {"message": "Account created. Check your email to verify it."}
    if not sent and current_app.config.get("SHOW_DEV_EMAIL_LINK", False):
        response["development_verification_link"] = link
    return jsonify(response), 201


@main.get("/auth/verify/<token>")
@limiter.limit("20 per minute")
def verify_email(token):
    user = User.query.filter_by(verification_token_hash=token_hash(token)).first()
    if not user or not user.verification_expires_at or user.verification_expires_at < datetime.now(timezone.utc):
        return "Verification link is invalid or expired.", 400
    user.email_verified = True
    user.verification_token_hash = None
    user.verification_expires_at = None
    db.session.commit()
    return redirect(url_for("main.auth_page", verified="1"))


@main.post("/auth/login")
@limiter.limit("5 per minute")
def login():
    email = str(request.form.get("email", "")).strip().lower()
    password = request.form.get("password", "")
    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        abort(401)
    if not user.email_verified:
        return jsonify(error="Verify your email before signing in."), 403
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    return jsonify(message="Logged in securely.", role=user.role)


@main.post("/auth/logout")
def logout():
    session.clear()
    return jsonify(message="Logged out")


@main.post("/auth/request-password-reset")
@limiter.limit("3 per hour")
def request_password_reset():
    email = str(request.form.get("email", "")).strip().lower()
    user = User.query.filter_by(email=email).first()
    # Same response for known/unknown email to reduce account enumeration.
    if user:
        raw = secrets.token_urlsafe(32)
        user.reset_token_hash = token_hash(raw)
        user.reset_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.session.commit()
        link = url_for("main.reset_password_page", token=raw, _external=True)
        send_email(email, "Nakuru Gaz Gang password reset", f"This link expires in 30 minutes:\n\n{link}")
    return jsonify(message="If that email is registered, a password-reset link has been sent.")


@main.get("/auth/reset/<token>")
def reset_password_page(token):
    user = User.query.filter_by(reset_token_hash=token_hash(token)).first()
    if not user or not user.reset_expires_at or user.reset_expires_at < datetime.now(timezone.utc):
        return "Reset link is invalid or expired.", 400
    return render_template("reset.html", token=token)


@main.post("/auth/reset/<token>")
@limiter.limit("5 per minute")
def reset_password(token):
    user = User.query.filter_by(reset_token_hash=token_hash(token)).first()
    password = request.form.get("password", "")
    if not user or not user.reset_expires_at or user.reset_expires_at < datetime.now(timezone.utc):
        return jsonify(error="Reset link is invalid or expired."), 400
    if not password_ok(password):
        return jsonify(error="Password must be at least 12 characters and contain upper/lowercase letters, a number and a symbol."), 400
    user.password_hash = generate_password_hash(password)
    user.reset_token_hash = None
    user.reset_expires_at = None
    db.session.commit()
    return jsonify(message="Password changed. You can now sign in.")


@main.post("/api/orders")
@limiter.limit("10 per minute")
def create_order():
    data = request.get_json(silent=True) or {}
    name, phone, area = str(data.get("customer_name", "")).strip(), str(data.get("phone", "")).strip(), str(data.get("delivery_area", "")).strip()
    items = data.get("items", [])
    if not name or not phone or not area or not isinstance(items, list) or len(items) > 30:
        return jsonify(error="Please provide valid customer details and items."), 400
    total = Decimal("0"); clean = []
    for item in items:
        try: pid, qty = int(item["product_id"]), int(item["quantity"])
        except (KeyError, ValueError, TypeError): return jsonify(error="Invalid item."), 400
        if qty < 1 or qty > 50: return jsonify(error="Quantity must be between 1 and 50."), 400
        product = db.session.get(Product, pid)
        if not product or not product.active: return jsonify(error="Product unavailable."), 400
        total += Decimal(str(product.price)) * qty; clean.append((product, qty))
    if not clean: return jsonify(error="Your order is empty."), 400
    user = current_user()
    order = Order(customer_name=name[:120], phone=phone[:30], delivery_area=area[:160], notes=str(data.get("notes", ""))[:1000], total=total, user_id=user.id if user else None)
    db.session.add(order); db.session.flush()
    for product, qty in clean: db.session.add(OrderItem(order_id=order.id, product_id=product.id, quantity=qty, unit_price=product.price))
    db.session.commit()
    return jsonify(message="Order received.", order_id=order.id, payment_number="0710525480")


@main.post("/api/service-requests")
@limiter.limit("10 per minute")
def create_service_request():
    data = request.get_json(silent=True) or {}
    try: sid = int(data.get("service_id"))
    except (TypeError, ValueError): return jsonify(error="Invalid service."), 400
    service = db.session.get(Service, sid)
    if not service or not service.active: return jsonify(error="Service unavailable."), 400
    if not data.get("customer_name") or not data.get("phone") or not data.get("area"): return jsonify(error="Name, phone and area are required."), 400
    user = current_user()
    r = ServiceRequest(customer_name=str(data["customer_name"])[:120], phone=str(data["phone"])[:30], area=str(data["area"])[:160], service_id=sid, description=str(data.get("description", ""))[:1500], user_id=user.id if user else None)
    db.session.add(r); db.session.commit()
    return jsonify(message="Service request received.", request_id=r.id)


@main.post("/admin/login")
@limiter.limit("5 per minute")
def admin_login():
    data = request.form
    if data.get("username") == current_app.config["ADMIN_USERNAME"] and data.get("password") == current_app.config["ADMIN_PASSWORD"]:
        session["admin"] = True
        return "Logged in"
    abort(401)


@main.post("/admin/logout")
def admin_logout():
    session.pop("admin", None); return "Logged out"


@main.get("/admin")
def admin():
    if not session.get("admin"): abort(403)
    return jsonify({"orders": Order.query.count(), "service_requests": ServiceRequest.query.count(), "products": Product.query.count(), "services": Service.query.count(), "customers": User.query.filter_by(role="customer").count(), "email_public": email_public(), "payment_and_customer_care": "0710525480"})
