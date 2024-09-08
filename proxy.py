import os
import requests
import argparse
from flask import Flask, request, session, g, abort, Response, send_from_directory
from html_utils import transcode_html
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import io
from PIL import Image
import hashlib
import shutil

os.environ['FLASK_ENV'] = 'development'
app = Flask(__name__)
session = requests.Session()

HTTP_ERRORS = (403, 404, 500, 503, 504)
ERROR_HEADER = "[[Macproxy Encountered an Error]]"

# Global variable to store the override extension
override_extension = None

# Global variables for image caching
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cached_images")
MAX_WIDTH = 512
MAX_HEIGHT = 342

# User-Agent string
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36"

# Call this function every time the proxy starts
def clear_image_cache():
	if os.path.exists(CACHE_DIR):
		shutil.rmtree(CACHE_DIR)
	os.makedirs(CACHE_DIR, exist_ok=True)

clear_image_cache()

# Try to import config.py from the extensions folder and enable extensions
try:
	import extensions.config as config
	ENABLED_EXTENSIONS = config.ENABLED_EXTENSIONS
except ModuleNotFoundError:
	print("config.py not found in extensions folder, running without extensions")
	ENABLED_EXTENSIONS = []

# Load extensions
extensions = {}
domain_to_extension = {}
print('Enabled Extensions: ')
for ext in ENABLED_EXTENSIONS:
	print(ext)
	module = __import__(f"extensions.{ext}.{ext}", fromlist=[''])
	extensions[ext] = module
	domain_to_extension[module.DOMAIN] = module

def optimize_image(image_data):
	img = Image.open(io.BytesIO(image_data))
	
	# Calculate the new size while maintaining aspect ratio
	width, height = img.size
	if width > MAX_WIDTH or height > MAX_HEIGHT:
		ratio = min(MAX_WIDTH / width, MAX_HEIGHT / height)
		new_size = (int(width * ratio), int(height * ratio))
		img = img.resize(new_size, Image.LANCZOS)
	
	# Convert to black and white
	img = img.convert("1")
	
	# Save as 1-bit GIF
	output = io.BytesIO()
	img.save(output, format="GIF", optimize=True)
	return output.getvalue()

def fetch_and_cache_image(url):
	try:
		print(f"Fetching image: {url}")
		response = requests.get(url, stream=True, headers={"User-Agent": USER_AGENT})
		response.raise_for_status()
		
		# Generate a unique filename based on the URL
		file_name = hashlib.md5(url.encode()).hexdigest() + ".gif"
		file_path = os.path.join(CACHE_DIR, file_name)
		
		# If the file doesn't exist, optimize and cache it
		if not os.path.exists(file_path):
			print(f"Optimizing and caching image: {url}")
			optimized_image = optimize_image(response.content)
			with open(file_path, 'wb') as f:
				f.write(optimized_image)
		else:
			print(f"Image already cached: {url}")
		
		cached_url = f"/cached_image/{file_name}"
		print(f"Cached URL: {cached_url}")
		return cached_url
	except Exception as e:
		print(f"Error processing image: {url}, Error: {str(e)}")
		return None

def replace_image_urls(content, base_url):
	soup = BeautifulSoup(content, 'html.parser')
	for img in soup.find_all('img'):
		src = img.get('src')
		if src:
			full_url = urljoin(base_url, src)
			print(f"Processing image: {full_url}")
			cached_url = fetch_and_cache_image(full_url)
			if cached_url:
				img['src'] = cached_url
				print(f"Replaced image URL: {src} -> {cached_url}")
			else:
				print(f"Failed to cache image: {full_url}")
	return str(soup)

@app.route("/cached_image/<path:filename>")
def serve_cached_image(filename):
	return send_from_directory(CACHE_DIR, filename, mimetype='image/gif')

