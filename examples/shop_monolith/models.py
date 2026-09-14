"""ShopMonolith ORM models - the classic 'one models.py for everything' God file.

Four tables, three domains, two cross-domain foreign keys (orders.user_id -> users.id,
order_items.product_id -> products.id) and one more in payments. This is exactly the shape
RepoSplit's DataSplit engine is built to untangle.
"""

from db import db


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    tenant_id = db.Column(db.String(36), nullable=False, default="default")

    def to_dict(self):
        return {"id": self.id, "email": self.email, "full_name": self.full_name, "tenant_id": self.tenant_id}


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    sku = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {"id": self.id, "sku": self.sku, "name": self.name, "price": self.price, "stock": self.stock}


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="pending")
    total = db.Column(db.Float, nullable=False, default=0.0)
    payment_id = db.Column(db.Integer, nullable=True)
    items = db.relationship("OrderItem", backref="order", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "status": self.status,
            "total": self.total,
            "payment_id": self.payment_id,
            "items": [item.to_dict() for item in self.items],
        }


class OrderItem(db.Model):
    __tablename__ = "order_items"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "unit_price": self.unit_price,
        }


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(32), nullable=False, default="captured")
    provider_ref = db.Column(db.String(64), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "user_id": self.user_id,
            "order_id": self.order_id,
            "amount": self.amount,
            "status": self.status,
            "provider_ref": self.provider_ref,
        }
