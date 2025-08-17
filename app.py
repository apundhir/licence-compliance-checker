# app.py
from flask import Flask, render_template, request, jsonify
import json
import requests
import re
import logging
import os
import zipfile
import io

# --- App Configuration ---
app = Flask(__name__)

# --- Logging Configuration ---
# Configure logging to print to the console.
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- In-memory Data ---
# In-memory database to store license policies. This can be expanded or moved to a config file.
policies = {
    "default": {
        "allowed": ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC"],
        "disallowed": ["GPL-2.0", "GPL-3.0", "AGPL-3.0"],
    }
}

# --- Helper Functions ---

def get_license_from_pypi(package_name):
    """
    Fetches license information for a Python package from the PyPI API.
    Handles potential request errors and missing license information.
    """
    url = f"https://pypi.org/pypi/{package_name}/json"
    logging.info(f"Fetching license for Python package: {package_name} from {url}")
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        data = response.json()
        # The license is often in 'license', but can also be in classifiers.
        license_info = data.get("info", {}).get("license")
        if not license_info or license_info.strip() == "UNKNOWN":
             # Fallback to checking classifiers for license information
            for classifier in data.get("info", {}).get("classifiers", []):
                if "License ::" in classifier:
                    # Extract license name from classifier string
                    license_info = classifier.split("::")[-1].strip()
                    break
        return license_info or "Unknown"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching license for {package_name}: {e}")
        return "Error fetching license"

def get_license_from_npm(package_name):
    """
    Fetches license information for a Node.js package from the npm registry API.
    Handles potential request errors.
    """
    url = f"https://registry.npmjs.org/{package_name}"
    logging.info(f"Fetching license for Node.js package: {package_name} from {url}")
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        # License can be a string or an object like {"type": "MIT", "url": "..."}
        license_info = data.get("license")
        if isinstance(license_info, dict):
            return license_info.get("type", "Unknown")
        return license_info or "Unknown"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching license for {package_name}: {e}")
        return "Error fetching license"

def check_compliance(license, policy):
    """Checks if a license is compliant with a given policy."""
    if not isinstance(license, str):
        return "review-required" # Should not happen, but a safeguard
    
    # Simple check for SPDX license expressions (e.g., "(MIT OR Apache-2.0)")
    # A more robust solution would use a proper SPDX parsing library.
    if " OR " in license or " AND " in license:
        return "review-required"

    if license in policy["allowed"]:
        return "compliant"
    if license in policy["disallowed"]:
        return "non-compliant"
    return "review-required"

def parse_requirements_txt(content):
    """
    Parses a requirements.txt file content to extract package names.
    - Ignores comments and empty lines.
    - Handles different version specifiers (==, >=, <=, ~, <, >).
    - Strips extras (e.g., [standard]).
    """
    # Regex to capture the package name. It's the part before any specifiers or brackets.
    package_pattern = re.compile(r"^\s*([a-zA-Z0-9_.-]+)")
    packages = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = package_pattern.match(line)
        if match:
            packages.append(match.group(1))
    return packages

# --- Flask Routes ---

@app.route('/')
def index():
    """Renders the main page of the application."""
    return render_template('index.html')

