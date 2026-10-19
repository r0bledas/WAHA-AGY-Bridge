#!/usr/bin/env python3
"""
send_file.py - Fast CLI tool to send files, PDFs, documents, or images to WhatsApp via WAHA.

Usage:
    python send_file.py <file_path> [caption] [--chat-id <chat_id>]

Examples:
    python send_file.py "C:\\waha\\document.pdf" "Here is your report"
    python send_file.py "screenshot.png"
    python send_file.py "data.docx" "Homework 3.1" --chat-id 5218135537757@c.us
"""

import argparse
import base64
import mimetypes
import os
import sys
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def load_dotenv(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass

load_dotenv(os.path.join(BASE_DIR, ".env"))

WAHA_URL = os.environ.get("WAHA_URL", "http://localhost:3001")
WAHA_API_KEY = os.environ.get("WAHA_API_KEY", "")

def get_default_chat_id():
    accounts = os.environ.get("BRIDGE_MY_ACCOUNTS", "").split(",")
    for acc in accounts:
        acc = acc.strip()
        if acc.endswith("@c.us"):
            return acc
    for acc in accounts:
        acc = acc.strip()
        if acc:
            return acc
    return "5218135537757@c.us"

def guess_mime(file_path):
    mime, _ = mimetypes.guess_type(file_path)
    if mime:
        return mime
    ext = os.path.splitext(file_path)[1].lower()
    fallback_map = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".txt": "text/plain",
        ".py": "text/plain",
        ".zip": "application/zip",
    }
    return fallback_map.get(ext, "application/octet-stream")

def send_file(file_path, caption="", chat_id=None):
    if not os.path.isabs(file_path):
        file_path = os.path.abspath(file_path)

    if not os.path.exists(file_path):
        print(f"[Error] File not found: {file_path}", file=sys.stderr)
        return False

    target_chat = chat_id or get_default_chat_id()
    mime = guess_mime(file_path)
    filename = os.path.basename(file_path)

    try:
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[Error] Failed to read file {file_path}: {e}", file=sys.stderr)
        return False

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": WAHA_API_KEY
    }
    payload = {
        "session": "default",
        "chatId": target_chat,
        "file": {
            "mimetype": mime,
            "filename": filename,
            "data": b64_data
        }
    }
    if caption:
        payload["caption"] = caption

    url = f"{WAHA_URL}/api/sendFile"
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=45)
        if r.status_code in [200, 201]:
            print(f"[OK] Successfully sent {filename} to {target_chat} ({len(b64_data)} b64 bytes)")
            return True
        else:
            print(f"[Error] WAHA rejected file: {r.status_code} {r.text}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"[Error] Exception sending file: {e}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Send a file to WhatsApp via WAHA")
    parser.add_argument("file_path", help="Path to file to send")
    parser.add_argument("caption", nargs="?", default="", help="Optional text caption")
    parser.add_argument("--chat-id", default=None, help="WhatsApp Chat ID (e.g. 5218135537757@c.us)")
    args = parser.parse_args()

    success = send_file(args.file_path, caption=args.caption, chat_id=args.chat_id)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
