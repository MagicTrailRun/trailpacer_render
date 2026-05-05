import os
import json
import urllib.parse
from pathlib import Path
from flask import Flask, jsonify, send_file, abort, Response

BASE_DIR = Path(__file__).parent
PUBLIC_DIR = BASE_DIR / "public"

app = Flask(__name__, static_folder=str(PUBLIC_DIR), static_url_path="")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


@app.route("/supabase-config.js")
def supabase_config():
    js = f"window.SUPABASE_URL='{SUPABASE_URL}';\nwindow.SUPABASE_ANON_KEY='{SUPABASE_ANON_KEY}';\n"
    return Response(js, mimetype="application/javascript")


@app.route("/api/files")
def file_list():
    files = []
    for f in BASE_DIR.iterdir():
        if f.name.startswith("predict_") and f.name.endswith(".html"):
            stat = f.stat()
            files.append({"name": f.name, "size": stat.st_size, "modified": stat.st_mtime * 1000})
    files.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(files)


@app.route("/pacing/<filename>")
def serve_pacing(filename):
    filename = os.path.basename(filename)
    if not (filename.startswith("predict_") and filename.endswith(".html")):
        abort(403)
    filepath = BASE_DIR / filename
    if not filepath.exists():
        abort(404)
    return send_file(filepath, mimetype="text/html")


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def static_files(path):
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
