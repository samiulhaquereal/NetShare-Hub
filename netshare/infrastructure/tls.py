"""Self-signed HTTPS certificate for the LAN.

Browsers only allow the microphone (voice messages), camera, clipboard API and
service workers on ``https://`` or ``localhost``. Devices reach this PC at
``http://192.168.x.x`` — so the app also listens on HTTPS with a certificate it
makes itself. It isn't signed by a public authority, so each device shows a
one-time "not private" warning; after accepting it the page is a secure context.

The certificate lists every local IP (and ``localhost``) as a subject alternative
name and is re-made when the PC's IP changes or it gets close to expiry.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
import ssl
from pathlib import Path

CERT_NAME, KEY_NAME = "netshare-cert.pem", "netshare-key.pem"
VALID_DAYS = 800          # Apple rejects TLS certificates valid for more than 825 days
RENEW_BEFORE_DAYS = 30


def local_hosts(extra: list[str] | None = None) -> list[str]:
    """IPs/names this PC is reachable at on the LAN (plus loopback)."""
    hosts = {"localhost", "127.0.0.1"}
    try:
        name = socket.gethostname()
        hosts.add(name)
        for info in socket.getaddrinfo(name, None, socket.AF_INET):
            hosts.add(info[4][0])
    except OSError:
        pass
    for h in extra or ():
        if h:
            hosts.add(h)
    return sorted(hosts)


def _split(hosts: list[str]) -> tuple[list[str], list[str]]:
    ips, names = [], []
    for h in hosts:
        try:
            ipaddress.ip_address(h)
            ips.append(h)
        except ValueError:
            names.append(h)
    return ips, names


def _covers(cert_path: Path, hosts: list[str]) -> bool:
    """Existing certificate still valid for a while and lists every current IP/name?"""
    from cryptography import x509
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return False
    if cert.not_valid_after_utc - dt.timedelta(days=RENEW_BEFORE_DAYS) < dt.datetime.now(dt.timezone.utc):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    have = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))
    return set(hosts) <= have


def ensure_certificate(cert_dir: Path, hosts: list[str]) -> tuple[Path, Path]:
    """(cert, key) paths, (re)generating them when needed."""
    cert_dir = Path(cert_dir)
    cert_path, key_path = cert_dir / CERT_NAME, cert_dir / KEY_NAME
    if cert_path.exists() and key_path.exists() and _covers(cert_path, hosts):
        return cert_path, key_path

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    ips, names = _split(hosts)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RetCore OnLAN (this PC)"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "RetCore OnLAN")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=VALID_DAYS))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]),
                critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                         data_encipherment=False, key_agreement=True, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))

    cert_dir.mkdir(parents=True, exist_ok=True)
    tmp_key, tmp_cert = key_path.with_suffix(".tmp"), cert_path.with_suffix(".tmp")
    tmp_key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    try:
        os.chmod(tmp_key, 0o600)
    except OSError:
        pass
    tmp_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(tmp_key, key_path)
    os.replace(tmp_cert, cert_path)
    return cert_path, key_path


def server_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert_path), str(key_path))
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx
