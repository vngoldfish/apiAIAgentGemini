"""
Reliable multi-backend media pipeline.

Image order:
  1) Gemini Web + model gemini-3-flash (works for Ultra when host network OK)
  2) Official Google AI Studio API (responseModalities IMAGE)
  3) Clear error with remediation

Video order:
  1) Gemini Web video (may fail / no URL)
  2) Official Veo if API key supports it
  3) Guaranteed fallback: animate keyframe image → MP4 (ffmpeg or imageio-ffmpeg)
"""
from __future__ import annotations

import asyncio
import base64
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from gemini_webapi import logger
    from gemini_webapi.constants import Model
except Exception:  # pragma: no cover
    class _L:
        def info(self, *a, **k): print(*a)
        def warning(self, *a, **k): print(*a)
        def error(self, *a, **k): print(*a)
        def debug(self, *a, **k): pass
    logger = _L()
    Model = None


# Official image models to try (first that works wins)
OFFICIAL_IMAGE_MODELS = [
    "gemini-2.5-flash-image-preview",
    "gemini-2.0-flash-preview-image-generation",
    "gemini-2.0-flash-exp-image-generation",
]

OFFICIAL_VIDEO_MODELS = [
    "veo-2.0-generate-001",
    "veo-3.0-generate-preview",
]


def _static_dir(root: Path) -> Path:
    d = root / "static"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def generate_image_web_flash(client: Any, prompt: str) -> Tuple[Optional[Any], str]:
    """Gemini Web client with Flash model — proven path for image gen."""
    from media_services import build_image_prompt, wait_and_collect_media

    p = build_image_prompt(prompt)
    model = Model.BASIC_FLASH if Model is not None else "gemini-3-flash"
    try:
        out = await wait_and_collect_media(
            client, p, want="image", model=model, poll_seconds=5.0, max_wait=60.0
        )
        n = 0
        try:
            n = len(out.images or [])
        except Exception:
            n = 0
        text = ""
        try:
            text = out.text or ""
        except Exception:
            pass
        if n > 0:
            return out, "web_flash"
        return out, f"web_flash_no_image:{text[:120]}"
    except Exception as e:
        return None, f"web_flash_error:{e}"


async def generate_image_official_api(api_key: str, prompt: str, static: Path) -> Dict[str, Any]:
    """Official Generative Language API image generation."""
    errors = []
    async with httpx.AsyncClient(timeout=120.0, verify=False) as http:
        for model in OFFICIAL_IMAGE_MODELS:
            url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent?key={api_key}"
            )
            payload = {
                "contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
            }
            try:
                resp = await http.post(url, json=payload)
                if resp.status_code != 200:
                    errors.append(f"{model}:{resp.status_code}:{resp.text[:200]}")
                    continue
                data = resp.json()
                parts = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [])
                )
                text_bits = []
                files = []
                for part in parts:
                    if "text" in part:
                        text_bits.append(part["text"])
                    inline = part.get("inlineData") or part.get("inline_data")
                    if inline and inline.get("data"):
                        raw = base64.b64decode(inline["data"])
                        mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                        ext = ".png"
                        if "jpeg" in mime or "jpg" in mime:
                            ext = ".jpg"
                        elif "webp" in mime:
                            ext = ".webp"
                        fn = f"official_{secrets.token_hex(6)}{ext}"
                        (static / fn).write_bytes(raw)
                        files.append(fn)
                if files:
                    return {
                        "ok": True,
                        "backend": f"official:{model}",
                        "filenames": files,
                        "text": "\n".join(text_bits),
                    }
                errors.append(f"{model}:no_inline_image")
            except Exception as e:
                errors.append(f"{model}:{e}")
    return {"ok": False, "errors": errors}


async def generate_video_web(client: Any, prompt: str, image_path: Optional[Path] = None) -> Tuple[Optional[Any], str]:
    from media_services import build_video_prompt, wait_and_collect_media

    p = build_video_prompt(prompt)
    model = Model.BASIC_FLASH if Model is not None else "gemini-3-flash"
    files = [str(image_path)] if image_path and image_path.exists() else None
    try:
        # wait_and_collect_media doesn't pass files — call generate then poll if needed
        chat = client.start_chat()
        kwargs = {"model": model, "chat": chat}
        if files:
            kwargs["files"] = files
        out = await client.generate_content(p, **kwargs)
        n = 0
        try:
            n = len(out.videos or [])
        except Exception:
            n = 0
        if n > 0:
            return out, "web_video"
        # poll history
        out2 = await wait_and_collect_media(
            client, p, want="video", model=model, poll_seconds=8.0, max_wait=120.0
        )
        try:
            n2 = len(out2.videos or [])
        except Exception:
            n2 = 0
        if n2 > 0:
            return out2, "web_video_polled"
        text = ""
        try:
            text = (out2.text or out.text or "")[:160]
        except Exception:
            pass
        return out2 or out, f"web_video_no_url:{text}"
    except Exception as e:
        return None, f"web_video_error:{e}"


