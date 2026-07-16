import hashlib
import uuid

from cryptography.fernet import Fernet, InvalidToken

from app.core.encryption import resolve_key_material
from app.exceptions.resume_exceptions import EncryptionUnavailableException
from app.models.config import EncryptionKey
from app.repositories.encryption_key_repository import EncryptionKeyRepository


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
        Deterministic MD5 hash of a normalized (trimmed, lowercased) value,
        used for dedup lookups (email_hash/phone_hash) without ever
        requiring decryption.
        """
        normalized = value.strip().lower()
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()
