"""User / identity domain."""

import hashlib

from db import db
from flask import Blueprint, abort, jsonify, request
from models import User

auth_bp = Blueprint("auth", __name__)


def _hash_password(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_user(user_id):
    """Internal helper used by other domains (checkout) - becomes an internal endpoint after the split."""
    return User.query.get(user_id)


@auth_bp.route("/api/v1/users", methods=["POST"])
def register():
    data = request.get_json()
    if User.query.filter_by(email=data["email"]).first() is not None:
        abort(409, description="email already registered")
    user = User(
        email=data["email"],
        password_hash=_hash_password(data["password"]),
        full_name=data.get("full_name", ""),
        tenant_id=data.get("tenant_id", "default"),
    )
    db.session.add(user)
    db.session.commit()
    return jsonify(user.to_dict()), 201


@auth_bp.route("/api/v1/login", methods=["POST"])
def login():
    data = request.get_json()
    user = User.query.filter_by(email=data["email"]).first()
    if user is None or user.password_hash != _hash_password(data["password"]):
        abort(401, description="invalid credentials")
    return jsonify({"user_id": user.id, "tenant_id": user.tenant_id, "token": _hash_password(user.email)})


@auth_bp.route("/api/v1/users/<int:user_id>", methods=["GET"])
def get_user(user_id):
    user = find_user(user_id)
    if user is None:
        abort(404, description="user not found")
    return jsonify(user.to_dict())
