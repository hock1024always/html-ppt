#!/usr/bin/env python3
"""
SlideJSON Local Server
Serves the editor, provides REST API for saving, and SSE for live sync.

Usage:
    python3 server.py [--host 0.0.0.0] [--port 8080] [--file presentation.json]

Compatible with Python 3.6+
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = "presentation.json"

# Track file state for change detection
file_hash = ""


def get_file_hash(filepath):
    """Get MD5 hash of file content."""
    try:
        with open(filepath, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except (FileNotFoundError, IOError):
        return ""


def ensure_data_file(filepath):
    """Create default presentation.json if it doesn't exist."""
    if not os.path.exists(filepath):
        default = {
            "meta": {"title": "Untitled Presentation", "author": ""},
            "slides": [
                {
                    "id": "s-1",
                    "background": {"color": "#ffffff", "image": "", "gradient": ""},
                    "elements": [],
                    "notes": "",
                }
            ],
        }
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)
        print("  Created " + filepath)


class SlideHandler(SimpleHTTPRequestHandler):
    """HTTP handler for the editor API."""

    # Class-level config (set by make_handler)
    data_file = DATA_FILE
    serve_dir = SCRIPT_DIR

    def translate_path(self, path):
        """Override to serve files from the project directory."""
        # Strip query string and fragment
        path = path.split("?", 1)[0].split("#", 1)[0]
        # Normalize
        import posixpath
        import urllib.parse
        path = urllib.parse.unquote(path)
        path = posixpath.normpath(path)
        # Map to serve_dir
        parts = path.split("/")
        result = self.serve_dir
        for part in parts:
            if part and part != "..":
                result = os.path.join(result, part)
        return result

    def log_message(self, format, *args):
        if args and isinstance(args[0], str) and "api/events" in args[0]:
            return
        sys.stderr.write(
            "  [%s] %s\n" % (self.log_date_time_string(), format % args)
        )

    def do_GET(self):
        if self.path == "/api/presentation":
            self._serve_json()
        elif self.path == "/api/events":
            self._serve_sse()
        elif self.path == "/api/status":
            self._json_response({"status": "ok", "file": self.data_file})
        else:
            super(SlideHandler, self).do_GET()

    def do_POST(self):
        if self.path == "/api/presentation":
            self._save_json()
        elif self.path == "/api/notify":
            self._notify_change()
        elif self.path.startswith("/api/upload"):
            self._upload_file()
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self):
        global file_hash
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                content = f.read()
            file_hash = hashlib.md5(content.encode()).hexdigest()
            body = content.encode("utf-8")
            self.send_response(200)
            self._cors_headers()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-File-Hash", file_hash)
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._json_response({"error": "File not found"}, 404)

    def _save_json(self):
        global file_hash
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body)
            formatted = json.dumps(data, ensure_ascii=False, indent=2)
            with open(self.data_file, "w", encoding="utf-8") as f:
                f.write(formatted)
            file_hash = hashlib.md5(formatted.encode()).hexdigest()
            self._json_response({"ok": True, "hash": file_hash})
        except (ValueError, json.JSONDecodeError) as e:
            self._json_response({"error": "Invalid JSON: %s" % str(e)}, 400)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _serve_sse(self):
        """Server-Sent Events endpoint for file change notifications."""
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        global file_hash
        last_hash = file_hash
        try:
            msg = "data: %s\n\n" % json.dumps({"hash": last_hash})
            self.wfile.write(msg.encode())
            self.wfile.flush()

            while True:
                time.sleep(0.5)
                current_hash = get_file_hash(self.data_file)
                if current_hash != last_hash:
                    last_hash = current_hash
                    file_hash = current_hash
                    msg = "data: %s\n\n" % json.dumps({"hash": current_hash, "changed": True})
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                else:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _notify_change(self):
        global file_hash
        file_hash = get_file_hash(self.data_file)
        self._json_response({"ok": True, "hash": file_hash})

    def _upload_file(self):
        """Handle file upload (images). Saves to uploads/ directory."""
        try:
            import uuid
            length = int(self.headers.get("Content-Length", 0))
            content_type = self.headers.get("Content-Type", "")

            # Parse multipart form data manually (no cgi module needed)
            if "multipart/form-data" not in content_type:
                # Raw binary upload with filename in query string
                import urllib.parse
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                ext = params.get("ext", ["png"])[0]
                body = self.rfile.read(length)
            else:
                # Read raw body for multipart
                boundary = content_type.split("boundary=")[-1].encode()
                body = self.rfile.read(length)
                # Extract file content between boundaries
                parts = body.split(b"--" + boundary)
                file_data = None
                ext = "png"
                for part in parts:
                    if b"filename=" in part:
                        # Get extension from filename
                        header_end = part.index(b"\r\n\r\n")
                        header = part[:header_end].decode("utf-8", errors="replace")
                        for segment in header.split(";"):
                            if "filename=" in segment:
                                fname = segment.split("=")[-1].strip().strip('"')
                                if "." in fname:
                                    ext = fname.rsplit(".", 1)[-1].lower()
                        file_data = part[header_end + 4:]
                        # Remove trailing \r\n
                        if file_data.endswith(b"\r\n"):
                            file_data = file_data[:-2]
                        break
                if file_data is None:
                    self._json_response({"error": "No file in upload"}, 400)
                    return
                body = file_data

            # Save to uploads/ directory
            upload_dir = os.path.join(self.serve_dir, "uploads")
            if not os.path.exists(upload_dir):
                os.makedirs(upload_dir)

            filename = "%s.%s" % (uuid.uuid4().hex[:12], ext)
            filepath = os.path.join(upload_dir, filename)
            with open(filepath, "wb") as f:
                f.write(body)

            url = "/uploads/%s" % filename
            self._json_response({"ok": True, "url": url, "filename": filename})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)


def make_handler(data_file, serve_dir):
    """Create a handler class with config bound."""

    class BoundHandler(SlideHandler):
        pass

    BoundHandler.data_file = data_file
    BoundHandler.serve_dir = serve_dir
    return BoundHandler


def _get_local_ip():
    """Try to detect the machine's LAN/public IP for display purposes."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def main():
    parser = argparse.ArgumentParser(description="SlideJSON Local Server")
    parser.add_argument("--host", "-H", type=str, default="0.0.0.0",
                        help="Host/IP to bind (default: 0.0.0.0, use specific IP for remote access)")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port to serve on")
    parser.add_argument(
        "--file", "-f", type=str, default="presentation.json", help="Presentation data file"
    )
    args = parser.parse_args()

    data_file = os.path.abspath(args.file)
    ensure_data_file(data_file)

    handler = make_handler(data_file, SCRIPT_DIR)

    # Use ThreadingHTTPServer if available (3.7+), else plain HTTPServer
    try:
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except ImportError:
        import socketserver

        class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True

        server = ThreadedHTTPServer((args.host, args.port), handler)

    # Determine display address
    if args.host == "0.0.0.0":
        display_host = _get_local_ip()
    else:
        display_host = args.host

    print("")
    print("  SlideJSON Server")
    print("  " + "-" * 40)
    print("  Editor:  http://%s:%d/" % (display_host, args.port))
    print("  Data:    %s" % data_file)
    print("  API:     http://%s:%d/api/presentation" % (display_host, args.port))
    if args.host == "0.0.0.0":
        print("  Local:   http://localhost:%d/" % args.port)
    print("  " + "-" * 40)
    print("  Press Ctrl+C to stop")
    print("")

    def signal_handler(sig, frame):
        print("\n  Server stopped.")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.shutdown()


if __name__ == "__main__":
    main()
