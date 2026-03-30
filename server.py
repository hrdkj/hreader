import json
import os
import pickle
import shutil
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from reader3 import (
    Book,
    BookMetadata,
    ChapterContent,
    TOCEntry,
    auto_process_books_folder,
)


# Highlight data models
class Highlight(BaseModel):
    id: str
    chapter_index: int
    text: str
    start_offset: int
    end_offset: int
    created_at: str


class HighlightRequest(BaseModel):
    book_id: str
    highlight: Highlight


class RemoveHighlightRequest(BaseModel):
    book_id: str
    chapter_index: int
    highlight_id: str


app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Serve static files (PWA manifest, icons, etc.)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Where are the book folders located?
BOOKS_DIR = "."
AUDIOBOOKS_DIR = "audiobooks"
AUDIOBOOK_MAPPING_FILE = "audiobook_mapping.json"


@app.get("/sw.js")
async def serve_service_worker():
    """Serve service worker from root for proper PWA scope."""
    return FileResponse("static/sw.js", media_type="application/javascript")


@lru_cache(maxsize=10)
def load_book_cached(folder_name: str) -> Optional[Book]:
    """
    Loads the book from the pickle file.
    Cached so we don't re-read the disk on every click.
    """
    file_path = os.path.join(BOOKS_DIR, folder_name, "book.pkl")
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "rb") as f:
            book = pickle.load(f)
        return book
    except Exception as e:
        print(f"Error loading book {folder_name}: {e}")
        return None


