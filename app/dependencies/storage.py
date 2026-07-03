from app.core.storage_service import StorageService


def get_storage_service() -> StorageService:
    """
    Returns a StorageService instance.
    """
    return StorageService()