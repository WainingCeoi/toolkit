import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor


raw = """

"""


def get_magnet_link(url):
    try:
        # Send HTTP GET request
        html_content = requests.get(url, timeout=5)

        # Parse the HTML content
        soup = BeautifulSoup(html_content.text, 'html.parser')

        magnets = soup.find_all("a", href=lambda href: href and href.startswith("magnet"))
        for magnet in magnets:
            return magnet.get("href")

    except Exception as e:
        print(e)


if __name__ == "__main__":
    urls = raw.strip().splitlines()

    # run them in parallel
    with ThreadPoolExecutor() as executor:
        all_magnets = executor.map(get_magnet_link, urls)
    for idx, magnet in enumerate(all_magnets):
        print(f"{idx+1}. {magnet}")

    print(f"\n{len(urls)} file(s) in total.")