def load_highlights(book_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Load highlights for a book from JSON file."""
    highlights_file = os.path.join(BOOKS_DIR, book_id, "highlights.json")
    if not os.path.exists(highlights_file):
        return {}

    try:
        with open(highlights_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading highlights for {book_id}: {e}")
        return {}


def save_highlights(book_id: str, highlights: Dict[str, List[Dict[str, Any]]]) -> bool:
    """Save highlights for a book to JSON file."""
    highlights_file = os.path.join(BOOKS_DIR, book_id, "highlights.json")
    try:
        with open(highlights_file, "w", encoding="utf-8") as f:
            json.dump(highlights, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving highlights for {book_id}: {e}")
        return False


# --- Reading Progress Functions ---


def load_reading_progress(book_id: str) -> Dict[str, Any]:
    """Load reading progress for a book from JSON file."""
    progress_file = os.path.join(BOOKS_DIR, book_id, "reading_progress.json")
    if not os.path.exists(progress_file):
        return {
            "current_chapter_index": 0,
            "scroll_position": 0,
            "scroll_percentage": 0.0,
        }

    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Ensure scroll position fields exist with defaults
            data.setdefault("scroll_position", 0)
            data.setdefault("scroll_percentage", 0.0)
            return data
    except Exception as e:
        print(f"Error loading reading progress for {book_id}: {e}")
        return {
            "current_chapter_index": 0,
            "scroll_position": 0,
            "scroll_percentage": 0.0,
        }


def save_reading_progress(
    book_id: str,
    chapter_index: int,
    scroll_position: int = 0,
    scroll_percentage: float = 0.0,
) -> bool:
    """Save reading progress for a book to JSON file."""
    progress_file = os.path.join(BOOKS_DIR, book_id, "reading_progress.json")
    try:
        data = {
            "current_chapter_index": chapter_index,
            "scroll_position": scroll_position,
            "scroll_percentage": scroll_percentage,
            "last_updated": datetime.now().isoformat(),
            "book_id": book_id,
        }
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving reading progress for {book_id}: {e}")
        return False


# --- Audiobook Functions ---


def load_audiobook_mapping() -> Dict[str, str]:
    """Load the audiobook mapping configuration file."""
    if not os.path.exists(AUDIOBOOK_MAPPING_FILE):
        return {}
    try:
        with open(AUDIOBOOK_MAPPING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Filter out comment keys (starting with _)
            return {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        print(f"Error loading audiobook mapping: {e}")
        return {}


def normalize_for_matching(name: str) -> str:
    """Normalize a string for fuzzy matching by removing special characters."""
    import re

    # Replace common separators and special chars with space
    normalized = re.sub(r"[_\-—–:;,\.\'\"\(\)\[\]]", " ", name)
    # Collapse multiple spaces
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower().strip()


def find_audiobook_for_book(book_id: str, book: Optional[Book] = None) -> Optional[str]:
    """
    Find the audiobook file for a given book.

    Search order:
    1. Check audiobook_mapping.json for explicit mapping (by book_id or title)
    2. Look for a folder matching the book name containing .m4b files
    3. Fall back to filename matching in audiobooks/ directory

    Returns the full path to the .m4b file (or folder with .m4b files), or None if not found.
    """
    if not os.path.exists(AUDIOBOOKS_DIR):
        return None

    # Load mapping
    mapping = load_audiobook_mapping()

    # Try mapping by book_id (folder name)
    if book_id in mapping:
        audiobook_path = os.path.join(AUDIOBOOKS_DIR, mapping[book_id])
        if os.path.exists(audiobook_path):
            return audiobook_path

    # Try mapping by book title (if book is loaded)
    if book and book.metadata.title in mapping:
        audiobook_path = os.path.join(AUDIOBOOKS_DIR, mapping[book.metadata.title])
        if os.path.exists(audiobook_path):
            return audiobook_path

    # Fallback: filename/folder matching
    # book_id is like "BookName_data", so we strip "_data" to get base name
    base_name = book_id.replace("_data", "") if book_id.endswith("_data") else book_id

    # Get book title if available
    book_title = book.metadata.title if book else None

    # Normalize names for matching
    base_normalized = normalize_for_matching(base_name)
    title_normalized = normalize_for_matching(book_title) if book_title else ""

    # Try to find a matching folder containing .m4b files
    try:
        for item in os.listdir(AUDIOBOOKS_DIR):
            item_path = os.path.join(AUDIOBOOKS_DIR, item)
            if os.path.isdir(item_path):
                item_normalized = normalize_for_matching(item)

                # Check for fuzzy match using normalized strings
                # Match if significant words overlap
                base_words = set(base_normalized.split())
                title_words = (
                    set(title_normalized.split()) if title_normalized else set()
                )
                item_words = set(item_normalized.split())

                # Remove common short words
                stop_words = {
                    "the",
                    "a",
                    "an",
                    "of",
                    "and",
                    "or",
                    "in",
                    "on",
                    "at",
                    "to",
                    "for",
                }
                base_words -= stop_words
                title_words -= stop_words
                item_words -= stop_words

                # Check if there's significant overlap (at least 2 words or 50% match)
                base_overlap = len(base_words & item_words)
                title_overlap = len(title_words & item_words) if title_words else 0

                base_match = base_overlap >= 2 or (
                    base_overlap >= 1 and base_overlap >= len(base_words) * 0.5
                )
                title_match = title_overlap >= 2 or (
                    title_overlap >= 1 and title_overlap >= len(title_words) * 0.5
                )

                # Also check if key identifying words match
                # For example, "Behave" should match "Behave"
                key_word_match = False
                for word in base_words | title_words:
                    if (
                        len(word) >= 5 and word in item_words
                    ):  # Match on significant words (5+ chars)
                        key_word_match = True
                        break

                if base_match or title_match or key_word_match:
                    # Check if this folder contains .m4b or .mp3 files
                    audio_files = sorted(
                        [
                            f
                            for f in os.listdir(item_path)
                            if f.lower().endswith((".m4b", ".mp3"))
                        ]
                    )
                    if audio_files:
                        return item_path
    except Exception as e:
        print(f"Error searching audiobook folders: {e}")

    # Try exact file match (single .m4b or .mp3 file in root, prioritize .m4b)
    for ext in [".m4b", ".M4B", ".mp3", ".MP3"]:
        audiobook_path = os.path.join(AUDIOBOOKS_DIR, base_name + ext)
        if os.path.exists(audiobook_path):
            return audiobook_path

    # Try case-insensitive file match
    try:
        for filename in os.listdir(AUDIOBOOKS_DIR):
            filepath = os.path.join(AUDIOBOOKS_DIR, filename)
            if os.path.isfile(filepath) and filename.lower().endswith((".m4b", ".mp3")):
                name_without_ext = os.path.splitext(filename)[0]
                if name_without_ext.lower() == base_name.lower():
                    return filepath
    except Exception:
        pass

    return None

    # Load mapping
    mapping = load_audiobook_mapping()

    # Try mapping by book_id (folder name)
    if book_id in mapping:
        audiobook_path = os.path.join(AUDIOBOOKS_DIR, mapping[book_id])
        if os.path.exists(audiobook_path):
            return audiobook_path

    # Try mapping by book title (if book is loaded)
    if book and book.metadata.title in mapping:
        audiobook_path = os.path.join(AUDIOBOOKS_DIR, mapping[book.metadata.title])
        if os.path.exists(audiobook_path):
            return audiobook_path

    # Fallback: filename/folder matching
    # book_id is like "BookName_data", so we strip "_data" to get base name
    base_name = book_id.replace("_data", "") if book_id.endswith("_data") else book_id

    # Get book title if available
    book_title = book.metadata.title if book else None

    # Try to find a matching folder containing .m4b files
    try:
        for item in os.listdir(AUDIOBOOKS_DIR):
            item_path = os.path.join(AUDIOBOOKS_DIR, item)
            if os.path.isdir(item_path):
                # Check if folder name matches book name or title (case-insensitive, fuzzy)
                item_lower = item.lower()
                base_lower = base_name.lower()
                title_lower = book_title.lower() if book_title else ""

                # Check for partial match (folder contains book name or vice versa)
                if (
                    base_lower in item_lower
                    or item_lower in base_lower
                    or (
                        title_lower
                        and (title_lower in item_lower or item_lower in title_lower)
                    )
                ):
                    # Check if this folder contains .m4b or .mp3 files
                    audio_files = sorted(
                        [
                            f
                            for f in os.listdir(item_path)
                            if f.lower().endswith((".m4b", ".mp3"))
                        ]
                    )
                    if audio_files:
                        # Return path to the folder (we'll handle multiple files in streaming)
                        return item_path
    except Exception as e:
        print(f"Error searching audiobook folders: {e}")

    # Try exact file match (single .m4b or .mp3 file in root, prioritize .m4b)
    for ext in [".m4b", ".M4B", ".mp3", ".MP3"]:
        audiobook_path = os.path.join(AUDIOBOOKS_DIR, base_name + ext)
        if os.path.exists(audiobook_path):
            return audiobook_path

    # Try case-insensitive file match
    try:
        for filename in os.listdir(AUDIOBOOKS_DIR):
            filepath = os.path.join(AUDIOBOOKS_DIR, filename)
            if os.path.isfile(filepath) and filename.lower().endswith((".m4b", ".mp3")):
                name_without_ext = os.path.splitext(filename)[0]
                if name_without_ext.lower() == base_name.lower():
                    return filepath
    except Exception:
        pass

    return None


def get_audiobook_files(audiobook_path: str) -> List[str]:
    """
    Get list of .m4b and .mp3 files for an audiobook.
    If audiobook_path is a directory, returns sorted list of all audio files inside.
    Prioritizes .m4b files over .mp3 files.
    If audiobook_path is a file, returns a list with just that file.
    """
    if os.path.isfile(audiobook_path):
        return [audiobook_path]

    if os.path.isdir(audiobook_path):
        # Get .m4b files first (priority), then .mp3 files
        m4b_files = sorted(
            [
                os.path.join(audiobook_path, f)
                for f in os.listdir(audiobook_path)
                if f.lower().endswith(".m4b")
            ]
        )
        mp3_files = sorted(
            [
                os.path.join(audiobook_path, f)
                for f in os.listdir(audiobook_path)
                if f.lower().endswith(".mp3")
            ]
        )
        # Return .m4b files first, then .mp3 files
        return m4b_files + mp3_files

    return []


def load_audiobook_position(book_id: str) -> Dict[str, Any]:
    """Load saved audiobook playback position for a book."""
    position_file = os.path.join(BOOKS_DIR, book_id, "audiobook_state.json")
    if not os.path.exists(position_file):
        return {"position": 0, "duration": 0, "chapter_index": 0}
    try:
        with open(position_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Ensure chapter_index exists for backwards compatibility
            if "chapter_index" not in data:
                data["chapter_index"] = 0
            return data
    except Exception as e:
        print(f"Error loading audiobook position for {book_id}: {e}")
        return {"position": 0, "duration": 0, "chapter_index": 0}


def save_audiobook_position(
    book_id: str, position: float, duration: float = 0, chapter_index: int = 0
) -> bool:
    """Save audiobook playback position for a book."""
    position_file = os.path.join(BOOKS_DIR, book_id, "audiobook_state.json")
    try:
        data = {
            "position": position,
            "duration": duration,
            "chapter_index": chapter_index,
            "last_updated": datetime.now().isoformat(),
        }
        with open(position_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving audiobook position for {book_id}: {e}")
        return False


@app.get("/", response_class=HTMLResponse)
async def library_view(request: Request):
    """Lists all available processed books."""
    books = []

    # Scan directory for folders ending in '_data' that have a book.pkl
    if os.path.exists(BOOKS_DIR):
        for item in os.listdir(BOOKS_DIR):
            if item.endswith("_data") and os.path.isdir(item):
                # Try to load it to get the title
                book = load_book_cached(item)
                if book:
                    books.append(
                        {
                            "id": item,
                            "title": book.metadata.title,
                            "author": ", ".join(book.metadata.authors),
                            "chapters": len(book.spine),
                        }
                    )

    return templates.TemplateResponse(
        "library.html", {"request": request, "books": books}
    )


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: str):
    """Delete a book's _data directory from the library."""
    safe_book_id = os.path.basename(book_id)

    # Validate that it's a _data directory
    if not safe_book_id.endswith("_data"):
        raise HTTPException(status_code=400, detail="Invalid book ID")

    book_path = os.path.join(BOOKS_DIR, safe_book_id)
    if not os.path.isdir(book_path):
        raise HTTPException(status_code=404, detail="Book not found")

    try:
        shutil.rmtree(book_path)
        # Clear the LRU cache so the deleted book doesn't linger
        load_book_cached.cache_clear()

        # Also delete the source epub/pdf from books/ directory
        # The _data dir name maps to the source file: e.g. "Sapiens_data" -> "books/Sapiens.epub"
        base_name = safe_book_id[: -len("_data")]  # strip _data suffix
        deleted_source = False
        for ext in (".epub",):
            source_path = os.path.join("books", base_name + ext)
            if os.path.exists(source_path):
                os.remove(source_path)
                deleted_source = True
                print(f"Deleted source file: {source_path}")

        return {
            "status": "success",
            "message": f"Book '{safe_book_id}' removed",
            "source_deleted": deleted_source,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete book: {str(e)}"
        )


@app.get("/read/{book_id}", response_class=HTMLResponse)
async def redirect_to_saved_chapter(request: Request, book_id: str):
    """Redirect to the last read chapter, or chapter 0 if no progress saved."""
    safe_book_id = os.path.basename(book_id)

    # Load saved progress
    progress = load_reading_progress(safe_book_id)
    chapter_index = progress.get("current_chapter_index", 0)

    # Validate chapter index against book spine length
    book = load_book_cached(safe_book_id)
    if book:
        chapter_index = max(0, min(chapter_index, len(book.spine) - 1))
    else:
        chapter_index = 0

    # Redirect to the saved chapter
    return RedirectResponse(url=f"/read/{book_id}/{chapter_index}", status_code=302)


@app.get("/read/{book_id}/{chapter_index}", response_class=HTMLResponse)
async def read_chapter(request: Request, book_id: str, chapter_index: int):
    """The main reader interface."""
    book = load_book_cached(book_id)
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")

    if chapter_index < 0 or chapter_index >= len(book.spine):
        raise HTTPException(status_code=404, detail="Chapter not found")

    current_chapter = book.spine[chapter_index]

    # Calculate Prev/Next links
    prev_idx = chapter_index - 1 if chapter_index > 0 else None
    next_idx = chapter_index + 1 if chapter_index < len(book.spine) - 1 else None

    return templates.TemplateResponse(
        "reader.html",
        {
            "request": request,
            "book": book,
            "current_chapter": current_chapter,
            "chapter_index": chapter_index,
            "book_id": book_id,
            "prev_idx": prev_idx,
            "next_idx": next_idx,
        },
    )


@app.get("/cover/{book_id}")
async def serve_cover(book_id: str):
    """Serves the cover image for a book."""
    safe_book_id = os.path.basename(book_id)

    # First, try to load the book and check if it has a cover_image path
    book = load_book_cached(safe_book_id)
    if book and hasattr(book, "cover_image") and book.cover_image:
        cover_path = os.path.join(BOOKS_DIR, safe_book_id, book.cover_image)
        if os.path.exists(cover_path):
            return FileResponse(cover_path)

    # Fallback: Try common cover image names
    images_dir = os.path.join(BOOKS_DIR, safe_book_id, "images")
    if os.path.exists(images_dir):
        # Try common cover patterns
        for filename in os.listdir(images_dir):
            if "cover" in filename.lower() and filename.lower().endswith(
                (".jpg", ".jpeg", ".png", ".gif")
            ):
                cover_path = os.path.join(images_dir, filename)
                return FileResponse(cover_path)

    # If no cover found, return 404
    raise HTTPException(status_code=404, detail="Cover image not found")


@app.get("/read/{book_id}/images/{image_name}")
async def serve_image(book_id: str, image_name: str):
    """
    Serves images specifically for a book.
    The HTML contains <img src="images/pic.jpg">.
    The browser resolves this to /read/{book_id}/images/pic.jpg.
    """
    # Security check: ensure book_id is clean
    safe_book_id = os.path.basename(book_id)
    safe_image_name = os.path.basename(image_name)

    img_path = os.path.join(BOOKS_DIR, safe_book_id, "images", safe_image_name)

    if not os.path.exists(img_path):
        raise HTTPException(status_code=404, detail="Image not found")

    return FileResponse(img_path)


# Highlight API endpoints
@app.post("/api/highlights")
async def create_highlight(request_data: HighlightRequest):
    """Create a new highlight."""
    try:
        highlights = load_highlights(request_data.book_id)
        chapter_key = str(request_data.highlight.chapter_index)

        if chapter_key not in highlights:
            highlights[chapter_key] = []

        # Add highlight to the chapter
        highlights[chapter_key].append(request_data.highlight.dict())

        # Save to file
        success = save_highlights(request_data.book_id, highlights)

        if success:
            return {"status": "success", "message": "Highlight saved"}
        else:
            raise HTTPException(status_code=500, detail="Failed to save highlight")

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error creating highlight: {str(e)}"
        )


