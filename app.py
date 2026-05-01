from flask import Flask, render_template, request, send_file, flash, redirect, url_for
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

# Admin's shared Gemini API key (set via env var)
ADMIN_GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')

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
@login_required
def image_to_latex():
    from flask import session
    if current_user.status != 'approved':
        flash('You must be an approved BHU user to use this feature.', 'warning')
        return redirect(url_for('latex_waiting', username=current_user.username))

    if request.method == 'POST':
        user_api_key = request.form.get('api_key', '').strip()
        final_api_key = user_api_key or ADMIN_GEMINI_KEY

        if not final_api_key:
            flash('Please provide a Gemini API key to continue.', 'danger')
            return redirect(request.url)

        try:
            client = google_genai.Client(api_key=final_api_key)
            # Order of preference from most basic/generous free tier to most advanced
            preference_order = [
                'gemini-1.5-flash-8b', 
                'gemini-1.5-flash', 
                'gemini-2.0-flash-exp', 
                'gemini-1.5-pro', 
                'gemini-2.0-flash'
            ]
            
            selected_model = None
            last_error = None
            
            for pref in preference_order:
                try:
                    # Send a tiny dummy request to verify quota and model existence
                    client.models.generate_content(
                        model=pref,
                        contents="test"
                    )
                    selected_model = pref
                    break
                except Exception as e:
                    last_error = str(e)
                    continue
            
            if not selected_model:
                flash(f'Your API key could not access any models. Last error: {last_error}', 'danger')
                return redirect(request.url)
                
            session['latex_api_key'] = final_api_key
            session['latex_model'] = selected_model
            
            return redirect(url_for('image_to_latex_upload'))
            
        except Exception as e:
            flash(f'API Key Validation Failed: {str(e)}', 'danger')
            return redirect(request.url)
            
    return render_template('image_to_latex.html')

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
        job_storage[job_id] = {
            'status': 'queued',
            'current_step': 0,
            'total_steps': 2,
            'steps': ['AI Vision Analysis', 'Awaiting User Edit'],
            'message': f'Initializing {model_name}...',
            'error': None,
            'file_id': None,
            'extracted_latex': None,
            'type': 'image_to_latex'
        }

        thread = threading.Thread(target=process_image_to_latex_task, args=(job_id, file.read(), api_key, model_name))
        thread.start()
        return redirect(url_for('processing_page', job_id=job_id))
                
    return render_template('image_to_latex_upload.html', model_name=model_name)

