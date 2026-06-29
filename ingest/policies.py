import copy
import hashlib
import logging
import os
import re

import requests
from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters.character import CharacterTextSplitter

from config import get_settings
from db.redis_client import redis
from db.vector import get_vectorstore

logger = logging.getLogger(__name__)

# Redis set key that tracks all ingested file hashes for duplicate detection
_INGESTED_HASHES_KEY = "ingested_file_hashes"

# Hebrew Unicode block U+0590–U+05FF
_HEBREW_UNICODE_RE = re.compile(r'[֐-׿]')
# Latin-1 characters that appear when Hebrew CP1255 bytes are mis-decoded as Latin-1
_GARBLED_HEBREW_RE = re.compile(r'[àáâãäåæçèéêëìíîïðñòóôõöøùú]{2,}')


def _fix_hebrew_visual_order(text: str) -> str:
    """
    Fix Hebrew PDFs stored in visual (right-to-left display) order.
    PyPDFLoader extracts these with each line's characters reversed.
    Reversing each Hebrew line restores correct logical reading order.
    e.g. 'לארשיב סמה יללכ' → 'כללי המס בישראל'
    """
    lines = text.split('\n')
    return '\n'.join(
        line[::-1] if _HEBREW_UNICODE_RE.search(line) else line
        for line in lines
    )


def _fix_hebrew_encoding(text: str) -> str:
    """
    Fix Hebrew PDFs where text was encoded in CP1255 (Windows Hebrew) but
    PyPDFLoader decoded it as Latin-1, producing garbled characters.
    e.g. 'îàú òå"ã' → 'מאת עו"ד'
    """
    try:
        return text.encode('latin-1').decode('cp1255')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _clean_text(text: str) -> str:
    # Modern PyPDFLoader extracts Hebrew Unicode in correct logical order already.
    # Only apply encoding fix for genuine Latin-1/CP1255 mismatch (no Hebrew Unicode present).
    if _GARBLED_HEBREW_RE.search(text) and not _HEBREW_UNICODE_RE.search(text):
        text = _fix_hebrew_encoding(text)
    text = re.sub(r'\n+', '\n', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _download_file(s3_url: str, file_name: str) -> str:
    setting = get_settings()
    try:
        # Why stream=True? beacuse To handle large files without loading them entirely into memory mean Downloads little-by-little in chunks.
        response = requests.get(
            s3_url,
            timeout=setting.download_timeout_seconds,
            stream=True,
        )
        # “If HTTP response is not successful, stop execution and throw an error.” else continue with the file processing. This is important to avoid processing invalid or incomplete files.
        response.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to download {s3_url}: {e}") from e

    # create a temporary file path to save the downloaded file. The file is saved in the /tmp directory with a .pdf extension. This allows us to work with the file locally for further processing.
    file_path = f"/tmp/{file_name}.pdf"
    # Calculate the maximum allowed file size in bytes based on the configuration setting. This is used to enforce a limit on the size of files that can be ingested, preventing excessive resource usage and potential abuse.
    max_bytes = setting.max_file_size_mb * 1024 * 1024
    size = 0
    # this block reads the response content in chunks and writes it to the temporary file. It also keeps track of the total size of the downloaded content. If the size exceeds the maximum allowed limit, it raises an error to prevent processing excessively large files.
    with open(file_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            size += len(chunk)
            if size > max_bytes:
                raise RuntimeError(
                    f"File exceeds maximum allowed size of {setting.max_file_size_mb}MB"
                )
            f.write(chunk)

    return file_path

# The _file_hash function computes the SHA-256 hash of a file's content. It reads the file in chunks to efficiently handle large files without consuming excessive memory. The resulting hash is used for duplicate detection, allowing the system to identify and skip files that have already been ingested based on their content rather than just their name.
def _file_hash(file_path: str) -> str:
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def process_policy(file_name: str, s3_url: str):
    setting = get_settings()
    logger.info("Processing file: %s", file_name)
    file_path = None

    try:
        file_path = _download_file(s3_url, file_name)
        fhash = _file_hash(file_path)
        # Check if the file hash already exists in the Redis set. If it does, it means the file has already been ingested, and we can skip processing it again. This helps to avoid duplicate entries in the vector store and saves resources by not reprocessing the same content.
        # The sismember command checks if the file hash is a member of the Redis set identified by _INGESTED_HASHES_KEY. If it returns true, we log that the file is being skipped due to duplicate content and return a response indicating that the file was skipped along with the reason.
        if redis.sismember(_INGESTED_HASHES_KEY, fhash):
            logger.info("Skipping duplicate file: %s (Redis)", file_name)
            return {
                "file_name": file_name,
                "status": "skipped",
                "reason": "duplicate content",
            }

        # Fallback: check ChromaDB metadata in case Redis was flushed
        existing = get_vectorstore()._collection.get(
            where={"file_hash": fhash}, limit=1
        )
        if existing["ids"]:
            redis.sadd(_INGESTED_HASHES_KEY, fhash)  # restore missing Redis entry
            logger.info("Skipping duplicate file: %s (ChromaDB)", file_name)
            return {
                "file_name": file_name,
                "status": "skipped",
                "reason": "duplicate content",
            }

        loader = PyPDFLoader(file_path)
        pages = loader.load()

        pages_clean = copy.deepcopy(pages)
        for doc in pages_clean:
            doc.page_content = _clean_text(doc.page_content)
            doc.metadata["source_file"] = file_name
            doc.metadata["file_hash"] = fhash

        splitter = CharacterTextSplitter(chunk_size=500, chunk_overlap=50)
        chunks = splitter.split_documents(pages_clean)

        get_vectorstore().add_documents(chunks)
        redis.sadd(_INGESTED_HASHES_KEY, fhash)

        logger.info("Stored %d chunks for %s", len(chunks), file_name)
        return {
            "file_name": file_name,
            "num_pages": len(pages),
            "num_chunks": len(chunks),
            "status": "ingested",
            "file_hash": fhash,
        }

    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
            logger.debug("Cleaned up temp file: %s", file_path)
