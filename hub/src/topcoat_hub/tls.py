"""TLS for the hub (ARCHITECTURE D11).

Modes (``TOPCOAT_TLS_MODE``):

- ``auto``: the hub keeps a small CA of its own in ``TOPCOAT_TLS_DIR`` and issues its server
  certificate from it. The CA is created once and kept; the server certificate is re-issued at
  start when the configured host names changed or it is about to expire. Nodes pin the CA by
  its fingerprint (shown in the pairing command), browsers can install ``/api/tls/ca.pem``.
- ``files``: certificate and key are provided by the admin (e.g. from an internal CA).
- ``off``: plain HTTP, only behind a TLS-terminating reverse proxy or in tests.
"""

import datetime as dt
import hashlib
import ipaddress
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from topcoat_hub.config import Settings

log = logging.getLogger(__name__)

CA_CERT = "ca.pem"
CA_KEY = "ca-key.pem"
SERVER_CERT = "server.pem"  # leaf followed by the CA, so clients can pin the CA from the chain
SERVER_KEY = "server-key.pem"

CA_VALIDITY = dt.timedelta(days=3650)
# Apple platforms reject TLS server certificates valid for more than 825 days, also from
# private CAs; stay below that.
SERVER_VALIDITY = dt.timedelta(days=800)
RENEW_BEFORE = dt.timedelta(days=60)
ALWAYS_NAMES = ("localhost", "127.0.0.1", "::1")


class TLSConfigError(Exception):
    """The TLS settings are inconsistent or the certificate files are unusable."""


@dataclass(frozen=True)
class TLSMaterial:
    mode: str
    cert_file: Path | None = None
    key_file: Path | None = None
    # SHA-256 of the CA certificate (DER) as "sha256:<hex>", when the hub knows its CA.
    ca_fingerprint: str | None = None
    ca_pem: bytes | None = None

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


def fingerprint(cert: x509.Certificate) -> str:
    return "sha256:" + hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def parse_names(raw: str) -> list[str]:
    """Split ``TOPCOAT_TLS_HOSTNAMES`` and add the loopback names; order kept, no duplicates."""
    names: list[str] = []
    for part in [*raw.split(","), *ALWAYS_NAMES]:
        name = part.strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def _general_name(name: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _write_private(path: Path, data: bytes) -> None:
    """Write a key file readable by the owner only (atomic replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    tmp.replace(path)


def _write_public(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _cert_pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TLSConfigError(f"{path} is not an EC private key")
    return key


def _create_ca(tls_dir: Path) -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    # A random suffix keeps CAs of different installations apart in a browser's trust store.
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "sloppy-topcoat"),
            x509.NameAttribute(
                NameOID.COMMON_NAME, f"sloppy-topcoat hub CA {secrets.token_hex(4)}"
            ),
        ]
    )
    now = _now()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private(tls_dir / CA_KEY, _key_pem(key))
    _write_public(tls_dir / CA_CERT, _cert_pem(cert))
    log.info("created hub CA %s (%s)", cert.subject.rfc4514_string(), fingerprint(cert))
    return cert, key


def _issue_server(
    tls_dir: Path, ca: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey, names: list[str]
) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    now = _now()
    ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(min(now + SERVER_VALIDITY, ca.not_valid_after_utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([_general_name(n) for n in names]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    _write_private(tls_dir / SERVER_KEY, _key_pem(key))
    _write_public(tls_dir / SERVER_CERT, _cert_pem(cert) + _cert_pem(ca))
    log.info("issued hub server certificate for %s", ", ".join(names))


def _san_names(cert: x509.Certificate) -> set[str]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return set()
    names = {str(n) for n in san.get_values_for_type(x509.DNSName)}
    names |= {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
    return names


def _server_needs_reissue(tls_dir: Path, ca: x509.Certificate, names: list[str]) -> str | None:
    """Return why the server certificate must be (re-)issued, or None if it is fine."""
    cert_path, key_path = tls_dir / SERVER_CERT, tls_dir / SERVER_KEY
    if not cert_path.exists() or not key_path.exists():
        return "missing"
    try:
        chain = x509.load_pem_x509_certificates(cert_path.read_bytes())
    except ValueError:
        return "unreadable"
    leaf = chain[0]
    if leaf.issuer != ca.subject:
        return "issued by another CA"
    if _san_names(leaf) != {str(_general_name(n).value) for n in names}:
        return "host names changed"
    if leaf.not_valid_after_utc - _now() < RENEW_BEFORE:
        return "expires soon"
    return None


def ensure_auto(tls_dir: Path, hostnames: str) -> TLSMaterial:
    """Create or refresh the hub CA and server certificate in ``tls_dir``."""
    tls_dir.mkdir(parents=True, exist_ok=True)
    names = parse_names(hostnames)
    if (tls_dir / CA_CERT).exists() and (tls_dir / CA_KEY).exists():
        ca = x509.load_pem_x509_certificate((tls_dir / CA_CERT).read_bytes())
        ca_key = _load_key(tls_dir / CA_KEY)
    else:
        ca, ca_key = _create_ca(tls_dir)
    reason = _server_needs_reissue(tls_dir, ca, names)
    if reason:
        log.info("server certificate: %s", reason)
        _issue_server(tls_dir, ca, ca_key, names)
    return TLSMaterial(
        mode="auto",
        cert_file=tls_dir / SERVER_CERT,
        key_file=tls_dir / SERVER_KEY,
        ca_fingerprint=fingerprint(ca),
        ca_pem=_cert_pem(ca),
    )


def load_files(cert_file: Path, key_file: Path, ca_file: Path | None) -> TLSMaterial:
    for path in (cert_file, key_file, *([ca_file] if ca_file else [])):
        if not path.is_file():
            raise TLSConfigError(f"TLS file {path} does not exist")
    try:
        chain = x509.load_pem_x509_certificates(cert_file.read_bytes())
    except ValueError as exc:
        raise TLSConfigError(f"{cert_file} is not a PEM certificate: {exc}") from exc
    if chain[0].not_valid_after_utc < _now():
        raise TLSConfigError(f"{cert_file} expired on {chain[0].not_valid_after_utc:%Y-%m-%d}")
    ca_fp = ca_pem = None
    if ca_file:
        ca = x509.load_pem_x509_certificate(ca_file.read_bytes())
        ca_fp, ca_pem = fingerprint(ca), _cert_pem(ca)
    return TLSMaterial(
        mode="files", cert_file=cert_file, key_file=key_file, ca_fingerprint=ca_fp, ca_pem=ca_pem
    )


def prepare(settings: Settings) -> TLSMaterial:
    """Resolve the TLS settings into usable material (idempotent)."""
    mode = settings.tls_mode
    if mode == "off":
        return TLSMaterial(mode="off")
    if mode == "auto":
        if not settings.tls_hostnames.strip():
            log.warning(
                "TOPCOAT_TLS_HOSTNAMES is empty: the certificate is only valid for localhost; "
                "nodes on other machines will refuse it"
            )
        return ensure_auto(settings.tls_dir, settings.tls_hostnames)
    if settings.tls_cert_file is None or settings.tls_key_file is None:
        raise TLSConfigError(
            "TOPCOAT_TLS_MODE=files needs TOPCOAT_TLS_CERT_FILE and TOPCOAT_TLS_KEY_FILE"
        )
    return load_files(settings.tls_cert_file, settings.tls_key_file, settings.tls_ca_file)
