from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import secrets
from pathlib import Path
import smtplib
from email.message import EmailMessage

from flask import Blueprint, current_app, jsonify, render_template, request, session, abort, redirect, url_for, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from . import db, limiter, csrf
from .models import User, Product, Service, Order, OrderItem, ServiceRequest
from .mpesa import get_mpesa_access_token
from .models import BusinessSetting

main = Blueprint("main", __name__)

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def save_product_image(upload):
    if not upload or not upload.filename:
        return ""
    original = secure_filename(upload.filename)
    if "." not in original:
        return ""
    ext = original.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return ""
    upload_dir = Path(current_app.root_path) / "static" / "uploads" / "products"
    upload_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(12)}.{ext}"
    upload.save(upload_dir / filename)
    return url_for("static", filename=f"uploads/products/{filename}")



def email_public():
    try:
        start = date.fromisoformat(current_app.config["BUSINESS_START_DATE"])
        return (date.today() - start).days >= current_app.config["EMAIL_PUBLIC_AFTER_DAYS"]
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_ok(password):
    return (
        isinstance(password, str) and len(password) >= 12 and
        any(c.isupper() for c in password) and any(c.islower() for c in password) and
        any(c.isdigit() for c in password) and any(not c.isalnum() for c in password)
    )


def send_email(to, subject, body):
    """Use HTTPS email API when configured; fall back to SMTP for local/other hosts.

    Render Free blocks outbound SMTP ports 25/465/587, so production on Render should
    use RESEND_API_KEY + RESEND_FROM (or another HTTPS mail provider).
    """
    resend_key = current_app.config.get("RESEND_API_KEY")
    resend_from = current_app.config.get("RESEND_FROM")
    if resend_key and resend_from:
        try:
            import requests
            r = requests.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                json={"from": resend_from, "to": [to], "subject": subject, "text": body},
                timeout=10,
            )
            r.raise_for_status()
            return True
        except Exception:
            current_app.logger.exception("HTTPS email delivery failed")
            return False

    host = current_app.config.get("SMTP_HOST")
    if not host:
        current_app.logger.warning("No email provider configured for %s", to)
        return False
    try:
        msg = EmailMessage()
        msg["From"] = current_app.config["SMTP_FROM"]
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(host, current_app.config["SMTP_PORT"], timeout=10) as smtp:
            smtp.starttls()
            smtp.login(current_app.config["SMTP_USERNAME"], current_app.config["SMTP_PASSWORD"])
            smtp.send_message(msg)
        return True
    except Exception:
        current_app.logger.exception("SMTP email delivery failed")
        return False


def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None


@main.get("/")
def index():
    products = Product.query.filter_by(active=True).order_by(Product.id).all()
    services = Service.query.filter_by(active=True).order_by(Service.category, Service.id).all()
    till_setting = BusinessSetting.query.filter_by(key="mpesa_till").first()
    mpesa_till = till_setting.value if till_setting else ""
    return render_template(
        "index.html",
        products=products,
        services=services,
        email_public=email_public(),
        user=current_user(),
        mpesa_till=mpesa_till
    )


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
    db.session.add(user)
    db.session.commit()
    link = url_for("main.verify_email", token=raw, _external=True)
    sent = send_email(email, "Verify your Nakuru Gaz Gang account", f"Verify your email within 24 hours:\n\n{link}")
    response = {"message": "Account created. Check your email to verify it."}
    if not sent and current_app.config.get("SHOW_DEV_EMAIL_LINK", False):
        response["development_verification_link"] = link
    return jsonify(response), 201


@main.get("/auth/verify/<token>")
@limiter.limit("20 per minute")
@csrf.exempt
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
        return jsonify(error="Invalid email or password."), 401
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
    if user:
        raw = secrets.token_urlsafe(32)
        user.reset_token_hash = token_hash(raw)
        user.reset_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        db.session.commit()
        link = url_for("main.reset_password_page", token=raw, _external=True)
        send_email(email, "Nakuru Gaz Gang password reset", f"This link expires in 30 minutes:\n\n{link}")
    return jsonify(message="If that email is registered, a password-reset link has been sent.")


@main.get("/auth/reset/<token>")
@csrf.exempt
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


