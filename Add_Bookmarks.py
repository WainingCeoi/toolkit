import requests
from bs4 import BeautifulSoup
import json
import os
import fitz


input_folder = r"/Users/xuweining/Desktop/安装图集/PD"
output_folder = r"/Users/xuweining/Desktop/Done/PD"

files = os.scandir(input_folder)

for idx, file in enumerate(files):
    url = input(f"{idx+1}. {file.name}: ")
    try:
        web = requests.get(url, timeout=5).text
        soup = BeautifulSoup(web, "html.parser")
        contents = soup.find_all("a")
        toc = []

        for content in contents:
            level = len(content.find_parents("li"))
            page_number = json.loads(content.get("data-dest-detail"))[0]
            page_name = f"{content.text}({page_number})"
            toc.append([level, page_name, page_number])

        doc = fitz.open(file.path)
        doc.set_toc(toc)
        doc.save(f"{output_folder}/{file.name}")

    except Exception as e:
        print(e)
        pass
