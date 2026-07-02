import base64
import json
import re
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import fitz
import requests
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from lib.folder_picker import folder_field

DRIVER_KEY = "wipdf_driver"


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
    URLs work) and data: URIs are decoded inline. Each fetch is isolated, so one
    bad image is skipped rather than aborting the whole capture. Returns
    (pdf_name, [PIL.Image, ...], skipped_count).
    """
    soup = BeautifulSoup(page_source, "html.parser")
    title_tag = soup.find("title")
    title = title_tag.text.strip() if title_tag and title_tag.text.strip() else "web"
    pdf_name = f"{sanitize_filename(title)}.pdf"

    images, skipped = [], 0
    for tag in soup.select("img[class*=bi]"):
        src = tag.get("src")
        if not src:
            continue
        try:
            if src.startswith("data:"):
                raw = base64.b64decode(src.partition(",")[2])
            else:
                resp = requests.get(urljoin(page_url, src), timeout=15)
                resp.raise_for_status()
                raw = resp.content
            images.append(Image.open(BytesIO(raw)).convert("RGB"))
        except Exception:
            skipped += 1
    return pdf_name, images, skipped


def build_pdf(images, output_folder, pdf_name):
    """Save the page images into a single multi-page PDF; return its path.

    Creates the output folder if a typed path doesn't exist yet.
    """
    pdf_path = Path(output_folder).expanduser() / pdf_name
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(str(pdf_path), save_all=True, append_images=images[1:])
    return str(pdf_path)


def add_bookmark(page_url, pdf_path):
    """Re-fetch the page and add a bookmarked TOC from its anchor tags.

    Best-effort: returns None on success or a short reason string on failure.
    The PDF is already saved regardless, so a failure here is non-fatal.
    """
    try:
        html = requests.get(page_url, timeout=10).text
        soup = BeautifulSoup(html, "html.parser")
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
        doc = fitz.open(pdf_path)
        doc.set_toc(toc)
        doc.save(pdf_path, incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        doc.close()
        return None
    except Exception as e:
        return str(e)


# =======================================================
# STREAMLIT UI SETUP
# =======================================================
st.title("🌐 Web Images to PDF")
st.write(
    "Open a web page in a real browser, scroll to load all of its images, "
    "then capture them into a single PDF (with bookmarks when available). "
    "Requires Google Chrome."
)

# --- 1. SOURCE & OUTPUT ---
st.write("## 1. Source & Output")
url = st.text_input("Page URL", placeholder="https://…")

output_folder = folder_field(
    "Output folder", "wipdf_output", str(Path("~/Desktop").expanduser())
)

# --- 2. CAPTURE ---
st.write("## 2. Capture")
driver_open = DRIVER_KEY in st.session_state

col_open, col_close = st.columns(2)
with col_open:
    if st.button("🌐 Open in browser", disabled=driver_open or not url.strip()):
        try:
            with st.spinner("Launching Chrome…"):
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service)
                driver.get(url)
            st.session_state[DRIVER_KEY] = driver
            st.session_state.pop("wipdf_result", None)
            st.rerun()
        except Exception as e:
            st.error(f"Could not open browser: {e}")
with col_close:
    if st.button("✖ Close browser", disabled=not driver_open):
        try:
            st.session_state[DRIVER_KEY].quit()
        except Exception:
            pass
        st.session_state.pop(DRIVER_KEY, None)
        st.rerun()

if driver_open:
    st.info(
        "Chrome is open. **Scroll down in that window until every page/image "
        "has loaded**, then click *Capture & build PDF*."
    )
    if st.button("📸 Capture & build PDF", type="primary"):
        # A relative (or empty) typed path would land in the app's CWD.
        if not Path(output_folder).expanduser().is_absolute():
            st.error("❌ Use an absolute output folder path.")
            st.stop()
        driver = st.session_state[DRIVER_KEY]
        try:
            with st.spinner("Capturing page & downloading images…"):
                page_source = driver.page_source
                pdf_name, images, skipped = scrape_images_from_source(page_source, url)
            if not images:
                st.error(
                    "No images found on the page (selector `img[class*=bi]`). "
                    "Make sure every page finished loading before capturing."
                )
            else:
                pdf_path = build_pdf(images, output_folder, pdf_name)
                warn = add_bookmark(url, pdf_path)
                try:
                    driver.quit()
                except Exception:
                    pass
                st.session_state.pop(DRIVER_KEY, None)
                st.session_state["wipdf_result"] = {
                    "path": pdf_path,
                    "name": pdf_name,
                    "pages": len(images),
                    "skipped": skipped,
                    "warn": warn,
                }
                st.toast(f"Web Images to PDF: saved {len(images)}-page PDF.", icon="🌐")
                st.rerun()
        except Exception as e:
            st.error(f"Capture failed: {e}")

# Last successful build — persisted so the download survives reruns.
result = st.session_state.get("wipdf_result")
if result:
    st.success(f"Saved **{result['pages']}**-page PDF → `{result['path']}`")
    if result["skipped"]:
        st.warning(
            f"{result['skipped']} image(s) couldn't be downloaded and were skipped."
        )
    if result["warn"]:
        st.warning(f"Bookmarks skipped: {result['warn']}")
    try:
        with open(result["path"], "rb") as fh:
            st.download_button(
                "⬇ Download PDF",
                fh.read(),
                file_name=result["name"],
                mime="application/pdf",
                key="wipdf_dl",
            )
    except OSError:
        st.session_state.pop("wipdf_result", None)