def _ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


def animate_image_to_mp4(
    image_path: Path,
    out_path: Path,
    duration: float = 5.0,
    fps: int = 24,
) -> Path:
    """
    Guaranteed video fallback: Ken Burns-ish motion from a still image.
    Uses system ffmpeg if present, else imageio-ffmpeg binary.
    """
    image_path = Path(image_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ff = _ffmpeg_path()
    # Prefer imageio-ffmpeg bundled binary when system ffmpeg missing
    if not ff:
        try:
            import imageio_ffmpeg

            ff = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ff = None

    if not ff:
        raise RuntimeError(
            "No ffmpeg available. Install ffmpeg or: pip install imageio-ffmpeg"
        )

    # zoompan filter: slow zoom in
    frames = max(int(duration * fps), fps)
    # scale to even dimensions for yuv420p
    vf = (
        f"scale=1280:720:force_original_aspect_ratio=increase,"
        f"crop=1280:720,"
        f"zoompan=z='min(zoom+0.0008,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1280x720:fps={fps},"
        f"format=yuv420p"
    )
    cmd = [
        ff,
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-vf",
        vf,
        "-t",
        str(duration),
        "-r",
        str(fps),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-500:] if proc.stderr else proc.returncode}")
    return out_path


async def reliable_generate_image(
    *,
    prompt: str,
    root: Path,
    base_url: str,
    web_clients: List[Tuple[Any, str]],
    official_api_keys: List[str],
) -> Dict[str, Any]:
    """
    Return {ok, backend, images:[{url,filename}], text, attempts}
    """
    from media_services import extract_and_save_media

    static = _static_dir(root)
    attempts: List[str] = []
    base_url = base_url.rstrip("/")

    # 1) Web flash for each GeminiClient
    for cl, acc_id in web_clients:
        out, tag = await generate_image_web_flash(cl, prompt)
        attempts.append(f"{acc_id}:{tag}")
        if out is not None:
            media = await extract_and_save_media(out, static, base_url)
            imgs = [im for im in (media.get("images") or []) if im.get("filename") or im.get("url")]
            if imgs:
                return {
                    "ok": True,
                    "backend": f"web_flash:{acc_id}",
                    "images": imgs,
                    "text": media.get("text") or "",
                    "attempts": attempts,
                    "pending_downloads": media.get("pending_downloads") or [],
                }

    # 2) Official API keys
    for key in official_api_keys:
        res = await generate_image_official_api(key, prompt, static)
        attempts.append(res.get("backend") or str(res.get("errors")))
        if res.get("ok"):
            images = []
            for fn in res.get("filenames") or []:
                images.append(
                    {
                        "type": "official",
                        "url": f"{base_url}/static/{fn}",
                        "filename": fn,
                        "title": "[Generated Image]",
                    }
                )
            return {
                "ok": True,
                "backend": res.get("backend"),
                "images": images,
                "text": res.get("text") or "",
                "attempts": attempts,
                "pending_downloads": [],
            }

    return {
        "ok": False,
        "backend": None,
        "images": [],
        "text": "",
        "attempts": attempts,
        "error": (
            "All image backends failed. "
            "Tips: run API on HOST (not Docker on Windows), keep Extension cookies fresh, "
            "add a Google AI Studio API key as provider for official Imagen/Gemini image."
        ),
    }


