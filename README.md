# PDF.PRO | Modern Document Portal

A robust, premium web application for document processing. Built with Flask, Pillow, and PyPDF2.

## Features
- **Image to PDF:** Convert JPG, PNG, and WEBP into high-quality PDFs.
- **Merge PDF:** Combine multiple PDF documents into one.
- **Image to LaTeX:** (Coming Soon).
- **Zero Disk Bloat:** All processing is done entirely in RAM (io.BytesIO). No files are saved to the server's disk.
- **Modern UI:** Glassmorphism design with a dynamic interactive particle background.
- **Secure:** 50MB upload limit and automatic color space conversion (RGBA to RGB).

## Tech Stack
- **Backend:** Python, Flask, Werkzeug, PyPDF2, Pillow.
- **Frontend:** HTML5, CSS3 (Glassmorphism), Bootstrap 5, Javascript (Canvas Particles).

## Installation
1. Clone the repository.
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the application:
   ```bash
   python3 app.py
   ```
4. Open `http://127.0.0.1:5002` in your browser.
