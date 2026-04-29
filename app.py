from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from werkzeug.utils import secure_filename
from PIL import Image
import io
import uuid
import PyPDF2
import convertapi
import os
import re
import subprocess
import tempfile
import base64
from google import genai as google_genai
from google.genai import types as genai_types
from flask_sqlalchemy import SQLAlchemy

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

# Database configuration for Vercel compatibility
if os.environ.get('DATABASE_URL'):
    # Vercel Postgres usually provides DATABASE_URL
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL').replace("postgres://", "postgresql://", 1)
else:
    # Fallback for local development or Vercel ephemeral storage
    # On Vercel, /tmp/ is the only writable directory
    db_path = "/tmp/portal.db" if os.environ.get('VERCEL') else "portal.db"
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# User Model for Image-to-LaTeX
class LatexUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='pending') # 'pending' or 'approved'

# Initialize database
with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Database initialization warning: {e}")

# Health check for Vercel
@app.route('/health')
def health_check():
    return {"status": "healthy"}, 200

# In-memory storage for generated PDFs (UUID -> BytesIO)
pdf_storage = {}

# Admin's shared Gemini API key (set via env var)
ADMIN_GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')

ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
ALLOWED_PDF_EXTENSIONS = {'pdf'}
ALLOWED_DOC_EXTENSIONS = {'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}

def allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

def allowed_pdf(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_PDF_EXTENSIONS

def allowed_doc(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_DOC_EXTENSIONS

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

@app.route('/latex-register', methods=['GET', 'POST'])
def latex_register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()

        if not name or not username or not password:
            flash('All fields are required.', 'danger')
            return redirect(request.url)

        if LatexUser.query.filter_by(username=username).first():
            flash('Username already taken. Please choose another.', 'warning')
            return redirect(request.url)

        new_user = LatexUser(name=name, username=username, password=password)
        db.session.add(new_user)
        db.session.commit()
        return redirect(url_for('latex_waiting', username=username))

    return render_template('latex_register.html')


@app.route('/latex-waiting')
def latex_waiting():
    username = request.args.get('username', '')
    user = LatexUser.query.filter_by(username=username).first()
    status = user.status if user else 'unknown'
    return render_template('latex_waiting.html', username=username, status=status)


@app.route('/latex-login', methods=['GET', 'POST'])
def latex_login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = LatexUser.query.filter_by(username=username).first()
        if not user or user.password != password:
            flash('Invalid username or password.', 'danger')
            return redirect(request.url)
        if user.status == 'pending':
            return redirect(url_for('latex_waiting', username=username))
        if user.status == 'approved':
            from flask import session
            session['latex_user'] = username
            return redirect(url_for('image_to_latex'))
    return render_template('latex_login.html')


# --- Admin-only approval endpoint ---
# Access: /latex-admin?secret=ARYAN&action=approve&username=XYZ
@app.route('/latex-admin')
def latex_admin():
    secret = request.args.get('secret', '')
    if secret != 'ARYAN':
        return "Unauthorized", 403
    action = request.args.get('action', '')
    username = request.args.get('username', '')
    
    if action == 'approve' and username:
        user = LatexUser.query.filter_by(username=username).first()
        if user:
            user.status = 'approved'
            db.session.commit()
            return f"✅ {username} approved!", 200
            
    if action == 'list':
        users = LatexUser.query.all()
        rows = ''.join(
            f"<tr><td>{u.username}</td><td>{u.name}</td><td>{u.status}</td>"
            f"<td><a href='/latex-admin?secret=ARYAN&action=approve&username={u.username}'>Approve</a></td></tr>"
            for u in users
        )
        return f"<h2>Registered Users</h2><table border=1><tr><th>Username</th><th>Name</th><th>Status</th><th>Action</th></tr>{rows}</table>"
    return "No action taken.", 200


@app.route('/image-to-latex', methods=['GET', 'POST'])
def image_to_latex():
    if request.method == 'POST':
        if 'image' not in request.files:
            flash('No file part provided.', 'danger')
            return redirect(request.url)

        file = request.files['image']
        if file.filename == '':
            flash('No image selected.', 'danger')
            return redirect(request.url)

        if not allowed_image(file.filename):
            flash('Invalid file type. Please upload a JPG, PNG, or WEBP image.', 'danger')
            return redirect(request.url)

        # Check session/approval
        from flask import session
        latex_username = session.get('latex_user')
        user = LatexUser.query.filter_by(username=latex_username).first()
        if not latex_username or not user or user.status != 'approved':
            flash('You must be an approved BHU user to use this feature.', 'warning')
            return redirect(url_for('latex_register'))

        user_api_key = request.form.get('api_key', '').strip()
        final_api_key = user_api_key or ADMIN_GEMINI_KEY

        if not final_api_key:
            flash('Please provide a Gemini API key to continue.', 'danger')
            return redirect(request.url)

        try:
            image_bytes = file.read()

            # --- Step 1: Gemini Vision → LaTeX (using new google.genai SDK) ---
            client = google_genai.Client(api_key=final_api_key)
            prompt = (
                "You are an expert LaTeX typesetter. Look at this handwritten image of study notes or equations. "
                "Convert ALL visible handwriting, text, and mathematical expressions into a complete, compilable LaTeX document. "
                "Use the 'article' documentclass. Include packages: amsmath, amssymb, geometry (with margins=1in), fontenc (T1), inputenc (utf8). "
                "Preserve the structure and order of the content as closely as possible. "
                "Output ONLY the raw LaTeX code, starting with \\documentclass and ending with \\end{document}. No explanations."
            )
            import PIL.Image as PILImage
            pil_img = PILImage.open(io.BytesIO(image_bytes))
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=[prompt, pil_img]
            )
            raw_latex = response.text

            # --- Step 2: Clean & extract LaTeX ---
            # Strip markdown code fences if present
            raw_latex = re.sub(r'^```(?:latex)?\n?', '', raw_latex, flags=re.IGNORECASE)
            raw_latex = re.sub(r'```$', '', raw_latex.strip())
            raw_latex = raw_latex.strip()

            # If Gemini returned only the body, wrap it
            if not raw_latex.startswith('\\documentclass'):
                raw_latex = (
                    '\\documentclass{article}\n'
                    '\\usepackage[utf8]{inputenc}\n'
                    '\\usepackage[T1]{fontenc}\n'
                    '\\usepackage{amsmath,amssymb}\n'
                    '\\usepackage[margin=1in]{geometry}\n'
                    '\\begin{document}\n'
                    + raw_latex +
                    '\n\\end{document}\n'
                )

            # --- Step 3: Compile LaTeX → PDF ---
            with tempfile.TemporaryDirectory() as tmpdir:
                tex_path = os.path.join(tmpdir, 'output.tex')
                pdf_path = os.path.join(tmpdir, 'output.pdf')

                with open(tex_path, 'w', encoding='utf-8') as f:
                    f.write(raw_latex)

                result = subprocess.run(
                    ['pdflatex', '-interaction=nonstopmode', '-output-directory', tmpdir, tex_path],
                    capture_output=True, text=True, timeout=60
                )

                if not os.path.exists(pdf_path):
                    # Compilation failed — serve the raw LaTeX as a .tex download instead
                    flash('LaTeX was generated but PDF compilation failed (pdflatex not installed). Downloading the .tex file instead.', 'warning')
                    tex_bytes = io.BytesIO(raw_latex.encode('utf-8'))
                    tex_bytes.seek(0)
                    return send_file(tex_bytes, mimetype='application/x-tex',
                                     as_attachment=True, download_name='converted_notes.tex')

                with open(pdf_path, 'rb') as f:
                    pdf_bytes = io.BytesIO(f.read())
                pdf_bytes.seek(0)

            file_id = str(uuid.uuid4())
            pdf_storage[file_id] = {
                'data': pdf_bytes,
                'name': 'converted_notes.pdf'
            }
            return redirect(url_for('download_page', file_id=file_id))

        except subprocess.TimeoutExpired:
            flash('PDF compilation timed out. Please try a simpler image.', 'danger')
            return redirect(request.url)
        except Exception as e:
            flash(f'Error processing image: {str(e)}', 'danger')
            return redirect(request.url)

    return render_template('image_to_latex.html')


@app.errorhandler(413)
def request_entity_too_large(error):
    flash('File(s) exceed the maximum allowed size of 50MB.', 'danger')
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=True, port=5001)
