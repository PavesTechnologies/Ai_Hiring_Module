import re
import unicodedata

from pathlib import Path

from app.core.config import settings
from app.core.storage import get_supabase
from app.exceptions.storage_exception import StorageException

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_KEY_STEM = 100


def storage_safe_filename(filename: str) -> str:
    """
    A user-supplied file name made safe for a Supabase Storage object key.
    Supabase rejects keys with characters like [ ] { } # % or non-ASCII
    ("Invalid key"), and job-board exports routinely use them - e.g. Naukri's
    "Name[24y_0m].pdf". Accents are folded to ASCII, anything else outside
    letters/digits/. _ - becomes "_", the extension is kept (lower-cased) so
    the file type still reads from the key. Only for the key - always keep
    the original name separately for display.
    """
    name = unicodedata.normalize("NFKD", filename or "").encode("ascii", "ignore").decode("ascii")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = _UNSAFE_KEY_CHARS.sub("_", stem).strip("._-")[:_MAX_KEY_STEM] or "file"
    ext = _UNSAFE_KEY_CHARS.sub("", ext).lower()
    return f"{stem}.{ext}" if ext else stem


class StorageService:
    """
    Generic Storage Service for Supabase Storage.

    This service is responsible only for interacting with
    Supabase Storage.

    It does NOT know anything about:
        - Job Descriptions
        - Resumes
        - Candidates
        - Database
        - Business Logic
    """

    # STORAGE_BACKEND=local: files under LOCAL_STORAGE_DIR/<bucket>/<path> -
    # isolated local stacks only; Supabase is never contacted.
    @staticmethod
    def _local() -> bool:
        return (settings.storage_backend or "supabase").lower() == "local"

    @staticmethod
    def _local_path(bucket_name: str, file_path: str) -> Path:
        root = Path(settings.local_storage_dir).resolve()
        path = (root / bucket_name / file_path).resolve()
        if root not in path.parents:
            raise StorageException(f"Invalid storage path '{file_path}'.")
        return path

    def upload_file(
        self,
        bucket_name: str,
        file_path: str,
        file_content: bytes,
        content_type: str | None = None,
    ) -> str:
        """
        Upload a file to Supabase Storage.

        Args:
            bucket_name: Storage bucket name.
            file_path: Object path inside bucket.
            file_content: File bytes.
            content_type: MIME type.

        Returns:
            Uploaded object path.
        """
        try:
            if self._local():
                path = self._local_path(bucket_name, file_path)
                if path.exists():
                    raise StorageException(f"File '{file_path}' already exists.")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(file_content)
                return file_path

            file_options = {}

            if content_type:
                file_options["content-type"] = content_type

            get_supabase().storage.from_(bucket_name).upload(
                path=file_path,
                file=file_content,
                file_options=file_options,
            )

            return file_path

        except Exception as ex:
            raise StorageException(
                f"Failed to upload file to bucket '{bucket_name}'."
            ) from ex

    def delete_file(
        self,
        bucket_name: str,
        file_path: str,
    ) -> None:
        """
        Delete a file from Supabase Storage.
        """
        try:
            if self._local():
                self._local_path(bucket_name, file_path).unlink(missing_ok=True)
                return
            get_supabase().storage.from_(bucket_name).remove([file_path])

        except Exception as ex:
            raise StorageException(
                f"Failed to delete file '{file_path}'."
            ) from ex

    def download_file(
        self,
        bucket_name: str,
        file_path: str,
    ) -> bytes:
        """
        Download a file from Supabase Storage.

        Returns:
            File bytes.
        """
        try:
            if self._local():
                return self._local_path(bucket_name, file_path).read_bytes()
            return get_supabase().storage.from_(bucket_name).download(file_path)

        except Exception as ex:
            raise StorageException(
                f"Failed to download file '{file_path}'."
            ) from ex

    def generate_signed_url(
        self,
        bucket_name: str,
        file_path: str,
        expires_in: int = 3600,
    ) -> str:
        """
        Generate a signed URL for a private file.

        Args:
            bucket_name: Storage bucket.
            file_path: Object path.
            expires_in: URL validity in seconds.

        Returns:
            Signed URL.
        """
        try:
            if self._local():
                return self._local_path(bucket_name, file_path).as_uri()
            response = get_supabase().storage.from_(bucket_name).create_signed_url(
                file_path,
                expires_in,
            )

            return response["signedURL"]

        except Exception as ex:
            raise StorageException(
                f"Failed to generate signed URL for '{file_path}'."
            ) from ex

    def file_exists(
        self,
        bucket_name: str,
        file_path: str,
    ) -> bool:
        """
        Check whether a file exists.

        Returns:
            True if the file exists.
            False otherwise.
        """
        try:
            folder = "/".join(file_path.split("/")[:-1])
            filename = file_path.split("/")[-1]

            if self._local():
                return self._local_path(bucket_name, file_path).exists()
            files = get_supabase().storage.from_(bucket_name).list(folder)

            return any(file["name"] == filename for file in files)

        except Exception as ex:
            raise StorageException(
                f"Failed to check file '{file_path}'."
            ) from ex