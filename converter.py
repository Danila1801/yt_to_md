"""Core logic: fetch YouTube metadata + transcript, build clean Markdown."""

import html
import json
import re
import urllib.request

import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)

PREFERRED_LANGS = ["en", "ru"]

# Bracketed caption noise like [Music], [Applause], [музыка], (applause)
_NOISE_RE = re.compile(r"[\[(][^\])]{0,40}[\])]")

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_PATTERNS = [
    re.compile(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})"),
]


class ConversionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def parse_video_id(url: str):
    url = url.strip()
    if _VIDEO_ID_RE.match(url):
        return url
    for pat in _URL_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _ydl(extra=None):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if extra:
        opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def expand_urls(urls):
    """Turn raw pasted lines into a flat list of videos. Returns (videos, errors)."""
    videos, errors, seen = [], [], set()

    def add(url, vid, title=None):
        if vid not in seen:
            seen.add(vid)
            videos.append({"url": url, "id": vid, "title": title})

    for raw in urls:
        line = raw.strip()
        if not line:
            continue
        vid = parse_video_id(line)
        if vid:
            add(f"https://www.youtube.com/watch?v={vid}", vid)
            continue
        if "list=" in line or "/playlist" in line:
            try:
                with _ydl({"extract_flat": "in_playlist"}) as ydl:
                    info = ydl.extract_info(line, download=False)
                entries = info.get("entries") or []
                for e in entries:
                    evid = e.get("id")
                    if evid and _VIDEO_ID_RE.match(evid):
                        add(f"https://www.youtube.com/watch?v={evid}", evid, e.get("title"))
                if not entries:
                    errors.append(f"Playlist is empty or private: {line}")
            except Exception:
                errors.append(f"Could not read playlist: {line}")
        else:
            errors.append(f"Not a YouTube URL: {line}")
    return videos, errors


def fetch_metadata(url: str) -> dict:
    try:
        with _ydl() as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if any(s in msg.lower() for s in ("private", "unavailable", "removed", "terminated")):
            raise ConversionError("unavailable", "Video is unavailable or private.")
        raise ConversionError("unknown", msg[:200])
    return {
        "id": info.get("id"),
        "title": info.get("title") or "Untitled",
        "channel": info.get("channel") or info.get("uploader") or "Unknown",
        "upload_date": info.get("upload_date"),  # "YYYYMMDD" or None
        "duration": info.get("duration"),  # seconds or None
        "language": info.get("language"),
        "chapters": info.get("chapters"),
        "subtitles": info.get("subtitles") or {},
        "automatic_captions": info.get("automatic_captions") or {},
    }


def _lang_order(info: dict):
    lang = (info.get("language") or "").lower()
    return ["ru", "en"] if lang.startswith("ru") else ["en", "ru"]


def fetch_transcript(video_id: str, info: dict, langs=None):
    """Returns (snippets, lang_label). snippets = [{"text","start","duration"}]."""
    langs = langs or _lang_order(info)
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
        transcript = None
        kind = None
        try:
            transcript = transcript_list.find_manually_created_transcript(langs)
            kind = "manual"
        except NoTranscriptFound:
            try:
                transcript = transcript_list.find_generated_transcript(langs)
                kind = "auto-generated"
            except NoTranscriptFound:
                raise ConversionError(
                    "no_captions", "No English or Russian captions available for this video."
                )
        fetched = transcript.fetch()
        snippets = [
            {"text": s.text, "start": s.start, "duration": s.duration} for s in fetched
        ]
        return snippets, f"{transcript.language_code} ({kind})"
    except ConversionError:
        raise
    except TranscriptsDisabled:
        # Captions disabled for the API sometimes still exist via yt-dlp
        return _fallback_ytdlp_captions(info, langs)
    except VideoUnavailable:
        raise ConversionError("unavailable", "Video is unavailable or private.")
    except Exception:
        # Blocked / parse errors -> try the yt-dlp caption URLs we already have
        return _fallback_ytdlp_captions(info, langs)