@app.get("/api/highlights/{book_id}/{chapter_index}")
async def get_highlights(book_id: str, chapter_index: int):
    """Get all highlights for a specific chapter."""
    try:
        highlights = load_highlights(book_id)
        chapter_key = str(chapter_index)
        chapter_highlights = highlights.get(chapter_key, [])

        return {"highlights": chapter_highlights}

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error loading highlights: {str(e)}"
        )


@app.delete("/api/highlights")
async def remove_highlight(request_data: RemoveHighlightRequest):
    """Remove a specific highlight."""
    try:
        highlights = load_highlights(request_data.book_id)
        chapter_key = str(request_data.chapter_index)

        if chapter_key not in highlights:
            raise HTTPException(status_code=404, detail="Chapter highlights not found")

        # Find and remove the highlight with matching ID
        original_count = len(highlights[chapter_key])
        highlights[chapter_key] = [
            h
            for h in highlights[chapter_key]
            if h.get("id") != request_data.highlight_id
        ]

        if len(highlights[chapter_key]) == original_count:
            raise HTTPException(status_code=404, detail="Highlight not found")

        # Save to file
        success = save_highlights(request_data.book_id, highlights)

        if success:
            return {"status": "success", "message": "Highlight removed"}
        else:
            raise HTTPException(status_code=500, detail="Failed to remove highlight")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error removing highlight: {str(e)}"
        )


