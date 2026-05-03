from flask import Flask, render_template, request, send_file, flash, redirect, url_for, session
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from PIL import Image
import PIL.Image as PILImage
import io
import uuid
import PyPDF2
import convertapi
import os
import re
import subprocess
import tempfile
import base64
import time
import threading
from google import genai as google_genai
from google.genai import types as genai_types
from flask_sqlalchemy import SQLAlchemy
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception, RetryError

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

# Database configuration for Vercel/Supabase stability
db_url = os.environ.get('SUPABASE_DB_URL') or os.environ.get('DATABASE_URL')

if db_url:
    # Ensure protocol is postgresql:// for SQLAlchemy
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    # Essential for Vercel/Supabase stability: 
    # Use SSL and handle connection pooling correctly for serverless
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "connect_args": {"sslmode": "require"},
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }
else:
    # Fallback for local development
    db_path = "/tmp/portal.db" if os.environ.get('VERCEL') else "portal.db"
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'latex_login'
login_manager.login_message_category = 'warning'

@login_manager.user_loader
def load_user(user_id):
    return LatexUser.query.get(int(user_id))

# User Model for Image-to-LaTeX
class LatexUser(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
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
# In-memory storage for job statuses
job_storage = {}

# Rate limit lock for Gemini
gemini_lock = threading.Lock()
last_gemini_time = 0.0
GEMINI_MIN_INTERVAL = 5.0 # Max 12 requests per minute

# Admin's shared API keys (set via env var)
ADMIN_GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')
ADMIN_NVIDIA_KEY = os.environ.get('NVIDIA_API_KEY', '')

# In-memory storage for images (UUID -> bytes)
image_storage = {}

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
        
        file_data = []
        for file in files:
            if file and allowed_image(file.filename):
                file_data.append({
                    'name': file.filename,
                    'bytes': file.read()
                })
            else:
                flash(f'Invalid file type: {file.filename}', 'danger')
                return redirect(request.url)

        if not file_data:
            flash('No valid images uploaded.', 'danger')
            return redirect(request.url)

        job_id = str(uuid.uuid4())
        job_storage[job_id] = {
            'status': 'queued',
            'current_step': 0,
            'total_steps': 3,
            'steps': ['Uploading Images', 'Processing Format', 'Generating PDF'],
            'message': 'Starting conversion...',
            'error': None,
            'file_id': None
        }

        # Start background thread
        thread = threading.Thread(target=process_image_to_pdf_task, args=(job_id, file_data))
        thread.start()

        return redirect(url_for('processing_page', job_id=job_id))
                
    return render_template('image_to_pdf.html')

def process_image_to_pdf_task(job_id, file_data):
    try:
        job = job_storage[job_id]
        job['current_step'] = 1
        job['message'] = f'Processing {len(file_data)} images...'
        
        images_list = []
        for item in file_data:
            try:
                img = Image.open(io.BytesIO(item['bytes']))
                if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                    alpha = img.convert('RGBA').split()[-1]
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    bg.paste(img, mask=alpha)
                    img = bg
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                images_list.append(img)
            except Exception as e:
                job['error'] = f"Error processing {item['name']}: {str(e)}"
                return

        job['current_step'] = 2
        job['message'] = 'Merging images into PDF document...'
        
        pdf_bytes = io.BytesIO()
        images_list[0].save(pdf_bytes, format='PDF', save_all=True, append_images=images_list[1:])
        pdf_bytes.seek(0)
        
        file_id = str(uuid.uuid4())
        pdf_storage[file_id] = {
            'data': pdf_bytes,
            'name': 'converted_images.pdf'
        }
        
        job['file_id'] = file_id
        job['current_step'] = 3
        job['status'] = 'finished'
        job['message'] = 'Success! Your PDF is ready.'
    except Exception as e:
        job_storage[job_id]['error'] = str(e)

@app.route('/processing/<job_id>')
def processing_page(job_id):
    if job_id not in job_storage:
        flash('Invalid or expired job session.', 'danger')
        return redirect(url_for('home'))
    return render_template('processing.html', job_id=job_id)

@app.route('/api/job-status/<job_id>')
def job_status_api(job_id):
    if job_id not in job_storage:
        return {"error": "Job not found"}, 404
    return job_storage[job_id]

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
        
        file_data = []
        for file in files:
            if file and allowed_pdf(file.filename):
                file_data.append({
                    'name': file.filename,
                    'bytes': file.read()
                })
            else:
                flash(f'Invalid file type: {file.filename}', 'danger')
                return redirect(request.url)

        job_id = str(uuid.uuid4())
        job_storage[job_id] = {
            'status': 'queued',
            'current_step': 0,
            'total_steps': 2,
            'steps': ['Uploading Files', 'Merging Documents'],
            'message': 'Starting merge process...',
            'error': None,
            'file_id': None
        }

        thread = threading.Thread(target=process_merge_pdf_task, args=(job_id, file_data))
        thread.start()

        return redirect(url_for('processing_page', job_id=job_id))
            
    return render_template('merge_pdf.html')

def process_merge_pdf_task(job_id, file_data):
    try:
        job = job_storage[job_id]
        job['current_step'] = 1
        job['message'] = f'Merging {len(file_data)} PDF files...'
        
        merger = PyPDF2.PdfMerger()
        for item in file_data:
            pdf_stream = io.BytesIO(item['bytes'])
            merger.append(pdf_stream)
        
        output_bytes = io.BytesIO()
        merger.write(output_bytes)
        merger.close()
        output_bytes.seek(0)
        
        file_id = str(uuid.uuid4())
        pdf_storage[file_id] = {
            'data': output_bytes,
            'name': 'merged_documents.pdf'
        }
        
        job['file_id'] = file_id
        job['current_step'] = 2
        job['status'] = 'finished'
        job['message'] = 'Success! Your documents have been merged.'
    except Exception as e:
        job_storage[job_id]['error'] = f"Merge failed: {str(e)}"

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
        
        if not allowed_doc(file.filename):
            flash('Invalid file format. Supported: DOC, DOCX, XLS, XLSX, PPT, PPTX', 'danger')
            return redirect(request.url)

        job_id = str(uuid.uuid4())
        job_storage[job_id] = {
            'status': 'queued',
            'current_step': 0,
            'total_steps': 2,
            'steps': ['Uploading Document', 'Converting via API'],
            'message': 'Starting conversion...',
            'error': None,
            'file_id': None
        }

        file_bytes = file.read()
        filename = file.filename
        thread = threading.Thread(target=process_document_to_pdf_task, args=(job_id, filename, file_bytes))
        thread.start()

        return redirect(url_for('processing_page', job_id=job_id))
                
    return render_template('document_to_pdf.html')

def process_document_to_pdf_task(job_id, filename, file_bytes):
    try:
        job = job_storage[job_id]
        job['current_step'] = 1
        job['message'] = 'Connecting to ConvertAPI for professional conversion...'
        
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, secure_filename(filename))
            with open(input_path, 'wb') as f:
                f.write(file_bytes)
            
            file_ext = filename.rsplit('.', 1)[1].lower()
            result = convertapi.convert('pdf', { 'File': input_path }, from_format = file_ext)
            
            output_bytes = io.BytesIO()
            output_bytes.write(result.file.url_to_bytes())
            output_bytes.seek(0)
            
            file_id = str(uuid.uuid4())
            pdf_storage[file_id] = {
                'data': output_bytes,
                'name': filename.rsplit('.', 1)[0] + '.pdf'
            }
            
            job['file_id'] = file_id
            job['current_step'] = 2
            job['status'] = 'finished'
            job['message'] = 'Success! Your document is converted.'
    except Exception as e:
        job_storage[job_id]['error'] = f"Conversion failed: {str(e)}"

