from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib, secrets, smtplib, base64, json, re
import requests
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



def normalize_mpesa_phone(phone):
    digits = re.sub(r"\D", "", str(phone or ""))
    if digits.startswith("254") and len(digits) == 12:
        return digits
    if digits.startswith("07") and len(digits) == 10:
        return "254" + digits[1:]
    if digits.startswith("01") and len(digits) == 10:
        return "254" + digits[1:]
    if digits.startswith("7") and len(digits) == 9:
        return "254" + digits
    if digits.startswith("1") and len(digits) == 9:
        return "254" + digits
    return None


def mpesa_access_token():
    env = current_app.config.get("MPESA_ENV", "sandbox").lower()
    base = (
        "https://api.safaricom.co.ke"
        if env == "production"
        else "https://sandbox.safaricom.co.ke"
    )

    key = current_app.config.get("MPESA_CONSUMER_KEY")
    secret = current_app.config.get("MPESA_CONSUMER_SECRET")

    if not key or not secret:
        raise RuntimeError("M-Pesa credentials are not configured.")

    response = requests.get(
        base + "/oauth/v1/generate",
        params={"grant_type": "client_credentials"},
        auth=(key, secret),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def mpesa_password(timestamp):
    shortcode = current_app.config.get("MPESA_SHORTCODE")
    passkey = current_app.config.get("MPESA_PASSKEY")

    if not shortcode or not passkey:
        raise RuntimeError("M-Pesa shortcode/passkey are not configured.")

    raw = f"{shortcode}{passkey}{timestamp}".encode()
    return base64.b64encode(raw).decode()


def initiate_mpesa_stk(order):
    phone = normalize_mpesa_phone(order.phone)
    if not phone:
        raise ValueError("Enter a valid Kenyan M-Pesa phone number.")

    amount = int(Decimal(str(order.total)))
    if amount < 1:
        raise ValueError("This order has no payable amount yet.")

    shortcode = current_app.config.get("MPESA_SHORTCODE")
    callback_url = current_app.config.get("MPESA_CALLBACK_URL")

    if not shortcode:
        raise RuntimeError("M-Pesa shortcode is not configured.")
    if not callback_url:
        raise RuntimeError("MPESA_CALLBACK_URL is not configured.")

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d%H%M%S")

    env = current_app.config.get("MPESA_ENV", "sandbox").lower()
    base = (
        "https://api.safaricom.co.ke"
        if env == "production"
        else "https://sandbox.safaricom.co.ke"
    )

    payload = {
        "BusinessShortCode": shortcode,
        "Password": mpesa_password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": f"ORDER-{order.id}",
        "TransactionDesc": f"Danstar Gas Order {order.id}",
    }

    token = mpesa_access_token()

    response = requests.post(
        base + "/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )

    try:
        data = response.json()
    except ValueError:
        data = {}

    if not response.ok:
        raise RuntimeError(
            data.get("errorMessage")
            or data.get("errorCode")
            or "M-Pesa request failed."
        )

    return data


@main.post("/api/orders")
@limiter.limit("10 per minute")
def create_order():
    data = request.get_json(silent=True) or {}

    name = str(data.get("customer_name", "")).strip()
    phone = str(data.get("phone", "")).strip()
    area = str(data.get("delivery_area", "")).strip()
    items = data.get("items", [])

    if not name or not phone or not area or not isinstance(items, list) or len(items) > 30:
        return jsonify(error="Please provide valid customer details and items."), 400

    total = Decimal("0")
    clean = []

    for item in items:
        try:
            pid = int(item["product_id"])
            qty = int(item["quantity"])
        except (KeyError, ValueError, TypeError):
            return jsonify(error="Invalid item."), 400

        if qty < 1 or qty > 50:
            return jsonify(error="Quantity must be between 1 and 50."), 400

        product = db.session.get(Product, pid)

        if not product or not product.active:
            return jsonify(error="Product unavailable."), 400

        total += Decimal(str(product.price)) * qty
        clean.append((product, qty))

    if not clean:
        return jsonify(error="Your order is empty."), 400

    user = current_user()

    order = Order(
        customer_name=name[:120],
        phone=phone[:30],
        delivery_area=area[:160],
        notes=str(data.get("notes", ""))[:1000],
        total=total,
        payment_phone=normalize_mpesa_phone(phone),
        user_id=user.id if user else None,
    )

    db.session.add(order)
    db.session.flush()

    for product, qty in clean:
        db.session.add(
            OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=qty,
                unit_price=product.price,
            )
        )

    db.session.commit()

    # Automatically start M-Pesa for orders with a payable total.
    if total > 0:
        try:
            mpesa = initiate_mpesa_stk(order)

            order.payment_status = "prompt_sent"
            order.mpesa_checkout_request_id = mpesa.get("CheckoutRequestID")
            order.mpesa_merchant_request_id = mpesa.get("MerchantRequestID")
            order.mpesa_result_code = str(mpesa.get("ResponseCode", ""))
            order.mpesa_result_desc = mpesa.get("ResponseDescription", "")

            db.session.commit()

            return jsonify(
                message="Order received. Check your phone for the M-Pesa payment prompt.",
                order_id=order.id,
                payment_status=order.payment_status,
                checkout_request_id=order.mpesa_checkout_request_id,
            ), 201

        except ValueError as exc:
            order.payment_status = "payment_error"
            order.mpesa_result_desc = str(exc)
            db.session.commit()
            return jsonify(
                message="Order received, but payment could not be started.",
                order_id=order.id,
                payment_status=order.payment_status,
                error=str(exc),
            ), 400

        except Exception as exc:
            current_app.logger.exception("M-Pesa STK Push failed for order %s", order.id)
            order.payment_status = "payment_error"
            order.mpesa_result_desc = str(exc)[:500]
            db.session.commit()
            return jsonify(
                message="Order received, but M-Pesa could not be started. Please try again.",
                order_id=order.id,
                payment_status=order.payment_status,
            ), 502

    return jsonify(
        message="Order received. Price will be confirmed before service.",
        order_id=order.id,
        payment_status="pending",
    ), 201


@main.post("/api/mpesa/callback")
def mpesa_callback():
    data = request.get_json(silent=True) or {}
    stk = data.get("Body", {}).get("stkCallback", {})

    checkout_id = stk.get("CheckoutRequestID")
    result_code = stk.get("ResultCode")
    result_desc = stk.get("ResultDesc", "")

    if not checkout_id:
        return jsonify(ResultCode=0, ResultDesc="Accepted")

    order = Order.query.filter_by(
        mpesa_checkout_request_id=checkout_id
    ).first()

    if not order:
        current_app.logger.warning(
            "M-Pesa callback for unknown checkout request: %s",
            checkout_id,
        )
        return jsonify(ResultCode=0, ResultDesc="Accepted")

    order.mpesa_result_code = str(result_code)
    order.mpesa_result_desc = str(result_desc)[:500]

    # ResultCode 0 means the customer payment completed successfully.
    if str(result_code) == "0":
        metadata = stk.get("CallbackMetadata", {}).get("Item", [])

        values = {
            item.get("Name"): item.get("Value")
            for item in metadata
            if item.get("Name")
        }

        receipt = values.get("MpesaReceiptNumber")

        order.payment_status = "paid"
        order.mpesa_receipt = str(receipt) if receipt else None
        order.paid_at = datetime.now(timezone.utc)

    else:
        order.payment_status = "failed"

    db.session.commit()

    return jsonify(ResultCode=0, ResultDesc="Accepted")


@main.get("/api/orders/<int:order_id>/status")
def order_status(order_id):
    order = db.session.get(Order, order_id)

    if not order:
        return jsonify(error="Order not found."), 404

    phone = request.args.get("phone", "").strip()

    if not phone or normalize_mpesa_phone(phone) != normalize_mpesa_phone(order.phone):
        return jsonify(error="Order not found."), 404

    history = [
        {
            "status": order.status or "received",
            "created_at": (
                order.created_at.isoformat()
                if order.created_at else None
            ),
            "note": "Order received."
        }
    ]

    return jsonify(
        order_id=order.id,
        status=order.status or "received",
        payment_status=order.payment_status or "pending",
        receipt=order.mpesa_receipt,
        history=history
    )


@main.get("/api/orders/<int:order_id>/payment-status")
def payment_status(order_id):
    order = db.session.get(Order, order_id)

    if not order:
        return jsonify(error="Order not found."), 404

    phone = request.args.get("phone", "").strip()

    if phone and normalize_mpesa_phone(phone) != normalize_mpesa_phone(order.phone):
        return jsonify(error="Order not found."), 404

    return jsonify(
        order_id=order.id,
        payment_status=order.payment_status,
        receipt=order.mpesa_receipt,
        result_description=order.mpesa_result_desc,
    )


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


@main.post("/admin/orders/<int:order_id>/status")
def admin_update_order_status(order_id):
    if not session.get("admin"):
        abort(403)

    data = request.get_json(silent=True) or {}
    new_status = str(data.get("status", "")).strip()

    allowed = {
        "received",
        "confirmed",
        "processing",
        "out_for_delivery",
        "delivered",
        "cancelled",
    }

    if new_status not in allowed:
        return jsonify(error="Invalid order status."), 400

    order = db.session.get(Order, order_id)
    if not order:
        return jsonify(error="Order not found."), 404

    order.status = new_status
    db.session.commit()

    return jsonify(
        message="Order status updated.",
        order_id=order.id,
        status=order.status,
    )


@main.get("/admin")
def admin():
    if not session.get("admin"): abort(403)
    return jsonify({"orders": Order.query.count(), "service_requests": ServiceRequest.query.count(), "products": Product.query.count(), "services": Service.query.count(), "customers": User.query.filter_by(role="customer").count(), "email_public": email_public(), "payment_and_customer_care": "0710525480"})
