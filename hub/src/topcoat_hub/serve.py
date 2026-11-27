"""Start the hub: run migrations, prepare TLS, serve with uvicorn.

    python -m topcoat_hub.serve [--no-migrate]

Settings come from the ``TOPCOAT_*`` environment (config.py); TLS modes are described in
tls.py.
"""

import argparse
import logging

import uvicorn
from alembic import command
from alembic.config import Config

from topcoat_hub import tls
from topcoat_hub.auth import dev_token_problem
from topcoat_hub.config import REPO_ROOT, get_settings
from topcoat_hub.dbrevision import UnknownRevisionError, check_known

log = logging.getLogger("topcoat_hub.serve")

# Transport limits for every WebSocket (node session, voice). uvicorn buffers a whole message
# before the route sees it, also before sign-in, so these are the bound for an unauthenticated
# client; a route's own checks come after. The largest legitimate message is a node's answer
# to voice.speak (2.8 MB WAV, base64 in one action_result: about 3.7 MB); the node itself reads
# at most 4 MiB (node/internal/agent/session.go). 8 MiB leaves twice that margin; uvicorn's
# default is 16 MiB. The queue (websockets' legacy implementation only; the default sans-I/O
# one pauses reading after each message) holds a few messages instead of 32.
WS_MAX_SIZE = 8 * 1024 * 1024
WS_MAX_QUEUE = 8
WS_LIMITS = {"ws_max_size": WS_MAX_SIZE, "ws_max_queue": WS_MAX_QUEUE}


def alembic_config() -> Config:
    hub_dir = REPO_ROOT / "hub"
    cfg = Config(str(hub_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(hub_dir / "migrations"))
    return cfg


def migrate() -> None:
    command.upgrade(alembic_config(), "head")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m topcoat_hub.serve")
    parser.add_argument("--no-migrate", action="store_true", help="skip `alembic upgrade head`")
    args = parser.parse_args(argv)
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", force=True
    )
    if problem := dev_token_problem(settings.dev_admin_token):
        # until OIDC this token is the whole admin login (auth.py)
        log.error("%s; the hub does not start. Make one with: openssl rand -base64 32", problem)
        raise SystemExit(1)
    try:
        # also with --no-migrate: a build must not run against a database that is ahead of it
        check_known(alembic_config(), settings.database_url)
    except UnknownRevisionError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    if not args.no_migrate:
        migrate()
    # After the migrations: alembic.ini's logging setup would otherwise silence our INFO logs.
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", force=True
    )
    material = tls.prepare(settings)
    scheme = "https" if material.enabled else "http"
    log.info(
        "hub listening on %s://%s:%d (TLS mode %s)",
        scheme,
        settings.host,
        settings.port,
        material.mode,
    )
    if material.ca_fingerprint:
        log.info("hub CA fingerprint %s", material.ca_fingerprint)
    uvicorn.run(
        "topcoat_hub.main:app",
        factory=True,
        host=settings.host,
        port=settings.port,
        ssl_certfile=str(material.cert_file) if material.cert_file else None,
        ssl_keyfile=str(material.key_file) if material.key_file else None,
        # Behind a reverse proxy (mode off) the client address comes from X-Forwarded-For.
        proxy_headers=not material.enabled,
        # Open GUI live streams (SSE) never end on their own; without a limit a stop or
        # restart would hang until the container runtime kills the process.
        timeout_graceful_shutdown=3,
        log_config=None,
        **WS_LIMITS,
    )


if __name__ == "__main__":
    main()
