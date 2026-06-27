import streamlit as st

st.title("🧰 Toolkit")
st.write(
    "A local collection of small media & file utilities. "
    "Choose a tool from the sidebar — or jump in below."
)

TOOLS = [
    (
        "pages/magnet_scraper.py",
        "🧲 Magnet Scraper",
        "Scrape unwatched video magnet links automatically, in bulk, "
        "or de-duplicate a pasted list.",
    ),
    (
        "pages/file_gatherer.py",
        "📦 File Gatherer",
        "Recursively gather files by type and move them into one folder.",
    ),
    (
        "pages/remux_processor.py",
        "🎬 Remux Processor",
        "Parallel, lossless remuxing (stream-copy) of videos with FFmpeg.",
    ),
    (
        "pages/img_to_pdf.py",
        "🖼️ Image to PDF",
        "Combine selected images into a single PDF on your Desktop.",
    ),
    (
        "pages/optimized_ip_generator.py",
        "🛰️ Optimized-IP Subscription",
        "Rewrite vmess/vless/trojan nodes with optimized Cloudflare IPs and "
        "serve Shadowrocket / Clash / Surge subscriptions over your LAN.",
    ),
]

cols = st.columns(2)
for index, (path, title, description) in enumerate(TOOLS):
    with cols[index % 2]:
        with st.container(border=True):
            st.page_link(path, label=title)
            st.caption(description)
