"""Self-signed certificate so the iPhone can open the live-camera page over https (Safari requires it for camera access)."""
import datetime, ipaddress
from . import db


def ensure(ips):
    cert, key, meta = db.DATA_DIR / "cert.pem", db.DATA_DIR / "key.pem", db.DATA_DIR / "cert.ips"
    ips = sorted(set(ips))
    if cert.exists() and key.exists() and meta.exists() and set(meta.read_text().split(",")) >= set(ips):
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MTG Pile Scanner")])
    san = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=800))
         .add_extension(x509.SubjectAlternativeName(san), critical=False).sign(k, hashes.SHA256()))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    meta.write_text(",".join(ips))
    return cert, key
