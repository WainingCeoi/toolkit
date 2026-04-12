import os
import win32com.client
from pathlib import Path
import pypandoc


def convert_word_to_pdf(input_file, output_folder, create_bookmarks=0):
    # Single Word file convertion function, store converted file to specific folder
    try:
        word_app = win32com.client.Dispatch("Word.Application")
        word_app.Visible = False

        pdf_name = Path(input_file).with_suffix(".pdf").name
        pdf_path = os.path.join(output_folder, pdf_name)
        doc = word_app.Documents.Open(
            FileName=input_file,
            AddToRecentFiles =False
        )

        # Accept all revision and delete all ink annotations.
        # Comments won't be included during conversion.
        '''Using "AcceptAllRevisions()" function might caught word crash and resulting no output'''
        # doc.AcceptAllRevisions()
        doc.ActiveWindow.View.RevisionsMode = 0

        # convert to PDF
        doc.ExportAsFixedFormat2(
            OutputFileName=pdf_path,
            ExportFormat=17, # PDF format
            CreateBookmarks=create_bookmarks, # 0 for no bookmarks, 1 for basic bookmark
            IncludeDocProps=True
        )
        print(f"File \"{pdf_name}\" Created")

    except Exception as e:
        print(e)

    finally:
        if word_app:
            word_app.Quit(SaveChanges=False)
            del word_app




def convert_md_to_word_and_pdf(input_file, word_output_folder, pdf_out_folder, create_bookmarks):
    # Single Markdown file convertion function, store converted file to specific folder(s).
    try:
        ''' 
        Convert Markdown file to Word
        Pandoc and above function (convert_word_to_pdf) required 
        '''
        word_name = Path(input_file).with_suffix(".docx").name
        word_file = os.path.join(word_output_folder, word_name)
        pypandoc.convert_file(input_file, "docx", outputfile=word_file)
        print(f"File \"{word_name}\" Created.")
        # Convert Word to PDF
        convert_word_to_pdf(word_file, pdf_out_folder, create_bookmarks)

    except Exception as e:
        print(e)
