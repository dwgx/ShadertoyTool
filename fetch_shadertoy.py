#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests

try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None

DEFAULT_SHADER_URL = "https://www.shadertoy.com/view/4djyRD"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
VIEW_URL = "https://www.shadertoy.com/view/{shader_id}"
API_URL = "https://www.shadertoy.com/api/v1/shaders/{shader_id}?key={api_key}"
POST_URL = "https://www.shadertoy.com/shadertoy"
_DEFAULT_REQUEST_ATTEMPTS = 3
_RETRIABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch GLSL source from a Shadertoy URL or shader ID."
    )
    parser.add_argument(
        "shader",
        nargs="?",
        default=DEFAULT_SHADER_URL,
        help="Shadertoy URL or shader ID (default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--out-dir",
        default="downloads",
        help="Directory for output .glsl files (default: %(default)s)",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Optional Shadertoy API key for /api/v1 route.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Disable browser fallback.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Use headless mode in browser fallback (default: visible browser).",
    )
    parser.add_argument(
        "--verify-attempts",
        type=int,
        default=5,
        help="Manual verification retry count for browser mode (default: %(default)s).",
    )
    parser.add_argument(
        "--skip-assets",
        action="store_true",
        help="Do not download linked assets (textures/videos/cubemaps).",
    )
    parser.add_argument(
        "--assets-dir",
        default="",
        help="Directory for downloaded assets (default: <out-dir>/<shader_id>_assets).",
    )
    return parser.parse_args()


def extract_shader_id(raw: str) -> str:
    raw = raw.strip()
    url_match = re.search(r"/view/([A-Za-z0-9]{6})", raw)
    if url_match:
        return url_match.group(1)
    id_match = re.fullmatch(r"[A-Za-z0-9]{6}", raw)
    if id_match:
        return raw
    raise ValueError(f"Invalid shader input: {raw}")


def build_cookie_jar() -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    if browser_cookie3 is None:
        return jar

    loaders = ["chrome", "edge", "brave", "firefox"]
    domains = [".shadertoy.com", "www.shadertoy.com"]
    for name in loaders:
        loader = getattr(browser_cookie3, name, None)
        if loader is None:
            continue
        for domain in domains:
            try:
                src = loader(domain_name=domain)
                for cookie in src:
                    jar.set_cookie(cookie)
            except Exception:
                # Keep trying other browsers/domains if one profile fails.
                continue
    return jar


def create_session(shader_id: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": VIEW_URL.format(shader_id=shader_id),
            "Origin": "https://www.shadertoy.com",
        }
    )
    cookies = build_cookie_jar()
    if cookies:
        session.cookies.update(cookies)
    return session


def legacy_post_data(shader_id: str) -> Dict[str, str]:
    return {
        "s": json.dumps({"shaders": [shader_id]}, separators=(",", ":")),
        "nt": "1",
        "nl": "1",
        "np": "1",
    }


def _request_with_retries(
    session: requests.Session,
    url: str,
    method: str = "GET",
    data: Optional[Dict[str, str]] = None,
    attempts: int = _DEFAULT_REQUEST_ATTEMPTS,
    **request_kwargs: Any,
) -> Optional[requests.Response]:
    total_attempts = max(1, int(attempts))
    for attempt in range(1, total_attempts + 1):
        try:
            if method == "GET":
                resp = session.get(url, **request_kwargs)
            else:
                resp = session.post(url, data=data, **request_kwargs)
        except requests.RequestException:
            if attempt < total_attempts:
                time.sleep(min(0.7 * attempt, 2.0))
                continue
            return None

        if resp.status_code in _RETRIABLE_STATUS_CODES and attempt < total_attempts:
            resp.close()
            time.sleep(min(0.7 * attempt, 2.0))
            continue
        return resp
    return None


