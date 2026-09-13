import base64
import hashlib
import hmac
import os
import secrets
from uuid import UUID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_settings

# Salt fijo para derivación de claves por tenant
TENANT_KDF_SALT = b"facturia-tenant-aes-256-gcm-v1"


def get_master_key() -> bytes:
    """Obtiene y decodifica la clave maestra de cifrado en base64."""
    settings = get_settings()
    key_bytes = base64.b64decode(settings.MASTER_ENCRYPTION_KEY)
    if len(key_bytes) != 32:
        raise ValueError("MASTER_ENCRYPTION_KEY debe ser exactamente de 32 bytes (256 bits)")
    return key_bytes


def derive_tenant_key(tenant_id: UUID, master_key: bytes | None = None) -> bytes:
    """
    Deriva criptográficamente una clave AES-256 única para el tenant usando HKDF-SHA256.
    Regla 6: Clave derivada por tenant desde la clave maestra.
    """
    if master_key is None:
        master_key = get_master_key()

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=TENANT_KDF_SALT,
        info=str(tenant_id).encode("utf-8"),
    )
    return hkdf.derive(master_key)


def encrypt_credentials(
    tenant_id: UUID, plaintext: bytes, master_key: bytes | None = None
) -> tuple[bytes, bytes]:
    """
    Cifra las credenciales de un portal usando AES-256-GCM.
    El tenant_id se vincula como Datos Asociados Autenticados (AAD), garantizando
    que el texto cifrado no pueda ser descifrado si se cambia el tenant.
    
    Retorna: (payload_cifrado, nonce)
    """
    derived_key = derive_tenant_key(tenant_id, master_key)
    aesgcm = AESGCM(derived_key)
    nonce = os.urandom(12)  # Nonce estándar de 96 bits para AES-GCM
    aad = str(tenant_id).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
    return ciphertext, nonce


def decrypt_credentials(
    tenant_id: UUID, ciphertext: bytes, nonce: bytes, master_key: bytes | None = None
) -> bytes:
    """
    Descifra credenciales usando AES-256-GCM con la clave derivada del tenant y AAD.
    Si se intenta descifrar con el tenant_id incorrecto, la verificación falla con InvalidTag.
    """
    derived_key = derive_tenant_key(tenant_id, master_key)
    aesgcm = AESGCM(derived_key)
    aad = str(tenant_id).encode("utf-8")
    return aesgcm.decrypt(nonce, ciphertext, aad)


def generate_one_time_token() -> tuple[str, str]:
    """
    Genera un token seguro de un solo uso.
    Retorna: (token_plano, token_hash_sha256)
    Solo se almacena en la base de datos el token_hash.
    """
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return token, token_hash


def verify_one_time_token(token: str, token_hash: str) -> bool:
    """
    Verifica un token de un solo uso en tiempo constante contra su hash SHA256.
    """
    calculated_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(calculated_hash, token_hash)