def clean_text(value, max_len):
    return str(value or "").strip()[:max_len]


@main.post("/api/orders")
@limiter.limit("10 per minute")
def create_order():
    data = request.get_json(silent=True) or {}
    name, phone, area = clean_text(data.get("customer_name"), 120), clean_text(data.get("phone"), 30), clean_text(data.get("delivery_area"), 160)
    items = data.get("items", [])
    if not name or not phone or not area or not isinstance(items, list) or len(items) > 30:
        return jsonify(error="Please provide valid customer details and items."), 400

    total = Decimal("0")
    clean = []
    product_ids = []
    for item in items:
        try:
            pid, qty = int(item["product_id"]), int(item["quantity"])
        except (KeyError, ValueError, TypeError):
            return jsonify(error="Invalid item."), 400
        if qty < 1 or qty > 50:
            return jsonify(error="Quantity must be between 1 and 50."), 400
        product_ids.append(pid)

    # One indexed query instead of one database query per cart item.
    products = Product.query.filter(Product.id.in_(set(product_ids)), Product.active.is_(True)).all()
    product_map = {p.id: p for p in products}
    for item in items:
        pid, qty = int(item["product_id"]), int(item["quantity"])
        product = product_map.get(pid)
        if not product:
            return jsonify(error="Product unavailable."), 400
        total += Decimal(str(product.price)) * qty
        clean.append((product, qty))

    if not clean:
        return jsonify(error="Your order is empty."), 400

    user = current_user()
    order = Order(customer_name=name, phone=phone, delivery_area=area,
                  notes=clean_text(data.get("notes"), 1000), total=total,
                  user_id=user.id if user else None)
    db.session.add(order)
    db.session.flush()
    db.session.add_all([OrderItem(order_id=order.id, product_id=p.id, quantity=q, unit_price=p.price) for p, q in clean])
    db.session.commit()
    return jsonify(message="Order received.", order_id=order.id, payment_number="0710525480")


@main.post("/api/orders/<int:order_id>/payment-reference")
@limiter.limit("10 per minute")
def submit_payment_reference(order_id):
    data = request.get_json(silent=True) or {}
    phone = clean_text(data.get("phone"), 30)
    reference = clean_text(data.get("payment_reference"), 120)
    if not phone or not reference:
        return jsonify(error="Phone number and M-PESA transaction reference are required."), 400
    order = db.session.get(Order, order_id)
    if order is None:
        return jsonify(error="Order not found."), 404
    if order.phone != phone:
        return jsonify(error="The phone number does not match this order."), 403
    if order.payment_status == "paid":
        return jsonify(error="This order is already marked as paid."), 400
    order.payment_method = "mpesa"
    order.payment_reference = reference
    db.session.commit()
    return jsonify(message="Payment reference received. Admin will verify the payment.", order_id=order.id)


@main.post("/api/service-requests")
@limiter.limit("10 per minute")
def create_service_request():
    data = request.get_json(silent=True) or {}
    try:
        sid = int(data.get("service_id"))
    except (TypeError, ValueError):
        return jsonify(error="Invalid service."), 400
    service = db.session.get(Service, sid)
    if not service or not service.active:
        return jsonify(error="Service unavailable."), 400
    name, phone, area = clean_text(data.get("customer_name"), 120), clean_text(data.get("phone"), 30), clean_text(data.get("area"), 160)
    if not name or not phone or not area:
        return jsonify(error="Name, phone and area are required."), 400
    user = current_user()
    r = ServiceRequest(customer_name=name, phone=phone, area=area, service_id=sid,
                       description=clean_text(data.get("description"), 1500),
                       user_id=user.id if user else None)
    db.session.add(r)
    db.session.commit()
    return jsonify(message="Service request received.", request_id=r.id)


@main.get("/admin/login")
def admin_login_page():
    return render_template("admin_login.html")


@main.post("/admin/login")
@limiter.limit("5 per minute")
def admin_login():
    data = request.form
    configured_password = current_app.config.get("ADMIN_PASSWORD", "")
    if configured_password and data.get("username") == current_app.config["ADMIN_USERNAME"] and data.get("password") == configured_password:
        session.clear()
        session["admin"] = True
        return "Logged in"
    abort(401)


