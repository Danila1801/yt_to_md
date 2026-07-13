"""Flask app serving the yt_to_md web UI and API."""

import io
import os
import threading
import webbrowser
import zipfile

from flask import Flask, jsonify, request, send_file, send_from_directory

from converter import ConversionError, convert_video, expand_urls

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

app = Flask(__name__, static_folder="static", static_url_path="/static")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/expand")
def api_expand():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    videos, errors = expand_urls(urls)
    return jsonify({"videos": videos, "errors": errors})


@app.post("/api/convert")
def api_convert():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    try:
        result = convert_video(url)
    except ConversionError as e:
        return jsonify({"ok": False, "error_code": e.code, "error_message": e.message})
    except Exception as e:  # never let one video 500 the batch
        return jsonify({"ok": False, "error_code": "unknown",
                        "error_message": str(e)[:200]})
    path = os.path.join(OUTPUT_DIR, result["filename"])
    with open(path, "w", encoding="utf-8") as f:
        f.write(result["markdown"])
    return jsonify(result)


@app.post("/api/download_zip")
def api_download_zip():
    data = request.get_json(silent=True) or {}
    filenames = data.get("filenames") or []
    buf = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in filenames:
            if os.path.basename(name) != name:  # no path traversal
                continue
            path = os.path.join(OUTPUT_DIR, name)
            if os.path.isfile(path):
                zf.write(path, arcname=name)
                added += 1
    if not added:
        return jsonify({"error": "No files to zip."}), 400
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                     as_attachment=True, download_name="transcripts.zip")


if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    print("yt_to_md running at http://127.0.0.1:5000  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=5000, debug=False)
