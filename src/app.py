import streamlit as st

st.set_page_config(page_title="Toolkit", page_icon="🧰", layout="wide")

pages = [
    st.Page("home.py", title="Home", icon="🏠", default=True),
    st.Page("pages/magnet_scraper.py", title="Magnet Scraper", icon="🧲"),
    st.Page("pages/file_gatherer.py", title="File Gatherer", icon="📦"),
    st.Page("pages/remux_processor.py", title="Remux Processor", icon="🎬"),
    st.Page("pages/img_to_pdf.py", title="Image to PDF", icon="🖼️"),
    st.Page(
        "pages/optimized_ip_generator.py",
        title="Optimized-IP Subscription",
        icon="🛰️",
    ),
    st.Page("pages/cache_purge.py", title="Cache Purge", icon="🧹"),
    st.Page("pages/web_images_to_pdf.py", title="Web Images to PDF", icon="🌐"),
    st.Page("pages/doc_to_pdf.py", title="Doc to PDF", icon="📄"),
]

nav = st.navigation(pages)
st.sidebar.caption("🧰 Toolkit · Media & Files Utilities")
nav.run()