@app.route('/pdf-to-image', methods=['GET', 'POST'])
def pdf_to_image():
    if request.method == 'POST':
        if 'pdf' not in request.files:
            flash('No file part provided.', 'danger')
            return redirect(request.url)
        
        file = request.files['pdf']
        if file.filename == '':
            flash('No selected file.', 'danger')
            return redirect(request.url)
        
        if not allowed_pdf(file.filename):
            flash('Invalid file format. Please upload a PDF.', 'danger')
            return redirect(request.url)

        job_id = str(uuid.uuid4())
        job_storage[job_id] = {
            'status': 'queued',
            'current_step': 0,
            'total_steps': 2,
            'steps': ['Uploading PDF', 'Converting to Images'],
            'message': 'Starting conversion...',
            'error': None,
            'file_id': None
        }

        file_bytes = file.read()
        filename = file.filename
        thread = threading.Thread(target=process_pdf_to_image_task, args=(job_id, filename, file_bytes))
        thread.start()

        return redirect(url_for('processing_page', job_id=job_id))
                
    return render_template('pdf_to_image.html')

def process_pdf_to_image_task(job_id, filename, file_bytes):
    try:
        job = job_storage[job_id]
        job['current_step'] = 1
        job['message'] = 'Connecting to ConvertAPI for high-quality image extraction...'
        
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, secure_filename(filename))
            with open(input_path, 'wb') as f:
                f.write(file_bytes)
            
            # Convert PDF to JPG. ConvertAPI returns a ZIP if multiple images are generated.
            result = convertapi.convert('jpg', { 'File': input_path }, from_format = 'pdf')
            
            output_bytes = io.BytesIO()
            output_bytes.write(result.file.url_to_bytes())
            output_bytes.seek(0)
            
            file_id = str(uuid.uuid4())
            
            # If the result is a single image, name it .jpg, otherwise it's likely a .zip from ConvertAPI
            output_filename = filename.rsplit('.', 1)[0] + '.zip'
            mimetype = 'application/zip'
            
            # Check if it's actually a single image or multiple
            if result.file.filename.lower().endswith('.jpg'):
                output_filename = filename.rsplit('.', 1)[0] + '.jpg'
                mimetype = 'image/jpeg'

            pdf_storage[file_id] = {
                'data': output_bytes,
                'name': output_filename,
                'mimetype': mimetype
            }
            
            job['file_id'] = file_id
            job['current_step'] = 2
            job['status'] = 'finished'
            job['message'] = 'Success! Your images are ready.'
    except Exception as e:
        job_storage[job_id]['error'] = f"Conversion failed: {str(e)}"

