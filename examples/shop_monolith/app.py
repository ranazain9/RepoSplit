"""ShopMonolith - a deliberately tangled Flask + SQLAlchemy e-commerce monolith.

Run:  python app.py   (listens on :5000, SQLite, seeded with deterministic data)
"""

import os

from db import db
from flask import Flask, jsonify
from models import Product, User
from routes.auth import auth_bp
from routes.catalog import catalog_bp
from routes.checkout import checkout_bp
from werkzeug.exceptions import HTTPException


def seed_data():
    if User.query.count() > 0:
        return
    db.session.add(User(email="alice@example.com", password_hash="x", full_name="Alice Example", tenant_id="acme"))
    db.session.add(User(email="bob@example.com", password_hash="x", full_name="Bob Example", tenant_id="acme"))
    db.session.add(Product(sku="SKU-001", name="Mechanical Keyboard", price=89.0, stock=25))
    db.session.add(Product(sku="SKU-002", name="4K Monitor", price=349.0, stock=10))
    db.session.add(Product(sku="SKU-003", name="USB-C Hub", price=29.5, stock=100))
    db.session.commit()


def create_app(database_url=None):
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url or os.environ.get("DATABASE_URL", "sqlite:///shop.sqlite")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(checkout_bp)

    @app.errorhandler(HTTPException)
    def _json_error(err):
        return jsonify({"error": err.description}), err.code

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "shop-monolith"})

    with app.app_context():
        db.create_all()
        seed_data()
    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
