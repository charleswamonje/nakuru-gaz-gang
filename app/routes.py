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
from .models import (
    User,
    Product,
    Service,
    Order,
    OrderItem,
    ServiceRequest,
    CustomerFeedback,
    StatusHistory,
    Notification,
    AdminAuditLog,
)
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


def website_setting(key, default=""):
    setting = BusinessSetting.query.filter_by(key=key).first()
    return setting.value if setting else default


@main.before_request
def enforce_website_controls():
    endpoint = request.endpoint or ""

    # Always keep administrator access available so the site can be restored.
    if endpoint.startswith("main.admin") or endpoint in {
        "main.admin_login_page",
        "main.admin_login",
        "main.admin_logout",
    }:
        return None

    # Emergency website kill-switch.
    if website_setting("website_enabled", "1") != "1":
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Temporarily Offline | Danstar Gas Delivery</title>"
            "</head><body style='font-family:Arial,sans-serif;text-align:center;"
            "padding:80px 20px'>"
            "<h1>Website Temporarily Offline</h1>"
            "<p>Danstar Gas Delivery is temporarily unavailable for maintenance.</p>"
            "<p>Please try again later or contact customer care.</p>"
            "</body></html>",
            503,
        )

    # Ordering kill-switch.
    if endpoint == "main.create_order" and website_setting("ordering_enabled", "1") != "1":
        return jsonify(
            error="Online ordering is temporarily disabled. Please contact customer care."
        ), 503

    # M-PESA payment-reference kill-switch.
    if endpoint == "main.submit_payment_reference" and website_setting("mpesa_enabled", "1") != "1":
        return jsonify(
            error="M-PESA payments are temporarily disabled. Please contact customer care."
        ), 503


def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None

def create_notification(user_id, title, message, entity_type="", entity_id=None):
    if not user_id:
        return None

    notification = Notification(
        user_id=user_id,
        title=clean_text(title, 160),
        message=clean_text(message, 1000),
        entity_type=clean_text(entity_type, 40),
        entity_id=entity_id
    )

    db.session.add(notification)
    return notification


@main.get("/")
def index():
    products = Product.query.filter_by(active=True).order_by(Product.id).all()
    services = Service.query.filter_by(active=True).order_by(Service.category, Service.id).all()

    till_setting = BusinessSetting.query.filter_by(key="mpesa_till").first()
    mpesa_till = till_setting.value if till_setting else ""

    business_defaults = {
        "business_name": "DANSTAR GAS DELIVERY",
        "customer_care_phone": "0710525480",
        "business_email": "redgroup@gmail.com",
        "whatsapp_number": "254710525480",
        "business_location": "Nakuru, Kenya",
        "business_tagline": "RELIABLE ON TIME CLEAN AND STRONG.",
        "business_mission": "To provide dependable water, gas delivery and authorized technical support to customers in Nakuru.",
        "business_vision": "Reliable on time clean and strong.",
    }

    business = {}

    for key, default in business_defaults.items():
        setting = BusinessSetting.query.filter_by(key=key).first()
        business[key] = setting.value.strip() if setting and setting.value.strip() else default

    whatsapp_number = business["whatsapp_number"]
    if whatsapp_number.startswith("0"):
        whatsapp_number = "254" + whatsapp_number[1:]
    whatsapp_url = "https://wa.me/" + whatsapp_number

    return render_template(
        "index.html",
        products=products,
        services=services,
        email_public=email_public(),
        user=current_user(),
        mpesa_till=mpesa_till,
        business=business,
        whatsapp_url=whatsapp_url
    )


@main.get("/account")
def account_dashboard():
    user = current_user()

    if user is None:
        return redirect(url_for("main.auth_page"))

    order_count = Order.query.filter_by(user_id=user.id).count()
    service_request_count = ServiceRequest.query.filter_by(user_id=user.id).count()
    unread_notification_count = (
        Notification.query
        .filter_by(user_id=user.id, is_read=False)
        .count()
    )

    return render_template(
        "account_dashboard.html",
        user=user,
        order_count=order_count,
        service_request_count=service_request_count,
        unread_notification_count=unread_notification_count
    )