# --- Reading Progress API Endpoints ---


class ReadingProgressRequest(BaseModel):
    book_id: str
    chapter_index: int
    scroll_position: Optional[int] = 0
    scroll_percentage: Optional[float] = 0.0


@app.get("/api/progress/{book_id}")
async def get_reading_progress(book_id: str):
    """Get saved reading progress for a book."""
    safe_book_id = os.path.basename(book_id)
    progress = load_reading_progress(safe_book_id)
    return progress


@app.post("/api/progress")
async def save_progress_endpoint(request_data: ReadingProgressRequest):
    """Save reading progress for a book."""
    safe_book_id = os.path.basename(request_data.book_id)

    # Validate chapter index against book spine length
    book = load_book_cached(safe_book_id)
    if book:
        # Clamp chapter index to valid range
        chapter_index = max(0, min(request_data.chapter_index, len(book.spine) - 1))
    else:
        chapter_index = request_data.chapter_index

    success = save_reading_progress(
        safe_book_id,
        chapter_index,
        request_data.scroll_position or 0,
        request_data.scroll_percentage or 0.0,
    )

    if success:
        return {"status": "success", "chapter_index": chapter_index}
    else:
        raise HTTPException(status_code=500, detail="Failed to save reading progress")


# --- Audiobook API Endpoints ---


