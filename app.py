from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import gradio as gr
import requests
from gradio_client import Client, handle_file


APP_NAME = "SVCVC-API"
APP_VERSION = "1.1.0"
PIPELINE_VERSION = "soulx-svcvc-v3"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SVCVC_CONFIG", BASE_DIR / "config.json"))
VOICE_DIR = BASE_DIR / "voice_profiles"
CACHE_DIR = BASE_DIR / "cache"
DOWNLOAD_DIR = BASE_DIR / "downloads"
OUTPUT_DIR = BASE_DIR / "outputs"
SOULX_DOWNLOAD_DIR = OUTPUT_DIR / "_soulx_downloads"
SOULX_ROOT = BASE_DIR.parent / "SoulX-Singer"
SOULX_MODEL_PATH = SOULX_ROOT / "pretrained_models" / "SoulX-Singer" / "model-svc.pt"
FFMPEG_PATH = SOULX_ROOT / "ffmpeg" / "bin" / "ffmpeg.exe"
SOULX_FINGERPRINT_PATHS = (
    SOULX_ROOT / "soulxsinger" / "config" / "soulxsinger.yaml",
    SOULX_MODEL_PATH,
    SOULX_ROOT / "pretrained_models" / "SoulX-Singer" / "config.yaml",
    SOULX_ROOT / "pretrained_models" / "openai-whisper-base",
    SOULX_ROOT / "pretrained_models" / "SoulX-Singer-Preprocess",
)
SUPPORTED_AUDIO_EXTENSIONS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac"}
SOULX_ENDPOINT_CANDIDATES = (
    "/soulx_svc_convert_path",
    "/soulx_svc_convert",
    "/_start_svc",
)
GPU_JOB_LOCK = threading.Lock()
DOWNLOAD_CACHE_LOCK = threading.Lock()
SOULX_FINGERPRINT_LOCK = threading.Lock()
_SOULX_FINGERPRINT_CACHE: dict[str, Any] = {"checked_at": 0.0, "value": None}
MAX_PROFILE_ID_LENGTH = 80
WINDOWS_DANGEROUS_CHARS = set('<>:"/\\|?*')
MAX_HTTP_REDIRECTS = 8


