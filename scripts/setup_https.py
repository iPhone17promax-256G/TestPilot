"""Create private local-development TLS material; never install trust implicitly.

Run with the project's Python dependencies or the runtime image. Only rootCA.pem
is a trust anchor. rootCA-key.pem must remain on the host and must not be mounted
into either service. Servers receive only their own leaf key and public CA bundle.
"""
import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def private_write(path, data, private=False):
    """Write atomically with explicit permissions, without exposing key contents."""
    temp = path.with_name(path.name + ".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, 0o600 if private else 0o644)
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def key_bytes(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def ca_material(root, now):
    cert_path, key_path = root / "rootCA.pem", root / "rootCA-key.pem"
    if cert_path.exists() != key_path.exists():
        raise RuntimeError("本地 CA 文件不完整，未覆盖；请先检查已有证书备份")
    if cert_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if cert.public_key().public_numbers() != key.public_key().public_numbers():
            raise RuntimeError("本地 CA 证书与私钥不匹配")
        if cert.not_valid_after_utc <= now + timedelta(days=30):
            raise RuntimeError("本地 CA 即将过期，请显式更换并重新安装信任")
        os.chmod(key_path, 0o600)
        return cert, key
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TestPilot Local Development CA")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    private_write(key_path, key_bytes(key), private=True)
    private_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert, key


def server_material(root, service, names, ca, ca_key, now):
    cert_path, key_path = root / f"{service}.pem", root / f"{service}-key.pem"
    expected = set()
    for name in names:
        try:
            expected.add(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            if any(char in name for char in "/\\\r\n ") or not name:
                raise ValueError("证书主机名应为域名或 IP，不能填写完整 URL")
            expected.add(x509.DNSName(name.encode("idna").decode("ascii")))
    expected |= {x509.IPAddress(ipaddress.ip_address(value)) for value in ["127.0.0.1", "::1"]}
    if cert_path.exists() and key_path.exists():
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        actual = set(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value)
        try:
            ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
            valid = cert.issuer == ca.subject and cert.not_valid_after_utc > now + timedelta(days=30)
            valid &= cert.public_key().public_numbers() == key.public_key().public_numbers() and expected <= actual
        except Exception:
            valid = False
        if valid:
            os.chmod(key_path, 0o600)
            return cert
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"TestPilot {service}")]))
            .issuer_name(ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(min(now + timedelta(days=397), ca.not_valid_after_utc))
            .add_extension(x509.SubjectAlternativeName(sorted(expected, key=str)), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                key_encipherment=True, data_encipherment=False, key_agreement=False,
                key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    private_write(key_path, key_bytes(key), private=True)
    private_write(cert_path, cert.public_bytes(serialization.Encoding.PEM))
    return cert


def main():
    parser = argparse.ArgumentParser(description="生成本机开发证书，不自动修改系统信任")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--host", action="append", default=[], help="额外访问域名或 IP，可重复指定")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    now = datetime.now(timezone.utc)
    ca, ca_key = ca_material(root, now)
    platform = server_material(root, "platform", ["localhost", "api", "host.docker.internal", *args.host], ca, ca_key, now)
    executor = server_material(root, "executor", ["localhost", "executor", "host.docker.internal", *args.host], ca, ca_key, now)
    # Retain public roots too: replacing the trust bundle with only the local CA
    # would break HTTPS calls to public model providers and external MCP services.
    public_roots = Path(certifi.where()).read_bytes().rstrip()
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    private_write(root / "ca-bundle.pem", public_roots + b"\n" + ca_pem)
    print(json.dumps({"directory": str(root), "caSha256": ca.fingerprint(hashes.SHA256()).hex(),
        "platformExpires": platform.not_valid_after_utc.isoformat(),
        "executorExpires": executor.not_valid_after_utc.isoformat(),
        "trustAnchor": "rootCA.pem", "trustInstalled": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