def try_request_json(
    session: requests.Session,
    url: str,
    method: str = "GET",
    data: Optional[Dict[str, str]] = None,
) -> Optional[Any]:
    resp = _request_with_retries(
        session=session,
        url=url,
        method=method,
        data=data,
        attempts=_DEFAULT_REQUEST_ATTEMPTS,
        timeout=20,
    )
    if resp is None:
        return None

    try:
        if resp.status_code != 200:
            return None
        return resp.json()
    except json.JSONDecodeError:
        return None
    finally:
        resp.close()


def extract_shader_obj(payload: Any) -> Optional[Dict[str, Any]]:
    if isinstance(payload, dict):
        if payload.get("Error"):
            return None
        if isinstance(payload.get("Shader"), dict):
            return payload["Shader"]
        if isinstance(payload.get("shader"), dict):
            return payload["shader"]
        if isinstance(payload.get("Results"), list) and payload["Results"]:
            first = payload["Results"][0]
            if isinstance(first, dict):
                return first
        if isinstance(payload.get("result"), list) and payload["result"]:
            first = payload["result"][0]
            if isinstance(first, dict):
                return first
    elif isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first
    return None


def sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "pass"


def renderpass_code(shader: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    passes = shader.get("renderpass")
    if isinstance(passes, list):
        return [p for p in passes if isinstance(p, dict) and isinstance(p.get("code"), str)]
    return []


def write_glsl(shader_id: str, shader: Dict[str, Any], out_dir: Path) -> Path:
    info = shader.get("info", {}) if isinstance(shader.get("info"), dict) else {}
    name = info.get("name", "")
    passes = list(renderpass_code(shader))
    if not passes:
        raise RuntimeError("Shader payload does not include renderpass code.")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{shader_id}.glsl"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    parts = [
        f"// ShaderToy ID: {shader_id}",
        f"// Name: {name}" if name else "// Name: (unknown)",
        f"// Retrieved: {timestamp}",
        "",
    ]

    for idx, p in enumerate(passes):
        pass_name = sanitize_name(str(p.get("name") or f"pass_{idx:02d}"))
        pass_type = str(p.get("type", "")).strip()
        parts.append(f"// ===== Pass {idx}: {pass_name} ({pass_type or 'unknown'}) =====")
        parts.append(p["code"].rstrip())
        parts.append("")

    out_file.write_text("\n".join(parts), encoding="utf-8", newline="\n")
    return out_file


def normalize_asset_url(src: str) -> Optional[str]:
    src = src.strip()
    if not src:
        return None
    if src.startswith("http://") or src.startswith("https://"):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return "https://www.shadertoy.com" + src
    return "https://www.shadertoy.com/" + src.lstrip("/")


def guess_extension_from_ctype(ctype: str) -> str:
    mapped = {
        "texture": ".png",
        "cubemap": ".png",
        "volume": ".bin",
        "video": ".mp4",
        "music": ".mp3",
        "mic": ".wav",
    }
    return mapped.get(ctype.lower(), ".bin")


def build_asset_filename(url: str, ctype: str, pass_index: int, channel: Any) -> str:
    parsed = urlparse(url)
    basename = Path(unquote(parsed.path)).name
    if basename:
        cleaned = sanitize_name(basename)
        if cleaned:
            return cleaned
    chan = f"{channel}" if channel is not None else "x"
    return f"pass{pass_index:02d}_ch{chan}_{sanitize_name(ctype or 'asset')}{guess_extension_from_ctype(ctype)}"


