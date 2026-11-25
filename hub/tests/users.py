"""Second users for isolation tests (D20). Until OIDC (stage 5) users other than the seeded dev
admin exist only in the database and authenticate with API keys."""

import secrets
import uuid

from sqlalchemy import create_engine, text

from topcoat_hub.auth import API_KEY_PREFIX, hash_secret


def make_user(db_url: str, name: str, role: str = "user") -> tuple[uuid.UUID, dict[str, str]]:
    """Insert a user and an API key; returns the user id and the Authorization header."""
    user_id = uuid.uuid4()
    token = API_KEY_PREFIX + secrets.token_urlsafe(30)
    engine = create_engine(db_url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, name, role) VALUES (:id, :name, :role)"),
            {"id": str(user_id), "name": name, "role": role},
        )
        conn.execute(
            text(
                "INSERT INTO api_keys (id, user_id, name, prefix, key_hash) "
                "VALUES (gen_random_uuid(), :uid, 'test', :prefix, :hash)"
            ),
            {"uid": str(user_id), "prefix": token[:10], "hash": hash_secret(token)},
        )
    engine.dispose()
    return user_id, {"Authorization": f"Bearer {token}"}
