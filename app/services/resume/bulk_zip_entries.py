"""
Which entries of a bulk ZIP are resume files, and which entry a staged file came from.

Extraction (bulk_upload_tasks._extract_and_stage) stages the i-th real entry under
".../{i:04d}_{name}" (review item 23). Erasure (review item 15) uses the same listing to remove exactly
the candidate's entries from the stored ZIP - by position, never by file name, since two files called
"CV.pdf" in different folders belong to different people.
"""
import re
import zipfile

JUNK_PATH_PREFIXES = ("__MACOSX/",)
# "0003.pdf" (review item 28: no file name in the key) or "0003_cv.pdf" (staged before that).
_STAGED_INDEX = re.compile(r"^(\d{4})(?:[_.]|$)")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def staged_file_name(index: int, basename: str) -> str:
    """The staged object's name: the entry's position and extension only - an uploaded file name
    often is the candidate's name, and storage keys appear in logs and errors (review item 28)."""
    match = _EXTENSION.search(basename or "")
    return f"{index:04d}{match.group(0).lower() if match else ''}"


def real_entries(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    """(entry, basename) for every file extraction stages, in extraction order."""
    entries = []
    for entry in archive.infolist():
        if entry.is_dir():
            continue
        if any(entry.filename.startswith(prefix) for prefix in JUNK_PATH_PREFIXES):
            continue
        basename = entry.filename.rsplit("/", 1)[-1]
        if not basename or basename.startswith("."):
            continue
        entries.append((entry, basename))
    return entries


def staged_entry_index(storage_path: str | None) -> int | None:
    """The ZIP position a staged file came from ("0003_cv.pdf" -> 3); None for older "{uuid}_" keys."""
    if not storage_path:
        return None
    match = _STAGED_INDEX.match(storage_path.rsplit("/", 1)[-1])
    return int(match.group(1)) if match else None
