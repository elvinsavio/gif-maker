import base64
import os
import shutil
import subprocess
import tempfile
import uuid
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

load_dotenv()

SECRET_KEY = os.environ.get("SECRET_KEY")
APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

if not (SECRET_KEY and APP_USERNAME and APP_PASSWORD):
    raise RuntimeError(
        "SECRET_KEY, APP_USERNAME and APP_PASSWORD must be set (see .env.example)"
    )

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "1024"))
ALLOWED_VIDEO_EXT = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

UPLOAD_DIR = Path(app.instance_path) / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


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

    dest = UPLOAD_DIR / f"video.{ext}"
    file.save(dest)
    flash("Video uploaded.")
    return redirect_to_index()


@app.route("/clear-all", methods=["POST"])
@login_required
def clear_all():
    check_csrf()
    for f in UPLOAD_DIR.glob("video.*"):
        f.unlink(missing_ok=True)
    flash("Cleared.")
    return redirect_to_index()


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

    fd, tmp_path = tempfile.mkstemp(suffix=".webp")
    os.close(fd)
    tmp_path = Path(tmp_path)

    webp_cmd = [
        FFMPEG, "-y",
        "-ss", str(start), "-to", str(end),
        "-i", str(video),
        "-vf", f"fps={fps},scale={width}:-1:flags=lanczos",
        "-an", "-loop", "0",
        "-vcodec", "libwebp", "-lossless", "0", "-q:v", "85",
        "-compression_level", "6",
        str(tmp_path),
    ]

    try:
        subprocess.run(webp_cmd, capture_output=True, check=True, timeout=120)
        data = tmp_path.read_bytes()
    except subprocess.CalledProcessError as exc:
        flash("ffmpeg failed: " + exc.stderr.decode(errors="ignore")[-500:])
        return redirect_to_index()
    except subprocess.TimeoutExpired:
        flash("ffmpeg timed out.")
        return redirect_to_index()
    finally:
        tmp_path.unlink(missing_ok=True)

    return render_template(
        "partials/result.html",
        gif_data=base64.b64encode(data).decode("ascii"),
        filename=f"clip_{start:g}-{end:g}s.webp",
        size_kb=round(len(data) / 1024),
    )


if __name__ == "__main__":
    app.run(debug=True)
