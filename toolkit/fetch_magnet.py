import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv, set_key

# For manual input
raw = """

"""


# Return on page magnet by giving an url
def get_magnet_link(url):
    try:
        html_content = requests.get(url, timeout=10)
        soup = BeautifulSoup(html_content.text, "html.parser")
        magnet = soup.find("a", string="Magnet").get("href")
        
        return {"success": True, "result": magnet}

    except Exception:
        return {"success": False, "url": url}


if __name__ == "__main__":
    env_path = "./.env"
    
    if not raw.strip():
        # Get unwatched videos automatically
        
        # Get last run info.
        load_dotenv()
        cutoff_video_url = os.getenv("CUTOFF_VIDEO")
        source_website = os.getenv("WEBSITE_URL")
        
        # Initial parameters
        unwatched_video_urls = []
        page_idx = 1
        found = False

        # Get unwatched videos
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
                print(e)
                print("Urls collected so far:\n")
                for url in unwatched_video_urls:
                    print(url)
                print(f"Stopped at Page {page_idx}")
                break
        
        # Remove watched videos urls
        cutoff_idx = unwatched_video_urls.index(cutoff_video_url)
        unwatched_video_urls = unwatched_video_urls[:cutoff_idx]
        
        # Save latest video info.
        set_key(env_path, "CUTOFF_VIDEO", unwatched_video_urls[0])
    
    else:
        # Input unwatched video urls manually
        unwatched_video_urls = raw.strip().splitlines()
    
    
    # Fetch magnets simultaneously
    with ThreadPoolExecutor() as executor:
        results = list( executor.map(get_magnet_link, unwatched_video_urls))
    
    
    # Retrieve and print results
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    for item in successful:
        print(item["result"])

    if failed:
        print("\n--- Failed URLs ---")
        for item in failed:
            print(item["url"])
    
    print(f"\n{len(unwatched_video_urls)} in total. {len(successful)} ✅, {len(failed)} ❌")
    subprocess.run(["afplay", "/System/Library/Sounds/Hero.aiff"])