def collect_asset_entries(shader: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries_by_url: Dict[str, Dict[str, Any]] = {}
    passes = shader.get("renderpass")
    if not isinstance(passes, list):
        return []

    for pass_index, pass_obj in enumerate(passes):
        if not isinstance(pass_obj, dict):
            continue
        pass_name = sanitize_name(str(pass_obj.get("name") or f"pass_{pass_index:02d}"))
        pass_type = str(pass_obj.get("type") or "")
        inputs = pass_obj.get("inputs")
        if not isinstance(inputs, list):
            continue
        for input_obj in inputs:
            if not isinstance(input_obj, dict):
                continue
            raw_src = input_obj.get("src") or input_obj.get("filepath") or ""
            if not isinstance(raw_src, str):
                continue
            url = normalize_asset_url(raw_src)
            if not url:
                continue
            ctype = str(input_obj.get("ctype") or "")
            channel = input_obj.get("channel")
            ref = {
                "pass_index": pass_index,
                "pass_name": pass_name,
                "pass_type": pass_type,
                "channel": channel,
                "ctype": ctype,
                "src": raw_src,
            }
            if url not in entries_by_url:
                entries_by_url[url] = {
                    "url": url,
                    "filename": build_asset_filename(url, ctype, pass_index, channel),
                    "refs": [ref],
                }
            else:
                entries_by_url[url]["refs"].append(ref)
    return list(entries_by_url.values())


def is_cloudflare_blocked_content(content_type: str, content: bytes) -> bool:
    ctype = (content_type or "").lower()
    if "html" not in ctype and "text/plain" not in ctype and "json" not in ctype:
        return False
    sample = content[:4096].decode("utf-8", errors="ignore").lower()
    markers = ["just a moment", "performing security verification", "cloudflare", "cf-ray", "bad request"]
    return any(marker in sample for marker in markers)


def is_cloudflare_blocked_response(resp: requests.Response) -> bool:
    if resp.status_code in (401, 403, 429, 503):
        return True
    ctype = resp.headers.get("Content-Type", "")
    return is_cloudflare_blocked_content(ctype, resp.content)


def choose_unique_filename(filename: str, used_lower: set) -> str:
    base = sanitize_name(filename) or "asset.bin"
    stem = Path(base).stem
    suffix = Path(base).suffix
    candidate = base
    idx = 2
    while candidate.lower() in used_lower:
        candidate = f"{stem}_{idx}{suffix}"
        idx += 1
    used_lower.add(candidate.lower())
    return candidate


def retry_assets_via_browser(
    retry_items: List[Dict[str, Any]],
    shader_id: str,
    headless: bool,
    verify_attempts: int,
) -> Dict[int, Dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return {}

    edge_exe = find_edge_executable()
    if edge_exe is None:
        return {}

    results: Dict[int, Dict[str, Any]] = {}
    pending_ids = {int(item["row_idx"]) for item in retry_items if "row_idx" in item}

    def attempt_once(page: Any) -> None:
        for item in retry_items:
            row_idx = int(item["row_idx"])
            if row_idx not in pending_ids:
                continue
            url = str(item["url"])
            target = Path(str(item["target"]))
            try:
                resp = page.context.request.get(url, timeout=40000)
                status = int(resp.status)
                body = resp.body() if status == 200 else b""
                content_type = resp.headers.get("content-type", "")
                if status == 200 and not is_cloudflare_blocked_content(content_type, body):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(body)
                    results[row_idx] = {"ok": True, "error": ""}
                    pending_ids.remove(row_idx)
                else:
                    results[row_idx] = {"ok": False, "error": f"HTTP {status}"}
            except Exception as exc:
                results[row_idx] = {"ok": False, "error": str(exc)}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=edge_exe,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = browser.new_page()
            page.goto(VIEW_URL.format(shader_id=shader_id), wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)
            attempt_once(page)

            if pending_ids and not headless:
                print("[INFO] Asset download fallback: waiting for browser verification...")
                for _ in range(max(1, verify_attempts)):
                    page.wait_for_timeout(5000)
                    attempt_once(page)
                    if not pending_ids:
                        break

            browser.close()
    except Exception:
        return results

    return results


def download_assets(
    session: requests.Session,
    shader: Dict[str, Any],
    shader_id: str,
    assets_dir: Path,
    headless: bool,
    verify_attempts: int,
) -> Tuple[int, int, Optional[Path]]:
    entries = collect_asset_entries(shader)
    if not entries:
        return 0, 0, None

    assets_dir.mkdir(parents=True, exist_ok=True)
    used_names: set = set()
    downloaded = 0
    failed = 0
    manifest_assets: List[Dict[str, Any]] = []
    retry_items: List[Dict[str, Any]] = []

    for entry in entries:
        filename = choose_unique_filename(str(entry.get("filename") or "asset.bin"), used_names)
        target = assets_dir / filename
        url = str(entry.get("url") or "")
        status = "ok"
        error = ""

        resp = _request_with_retries(
            session=session,
            url=url,
            method="GET",
            attempts=2,
            timeout=40,
            stream=True,
            headers={"Accept": "*/*"},
        )
        if resp is None:
            status = "failed"
            error = "Request failed after retries"
        else:
            try:
                if resp.status_code != 200:
                    status = "failed"
                    error = f"HTTP {resp.status_code}"
                elif is_cloudflare_blocked_response(resp):
                    status = "failed"
                    error = "Blocked by Cloudflare challenge"
                else:
                    with target.open("wb") as fp:
                        for chunk in resp.iter_content(chunk_size=65536):
                            if chunk:
                                fp.write(chunk)
                    if target.stat().st_size <= 0:
                        status = "failed"
                        error = "Empty response body"
                        target.unlink(missing_ok=True)
            finally:
                resp.close()

        if status == "ok":
            downloaded += 1
        else:
            failed += 1
            target.unlink(missing_ok=True)

        manifest_row: Dict[str, Any] = {
            "url": url,
            "saved_as": filename,
            "status": status,
            "refs": entry.get("refs", []),
        }
        if error:
            manifest_row["error"] = error
        manifest_assets.append(manifest_row)

        # Retry only challenge-like failures in a real browser context.
        if status == "failed" and ("HTTP 403" in error or "Cloudflare" in error):
            retry_items.append(
                {
                    "row_idx": len(manifest_assets) - 1,
                    "url": url,
                    "target": str(target),
                }
            )

    if retry_items:
        browser_results = retry_assets_via_browser(
            retry_items=retry_items,
            shader_id=shader_id,
            headless=headless,
            verify_attempts=verify_attempts,
        )
        for row_idx, result in browser_results.items():
            if not isinstance(result, dict):
                continue
            if bool(result.get("ok")):
                row = manifest_assets[row_idx]
                if row.get("status") == "failed":
                    failed = max(0, failed - 1)
                    downloaded += 1
                row["status"] = "ok"
                if "error" in row:
                    del row["error"]
            else:
                row = manifest_assets[row_idx]
                err = str(result.get("error") or "")
                if err:
                    row["error"] = err

    manifest = {
        "shader_id": shader_id,
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "asset_total": len(entries),
        "asset_downloaded": downloaded,
        "asset_failed": failed,
        "assets": manifest_assets,
    }
    manifest_path = assets_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8", newline="\n")
    return downloaded, failed, manifest_path


def update_session_from_browser_cookies(session: requests.Session, cookies: List[Dict[str, Any]]) -> None:
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        path = cookie.get("path") or "/"
        if not name or value is None:
            continue
        if isinstance(domain, str) and domain:
            session.cookies.set(name, value, domain=domain, path=path)
        else:
            session.cookies.set(name, value, path=path)


def find_edge_executable() -> Optional[str]:
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def fetch_legacy_via_browser(
    shader_id: str,
    headless: bool,
    verify_attempts: int,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except Exception:
        print("[ERROR] Browser fallback needs playwright. Install: python -m pip install playwright")
        return None, []

    edge_exe = find_edge_executable()
    if edge_exe is None:
        print("[ERROR] Microsoft Edge executable not found for browser fallback.")
        return None, []

    fetch_js = """
    async (shaderId) => {
      const params = new URLSearchParams();
      params.set("s", JSON.stringify({shaders:[shaderId]}));
      params.set("nt", "1");
      params.set("nl", "1");
      params.set("np", "1");
      const resp = await fetch("https://www.shadertoy.com/shadertoy", {
        method: "POST",
        headers: {"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},
        credentials: "include",
        body: params.toString()
      });
      const text = await resp.text();
      return {status: resp.status, text: text};
    }
    """

    def attempt_fetch(page: Any) -> Optional[Dict[str, Any]]:
        try:
            result = page.evaluate(fetch_js, shader_id)
        except PlaywrightError:
            return None
        if not isinstance(result, dict):
            return None
        if int(result.get("status", 0)) != 200:
            return None
        try:
            return json.loads(str(result.get("text", "")))
        except json.JSONDecodeError:
            return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                executable_path=edge_exe,
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = browser.new_page()
            page.goto(VIEW_URL.format(shader_id=shader_id), wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(3000)

            payload = attempt_fetch(page)
            if payload is not None:
                cookies = page.context.cookies(["https://www.shadertoy.com"])
                browser.close()
                return payload, cookies

            if headless:
                browser.close()
                return None, []

            if not sys.stdin.isatty():
                print("[ERROR] Browser verification is needed, but current console is non-interactive.")
                browser.close()
                return None, []

            print("[INFO] Browser fallback is active.")
            print("[ACTION] Complete verification/login in the opened Edge window, then return here.")
            for idx in range(max(1, verify_attempts)):
                input(f"[ACTION] Press Enter to retry fetch ({idx + 1}/{max(1, verify_attempts)}): ")
                payload = attempt_fetch(page)
                if payload is not None:
                    cookies = page.context.cookies(["https://www.shadertoy.com"])
                    browser.close()
                    return payload, cookies

            browser.close()
            return None, []
    except Exception:
        return None, []


def main() -> int:
    args = parse_args()
    try:
        shader_id = extract_shader_id(args.shader)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 2

    session = create_session(shader_id)
    shader: Optional[Dict[str, Any]] = None

    if args.api_key.strip():
        payload = try_request_json(
            session,
            API_URL.format(shader_id=shader_id, api_key=args.api_key.strip()),
            method="GET",
        )
        shader = extract_shader_obj(payload)

    if shader is None:
        payload = try_request_json(
            session,
            POST_URL,
            method="POST",
            data=legacy_post_data(shader_id),
        )
        shader = extract_shader_obj(payload)

    if shader is None and not args.no_browser:
        payload, browser_cookies = fetch_legacy_via_browser(
            shader_id=shader_id,
            headless=args.headless,
            verify_attempts=args.verify_attempts,
        )
        if browser_cookies:
            update_session_from_browser_cookies(session, browser_cookies)
        shader = extract_shader_obj(payload)

    if shader is None:
        print("[ERROR] Failed to fetch shader source.")
        print("[HINT] Cloudflare currently blocks non-browser requests on many machines.")
        print("[HINT] Re-run and complete verification in the auto-opened Edge window.")
        print("[HINT] You can also provide your own API key: --api-key <YOUR_KEY>")
        return 1

    try:
        out_file = write_glsl(shader_id, shader, Path(args.out_dir))
    except Exception as exc:
        print(f"[ERROR] Failed to write output: {exc}")
        return 1

    print(f"[OK] Saved GLSL: {out_file}")

    if not args.skip_assets:
        assets_dir = Path(args.assets_dir) if args.assets_dir.strip() else (Path(args.out_dir) / f"{shader_id}_assets")
        downloaded, failed, manifest_path = download_assets(
            session=session,
            shader=shader,
            shader_id=shader_id,
            assets_dir=assets_dir,
            headless=args.headless,
            verify_attempts=args.verify_attempts,
        )
        if manifest_path is None:
            print("[INFO] No linked assets found in shader inputs.")
        else:
            print(f"[OK] Assets downloaded: {downloaded}, failed: {failed}")
            print(f"[OK] Asset manifest: {manifest_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
