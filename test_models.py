import os
from google import genai as google_genai

api_key = os.environ.get('GEMINI_API_KEY')
if not api_key:
    # try reading from the app.py context or just mock it if we can't
    pass

client = google_genai.Client()
for m in client.models.list():
    print(m.name)