@main.get("/account/orders")
def account_orders():
    user = current_user()

    if user is None:
        return redirect(url_for("main.auth_page"))

    orders = (
        Order.query
        .filter_by(user_id=user.id)
        .order_by(Order.created_at.desc())
        .all()
    )

    order_rows = []

    for order in orders:
        items = OrderItem.query.filter_by(order_id=order.id).all()
        item_rows = []

        for item in items:
            product = db.session.get(Product, item.product_id)

            item_rows.append({
                "item": item,
                "product": product
            })

        order_rows.append({
            "order": order,
            "items": item_rows
        })

    return render_template(
        "account_orders.html",
        user=user,
        order_rows=order_rows
    )


@main.get("/account/service-requests")
def account_service_requests():
    user = current_user()

    if user is None:
        return redirect(url_for("main.auth_page"))

    requests = (
        ServiceRequest.query
        .filter_by(user_id=user.id)
        .order_by(ServiceRequest.created_at.desc())
        .all()
    )

    request_rows = []

    for service_request in requests:
        service = db.session.get(Service, service_request.service_id)

        request_rows.append({
            "request": service_request,
            "service": service
        })

    return render_template(
        "account_service_requests.html",
        user=user,
        request_rows=request_rows
    )


@main.get("/account/notifications")
def account_notifications():
    user = current_user()

    if user is None:
        return redirect(url_for("main.auth_page"))

    notifications = (
        Notification.query
        .filter_by(user_id=user.id)
        .order_by(Notification.created_at.desc())
        .all()
    )

    return render_template(
        "account_notifications.html",
        user=user,
        notifications=notifications
    )


@main.post("/api/notifications/<int:notification_id>/read")
def mark_notification_read(notification_id):
    user = current_user()

    if user is None:
        return jsonify(error="Authentication required."), 401

    notification = db.session.get(Notification,
    CustomerFeedback, notification_id)

    if notification is None or notification.user_id != user.id:
        return jsonify(error="Notification not found."), 404

    notification.is_read = True
    db.session.commit()

    return jsonify(message="Notification marked as read.")


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

    if user is not None and not user.purchasing_enabled:
        return jsonify(
            error="Your purchasing access is currently disabled. Please contact customer care."
        ), 403

    order = Order(customer_name=name, phone=phone, delivery_area=area,
                  notes=clean_text(data.get("notes"), 1000), total=total,
                  user_id=user.id if user else None)
    db.session.add(order)
    db.session.flush()
    db.session.add_all([OrderItem(order_id=order.id, product_id=p.id, quantity=q, unit_price=p.price) for p, q in clean])
    db.session.commit()
    return jsonify(message="Order received.", order_id=order.id, payment_number="0710525480")


@main.get("/api/orders/<int:order_id>/status")
@limiter.limit("20 per minute")
def order_status(order_id):
    phone = clean_text(request.args.get("phone"), 30)
    if not phone:
        return jsonify(error="Phone number is required."), 400
    order = db.session.get(Order, order_id)
    if order is None or order.phone != phone:
        return jsonify(error="Order not found."), 404

    user = current_user()
    if user is not None and order.user_id is not None and order.user_id != user.id:
        return jsonify(error="Order not found."), 404

    history = (
        StatusHistory.query
        .filter_by(entity_type="order", entity_id=order.id)
        .order_by(StatusHistory.created_at.asc(), StatusHistory.id.asc())
        .all()
    )

    return jsonify(
        order_id=order.id,
        status=order.status,
        payment_status=order.payment_status,
        payment_method=order.payment_method,
        created_at=order.created_at.isoformat() if order.created_at else None,
        history=[
            {
                "status": item.status,
                "note": item.note,
                "created_at": (
                    item.created_at.isoformat()
                    if item.created_at else None
                )
            }
            for item in history
        ],
    )


