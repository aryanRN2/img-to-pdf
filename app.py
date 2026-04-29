from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from werkzeug.utils import secure_filename
from PIL import Image
import io
import uuid
import PyPDF2
import convertapi
import os

convertapi.api_secret = os.environ.get('CONVERTAPI_SECRET', 'your_secret_here')

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
ALLOWED_PDF_EXTENSIONS = {'pdf'}
ALLOWED_DOC_EXTENSIONS = {'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}

def allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

def allowed_pdf(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_PDF_EXTENSIONS

def allowed_doc(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_DOC_EXTENSIONS


app = Flask(__name__)
# In production, use a secure random key like os.urandom(24)
app.secret_key = "super_secret_key_for_portal" 
# 50 MB limit to prevent DoS attacks
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 

# In-memory storage for generated PDFs (UUID -> BytesIO)
pdf_storage = {}

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
ALLOWED_PDF_EXTENSIONS = {'pdf'}

def allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

def allowed_pdf(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_PDF_EXTENSIONS

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/image-to-pdf', methods=['GET', 'POST'])
def image_to_pdf():
    if request.method == 'POST':
        if 'images' not in request.files:
            flash('No file part provided.', 'danger')
            return redirect(request.url)
        
        files = request.files.getlist('images')
        if not files or files[0].filename == '':
            flash('No selected files.', 'danger')
            return redirect(request.url)
        
        images_list = []
        for file in files:
            if file and allowed_image(file.filename):
                try:
                    img_bytes = file.read()
                    img = Image.open(io.BytesIO(img_bytes))
                    
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        alpha = img.convert('RGBA').split()[-1]
                        bg = Image.new("RGB", img.size, (255, 255, 255))
                        bg.paste(img, mask=alpha)
                        img = bg
                    elif img.mode != 'RGB':
                        img = img.convert('RGB')
                        
                    images_list.append(img)
                except Exception as e:
                    flash(f'Error processing file {file.filename}: {str(e)}', 'danger')
                    return redirect(request.url)
            else:
                flash(f'Invalid file type: {file.filename}', 'danger')
                return redirect(request.url)
        
        if images_list:
            try:
                pdf_bytes = io.BytesIO()
                images_list[0].save(pdf_bytes, format='PDF', save_all=True, append_images=images_list[1:])
                pdf_bytes.seek(0)
                
                # Store in memory and redirect
                file_id = str(uuid.uuid4())
                pdf_storage[file_id] = {
                    'data': pdf_bytes,
                    'name': 'converted_images.pdf'
                }
                return redirect(url_for('download_page', file_id=file_id))
            except Exception as e:
                flash(f'Error generating PDF: {str(e)}', 'danger')
                return redirect(request.url)
                
    return render_template('image_to_pdf.html')

@app.route('/merge-pdf', methods=['GET', 'POST'])
def merge_pdf():
    if request.method == 'POST':
        if 'pdfs' not in request.files:
            flash('No file part provided.', 'danger')
            return redirect(request.url)
        
        files = request.files.getlist('pdfs')
        if not files or files[0].filename == '':
            flash('No selected files.', 'danger')
            return redirect(request.url)
        
        merger = PyPDF2.PdfMerger()
        
        try:
            for file in files:
                if file and allowed_pdf(file.filename):
                    # Read into memory first to avoid closing the stream prematurely
                    pdf_stream = io.BytesIO(file.read())
                    merger.append(pdf_stream)
                else:
                    flash(f'Invalid file type: {file.filename}', 'danger')
                    return redirect(request.url)
            
            output_bytes = io.BytesIO()
            merger.write(output_bytes)
            merger.close()
            output_bytes.seek(0)
            
            file_id = str(uuid.uuid4())
            pdf_storage[file_id] = {
                'data': output_bytes,
                'name': 'merged_documents.pdf'
            }
            return redirect(url_for('download_page', file_id=file_id))
            
        except Exception as e:
            flash(f'Error merging PDFs: {str(e)}', 'danger')
            return redirect(request.url)
            
    return render_template('merge_pdf.html')

@app.route('/download/<file_id>')
def download_page(file_id):
    if file_id not in pdf_storage:
        flash('File not found or has expired.', 'warning')
        return redirect(url_for('home'))
    return render_template('download.html', file_id=file_id)

@app.route('/document-to-pdf', methods=['GET', 'POST'])
def document_to_pdf():
    if request.method == 'POST':
        if 'document' not in request.files:
            flash('No file part provided.', 'danger')
            return redirect(request.url)
        
        file = request.files['document']
        if file.filename == '':
            flash('No selected file.', 'danger')
            return redirect(request.url)
            
        if file and allowed_doc(file.filename):
            try:
                # Process entirely in memory
                upload_io = io.BytesIO(file.read())
                upload_io.name = file.filename # ConvertAPI needs filename to determine format
                ext = file.filename.rsplit('.', 1)[1].lower()
                
                result = convertapi.convert('pdf', { 'File': upload_io }, from_format=ext)
                
                pdf_bytes = io.BytesIO()
                result.file.save(pdf_bytes)
                pdf_bytes.seek(0)
                
                file_id = str(uuid.uuid4())
                pdf_storage[file_id] = {
                    'data': pdf_bytes,
                    'name': f"{file.filename.rsplit('.', 1)[0]}.pdf"
                }
                return redirect(url_for('download_page', file_id=file_id))
                
            except Exception as e:
                flash(f'Error converting document: {str(e)}. Please check your ConvertAPI configuration.', 'danger')
                return redirect(request.url)
        else:
            flash('Invalid file format. Supported: DOCX, XLSX, PPTX', 'danger')
            return redirect(request.url)
            
    return render_template('document_to_pdf.html')

@app.route('/get-file/<file_id>')
def get_file(file_id):
    if file_id not in pdf_storage:
        return "File not found", 404
    
    file_info = pdf_storage[file_id]
    return send_file(
        file_info['data'],
        mimetype='application/pdf',
        as_attachment=True,
        download_name=file_info['name']
    )

@app.errorhandler(413)
def request_entity_too_large(error):
    flash('File(s) exceed the maximum allowed size of 50MB.', 'danger')
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=True, port=5002)
