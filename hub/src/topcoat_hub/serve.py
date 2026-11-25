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
from topcoat_hub.config import REPO_ROOT, get_settings
from topcoat_hub.dbrevision import UnknownRevisionError, check_known

log = logging.getLogger("topcoat_hub.serve")


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
    )


if __name__ == "__main__":
    main()