@main.get("/api/service-requests/<int:request_id>/status")
@limiter.limit("20 per minute")
def service_request_status(request_id):
    phone = clean_text(request.args.get("phone"), 30)

    if not phone:
        return jsonify(error="Phone number is required."), 400

    service_request = db.session.get(ServiceRequest, request_id)

    if service_request is None or service_request.phone != phone:
        return jsonify(error="Service request not found."), 404

    user = current_user()
    if user is not None and service_request.user_id is not None and service_request.user_id != user.id:
        return jsonify(error="Service request not found."), 404

    service = db.session.get(Service, service_request.service_id)

    history = (
        StatusHistory.query
        .filter_by(
            entity_type="service_request",
            entity_id=service_request.id
        )
        .order_by(StatusHistory.created_at.asc(), StatusHistory.id.asc())
        .all()
    )

    return jsonify(
        request_id=service_request.id,
        status=service_request.status,
        service_name=service.name if service else "Service unavailable",
        created_at=(
            service_request.created_at.isoformat()
            if service_request.created_at else None
        ),
        history=[
            {
                "status": item.status,
                "note": item.note,
                "created_at": (
                    item.created_at.isoformat()
                    if item.created_at else None
                )
            }
            for item in history
        ]
    )


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

    user = current_user()
    if user is not None:
        if order.user_id != user.id:
            return jsonify(error="You cannot update this order."), 403
    elif order.user_id is not None:
        return jsonify(error="Authentication required for this order."), 401

    if order.phone != phone:
        return jsonify(error="The phone number does not match this order."), 403
    if order.payment_status == "paid":
        return jsonify(error="This order is already marked as paid."), 400
    order.payment_method = "mpesa"
    order.payment_reference = reference
    db.session.commit()
    return jsonify(message="Payment reference received. Admin will verify the payment.", order_id=order.id)


@main.post("/api/feedback")
def submit_feedback():
    data = request.get_json(silent=True) or request.form

    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()

    if not name or not message:
        return jsonify(error="Name and feedback message are required."), 400

    if len(name) > 120 or len(phone) > 30 or len(message) > 1000:
        return jsonify(error="Feedback is too long."), 400

    user = current_user()

    feedback = CustomerFeedback(
        name=name,
        phone=phone,
        message=message,
        user_id=user.id if user else None
    )

    db.session.add(feedback)
    db.session.commit()

    return jsonify(
        ok=True,
        message="Thank you. Your feedback has been received."
    ), 201


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


@main.get("/admin/service-requests")
def admin_service_requests():
    if not session.get("admin"):
        abort(403)

    requests = (
        ServiceRequest.query
        .order_by(ServiceRequest.created_at.desc())
        .all()
    )

    request_rows = []

    for service_request in requests:
        service = db.session.get(Service, service_request.service_id)

        request_rows.append({
            "request": service_request,
            "service": service
        })

    return render_template(
        "admin_service_requests.html",
        request_rows=request_rows
    )


@main.post("/admin/service-requests/<int:request_id>/update")
def admin_service_request_update(request_id):
    if not session.get("admin"):
        abort(403)

    service_request = db.session.get(ServiceRequest, request_id)

    if not service_request:
        abort(404)

    allowed_statuses = {
        "received",
        "contacted",
        "scheduled",
        "in_progress",
        "completed",
        "cancelled"
    }

    old_status = service_request.status
    status = clean_text(request.form.get("status"), 40)

    if status not in allowed_statuses:
        return "Invalid service-request status.", 400

    if status != service_request.status:
        db.session.add(
            StatusHistory(
                entity_type="service_request",
                entity_id=service_request.id,
                status=status,
                note="Service request status updated by admin."
            )
        )

        if service_request.user_id:
            create_notification(
                user_id=service_request.user_id,
                title=f"Service request #{service_request.id} status updated",
                message=(
                    f"Your service request is now "
                    f"{status.replace('_', ' ')}."
                ),
                entity_type="service_request",
                entity_id=service_request.id
            )

    service_request.status = status

    if old_status != status:
        record_admin_audit(
            action=f"Service request status changed: {service_request.id}",
            old_value=old_status,
            new_value=status,
        )

    db.session.commit()

    return redirect(url_for("main.admin_service_requests"))


@main.get("/admin/login")
def admin_login_page():
    return render_template("admin_login.html")


@main.post("/admin/login")
@limiter.limit("5 per minute")
def admin_login():
    data = request.form
    configured_password = current_app.config.get("ADMIN_PASSWORD", "")

    if (
        configured_password
        and data.get("username") == current_app.config["ADMIN_USERNAME"]
        and data.get("password") == configured_password
    ):
        record_admin_audit(
            action="Admin login",
            new_value="Successful"
        )

        session.clear()
        session["admin"] = True
        session["admin_username"] = current_app.config["ADMIN_USERNAME"]
        session.permanent = True

        return "Logged in"

    record_admin_audit(
        action="Admin login",
        new_value="Failed"
    )

    abort(401)


