import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from bs4 import BeautifulSoup
import json
import fitz
from PIL import Image
from io import BytesIO


def add_bookmark (pdf_url, input_pdf, output_pdf):
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
        doc.save(output_pdf)

    except Exception as e:
        print(e)
        pass


def fetch_pdf(pdf_url, file_name):
    # Set up Chrome options
    # chrome_options = Options()
    # chrome_options.add_argument("--window-size=1920,1080")
    # Set up the ChromeDriver service
    service = Service(ChromeDriverManager().install())

    # Initialize the WebDriver
    driver = webdriver.Chrome(service=service)

    driver.get(pdf_url)

    input("Scroll Down until All Pages are Loaded.\nThen Press enter to continue...")

    html_content = driver.page_source
    driver.quit()

    soup = BeautifulSoup(html_content, features="html.parser")
    img_urls = soup.select("img[class*=bi]")
    images = []

    for img_url in img_urls:
        img_url = img_url.get("src")
        img = requests.get(img_url, timeout=5).content
        img = Image.open(BytesIO(img))
        images.append(img)

    images[0].save(file_name, save_all=True, append_images=images[1:])