def _fallback_ytdlp_captions(info: dict, langs):
    for source, kind in ((info["subtitles"], "manual"), (info["automatic_captions"], "auto-generated")):
        for lang in langs:
            # auto caption keys can be "en", "en-orig", "en-US"...
            for key, tracks in source.items():
                if not key.lower().startswith(lang):
                    continue
                json3 = next((t for t in tracks if t.get("ext") == "json3"), None)
                if not json3 or not json3.get("url"):
                    continue
                try:
                    req = urllib.request.Request(
                        json3["url"], headers={"User-Agent": "Mozilla/5.0"}
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                    snippets = _parse_json3(data)
                    if snippets:
                        return snippets, f"{lang} ({kind})"
                except Exception:
                    continue
    raise ConversionError(
        "no_captions", "No English or Russian captions available for this video."
    )


def _parse_json3(data: dict):
    snippets = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        if not text.strip():
            continue
        start = event.get("tStartMs", 0) / 1000.0
        dur = event.get("dDurationMs", 0) / 1000.0
        snippets.append({"text": text, "start": start, "duration": dur})
    return snippets


def _clean_text(text: str) -> str:
    text = html.unescape(text)
    text = _NOISE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def merge_into_paragraphs(snippets, max_chars=900, gap_break=3.0, sentence_gap=2.0):
    """Merge caption snippets into readable paragraphs (no timestamps)."""
    cleaned = []
    for s in snippets:
        text = _clean_text(s["text"])
        if text:
            cleaned.append({"text": text, "start": s["start"], "end": s["start"] + (s["duration"] or 0)})
    if not cleaned:
        return []

    # 1) accumulate snippets into "sentence units"
    units = []  # [{"text","start","end"}]
    buf, buf_start, buf_end = [], None, None
    for i, s in enumerate(cleaned):
        if buf_start is None:
            buf_start = s["start"]
        buf.append(s["text"])
        buf_end = s["end"]
        text_so_far = " ".join(buf)
        next_gap = cleaned[i + 1]["start"] - s["end"] if i + 1 < len(cleaned) else 0
        if re.search(r"[.!?…]['\")\]]?$", text_so_far) or next_gap > sentence_gap:
            units.append({"text": text_so_far, "start": buf_start, "end": buf_end})
            buf, buf_start, buf_end = [], None, None
    if buf:
        units.append({"text": " ".join(buf), "start": buf_start, "end": buf_end})

    # 2) group units into paragraphs
    paragraphs, para, para_len, last_end = [], [], 0, None
    for u in units:
        gap = (u["start"] - last_end) if last_end is not None else 0
        if para and (para_len + len(u["text"]) > max_chars or gap > gap_break):
            paragraphs.append(" ".join(para))
            para, para_len = [], 0
        para.append(u["text"])
        para_len += len(u["text"]) + 1
        last_end = u["end"]
    if para:
        paragraphs.append(" ".join(para))
    return paragraphs


def split_by_chapters(snippets, chapters):
    """Returns [(chapter_title_or_None, snippets)] buckets ordered by time."""
    if not chapters:
        return [(None, snippets)]
    sections = []
    for ch in chapters:
        start = ch.get("start_time") or 0
        end = ch.get("end_time")
        bucket = [
            s for s in snippets
            if s["start"] >= start and (end is None or s["start"] < end)
        ]
        if bucket:
            sections.append((ch.get("title") or "Chapter", bucket))
    return sections or [(None, snippets)]


def _format_duration(seconds):
    if not seconds:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _format_date(upload_date):
    if upload_date and len(upload_date) == 8:
        return f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    return None


def build_markdown(meta: dict, sections, lang_label: str) -> str:
    lines = [f"# {meta['title']}", ""]
    lines.append(f"- **Channel:** {meta['channel']}")
    lines.append(f"- **URL:** https://www.youtube.com/watch?v={meta['id']}")
    date = _format_date(meta.get("upload_date"))
    if date:
        lines.append(f"- **Uploaded:** {date}")
    dur = _format_duration(meta.get("duration"))
    if dur:
        lines.append(f"- **Duration:** {dur}")
    lines.append(f"- **Captions:** {lang_label}")
    lines += ["", "---", ""]
    for title, paragraphs in sections:
        lines.append(f"## {title or 'Transcript'}")
        lines.append("")
        for p in paragraphs:
            lines.append(p)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(title: str, video_id: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if name.upper().split(".")[0] in _RESERVED:
        name = "_" + name
    name = name[:80].strip(" .") or "video"
    return f"{name} [{video_id}].md"


def convert_video(url: str) -> dict:
    vid = parse_video_id(url)
    if not vid:
        raise ConversionError("bad_url", "Not a valid YouTube URL.")
    meta = fetch_metadata(f"https://www.youtube.com/watch?v={vid}")
    snippets, lang_label = fetch_transcript(vid, meta)
    if not snippets:
        raise ConversionError("no_captions", "Captions were empty for this video.")
    sections = []
    for title, bucket in split_by_chapters(snippets, meta.get("chapters")):
        paragraphs = merge_into_paragraphs(bucket)
        if paragraphs:
            sections.append((title, paragraphs))
    if not sections:
        raise ConversionError("no_captions", "Captions were empty for this video.")
    markdown = build_markdown(meta, sections, lang_label)
    return {
        "ok": True,
        "video_id": vid,
        "title": meta["title"],
        "filename": sanitize_filename(meta["title"], vid),
        "markdown": markdown,
        "meta": {
            "channel": meta["channel"],
            "upload_date": _format_date(meta.get("upload_date")),
            "duration": _format_duration(meta.get("duration")),
            "captions": lang_label,
        },
    }
