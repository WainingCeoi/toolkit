import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv, set_key

MAX_PAGES = 100  # hard cap so Automatic mode can never loop forever
CODE_HEIGHT = 200  # px — fixed height for result blocks; overflow scrolls

# Anchor .env to the repo root regardless of the launch directory
if "__file__" in globals():
    ENV_PATH = str(Path(__file__).resolve().parents[2] / ".env")
else:
    ENV_PATH = str(Path.cwd() / ".env")


# =======================================================
# CORE FUNCTIONS — fetch a magnet link from a video page URL
# =======================================================
def get_magnet_link(url):
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        tag = soup.find("a", string="Magnet")
        if tag is None or not tag.get("href"):
            return {"success": False, "url": url, "reason": "no magnet link on page"}
        return {"success": True, "result": tag.get("href")}
    except Exception as e:
        return {"success": False, "url": url, "reason": str(e)}


# ==========================================
# STREAMLIT UI SETUP
# ==========================================
st.title("🧲 Magnet Scraper")
st.write("Scrape your unwatched video links automatically or manually.")

option_map = {
    "auto": "Automatic Mode",
    "manual": "Manual Mode",
    "cleanup": "Remove Duplicated",
}

mode = st.segmented_control(
    "Mode",
    options=option_map.keys(),
    default="auto",
    format_func=lambda x: option_map.get(x),
    label_visibility="collapsed",
)

unwatched_video_urls = []
run_scraper = False

# --- OPTION 1: AUTOMATIC MODE ---
if mode == "auto":
    st.write("## Automatic Scraper Config")
    start_page = st.number_input("Start Page", min_value=1, value=1, step=1)

    if st.button("Start Automatic Scrape", type="primary"):
        load_dotenv(ENV_PATH)
        cutoff_video_url = os.getenv("CUTOFF_VIDEO")
        source_website = os.getenv("WEBSITE_URL")

        # Guard against missing config (otherwise URLs become "None/page/1/"
        # and the loop below has no valid stopping point)
        if not source_website:
            st.error("❌ WEBSITE_URL is not set in .env.")
            st.stop()
        if not cutoff_video_url:
            st.error("❌ CUTOFF_VIDEO is not set in .env (no stopping point).")
            st.stop()

        # Initial parameters
        page_idx = start_page
        last_page = start_page + MAX_PAGES
        found = False

        # Visual indicator that the background loop is working
        with st.spinner(f"Finding unwatched videos from page {page_idx}..."):
            while not found and page_idx < last_page:
                try:
                    page_url = f"{source_website}/page/{page_idx}/"
                    response = requests.get(url=page_url, timeout=10)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    on_page_links = soup.find_all("a", rel="bookmark")

                    urls = [
                        link.get("href") for link in on_page_links if link.get("href")
                    ]
                    if not urls:
                        break  # ran past the last page of results

                    unwatched_video_urls += urls
                    if cutoff_video_url in urls:
                        found = True
                    else:
                        page_idx += 1

                except Exception as e:
                    st.error(f"❌ Error on page {page_idx}: {e}")
                    break

        # Only save / advance the cutoff / scrape once the cutoff is located —
        # otherwise a stale CUTOFF_VIDEO or a network error would overwrite the
        # anchor and submit a huge/partial batch.
        if found:
            # Keep only the videos newer than the cutoff, then advance the
            # cutoff to the newest so the next run stops here.
            cutoff_idx = unwatched_video_urls.index(cutoff_video_url)
            unwatched_video_urls = unwatched_video_urls[:cutoff_idx]
            if unwatched_video_urls:
                set_key(ENV_PATH, "CUTOFF_VIDEO", unwatched_video_urls[0])
            run_scraper = True
        else:
            st.warning(
                "Cutoff video not found — check CUTOFF_VIDEO or raise the page "
                "limit. Nothing was scraped and the cutoff was left unchanged."
            )


# --- OPTION 2: MANUAL MODE ---
if mode == "manual":
    st.write("## Manual Input")
    raw_input = st.text_area("Paste your non fetched video URLs here (one per line):")

    if st.button("Process Manual Links", type="primary"):
        if raw_input.strip():
            unwatched_video_urls = raw_input.strip().splitlines()
            run_scraper = True
        else:
            st.warning("Please enter at least one URL")


# --- OPTION 3: REMOVE DUPLICATED ---
if mode == "cleanup":
    st.write("## Paste Raw Magnet")
    raw_input = st.text_area("Paste all your magnet links here (one per line):")
    if st.button("Remove Duplicated", type="primary"):
        if raw_input.strip():
            unique_magnet = set(raw_input.strip().splitlines())
            pure_magnet = "\n".join(unique_magnet)
            st.write(f"Found {len(unique_magnet)} Unique Links")
            st.code(pure_magnet, language="text", height=CODE_HEIGHT)
        else:
            st.warning("Please enter at least one magnet link")


# ==========================================
# EXECUTION & RESULTS (persisted across reruns)
# ==========================================
if run_scraper:
    if not unwatched_video_urls:
        st.session_state.scrape = {"urls": [], "successful": [], "failed": []}
    else:
        # Fetch magnets simultaneously, advancing the bar as each one returns.
        total = len(unwatched_video_urls)
        bar = st.progress(0, text=f"Fetching magnets… 0/{total}")
        results = []
        with ThreadPoolExecutor() as executor:
            for idx, result in enumerate(
                executor.map(get_magnet_link, unwatched_video_urls), start=1
            ):
                results.append(result)
                bar.progress(
                    int(idx / total * 100), text=f"Fetching magnets… {idx}/{total}"
                )
        bar.progress(100, text=f"Fetched {total}/{total} link(s).")

        st.session_state.scrape = {
            "urls": unwatched_video_urls,
            "successful": [r for r in results if r["success"]],
            "failed": [r for r in results if not r["success"]],
        }
        # Notify only on a fresh scrape
        st.toast(
            f"Magnet Scraper: {sum(r['success'] for r in results)} magnet(s) found.",
            icon="🧲",
        )

# Render the most recent scrape so results survive unrelated reruns
scrape = st.session_state.get("scrape")
if scrape is not None and mode in ("auto", "manual"):
    if not scrape["urls"]:
        st.info("No new unwatched video found.")
    else:
        successful = scrape["successful"]
        failed = scrape["failed"]

        # Display Metrics
        col1, col2, col3 = st.columns(3)
        col1.metric("Total Found", len(scrape["urls"]))
        col2.metric("Successful ✅", len(successful))
        col3.metric("Failed ❌", len(failed))

        # Show Successful Magnets
        if successful:
            st.write("## 🚀 Grabbed Magnets")
            magnets_text = "\n".join(item["result"] for item in successful)
            st.code(magnets_text, language="text", height=CODE_HEIGHT)

        # Show Failed URLs (URLs only, no reason)
        if failed:
            st.write("## ⚠️ Failed URLs")
            failed_text = "\n".join(item["url"] for item in failed)
            st.code(failed_text, language="text", height=CODE_HEIGHT)
