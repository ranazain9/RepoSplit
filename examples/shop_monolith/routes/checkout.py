"""Order / checkout domain.

`create_order` is the multi-domain write path (user lookup -> stock reservation -> payment) that
becomes a Saga after the split; `order_history` is the cross-domain SQL JOIN that becomes a CQRS
read projection.
"""

from db import db
from flask import Blueprint, abort, jsonify, request
from models import Order, OrderItem, Product, User
from services.payment import charge_payment, refund_payment
from services.pricing import compute_tax

from routes.auth import find_user
from routes.catalog import release_stock, reserve_stock

checkout_bp = Blueprint("checkout", __name__)


@checkout_bp.route("/api/v1/orders", methods=["POST"])
def create_order():
    data = request.get_json()
    user = find_user(data["user_id"])
    if user is None:
        abort(404, description="user not found")
    items = data.get("items", [])
    if not items:
        abort(400, description="order has no items")

    order = Order(user_id=user.id, status="pending")
    db.session.add(order)
    db.session.flush()

    subtotal = 0.0
    reserved = []
    for item in items:
        product = reserve_stock(item["product_id"], item["quantity"])
        if product is None:
            for product_id, quantity in reserved:
                release_stock(product_id, quantity)
            db.session.rollback()
            abort(409, description="insufficient stock")
        reserved.append((product.id, item["quantity"]))
        subtotal = subtotal + product.price * item["quantity"]
        db.session.add(
            OrderItem(order_id=order.id, product_id=product.id, quantity=item["quantity"], unit_price=product.price)
        )

    tax = compute_tax(subtotal)
    order.total = round(subtotal + tax, 2)
    payment = charge_payment(user.id, order.id, order.total)
    if not payment["ok"]:
        for product_id, quantity in reserved:
            release_stock(product_id, quantity)
        db.session.rollback()
        abort(402, description="payment declined")

    order.status = "paid"
    order.payment_id = payment["payment_id"]
    db.session.commit()
    return jsonify(order.to_dict()), 201


@checkout_bp.route("/api/v1/orders/<int:order_id>", methods=["GET"])
def get_order(order_id):
    order = Order.query.get(order_id)
    if order is None:
        abort(404, description="order not found")
    return jsonify(order.to_dict())


@checkout_bp.route("/api/v1/orders/<int:order_id>/cancel", methods=["POST"])
def cancel_order(order_id):
    order = Order.query.get(order_id)
    if order is None:
        abort(404, description="order not found")
    if order.status == "cancelled":
        return jsonify(order.to_dict())
    for item in order.items:
        release_stock(item.product_id, item.quantity)
    if order.payment_id is not None:
        refund_payment(order.payment_id)
    order.status = "cancelled"
    db.session.commit()
    return jsonify(order.to_dict())


@checkout_bp.route("/api/v1/users/<int:user_id>/orders", methods=["GET"])
def order_history(user_id):
    """Cross-domain JOIN: users x orders x order_items x products in one screen."""
    user = User.query.get(user_id)
    if user is None:
        abort(404, description="user not found")
    rows = (
        db.session.query(Order, OrderItem, Product)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Product, Product.id == OrderItem.product_id)
        .filter(Order.user_id == user_id)
        .order_by(Order.id, OrderItem.id)
        .all()
    )
    history = {}
    for order, item, product in rows:
        entry = history.setdefault(
            order.id, {"order_id": order.id, "status": order.status, "total": order.total, "lines": []}
        )
        entry["lines"].append(
            {"product": product.name, "sku": product.sku, "quantity": item.quantity, "unit_price": item.unit_price}
        )
    return jsonify({"user": user.to_dict(), "orders": list(history.values())})
