from app.core.storage import supabase
from app.exceptions.storage_exception import StorageException


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
            file_options = {}

            if content_type:
                file_options["content-type"] = content_type

            supabase.storage.from_(bucket_name).upload(
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
            supabase.storage.from_(bucket_name).remove([file_path])

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
            return supabase.storage.from_(bucket_name).download(file_path)

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
            response = supabase.storage.from_(bucket_name).create_signed_url(
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

            files = supabase.storage.from_(bucket_name).list(folder)

            return any(file["name"] == filename for file in files)

        except Exception as ex:
            raise StorageException(
                f"Failed to check file '{file_path}'."
            ) from ex