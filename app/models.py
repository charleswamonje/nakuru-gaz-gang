from datetime import datetime, timezone
from . import db


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    email_verified = db.Column(db.Boolean, default=False, nullable=False, index=True)
    role = db.Column(db.String(30), default="customer", nullable=False, index=True)
    verification_token_hash = db.Column(db.String(64), index=True)
    verification_expires_at = db.Column(db.DateTime(timezone=True))
    reset_token_hash = db.Column(db.String(64), index=True)
    reset_expires_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    category = db.Column(db.String(40), nullable=False, index=True)
    price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    unit = db.Column(db.String(40), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    description = db.Column(db.String(500), default="")


class Service(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), nullable=False, index=True)
    description = db.Column(db.String(500), default="")
    price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    active = db.Column(db.Boolean, default=True, nullable=False, index=True)


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=False, index=True)
    delivery_area = db.Column(db.String(160), nullable=False, index=True)
    notes = db.Column(db.String(1000), default="")
    total = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    status = db.Column(db.String(40), default="received", nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("order.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False, index=True)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)


class ServiceRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), nullable=False, index=True)
    area = db.Column(db.String(160), nullable=False, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("service.id"), nullable=False, index=True)
    description = db.Column(db.String(1500), default="")
    status = db.Column(db.String(40), default="received", nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)


def seed_data():
    if Product.query.count() == 0:
        db.session.add_all([
            Product(name="20L Drinking Water", category="Water", price=100, unit="container", description="Clean drinking water delivery."),
            Product(name="Gas Cylinder Refill", category="Gas", price=0, unit="refill", description="Price confirmed before service."),
        ])
    if Service.query.count() == 0:
        names = [
            ("Network troubleshooting", "Technology & Network Services"),
            ("Wi-Fi/router setup", "Technology & Network Services"),
            ("LAN installation and configuration", "Technology & Network Services"),
            ("Computer troubleshooting", "Technology & Network Services"),
            ("Windows/software installation", "Technology & Network Services"),
            ("Printer setup", "Technology & Network Services"),
            ("Network security assessments", "Technology & Network Services"),
            ("Basic cybersecurity assistance", "Technology & Network Services"),
            ("Data backup and recovery assistance", "Technology & Network Services"),
            ("CCTV/network-device setup", "Technology & Network Services"),
            ("Small-business IT support", "Technology & Network Services"),
            ("Drinking-water delivery", "Delivery Services"),
            ("Gas cylinder delivery", "Delivery Services"),
            ("Gas refills", "Delivery Services"),
            ("Scheduled/repeat deliveries", "Delivery Services"),
        ]
        db.session.add_all([Service(name=n, category=c, description="Request this service and we will contact you.", price=0) for n, c in names])
    db.session.commit()
