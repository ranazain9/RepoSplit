"""Product catalog / inventory domain."""

from db import db
from flask import Blueprint, abort, jsonify
from models import Product

catalog_bp = Blueprint("catalog", __name__)


def find_product(product_id):
    return Product.query.get(product_id)


def reserve_stock(product_id, quantity):
    """Decrement stock atomically. Returns the product or None if there is not enough stock."""
    product = Product.query.get(product_id)
    if product is None or product.stock < quantity:
        return None
    product.stock = product.stock - quantity
    db.session.flush()
    return product


def release_stock(product_id, quantity):
    """Compensating action for reserve_stock."""
    product = Product.query.get(product_id)
    if product is None:
        return None
    product.stock = product.stock + quantity
    db.session.flush()
    return product


@catalog_bp.route("/api/v1/products", methods=["GET"])
def list_products():
    products = Product.query.order_by(Product.id).all()
    return jsonify([p.to_dict() for p in products])


@catalog_bp.route("/api/v1/products/<int:product_id>", methods=["GET"])
def get_product(product_id):
    product = find_product(product_id)
    if product is None:
        abort(404, description="product not found")
    return jsonify(product.to_dict())