@app.route('/download/<file_id>')
def download_page(file_id):
    if file_id not in pdf_storage:
        flash('File not found or has expired.', 'warning')
        return redirect(url_for('home'))
    return render_template('download.html', file_id=file_id)

@app.route('/get-file/<file_id>')
def get_file(file_id):
    if file_id not in pdf_storage:
        return "File not found", 404
    
    file_info = pdf_storage[file_id]
    return send_file(
        file_info['data'],
        mimetype=file_info.get('mimetype', 'application/pdf'),
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

        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = LatexUser(name=name, username=username, password=hashed_password)
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
        
        if not user:
            flash('Invalid username or password.', 'danger')
            return redirect(request.url)
            
        if not check_password_hash(user.password, password):
            # Fallback for old plaintext passwords (useful during transition)
            if user.password == password:
                user.password = generate_password_hash(password, method='pbkdf2:sha256')
                db.session.commit()
            else:
                flash('Invalid username or password.', 'danger')
                return redirect(request.url)

        if user.status == 'pending':
            return redirect(url_for('latex_waiting', username=username))
        if user.status == 'approved':
            login_user(user)
            return redirect(url_for('image_to_latex'))
    return render_template('latex_login.html')

@app.route('/latex-logout')
@login_required
def latex_logout():
    logout_user()
    from flask import session
    session.pop('latex_api_key', None)
    session.pop('latex_model', None)
    flash('You have been logged out.', 'info')
    return redirect(url_for('home'))

@app.route('/latex-admin')
def latex_admin():
    secret = request.args.get('secret', '')
    if secret != 'ARYAN':
        return "Unauthorized", 403
    action = request.args.get('action', 'list') # Default to 'list'
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

@app.route('/image-to-latex')
@login_required
def image_to_latex():
    if current_user.status != 'approved':
        flash('You must be an approved BHU user to use this feature.', 'warning')
        return redirect(url_for('latex_waiting', username=current_user.username))

    # Force the premium RN-Vision-Transformer-200B engine
    if not ADMIN_NVIDIA_KEY:
        flash('The Premium Engine is currently offline (Key missing).', 'danger')
        return redirect(url_for('home'))
        
    session['latex_api_key'] = ADMIN_NVIDIA_KEY
    session['latex_model'] = "meta/llama-3.2-90b-vision-instruct"
    session['latex_provider'] = 'nvidia'
    
    return redirect(url_for('image_to_latex_upload'))

@app.route('/image-to-latex-upload', methods=['GET', 'POST'])
@login_required
def image_to_latex_upload():
    from flask import session
    if current_user.status != 'approved':
        flash('You must be an approved BHU user to use this feature.', 'warning')
        return redirect(url_for('latex_waiting', username=current_user.username))
        
    if 'latex_api_key' not in session or 'latex_model' not in session:
        flash('Please verify your API key first.', 'warning')
        return redirect(url_for('image_to_latex'))
        
    api_key = session['latex_api_key']
    model_name = session['latex_model']

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

        job_id = str(uuid.uuid4())
        image_id = str(uuid.uuid4())
        image_data = file.read()
        image_storage[image_id] = image_data

        job_storage[job_id] = {
            'status': 'queued',
            'current_step': 0,
            'total_steps': 4,
            'steps': ['Extracting Structured Text', 'Awaiting Text Review', 'Converting to LaTeX', 'Awaiting Compilation'],
            'message': f'Initializing {model_name}...',
            'error': None,
            'file_id': None,
            'extracted_text': None,
            'extracted_latex': None,
            'image_id': image_id,
            'type': 'image_to_latex'
        }

        api_key = session['latex_api_key']
        model_name = session['latex_model']
        provider = session.get('latex_provider', 'gemini')

        thread = threading.Thread(target=process_image_to_text_task, args=(job_id, image_data, api_key, model_name, provider))
        thread.start()
        return redirect(url_for('processing_page', job_id=job_id))
                
    return render_template('image_to_latex_upload.html', model_name=model_name)

def process_image_to_text_task(job_id, image_bytes, api_key, model_name, provider='gemini'):
    try:
        from langchain_core.messages import HumanMessage
        from langchain_core.output_parsers import StrOutputParser
        
        job = job_storage[job_id]
        job['current_step'] = 0
        job['message'] = 'RN-Vision-Transformer-200B is performing deep architectural analysis...'
        
        # Instantiate LLM
        if provider == 'nvidia':
            from langchain_nvidia_ai_endpoints import ChatNVIDIA
            vision_llm = ChatNVIDIA(model=model_name, nvidia_api_key=api_key, temperature=0.1)
        else:
            # Using google-genai SDK directly
            pass

        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        vision_prompt = (
            "You are an expert academic scribe. Your task is to extract all content from this handwritten image with absolute fidelity to the logical structure.\n\n"
            "CRITICAL: Capture all mathematical symbols (summations, limits, integrals, fractions, etc.) using standard LaTeX notation (e.g., use \\sum_{k=0}^{\\infty} for summations).\n\n"
            "Smartly identify:\n"
            "1. Theorems and their corresponding Proofs (maintain the logical link).\n"
            "2. Mathematical derivations, ensuring every step and every symbol is captured perfectly.\n"
            "3. Definitions and Examples.\n"
            "4. Page structure (headings, bullet points, numbered lists).\n\n"
            "Output the result in clearly structured plain text. Use [Theorem], [Proof], [Definition] markers to indicate sections. "
            "Ensure no content from the page is missed."
        )

        vision_message = HumanMessage(
            content=[
                {"type": "text", "text": vision_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        )

        def call_vision():
            if provider == 'nvidia':
                res = vision_llm.invoke([vision_message])
                return res.content
            else:
                from google import genai
                from google.genai import types
                client = genai.Client(api_key=api_key)
                response = client.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=[
                        vision_prompt,
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
                    ]
                )
                return response.text

        extracted_text = call_vision()
        job['extracted_text'] = extracted_text
        job['current_step'] = 1
        job['status'] = 'requires_text_review'
        job['message'] = 'Extraction complete. Please review the text.'
    except Exception as e:
        job_storage[job_id]['error'] = str(e)

@app.route('/review-text/<job_id>', methods=['GET', 'POST'])
def review_text(job_id):
    if job_id not in job_storage: return redirect(url_for('home'))
    job = job_storage[job_id]
    
    if request.method == 'POST':
        edited_text = request.form.get('text_content', '')
        job['extracted_text'] = edited_text
        job['status'] = 'queued'
        job['current_step'] = 2
        job['message'] = 'Converting reviewed text to LaTeX...'
        
        api_key = session.get('latex_api_key')
        model_name = session.get('latex_model')
        provider = session.get('latex_provider', 'gemini')
        
        thread = threading.Thread(target=process_text_to_latex_task, args=(job_id, edited_text, api_key, model_name, provider))
        thread.start()
        return redirect(url_for('processing_page', job_id=job_id))
        
    return render_template('review_text.html', job_id=job_id, text_content=job['extracted_text'], image_id=job.get('image_id'))

def process_text_to_latex_task(job_id, text_content, api_key, model_name, provider='gemini'):
    try:
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser
        
        job = job_storage[job_id]
        job['current_step'] = 2
        job['message'] = 'RN-Vision-Transformer-200B is synthesizing LaTeX code...'
        if provider == 'nvidia':
            from langchain_nvidia_ai_endpoints import ChatNVIDIA
            llm = ChatNVIDIA(model="meta/llama-3.1-8b-instruct", nvidia_api_key=api_key, temperature=0.1)
        else:
            # Using google-genai SDK directly
            pass

        def call_llm():
            if provider == 'nvidia':
                prompt = ChatPromptTemplate.from_messages([
                    ("system", (
                        "You are a professional LaTeX typesetter. Convert the provided academic text into a beautiful, compilable LaTeX document.\n\n"
                        "CRITICAL: Preserve and properly typeset all mathematical symbols (summations, limits, integrals, matrices, etc.) provided in the text. "
                        "The text already contains LaTeX-style math notation (e.g., \\sum) - use these to create high-quality mathematical environments.\n\n"
                        "Guidelines:\n"
                        "1. Use 'article' documentclass with: amsmath, amssymb, amsthm, geometry, fontenc(T1), inputenc(utf8).\n"
                        "2. Use 'theorem' and 'proof' environments for identified sections.\n"
                        "3. Use 'align*' or 'equation' environments for multi-step derivations.\n"
                        "4. Output ONLY raw LaTeX code starting with \\documentclass."
                    )),
                    ("user", "{text}")
                ])
                chain = prompt | llm | StrOutputParser()
                return chain.invoke({"text": text_content})
            else:
                from google import genai
                client = genai.Client(api_key=api_key)
                sys_instruct = (
                    "You are a professional LaTeX typesetter. Convert the provided academic text into a beautiful, compilable LaTeX document.\n\n"
                    "CRITICAL: Preserve and properly typeset all mathematical symbols (summations, limits, integrals, matrices, etc.) provided in the text.\n"
                    "Guidelines:\n"
                    "1. Use 'article' documentclass with: amsmath, amssymb, amsthm, geometry, fontenc(T1), inputenc(utf8).\n"
                    "2. Use 'theorem' and 'proof' environments for identified sections.\n"
                    "3. Use 'align*' or 'equation' environments for multi-step derivations.\n"
                    "4. Output ONLY raw LaTeX code starting with \\documentclass."
                )
                response = client.models.generate_content(
                    model="gemini-1.5-flash",
                    config=types.GenerateContentConfig(system_instruction=sys_instruct),
                    contents=[text_content]
                )
                return response.text

        latex_code = call_llm()
        
        # Clean markdown
        latex_code = re.sub(r'^```(?:latex)?\n?', '', latex_code, flags=re.IGNORECASE)
        latex_code = re.sub(r'```$', '', latex_code.strip())

        job['extracted_latex'] = latex_code
        job['current_step'] = 3
        job['status'] = 'requires_latex_review'
        job['message'] = 'LaTeX conversion complete. Final review required.'
    except Exception as e:
        job_storage[job_id]['error'] = str(e)

@app.route('/review-latex/<job_id>', methods=['GET', 'POST'])
def review_latex(job_id):
    if job_id not in job_storage: return redirect(url_for('home'))
    job = job_storage[job_id]
    
    if request.method == 'POST':
        edited_latex = request.form.get('latex_code', '')
        job['extracted_latex'] = edited_latex
        job['status'] = 'queued'
        job['current_step'] = 3
        job['message'] = 'Compiling final PDF...'
        
        thread = threading.Thread(target=process_compile_latex_task, args=(job_id, edited_latex))
        thread.start()
        return redirect(url_for('processing_page', job_id=job_id))
        
    return render_template('review_latex.html', job_id=job_id, latex_code=job['extracted_latex'])

@app.route('/view-image/<image_id>')
def view_image(image_id):
    if image_id not in image_storage: return "Image not found", 404
    return send_file(io.BytesIO(image_storage[image_id]), mimetype='image/jpeg')

@app.route('/edit-latex/<job_id>', methods=['GET', 'POST'])
def edit_latex(job_id):
    if job_id not in job_storage:
        flash('Invalid or expired job session.', 'danger')
        return redirect(url_for('home'))
        
    job = job_storage[job_id]
    
    if request.method == 'POST':
        edited_latex = request.form.get('latex_code', '')
        
        job['extracted_latex'] = edited_latex
        job['status'] = 'queued'
        job['current_step'] = 0
        job['total_steps'] = 2
        job['steps'] = ['Compiling PDF', 'Finalizing Results']
        job['message'] = 'Compiling LaTeX to PDF...'
        
        thread = threading.Thread(target=process_compile_latex_task, args=(job_id, edited_latex))
        thread.start()
        
        return redirect(url_for('processing_page', job_id=job_id))
        
    return render_template('edit_latex.html', job_id=job_id, latex_code=job.get('extracted_latex', ''))

def process_compile_latex_task(job_id, latex_code):
    try:
        job = job_storage[job_id]
        job['current_step'] = 3
        job['message'] = 'Compiling LaTeX to PDF (this may take a minute)...'
        
        pdf_output_bytes = None
        
        # Strategy 1: Try local pdflatex (Local Dev)
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                tex_path = os.path.join(tmpdir, 'output.tex')
                pdf_path = os.path.join(tmpdir, 'output.pdf')
                with open(tex_path, 'w', encoding='utf-8') as f:
                    f.write(latex_code)
                subprocess.run(['pdflatex', '-interaction=nonstopmode', '-output-directory', tmpdir, tex_path],
                             capture_output=True, text=True, timeout=60)
                if os.path.exists(pdf_path):
                    with open(pdf_path, 'rb') as f:
                        pdf_output_bytes = io.BytesIO(f.read())
        except Exception:
            pass # Fallback to Strategy 2

        # Strategy 2: ConvertAPI (Cloud / Vercel)
        if not pdf_output_bytes:
            import convertapi
            secret = os.environ.get('CONVERTAPI_SECRET')
            if not secret or secret == 'your_convertapi_secret_here':
                job['error'] = "Cloud PDF compilation requires a valid CONVERTAPI_SECRET in Vercel settings."
                return
            
            convertapi.api_secret = secret
            job['message'] = 'Local compiler missing. Using Cloud Compiler (ConvertAPI)...'
            
            with tempfile.NamedTemporaryFile(suffix='.tex', mode='w', delete=False) as tf:
                tf.write(latex_code)
                tf_path = tf.name
            
            try:
                result = convertapi.convert('pdf', {'File': tf_path}, from_format='tex')
                pdf_output_bytes = io.BytesIO(result.file.url_to_bytes())
            finally:
                if os.path.exists(tf_path): os.remove(tf_path)

        if not pdf_output_bytes:
            job['error'] = "PDF compilation failed. Please check your LaTeX syntax or API key."
            return

        pdf_output_bytes.seek(0)

        job['current_step'] = 3
        job['message'] = 'Finalizing your document...'
        file_id = str(uuid.uuid4())
        pdf_storage[file_id] = {'data': pdf_output_bytes, 'name': 'converted_notes.pdf'}
        job['file_id'] = file_id
        job['current_step'] = 4
        job['status'] = 'finished'
        job['message'] = 'Success! Your LaTeX conversion is complete.'
    except Exception as e:
        job_storage[job_id]['error'] = str(e)

@app.errorhandler(413)
def request_entity_too_large(error):
    flash('File(s) exceed the maximum allowed size of 50MB.', 'danger')
    return redirect(url_for('home'))

if __name__ == '__main__':
    app.run(debug=True, port=5001)
