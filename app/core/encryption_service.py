import functools
import hashlib
import hmac
import os
import uuid

from dotenv import dotenv_values

from cryptography.fernet import Fernet, InvalidToken

from app.core.encryption import resolve_key_material
from app.exceptions.resume_exceptions import EncryptionUnavailableException
from app.models.config import EncryptionKey
from app.repositories.encryption_key_repository import EncryptionKeyRepository


# P-M3: the key for email/phone lookup hashes. LOOKUP_HASH_KEY when set; otherwise derived from the
# candidate-PII encryption key, so no new secret is needed. Rotating that key (or setting/changing
# LOOKUP_HASH_KEY) changes every hash: re-hash the stored ones with scripts/rehash_candidate_lookups.py.
LOOKUP_HASH_KEY_ENV = "LOOKUP_HASH_KEY"
_LOOKUP_KEY_BASE_ALIAS = "candidate-pii-v1"
_LOOKUP_KEY_CONTEXT = b"airs-lookup-hash-v1"


@functools.lru_cache(maxsize=1)
def lookup_hash_key() -> bytes:
    raw = os.environ.get(LOOKUP_HASH_KEY_ENV) or dotenv_values(".env").get(LOOKUP_HASH_KEY_ENV)
    if raw:
        return raw.encode("utf-8")
    base = resolve_key_material(_LOOKUP_KEY_BASE_ALIAS)
    return hmac.new(base, _LOOKUP_KEY_CONTEXT, hashlib.sha256).digest()


class DecryptionError(Exception):
    """Raised when ciphertext cannot be decrypted with the resolved key."""


class EncryptionService:
    def __init__(self, encryption_key_repo: EncryptionKeyRepository):
        self.encryption_key_repo = encryption_key_repo

    def _resolve_usable_key(self, purpose: str) -> EncryptionKey:
        key_row = self.encryption_key_repo.get_active_by_purpose(purpose)
        if key_row is None:
            # No ACTIVE key — fall back to a ROTATING key so uploads are not
            # blocked mid key-rotation.
            key_row = self.encryption_key_repo.get_rotating_by_purpose(purpose)
        if key_row is None:
            raise EncryptionUnavailableException(
                f"No ACTIVE or ROTATING encryption key configured for purpose '{purpose}'."
            )
        return key_row

    def encrypt(self, value: str, purpose: str) -> tuple[bytes, uuid.UUID]:
        """
        Encrypts `value` using the active (or rotating-fallback) key for
        `purpose`. Returns (ciphertext, encryption_key_id) — the caller is
        responsible for persisting both alongside the encrypted column.
        """
        key_row = self._resolve_usable_key(purpose)
        key_material = resolve_key_material(key_row.key_alias)
        ciphertext = Fernet(key_material).encrypt(value.encode("utf-8"))
        return ciphertext, key_row.id

    def decrypt(self, ciphertext: bytes, encryption_key_id: uuid.UUID) -> str:
        """
        Decrypts `ciphertext` using the specific key referenced by
        `encryption_key_id` (as stored on the encrypted record) — not
        necessarily the currently active key, since old records may have
        been encrypted under a since-retired key version.
        """
        key_row = self.encryption_key_repo.get_by_id(encryption_key_id)
        if key_row is None:
            raise DecryptionError(f"Encryption key '{encryption_key_id}' not found.")

        key_material = resolve_key_material(key_row.key_alias)
        try:
            return Fernet(key_material).decrypt(ciphertext).decode("utf-8")
        except InvalidToken as exc:
            raise DecryptionError(
                "Unable to decrypt value — invalid key or corrupted ciphertext."
            ) from exc

    @staticmethod
    def generate_hash(value: str) -> str:
        """
        Deterministic keyed hash (HMAC-SHA256, hex) of a normalized (trimmed, lowercased) value, used
        for dedup lookups (email_hash/phone_hash) without ever requiring decryption. Keyed (P-M3): an
        unsalted MD5 of an email or phone number could be reversed by trying likely values.
        """
        normalized = value.strip().lower()
        return hmac.new(lookup_hash_key(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()
