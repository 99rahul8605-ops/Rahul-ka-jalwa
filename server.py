"""
Simple HTTP server for Render's port health check.
Render requires a web service to bind to a PORT — this runs alongside the bot.
"""

import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import logging

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", 8080))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK - Google Drive Telegram Bot is running")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        # Suppress default HTTP log spam
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    logger.info(f"Health check server running on port {PORT}")
    server.serve_forever()


def run_in_background():
    """Start health server in a daemon thread."""
    thread = threading.Thread(target=start_health_server, daemon=True)
    thread.start()
    return thread
