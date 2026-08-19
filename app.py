import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)
from PIL import Image
from werkzeug.utils import secure_filename

load_dotenv()

SECRET_KEY = os.environ.get("SECRET_KEY")
APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

if not (SECRET_KEY and APP_USERNAME and APP_PASSWORD):
    raise RuntimeError(
        "SECRET_KEY, APP_USERNAME and APP_PASSWORD must be set (see .env.example)"
    )

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4096"))
ALLOWED_VIDEO_EXT = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

UPLOAD_DIR = Path(app.instance_path) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

CHOMP_DIR = Path(app.instance_path) / "chomps"
CHOMP_DIR.mkdir(parents=True, exist_ok=True)
CHOMP_SEGMENT_SECONDS = 10

CLIPS_DIR = Path(app.instance_path) / "clips"
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

THUMB_COUNT = 40
THUMB_W = 100
THUMB_H = 56


# ---------------------------------------------------------------- helpers --

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = uuid.uuid4().hex
        session["csrf_token"] = token
    return token


def check_csrf():
    token = session.get("csrf_token")
    submitted = request.form.get("csrf_token")
    if not token or not submitted or token != submitted:
        abort(400, "Invalid or missing CSRF token")


app.jinja_env.globals["csrf_token"] = csrf_token


def redirect_to_index():
    """Redirect to '/', forcing a real browser navigation for htmx requests
    instead of letting htmx swap the followed page into a partial target."""
    resp = redirect(url_for("index"))
    if request.headers.get("HX-Request"):
        resp.headers["HX-Redirect"] = url_for("index")
    return resp


def current_video_path():
    for f in UPLOAD_DIR.glob("video.*"):
        return f
    return None


def thumbs_path():
    return UPLOAD_DIR / "thumbs.jpg"


def clear_thumbs():
    thumbs_path().unlink(missing_ok=True)


def clear_chomps():
    shutil.rmtree(CHOMP_DIR, ignore_errors=True)
    CHOMP_DIR.mkdir(parents=True, exist_ok=True)


def chomp_batch_dir(batch_id):
    if not re.fullmatch(r"[0-9a-f]{32}", batch_id):
        abort(404)
    return CHOMP_DIR / batch_id


CHOMP_STATE_LOCK = threading.Lock()


def chomp_state_path():
    return CHOMP_DIR / "state.json"