class AudioPositionRequest(BaseModel):
    position: float
    duration: float = 0
    chapter_index: int = 0


@app.get("/api/audio/{book_id}/metadata")
async def get_audiobook_metadata(book_id: str):
    """Check if audiobook exists for a book and return metadata."""
    safe_book_id = os.path.basename(book_id)
    book = load_book_cached(safe_book_id)

    audiobook_path = find_audiobook_for_book(safe_book_id, book)

    if not audiobook_path:
        return {"available": False}

    try:
        audio_files = get_audiobook_files(audiobook_path)
        if not audio_files:
            return {"available": False}

        # Calculate total size
        total_size = sum(os.path.getsize(f) for f in audio_files)

        # Build chapters list with file info
        chapters = []
        for i, filepath in enumerate(audio_files):
            filename = os.path.basename(filepath)
            chapters.append(
                {"index": i, "filename": filename, "size": os.path.getsize(filepath)}
            )

        return {
            "available": True,
            "is_multi_file": len(audio_files) > 1,
            "total_files": len(audio_files),
            "total_size": total_size,
            "chapters": chapters,
        }
    except Exception as e:
        print(f"Error getting audiobook metadata: {e}")
        return {"available": False}


@app.get("/api/audio/{book_id}/position")
async def get_audiobook_position(book_id: str):
    """Get saved playback position for a book's audiobook."""
    safe_book_id = os.path.basename(book_id)
    position_data = load_audiobook_position(safe_book_id)
    return position_data


