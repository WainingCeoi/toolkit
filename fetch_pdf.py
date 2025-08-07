from ToolFunc.fetchor import fetch_pdf, add_bookmark


url = "https://ebook.chinabuilding.com.cn/zbooklib/bookpdf/probation?SiteID=1&bookID=59444"
pdf_name = "/Users/xuweining/Desktop/1.pdf"


if __name__ == "__main__":
    fetch_pdf(url, pdf_name)
    add_bookmark(url, pdf_name, pdf_name)