@main.post("/admin/logout")
def admin_logout():
    if session.get("admin"):
        record_admin_audit(
            action="Admin logout",
            new_value="Successful"
        )

    session.clear()
    return "Logged out"


@main.get("/admin/orders")
def admin_orders():
    if not session.get("admin"):
        abort(403)

    orders = Order.query.order_by(Order.created_at.desc()).all()
    pending_orders = [o for o in orders if o.payment_status == "pending" and o.payment_reference]
    order_rows = []

    for order in orders:
        items = OrderItem.query.filter_by(order_id=order.id).all()
        item_rows = []

        for item in items:
            product = db.session.get(Product, item.product_id)
            item_rows.append({
                "item": item,
                "product_name": product.name if product else f"Product #{item.product_id}",
                "line_total": item.quantity * item.unit_price,
            })

        order_rows.append({
            "order": order,
            "items": item_rows
        })

    return render_template("admin_orders.html", order_rows=order_rows)


@main.post("/admin/orders/<int:order_id>/update")
def admin_update_order(order_id):
    if not session.get("admin"):
        abort(403)

    order = db.session.get(Order, order_id)

    if order is None:
        abort(404)

    allowed_statuses = {
        "received",
        "confirmed",
        "preparing",
        "out_for_delivery",
        "delivered",
        "cancelled",
    }
    allowed_payment_statuses = {
        "pending",
        "paid",
        "failed",
        "refunded",
    }
    allowed_payment_methods = {"", "mpesa", "cash", "paypal"}

    new_status = clean_text(request.form.get("status", order.status), 40)
    new_payment_status = clean_text(
        request.form.get("payment_status", order.payment_status), 40
    )
    new_payment_method = clean_text(
        request.form.get("payment_method", order.payment_method), 40
    )
    new_payment_reference = clean_text(
        request.form.get("payment_reference", order.payment_reference), 120
    )

    if new_status not in allowed_statuses:
        abort(400)

    if new_payment_status not in allowed_payment_statuses:
        abort(400)

    if new_payment_method not in allowed_payment_methods:
        abort(400)

    if new_status != order.status:
        db.session.add(
            StatusHistory(
                entity_type="order",
                entity_id=order.id,
                status=new_status,
                note="Order status updated by admin."
            )
        )

        if order.user_id:
            create_notification(
                user_id=order.user_id,
                title=f"Order #{order.id} status updated",
                message=f"Your order is now {new_status.replace('_', ' ')}.",
                entity_type="order",
                entity_id=order.id
            )

    old_order_status = order.status
    old_payment_status = order.payment_status
    old_payment_method = order.payment_method
    old_payment_reference = order.payment_reference

    order.status = new_status
    order.payment_status = new_payment_status
    order.payment_method = new_payment_method
    order.payment_reference = new_payment_reference

    if new_payment_status != old_payment_status:
        db.session.add(
            StatusHistory(
                entity_type="order",
                entity_id=order.id,
                status=f"payment_{new_payment_status}",
                note="Payment status changed by admin."
            )
        )

        if order.user_id:
            create_notification(
                user_id=order.user_id,
                title=f"Order #{order.id} payment updated",
                message=f"Your payment status is now {new_payment_status}.",
                entity_type="order",
                entity_id=order.id
            )

    if old_order_status != new_status:
        record_admin_audit(
            action=f"Order status changed: {order.id}",
            old_value=old_order_status,
            new_value=new_status,
        )

    if old_payment_status != new_payment_status:
        record_admin_audit(
            action=f"Order payment status changed: {order.id}",
            old_value=old_payment_status,
            new_value=new_payment_status,
        )

    if old_payment_method != new_payment_method:
        record_admin_audit(
            action=f"Order payment method changed: {order.id}",
            old_value=old_payment_method,
            new_value=new_payment_method,
        )

    if old_payment_reference != new_payment_reference:
        record_admin_audit(
            action=f"Order payment reference changed: {order.id}",
            old_value=old_payment_reference,
            new_value=new_payment_reference,
        )

    db.session.commit()

    return redirect(url_for("main.admin_orders"))

