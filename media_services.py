"""
Media helpers for Gemini multi-modal gateway:
- image / video save to static/
- extract media from ModelOutput
- prompt builders for image/video/web research
"""
from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gemini_webapi import logger
from gemini_webapi.types.image import GeneratedImage, WebImage


def build_image_prompt(user_prompt: str) -> str:
    return (
        "Generate an image based on the following description. "
        "Actually create/generate the image (do not only describe it).\n\n"
        f"Description: {user_prompt.strip()}"
    )


def build_video_prompt(user_prompt: str) -> str:
    return (
        "Generate a video based on the following description. "
        "Actually create/generate the video clip if the feature is available "
        "(do not only describe it).\n\n"
        f"Description: {user_prompt.strip()}"
    )


def build_web_search_prompt(query: str) -> str:
    return (
        "You have access to web browsing / Google Search tools. "
        "Search the live web for up-to-date information and answer thoroughly.\n"
        "Include key facts, dates, and cite source URLs when possible.\n\n"
        f"Query: {query.strip()}"
    )


async def save_image_asset(img: Any, static_dir: Path) -> Optional[str]:
    """Save an image object; return filename relative to static/."""
    static_dir.mkdir(parents=True, exist_ok=True)
    try:
        kwargs = {}
        if isinstance(img, GeneratedImage):
            kwargs["full_size"] = True
        abs_path = await img.save(path=str(static_dir), verbose=False, **kwargs)
        return Path(abs_path).name
    except Exception as e:
        logger.warning(f"save_image_asset failed: {e}")
        return None


async def save_video_asset(video: Any, static_dir: Path) -> Dict[str, Optional[str]]:
    """
    Save a video object. Returns dict with local filenames and original URLs.
    GeneratedVideo.save may return {'video': path, 'video_thumbnail': path}.
    """
    static_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "filename": None,
        "thumbnail": None,
        "url": getattr(video, "url", None),
        "thumbnail_url": getattr(video, "thumbnail", None) or None,
        "title": getattr(video, "title", "[Video]"),
        "error": None,
    }
    try:
        saved = await video.save(path=str(static_dir), verbose=False)
        if isinstance(saved, dict):
            vpath = saved.get("video")
            tpath = saved.get("video_thumbnail")
            if vpath and vpath != "206":
                result["filename"] = Path(str(vpath)).name
            if tpath:
                result["thumbnail"] = Path(str(tpath)).name
            if vpath == "206":
                result["error"] = "partial_content_206"
        elif isinstance(saved, str) and saved and saved != "206":
            result["filename"] = Path(saved).name
        else:
            result["error"] = "save_returned_empty"
    except Exception as e:
        result["error"] = str(e)
        logger.warning(f"save_video_asset failed: {e}")
    return result