async def reliable_generate_video(
    *,
    prompt: str,
    root: Path,
    base_url: str,
    web_clients: List[Tuple[Any, str]],
    official_api_keys: List[str],
    prefer_image_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Always try to return an MP4.
    Pipeline: optional/use keyframe image → real video backends → ffmpeg animate fallback.
    """
    from media_services import extract_and_save_media

    static = _static_dir(root)
    base_url = base_url.rstrip("/")
    attempts: List[str] = []
    keyframe: Optional[Path] = prefer_image_path if prefer_image_path and prefer_image_path.exists() else None
    keyframe_url = None

    # Ensure we have a keyframe image (needed for guaranteed fallback)
    if keyframe is None:
        img_res = await reliable_generate_image(
            prompt=prompt,
            root=root,
            base_url=base_url,
            web_clients=web_clients,
            official_api_keys=official_api_keys,
        )
        attempts.append(f"keyframe:{img_res.get('backend')}")
        if img_res.get("ok") and img_res.get("images"):
            fn = img_res["images"][0].get("filename")
            if fn:
                keyframe = static / fn
                keyframe_url = img_res["images"][0].get("url")
        else:
            attempts.extend(img_res.get("attempts") or [])

    # 1) Gemini web video
    for cl, acc_id in web_clients:
        out, tag = await generate_video_web(cl, prompt, keyframe)
        attempts.append(f"{acc_id}:{tag}")
        if out is not None:
            media = await extract_and_save_media(out, static, base_url)
            vids = [v for v in (media.get("videos") or []) if v.get("filename") or v.get("url")]
            if any(v.get("filename") for v in vids):
                return {
                    "ok": True,
                    "backend": f"web_video:{acc_id}",
                    "videos": vids,
                    "keyframe": keyframe_url,
                    "text": media.get("text") or "",
                    "attempts": attempts,
                    "pending_downloads": media.get("pending_downloads") or [],
                    "status": "ok",
                }
            if media.get("pending_downloads"):
                return {
                    "ok": True,
                    "backend": f"web_video_pending:{acc_id}",
                    "videos": vids,
                    "keyframe": keyframe_url,
                    "text": media.get("text") or "",
                    "attempts": attempts,
                    "pending_downloads": media.get("pending_downloads"),
                    "status": "pending_extension_download",
                }

    # 2) Guaranteed fallback: animate keyframe → mp4
    if keyframe and keyframe.exists():
        out_fn = f"anim_{secrets.token_hex(6)}.mp4"
        out_path = static / out_fn
        try:
            await asyncio.to_thread(animate_image_to_mp4, keyframe, out_path, 5.0, 24)
            attempts.append("ffmpeg_kenburns:ok")
            return {
                "ok": True,
                "backend": "ffmpeg_from_image",
                "videos": [
                    {
                        "type": "video",
                        "title": "[Animated from image]",
                        "url": f"{base_url}/static/{out_fn}",
                        "filename": out_fn,
                        "source_url": None,
                    }
                ],
                "keyframe": keyframe_url or f"{base_url}/static/{keyframe.name}",
                "text": "Video created by animating the generated keyframe image (reliable fallback).",
                "attempts": attempts,
                "pending_downloads": [],
                "status": "ok_fallback_animation",
                "message": (
                    "Gemini Web did not return a downloadable video URL. "
                    "Delivered a guaranteed MP4 by animating the keyframe image."
                ),
            }
        except Exception as e:
            attempts.append(f"ffmpeg_kenburns:error:{e}")

    return {
        "ok": False,
        "backend": None,
        "videos": [],
        "keyframe": keyframe_url,
        "attempts": attempts,
        "status": "failed",
        "error": (
            "Could not produce video. Ensure image generation works first "
            "(host network + Extension cookies, or official API key), "
            "and install ffmpeg or imageio-ffmpeg for animation fallback."
        ),
    }


def collect_web_clients(client_pool: dict, default_client, default_status: str, secure_1psid: str) -> List[Tuple[Any, str]]:
    out: List[Tuple[Any, str]] = []
    for aid, info in client_pool.items():
        if info.get("status") != "Active" or not info.get("client"):
            continue
        cl = info["client"]
        # skip official wrapper without web generate semantics
        if cl.__class__.__name__ == "GoogleAPIClient":
            continue
        out.append((cl, aid))
    if default_status == "Active" and default_client and secure_1psid:
        if default_client.__class__.__name__ != "GoogleAPIClient":
            out.append((default_client, "default"))
    return out


def collect_official_keys(client_pool: dict, accounts: list) -> List[str]:
    keys = []
    for aid, info in client_pool.items():
        cl = info.get("client")
        if cl is not None and hasattr(cl, "api_key") and getattr(cl, "api_key", None):
            keys.append(cl.api_key)
    for acc in accounts:
        if acc.get("provider_type") == "api_key" and acc.get("api_key"):
            if acc["api_key"] not in keys:
                keys.append(acc["api_key"])
    return keys
