import http.server
import json
import os
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"
PORT = int(os.environ.get("PORT", 3000))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")

CONFIG_JS = f"""window.SUPABASE_URL = '{SUPABASE_URL}';
window.SUPABASE_ANON_KEY = '{SUPABASE_ANON_KEY}';
"""


class PacingHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/supabase-config.js":
            self._serve_config()
        elif path == "/api/files":
            self._serve_file_list()
        elif path.startswith("/pacing/"):
            self._serve_pacing_file(path[len("/pacing/"):])
        else:
            super().do_GET()

    def _serve_config(self):
        body = CONFIG_JS.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file_list(self):
        files = []
        for f in BASE_DIR.iterdir():
            if f.name.startswith("predict_") and f.name.endswith(".html"):
                stat = f.stat()
                files.append({
                    "name": f.name,
                    "size": stat.st_size,
                    "modified": stat.st_mtime * 1000,
                })
        files.sort(key=lambda x: x["modified"], reverse=True)

        body = json.dumps(files).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_pacing_file(self, raw_name):
        filename = os.path.basename(urllib.parse.unquote(raw_name))
        if not (filename.startswith("predict_") and filename.endswith(".html")):
            self.send_error(403, "Forbidden")
            return

        filepath = BASE_DIR / filename
        if not filepath.exists():
            self.send_error(404, "Not found")
            return

        body = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if args and str(args[1]) not in ("200", "304"):
            super().log_message(fmt, *args)


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("0.0.0.0", PORT), PacingHandler) as httpd:
        print(f"\n🏔️  Trail Pacer — http://localhost:{PORT}\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nArrêté.")