async def extract_and_save_media(
    output: Any,
    static_dir: Path,
    base_url: str,
) -> Dict[str, Any]:
    """
    From a Gemini ModelOutput, save images/videos and build public URLs.
    """
    base_url = base_url.rstrip("/")
    text = ""
    thoughts = None
    try:
        text = output.text or ""
        thoughts = getattr(output, "thoughts", None)
    except Exception:
        text = str(output)

    images_out: List[Dict[str, Any]] = []
    videos_out: List[Dict[str, Any]] = []
    pending_downloads: List[Dict[str, Any]] = []

    images = []
    try:
        images = list(output.images or [])
    except Exception:
        images = []

    for img in images:
        kind = "generated" if isinstance(img, GeneratedImage) else (
            "web" if isinstance(img, WebImage) else "image"
        )
        fn = await save_image_asset(img, static_dir)
        entry = {
            "type": kind,
            "title": getattr(img, "title", "[Image]"),
            "alt": getattr(img, "alt", "") or "",
            "source_url": getattr(img, "url", None),
            "url": f"{base_url}/static/{fn}" if fn else getattr(img, "url", None),
            "filename": fn,
        }
        if not fn and entry.get("source_url"):
            pending_downloads.append({
                "media_type": "image",
                "source_url": entry["source_url"],
                "title": entry["title"],
            })
        images_out.append(entry)

    videos = []
    try:
        videos = list(output.videos or [])
    except Exception:
        videos = []
    # also check generated_media if present
    try:
        media = list(output.media or [])
        for m in media:
            if m not in videos and getattr(m, "url", None):
                # GeneratedMedia may be audio/video
                videos.append(m)
    except Exception:
        pass

    for vid in videos:
        saved = await save_video_asset(vid, static_dir)
        entry = {
            "type": "video",
            "title": saved.get("title") or "[Video]",
            "source_url": saved.get("url"),
            "thumbnail_source_url": saved.get("thumbnail_url"),
            "url": f"{base_url}/static/{saved['filename']}" if saved.get("filename") else saved.get("url"),
            "thumbnail_url": (
                f"{base_url}/static/{saved['thumbnail']}"
                if saved.get("thumbnail")
                else saved.get("thumbnail_url")
            ),
            "filename": saved.get("filename"),
            "error": saved.get("error"),
        }
        if not saved.get("filename") and saved.get("url"):
            pending_downloads.append({
                "media_type": "video",
                "source_url": saved["url"],
                "title": entry["title"],
                "thumbnail_url": saved.get("thumbnail_url"),
            })
        videos_out.append(entry)

    # Markdown append for chat-style clients
    md_parts = []
    for im in images_out:
        if im.get("url"):
            md_parts.append(f"\n\n![{im.get('title') or 'Image'}]({im['url']})")
    for v in videos_out:
        if v.get("url"):
            md_parts.append(f"\n\n[🎬 {v.get('title') or 'Video'}]({v['url']})")
            if v.get("thumbnail_url"):
                md_parts.append(f"\n![thumbnail]({v['thumbnail_url']})")

    return {
        "text": text,
        "thoughts": thoughts,
        "images": images_out,
        "videos": videos_out,
        "markdown_extra": "".join(md_parts),
        "pending_downloads": pending_downloads,
    }


def public_url(base_url: str, filename: str) -> str:
    return f"{base_url.rstrip('/')}/static/{filename}"


async def wait_and_collect_media(
    client: Any,
    prompt: str,
    *,
    want: str = "any",  # any | image | video
    model: Any = None,
    poll_seconds: float = 8.0,
    max_wait: float = 180.0,
) -> Any:
    """
    Generate content, then if media is still missing (common for async video),
    poll the chat history until images/videos appear or timeout.

    For images, prefer model=gemini-3-flash — default UNSPECIFIED often hits
    a separate image-quota / capability path and returns limit text.
    """
    import asyncio

    kwargs = {}
    if model is not None:
        kwargs["model"] = model

    chat = client.start_chat()
    output = await client.generate_content(prompt, chat=chat, **kwargs)

    def _has_wanted(out: Any) -> bool:
        if out is None:
            return False
        try:
            imgs = list(out.images or [])
        except Exception:
            imgs = []
        try:
            vids = list(out.videos or [])
        except Exception:
            vids = []
        if want == "image":
            return len(imgs) > 0
        if want == "video":
            return len(vids) > 0
        return len(imgs) > 0 or len(vids) > 0

    if _has_wanted(output):
        return output

    text = ""
    try:
        text = (output.text or "").lower()
    except Exception:
        text = ""

    # Async generation hints from Gemini UI text
    pending_hints = (
        "ready",
        "generating",
        "creating",
        "almost",
        "đang tạo",
        "sẵn sàng",
        "video is ready",
        "image is ready",
    )
    should_poll = want in ("video", "image", "any") and (
        any(h in text for h in pending_hints) or not _has_wanted(output)
    )
    if not should_poll:
        return output

    cid = getattr(chat, "cid", None) or (output.metadata[0] if getattr(output, "metadata", None) else None)
    if not cid:
        return output

    logger.info(f"Media still pending after first response; polling chat {cid} up to {max_wait}s...")
    elapsed = 0.0
    best = output
    while elapsed < max_wait:
        await asyncio.sleep(poll_seconds)
        elapsed += poll_seconds
        try:
            # keep session warm
            if hasattr(client, "_send_bard_activity"):
                try:
                    await client._send_bard_activity()
                except Exception:
                    pass
            history = await client.read_chat(cid, limit=5)
            if not history or not getattr(history, "turns", None):
                continue
            for turn in history.turns:
                if getattr(turn, "role", None) != "model":
                    continue
                mo = getattr(turn, "model_output", None)
                if mo and _has_wanted(mo):
                    logger.info(f"Media recovered from history after {elapsed:.0f}s")
                    return mo
                if mo:
                    best = mo
        except Exception as e:
            logger.debug(f"media poll error: {e}")
    return best
