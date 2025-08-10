import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup

import json
import fitz
from PIL import Image
from io import BytesIO
import os



def add_bookmark (pdf_url, input_pdf, output_pdf=None):
    try:
        html_content = requests.get(pdf_url, timeout=5).text
        soup = BeautifulSoup(html_content, "html.parser")
        html_content = soup.find_all("a")
        toc = []

        for content in html_content:
            level = len(content.find_parents("li"))
            page_number = json.loads(content.get("data-dest-detail"))[0]
            page_name = f"{content.text}({page_number})"
            toc.append([level, page_name, page_number])

        doc = fitz.open(input_pdf)
        doc.set_toc(toc)
        if output_pdf:
            doc.save(output_pdf)
        else:
            doc.save(input_pdf, incremental=True)

    except Exception as e:
        print(e)
        pass


def fetch_pdf(pdf_url, output_folder):
    # Set up the ChromeDriver service
    service = Service(ChromeDriverManager().install())

    # Initialize the WebDriver
    driver = webdriver.Chrome(service=service)

    driver.get(pdf_url)

    input("Scroll Down until All Pages are Loaded.\nThen Press enter to continue...")

    html_content = driver.page_source
    driver.quit()

    soup = BeautifulSoup(html_content, features="html.parser")
    pdf_name = f"{soup.find("title").text}.pdf"
    img_urls = soup.select("img[class*=bi]")
    images = []

    for img_url in img_urls:
        img_url = img_url.get("src")
        img = requests.get(img_url, timeout=5).content
        img = Image.open(BytesIO(img))
        images.append(img)

    pdf_path = os.path.join(output_folder, pdf_name)
    images[0].save(pdf_path, save_all=True, append_images=images[1:])

    add_bookmark(pdf_url, pdf_path)