def read_chomp_state():
    path = chomp_state_path()
    if not path.is_file():
        return None
    try:
        with path.open("r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_chomp_state(state):
    path = chomp_state_path()
    tmp = path.with_suffix(".json.tmp")
    with CHOMP_STATE_LOCK:
        tmp.write_text(json.dumps(state))
        tmp.replace(path)


CLIPS_LIST_LOCK = threading.Lock()


def clips_list_path():
    return CLIPS_DIR / "list.json"


def read_clips_list():
    path = clips_list_path()
    if not path.is_file():
        return []
    try:
        with path.open("r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def add_clip(entry):
    path = clips_list_path()
    tmp = path.with_suffix(".json.tmp")
    with CLIPS_LIST_LOCK:
        clips = read_clips_list()
        clips.insert(0, entry)
        tmp.write_text(json.dumps(clips))
        tmp.replace(path)


def clear_clips():
    shutil.rmtree(CLIPS_DIR, ignore_errors=True)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)


def generate_thumbnails(video_path, duration):
    """Build a single horizontal filmstrip sprite (THUMB_COUNT frames tiled
    side by side) for the trim timeline. Frames are extracted in parallel
    since each ffmpeg call is a cheap single-frame seek-and-grab."""
    if not FFMPEG or not duration or duration <= 0:
        return

    with tempfile.TemporaryDirectory() as tmp:
        def grab(i):
            t = duration * i / THUMB_COUNT
            out = Path(tmp) / f"{i:03d}.jpg"
            cmd = [
                FFMPEG, "-y", "-ss", str(t), "-i", str(video_path),
                "-frames:v", "1", "-vf", f"scale={THUMB_W}:{THUMB_H}",
                "-q:v", "4", str(out),
            ]
            subprocess.run(cmd, capture_output=True, timeout=15)
            return out if out.exists() else None

        with ThreadPoolExecutor(max_workers=6) as pool:
            frames = list(pool.map(grab, range(THUMB_COUNT)))
        frames = [f for f in frames if f]
        if not frames:
            return

        sprite = Image.new("RGB", (THUMB_W * len(frames), THUMB_H), "black")
        for i, frame in enumerate(frames):
            with Image.open(frame) as im:
                sprite.paste(im, (i * THUMB_W, 0))
        sprite.save(thumbs_path(), "JPEG", quality=72)


def video_duration(path):
    if not FFPROBE:
        return None
    try:
        out = subprocess.run(
            [
                FFPROBE,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return round(float(out.stdout.strip()), 1)
    except Exception:
        return None


def format_hms(seconds):
    if seconds is None:
        return None
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes:02d}:{secs:04.1f}"


app.jinja_env.filters["hms"] = format_hms


@app.context_processor
def inject_has_video():
    return {"nav_has_video": bool(current_video_path())}


# ------------------------------------------------------------------ auth --

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == APP_USERNAME and password == APP_PASSWORD:
            session.clear()
            session["logged_in"] = True
            return redirect(request.args.get("next") or url_for("index"))
        flash("Incorrect username or password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ main --

@app.route("/")
@login_required
def index():
    video = current_video_path()
    duration = video_duration(video) if video else None
    return render_template(
        "index.html",
        has_video=bool(video),
        duration=duration,
        has_thumbs=thumbs_path().is_file(),
        ffmpeg_ok=bool(FFMPEG and FFPROBE),
    )


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    check_csrf()
    file = request.files.get("video")
    if not file or file.filename == "":
        flash("No file selected.")
        return redirect_to_index()

    ext = secure_filename(file.filename).rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_VIDEO_EXT:
        flash("Unsupported file type: " + ", ".join(sorted(ALLOWED_VIDEO_EXT)))
        return redirect_to_index()

    for old in UPLOAD_DIR.glob("video.*"):
        old.unlink(missing_ok=True)
    clear_thumbs()
    clear_chomps()
    clear_clips()

    dest = UPLOAD_DIR / f"video.{ext}"
    file.save(dest)
    generate_thumbnails(dest, video_duration(dest))
    flash("Video uploaded.")
    return redirect_to_index()


@app.route("/clear-all", methods=["POST"])
@login_required
def clear_all():
    check_csrf()
    for f in UPLOAD_DIR.glob("video.*"):
        f.unlink(missing_ok=True)
    clear_thumbs()
    clear_chomps()
    clear_clips()
    flash("Cleared.")
    return redirect_to_index()


@app.route("/thumbnails")
@login_required
def serve_thumbnails():
    if not thumbs_path().is_file():
        abort(404)
    return send_from_directory(UPLOAD_DIR, "thumbs.jpg", conditional=True)


@app.route("/video")
@login_required
def serve_video():
    video = current_video_path()
    if not video:
        abort(404)
    return send_from_directory(UPLOAD_DIR, video.name, conditional=True)


@app.route("/generate", methods=["POST"])
@login_required
def generate():
    check_csrf()
    video = current_video_path()
    if not video:
        abort(404)

    duration = video_duration(video)

    def to_float(name, default):
        try:
            return float(request.form.get(name, default))
        except (TypeError, ValueError):
            return default

    start = max(0.0, to_float("start", 0))
    end = to_float("end", start + 1)
    fps = int(max(1, min(30, to_float("fps", 15))))
    width = int(max(120, min(1280, to_float("width", 480))))

    if duration is not None:
        end = min(end, duration)
    if end <= start:
        flash("End time must be after start time.")
        return redirect_to_index()
    if end - start > 15:
        flash("Clip must be 15 seconds or shorter.")
        return redirect_to_index()

    disk_filename = f"{uuid.uuid4().hex}.webp"
    out_path = CLIPS_DIR / disk_filename

    webp_cmd = [
        FFMPEG, "-y",
        "-ss", str(start), "-to", str(end),
        "-i", str(video),
        "-vf", f"fps={fps},scale={width}:-1:flags=lanczos",
        "-an", "-loop", "0",
        "-vcodec", "libwebp", "-lossless", "0", "-q:v", "85",
        "-compression_level", "6",
        str(out_path),
    ]

    try:
        subprocess.run(webp_cmd, capture_output=True, check=True, timeout=120)
        data = out_path.read_bytes()
    except subprocess.CalledProcessError as exc:
        out_path.unlink(missing_ok=True)
        flash("ffmpeg failed: " + exc.stderr.decode(errors="ignore")[-500:])
        return redirect_to_index()
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        flash("ffmpeg timed out.")
        return redirect_to_index()

    download_name = f"clip_{start:g}-{end:g}s.webp"
    size_mb = round(len(data) / (1024 * 1024), 2)
    add_clip({
        "filename": disk_filename,
        "download_name": download_name,
        "label": f"{format_hms(start)} – {format_hms(end)}",
        "size_mb": size_mb,
    })

    return render_template(
        "partials/result.html",
        gif_data=base64.b64encode(data).decode("ascii"),
        filename=download_name,
        size_mb=size_mb,
    )


def make_chomp_clip(batch_dir, video, index, start, end, fps, width):
    out = batch_dir / f"clip_{index:03d}.webp"
    cmd = [
        FFMPEG, "-y",
        "-ss", str(start), "-to", str(end),
        "-i", str(video),
        "-vf", f"fps={fps},scale={width}:-1:flags=lanczos",
        "-an", "-loop", "0",
        "-vcodec", "libwebp", "-lossless", "0", "-q:v", "85",
        "-compression_level", "6",
        str(out),
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=120)
    return {
        "index": index,
        "filename": out.name,
        "label": f"{format_hms(start)} – {format_hms(end)}",
        "size_mb": round(out.stat().st_size / (1024 * 1024), 2),
    }


@app.route("/chomp-stream")
@login_required
def chomp_stream():
    token = request.args.get("csrf_token")
    if not token or token != session.get("csrf_token"):
        abort(400, "Invalid or missing CSRF token")

    video = current_video_path()
    if not video:
        abort(404)

    duration = video_duration(video)
    if not duration:
        abort(400, "Could not read video duration.")

    def to_float(name, default):
        try:
            return float(request.args.get(name, default))
        except (TypeError, ValueError):
            return default

    fps = int(max(1, min(30, to_float("fps", 15))))
    width = int(max(120, min(1280, to_float("width", 480))))

    segments = []
    t = 0.0
    while t < duration:
        segments.append((t, min(t + CHOMP_SEGMENT_SECONDS, duration)))
        t += CHOMP_SEGMENT_SECONDS
    segments = [(s, e) for s, e in segments if e - s >= 0.2]

    # a new chomp run replaces whatever the previous run left on disk
    clear_chomps()
    batch_id = uuid.uuid4().hex
    batch_dir = CHOMP_DIR / batch_id
    batch_dir.mkdir(parents=True)

    def sse(event_name, data):
        return f"event: {event_name}\ndata: {json.dumps(data)}\n\n"

    def generate():
        yield sse("total", {"total": len(segments)})
        if not segments:
            yield sse("error", {"message": "Video too short to chomp."})
            return

        write_chomp_state({
            "batch_id": batch_id, "total": len(segments),
            "clips": [], "status": "running", "message": None,
        })

        done_clips = []
        done = 0
        with ThreadPoolExecutor(max_workers=min(4, len(segments))) as pool:
            futures = [
                pool.submit(make_chomp_clip, batch_dir, video, i, s, e, fps, width)
                for i, (s, e) in enumerate(segments)
            ]
            for fut in as_completed(futures):
                done += 1
                try:
                    clip = fut.result()
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    yield sse("clip_error", {"done": done, "total": len(segments)})
                    continue
                done_clips.append(clip)
                write_chomp_state({
                    "batch_id": batch_id, "total": len(segments),
                    "clips": done_clips, "status": "running", "message": None,
                })
                event_clip = dict(clip, done=done, total=len(segments))
                event_clip["url"] = url_for("serve_chomp_clip", batch_id=batch_id, filename=clip["filename"])
                yield sse("clip", event_clip)

        if not done_clips:
            clear_chomps()
            yield sse("error", {"message": "Chomp failed: no clips were generated."})
            return

        write_chomp_state({
            "batch_id": batch_id, "total": len(segments),
            "clips": done_clips, "status": "done", "message": None,
        })
        yield sse("done", {
            "batch_id": batch_id,
            "download_all": url_for("download_chomp_all", batch_id=batch_id),
        })

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/chomp-status")
@login_required
def chomp_status():
    state = read_chomp_state()
    if not state:
        return {"status": "empty"}

    clips = []
    for clip in state["clips"]:
        c = dict(clip)
        c["url"] = url_for("serve_chomp_clip", batch_id=state["batch_id"], filename=clip["filename"])
        clips.append(c)

    resp = {"status": state["status"], "total": state["total"], "clips": clips}
    if state["status"] == "done":
        resp["download_all"] = url_for("download_chomp_all", batch_id=state["batch_id"])
    if state.get("message"):
        resp["message"] = state["message"]
    return resp


@app.route("/clip/<filename>")
@login_required
def serve_clip(filename):
    filename = secure_filename(filename)
    if not filename or not (CLIPS_DIR / filename).is_file():
        abort(404)
    return send_from_directory(CLIPS_DIR, filename, conditional=True)


@app.route("/clip-list")
@login_required
def clip_list():
    clips = read_clips_list()
    for c in clips:
        c["url"] = url_for("serve_clip", filename=c["filename"])
    return {"clips": clips}


@app.route("/chomp/<batch_id>/<filename>")
@login_required
def serve_chomp_clip(batch_id, filename):
    batch_dir = chomp_batch_dir(batch_id)
    filename = secure_filename(filename)
    if not filename or not (batch_dir / filename).is_file():
        abort(404)
    return send_from_directory(batch_dir, filename, conditional=True)


@app.route("/chomp/<batch_id>/download-all")
@login_required
def download_chomp_all(batch_id):
    batch_dir = chomp_batch_dir(batch_id)
    if not batch_dir.is_dir():
        abort(404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(batch_dir.glob("*.webp")):
            zf.write(f, arcname=f.name)
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"chomp_{batch_id}.zip",
    )


if __name__ == "__main__":
    app.run(debug=True)
