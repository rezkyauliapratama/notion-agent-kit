"""Media-file handler for retrieving and uploading Notion files."""

import mimetypes
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from handlers.validate import extract_page_id

# These block types use the same Notion file-object shape. Keeping the set here
# makes the supported surface explicit and gives callers one consistent result.
SUPPORTED_MEDIA_BLOCK_TYPES = frozenset({"image", "file", "video", "pdf", "audio"})
SUPPORTED_UPLOAD_BLOCK_TYPES = SUPPORTED_MEDIA_BLOCK_TYPES
_FILE_OBJECT_TYPES = frozenset({"file", "external"})
_SUPPORTED_URL_SCHEMES = frozenset({"http", "https"})
MAX_SINGLE_PART_BYTES = 20 * 1024 * 1024
MAX_FILENAME_BYTES = 900
_DEFAULT_CONTENT_TYPE = "application/octet-stream"
_MIME_TYPE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+\\-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+\\-]{0,126}$")


class FileHandler:
    """Handle media download and local upload requests."""

    def __init__(self, client):
        self.client = client

    async def upload_file(self, file_path: str, target_id: str,
                          target_type: str = "page",
                          mime_type: Optional[str] = None) -> dict:
        """Upload one local file and append it as a media block.

        The local path is opened only by the client during the send request.
        This handler owns all user-input validation and never returns the local
        path or authentication material in its result.
        """
        prepared, error = self._prepare_upload(
            file_path, target_id, target_type, mime_type)
        if error:
            return error
        assert prepared is not None

        try:
            created = await self.client.create_file_upload(
                prepared["filename"], prepared["content_type"])
        except Exception as exc:
            return self._phase_failed("CREATE_FAILED", exc)
        if not isinstance(created, dict):
            return self._invalid_input("Notion returned an invalid file upload response.")
        if created.get("error"):
            return created
        upload_id = created.get("id")
        if not isinstance(upload_id, str) or not upload_id.strip():
            return self._invalid_input(
                "Notion returned a file upload response without an upload ID.")

        try:
            sent = await self.client.send_file_upload(
                upload_id, prepared["path"], prepared["filename"],
                prepared["content_type"])
        except Exception as exc:
            return self._phase_failed(
                "SEND_FAILED", exc, upload_id, redacted_value=prepared["path"])
        if not isinstance(sent, dict):
            return self._phase_failed(
                "SEND_FAILED", "Notion returned an invalid send response.", upload_id)
        if sent.get("error"):
            return sent
        status = sent.get("status")
        if status is not None and status != "uploaded":
            return {
                "error": True,
                "code": "SEND_FAILED",
                "message": f"Notion did not mark the upload as uploaded (status: {status}).",
                "upload_id": upload_id,
            }

        block_type = self._block_type(prepared["content_type"], prepared["filename"])
        child = {
            "object": "block",
            "type": block_type,
            block_type: {
                "type": "file_upload",
                "file_upload": {"id": upload_id},
            },
        }
        try:
            attached = await self.client.append_block_children(
                prepared["target_id"], [child])
        except Exception as exc:
            return self._phase_failed("ATTACH_FAILED", exc, upload_id)
        if not isinstance(attached, dict):
            return self._phase_failed(
                "ATTACH_FAILED", "Notion returned an invalid attach response.", upload_id)
        if attached.get("error"):
            return attached

        block_id = None
        results = attached.get("results")
        if isinstance(results, list) and results and isinstance(results[0], dict):
            candidate = results[0].get("id")
            if isinstance(candidate, str) and candidate:
                block_id = candidate
        return {
            "upload_id": upload_id,
            "block_id": block_id,
            "target_id": prepared["target_id"],
            "target_type": prepared["target_type"],
            "filename": prepared["filename"],
            "content_type": prepared["content_type"],
            "content_length": prepared["content_length"],
            "block_type": block_type,
        }

    @classmethod
    def _prepare_upload(cls, file_path, target_id, target_type, mime_type):
        if not isinstance(target_type, str) or target_type.strip().lower() not in {"page", "block"}:
            return None, cls._invalid_input("target_type must be 'page' or 'block'")
        normalized_target = extract_page_id(target_id)
        if normalized_target is None:
            return None, cls._invalid_input(
                "Invalid target_id. Expected 32 hex chars, a UUID, or a Notion URL.")
        if not isinstance(file_path, str) or not file_path.strip():
            return None, cls._invalid_input("file_path is required")
        if "\x00" in file_path:
            return None, cls._invalid_input("file_path contains an invalid null character")
        try:
            path = Path(file_path).expanduser()
            stat = path.stat()
        except (OSError, ValueError):
            return None, cls._invalid_input("file_path must point to a readable local file")
        if not path.is_file():
            return None, cls._invalid_input("file_path must point to a regular local file")
        try:
            with path.open("rb"):
                pass
        except OSError:
            return None, cls._invalid_input("file_path must point to a readable local file")
        if stat.st_size > MAX_SINGLE_PART_BYTES:
            return None, cls._invalid_input(
                f"file exceeds the single-part upload limit of {MAX_SINGLE_PART_BYTES} bytes")

        filename = path.name
        if (not filename or filename in {".", ".."} or
                any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            return None, cls._invalid_input("file name contains unsupported control characters")
        if len(filename.encode("utf-8")) > MAX_FILENAME_BYTES:
            return None, cls._invalid_input(
                f"file name exceeds Notion's {MAX_FILENAME_BYTES}-byte limit")

        if mime_type is None:
            content_type = mimetypes.guess_type(filename, strict=False)[0] or _DEFAULT_CONTENT_TYPE
        elif isinstance(mime_type, str) and mime_type.strip():
            content_type = mime_type.strip().lower()
        else:
            return None, cls._invalid_input("mime_type must be a non-empty MIME type when supplied")
        if not _MIME_TYPE_PATTERN.fullmatch(content_type):
            return None, cls._invalid_input("mime_type must be a valid type/subtype MIME value")
        if not Path(filename).suffix:
            inferred_extension = mimetypes.guess_extension(content_type, strict=False) or ""
            if len((filename + inferred_extension).encode("utf-8")) > MAX_FILENAME_BYTES:
                return None, cls._invalid_input(
                    f"file name plus inferred extension exceeds Notion's {MAX_FILENAME_BYTES}-byte limit")

        return {
            "path": path,
            "target_id": normalized_target,
            "target_type": target_type.strip().lower(),
            "filename": filename,
            "content_type": content_type,
            "content_length": stat.st_size,
        }, None

    @staticmethod
    def _block_type(content_type: str, filename: str) -> str:
        if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
            return "pdf"
        if content_type.startswith("image/"):
            return "image"
        if content_type.startswith("video/"):
            return "video"
        if content_type.startswith("audio/"):
            return "audio"
        return "file"

    @staticmethod
    def _phase_failed(code: str, message, upload_id=None,
                      redacted_value=None) -> dict:
        safe_message = str(message)
        if redacted_value:
            safe_message = safe_message.replace(str(redacted_value), "local file")
        result = {"error": True, "code": code, "message": safe_message}
        if upload_id:
            result["upload_id"] = upload_id
        return result

    @staticmethod
    def _invalid_input(message: str) -> dict:
        return {"error": True, "code": "INVALID_INPUT", "message": message}

    async def get_block_download_urls(self, block_id: str) -> dict:
        """Retrieve a media block and return its current downloadable URL.

        Notion-hosted media uses a temporary, signed URL in ``file.url``;
        external media uses ``external.url``. The block is retrieved on every
        call so a caller can refresh an expired Notion-hosted URL.
        """
        if not isinstance(block_id, str) or not block_id.strip():
            return self._invalid_input("block_id is required")

        try:
            block = await self.client.retrieve_block(block_id)
        except Exception as e:
            return {"error": True, "code": "FETCH_FAILED", "message": str(e)}

        if not isinstance(block, dict):
            return self._invalid_input("Notion returned an invalid block response.")
        if block.get("error"):
            return block

        block_type = block.get("type")
        if not isinstance(block_type, str) or block_type not in SUPPORTED_MEDIA_BLOCK_TYPES:
            received_type = block_type if isinstance(block_type, str) and block_type else "unknown"
            supported = ", ".join(sorted(SUPPORTED_MEDIA_BLOCK_TYPES))
            return self._invalid_input(
                f"Block type '{received_type}' does not support downloadable URLs. "
                f"Supported types: {supported}.")

        content = block.get(block_type)
        if not isinstance(content, dict):
            return self._invalid_input(
                f"Block '{block_id}' has malformed {block_type} file data.")

        source_type = content.get("type")
        if not isinstance(source_type, str) or source_type not in _FILE_OBJECT_TYPES:
            return self._invalid_input(
                f"Block '{block_id}' has unsupported or missing {block_type} "
                "file-object type. Expected 'file' or 'external'.")

        file_object = content.get(source_type)
        if not isinstance(file_object, dict):
            return self._invalid_input(
                f"Block '{block_id}' has malformed {source_type} file data.")

        url = file_object.get("url")
        if not self._is_download_url(url):
            return self._invalid_input(
                f"Block '{block_id}' has a missing or malformed {source_type} URL.")

        entry = {"url": url, "source": source_type}
        expiry_time = file_object.get("expiry_time")
        if isinstance(expiry_time, str) and expiry_time.strip():
            entry["expiry_time"] = expiry_time

        # A Notion media block currently has one file object. Returning a list
        # keeps the response stable if Notion ever exposes multiple downloads.
        return {
            "block_id": block_id,
            "type": block_type,
            "urls": [entry],
        }

    @staticmethod
    def _is_download_url(value) -> bool:
        """Accept only unmodified absolute HTTP(S) URLs usable by callers."""
        if not isinstance(value, str) or not value or value != value.strip():
            return False
        if any(character.isspace() for character in value):
            return False
        try:
            parsed = urlparse(value)
            if parsed.scheme.lower() not in _SUPPORTED_URL_SCHEMES or not parsed.hostname:
                return False
            # Accessing ``port`` validates malformed ports and unmatched IPv6
            # brackets that urlparse otherwise defers until this property read.
            _ = parsed.port
            return "\\" not in parsed.netloc
        except ValueError:
            return False