@main.post("/admin/orders/<int:order_id>/mpesa")
def initiate_mpesa_payment(order_id):
    if not session.get("admin"):
        abort(403)

    order = db.session.get(Order, order_id)

    if order is None:
        abort(404)

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

    old_till = setting.value if setting else ""

    if setting is None:
        setting = BusinessSetting(key="mpesa_till", value=till)
        db.session.add(setting)
    else:
        setting.value = till

    if old_till != till:
        record_admin_audit(
            action="M-PESA Till changed",
            old_value=old_till,
            new_value=till,
        )

    db.session.commit()

    return redirect(url_for("main.admin_payments"))


@main.get("/admin/payments")
def admin_payments():
    if not session.get("admin"):
        abort(403)

    orders = Order.query.order_by(Order.created_at.desc()).all()
    pending_orders = [o for o in orders if o.payment_status == "pending" and o.payment_reference]

    till_setting = BusinessSetting.query.filter_by(
        key="mpesa_till"
    ).first()

    return render_template(
        "admin_payments.html",
        orders=orders,
        pending_orders=pending_orders,
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

@main.get("/robots.txt")
def robots_txt():
    return current_app.send_static_file("robots.txt")


@main.get("/sitemap.xml")
def sitemap_xml():
    return current_app.response_class("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n  <url>\n    <loc>https://danstargasdelivery.co.ke/</loc>\n  </url>\n</urlset>", mimetype="application/xml")


@main.get("/admin")
def admin():
    if not session.get("admin"):
        abort(403)

    orders = Order.query.order_by(Order.created_at.desc()).all()

    stats = {
        "orders": len(orders),
        "service_requests": ServiceRequest.query.count(),
        "products": Product.query.count(),
        "active_products": Product.query.filter_by(active=True).count(),
        "inactive_products": Product.query.filter_by(active=False).count(),
        "services": Service.query.count(),
        "active_services": Service.query.filter_by(active=True).count(),
        "customers": User.query.filter_by(role="customer").count(),
        "feedback": CustomerFeedback.query.count(),
        "pending_feedback": CustomerFeedback.query.filter_by(status="pending").count(),
        "pending_payments": Order.query.filter(
            Order.payment_status == "pending",
            Order.payment_reference != ""
        ).count(),
        "paid_orders": Order.query.filter_by(payment_status="paid").count(),
        "cancelled_orders": Order.query.filter_by(status="cancelled").count(),
        "email_public": email_public(),
        "payment_and_customer_care": "0710525480",
    }

    recent_orders = orders[:10]

    recent_feedback = (
        CustomerFeedback.query
        .order_by(CustomerFeedback.created_at.desc())
        .limit(5)
        .all()
    )

    return render_template(
        "admin_dashboard.html",
        stats=stats,
        recent_orders=recent_orders,
        recent_feedback=recent_feedback
    )


@main.get("/admin/customers")
def admin_customers():
    if not session.get("admin"):
        abort(403)

    customers = (
        User.query
        .filter_by(role="customer")
        .order_by(User.created_at.desc())
        .all()
    )

    customer_rows = []

    for customer in customers:
        order_count = Order.query.filter_by(user_id=customer.id).count()
        service_count = ServiceRequest.query.filter_by(user_id=customer.id).count()
        feedback_count = CustomerFeedback.query.filter_by(user_id=customer.id).count()

        customer_rows.append({
            "customer": customer,
            "order_count": order_count,
            "service_count": service_count,
            "feedback_count": feedback_count,
        })

    return render_template(
        "admin_customers.html",
        customer_rows=customer_rows
    )


@main.get("/admin/customers/<int:user_id>")
def admin_customer_detail(user_id):
    if not session.get("admin"):
        abort(403)

    customer = db.session.get(User, user_id)

    if customer is None or customer.role != "customer":
        abort(404)

    orders = (
        Order.query
        .filter_by(user_id=customer.id)
        .order_by(Order.created_at.desc())
        .all()
    )

    service_requests = (
        ServiceRequest.query
        .filter_by(user_id=customer.id)
        .order_by(ServiceRequest.created_at.desc())
        .all()
    )

    feedback = (
        CustomerFeedback.query
        .filter_by(user_id=customer.id)
        .order_by(CustomerFeedback.created_at.desc())
        .all()
    )

    notifications = (
        Notification.query
        .filter_by(user_id=customer.id)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )

    return render_template(
        "admin_customer_detail.html",
        customer=customer,
        orders=orders,
        service_requests=service_requests,
        feedback=feedback,
        notifications=notifications,
    )


@main.post("/admin/customers/<int:user_id>/verification")
def admin_customer_verification(user_id):
    if not session.get("admin"):
        abort(403)

    customer = db.session.get(User, user_id)

    if customer is None or customer.role != "customer":
        abort(404)

    if customer.email_verified:
        return redirect(url_for("main.admin_customer_detail", user_id=customer.id))

    customer.email_verified = True
    customer.verification_token_hash = None
    customer.verification_expires_at = None

    record_admin_audit(
        action=f"Customer email manually verified: {customer.id}",
        old_value="unverified",
        new_value="verified",
    )

    db.session.commit()

    create_notification(
        user_id=customer.id,
        title="Email verification completed",
        message="Your email address has been verified by the administrator.",
        entity_type="customer",
        entity_id=customer.id
    )

    return redirect(url_for("main.admin_customer_detail", user_id=customer.id))


@main.post("/admin/customers/<int:user_id>/purchasing")
def admin_customer_purchasing(user_id):
    if not session.get("admin"):
        abort(403)

    customer = db.session.get(User, user_id)

    if customer is None or customer.role != "customer":
        abort(404)

    action = clean_text(request.form.get("action"), 20).lower()

    old_value = "enabled" if customer.purchasing_enabled else "disabled"

    if action == "disable":
        customer.purchasing_enabled = False
        message = "Purchasing disabled by administrator."
        new_value = "disabled"

    elif action == "enable":
        customer.purchasing_enabled = True
        message = "Purchasing enabled by administrator."
        new_value = "enabled"

    else:
        return "Invalid customer purchasing action.", 400

    if old_value != new_value:
        record_admin_audit(
            action=f"Customer purchasing access changed: {customer.id}",
            old_value=old_value,
            new_value=new_value,
        )

    db.session.commit()

    create_notification(
        user_id=customer.id,
        title="Purchasing access updated",
        message=message,
        entity_type="customer",
        entity_id=customer.id
    )

    return redirect(url_for("main.admin_customers"))


@main.get("/admin/customer-activity")
def admin_customer_activity():
    if not session.get("admin"):
        abort(403)

    feedback = (
        CustomerFeedback.query
        .order_by(CustomerFeedback.created_at.desc())
        .all()
    )

    return render_template(
        "admin_customer_activity.html",
        feedback=feedback
    )


@main.post("/admin/customer-activity/<int:feedback_id>/status")
def admin_customer_feedback_status(feedback_id):
    if not session.get("admin"):
        abort(403)

    feedback = db.session.get(CustomerFeedback, feedback_id)

    if not feedback:
        abort(404)

    old_status = feedback.status
    status = (request.form.get("status") or "").strip().lower()

    if status not in {"pending", "reviewed", "resolved"}:
        return "Invalid feedback status.", 400

    feedback.status = status

    if old_status != status:
        record_admin_audit(
            action=f"Customer feedback status changed: {feedback.id}",
            old_value=old_status,
            new_value=status,
        )

    db.session.commit()

    return redirect(url_for("main.admin_customer_activity"))


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

    old_product = (
        f"{product.name} | price={product.price} | "
        f"category={product.category} | unit={product.unit}"
    )

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

    new_product = (
        f"{product.name} | price={product.price} | "
        f"category={product.category} | unit={product.unit}"
    )

    if old_product != new_product:
        record_admin_audit(
            action=f"Product edited: {product.id}",
            old_value=old_product,
            new_value=new_product,
        )

    db.session.commit()

    return redirect(url_for("main.admin_products"))


@main.post("/admin/products/<int:product_id>/toggle")
def admin_product_toggle(product_id):
    if not session.get("admin"):
        abort(403)

    product = db.session.get(Product, product_id)
    if not product:
        abort(404)

    old_value = "active" if product.active else "inactive"

    product.active = not product.active

    new_value = "active" if product.active else "inactive"

    record_admin_audit(
        action=f"Product availability changed: {product.id}",
        old_value=old_value,
        new_value=new_value,
    )

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

    service = Service(
        name=name,
        category=category,
        description=description,
        price=price,
        active=True
    )

    db.session.add(service)
    db.session.flush()

    record_admin_audit(
        action=f"Service added: {service.id}",
        new_value=f"{service.name} | price={service.price}"
    )

    db.session.commit()

    return redirect(url_for("main.admin_services"))


@main.post("/admin/services/<int:service_id>/edit")
def admin_service_edit(service_id):
    if not session.get("admin"):
        abort(403)

    service = db.session.get(Service, service_id)
    if not service:
        abort(404)

    old_service = (
        f"{service.name} | price={service.price} | "
        f"category={service.category}"
    )

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

    new_service = (
        f"{service.name} | price={service.price} | "
        f"category={service.category}"
    )

    if old_service != new_service:
        record_admin_audit(
            action=f"Service edited: {service.id}",
            old_value=old_service,
            new_value=new_service,
        )

    db.session.commit()

    return redirect(url_for("main.admin_services"))


@main.post("/admin/services/<int:service_id>/toggle")
def admin_service_toggle(service_id):
    if not session.get("admin"):
        abort(403)

    service = db.session.get(Service, service_id)
    if not service:
        abort(404)

    old_value = "active" if service.active else "inactive"

    service.active = not service.active

    new_value = "active" if service.active else "inactive"

    record_admin_audit(
        action=f"Service availability changed: {service.id}",
        old_value=old_value,
        new_value=new_value,
    )

    db.session.commit()

    return redirect(url_for("main.admin_services"))


@main.get("/admin/business-settings")
def admin_business_settings():
    if not session.get("admin"):
        abort(403)

    keys = [
        "business_name",
        "customer_care_phone",
        "business_email",
        "whatsapp_number",
        "business_location",
        "business_tagline",
        "business_mission",
        "business_vision",
        "website_enabled",
        "ordering_enabled",
        "mpesa_enabled",
    ]

    settings = {}

    for key in keys:
        setting = BusinessSetting.query.filter_by(key=key).first()
        settings[key] = setting.value if setting else ""

    return render_template(
        "admin_business_settings.html",
        settings=settings
    )


def record_admin_audit(action, old_value="", new_value=""):
    db.session.add(
        AdminAuditLog(
            admin_username=clean_text(
                session.get("admin_username")
                or current_app.config.get("ADMIN_USERNAME", "admin"),
                120
            ),
            action=clean_text(action, 120),
            old_value=clean_text(old_value, 500),
            new_value=clean_text(new_value, 500),
        )
    )


@main.get("/admin/audit-log")
def admin_audit_log():
    if not session.get("admin"):
        abort(403)

    audit_rows = (
        AdminAuditLog.query
        .order_by(AdminAuditLog.created_at.desc())
        .limit(200)
        .all()
    )

    return render_template(
        "admin_audit_log.html",
        audit_rows=audit_rows
    )


@main.post("/admin/business-settings")
def save_business_settings():
    if not session.get("admin"):
        abort(403)

    fields = {
        "business_name": 160,
        "customer_care_phone": 30,
        "business_email": 320,
        "whatsapp_number": 30,
        "business_location": 160,
        "business_tagline": 300,
        "business_mission": 500,
        "business_vision": 500,
    }

    for key, limit in fields.items():
        value = clean_text(request.form.get(key), limit)

        setting = BusinessSetting.query.filter_by(key=key).first()

        if setting is None:
            setting = BusinessSetting(
                key=key,
                value=value
            )
            db.session.add(setting)
        else:
            setting.value = value

    control_defaults = {
        "website_enabled": "1",
        "ordering_enabled": "1",
        "mpesa_enabled": "1",
    }

    for key, default in control_defaults.items():
        value = "1" if request.form.get(key) == "1" else "0"
        setting = BusinessSetting.query.filter_by(key=key).first()

        old_value = setting.value if setting else default

        if setting is None:
            setting = BusinessSetting(key=key, value=value)
            db.session.add(setting)
        else:
            setting.value = value

        if old_value != value:
            record_admin_audit(
                action=f"Changed {key}",
                old_value=old_value,
                new_value=value,
            )

    db.session.commit()

    return redirect(url_for("main.admin_business_settings"))