@main.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return "Logged out"


@main.get("/admin/orders")
def admin_orders():
    if not session.get("admin"):
        abort(403)

    orders = Order.query.order_by(Order.created_at.desc()).all()
    order_rows = []

    for order in orders:
        items = OrderItem.query.filter_by(order_id=order.id).all()
        order_rows.append({
            "order": order,
            "items": items
        })

    return render_template("admin_orders.html", order_rows=order_rows)


@main.post("/admin/orders/<int:order_id>/update")
def admin_update_order(order_id):
    if not session.get("admin"):
        abort(403)

    order = db.session.get(Order, order_id)

    if order is None:
        abort(404)

    order.status = request.form.get("status", order.status)
    order.payment_status = request.form.get("payment_status", order.payment_status)
    order.payment_method = request.form.get("payment_method", order.payment_method)
    order.payment_reference = request.form.get("payment_reference", order.payment_reference)

    db.session.commit()

    return redirect(url_for("main.admin_orders"))

@main.post("/admin/orders/<int:order_id>/mpesa")
def initiate_mpesa_payment(order_id):
    order = db.session.get(Order, order_id)

    if order is None:
        abort(404)

    user = current_user()

    if not session.get("admin"):
        if user is None or order.user_id != user.id:
            abort(403)

    if order.payment_status == "paid":
        return jsonify(error="Order is already marked as paid."), 400

    try:
        token = get_mpesa_access_token()
    except Exception:
        return jsonify(error="M-PESA payment service is not configured."), 503

    return jsonify(
        message="M-PESA connection is ready for payment initiation.",
        order_id=order.id,
        amount=str(order.total),
        access_token_received=bool(token),
    )


@main.post("/admin/payments/settings")
def save_payment_settings():
    if not session.get("admin"):
        abort(403)

    till = clean_text(request.form.get("mpesa_till"), 20)

    if till and not till.isdigit():
        return "Invalid Till number.", 400

    setting = BusinessSetting.query.filter_by(key="mpesa_till").first()

    if setting is None:
        setting = BusinessSetting(key="mpesa_till", value=till)
        db.session.add(setting)
    else:
        setting.value = till

    db.session.commit()

    return redirect(url_for("main.admin_payments"))


@main.get("/admin/payments")
def admin_payments():
    if not session.get("admin"):
        abort(403)

    orders = Order.query.order_by(Order.created_at.desc()).all()

    till_setting = BusinessSetting.query.filter_by(
        key="mpesa_till"
    ).first()

    return render_template(
        "admin_payments.html",
        orders=orders,
        mpesa_till=till_setting.value if till_setting else ""
    )


@main.get("/admin/sales")
def admin_sales():
    if not session.get("admin"):
        abort(403)

    orders = Order.query.order_by(Order.created_at.desc()).all()

    paid_orders = [o for o in orders if o.payment_status == "paid"]
    pending_orders = [o for o in orders if o.payment_status == "pending"]

    total_revenue = sum((o.total for o in paid_orders), 0)

    today = datetime.now(timezone.utc).date()
    today_orders = [
        o for o in orders
        if o.created_at and o.created_at.date() == today
    ]
    today_paid = [
        o for o in today_orders
        if o.payment_status == "paid"
    ]

    today_revenue = sum((o.total for o in today_paid), 0)

    stats = {
        "total_orders": len(orders),
        "paid_orders": len(paid_orders),
        "pending_orders": len(pending_orders),
        "total_revenue": total_revenue,
        "today_orders": len(today_orders),
        "today_revenue": today_revenue,
    }

    return render_template(
        "admin_sales.html",
        stats=stats,
        orders=orders
    )

@main.get("/admin")
def admin():
    if not session.get("admin"):
        abort(403)
    return render_template("admin_dashboard.html", stats={"orders": Order.query.count(), "service_requests": ServiceRequest.query.count(), "products": Product.query.count(), "services": Service.query.count(), "customers": User.query.filter_by(role="customer").count(), "email_public": email_public(), "payment_and_customer_care": "0710525480"})


@main.get("/admin/products")
def admin_products():
    if not session.get("admin"):
        abort(403)
    products = Product.query.order_by(Product.id).all()
    return render_template("admin_products.html", products=products)


