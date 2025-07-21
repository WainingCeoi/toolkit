import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from more_itertools import collapse

raw = """

"""


def get_magnet_link(url):
    try:
        # Send HTTP GET request
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Raise an exception for HTTP errors

        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find all <a> tags with href starting with "magnet:"
        magnet_links = [
            a['href'] for a in soup.find_all('a', href=True)
            if a['href'].startswith("magnet:")
        ]

        if magnet_links:
            return magnet_links
        else:
            return f"[NO LINK FOUND] {url}"

    except Exception as e:
        return f"[FAILED] {url} - {str(e)}"


if __name__ == "__main__":

    urls = raw.strip().splitlines()

    # run them in parallel
    with ThreadPoolExecutor() as executor:
        all_magnets = collapse(executor.map(get_magnet_link, urls))
    for idx, magnet in enumerate(all_magnets):
        print(f"{idx+1}. {magnet}")


    print(f"\n{len(urls)} file(s) in total.")
