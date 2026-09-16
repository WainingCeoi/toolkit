"""Web Images to PDF engine: capture a page's images in Chrome and build a PDF."""

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
    """Lazy: fitz imported under filterwarnings=error segfaults at exit on 3.14."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message="builtin type .* has no __module__ attribute"
        )
        import fitz
    return fitz


# A path component is capped at 255 characters; the artifact store prefixes 13.
MAX_NAME = 200


# --- CORE LOGIC ---
def sanitize_filename(name):
    """Strip path-unsafe characters from a page title used as a file name."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name).strip()
    return cleaned[:MAX_NAME] or "web"


def scrape_images_from_source(page_source, page_url):
    """Parse a captured page for its title + lazy-loaded images; download them."""
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
    """Save the page images into a single multi-page PDF; return its path."""
    pdf_path = Path(output_folder).expanduser() / pdf_name
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(str(pdf_path), save_all=True, append_images=images[1:])
    return str(pdf_path)


def add_bookmark(page_source, pdf_path):
    """Best-effort TOC from the page's anchors; None on success, else a reason."""
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


# --- BROWSER SESSION ---
class BrowserSession:
    """One live Chrome window the user scrolls until every image has loaded."""

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
        # Quit the driver on a failed navigation or Chrome + chromedriver leak.
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

    @property
    def current_url(self) -> str | None:
        """Where Chrome is now: the user may have navigated since open()."""
        return self._driver.current_url or self.url

    def page_source(self) -> str:
        return self._driver.page_source

    def quit(self) -> None:
        try:
            self._driver.quit()
        except Exception:
            pass
        self._driver = None

    # Alias the app lifespan calls at shutdown.
    shutdown = quit
