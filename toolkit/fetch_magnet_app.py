import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import requests
import streamlit as st
from bs4 import BeautifulSoup
from dotenv import load_dotenv, set_key


# =======================================================
# CORE FUNCTIONS (Return on page magnet by giving an url)
# =======================================================
def get_magnet_link(url):
    try:
        html_content = requests.get(url, timeout=10)
        soup = BeautifulSoup(html_content.text, "html.parser")
        magnet = soup.find("a", string="Magnet").get("href")
        
        return {"success": True, "result": magnet}

    except Exception:
        return {"success": False, "url": url}


# ==========================================
# STREAMLIT UI SETUP
# ==========================================
st.title("🧲 Magnet Link Scraper")
st.write("Scrape your unwatched video links automatically or manually.")

option_map = {
    "auto": "Automatic Mode",
    "manual": "Manual Mode",
    "cleanup": "Remove Duplicated"
}

mode = st.segmented_control(
    "",
    options=option_map.keys(),
    default="auto",
    format_func=lambda x: option_map.get(x)
)

unwatched_video_urls = []
run_scraper = False
env_path = "./.env"

# --- OPTION 1: AUTOMATIC MODE ---
if mode == "auto":
    st.write("## Automatic Scraper Config")
    start_page = st.number_input("Start Page", min_value=1, value=1, step=1)
    
    if st.button("Start Automatic Scrape", type="primary"):
        # Get unwatched videos automatically
        load_dotenv()
        cutoff_video_url = os.getenv("CUTOFF_VIDEO")
        source_website = os.getenv("WEBSITE_URL")
        
        # Initial parameters
        page_idx = start_page
        found =False
    
        # Visual indicator that the background loop is working
        with st.spinner(f"Finding unwatched videos starting from page {page_idx}..."):
            while not found:
                try:
                    page_url = f"{source_website}/page/{page_idx}/"
                    content = requests.get(url=page_url, timeout=10)
                    soup = BeautifulSoup(content.text, "html.parser")
                    on_page_links = soup.find_all("a", rel="bookmark")
                    
                    urls = [link.get("href") for link in on_page_links if link.get("href")]
                    unwatched_video_urls += urls
                    
                    if cutoff_video_url in urls:
                        found = True
                    else:
                        page_idx += 1
                
                except Exception as e:
                    st.error(f"Error on Page {page_idx}: {e}")
                    break
        
        if unwatched_video_urls and cutoff_video_url in unwatched_video_urls:
            # Remove watched videos urls
            cutoff_idx = unwatched_video_urls.index(cutoff_video_url)
            unwatched_video_urls = unwatched_video_urls[:cutoff_idx]
        
        if unwatched_video_urls:
            set_key(env_path, "CUTOFF_VIDEO", unwatched_video_urls[0])
        
        run_scraper = True


# --- OPTION 2: MANUAL MODE ---
if mode == "manual":
    st.write("## Manual Input")
    raw_input =st.text_area(
        "Paste your non fetched video URLs here (one per line):"
    )
    
    if st.button("Process Manual Links", type="primary"):
        if raw_input.strip():
            unwatched_video_urls = raw_input.strip().splitlines()
            run_scraper = True
        else:
            st.warning("Please enter at least one URL")
            

# --- OPTION 3: REMOVE DUPLICATED ---
if mode == "cleanup":
    st.write("## Paste Raw Magnet")
    raw_input =st.text_area(
        "Paste all your magnet links here (one per line):"
    )
    if st.button("Remove Duplicated", type="primary"):
        if raw_input.strip():
            raw_input = raw_input.strip().splitlines()
            unique_magnet = set(raw_input)
            pure_magnet = "\n".join([magnet for magnet in unique_magnet])
            st.write(f"Found {len(unique_magnet)} Unique Links")
            st.code(pure_magnet, language="text")
        else:
            st.warning("Please enter at least one magnet link")


# ==========================================
# EXECUTION & RESULTS DISPLAY
# ==========================================
# This only runs if one of the buttons set run_scraper to True
if run_scraper:
    if not unwatched_video_urls:
        st.info("No new unwatched video found.")
    else:
        # Fetch magnets simultaneously
        with st.status("Fetching magnet links concurrently...", expanded=True) as status:
            with ThreadPoolExecutor() as executor:
                results = list(executor.map(get_magnet_link, unwatched_video_urls))
            status.update(label="Scraping complete!", state="complete", expanded=False)
        # Retrieve results
        successful = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]
    
    # Display Metrics
    col1, col2, col3 = st.columns(3)
    col1.metric("Total Found", len(unwatched_video_urls))
    col2.metric("Successful ✅", len(successful))
    col3.metric("Failed ❌", len(failed))
    
    # Show Successful Magnets
    if successful:
        st.write("## 🚀 Grabbed Magnets")
        # Put them in a copyable text box for convenience
        magnets_text = "\n".join([item["result"] for item in successful])
        st.code(magnets_text, language="text")
    
    # Show Failed URLs
    if failed:
        st.write("## ⚠️ Failed URLs")
        failed_url = "\n".join([item["url"] for item in failed])
        st.code(failed_url, language="text")
    
    # Play notification sound
    subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
