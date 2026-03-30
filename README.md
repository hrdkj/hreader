# reader3

![reader3](reader3.png)

A lightweight, self-hosted EPUB reader designed for reading books alongside Large Language Models (LLMs). Features a clean chapter-by-chapter reading interface, text highlighting functionality, and seamless Obsidian integration for your personal knowledge management workflow.

Originally created by Andrej Karpathy to demonstrate how one can [read books together with LLMs](https://x.com/karpathy/status/1990577951671509438). Get EPUB books from sources like [Project Gutenberg](https://www.gutenberg.org/), open them in this reader, copy chapter text to your favorite LLM, and read together.

## Features

- **Clean Reading Interface**: Distraction-free chapter-by-chapter reading with sidebar navigation
- **Text Highlighting**: Select and highlight passages with persistent storage across sessions
- **Obsidian Integration**: Export books and highlights to your Obsidian vault as markdown notes
- **Library Management**: Visual grid library with book covers and metadata
- **Image Support**: Properly extracts and displays images from EPUB files
- **LLM-Friendly**: Easy chapter text copying for use with ChatGPT, Claude, or other LLMs
- **Self-Hosted**: Runs entirely on your local machine, no cloud services required

## Quick Start

### Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) (modern Python package manager)

### Installation

Clone this repository and ensure uv is installed:

```bash
git clone <repository-url>
cd reader3
```

### Basic Usage

1. **Get an EPUB file**. For example, download [Dracula from Project Gutenberg](https://www.gutenberg.org/ebooks/345):

```bash
wget https://www.gutenberg.org/ebooks/345.epub.noimages -O dracula.epub
```

2. **Process the EPUB file**:

```bash
uv run reader3.py dracula.epub
```

This creates a `dracula_data/` directory containing:
- `book.pkl` - Processed book data
- `highlights.json` - Your saved highlights
- `images/` - Extracted book images

3. **Start the web server**:

```bash
uv run server.py
```

4. **Open your browser** and visit [http://localhost:8123](http://localhost:8123)

You'll see your library. Click on a book to start reading.

## Usage Guide

### Processing Books

```bash
# Process a single EPUB file
uv run reader3.py book.epub

# Process all EPUB files in the current directory
uv run reader3.py --process-all
```

### Reading Books

Once the server is running:

1. Navigate to the library at `http://localhost:8123`
2. Click "Read Book" on any processed book
3. Use the sidebar navigation or Previous/Next buttons to move between chapters
4. Select text and click "Highlight" to save passages
5. Click "Remove Highlight" to delete highlights
6. Copy chapter text (Ctrl+A, Ctrl+C) to share with your LLM

### Highlighting System

The highlighting system uses character offset tracking for reliable restoration:

- **Create Highlights**: Select text and click the "Highlight" button in the floating toolbar
- **Remove Highlights**: Select highlighted text and click "Remove Highlight"
- **Persistence**: Highlights are saved to `{book_name}_data/highlights.json`
- **Restoration**: Highlights automatically reload when you return to a chapter

### Obsidian Integration

Reader3 can export books and highlights to your Obsidian vault:

**Configure Obsidian paths** (edit `reader3.py` lines 19-20):

```python
OBSIDIAN_BOOKS_PATH = "/path/to/your/vault/books"
OBSIDIAN_IMAGES_PATH = "/path/to/your/vault/Images"
```

**Export commands:**

```bash
# Export a single book's metadata as an Obsidian note
uv run reader3.py --obsidian book_data/

# Export all processed books
uv run reader3.py --obsidian-all

# Export highlights from a single book
uv run reader3.py --export-highlights book_data/

# Export highlights from all books
uv run reader3.py --export-highlights-all
```

**Exported book notes include:**
- YAML frontmatter with metadata (title, author, published date, cover)
- Author wikilinks for connecting with other notes
- Cover image copied to your vault
- Status field (defaults to "want to read")

**Exported highlights are formatted as:**
```markdown
## Highlights

> First highlighted passage from the book

> Second highlighted passage

> Third highlighted passage
```

### Library Management

- **Add books**: Process new EPUB files with `uv run reader3.py book.epub`
- **Remove books**: Delete the `{book_name}_data/` directory
- **View library**: Visit the root URL to see all processed books with covers

## Project Structure

```
reader3/
├── reader3.py              # EPUB processing and Obsidian integration
├── server.py               # FastAPI web server
├── templates/
│   ├── library.html       # Library grid view
│   └── reader.html        # Reading interface with highlights
├── pyproject.toml         # Project dependencies
├── .python-version        # Python version specification
├── README.md              # This file
├── *.epub                 # Your EPUB files (gitignored)
└── *_data/                # Processed book directories (gitignored)
    ├── book.pkl           # Serialized book object
    ├── highlights.json    # Your highlights
    └── images/            # Extracted images
```

## Technology Stack

**Backend:**
- FastAPI - Web framework
- ebooklib - EPUB parsing
- BeautifulSoup4 - HTML processing
- Uvicorn - ASGI server

**Frontend:**
- Vanilla JavaScript - No frameworks
- Modern HTML5 & CSS3 - Responsive design
- Selection API - Text highlighting

## Configuration

### Server Port

Default: `http://127.0.0.1:8123`

To change, edit `server.py` line 282:

```python
uvicorn.run(app, host="127.0.0.1", port=8123)
```

### Obsidian Paths

Edit `reader3.py` lines 19-20:

```python
OBSIDIAN_BOOKS_PATH = "/home/user/vault/books"
OBSIDIAN_IMAGES_PATH = "/home/user/vault/Images"
```

### Cache Size

The server caches up to 10 books in memory. To adjust, edit `server.py` line 44:

```python
@lru_cache(maxsize=10)
```

## How It Works

### EPUB Processing Pipeline

1. Load EPUB file with ebooklib
2. Extract metadata (title, authors, publisher, dates, identifiers)
3. Extract all images to `images/` directory
4. Build navigation tree (table of contents)
5. Process spine (linear reading order) into chapters
6. Clean HTML (remove scripts, styles, dangerous tags)
7. Rewrite image paths to local references
8. Extract plain text for each chapter
9. Detect cover image using multiple fallback methods
10. Serialize to `book.pkl` with pickle

### Highlight System Architecture

**Creation:**
1. User selects text in the reading area
2. JavaScript calculates character offset from chapter start
3. Wraps selection in `<span class="highlight">` element
4. POSTs highlight data to server (text, offsets, chapter)
5. Server saves to `highlights.json`

**Restoration:**
1. Page loads and fetches highlights for current chapter
2. Uses TreeWalker to traverse text nodes in document order
3. Finds exact character positions matching saved offsets
4. Wraps text in highlight spans
5. Falls back to text search if offset restoration fails

### API Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Library view |
| `/read/{book_id}/{chapter_index}` | GET | Chapter reading interface |
| `/cover/{book_id}` | GET | Book cover image |
| `/read/{book_id}/images/{image_name}` | GET | Chapter images |
| `/api/highlights` | POST | Create highlight |
| `/api/highlights/{book_id}/{chapter_index}` | GET | Get chapter highlights |
| `/api/highlights` | DELETE | Remove highlight |

## Limitations

- Single-user application (no authentication)
- No full-text search across books
- No reading position bookmarks
- TOC anchor links navigate to file but don't scroll to anchor
- EPUB format only (no PDF support)
- No highlight editing or annotations
- Obsidian paths are hardcoded (not CLI configurable)

## Use Cases

**Reading with LLMs:**
1. Open a chapter in reader3
2. Highlight interesting passages
3. Copy chapter text (Ctrl+A, Ctrl+C)
4. Paste into ChatGPT/Claude for discussion
5. Ask questions, get explanations, explore ideas
6. Export highlights to Obsidian for permanent notes

**Building a Personal Library:**
1. Process your entire EPUB collection
2. Browse with visual covers
3. Read and annotate books
4. Export to Obsidian vault
5. Link book notes with other knowledge
6. Build a connected reading database

**Academic Reading:**
1. Import academic books or papers (EPUB format)
2. Highlight key passages and arguments
3. Copy chapters to LLM for summarization
4. Export highlights as literature review notes
5. Connect to your research notes in Obsidian

## Troubleshooting

**Book not appearing in library:**
- Ensure the `*_data` directory was created successfully
- Check that `book.pkl` exists in the data directory
- Verify the EPUB file is valid

**Highlights not saving:**
- Check browser console for JavaScript errors
- Verify `highlights.json` is writable
- Ensure book_id and chapter_index are correct

**Images not displaying:**
- Check that `images/` directory exists in book data folder
- Verify image files were extracted during processing
- Check browser console for 404 errors

**Obsidian export not working:**
- Verify OBSIDIAN_BOOKS_PATH and OBSIDIAN_IMAGES_PATH are correct
- Ensure target directories exist and are writable
- Check that book has been processed (book.pkl exists)

## Development

This project was intentionally kept simple and straightforward. The codebase is designed to be easily understood and modified:

- `reader3.py` (951 lines) - Core EPUB processing logic
- `server.py` (283 lines) - Web server and API
- `reader.html` (890 lines) - Reading interface with highlighting

No complex build process, no heavy frameworks, no unnecessary abstractions. Ask your LLM to modify it however you like.

## Contributing

This project was created as a demonstration and is provided as-is. Feel free to fork and modify for your own needs. The code is intentionally simple to encourage experimentation and customization.

## License

MIT