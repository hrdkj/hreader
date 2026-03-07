"""
Parses an EPUB file into a structured object that can be used to serve the book via a web interface.
"""

import json
import os
import pickle
import re
import shutil
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from datetime import datetime
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Comment

# --- Obsidian Integration Config ---
OBSIDIAN_BOOKS_PATH = "/home/hrdk/gen/Notes/books"
OBSIDIAN_IMAGES_PATH = "/home/hrdk/gen/Notes/Images"

# --- Data structures ---


@dataclass
class ChapterContent:
    """
    Represents a physical file in the EPUB (Spine Item).
    A single file might contain multiple logical chapters (TOC entries).
    """

    id: str  # Internal ID (e.g., 'item_1')
    href: str  # Filename (e.g., 'part01.html')
    title: str  # Best guess title from file
    content: str  # Cleaned HTML with rewritten image paths
    text: str  # Plain text for search/LLM context
    order: int  # Linear reading order


@dataclass
class TOCEntry:
    """Represents a logical entry in the navigation sidebar."""

    title: str
    href: str  # original href (e.g., 'part01.html#chapter1')
    file_href: str  # just the filename (e.g., 'part01.html')
    anchor: str  # just the anchor (e.g., 'chapter1'), empty if none
    children: List["TOCEntry"] = field(default_factory=list)


@dataclass
class BookMetadata:
    """Metadata"""

    title: str
    language: str
    authors: List[str] = field(default_factory=list)
    description: Optional[str] = None
    publisher: Optional[str] = None
    date: Optional[str] = None
    identifiers: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)


@dataclass
class Book:
    """The Master Object to be pickled."""

    metadata: BookMetadata
    spine: List[ChapterContent]  # The actual content (linear files)
    toc: List[TOCEntry]  # The navigation tree
    images: Dict[str, str]  # Map: original_path -> local_path
    source_file: str
    processed_at: str
    version: str = "3.0"
    cover_image: Optional[str] = None  # Relative path to cover image
    audiobook_path: Optional[str] = None  # Path to associated audiobook (.m4b)


# --- Utilities ---


def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:
    # Remove dangerous/useless tags
    for tag in soup(["script", "style", "iframe", "video", "nav", "form", "button"]):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Remove input tags
    for tag in soup.find_all("input"):
        tag.decompose()

    return soup


def extract_plain_text(soup: BeautifulSoup) -> str:
    """Extract clean text for LLM/Search usage."""
    text = soup.get_text(separator=" ")
    # Collapse whitespace
    return " ".join(text.split())


def parse_toc_recursive(toc_list, depth=0) -> List[TOCEntry]:
    """
    Recursively parses the TOC structure from ebooklib.
    """
    result = []

    # Handle case where toc_list is a single Link object instead of a list
    if isinstance(toc_list, epub.Link):
        toc_list = [toc_list]
    elif not isinstance(toc_list, (list, tuple)):
        return result

    for item in toc_list:
        # ebooklib TOC items are either `Link` objects or tuples (Section, [Children])
        if isinstance(item, tuple):
            section, children = item
            # Skip entries with empty href
            if not section.href:
                continue
            entry = TOCEntry(
                title=section.title or "",
                href=section.href,
                file_href=section.href.split("#")[0],
                anchor=section.href.split("#")[1] if "#" in section.href else "",
                children=parse_toc_recursive(children, depth + 1),
            )
            result.append(entry)
        elif isinstance(item, epub.Link):
            # Skip entries with empty href
            if not item.href:
                continue
            entry = TOCEntry(
                title=item.title or "",
                href=item.href,
                file_href=item.href.split("#")[0],
                anchor=item.href.split("#")[1] if "#" in item.href else "",
            )
            result.append(entry)
        # Note: ebooklib sometimes returns direct Section objects without children
        elif isinstance(item, epub.Section):
            # Skip entries with empty href
            if not item.href:
                continue
            entry = TOCEntry(
                title=item.title or "",
                href=item.href,
                file_href=item.href.split("#")[0],
                anchor=item.href.split("#")[1] if "#" in item.href else "",
            )
            result.append(entry)

    return result


def get_fallback_toc(book_obj) -> List[TOCEntry]:
    """
    If TOC is missing, build a flat one from the Spine.
    """
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            # Try to guess a title from the content or ID
            title = (
                item.get_name()
                .replace(".html", "")
                .replace(".xhtml", "")
                .replace("_", " ")
                .title()
            )
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def extract_metadata_robust(book_obj) -> BookMetadata:
    """
    Extracts metadata handling both single and list values.
    """

    def get_list(key):
        data = book_obj.get_metadata("DC", key)
        return [x[0] for x in data] if data else []

    def get_one(key):
        data = book_obj.get_metadata("DC", key)
        return data[0][0] if data else None

    return BookMetadata(
        title=get_one("title") or "Untitled",
        language=get_one("language") or "en",
        authors=get_list("creator"),
        description=get_one("description"),
        publisher=get_one("publisher"),
        date=get_one("date"),
        identifiers=get_list("identifier"),
        subjects=get_list("subject"),
    )


