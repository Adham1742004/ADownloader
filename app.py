import os
import threading
import time
import uuid
from pathlib import Path
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

HERE = Path(__file__).parent

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/yt-downloads"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _run_download(job_id: str, url: str, options: dict):
    output_template = str(DOWNLOAD_DIR / job_id / "%(title)s.%(ext)s")
    (DOWNLOAD_DIR / job_id).mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [lambda d: _progress_hook(job_id, d)],
    }

    fmt = options.get("format", "best")
    if fmt == "audio":
        ydl_opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio/best",
        })
    elif fmt == "video":
        quality = options.get("quality", "best")
        if quality == "best":
            ydl_opts["format"] = "best"
        elif quality == "720p":
            ydl_opts["format"] = "best[height<=720]/best"
        elif quality == "480p":
            ydl_opts["format"] = "best[height<=480]/best"
        elif quality == "360p":
            ydl_opts["format"] = "best[height<=360]/best"
        else:
            ydl_opts["format"] = "best"
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"

    with jobs_lock:
        jobs[job_id]["status"] = "downloading"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not Path(filename).exists():
                found = list((DOWNLOAD_DIR / job_id).iterdir())
                filename = str(found[0]) if found else filename

        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "filename": Path(filename).name,
                "filepath": filename,
                "title": info.get("title", ""),
                "duration": info.get("duration"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader"),
                "finished_at": time.time(),
            })
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({
                "status": "error",
                "error": str(e),
                "finished_at": time.time(),
            })


def _progress_hook(job_id: str, d: dict):
    if d["status"] == "downloading":
        with jobs_lock:
            jobs[job_id]["progress"] = {
                "downloaded_bytes": d.get("downloaded_bytes"),
                "total_bytes": d.get("total_bytes") or d.get("total_bytes_estimate"),
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "percent": d.get("_percent_str", "").strip(),
            }
    elif d["status"] == "finished":
        with jobs_lock:
            jobs[job_id]["status"] = "processing"


@app.route("/api/info", methods=["POST"])
def get_info():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or f.get("format_note"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "tbr": f.get("tbr"),
            })

        return jsonify({
            "title": info.get("title"),
            "description": info.get("description", "")[:500] if info.get("description") else "",
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
            "webpage_url": info.get("webpage_url"),
            "formats": formats,
        })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    options = {
        "format": body.get("format", "video"),
        "quality": body.get("quality", "best"),
        "container": body.get("container", "mp4"),
        "audio_codec": body.get("audio_codec", "mp3"),
        "audio_quality": body.get("audio_quality", "192"),
    }

    job_id = str(uuid.uuid4())

    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "url": url,
            "status": "queued",
            "progress": {},
            "created_at": time.time(),
        }

    thread = threading.Thread(target=_run_download, args=(job_id, url, options), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"}), 202


@app.route("/api/status/<job_id>", methods=["GET"])
def job_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    safe = {k: v for k, v in job.items() if k != "filepath"}
    return jsonify(safe)


@app.route("/api/file/<job_id>", methods=["GET"])
def download_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] != "done":
        return jsonify({"error": f"Job not ready (status: {job['status']})"}), 400

    filepath = job.get("filepath")
    if not filepath or not Path(filepath).exists():
        found = list((DOWNLOAD_DIR / job_id).iterdir())
        if not found:
            return jsonify({"error": "File not found on disk"}), 404
        filepath = str(found[0])

    return send_file(
        filepath,
        as_attachment=True,
        download_name=Path(filepath).name,
    )


@app.route("/api/jobs", methods=["GET"])
def list_jobs():
    with jobs_lock:
        result = [{k: v for k, v in j.items() if k != "filepath"} for j in jobs.values()]
    result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return jsonify(result)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id: str):
    with jobs_lock:
        job = jobs.pop(job_id, None)

    if not job:
        return jsonify({"error": "Job not found"}), 404

    import shutil
    job_dir = DOWNLOAD_DIR / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)

    return jsonify({"message": "Job deleted"})


@app.route("/", methods=["GET"])
def index():
    return send_file(HERE / "index.html")


@app.route("/api/healthz", methods=["GET"])
def health():
    return jsonify({"status": "ok", "yt_dlp_version": yt_dlp.version.__version__})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