def analyze_github_repository(repo_url):
    """
    Analyzes a public GitHub repository for license compliance.
    - Downloads the repo as a zip file.
    - Finds and analyzes dependency files (requirements.txt, package.json).
    """
    logging.info(f"Analyzing GitHub repository: {repo_url}")

    # Correctly format the URL to point to the zip archive.
    # e.g., https://github.com/user/repo -> https://github.com/user/repo/archive/refs/heads/main.zip
    # This is a common default, but master or other branch names could also be the default.
    # A more robust solution would use the GitHub API to find the default branch.
    # For this implementation, we will try 'main' and then 'master'.

    repo_path = re.sub(r"https?://github.com/", "", repo_url).strip('/')
    zip_url_main = f"https://github.com/{repo_path}/archive/refs/heads/main.zip"
    zip_url_master = f"https://github.com/{repo_path}/archive/refs/heads/master.zip"

    results = []

    try:
        response = requests.get(zip_url_main, timeout=30)
        if response.status_code != 200:
            logging.warning(f"Failed to fetch 'main' branch, trying 'master'. Status: {response.status_code}")
            response = requests.get(zip_url_master, timeout=30)
            response.raise_for_status() # Raise an exception if master also fails

        zip_file = zipfile.ZipFile(io.BytesIO(response.content))

        # In-memory policy, can be fetched from request or config
        policy = policies.get("default")

        for filename in zip_file.namelist():
            if filename.endswith('requirements.txt'):
                logging.info(f"Found requirements.txt at: {filename}")
                content = zip_file.read(filename).decode('utf-8')
                packages = parse_requirements_txt(content)
                for package in packages:
                    license = get_license_from_pypi(package)
                    status = check_compliance(license, policy)
                    results.append({"dependency": package, "license": license, "status": status})

            elif filename.endswith('package.json'):
                logging.info(f"Found package.json at: {filename}")
                content = zip_file.read(filename).decode('utf-8')
                try:
                    data = json.loads(content)
                    dependencies = data.get('dependencies', {})
                    devDependencies = data.get('devDependencies', {})
                    all_deps = {**dependencies, **devDependencies}

                    for package in all_deps:
                        license = get_license_from_npm(package)
                        status = check_compliance(license, policy)
                        results.append({"dependency": package, "license": license, "status": status})
                except json.JSONDecodeError:
                    logging.warning(f"Could not parse package.json at {filename}")
                    continue

    except requests.exceptions.RequestException as e:
        logging.error(f"Error downloading repository {repo_url}: {e}")
        raise Exception(f"Could not download repository. Please check the URL and that it's a public repo.")
    except zipfile.BadZipFile:
        logging.error(f"Downloaded file from {repo_url} is not a valid zip file.")
        raise Exception("Failed to process the repository file. It may not be a valid zip archive.")

    return results

@app.route('/check', methods=['POST'])
def check():
    """Handles the file upload and license compliance check."""
    logging.info("Received a new compliance check request.")

    repo_url = request.form.get('repo_url')
    file = request.files.get('dependencyFile')
    policy_name = request.form.get('policy', 'default')
    policy = policies.get(policy_name, policies["default"])
    
    if not file and not repo_url:
        logging.warning("Request failed: No file or repository URL provided.")
        return jsonify({"error": "No file uploaded or repository URL provided"}), 400

    results = []

    try:
        if repo_url:
            results = analyze_github_repository(repo_url)
        elif file:
            filename = file.filename
            logging.info(f"Processing file: {filename}")
            
            try:
                content = file.read().decode('utf-8')
            except UnicodeDecodeError:
                logging.error("File processing error: Not a valid UTF-8 file.")
                return jsonify({"error": "Invalid file encoding. Please upload a UTF-8 encoded file."}), 400

            if filename.endswith('requirements.txt'):
                packages = parse_requirements_txt(content)
                for package in packages:
                    license = get_license_from_pypi(package)
                    status = check_compliance(license, policy)
                    results.append({"dependency": package, "license": license, "status": status})

            elif filename.endswith('package.json'):
                try:
                    data = json.loads(content)
                    dependencies = data.get('dependencies', {})
                    devDependencies = data.get('devDependencies', {})
                    all_deps = {**dependencies, **devDependencies}

                    for package in all_deps:
                        license = get_license_from_npm(package)
                        status = check_compliance(license, policy)
                        results.append({"dependency": package, "license": license, "status": status})
                except json.JSONDecodeError:
                    logging.error("File processing error: Invalid JSON in package.json.")
                    return jsonify({"error": "Invalid package.json file. Please check the file format."}), 400
            else:
                logging.warning(f"Unsupported file type uploaded: {filename}")
                return jsonify({"error": "Unsupported file type. Please upload 'requirements.txt' or 'package.json'."}), 400
    except Exception as e:
        logging.error(f"An error occurred during processing: {e}")
        return jsonify({"error": str(e)}), 500

    logging.info(f"Compliance check completed. Found {len(results)} dependencies.")
    return jsonify(results)

if __name__ == '__main__':
    app.run(debug=True)