def detect_cover_image(book_obj, image_map: Dict[str, str]) -> Optional[str]:
    """
    Tries to detect the cover image from the EPUB.
    Returns the relative path to the cover image or None.
    """
    # Method 1: Check for cover metadata
    cover_meta = book_obj.get_metadata("OPF", "cover")
    if cover_meta:
        cover_id = cover_meta[0][1].get("content") if cover_meta[0][1] else None
        if cover_id:
            cover_item = book_obj.get_item_with_id(cover_id)
            if cover_item:
                cover_name = cover_item.get_name()
                if cover_name in image_map:
                    return image_map[cover_name]

    # Method 2: Look for images with 'cover' in the name
    for original_path, local_path in image_map.items():
        if "cover" in original_path.lower():
            return local_path

    # Method 3: Try to find the first image in the first chapter
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            content = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(content, "html.parser")
            first_img = soup.find("img")
            if first_img:
                src = first_img.get("src", "")
                if src:
                    src_decoded = unquote(str(src))
                    filename = os.path.basename(src_decoded)
                    if src_decoded in image_map:
                        return image_map[src_decoded]
                    elif filename in image_map:
                        return image_map[filename]
            # Only check first document
            break

    return None


# --- Main Conversion Logic ---


def process_epub(epub_path: str, output_dir: str) -> Book:
    # 1. Load Book
    print(f"Loading {epub_path}...")
    book = epub.read_epub(epub_path)

    # 2. Extract Metadata
    metadata = extract_metadata_robust(book)

    # 3. Prepare Output Directories
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    # 4. Extract Images & Build Map
    print("Extracting images...")
    image_map = {}  # Key: internal_path, Value: local_relative_path

    for item in book.get_items():
        if item.get_type() == ebooklib.ITEM_IMAGE:
            # Normalize filename
            original_fname = os.path.basename(item.get_name())
            # Sanitize filename for OS
            safe_fname = "".join(
                [c for c in original_fname if c.isalpha() or c.isdigit() or c in "._-"]
            ).strip()

            # Save to disk
            local_path = os.path.join(images_dir, safe_fname)
            with open(local_path, "wb") as f:
                f.write(item.get_content())

            # Map keys: We try both the full internal path and just the basename
            # to be robust against messy HTML src attributes
            rel_path = f"images/{safe_fname}"
            image_map[item.get_name()] = rel_path
            image_map[original_fname] = rel_path

    # 5. Process TOC
    print("Parsing Table of Contents...")
    toc_structure = parse_toc_recursive(book.toc)
    if not toc_structure:
        print("Warning: Empty TOC, building fallback from Spine...")
        toc_structure = get_fallback_toc(book)

    # 6. Process Content (Spine-based to preserve HTML validity)
    print("Processing chapters...")
    spine_chapters = []

    # We iterate over the spine (linear reading order)
    for i, spine_item in enumerate(book.spine):
        item_id, linear = spine_item
        item = book.get_item_with_id(item_id)

        if not item:
            continue

        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            # Raw content
            raw_content = item.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(raw_content, "html.parser")

            # A. Fix Images
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if not src:
                    continue

                # Decode URL (part01/image%201.jpg -> part01/image 1.jpg)
                src_decoded = unquote(str(src))
                filename = os.path.basename(src_decoded)

                # Try to find in map
                if src_decoded in image_map:
                    img["src"] = image_map[src_decoded]
                elif filename in image_map:
                    img["src"] = image_map[filename]

            # B. Clean HTML
            soup = clean_html_content(soup)

            # C. Extract Body Content only
            body = soup.find("body")
            if body:
                # Extract inner HTML of body
                final_html = "".join([str(x) for x in body.contents])
            else:
                final_html = str(soup)

            # D. Create Object
            chapter = ChapterContent(
                id=item_id,
                href=item.get_name(),  # Important: This links TOC to Content
                title=f"Section {i + 1}",  # Fallback, real titles come from TOC
                content=final_html,
                text=extract_plain_text(soup),
                order=i,
            )
            spine_chapters.append(chapter)

    # 7. Detect Cover Image
    print("Detecting cover image...")
    cover_image = detect_cover_image(book, image_map)
    if cover_image:
        print(f"Found cover image: {cover_image}")
    else:
        print("No cover image detected")

    # 8. Final Assembly
    final_book = Book(
        metadata=metadata,
        spine=spine_chapters,
        toc=toc_structure,
        images=image_map,
        source_file=os.path.basename(epub_path),
        processed_at=datetime.now().isoformat(),
        cover_image=cover_image,
    )

    return final_book


def save_to_pickle(book: Book, output_dir: str):
    p_path = os.path.join(output_dir, "book.pkl")
    with open(p_path, "wb") as f:
        pickle.dump(book, f)
    print(f"Saved structured data to {p_path}")
    # Also export JSON for offline Android reader
    export_to_json(book, output_dir)