DEFAULT_CONFIG: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 6666,
        "queue_max_size": 8,
    },
    "soulx": {
        "base_url": "http://127.0.0.1:7861",
        "request_timeout_seconds": 9000,
        "asset_fingerprint_refresh_seconds": 5,
    },
    "download": {
        "timeout_seconds": 60,
        "max_size_mb": 500,
        "max_files": 100,
    },
    "cache": {
        "enabled": True,
        "random_seed_persistent": False,
        "max_files": 100,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        return DEFAULT_CONFIG
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取配置文件 {CONFIG_PATH}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("config.json 顶层必须是 JSON 对象")
    return _deep_merge(DEFAULT_CONFIG, data)


CONFIG = _load_config()
for directory in (VOICE_DIR, CACHE_DIR, DOWNLOAD_DIR, OUTPUT_DIR, SOULX_DOWNLOAD_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_voice_file(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(VOICE_DIR.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return resolved.parent == VOICE_DIR.resolve()


def _validate_profile_id(value: Any) -> str:
    profile_id = str(value or "").strip()
    if not profile_id:
        raise ValueError("profile_id 不能为空")
    if len(profile_id) > MAX_PROFILE_ID_LENGTH:
        raise ValueError(f"profile_id 不能超过 {MAX_PROFILE_ID_LENGTH} 个字符")
    if "|||" in profile_id:
        raise ValueError("profile_id 不能包含模型别名分隔符 |||")
    if profile_id in {".", ".."} or profile_id.endswith((" ", ".")):
        raise ValueError("profile_id 不能是相对路径标记，也不能以空格或点结尾")
    if any(ch in WINDOWS_DANGEROUS_CHARS for ch in profile_id):
        raise ValueError("profile_id 包含 Windows 文件名危险字符")
    if any(unicodedata.category(ch) == "Cc" for ch in profile_id):
        raise ValueError("profile_id 不能包含控制字符")
    return profile_id


def _read_profile_metadata(audio_path: Path) -> dict[str, Any]:
    metadata_path = audio_path.with_suffix(".json")
    if not metadata_path.is_file() or metadata_path.is_symlink():
        return {}
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] 忽略无效音色元数据 {metadata_path.name}: {exc}", flush=True)
        return {}


def _scan_voice_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for audio_path in sorted(VOICE_DIR.iterdir(), key=lambda item: item.name.casefold()):
        if audio_path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
            continue
        if not _safe_voice_file(audio_path):
            print(f"[WARN] 跳过不安全的音色文件: {audio_path}", flush=True)
            continue
        metadata = _read_profile_metadata(audio_path)
        try:
            profile_id = _validate_profile_id(metadata.get("profile_id") or audio_path.stem)
        except ValueError as exc:
            print(f"[WARN] 跳过 ID 不安全的音色文件 {audio_path.name}: {exc}", flush=True)
            continue
        if profile_id in used_ids:
            print(f"[WARN] 音色 ID 重复，跳过 {audio_path.name}: {profile_id}", flush=True)
            continue
        used_ids.add(profile_id)
        profiles.append(
            {
                "profile_id": profile_id,
                "display_name": str(metadata.get("display_name") or audio_path.stem),
                "description": str(metadata.get("description") or ""),
                "audio_file": audio_path.name,
                "audio_path": str(audio_path.resolve()),
                "audio_sha256": _sha256_file(audio_path),
                "prompt_vocal_sep": bool(metadata.get("prompt_vocal_sep", False)),
            }
        )
    return profiles


def show_model() -> list[str]:
    """返回兼容 Matsuko Cover 模型刷新逻辑的音色 ID 列表。"""
    models = [profile["profile_id"] for profile in _scan_voice_profiles()]
    print(f"[音色列表Debug] 返回 {len(models)} 个 SVCVC 音色: {', '.join(models) or '(空)'}", flush=True)
    return models


def list_voice_profiles() -> list[dict[str, Any]]:
    """返回音色库的详细信息。"""
    profiles = _scan_voice_profiles()
    print(f"[音色详情Debug] 返回 {len(profiles)} 个音色配置", flush=True)
    return profiles


def _normalize_endpoint_name(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return "/" + name.strip().lstrip("/")


def _discover_soulx_endpoints(base_url: str, timeout: float = 3.0) -> list[str]:
    response = requests.get(f"{base_url.rstrip('/')}/config", timeout=timeout)
    response.raise_for_status()
    config = response.json()
    found: set[str] = set()
    for dependency in config.get("dependencies", []):
        endpoint = _normalize_endpoint_name(dependency.get("api_name"))
        if endpoint in SOULX_ENDPOINT_CANDIDATES:
            found.add(endpoint)
    # Prefer the local-path endpoint even if Gradio lists the visible Audio
    # endpoint first.  Both services run on this machine, so transferring a
    # large WAV through HTTP is unnecessary and less reliable.
    return [endpoint for endpoint in SOULX_ENDPOINT_CANDIDATES if endpoint in found]


def health() -> dict[str, Any]:
    profiles = _scan_voice_profiles()
    base_url = str(CONFIG["soulx"]["base_url"]).rstrip("/")
    online = False
    endpoints: list[str] = []
    error = ""
    try:
        endpoints = _discover_soulx_endpoints(base_url)
        online = True
    except Exception as exc:  # health 必须返回结构化结果，不能让检查接口本身失败
        error = str(exc)
    compatible = online and bool(endpoints)
    return {
        "status": "ok" if compatible else "degraded",
        "service": APP_NAME,
        "version": APP_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "voice_profile_count": len(profiles),
        "soulx": {
            "base_url": base_url,
            "online": online,
            "compatible_api": compatible,
            "available_endpoints": endpoints,
            "preferred_endpoint": endpoints[0] if endpoints else None,
            "error": error,
        },
    }


def _extract_netease_id(source: str) -> str | None:
    value = source.strip()
    if re.fullmatch(r"\d{1,20}", value):
        return value
    parsed = urlparse(value)
    if parsed.hostname and (parsed.hostname.endswith("music.163.com") or parsed.hostname.endswith("163cn.tv")):
        query_id = parse_qs(parsed.query).get("id", [None])[0]
        if query_id and re.fullmatch(r"\d{1,20}", query_id):
            return query_id
        match = re.search(r"(?:song/|id=)(\d{1,20})", value)
        if match:
            return match.group(1)
    return None


def _download_max_bytes() -> int:
    return max(1, int(float(CONFIG["download"]["max_size_mb"]) * 1024 * 1024))


def _touch_and_trim_downloads(current_path: Path) -> None:
    """Update LRU time and bound the managed source-audio cache."""
    try:
        current = current_path.resolve(strict=True)
        download_root = DOWNLOAD_DIR.resolve(strict=True)
        current.relative_to(download_root)
    except (OSError, ValueError):
        return
    if current.parent != download_root or current.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        return

    max_files = max(1, int(CONFIG["download"].get("max_files", 100)))
    with DOWNLOAD_CACHE_LOCK:
        try:
            os.utime(current, None)
        except OSError as exc:
            print(f"[WARN] 无法更新下载缓存使用时间 {current.name}: {exc}", flush=True)
        files: list[Path] = []
        for path in DOWNLOAD_DIR.iterdir():
            try:
                if (
                    path.is_file()
                    and not path.is_symlink()
                    and path.suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS
                ):
                    files.append(path.resolve(strict=True))
            except OSError:
                continue
        other_files = sorted(
            (path for path in files if path != current),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in other_files[max_files - 1 :]:
            try:
                stale.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[WARN] 无法清理下载缓存 {stale.name}: {exc}", flush=True)


def _validate_target_file(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if resolved.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        raise ValueError(
            "目标文件必须是受支持的音频格式: "
            + ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        )
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError("目标音频为空文件")
    max_bytes = _download_max_bytes()
    if size > max_bytes:
        raise ValueError(f"目标音频超过 {CONFIG['download']['max_size_mb']} MB 限制")
    return resolved


def _validate_public_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许使用 HTTP/HTTPS 音频 URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("远程音频 URL 不能包含用户名或密码")
    hostname = parsed.hostname.rstrip(".")
    if hostname.casefold() == "localhost" or hostname.casefold().endswith(".localhost"):
        raise ValueError("拒绝访问本机地址")
    try:
        literal_addresses = [ipaddress.ip_address(hostname.split("%", 1)[0])]
    except ValueError:
        try:
            records = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise ValueError(f"无法解析远程音频主机 {hostname}: {exc}") from exc
        literal_addresses = []
        for record in records:
            address = record[4][0].split("%", 1)[0]
            ip = ipaddress.ip_address(address)
            if ip not in literal_addresses:
                literal_addresses.append(ip)
    if not literal_addresses:
        raise ValueError(f"远程音频主机 {hostname} 没有可用地址")
    blocked = [str(ip) for ip in literal_addresses if not ip.is_global]
    if blocked:
        raise ValueError(f"拒绝访问非公网地址: {', '.join(blocked)}")


def _looks_like_audio_payload(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
    except OSError:
        return False
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return True
    if header.startswith((b"fLaC", b"OggS", b"ID3")):
        return True
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return True
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return True
    try:
        import soundfile as sf

        info = sf.info(str(path))
        return bool(info.frames > 0 and info.samplerate > 0)
    except Exception:
        return False


def _validate_downloaded_audio(path: Path, content_type: str = "") -> None:
    _validate_target_file(path)
    mime = content_type.split(";", 1)[0].strip().casefold()
    plausible_mime = (
        not mime
        or mime.startswith("audio/")
        or mime in {"application/octet-stream", "application/ogg", "video/mp4"}
    )
    if not plausible_mime:
        raise ValueError(f"远程服务器返回的内容类型不是音频: {content_type}")
    if not _looks_like_audio_payload(path):
        raise ValueError("远程服务器返回的内容无法识别为有效音频")


def _emit_status(
    callback: Callable[[float, str], None] | None,
    value: float,
    description: str,
) -> None:
    print(f"[下载Debug] {description}", flush=True)
    if callback is None:
        return
    try:
        callback(max(0.0, min(1.0, float(value))), description)
    except Exception:
        pass


def _download_stream(
    url: str,
    destination: Path,
    status_callback: Callable[[float, str], None] | None = None,
    source_label: str = "远程音频",
) -> Path:
    timeout = float(CONFIG["download"]["timeout_seconds"])
    max_bytes = _download_max_bytes()
    temporary = destination.with_suffix(destination.suffix + ".part")
    written = 0
    response: requests.Response | None = None
    session: requests.Session | None = None
    try:
        _emit_status(status_callback, 0.02, f"正在连接{source_label}下载源")
        current_url = url
        session = requests.Session()
        for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
            _validate_public_http_url(current_url)
            response = session.get(
                current_url,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
                timeout=timeout,
                allow_redirects=False,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    response.close()
                    raise ValueError("远程服务器返回了没有 Location 的重定向")
                next_url = urljoin(str(response.url), location)
                _validate_public_http_url(next_url)
                response.close()
                response = None
                if redirect_count >= MAX_HTTP_REDIRECTS:
                    raise ValueError("远程音频重定向次数过多")
                current_url = next_url
                continue
            break
        if response is None:
            raise ValueError("未能取得远程音频响应")
        _validate_public_http_url(str(response.url))
        with response:
            response.raise_for_status()
            length = int(response.headers.get("Content-Length") or 0)
            if length > max_bytes:
                raise ValueError(f"远程音频超过 {CONFIG['download']['max_size_mb']} MB 限制")
            with temporary.open("wb") as output:
                for block in response.iter_content(1024 * 1024):
                    if not block:
                        continue
                    written += len(block)
                    if written > max_bytes:
                        raise ValueError(f"远程音频超过 {CONFIG['download']['max_size_mb']} MB 限制")
                    output.write(block)
                    downloaded_mb = written / (1024 * 1024)
                    if length > 0:
                        ratio = min(1.0, written / length)
                        total_mb = length / (1024 * 1024)
                        _emit_status(
                            status_callback,
                            0.08 + ratio * 0.84,
                            f"正在下载{source_label}：{downloaded_mb:.1f}/{total_mb:.1f} MiB（{ratio * 100:.0f}%）",
                        )
                    else:
                        _emit_status(
                            status_callback,
                            min(0.90, 0.08 + written / max_bytes * 0.82),
                            f"正在下载{source_label}：已接收 {downloaded_mb:.1f} MiB",
                        )
        if written == 0:
            raise ValueError("远程服务器返回了空音频")
        temporary.replace(destination)
        try:
            _validate_downloaded_audio(destination, response.headers.get("Content-Type", ""))
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        _touch_and_trim_downloads(destination)
        _emit_status(status_callback, 1.0, f"{source_label}下载完成：{destination.name}")
        return destination
    finally:
        if response is not None:
            response.close()
        if session is not None:
            session.close()
        temporary.unlink(missing_ok=True)


def _get_public_json(url: str, timeout: float) -> dict[str, Any]:
    current_url = url
    with requests.Session() as session:
        for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
            _validate_public_http_url(current_url)
            with session.get(current_url, timeout=timeout, allow_redirects=False) as response:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("JSON 服务返回了没有 Location 的重定向")
                    next_url = urljoin(str(response.url), location)
                    _validate_public_http_url(next_url)
                    if redirect_count >= MAX_HTTP_REDIRECTS:
                        raise ValueError("JSON 服务重定向次数过多")
                    current_url = next_url
                    continue
                _validate_public_http_url(str(response.url))
                response.raise_for_status()
                if len(response.content) > 2 * 1024 * 1024:
                    raise ValueError("JSON 服务响应过大")
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("JSON 服务未返回对象")
                return data
    raise ValueError("未能取得 JSON 服务响应")


def _download_netease(
    song_id: str,
    status_callback: Callable[[float, str], None] | None = None,
) -> Path:
    destination = DOWNLOAD_DIR / f"netease_{song_id}.mp3"
    if destination.is_file() and destination.stat().st_size > 0:
        try:
            _validate_downloaded_audio(destination)
            _touch_and_trim_downloads(destination)
            _emit_status(status_callback, 1.0, f"网易云歌曲下载缓存命中：ID {song_id}")
            return destination
        except ValueError:
            destination.unlink(missing_ok=True)
    primary = f"https://biliplayer.91vrchat.com/player/?url=https://music.163.com/song?id={song_id}"
    _emit_status(status_callback, 0.0, f"正在下载网易云歌曲：ID {song_id}")
    try:
        return _download_stream(primary, destination, status_callback, "网易云歌曲")
    except Exception as primary_error:
        print(f"[WARN] 网易云主下载源失败: {primary_error}", flush=True)
        _emit_status(status_callback, 0.10, "网易云主下载源失败，正在切换备用源")
    timeout = float(CONFIG["download"]["timeout_seconds"])
    metadata = _get_public_json(
        f"https://api.vkeys.cn/v2/music/netease?id={song_id}", timeout=timeout
    )
    media_url = metadata.get("data", {}).get("url")
    if not media_url:
        raise ValueError("网易云备用下载源没有返回音频地址")
    return _download_stream(str(media_url), destination, status_callback, "网易云歌曲（备用源）")


def _download_direct(
    url: str,
    status_callback: Callable[[float, str], None] | None = None,
) -> Path:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in SUPPORTED_AUDIO_EXTENSIONS:
        suffix = ".mp3"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    destination = DOWNLOAD_DIR / f"url_{key}{suffix}"
    if destination.is_file() and destination.stat().st_size > 0:
        try:
            _validate_downloaded_audio(destination)
            _touch_and_trim_downloads(destination)
            _emit_status(status_callback, 1.0, f"远程音频下载缓存命中：{destination.name}")
            return destination
        except ValueError:
            destination.unlink(missing_ok=True)
    return _download_stream(url, destination, status_callback, "远程/QQ音乐音频")


def _resolve_target_audio(
    source: str,
    status_callback: Callable[[float, str], None] | None = None,
) -> Path:
    if not source or not source.strip():
        raise ValueError("song_name_src 不能为空")
    source = source.strip().strip('"')
    local = Path(source).expanduser()
    if local.is_file():
        if local.is_symlink():
            raise ValueError("不接受符号链接目标音频")
        resolved = _validate_target_file(local)
        _emit_status(
            status_callback,
            1.0,
            f"已收到本地音频：{resolved.name}（QQ音乐由 AstrBot 插件预下载）",
        )
        return resolved
    netease_id = _extract_netease_id(source)
    if netease_id:
        return _download_netease(netease_id, status_callback)
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return _download_direct(source, status_callback)
    raise FileNotFoundError(f"找不到目标音频，也无法识别为 HTTP/网易云来源: {source}")


def _validate_parameters(
    pitch_shift: int,
    n_step: int,
    cfg: float,
    seed: int,
) -> tuple[int, int, float, int]:
    pitch_shift = int(pitch_shift)
    n_step = int(n_step)
    cfg = float(cfg)
    seed = int(seed)
    if not -36 <= pitch_shift <= 36:
        raise ValueError("pitch_shift 必须在 -36 到 36 之间")
    if not 1 <= n_step <= 200:
        raise ValueError("n_step 必须在 1 到 200 之间")
    if not 0.0 <= cfg <= 10.0:
        raise ValueError("cfg 必须在 0 到 10 之间")
    if seed != -1 and not 0 <= seed <= 10000:
        raise ValueError("seed 必须是 -1（随机）或 0 到 10000")
    return pitch_shift, n_step, cfg, seed


def _find_profile(profile_id: str) -> dict[str, Any]:
    profile_id = _validate_profile_id(profile_id)
    for profile in _scan_voice_profiles():
        if profile["profile_id"] == profile_id:
            return profile
    raise ValueError(f"找不到参考音色: {profile_id}；可用音色: {', '.join(show_model()) or '(空)'}")


def _build_soulx_asset_manifest() -> list[tuple[str, int | None, int | None]]:
    """Build a metadata-only manifest without reading multi-gigabyte model bodies."""
    manifest: list[tuple[str, int | None, int | None]] = []
    for asset_root in SOULX_FINGERPRINT_PATHS:
        try:
            candidates = [asset_root] if asset_root.is_file() else asset_root.rglob("*")
            found = False
            for path in candidates:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                relative = path.relative_to(SOULX_ROOT).as_posix()
                manifest.append((relative, stat.st_size, stat.st_mtime_ns))
                found = True
            if not found:
                relative = asset_root.relative_to(SOULX_ROOT).as_posix()
                manifest.append((f"!missing:{relative}", None, None))
        except (OSError, ValueError) as exc:
            try:
                relative = asset_root.relative_to(SOULX_ROOT).as_posix()
            except ValueError:
                relative = str(asset_root)
            manifest.append((f"!error:{relative}:{type(exc).__name__}", None, None))
    return sorted(set(manifest), key=lambda item: item[0])


def _soulx_asset_fingerprint(force_refresh: bool = False) -> dict[str, Any]:
    """Return a short cached digest and periodically notice changed SoulX assets."""
    refresh_seconds = max(
        0.0,
        float(CONFIG["soulx"].get("asset_fingerprint_refresh_seconds", 5)),
    )
    now = time.monotonic()
    with SOULX_FINGERPRINT_LOCK:
        cached = _SOULX_FINGERPRINT_CACHE.get("value")
        checked_at = float(_SOULX_FINGERPRINT_CACHE.get("checked_at") or 0.0)
        if (
            not force_refresh
            and isinstance(cached, dict)
            and now - checked_at < refresh_seconds
        ):
            return dict(cached)

        manifest = _build_soulx_asset_manifest()
        serialized = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
        value = {
            "digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "file_count": sum(1 for item in manifest if not item[0].startswith("!")),
            "total_size": sum(item[1] or 0 for item in manifest),
        }
        _SOULX_FINGERPRINT_CACHE["checked_at"] = now
        _SOULX_FINGERPRINT_CACHE["value"] = value
        return dict(value)


def _cache_key(target_path: Path, profile: dict[str, Any], parameters: dict[str, Any]) -> str:
    payload = {
        "pipeline_version": PIPELINE_VERSION,
        "soulx_assets": _soulx_asset_fingerprint(),
        "target_sha256": _sha256_file(target_path),
        "prompt_sha256": profile["audio_sha256"],
        **parameters,
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _extract_result_path(result: Any) -> Path | None:
    if isinstance(result, (str, os.PathLike)):
        path = Path(result)
        return path if path.is_file() else None
    if isinstance(result, dict):
        for key in ("path", "name", "value"):
            candidate = _extract_result_path(result.get(key))
            if candidate:
                return candidate
    if isinstance(result, (list, tuple)):
        for item in result:
            candidate = _extract_result_path(item)
            if candidate:
                return candidate
    return None


def _looks_like_missing_endpoint(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "cannot find a function",
            "could not find api",
            "api_name",
            "not a valid api",
            "function not found",
        )
    )


def _call_soulx(
    prompt_path: Path,
    target_path: Path,
    prompt_vocal_sep: bool,
    target_vocal_sep: bool,
    auto_shift: bool,
    auto_mix_acc: bool,
    pitch_shift: int,
    n_step: int,
    cfg: float,
    seed: int,
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[Path, str, Path]:
    base_url = str(CONFIG["soulx"]["base_url"]).rstrip("/")
    timeout = float(CONFIG["soulx"]["request_timeout_seconds"])
    try:
        discovered = _discover_soulx_endpoints(base_url, timeout=5.0)
    except Exception:
        discovered = []
    endpoints = discovered or list(SOULX_ENDPOINT_CANDIDATES)
    call_download_dir = Path(tempfile.mkdtemp(prefix="call_", dir=SOULX_DOWNLOAD_DIR))
    try:
        client = Client(
            base_url,
            verbose=False,
            analytics_enabled=False,
            # Gradio's upload, queue event stream, and result download can use
            # separate executor tasks.  A single worker deadlocks long SVC jobs
            # after the upstream inference has already finished.
            max_workers=4,
            httpx_kwargs={"timeout": timeout},
            download_files=call_download_dir,
        )
        arguments = (
            handle_file(str(prompt_path)),
            handle_file(str(target_path)),
            bool(prompt_vocal_sep),
            bool(target_vocal_sep),
            bool(auto_shift),
            bool(auto_mix_acc),
            int(pitch_shift),
            int(n_step),
            float(cfg),
            int(seed),
        )
    except Exception:
        shutil.rmtree(call_download_dir, ignore_errors=True)
        raise
    last_error: Exception | None = None
    try:
        for index, endpoint in enumerate(endpoints):
            try:
                print(f"[SoulX Debug] 调用 {base_url}{endpoint}", flush=True)
                job = client.submit(*arguments, api_name=endpoint)
                deadline = time.monotonic() + timeout
                last_progress: tuple[float | None, str] | None = None
                while not job.done():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        try:
                            job.cancel()
                        except Exception:
                            pass
                        raise TimeoutError(f"SoulX 推理超过 {timeout:g} 秒")
                    try:
                        status = job.status()
                        progress_data = getattr(status, "progress_data", None) or []
                        if progress_data:
                            item = progress_data[-1]
                            description = getattr(item, "desc", None)
                            if description is None and isinstance(item, dict):
                                description = item.get("desc")
                            fraction = getattr(item, "progress", None)
                            if fraction is None and isinstance(item, dict):
                                fraction = item.get("progress")
                            if fraction is None:
                                current = getattr(item, "index", None)
                                total = getattr(item, "length", None)
                                if isinstance(item, dict):
                                    current = item.get("index", current)
                                    total = item.get("length", total)
                                if current is not None and total:
                                    fraction = float(current) / float(total)
                            if description:
                                normalized = (
                                    max(0.0, min(1.0, float(fraction)))
                                    if fraction is not None
                                    else 0.0
                                )
                                marker = (round(normalized, 4), str(description))
                                if marker != last_progress:
                                    print(
                                        f"[SoulX进度Debug] {normalized * 100:.1f}% {description}",
                                        flush=True,
                                    )
                                    if progress_callback is not None:
                                        progress_callback(normalized, str(description))
                                    last_progress = marker
                    except Exception as progress_error:
                        print(f"[WARN] 读取 SoulX 进度失败: {progress_error}", flush=True)
                    time.sleep(min(0.5, max(0.05, remaining)))

                remaining = max(0.1, deadline - time.monotonic())
                result = job.result(timeout=remaining)
                result_path = _extract_result_path(result)
                if not result_path:
                    raise RuntimeError(f"SoulX {endpoint} 返回了空路径或不存在的文件: {result!r}")
                return result_path.resolve(), endpoint, call_download_dir
            except Exception as exc:
                last_error = exc
                if discovered or index == len(endpoints) - 1 or not _looks_like_missing_endpoint(exc):
                    break
                print(f"[WARN] SoulX 不支持 {endpoint}，尝试兼容端点", flush=True)
        raise RuntimeError(f"SoulX-SVC 调用失败: {last_error}") from last_error
    except Exception:
        shutil.rmtree(call_download_dir, ignore_errors=True)
        raise
    finally:
        try:
            client.close()
        except Exception:
            pass


def _export_mp3(source: Path, destination: Path) -> Path:
    """Export a QQ-friendly high-quality MP3 atomically."""
    if not FFMPEG_PATH.is_file():
        raise FileNotFoundError(f"找不到 FFmpeg: {FFMPEG_PATH}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp3")
    temporary.unlink(missing_ok=True)
    command = [
        str(FFMPEG_PATH),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vn",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "320k",
        str(temporary),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
        raise RuntimeError(f"导出 MP3 失败: {detail}")
    temporary.replace(destination)
    return destination


def _cleanup_soulx_download_dir(path: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
        root = SOULX_DOWNLOAD_DIR.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        print(f"[WARN] 拒绝清理不属于 SoulX 临时目录的路径: {path}", flush=True)
        return
    if resolved.parent != root or not resolved.name.startswith("call_"):
        print(f"[WARN] 拒绝清理非任务级 SoulX 临时目录: {path}", flush=True)
        return
    shutil.rmtree(resolved, ignore_errors=True)


def _trim_cache() -> None:
    max_files = max(1, int(CONFIG["cache"]["max_files"]))
    files = sorted(
        (
            path
            for path in CACHE_DIR.iterdir()
            if path.is_file() and path.suffix.lower() in {".mp3", ".wav"}
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in files[max_files:]:
        stale.unlink(missing_ok=True)


def _trim_outputs() -> None:
    """Limit non-cached random outputs so random mode cannot grow forever."""
    max_files = max(1, int(CONFIG["cache"]["max_files"]))
    files = sorted(
        (
            path
            for path in OUTPUT_DIR.iterdir()
            if path.is_file()
            and path.name.startswith("svcvc_")
            and path.suffix.lower() in {".mp3", ".wav"}
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in files[max_files:]:
        stale.unlink(missing_ok=True)


def convert(
    song_name_src: str,
    model_dropdown: str,
    prompt_vocal_sep: bool = False,
    target_vocal_sep: bool = True,
    auto_shift: bool = True,
    auto_mix_acc: bool = True,
    pitch_shift: int = 0,
    n_step: int = 32,
    cfg: float = 1.0,
    seed: int = 42,
    random_seed: bool = False,
    progress: gr.Progress = gr.Progress(),
) -> tuple[str, str]:
    """将目标歌曲交给 SoulX-Singer-SVC 转换，返回 (输出 MP3, 是否命中缓存)。"""
    started = time.monotonic()
    try:
        progress(0.02, desc="正在检查 SVCVC 参数与参考音色...")
        pitch_shift, n_step, cfg, seed = _validate_parameters(pitch_shift, n_step, cfg, seed)
        requested_random = bool(random_seed) or seed == -1
        actual_seed = random.SystemRandom().randint(0, 10000) if seed == -1 else seed
        profile = _find_profile(str(model_dropdown).strip())
        def source_progress(value: float, description: str) -> None:
            progress(0.02 + 0.06 * value, desc=description)

        target_path = _resolve_target_audio(song_name_src, source_progress)
        prompt_path = Path(profile["audio_path"])
        parameters = {
            "profile_id": profile["profile_id"],
            "prompt_vocal_sep": bool(prompt_vocal_sep),
            "target_vocal_sep": bool(target_vocal_sep),
            "auto_shift": bool(auto_shift),
            "auto_mix_acc": bool(auto_mix_acc),
            "pitch_shift": pitch_shift,
            "n_step": n_step,
            "cfg": cfg,
            "seed": actual_seed,
        }
        key = _cache_key(target_path, profile, parameters)
        cache_path = CACHE_DIR / f"{key}.mp3"
        persistent_cache = bool(CONFIG["cache"]["enabled"]) and (
            not requested_random or bool(CONFIG["cache"]["random_seed_persistent"])
        )
        print(
            "[SVCVC任务Debug] "
            f"音色={profile['profile_id']} target={target_path.name} "
            f"prompt_sep={bool(prompt_vocal_sep)} target_sep={bool(target_vocal_sep)} "
            f"auto_shift={bool(auto_shift)} auto_mix={bool(auto_mix_acc)} "
            f"pitch={pitch_shift} steps={n_step} cfg={cfg} "
            f"seed={actual_seed} random={requested_random} cache={persistent_cache}",
            flush=True,
        )
        if persistent_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
            print(f"[缓存命中] {cache_path.name} | 实际种子={actual_seed}", flush=True)
            progress(1.0, desc=f"缓存命中 / Cache hit，实际种子 {actual_seed}")
            return str(cache_path.resolve()), "true"

        progress(0.08, desc=f"等待 SoulX GPU 队列，实际种子 {actual_seed}...")
        with GPU_JOB_LOCK:
            if persistent_cache and cache_path.is_file() and cache_path.stat().st_size > 0:
                print(f"[缓存命中] GPU 队列复查命中 {cache_path.name}", flush=True)
                progress(1.0, desc=f"缓存命中 / Cache hit，实际种子 {actual_seed}")
                return str(cache_path.resolve()), "true"
            progress(0.12, desc="SoulX 正在做人声分离、F0 提取与音色转换...")
            call_download_dir: Path | None = None
            try:
                def upstream_progress(value: float, description: str) -> None:
                    progress(0.12 + 0.80 * value, desc=description)

                result_path, endpoint, call_download_dir = _call_soulx(
                    prompt_path,
                    target_path,
                    bool(prompt_vocal_sep),
                    bool(target_vocal_sep),
                    bool(auto_shift),
                    bool(auto_mix_acc),
                    pitch_shift,
                    n_step,
                    cfg,
                    actual_seed,
                    upstream_progress,
                )
                progress(0.95, desc="正在导出 QQ 兼容的 320 kbps MP3...")
                if persistent_cache:
                    final_path = _export_mp3(result_path, cache_path)
                    _trim_cache()
                else:
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    final_path = OUTPUT_DIR / f"svcvc_{stamp}_{actual_seed}_{key[:10]}.mp3"
                    _export_mp3(result_path, final_path)
                    _trim_outputs()
            finally:
                if call_download_dir is not None:
                    _cleanup_soulx_download_dir(call_download_dir)

        elapsed = time.monotonic() - started
        print(
            f"[SVCVC完成Debug] endpoint={endpoint} seed={actual_seed} "
            f"耗时={elapsed:.1f}s 输出={final_path}",
            flush=True,
        )
        progress(1.0, desc=f"转换完成，实际种子 {actual_seed}")
        return str(final_path.resolve()), "false"
    except gr.Error:
        raise
    except Exception as exc:
        print(f"[SVCVC错误] {type(exc).__name__}: {exc}", flush=True)
        raise gr.Error(str(exc)) from exc


def build_app() -> gr.Blocks:
    profiles = _scan_voice_profiles()
    choices = [item["profile_id"] for item in profiles]
    default_profile = choices[0] if choices else None
    with gr.Blocks(title="SVCVC-API · SoulX-Singer Gateway") as app:
        gr.Markdown(
            "# SVCVC-API\n"
            "SoulX-Singer-SVC 轻量中间层。参考音色位于 `voice_profiles/`，GPU 推理由 `127.0.0.1:7861` 完成。"
        )
        with gr.Row():
            song_name_src = gr.Textbox(label="目标音频路径 / HTTP URL / 网易云歌曲 ID")
            model_dropdown = gr.Dropdown(choices=choices, value=default_profile, label="参考音色")
        with gr.Row():
            prompt_vocal_sep = gr.Checkbox(False, label="Prompt 人声分离")
            target_vocal_sep = gr.Checkbox(True, label="Target 人声分离")
            auto_shift = gr.Checkbox(True, label="自动变调")
            auto_mix_acc = gr.Checkbox(True, label="自动混合伴奏")
        with gr.Row():
            pitch_shift = gr.Slider(-36, 36, value=0, step=1, label="指定变调（半音）")
            n_step = gr.Slider(1, 200, value=32, step=1, label="采样步数")
            cfg = gr.Slider(0, 10, value=1.0, step=0.1, label="CFG 系数")
            seed = gr.Slider(-1, 10000, value=42, step=1, label="种子（-1 为随机）")
        random_seed = gr.Checkbox(False, label="随机任务（不读取/写入持久缓存）")
        run_button = gr.Button("开始 SVC Voice Conversion", variant="primary")
        output_audio = gr.Audio(label="转换结果", type="filepath")
        cache_hit = gr.Textbox(label="Cache Hit", interactive=False)

        run_button.click(
            fn=convert,
            inputs=[
                song_name_src,
                model_dropdown,
                prompt_vocal_sep,
                target_vocal_sep,
                auto_shift,
                auto_mix_acc,
                pitch_shift,
                n_step,
                cfg,
                seed,
                random_seed,
            ],
            outputs=[output_audio, cache_hit],
            api_name="convert",
            concurrency_limit=1,
        )

        api_models = gr.JSON(visible=False)
        api_profiles = gr.JSON(visible=False)
        api_health = gr.JSON(visible=False)
        app.load(fn=show_model, inputs=[], outputs=api_models, api_name="show_model")
        app.load(fn=list_voice_profiles, inputs=[], outputs=api_profiles, api_name="list_voice_profiles")
        app.load(fn=health, inputs=[], outputs=api_health, api_name="health")
    return app


APP = build_app()


if __name__ == "__main__":
    host = str(CONFIG["server"]["host"])
    port = int(CONFIG["server"]["port"])
    queue_max_size = int(CONFIG["server"]["queue_max_size"])
    print(f"[INFO] {APP_NAME} {APP_VERSION} -> http://{host}:{port}", flush=True)
    print(f"[INFO] SoulX upstream: {CONFIG['soulx']['base_url']}", flush=True)
    print(f"[INFO] 已加载 {len(_scan_voice_profiles())} 个参考音色", flush=True)
    APP.queue(api_open=True, max_size=queue_max_size, default_concurrency_limit=1).launch(
        server_name=host,
        server_port=port,
        share=False,
        show_error=True,
        allowed_paths=[str(CACHE_DIR), str(OUTPUT_DIR)],
    )
