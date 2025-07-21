raw = """

"""

if __name__ == "__main__":
    urls = raw.strip().splitlines()

    for idx, url in enumerate(urls):
        print(f"{idx+1}. magnet:?xt=urn:btih:{url}")

    print(f"\n{len(urls)} file(s) in total.")