@main.post("/admin/products/add")
def admin_product_add():
    if not session.get("admin"):
        abort(403)

    name = clean_text(request.form.get("name"), 120)
    category = clean_text(request.form.get("category"), 40)
    unit = clean_text(request.form.get("unit"), 40)
    description = clean_text(request.form.get("description"), 500)
    image_url = clean_text(request.form.get("image_url"), 500)
    uploaded_image = save_product_image(request.files.get("image"))
    if uploaded_image:
        image_url = uploaded_image

    try:
        price = Decimal(request.form.get("price", "0"))
    except (InvalidOperation, ValueError):
        return "Invalid price.", 400

    if not name or not category or not unit or price < 0:
        return "Please provide valid product details.", 400

    db.session.add(Product(
        name=name,
        category=category,
        price=price,
        unit=unit,
        description=description,
        image_url=image_url,
        active=True
    ))
    db.session.commit()

    return redirect(url_for("main.admin_products"))


@main.post("/admin/products/<int:product_id>/edit")
def admin_product_edit(product_id):
    if not session.get("admin"):
        abort(403)

    product = db.session.get(Product, product_id)
    if not product:
        abort(404)

    name = clean_text(request.form.get("name"), 120)
    category = clean_text(request.form.get("category"), 40)
    unit = clean_text(request.form.get("unit"), 40)
    description = clean_text(request.form.get("description"), 500)
    image_url = clean_text(request.form.get("image_url"), 500)
    uploaded_image = save_product_image(request.files.get("image"))
    if uploaded_image:
        image_url = uploaded_image

    try:
        price = Decimal(request.form.get("price", "0"))
    except (InvalidOperation, ValueError):
        return "Invalid price.", 400

    if not name or not category or not unit or price < 0:
        return "Please provide valid product details.", 400

    product.name = name
    product.category = category
    product.unit = unit
    product.price = price
    product.description = description
    product.image_url = image_url

    db.session.commit()

    return redirect(url_for("main.admin_products"))


@main.post("/admin/products/<int:product_id>/toggle")
def admin_product_toggle(product_id):
    if not session.get("admin"):
        abort(403)

    product = db.session.get(Product, product_id)
    if not product:
        abort(404)

    product.active = not product.active
    db.session.commit()

    return redirect(url_for("main.admin_products"))


@main.get("/admin/services")
def admin_services():
    if not session.get("admin"):
        abort(403)
    services = Service.query.order_by(Service.category, Service.id).all()
    return render_template("admin_services.html", services=services)


@main.post("/admin/services/add")
def admin_service_add():
    if not session.get("admin"):
        abort(403)

    name = clean_text(request.form.get("name"), 160)
    category = clean_text(request.form.get("category"), 80)
    description = clean_text(request.form.get("description"), 500)

    try:
        price = Decimal(request.form.get("price", "0"))
    except (InvalidOperation, ValueError):
        return "Invalid price.", 400

    if not name or not category or price < 0:
        return "Please provide valid service details.", 400

    db.session.add(Service(
        name=name,
        category=category,
        description=description,
        price=price,
        active=True
    ))
    db.session.commit()
    return redirect(url_for("main.admin_services"))


@main.post("/admin/services/<int:service_id>/edit")
def admin_service_edit(service_id):
    if not session.get("admin"):
        abort(403)

    service = db.session.get(Service, service_id)
    if not service:
        abort(404)

    name = clean_text(request.form.get("name"), 160)
    category = clean_text(request.form.get("category"), 80)
    description = clean_text(request.form.get("description"), 500)

    try:
        price = Decimal(request.form.get("price", "0"))
    except (InvalidOperation, ValueError):
        return "Invalid price.", 400

    if not name or not category or price < 0:
        return "Please provide valid service details.", 400

    service.name = name
    service.category = category
    service.description = description
    service.price = price

    db.session.commit()
    return redirect(url_for("main.admin_services"))


@main.post("/admin/services/<int:service_id>/toggle")
def admin_service_toggle(service_id):
    if not session.get("admin"):
        abort(403)

    service = db.session.get(Service, service_id)
    if not service:
        abort(404)

    service.active = not service.active
    db.session.commit()
    return redirect(url_for("main.admin_services"))