@app.route("/", defaults={"path": "/"}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def handle_request(path):
	global override_extension
	parsed_url = urlparse(request.url)
	scheme = parsed_url.scheme
	host = parsed_url.netloc.split(':')[0]  # Remove port if present
	
	if override_extension:
		print(f'Current override extension: {override_extension}')

	override_response = handle_override_extension(scheme)
	if override_response is not None:
		return process_response_with_image_caching(override_response, request.url)

	matching_extension = find_matching_extension(host)
	if matching_extension:
		return handle_matching_extension(matching_extension)

	return handle_default_request()

def handle_override_extension(scheme):
	global override_extension
	if override_extension:
		extension_name = override_extension.split('.')[-1]
		if extension_name in extensions:
			if scheme in ['http', 'https', 'ftp']:
				response = extensions[extension_name].handle_request(request)
				check_override_status(extension_name)
				return process_response_with_image_caching(response, request.url)
			else:
				print(f"Warning: Unsupported scheme '{scheme}' for override extension.")
		else:
			print(f"Warning: Override extension '{extension_name}' not found. Resetting override.")
			override_extension = None
	return None  # Return None if no override is active

def check_override_status(extension_name):
	global override_extension
	if hasattr(extensions[extension_name], 'get_override_status') and not extensions[extension_name].get_override_status():
		override_extension = None
		print("Override disabled")

def find_matching_extension(host):
	for domain, extension in domain_to_extension.items():
		if host.endswith(domain):
			return extension
	return None

def handle_matching_extension(matching_extension):
	global override_extension
	print(f"Handling request with matching extension: {matching_extension.__name__}")
	response = matching_extension.handle_request(request)
	
	if hasattr(matching_extension, 'get_override_status') and matching_extension.get_override_status():
		override_extension = matching_extension.__name__
		print(f"Override enabled for {override_extension}")
	
	# Use the original request URL as the base URL
	return process_response_with_image_caching(response, request.url)

def process_response_with_image_caching(response, base_url):
	print(f"Processing response for URL: {base_url}")
	if isinstance(response, tuple):
		if len(response) == 3:
			content, status_code, headers = response
		elif len(response) == 2:
			content, status_code = response
			headers = {}
		else:
			content = response[0]
			status_code = 200
			headers = {}
	elif isinstance(response, Response):
		print("Response is already a Flask Response object")
		return response
	else:
		content = response
		status_code = 200
		headers = {}

	content_type = headers.get('Content-Type', '').lower()
	print(f"Content-Type: {content_type}")

	# Apply image caching for HTML content
	if content_type.startswith('text/html'):
		print("Applying image caching to HTML content")
		if isinstance(content, bytes):
			content = content.decode('utf-8', errors='replace')
		content = replace_image_urls(content, base_url)
		content = transcode_html(content, app.config["DISABLE_CHAR_CONVERSION"])
	elif content_type.startswith('text/'):
		print("Processing text content")
		if isinstance(content, bytes):
			content = content.decode('utf-8', errors='replace')
		content = transcode_html(content, app.config["DISABLE_CHAR_CONVERSION"])
	else:
		print(f"Content is not text ({content_type}), passing through unchanged")

	response = Response(content, status_code)
	for key, value in headers.items():
		response.headers[key] = value

	print("Finished processing response")
	return response

def handle_default_request():
	url = request.url.replace("https://", "http://", 1)
	headers = prepare_headers()
	
	print(f"Handling default request for URL: {url}")
	
	try:
		resp = send_request(url, headers)
		content = resp.content
		status_code = resp.status_code
		headers = dict(resp.headers)
		return process_response_with_image_caching((content, status_code, headers), url)
	except Exception as e:
		print(f"Error in handle_default_request: {str(e)}")
		return abort(500, ERROR_HEADER + str(e))

def prepare_headers():
	headers = {
		"Accept": request.headers.get("Accept"),
		"Accept-Language": request.headers.get("Accept-Language"),
		"Referer": request.headers.get("Referer"),
		"User-Agent": USER_AGENT,
	}
	return headers

def send_request(url, headers):
	print(f"Sending request to: {url}")
	if request.method == "POST":
		return session.post(url, data=request.form, headers=headers, allow_redirects=True)
	else:
		return session.get(url, params=request.args, headers=headers)

@app.after_request
def apply_caching(resp):
	try:
		resp.headers["Content-Type"] = g.content_type
	except:
		pass
	return resp

if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Macproxy command line arguments")
	parser.add_argument(
		"--port",
		type=int,
		default=5001,
		action="store",
		help="Port number the web server will run on",
	)
	parser.add_argument(
		"--user-agent",
		type=str,
		default=USER_AGENT,
		action="store",
		help="Spoof as a particular web browser, e.g. \"Mozilla/5.0\"",
	)
	parser.add_argument(
		"--html-formatter",
		type=str,
		choices=["minimal", "html", "html5"],
		default="html5",
		action="store",
		help="The BeautifulSoup html formatter that Macproxy will use",
	)
	parser.add_argument(
		"--disable-char-conversion",
		action="store_true",
		help="Disable the conversion of common typographic characters to ASCII",
	)
	arguments = parser.parse_args()
	app.config["USER_AGENT"] = arguments.user_agent
	app.config["DISABLE_CHAR_CONVERSION"] = arguments.disable_char_conversion
	app.run(host="0.0.0.0", port=arguments.port, debug=False)