def process_image_to_latex_task(job_id, image_bytes, api_key, model_name):
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage
        from langchain_core.prompts import ChatPromptTemplate
        from langchain_core.output_parsers import StrOutputParser
        
        job = job_storage[job_id]
        job['current_step'] = 0
        job['message'] = f'{model_name} is analyzing your image via LangChain...'
        
        # Instantiate LLMs
        vision_llm = ChatGoogleGenerativeAI(model=model_name, google_api_key=api_key, temperature=0.1)
        # Use a fast text model for correction, defaulting to standard gemini-1.5-flash
        correction_llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", google_api_key=api_key, temperature=0.1)

        # Convert image to base64 for LangChain Vision
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        
        vision_prompt = (
            "You are an expert LaTeX typesetter. Look at this handwritten image of study notes or equations. "
            "Convert ALL visible handwriting, text, and mathematical expressions into a complete, compilable LaTeX document. "
            "Use the 'article' documentclass. Include packages: amsmath, amssymb, geometry (with margins=1in), fontenc (T1), inputenc (utf8). "
            "Preserve the structure and order of the content as closely as possible. "
            "Output ONLY the raw LaTeX code, starting with \\documentclass and ending with \\end{document}. No explanations or conversational text."
        )

        vision_message = HumanMessage(
            content=[
                {"type": "text", "text": vision_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
            ]
        )

        correction_prompt = ChatPromptTemplate.from_messages([
            ("system", "You are an expert LaTeX debugger. The following LaTeX code was generated by an AI and may contain syntax errors, unclosed environments, missing brackets, or conversational filler. Fix all errors, remove any markdown formatting like ```latex, and return ONLY the raw, compilable LaTeX code. Do not include any explanations."),
            ("user", "{raw_latex}")
        ])
        
        correction_chain = correction_prompt | correction_llm | StrOutputParser()

        def is_retryable(exc):
            error_str = str(exc).lower()
            return any(keyword in error_str for keyword in [
                '429', 'resource_exhausted', 'rate limit', 'quota',
                'service unavailable', '503', '500', 'connection', 'timeout'
            ])

        @retry(
            wait=wait_exponential(multiplier=2, min=2, max=60),
            stop=stop_after_attempt(5),
            retry=retry_if_exception(is_retryable),
            reraise=True
        )
        def call_vision():
            global last_gemini_time
            job['message'] = 'Waiting for available AI slot (Vision step)...'
            with gemini_lock:
                job['message'] = f'{model_name} is extracting LaTeX from image...'
                now = time.time()
                elapsed = now - last_gemini_time
                if elapsed < GEMINI_MIN_INTERVAL:
                    time.sleep(GEMINI_MIN_INTERVAL - elapsed)
                
                try:
                    res = vision_llm.invoke([vision_message])
                    last_gemini_time = time.time()
                    return res.content
                except Exception as e:
                    last_gemini_time = time.time()
                    raise e
                    
        @retry(
            wait=wait_exponential(multiplier=2, min=2, max=60),
            stop=stop_after_attempt(5),
            retry=retry_if_exception(is_retryable),
            reraise=True
        )
        def call_correction(raw_latex_input):
            global last_gemini_time
            job['message'] = 'Waiting for available AI slot (Correction step)...'
            with gemini_lock:
                job['message'] = 'AI is checking and correcting LaTeX syntax...'
                now = time.time()
                elapsed = now - last_gemini_time
                if elapsed < GEMINI_MIN_INTERVAL:
                    time.sleep(GEMINI_MIN_INTERVAL - elapsed)
                
                try:
                    res = correction_chain.invoke({"raw_latex": raw_latex_input})
                    last_gemini_time = time.time()
                    return res
                except Exception as e:
                    last_gemini_time = time.time()
                    raise e

        try:
            # Step 1: Vision Model Generation
            raw_latex = call_vision()
            
            # Step 2: LangChain Correction & Parsing
            corrected_latex = call_correction(raw_latex)
            
        except Exception as e:
            job['error'] = f"AI Analysis Failed: {str(e)}"
            return

        job['current_step'] = 1
        job['message'] = 'Cleaning and formatting LaTeX code...'
        
        final_latex = corrected_latex
        final_latex = re.sub(r'^```(?:latex)?\n?', '', final_latex, flags=re.IGNORECASE)
        final_latex = re.sub(r'```$', '', final_latex.strip())
        final_latex = final_latex.strip()

        if not final_latex.startswith('\\documentclass'):
            final_latex = (
                '\\documentclass{article}\n'
                '\\usepackage[utf8]{inputenc}\n'
                '\\usepackage[T1]{fontenc}\n'
                '\\usepackage{amsmath,amssymb}\n'
                '\\usepackage[margin=1in]{geometry}\n'
                '\\begin{document}\n'
                + final_latex +
                '\n\\end{document}\n'
            )

        job['extracted_latex'] = final_latex
        job['current_step'] = 2
        job['status'] = 'requires_edit'
        job['message'] = 'Extraction complete. Redirecting to editor...'
    except Exception as e:
        job_storage[job_id]['error'] = str(e)

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
        job['current_step'] = 0
        job['message'] = 'Compiling LaTeX to PDF (this may take a minute)...'
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tex_path = os.path.join(tmpdir, 'output.tex')
            pdf_path = os.path.join(tmpdir, 'output.pdf')
            with open(tex_path, 'w', encoding='utf-8') as f:
                f.write(latex_code)
            try:
                subprocess.run(['pdflatex', '-interaction=nonstopmode', '-output-directory', tmpdir, tex_path],
                             capture_output=True, text=True, timeout=60)
            except subprocess.TimeoutExpired:
                job['error'] = "PDF compilation timed out."
                return
            if not os.path.exists(pdf_path):
                job['error'] = "PDF compilation failed. Check your LaTeX syntax."
                return
            with open(pdf_path, 'rb') as f:
                pdf_output_bytes = io.BytesIO(f.read())
            pdf_output_bytes.seek(0)

        job['current_step'] = 1
        job['message'] = 'Finalizing your document...'
        file_id = str(uuid.uuid4())
        pdf_storage[file_id] = {'data': pdf_output_bytes, 'name': 'converted_notes.pdf'}
        job['file_id'] = file_id
        job['current_step'] = 2
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
