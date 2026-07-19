"""Web Images to PDF engine — lifted from the Streamlit page web_images_to_pdf.py.

The core functions (sanitize_filename, scrape_images_from_source, build_pdf,
add_bookmark) are verbatim lifts. BrowserSession wraps the page's single live
Selenium driver (st.session_state[DRIVER_KEY]) as an object the API can park
on AppState.browser; selenium/webdriver_manager are imported lazily inside
open() so importing this module never requires them.
"""

from __future__ import annotations

import base64
import json
import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image


def _import_fitz():
    """Import pymupdf lazily, and only when a bookmark is actually written.

    On Python 3.14 pymupdf's SWIG bindings emit "builtin type ... has no
    __module__ attribute" DeprecationWarnings during C-extension init, and any
    process where warnings are promoted to errors (the test suite runs with
    filterwarnings=error) segfaults at interpreter shutdown once fitz has been
    loaded — even if the import itself is guarded. Keeping the import out of
    module scope means the API/test processes never load fitz at all; only the
    capture worker pays for it, with default warning filters.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="builtin type .* has no __module__ attribute"
        )
        import fitz
    return fitz


# =======================================================
# CORE LOGIC
# =======================================================
def sanitize_filename(name):
    """Strip path-unsafe characters from a page title used as a file name."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return cleaned or "web"


def scrape_images_from_source(page_source, page_url):
    """Parse a captured page for its title + lazy-loaded images; download them.

    Image srcs are resolved against page_url (so relative / protocol-relative
    URLs work) and data: URIs are decoded inline. Downloads run in parallel over
    one pooled session (keep-alive) but the page order is preserved. Each fetch
    is isolated, so one bad image is skipped rather than aborting the whole
    capture. Returns (pdf_name, [PIL.Image, ...], skipped_count).
    """
    soup = BeautifulSoup(page_source, "lxml")
    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag and title_tag.text.strip() else "web"
    pdf_name = f"{sanitize_filename(title)}.pdf"

    srcs = [src for tag in soup.select("img[class*=bi]") if (src := tag.get("src"))]

    def fetch(index_src):
        index, src = index_src
        if src.startswith("data:"):
            raw = base64.b64decode(src.partition(",")[2])
        else:
            resp = session.get(urljoin(page_url, src), timeout=15)
            resp.raise_for_status()
            raw = resp.content
        return index, Image.open(BytesIO(raw)).convert("RGB")

    by_index: dict[int, Image.Image] = {}
    skipped = 0
    session = requests.Session()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in as_completed(
                [pool.submit(fetch, item) for item in enumerate(srcs)]
            ):
                try:
                    index, image = future.result()
                    by_index[index] = image
                except Exception:
                    skipped += 1
    finally:
        session.close()
    images = [by_index[i] for i in sorted(by_index)]
    return pdf_name, images, skipped


def build_pdf(images, output_folder, pdf_name):
    """Save the page images into a single multi-page PDF; return its path.

    Creates the output folder if a typed path doesn't exist yet.
    """
    pdf_path = Path(output_folder).expanduser() / pdf_name
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(str(pdf_path), save_all=True, append_images=images[1:])
    return str(pdf_path)


def add_bookmark(page_source, pdf_path):
    """Add a bookmarked TOC to the PDF from the captured page's anchor tags.

    Best-effort: returns None on success or a short reason string on failure.
    The PDF is already saved regardless, so a failure here is non-fatal. Uses
    the page_source the caller already captured — no second network fetch.
    """
    try:
        soup = BeautifulSoup(page_source, "lxml")
        toc = []
        prev_level = 0
        for content in soup.find_all("a"):
            detail = content.get("data-dest-detail")
            if not detail:
                continue
            # set_toc requires the first item at level 1 and no jump > 1.
            raw_depth = max(len(content.find_parents("li")), 1)
            level = 1 if prev_level == 0 else min(raw_depth, prev_level + 1)
            prev_level = level
            page_number = json.loads(detail)[0]
            page_name = f"{content.text}({page_number})"
            toc.append([level, page_name, page_number])
        if not toc:
            return "no bookmark anchors found on the page"
        fitz = _import_fitz()
        doc = fitz.open(pdf_path)
        doc.set_toc(toc)
        doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()
        return None
    except Exception as e:
        return str(e)


# =======================================================
# BROWSER SESSION
# =======================================================
class BrowserSession:
    """One live Chrome window — the page's single-driver session model.

    open() is exactly the page's zero-options launch (ChromeDriverManager
    install + plain webdriver.Chrome + get). The user scrolls the real window
    until every image has loaded, then the router captures page_source().
    """

    def __init__(self) -> None:
        self._driver = None
        self.url: str | None = None

    @property
    def is_open(self) -> bool:
        return self._driver is not None

    def open(self, url: str) -> None:
        # Lazy imports: only launching a browser needs selenium installed.
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager

        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service)
        # If navigation fails, quit the just-launched driver before propagating —
        # otherwise an orphaned Chrome + chromedriver leaks for the session.
        try:
            driver.get(url)
        except Exception:
            try:
                driver.quit()
            except Exception:
                pass
            raise
        self._driver = driver
        self.url = url

    def page_source(self) -> str:
        return self._driver.page_source

    def quit(self) -> None:
        """Exception-swallowing quit, like the page's close button."""
        try:
            self._driver.quit()
        except Exception:
            pass
        self._driver = None

    # main.py's lifespan calls state.browser.shutdown() at process exit.
    shutdown = quit