@app.post("/api/audio/{book_id}/position")
async def save_audiobook_position_endpoint(
    book_id: str, request_data: AudioPositionRequest
):
    """Save playback position for a book's audiobook."""
    safe_book_id = os.path.basename(book_id)

    success = save_audiobook_position(
        safe_book_id,
        request_data.position,
        request_data.duration,
        request_data.chapter_index,
    )

    if success:
        return {"status": "success"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save position")


@app.get("/api/audio/{book_id}/{chapter_idx}")
async def stream_audiobook_chapter(book_id: str, chapter_idx: int, request: Request):
    """
    Stream a specific audiobook chapter file.
    For multi-file audiobooks, chapter_idx selects which file to play.
    """
    safe_book_id = os.path.basename(book_id)
    book = load_book_cached(safe_book_id)

    audiobook_path = find_audiobook_for_book(safe_book_id, book)

    if not audiobook_path:
        raise HTTPException(status_code=404, detail="Audiobook not found")

    audio_files = get_audiobook_files(audiobook_path)

    if not audio_files:
        raise HTTPException(status_code=404, detail="No audio files found")

    if chapter_idx < 0 or chapter_idx >= len(audio_files):
        raise HTTPException(status_code=404, detail="Audio chapter not found")

    file_path = audio_files[chapter_idx]
    return await _stream_audio_file(file_path, request)


@app.get("/api/audio/{book_id}")
async def stream_audiobook(book_id: str, request: Request):
    """
    Stream audiobook file with HTTP Range support for seeking.
    For multi-file audiobooks, this streams the first file.
    Use /api/audio/{book_id}/{chapter_idx} for specific chapters.
    """
    safe_book_id = os.path.basename(book_id)
    book = load_book_cached(safe_book_id)

    audiobook_path = find_audiobook_for_book(safe_book_id, book)

    if not audiobook_path:
        raise HTTPException(status_code=404, detail="Audiobook not found")

    audio_files = get_audiobook_files(audiobook_path)

    if not audio_files:
        raise HTTPException(status_code=404, detail="No audio files found")

    # Stream the first file (for single-file audiobooks or default)
    return await _stream_audio_file(audio_files[0], request)


async def _stream_audio_file(file_path: str, request: Request):
    """Helper to stream an audio file with HTTP Range support."""
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio file not found")

    # Determine content type based on file extension
    if file_path.lower().endswith(".mp3"):
        content_type = "audio/mpeg"
    else:
        content_type = "audio/mp4"  # Default for .m4b files

    file_size = os.path.getsize(file_path)

    # Parse Range header
    range_header = request.headers.get("range")

    if range_header:
        # Parse byte range (e.g., "bytes=0-1023")
        try:
            range_spec = range_header.replace("bytes=", "")
            if "-" in range_spec:
                parts = range_spec.split("-")
                start = int(parts[0]) if parts[0] else 0
                end = int(parts[1]) if parts[1] else file_size - 1
            else:
                start = int(range_spec)
                end = file_size - 1

            # Clamp values
            start = max(0, start)
            end = min(end, file_size - 1)
            content_length = end - start + 1

            def iter_file():
                with open(file_path, "rb") as f:
                    f.seek(start)
                    remaining = content_length
                    chunk_size = 64 * 1024  # 64KB chunks
                    while remaining > 0:
                        read_size = min(chunk_size, remaining)
                        data = f.read(read_size)
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            return StreamingResponse(
                iter_file(),
                status_code=206,
                headers={
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(content_length),
                    "Content-Type": content_type,
                },
                media_type=content_type,
            )
        except Exception as e:
            print(f"Error parsing range header: {e}")
            # Fall through to full file response

    # No range header - return full file
    def iter_full_file():
        with open(file_path, "rb") as f:
            chunk_size = 64 * 1024
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                yield data

    return StreamingResponse(
        iter_full_file(),
        headers={
            "Accept-Ranges": "bytes",
            "Content-Length": str(file_size),
            "Content-Type": content_type,
        },
        media_type=content_type,
    )


if __name__ == "__main__":
    import uvicorn

    # Auto-process any new EPUB files in the books/ folder
    print("Checking for new books to process...")
    processed, skipped = auto_process_books_folder(books_folder="books", output_dir=".")

    if processed > 0:
        print(f"Processed {processed} new book(s)")
    if skipped > 0:
        print(f"Skipped {skipped} already processed book(s)")

    print("\nStarting server at http://127.0.0.1:8123")
    uvicorn.run(app, host="0.0.0.0", port=8123)
