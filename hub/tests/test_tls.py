"""Hub TLS material (D11): hub-managed CA, re-issue rules, admin-provided files, endpoints."""

import datetime as dt
import ipaddress
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.x509.verification import PolicyBuilder, Store
from fastapi.testclient import TestClient

from tests.conftest import AUTH
from topcoat_hub import tls
from topcoat_hub.config import Settings


def _chain(tls_dir: Path) -> list[x509.Certificate]:
    return x509.load_pem_x509_certificates((tls_dir / tls.SERVER_CERT).read_bytes())


def _verify(tls_dir: Path, name: str) -> None:
    """Verify the server chain against the hub CA like a client would (incl. host name)."""
    ca = x509.load_pem_x509_certificate((tls_dir / tls.CA_CERT).read_bytes())
    leaf, *rest = _chain(tls_dir)
    builder = PolicyBuilder().store(Store([ca]))
    try:
        subject: x509.verification.Subject = x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        subject = x509.DNSName(name)
    builder.build_server_verifier(subject).verify(leaf, rest)


def test_auto_creates_ca_and_server_cert(tmp_path: Path) -> None:
    material = tls.ensure_auto(tmp_path, "hub.example.lan, 10.0.0.5")
    for name in (tls.CA_CERT, tls.CA_KEY, tls.SERVER_CERT, tls.SERVER_KEY):
        assert (tmp_path / name).is_file()
    assert (tmp_path / tls.CA_KEY).stat().st_mode & 0o777 == 0o600
    assert (tmp_path / tls.SERVER_KEY).stat().st_mode & 0o777 == 0o600
    ca = x509.load_pem_x509_certificate((tmp_path / tls.CA_CERT).read_bytes())
    assert material.ca_fingerprint == tls.fingerprint(ca)
    assert material.ca_fingerprint.startswith("sha256:")
    assert len(material.ca_fingerprint) == len("sha256:") + 64
    # The chain file carries the CA, so a node can find the pinned certificate in it.
    assert [c.subject for c in _chain(tmp_path)][1] == ca.subject
    for name in ("hub.example.lan", "10.0.0.5", "localhost", "127.0.0.1"):
        _verify(tmp_path, name)
    with pytest.raises(x509.verification.VerificationError):
        _verify(tmp_path, "other.example.lan")


def test_auto_is_idempotent(tmp_path: Path) -> None:
    first = tls.ensure_auto(tmp_path, "hub.example.lan")
    leaf = (tmp_path / tls.SERVER_CERT).read_bytes()
    second = tls.ensure_auto(tmp_path, "hub.example.lan")
    assert second.ca_fingerprint == first.ca_fingerprint
    assert (tmp_path / tls.SERVER_CERT).read_bytes() == leaf


def test_changed_names_reissue_server_cert_but_keep_ca(tmp_path: Path) -> None:
    first = tls.ensure_auto(tmp_path, "hub.example.lan")
    leaf = (tmp_path / tls.SERVER_CERT).read_bytes()
    second = tls.ensure_auto(tmp_path, "hub.example.lan,10.0.0.7")
    assert second.ca_fingerprint == first.ca_fingerprint
    assert (tmp_path / tls.SERVER_CERT).read_bytes() != leaf
    _verify(tmp_path, "10.0.0.7")


def test_expiring_server_cert_is_reissued(tmp_path: Path, monkeypatch) -> None:
    tls.ensure_auto(tmp_path, "hub.example.lan")
    leaf = (tmp_path / tls.SERVER_CERT).read_bytes()
    later = dt.datetime.now(dt.UTC) + tls.SERVER_VALIDITY - tls.RENEW_BEFORE + dt.timedelta(days=1)
    monkeypatch.setattr(tls, "_now", lambda: later)
    tls.ensure_auto(tmp_path, "hub.example.lan")
    assert (tmp_path / tls.SERVER_CERT).read_bytes() != leaf


def test_parse_names() -> None:
    assert tls.parse_names(" Hub.Example.lan ,10.0.0.5,,localhost") == [
        "hub.example.lan",
        "10.0.0.5",
        "localhost",
        "127.0.0.1",
        "::1",
    ]


def test_files_mode(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tls.ensure_auto(src, "hub.example.lan")
    material = tls.load_files(src / tls.SERVER_CERT, src / tls.SERVER_KEY, src / tls.CA_CERT)
    assert material.mode == "files"
    ca = x509.load_pem_x509_certificate((src / tls.CA_CERT).read_bytes())
    assert material.ca_fingerprint == tls.fingerprint(ca)
    without_ca = tls.load_files(src / tls.SERVER_CERT, src / tls.SERVER_KEY, None)
    assert without_ca.ca_fingerprint is None
    with pytest.raises(tls.TLSConfigError, match="does not exist"):
        tls.load_files(src / "missing.pem", src / tls.SERVER_KEY, None)


def test_files_mode_needs_both_files(tmp_path: Path) -> None:
    settings = Settings(tls_mode="files", tls_cert_file=tmp_path / "c.pem")
    with pytest.raises(tls.TLSConfigError, match="TOPCOAT_TLS_KEY_FILE"):
        tls.prepare(settings)


def test_off_mode() -> None:
    material = tls.prepare(Settings(tls_mode="off"))
    assert not material.enabled
    assert material.ca_fingerprint is None


def test_tls_endpoints(client: TestClient, settings: Settings) -> None:
    info = client.get("/api/tls").json()
    ca = x509.load_pem_x509_certificate((settings.tls_dir / tls.CA_CERT).read_bytes())
    assert info == {"mode": "auto", "ca_fingerprint": tls.fingerprint(ca), "ca_available": True}
    r = client.get("/api/tls/ca.pem")
    assert r.status_code == 200
    assert x509.load_pem_x509_certificate(r.content) == ca
    assert "attachment" in r.headers["content-disposition"]


def test_pairing_code_carries_fingerprint_and_public_url(
    client: TestClient, settings: Settings
) -> None:
    body = client.post("/api/pairing-codes", headers=AUTH).json()
    assert body["ca_fingerprint"] == client.get("/api/tls").json()["ca_fingerprint"]
    assert body["hub_url"] is None
    settings.public_url = "https://hub.example.lan/"
    assert client.post("/api/pairing-codes", headers=AUTH).json()["hub_url"] == (
        "https://hub.example.lan"
    )


def test_no_ca_when_tls_is_off(settings: Settings) -> None:
    from topcoat_hub.main import create_app

    settings.tls_mode = "off"
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/tls").json() == {
            "mode": "off",
            "ca_fingerprint": None,
            "ca_available": False,
        }
        assert c.get("/api/tls/ca.pem").status_code == 404


def test_empty_file_settings_mean_unset(monkeypatch) -> None:
    monkeypatch.setenv("TOPCOAT_TLS_CERT_FILE", "")
    monkeypatch.setenv("TOPCOAT_TLS_CA_FILE", "")
    s = Settings()
    assert s.tls_cert_file is None
    assert s.tls_ca_file is None
