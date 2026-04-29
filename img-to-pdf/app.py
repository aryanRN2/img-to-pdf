from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from werkzeug.utils import secure_filename
from PIL import Image
import io

app = Flask(__name__)
# In production, use a secure random key like os.urandom(24)
app.secret_key = "super_secret_key_for_flash_messages" 
# 50 MB limit to prevent DoS attacks
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024 

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # Check if the post request has the file part
        if 'images' not in request.files:
            flash('No file part provided.', 'danger')
            return redirect(request.url)
        
        files = request.files.getlist('images')
        
        # If user does not select file, browser also
        # submit an empty part without filename
        if not files or files[0].filename == '':
            flash('No selected files.', 'danger')
            return redirect(request.url)
        
        images_list = []
        for file in files:
            if file and allowed_file(file.filename):
                try:
                    # Read image data directly into memory (zero disk bloat)
                    img_bytes = file.read()
                    img = Image.open(io.BytesIO(img_bytes))
                    
                    # Convert to standard RGB to prevent crashes in PDF saving
                    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
                        # Create a white background and paste the image to handle transparency
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
                flash(f'Invalid file type or no file provided: {file.filename}', 'danger')
                return redirect(request.url)
        
        if images_list:
            try:
                # Create a virtual file in RAM
                pdf_bytes = io.BytesIO()
                
                # The first image saves the PDF, the rest are appended to it
                first_img = images_list[0]
                other_imgs = images_list[1:]
                
                first_img.save(
                    pdf_bytes, 
                    format='PDF', 
                    save_all=True, 
                    append_images=other_imgs
                )
                
                # Reset the virtual file pointer to the beginning before sending
                pdf_bytes.seek(0)
                
                # Send the PDF directly to the user without saving to disk
                return send_file(
                    pdf_bytes, 
                    mimetype='application/pdf', 
                    as_attachment=True, 
                    download_name='converted_images.pdf'
                )
            except Exception as e:
                flash(f'Error generating PDF: {str(e)}', 'danger')
                return redirect(request.url)
                
    return render_template('index.html')

# Gracefully handle file size exceed error
@app.errorhandler(413)
def request_entity_too_large(error):
    flash('File(s) exceed the maximum allowed size of 50MB.', 'danger')
    return redirect(url_for('index'))

if __name__ == '__main__':
    # Run the app locally
    app.run(debug=True, port=5000)