def export_to_json(book: Book, output_dir: str):
    """
    Export a Book object to book.json for use by the offline Android reader.
    The JSON contains all the data needed to render the book without a server.
    """

    def toc_to_dict(entries: List[TOCEntry]) -> List[Dict]:
        result = []
        for entry in entries:
            d = {
                "title": entry.title,
                "href": entry.href,
                "file_href": entry.file_href,
                "anchor": entry.anchor,
            }
            if entry.children:
                d["children"] = toc_to_dict(entry.children)
            else:
                d["children"] = []
            result.append(d)
        return result

    data = {
        "metadata": {
            "title": book.metadata.title,
            "language": book.metadata.language,
            "authors": book.metadata.authors,
            "description": book.metadata.description,
            "publisher": book.metadata.publisher,
            "date": book.metadata.date,
            "subjects": book.metadata.subjects,
        },
        "spine": [
            {
                "id": ch.id,
                "href": ch.href,
                "title": ch.title,
                "content": ch.content,
                "order": ch.order,
            }
            for ch in book.spine
        ],
        "toc": toc_to_dict(book.toc),
        "cover_image": book.cover_image,
        "source_file": book.source_file,
        "processed_at": book.processed_at,
        "version": book.version,
    }

    json_path = os.path.join(output_dir, "book.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"Saved JSON data to {json_path}")


def export_all_to_json(directory: str = ".") -> tuple[int, int]:
    """
    Export all processed books to JSON format for the offline Android reader.
    Reads book.pkl from each _data folder and writes book.json.

    Returns:
        Tuple of (exported_count, skipped_count)
    """
    exported = 0
    skipped = 0

    data_folders = [
        f
        for f in os.listdir(directory)
        if f.endswith("_data") and os.path.isdir(os.path.join(directory, f))
    ]

    if not data_folders:
        print("No processed books found (no *_data folders)")
        return 0, 0

    print(f"Found {len(data_folders)} processed book(s)")

    for folder in sorted(data_folders):
        folder_path = os.path.join(directory, folder)
        pkl_path = os.path.join(folder_path, "book.pkl")
        json_path = os.path.join(folder_path, "book.json")

        if not os.path.exists(pkl_path):
            print(f"Skipping (no book.pkl): {folder}")
            skipped += 1
            continue

        # Skip if JSON already exists and is newer than pkl
        if os.path.exists(json_path):
            pkl_mtime = os.path.getmtime(pkl_path)
            json_mtime = os.path.getmtime(json_path)
            if json_mtime >= pkl_mtime:
                print(f"Skipping (book.json up to date): {folder}")
                skipped += 1
                continue

        print(f"Exporting to JSON: {folder}")

        try:
            with open(pkl_path, "rb") as f:
                book = pickle.load(f)
            export_to_json(book, folder_path)
            exported += 1
        except Exception as e:
            print(f"Error exporting {folder}: {e}")
            skipped += 1

    return exported, skipped


# --- PDF Processing ---


def process_pdf(pdf_path: str, output_dir: str) -> Book:
    """
    Process a PDF file into the same Book structure used for EPUBs.
    Extracts text page-by-page and creates chapters.
    """
    import fitz  # PyMuPDF

    print(f"Loading PDF: {pdf_path}...")
    doc = fitz.open(pdf_path)

    # 1. Extract Metadata
    pdf_meta = doc.metadata or {}
    title = pdf_meta.get("title") or os.path.splitext(os.path.basename(pdf_path))[0]
    author = pdf_meta.get("author") or ""
    authors = [a.strip() for a in author.split(",")] if author else []

    metadata = BookMetadata(
        title=title,
        language="en",  # PDF metadata often lacks language
        authors=authors,
        description=pdf_meta.get("subject"),
        publisher=pdf_meta.get("creator"),  # Often the PDF creator tool
        date=pdf_meta.get("creationDate"),
    )

    # 2. Prepare Output Directories
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    # 3. Extract cover image (first page rendered as image)
    print("Extracting cover image...")
    cover_image = None
    if doc.page_count > 0:
        first_page = doc[0]
        # Render at 2x resolution for quality
        mat = fitz.Matrix(2, 2)
        pix = first_page.get_pixmap(matrix=mat)
        cover_filename = "cover.png"
        cover_path = os.path.join(images_dir, cover_filename)
        pix.save(cover_path)
        cover_image = f"images/{cover_filename}"
        print(f"Saved cover image: {cover_path}")

    # 4. Extract text and build chapters
    # Strategy: Group pages into chapters based on TOC or fixed page count
    print("Extracting text from PDF...")

    # Try to get TOC from PDF
    pdf_toc = doc.get_toc()  # Returns list of [level, title, page_number]

    spine_chapters = []
    toc_structure = []
    image_map = {}

    if pdf_toc and len(pdf_toc) > 0:
        # PDF has a table of contents - use it to create chapters
        print(f"Found PDF TOC with {len(pdf_toc)} entries")

        # Build chapter ranges from TOC
        chapter_ranges = []
        for i, (level, title, page_num) in enumerate(pdf_toc):
            if level == 1:  # Only use top-level entries as chapters
                start_page = page_num - 1  # TOC uses 1-indexed pages
                # Find end page (start of next chapter or end of doc)
                end_page = doc.page_count
                for j in range(i + 1, len(pdf_toc)):
                    if pdf_toc[j][0] == 1:  # Next top-level entry
                        end_page = pdf_toc[j][2] - 1
                        break
                chapter_ranges.append((title, start_page, end_page))

        # If no top-level entries, fall back to all entries
        if not chapter_ranges:
            for i, (level, title, page_num) in enumerate(pdf_toc):
                start_page = page_num - 1
                end_page = doc.page_count
                for j in range(i + 1, len(pdf_toc)):
                    end_page = pdf_toc[j][2] - 1
                    break
                chapter_ranges.append((title, start_page, end_page))

        # Extract text for each chapter
        for order, (title, start_page, end_page) in enumerate(chapter_ranges):
            chapter_text = []
            for page_num in range(start_page, min(end_page, doc.page_count)):
                page = doc[page_num]
                # Use structured extraction for better results
                text = extract_page_text_structured(page)
                if text.strip():
                    chapter_text.append(text)

            combined_text = "\n\n".join(chapter_text)
            if not combined_text.strip():
                continue

            # Convert plain text to simple HTML paragraphs
            html_content = text_to_html(combined_text, chapter_title=title)

            chapter = ChapterContent(
                id=f"chapter_{order}",
                href=f"chapter_{order}.html",
                title=title,
                content=html_content,
                text=combined_text,
                order=order,
            )
            spine_chapters.append(chapter)

            toc_entry = TOCEntry(
                title=title,
                href=f"chapter_{order}.html",
                file_href=f"chapter_{order}.html",
                anchor="",
            )
            toc_structure.append(toc_entry)

    else:
        # No TOC - create chapters by grouping pages
        print("No TOC found, grouping pages into chapters...")
        PAGES_PER_CHAPTER = 10

        total_pages = doc.page_count
        num_chapters = (total_pages + PAGES_PER_CHAPTER - 1) // PAGES_PER_CHAPTER

        for chapter_idx in range(num_chapters):
            start_page = chapter_idx * PAGES_PER_CHAPTER
            end_page = min(start_page + PAGES_PER_CHAPTER, total_pages)

            chapter_text = []
            for page_num in range(start_page, end_page):
                page = doc[page_num]
                # Use structured extraction for better results
                text = extract_page_text_structured(page)
                if text.strip():
                    chapter_text.append(text)

            combined_text = "\n\n".join(chapter_text)
            if not combined_text.strip():
                continue

            title = f"Pages {start_page + 1}-{end_page}"

            # Convert plain text to simple HTML paragraphs
            html_content = text_to_html(combined_text, chapter_title=title)

            chapter = ChapterContent(
                id=f"chapter_{chapter_idx}",
                href=f"chapter_{chapter_idx}.html",
                title=title,
                content=html_content,
                text=combined_text,
                order=chapter_idx,
            )
            spine_chapters.append(chapter)

            toc_entry = TOCEntry(
                title=title,
                href=f"chapter_{chapter_idx}.html",
                file_href=f"chapter_{chapter_idx}.html",
                anchor="",
            )
            toc_structure.append(toc_entry)

    doc.close()

    print(f"Created {len(spine_chapters)} chapters from PDF")

    # 5. Final Assembly
    final_book = Book(
        metadata=metadata,
        spine=spine_chapters,
        toc=toc_structure,
        images=image_map,
        source_file=os.path.basename(pdf_path),
        processed_at=datetime.now().isoformat(),
        cover_image=cover_image,
    )

    return final_book


def extract_page_text_structured(page) -> str:
    """
    Extract text from a PDF page using block-level information
    to better preserve structure and identify headings.
    Handles drop caps by detecting single large letters and prepending to following text.
    """
    blocks = page.get_text("dict")["blocks"]

    text_parts = []
    prev_block_bottom = 0
    pending_drop_cap = None  # Store drop cap letter to prepend to next body text block

    for block in blocks:
        if block["type"] != 0:  # Skip non-text blocks (images)
            continue

        # Get block position
        block_top = block["bbox"][1]

        # Add extra newline if there's a significant gap (new section)
        if prev_block_bottom > 0 and (block_top - prev_block_bottom) > 30:
            text_parts.append("\n")

        # Process lines in the block
        block_lines = []
        block_max_font_size = 0
        for line in block.get("lines", []):
            line_text = ""
            for span in line.get("spans", []):
                span_text = span.get("text", "")
                line_text += span_text
                # Track max font size in this block
                font_size = span.get("size", 0)
                if font_size > block_max_font_size:
                    block_max_font_size = font_size

            line_text = line_text.strip()
            if line_text:
                block_lines.append(line_text)

        if block_lines:
            # Join lines in the same block with spaces
            block_text = " ".join(block_lines)

            # Check if this is a drop cap: single uppercase letter in large font (>30pt)
            if (
                len(block_text) == 1
                and block_text.isupper()
                and block_max_font_size > 30
            ):
                pending_drop_cap = block_text
            else:
                # If we have a pending drop cap, try to prepend it
                if pending_drop_cap:
                    # Only prepend to body text:
                    # - starts with lowercase
                    # - not a quote (doesn't start with " and doesn't end with ")
                    # - is substantial (multiple lines OR long text)
                    is_quote = (
                        block_text.startswith('"')
                        or block_text.endswith('"')
                        or block_text.startswith("\u201c")  # left curly quote "
                        or block_text.endswith("\u201d")  # right curly quote "
                    )
                    is_body_text = (
                        block_text
                        and block_text[0].islower()
                        and not is_quote
                        and (len(block_lines) > 1 or len(block_text) > 100)
                    )
                    if is_body_text:
                        block_text = pending_drop_cap + block_text
                        pending_drop_cap = None
                    # Keep drop cap pending if this wasn't body text

                text_parts.append(block_text)

        prev_block_bottom = block["bbox"][3]

    return "\n\n".join(text_parts)


def text_to_html(text: str, chapter_title: str = "") -> str:
    """
    Convert plain text to HTML with proper paragraph structure.
    Handles common text patterns from PDF extraction.
    """
    import html

    # Clean up common PDF artifacts first (before escaping)
    text = clean_pdf_text(text, chapter_title)

    # Escape HTML entities
    text = html.escape(text)

    # Split into paragraphs (double newline or significant whitespace)
    paragraphs = re.split(r"\n\s*\n", text)

    # First pass: merge quote continuations
    # Pattern: paragraph starts with " but doesn't end with " -> merge with next until we find closing "
    merged_paragraphs = []
    quote_buffer = None

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Replace single newlines with spaces (PDF often wraps lines)
        para = re.sub(r"\n", " ", para)
        # Collapse multiple spaces
        para = re.sub(r"  +", " ", para)

        if not para or len(para) < 3:
            continue

        # Check quote state
        starts_quote = para.startswith('"') or para.startswith("\u201c")
        ends_quote = para.endswith('"') or para.endswith("\u201d")

        if quote_buffer is not None:
            # We're in a quote continuation - append to buffer
            quote_buffer += " " + para
            if ends_quote:
                # Quote complete - add to merged paragraphs
                merged_paragraphs.append(quote_buffer)
                quote_buffer = None
        elif starts_quote and not ends_quote:
            # Start of a multi-paragraph quote
            quote_buffer = para
        else:
            # Normal paragraph
            merged_paragraphs.append(para)

    # If quote never closed, add what we have
    if quote_buffer is not None:
        merged_paragraphs.append(quote_buffer)

    # Second pass: convert to HTML
    html_parts = []
    for para in merged_paragraphs:
        # Skip very short paragraphs that are likely artifacts
        if len(para) < 3:
            continue

        # Detect and format section headings (numbered: "01 Title Here")
        heading_match = re.match(r"^(\d{1,2})\s+([A-Z][^.!?]*[?!]?)$", para)
        if heading_match and len(para) < 100:
            num, title = heading_match.groups()
            html_parts.append(f"<h3>{num}. {title}</h3>")
            continue

        # Detect epigraphs/quotes (text in quotes, usually short)
        if (
            (para.startswith('"') or para.startswith("\u201c"))
            and (para.endswith('"') or para.endswith("\u201d"))
            and len(para) < 500
        ):
            html_parts.append(f"<blockquote><p>{para}</p></blockquote>")
            continue

        html_parts.append(f"<p>{para}</p>")

    return "\n".join(html_parts)


def clean_pdf_text(text: str, chapter_title: str = "") -> str:
    """
    Clean up common PDF text extraction artifacts.
    Note: Drop cap handling is done in extract_page_text_structured() during extraction.
    """
    # Remove common watermarks/footers
    watermarks = [
        r"OceanofPDF\.com",
        r"oceanofpdf\.com",
        r"www\.oceanofpdf\.com",
    ]
    for wm in watermarks:
        text = re.sub(wm, "", text, flags=re.IGNORECASE)

    # Fix lines that are just chapter numbers "01" or "1" alone
    text = re.sub(r"^\d{1,2}\s*$", "", text, flags=re.MULTILINE)

    # Merge split quotes: lines that start with lowercase and end previous line had opening quote
    # This handles: '"First part of quote' + 'second part."'
    lines = text.split("\n")
    merged_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # Check if this line continues a quote from previous line
        if (
            i > 0
            and merged_lines
            and merged_lines[-1].strip().startswith('"')
            and not merged_lines[-1].strip().endswith('"')
            and line.strip()
            and (line.strip()[0].islower() or line.strip().endswith('"'))
        ):
            # Merge with previous line
            merged_lines[-1] = merged_lines[-1].rstrip() + " " + line.strip()
        else:
            merged_lines.append(line)
        i += 1

    text = "\n".join(merged_lines)

    # Remove the chapter title if it appears at the very start (redundant with TOC)
    if chapter_title:
        # Escape special regex chars in title
        escaped_title = re.escape(chapter_title)
        # Remove title at start (case insensitive), possibly with "OceanofPDF" after
        text = re.sub(
            rf"^\s*{escaped_title}\s*\n?", "", text, count=1, flags=re.IGNORECASE
        )

    # Clean up excessive whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    return text


def sanitize_filename(filename: str) -> str:
    """
    Sanitize a filename by removing or replacing invalid characters.
    Suitable for creating safe filenames from book titles.
    """
    # Remove or replace characters that are problematic in filenames
    # Replace common punctuation with underscores or remove them
    sanitized = re.sub(r'[<>:"/\\|?*]', "", filename)  # Remove invalid chars
    sanitized = re.sub(r"[\s]+", "_", sanitized)  # Replace spaces with underscores
    sanitized = re.sub(r"_+", "_", sanitized)  # Collapse multiple underscores
    sanitized = sanitized.strip("_")  # Remove leading/trailing underscores
    return sanitized


def find_cover_image(book_data_dir: str, book: Book) -> Optional[str]:
    """
    Find the cover image path. Tries book.cover_image first,
    then falls back to looking for cover.* in images folder.
    Returns the full path to the cover image or None.
    """
    images_dir = os.path.join(book_data_dir, "images")

    # Try the cover_image attribute first
    if book.cover_image:
        cover_path = os.path.join(book_data_dir, book.cover_image)
        if os.path.exists(cover_path):
            return cover_path

    # Fallback: look for cover.* in images folder
    if os.path.exists(images_dir):
        for fname in os.listdir(images_dir):
            if fname.lower().startswith("cover."):
                return os.path.join(images_dir, fname)

    return None


def export_to_obsidian(book_data_dir: str) -> bool:
    """
    Export a processed book to Obsidian vault.

    Args:
        book_data_dir: Path to the book's data directory (e.g., 'naval_data')

    Returns:
        True if export was successful, False otherwise.
    """
    # Load the book
    pkl_path = os.path.join(book_data_dir, "book.pkl")
    if not os.path.exists(pkl_path):
        print(f"Error: {pkl_path} not found")
        return False

    with open(pkl_path, "rb") as f:
        book = pickle.load(f)

    # Generate sanitized title for filenames
    title_sanitized = sanitize_filename(book.metadata.title)

    # Check if note already exists
    note_filename = f"{title_sanitized}.md"
    note_path = os.path.join(OBSIDIAN_BOOKS_PATH, note_filename)

    if os.path.exists(note_path):
        print(f"Skipping: Note already exists at {note_path}")
        return False

    # Find and copy cover image
    cover_image_name = None
    cover_source = find_cover_image(book_data_dir, book)

    if cover_source:
        # Determine extension
        _, ext = os.path.splitext(cover_source)
        cover_image_name = f"{title_sanitized}_cover{ext}"
        cover_dest = os.path.join(OBSIDIAN_IMAGES_PATH, cover_image_name)

        # Copy if doesn't exist
        if not os.path.exists(cover_dest):
            shutil.copy2(cover_source, cover_dest)
            print(f"Copied cover image to {cover_dest}")
        else:
            print(f"Cover image already exists at {cover_dest}")
    else:
        print("Warning: No cover image found for this book")

    # Prepare metadata
    title = book.metadata.title
    authors = book.metadata.authors or ["Unknown"]
    description = (
        strip_html_tags(book.metadata.description) if book.metadata.description else ""
    )
    # Truncate description if too long (for frontmatter readability)
    if len(description) > 300:
        description = description[:297] + "..."

    # Extract year from date if available
    published = ""
    if book.metadata.date:
        # Try to extract year from various date formats
        year_match = re.search(r"\b(19|20)\d{2}\b", book.metadata.date)
        if year_match:
            published = year_match.group(0)

    # Build frontmatter
    created_date = datetime.now().strftime("%Y-%m-%d")

    # Format authors as wiki-links
    author_lines = "\n".join([f'  - "[[{author}]]"' for author in authors])

    # Build the markdown content
    lines = [
        "---",
        f'title: "{title}"',
        f"created: {created_date}",
    ]

    if description:
        # Escape quotes in description for YAML
        desc_escaped = description.replace('"', '\\"')
        lines.append(f'description: "{desc_escaped}"')

    lines.extend(
        [
            "tags:",
            "  - books",
        ]
    )

    if cover_image_name:
        lines.append(f'cover: "[[{cover_image_name}]]"')

    lines.extend(
        [
            "author:",
            author_lines,
            "status: want to read",
        ]
    )

    if published:
        lines.append(f"published: {published}")

    lines.append("---")

    # Body
    if cover_image_name:
        lines.append(f"![[{cover_image_name}]]")

    lines.extend(
        [
            "",
            "## notes",
            "",
        ]
    )

    # Write the note
    os.makedirs(OBSIDIAN_BOOKS_PATH, exist_ok=True)
    with open(note_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Created Obsidian note: {note_path}")
    return True


def process_all_epubs(directory: str = ".") -> tuple[int, int]:
    """
    Process all epub files in the directory that don't already have a _data folder.

    Returns:
        Tuple of (processed_count, skipped_count)
    """
    processed = 0
    skipped = 0

    # Find all epub files
    epub_files = [f for f in os.listdir(directory) if f.endswith(".epub")]

    if not epub_files:
        print("No epub files found in current directory")
        return 0, 0

    print(f"Found {len(epub_files)} epub file(s)")

    for epub_file in sorted(epub_files):
        epub_path = os.path.join(directory, epub_file)
        out_dir = os.path.splitext(epub_path)[0] + "_data"
        pkl_path = os.path.join(out_dir, "book.pkl")

        # Skip if already processed
        if os.path.exists(pkl_path):
            print(f"Skipping (already processed): {epub_file}")
            skipped += 1
            continue

        print(f"\n{'=' * 60}")
        print(f"Processing: {epub_file}")
        print("=" * 60)

        try:
            book_obj = process_epub(epub_path, out_dir)
            save_to_pickle(book_obj, out_dir)
            print(f"Done: {book_obj.metadata.title}")
            processed += 1
        except Exception as e:
            print(f"Error processing {epub_file}: {e}")
            skipped += 1

    return processed, skipped


def auto_process_books_folder(
    books_folder: str = "books", output_dir: str = "."
) -> tuple[int, int]:
    """
    Automatically process EPUB and PDF files from a specific folder.

    Args:
        books_folder: Folder containing EPUB/PDF files to process
        output_dir: Directory where _data folders should be created

    Returns:
        Tuple of (processed_count, skipped_count)
    """
    if not os.path.exists(books_folder):
        return 0, 0

    processed = 0
    skipped = 0

    # Find all epub and pdf files in the books folder
    book_files = [
        f for f in os.listdir(books_folder) if f.lower().endswith((".epub", ".pdf"))
    ]

    if not book_files:
        return 0, 0

    print(f"Auto-processing: Found {len(book_files)} book file(s) in {books_folder}/")

    for book_file in sorted(book_files):
        book_path = os.path.join(books_folder, book_file)
        out_dir = os.path.join(output_dir, os.path.splitext(book_file)[0] + "_data")
        pkl_path = os.path.join(out_dir, "book.pkl")

        # Skip if already processed
        if os.path.exists(pkl_path):
            print(f"  Skipping (already processed): {book_file}")
            skipped += 1
            continue

        print(f"\n{'=' * 60}")
        print(f"Auto-processing: {book_file}")
        print("=" * 60)

        try:
            if book_file.lower().endswith(".epub"):
                book_obj = process_epub(book_path, out_dir)
            else:  # PDF
                book_obj = process_pdf(book_path, out_dir)
            save_to_pickle(book_obj, out_dir)
            print(f"Done: {book_obj.metadata.title}")
            processed += 1
        except Exception as e:
            print(f"Error processing {book_file}: {e}")
            skipped += 1

    if processed > 0:
        print(f"\nAuto-processing complete: {processed} new book(s) added")

    return processed, skipped


def get_chapter_title_for_index(book: Book, chapter_index: int) -> str:
    """
    Get the chapter title for a given spine index.
    First tries to match via TOC, falls back to spine title.
    """
    if chapter_index < 0 or chapter_index >= len(book.spine):
        return f"Chapter {chapter_index + 1}"

    chapter = book.spine[chapter_index]
    chapter_href = chapter.href

    # Try to find matching TOC entry
    def find_in_toc(toc_entries: List[TOCEntry]) -> Optional[str]:
        for entry in toc_entries:
            if entry.file_href == chapter_href:
                return entry.title
            if entry.children:
                result = find_in_toc(entry.children)
                if result:
                    return result
        return None

    toc_title = find_in_toc(book.toc)
    if toc_title:
        return toc_title

    # Fallback to spine title
    return (
        chapter.title
        if chapter.title != f"Section {chapter_index + 1}"
        else f"Chapter {chapter_index + 1}"
    )


def update_obsidian_highlights_section(note_path: str, highlights_md: str) -> bool:
    """
    Update the highlights section in an Obsidian note.
    If ## Highlights section exists, replace it entirely.
    Otherwise, append it after ## notes section or at the end.

    Args:
        note_path: Path to the Obsidian markdown file
        highlights_md: The markdown content for the highlights section

    Returns:
        True if successful, False otherwise
    """
    try:
        with open(note_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Pattern to match the ## Highlights section and everything after it
        # until the next ## header or end of file
        highlights_pattern = re.compile(
            r"(## Highlights\s*\n)(.*?)(?=\n## |\Z)", re.DOTALL
        )

        if highlights_pattern.search(content):
            # Replace existing highlights section
            new_content = highlights_pattern.sub(highlights_md, content)
        else:
            # Append after ## notes section if it exists
            notes_pattern = re.compile(r"(## notes\s*\n)")
            if notes_pattern.search(content):
                # Insert after ## notes
                new_content = notes_pattern.sub(r"\1\n" + highlights_md + "\n", content)
            else:
                # Just append at the end
                new_content = content.rstrip() + "\n\n" + highlights_md + "\n"

        with open(note_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return True

    except Exception as e:
        print(f"Error updating highlights section: {e}")
        return False


def export_highlights_to_obsidian(book_data_dir: str) -> bool:
    """
    Export highlights from a book to its corresponding Obsidian note.

    Loads highlights from highlights.json, formats them as markdown quotes,
    and appends/updates the ## Highlights section in the book's Obsidian note.

    Args:
        book_data_dir: Path to the book's data directory (e.g., 'naval_data')

    Returns:
        True if export was successful, False otherwise.
    """
    import json

    # Load the book
    pkl_path = os.path.join(book_data_dir, "book.pkl")
    if not os.path.exists(pkl_path):
        print(f"Error: {pkl_path} not found")
        return False

    with open(pkl_path, "rb") as f:
        book = pickle.load(f)

    # Load highlights
    highlights_path = os.path.join(book_data_dir, "highlights.json")
    if not os.path.exists(highlights_path):
        print(f"No highlights file found for {book.metadata.title}")
        return False

    with open(highlights_path, "r", encoding="utf-8") as f:
        highlights_data = json.load(f)

    if not highlights_data:
        print(f"No highlights found for {book.metadata.title}")
        return False

    # Find the corresponding Obsidian note
    title_sanitized = sanitize_filename(book.metadata.title)
    note_filename = f"{title_sanitized}.md"
    note_path = os.path.join(OBSIDIAN_BOOKS_PATH, note_filename)

    if not os.path.exists(note_path):
        print(f"Warning: Obsidian note not found at {note_path}")
        print("Creating the book note first...")
        if not export_to_obsidian(book_data_dir):
            print(f"Failed to create book note for {book.metadata.title}")
            return False

    # Flatten and sort highlights by chapter_index, then by start_offset
    all_highlights = []
    for chapter_key, chapter_highlights in highlights_data.items():
        chapter_index = int(chapter_key)
        for h in chapter_highlights:
            all_highlights.append(
                {
                    "chapter_index": chapter_index,
                    "text": h.get("text", ""),
                    "start_offset": h.get("start_offset", 0),
                    "created_at": h.get("created_at", ""),
                }
            )

    # Sort by chapter_index first, then by start_offset (reading order)
    all_highlights.sort(key=lambda x: (x["chapter_index"], x["start_offset"]))

    if not all_highlights:
        print(f"No valid highlights to export for {book.metadata.title}")
        return False

    # Build markdown content
    lines = ["## Highlights", ""]

    for h in all_highlights:
        text = h["text"].strip()
        if text:
            # For multi-line quotes, add > prefix to each line to keep them in the blockquote
            text_lines = text.split("\n")
            quoted_lines = [f"> {line}" for line in text_lines]
            lines.append("\n".join(quoted_lines))
            lines.append("")

    highlights_md = "\n".join(lines)

    # Update the Obsidian note
    if update_obsidian_highlights_section(note_path, highlights_md):
        print(f"Exported {len(all_highlights)} highlights to {note_path}")
        return True
    else:
        print(f"Failed to update highlights in {note_path}")
        return False


def export_all_highlights_to_obsidian(directory: str = ".") -> tuple[int, int]:
    """
    Export highlights for all processed books to Obsidian.

    Args:
        directory: Directory containing book data folders

    Returns:
        Tuple of (exported_count, skipped_count)
    """
    import json

    exported = 0
    skipped = 0

    # Find all _data folders with highlights.json
    data_folders = [
        f
        for f in os.listdir(directory)
        if f.endswith("_data") and os.path.isdir(os.path.join(directory, f))
    ]

    if not data_folders:
        print("No processed books found (no *_data folders)")
        return 0, 0

    books_with_highlights = []
    for folder in data_folders:
        highlights_path = os.path.join(directory, folder, "highlights.json")
        if os.path.exists(highlights_path):
            # Check if highlights file has content
            try:
                with open(highlights_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data:  # Has some highlights
                        books_with_highlights.append(folder)
            except Exception:
                pass

    if not books_with_highlights:
        print("No books with highlights found")
        return 0, 0

    print(f"Found {len(books_with_highlights)} book(s) with highlights")

    for folder in sorted(books_with_highlights):
        folder_path = os.path.join(directory, folder)
        print(f"\nExporting highlights: {folder}")

        try:
            if export_highlights_to_obsidian(folder_path):
                exported += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"Error exporting {folder}: {e}")
            skipped += 1

    return exported, skipped


def export_all_to_obsidian(directory: str = ".") -> tuple[int, int]:
    """
    Export all processed books to Obsidian.

    Returns:
        Tuple of (exported_count, skipped_count)
    """
    exported = 0
    skipped = 0

    # Find all _data folders with book.pkl
    data_folders = [
        f
        for f in os.listdir(directory)
        if f.endswith("_data") and os.path.isdir(os.path.join(directory, f))
    ]

    if not data_folders:
        print("No processed books found (no *_data folders)")
        return 0, 0

    print(f"Found {len(data_folders)} processed book(s)")

    for folder in sorted(data_folders):
        folder_path = os.path.join(directory, folder)
        pkl_path = os.path.join(folder_path, "book.pkl")

        if not os.path.exists(pkl_path):
            print(f"Skipping (no book.pkl): {folder}")
            skipped += 1
            continue

        print(f"\nExporting: {folder}")

        try:
            if export_to_obsidian(folder_path):
                exported += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"Error exporting {folder}: {e}")
            skipped += 1

    return exported, skipped


# --- CLI ---

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  Process EPUB:       python reader3.py <file.epub>")
        print("  Process PDF:        python reader3.py <file.pdf>")
        print("  Process all books:  python reader3.py --process-all")
        print("  Export to Obsidian: python reader3.py --obsidian <book_data_folder>")
        print("  Export all to Obsidian: python reader3.py --obsidian-all")
        print(
            "  Export highlights:  python reader3.py --export-highlights <book_data_folder>"
        )
        print("  Export all highlights: python reader3.py --export-highlights-all")
        print("  Export to JSON:     python reader3.py --export-json-all")
        sys.exit(1)

    # Handle --process-all flag
    if sys.argv[1] == "--process-all":
        processed, skipped = process_all_epubs()
        print(f"\n{'=' * 60}")
        print(f"Summary: {processed} processed, {skipped} skipped")
        sys.exit(0)

    # Handle --export-json-all flag
    if sys.argv[1] == "--export-json-all":
        exported, skipped = export_all_to_json()
        print(f"\n{'=' * 60}")
        print(f"Summary: {exported} exported, {skipped} skipped")
        sys.exit(0)

    # Handle --obsidian-all flag
    if sys.argv[1] == "--obsidian-all":
        exported, skipped = export_all_to_obsidian()
        print(f"\n{'=' * 60}")
        print(f"Summary: {exported} exported, {skipped} skipped")
        sys.exit(0)

    # Handle --export-highlights-all flag
    if sys.argv[1] == "--export-highlights-all":
        exported, skipped = export_all_highlights_to_obsidian()
        print(f"\n{'=' * 60}")
        print(f"Summary: {exported} exported, {skipped} skipped")
        sys.exit(0)

    # Handle --export-highlights flag
    if sys.argv[1] == "--export-highlights":
        if len(sys.argv) < 3:
            print("Error: Please specify the book data folder")
            print("Usage: python reader3.py --export-highlights <book_data_folder>")
            sys.exit(1)

        book_folder = sys.argv[2]
        if not os.path.isdir(book_folder):
            print(f"Error: {book_folder} is not a directory")
            sys.exit(1)

        success = export_highlights_to_obsidian(book_folder)
        sys.exit(0 if success else 1)

    # Handle --obsidian flag
    if sys.argv[1] == "--obsidian":
        if len(sys.argv) < 3:
            print("Error: Please specify the book data folder")
            print("Usage: python reader3.py --obsidian <book_data_folder>")
            sys.exit(1)

        book_folder = sys.argv[2]
        if not os.path.isdir(book_folder):
            print(f"Error: {book_folder} is not a directory")
            sys.exit(1)

        success = export_to_obsidian(book_folder)
        sys.exit(0 if success else 1)

    # Normal EPUB/PDF processing
    book_file = sys.argv[1]
    assert os.path.exists(book_file), "File not found."
    out_dir = os.path.splitext(book_file)[0] + "_data"

    if book_file.lower().endswith(".pdf"):
        book_obj = process_pdf(book_file, out_dir)
    else:
        book_obj = process_epub(book_file, out_dir)
    save_to_pickle(book_obj, out_dir)
    print("\n--- Summary ---")
    print(f"Title: {book_obj.metadata.title}")
    print(f"Authors: {', '.join(book_obj.metadata.authors)}")
    print(f"Physical Files (Spine): {len(book_obj.spine)}")
    print(f"TOC Root Items: {len(book_obj.toc)}")
    print(f"Images extracted: {len(book_obj.images)}")
