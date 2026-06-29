import streamlit as st

st.title("🧰 Toolkit")
st.write(
    "A local collection of small media & file utilities. "
    "Choose a tool from the sidebar — or jump in below."
)

# Tools grouped by category. Reorder freely — each category renders as a
# top-aligned 2-column grid under its own divider.
CATEGORIES = [
    (
        "🎬 Media",
        [
            (
                "pages/magnet_scraper.py",
                "🧲 Magnet Scraper",
                "Scrape unwatched video magnet links automatically, in bulk, "
                "or de-duplicate a pasted list.",
            ),
            (
                "pages/remux_processor.py",
                "🎬 Remux Processor",
                "Parallel, lossless remuxing (stream-copy) of videos with FFmpeg.",
            ),
        ],
    ),
    (
        "🗂️ Documents & Files",
        [
            (
                "pages/web_images_to_pdf.py",
                "🌐 Web Images to PDF",
                "Open a web page, scroll to load its images, and capture them "
                "into a single PDF.",
            ),
            (
                "pages/file_gatherer.py",
                "📦 File Gatherer",
                "Recursively gather files by type and move them into one folder.",
            ),
            (
                "pages/img_to_pdf.py",
                "🖼️ Image to PDF",
                "Combine selected images into a single PDF on your Desktop.",
            ),
            (
                "pages/doc_to_pdf.py",
                "📄 Doc to PDF",
                "Clean a Word doc (accept changes, remove comments) and export "
                "it to PDF.",
            ),
            (
                "pages/doc_to_markdown.py",
                "📝 Doc to Markdown",
                "Convert PDFs, Office docs, and images into clean Markdown "
                "with MinerU.",
            ),
            (
                "pages/cache_purge.py",
                "🧹 Cache Purge",
                "Recursively find and delete cache / junk files from a folder.",
            ),
        ],
    ),
    (
        "🌐 Network",
        [
            (
                "pages/optimized_ip_generator.py",
                "🛰️ Optimized-IP Subscription",
                "Rewrite vmess/vless/trojan nodes with optimized Cloudflare IPs "
                "and serve Shadowrocket / Clash / Surge subscriptions over your "
                "LAN.",
            ),
        ],
    ),
]

for category, tools in CATEGORIES:
    st.subheader(category, divider="gray")
    for row in range(0, len(tools), 2):
        cols = st.columns(2, vertical_alignment="top")
        pair = tools[row : row + 2]
        for col, (path, title, description) in zip(cols, pair, strict=False):
            with col:
                with st.container(border=True):
                    st.page_link(path, label=title)
                    st.caption(description)
