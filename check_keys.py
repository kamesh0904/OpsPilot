"""
check_keys.py
─────────────
A utility script to check if the API keys specified in the .env file are valid and working.
It performs lightweight diagnostic calls to the official APIs of Google Gemini, Slack, Linear, Notion, and GitHub.
"""

import os
import sys
from typing import Tuple, Optional
import httpx
from dotenv import load_dotenv

# Colors for console output
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"

def print_header(title: str):
    print(f"\n{Colors.BOLD}{Colors.BLUE}=== {title} ==={Colors.END}")

def print_result(name: str, success: bool, message: str, is_placeholder: bool = False):
    if is_placeholder:
        status = f"{Colors.YELLOW}[PLACEHOLDER]{Colors.END}"
    elif success:
        status = f"{Colors.GREEN}[WORKING]{Colors.END}"
    else:
        status = f"{Colors.RED}[FAILED]{Colors.END}"
    print(f" - {Colors.BOLD}{name:<20}{Colors.END} {status} : {message}")

def check_gemini(api_key: str) -> Tuple[bool, str, bool]:
    """Verify Google Gemini API Key."""
    if not api_key or api_key == "your-google-api-key" or "..." in api_key:
        return False, "Key is a placeholder or not set.", True
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    
    try:
        response = httpx.get(url, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            models = [m.get("displayName", m.get("name")) for m in data.get("models", [])[:3]]
            models_str = ", ".join(models) + ("..." if len(data.get("models", [])) > 3 else "")
            return True, f"Successfully authenticated. Available models include: {models_str}", False
        else:
            try:
                error_msg = response.json().get("error", {}).get("message", response.text)
            except Exception:
                error_msg = response.text
            return False, f"API returned status {response.status_code}: {error_msg}", False
    except Exception as e:
        return False, f"Request failed: {str(e)}", False


def check_slack(token: str) -> Tuple[bool, str, bool]:
    """Verify Slack Bot Token."""
    if not token or token == "xoxb-..." or "..." in token:
        return False, "Token is a placeholder or not set.", True
    
    url = "https://slack.com/api/auth.test"
    headers = {
        "Authorization": f"Bearer {token}"
    }
    
    try:
        response = httpx.post(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            if data.get("ok"):
                user = data.get("user")
                team = data.get("team")
                return True, f"Authenticated successfully as bot '{user}' in workspace '{team}'.", False
            else:
                return False, f"Slack API error: {data.get('error')}", False
        else:
            return False, f"HTTP status {response.status_code}: {response.text}", False
    except Exception as e:
        return False, f"Request failed: {str(e)}", False

def check_linear(api_key: str) -> Tuple[bool, str, bool]:
    """Verify Linear API Key."""
    if not api_key or api_key == "lin_api_..." or "..." in api_key:
        return False, "Key is a placeholder or not set.", True
    
    url = "https://api.linear.app/graphql"
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json"
    }
    payload = {
        "query": "{ viewer { id name email } }"
    }
    
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            if "errors" in data:
                return False, f"Linear API returned errors: {data['errors']}", False
            viewer = data.get("data", {}).get("viewer")
            if viewer:
                name = viewer.get("name") or "Unnamed User"
                email = viewer.get("email") or "No email"
                return True, f"Connected successfully as '{name}' ({email}).", False
            return False, "Viewer information could not be retrieved.", False
        else:
            return False, f"HTTP status {response.status_code}: {response.text}", False
    except Exception as e:
        return False, f"Request failed: {str(e)}", False

def check_notion(api_key: str) -> Tuple[bool, str, bool]:
    """Verify Notion API Key."""
    if not api_key or api_key == "secret_..." or "..." in api_key:
        return False, "Key is a placeholder or not set.", True
    
    url = "https://api.notion.com/v1/users/me"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": "2022-06-28"
    }
    
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            name = data.get("name") or "Workspace Bot"
            return True, f"Connected successfully as integration/bot '{name}'.", False
        else:
            try:
                error_msg = response.json().get("message", response.text)
            except Exception:
                error_msg = response.text
            return False, f"Notion API error (status {response.status_code}): {error_msg}", False
    except Exception as e:
        return False, f"Request failed: {str(e)}", False

def check_github(token: str) -> Tuple[bool, str, bool]:
    """Verify GitHub Token."""
    if not token or token == "ghp_..." or "..." in token:
        return False, "Token is a placeholder or not set.", True
    
    url = "https://api.github.com/user"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "OpsPilot-Diagnostic"
    }
    
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            login = data.get("login")
            name = data.get("name") or login
            return True, f"Connected successfully as GitHub user '{login}' ({name}).", False
        else:
            try:
                error_msg = response.json().get("message", response.text)
            except Exception:
                error_msg = response.text
            return False, f"GitHub API error (status {response.status_code}): {error_msg}", False
    except Exception as e:
        return False, f"Request failed: {str(e)}", False

def main():
    print_header("OpsPilot API Key Validator")
    
    # Load .env file
    env_path = ".env"
    if not os.path.exists(env_path):
        print(f"{Colors.RED}Error: .env file not found at current directory.{Colors.END}")
        sys.exit(1)
        
    print(f"Loading environment from: {Colors.CYAN}{os.path.abspath(env_path)}{Colors.END}\n")
    load_dotenv(env_path)
    
    # Check Gemini
    gemini_key = os.getenv("GOOGLE_API_KEY")
    success, msg, is_placeholder = check_gemini(gemini_key)
    print_result("Google Gemini API Key", success, msg, is_placeholder)
    
    # Check Slack
    slack_token = os.getenv("SLACK_BOT_TOKEN")
    success, msg, is_placeholder = check_slack(slack_token)
    print_result("Slack Bot Token", success, msg, is_placeholder)
    
    # Check Linear
    linear_key = os.getenv("LINEAR_API_KEY")
    success, msg, is_placeholder = check_linear(linear_key)
    print_result("Linear API Key", success, msg, is_placeholder)
    
    # Check Notion
    notion_key = os.getenv("NOTION_API_KEY")
    success, msg, is_placeholder = check_notion(notion_key)
    print_result("Notion API Key", success, msg, is_placeholder)
    
    # Check GitHub
    github_token = os.getenv("GITHUB_TOKEN")
    success, msg, is_placeholder = check_github(github_token)
    print_result("GitHub Token", success, msg, is_placeholder)
    
    print(f"\n{Colors.BOLD}{Colors.BLUE}==================================={Colors.END}\n")

if __name__ == "__main__":
    # Enable colored output on Windows command prompt if needed
    if sys.platform == "win32":
        os.system("color")
    main()
