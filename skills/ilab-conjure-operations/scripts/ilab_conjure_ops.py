#!/usr/bin/env python3
"""Safe CLI for operating an iLab CONJURE WebUI deployment."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import netrc as netrc_module
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

VERSION = "1.2.0"
DEFAULT_BASE_URL = os.environ.get("ILAB_CONJURE_URL", "http://127.0.0.1:8787")
DEFAULT_NETRC = os.environ.get("ILAB_CONJURE_NETRC", "~/.netrc")
DEFAULT_OUTPUT_DIR = os.environ.get("ILAB_CONJURE_OUTPUT_DIR", "./downloads")
TERMINAL_STATUSES = {"completed", "failed", "partial_failed", "cancelled", "canceled"}
SENSITIVE_KEYS = {
    "password",
    "passwd",
    "secret",
    "authorization",
    "proxy-authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "cookie",
    "set-cookie",
    "credentials",
}


class CLIError(RuntimeError):
    pass


class APIError(CLIError):
    def __init__(self, status: int | None, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower().replace("-", "_")
            if lowered in SENSITIVE_KEYS or any(
                lowered.endswith("_" + fragment)
                for fragment in ("password", "passwd", "secret", "authorization", "api_key", "access_token", "refresh_token")
            ):
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return value


def print_json(value: Any) -> None:
    print(json.dumps(redact(value), ensure_ascii=False, indent=2, sort_keys=True))


def concise(text: Any, limit: int = 120) -> str:
    normalized = " ".join(str(text or "").split())
    return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"


def safe_filename(value: str, fallback: str) -> str:
    name = Path(value).name.strip() or fallback
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return name or fallback


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for number in range(2, 1000):
        candidate = path.with_name(f"{stem}-v{number}{suffix}")
        if not candidate.exists():
            return candidate
    raise CLIError(f"Could not choose a unique output path beside {path}")


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bool_text(value: bool) -> str:
    return "true" if value else "false"


@dataclass
class ResponseData:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise APIError(self.status, "Server did not return valid JSON") from exc


class Client:
    def __init__(
        self,
        *,
        base_url: str,
        netrc_path: str | None,
        timeout: float,
        allow_http: bool = False,
        allow_insecure_netrc: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise CLIError("--base-url must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and not allow_http:
            if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise CLIError("Refusing Basic Auth over plain HTTP; use HTTPS or --allow-http explicitly")
        self.hostname = parsed.hostname
        self.timeout = timeout
        self.netrc_path = Path(netrc_path).expanduser() if netrc_path else None
        self.auth_header = (
            self._load_auth(allow_insecure_netrc=allow_insecure_netrc)
            if self.netrc_path is not None
            else ""
        )

    def _load_auth(self, *, allow_insecure_netrc: bool) -> str:
        if self.netrc_path is None:
            return ""
        try:
            stat = self.netrc_path.stat()
        except FileNotFoundError as exc:
            raise CLIError(f"Credential file not found: {self.netrc_path}") from exc
        if not self.netrc_path.is_file():
            raise CLIError(f"Credential path is not a file: {self.netrc_path}")
        mode = stat.st_mode & 0o777
        if mode & 0o077 and not allow_insecure_netrc:
            raise CLIError(
                f"Credential file permissions are too broad ({mode:o}); run chmod 600 {self.netrc_path}"
            )
        try:
            parsed = netrc_module.netrc(str(self.netrc_path))
            auth = parsed.authenticators(self.hostname)
        except (netrc_module.NetrcParseError, OSError) as exc:
            raise CLIError(f"Could not parse netrc credential file: {exc}") from exc
        if not auth:
            raise CLIError(f"No netrc entry found for {self.hostname}")
        login, _account, password = auth
        if not login or password is None:
            raise CLIError(f"Incomplete netrc entry for {self.hostname}")
        token = base64.b64encode(f"{login}:{password}".encode("utf-8")).decode("ascii")
        return "Basic " + token

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: Any = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> ResponseData:
        url = urljoin(self.base_url, path.lstrip("/"))
        if query:
            encoded = urlencode({key: value for key, value in query.items() if value is not None})
            if encoded:
                url += ("&" if "?" in url else "?") + encoded
        request_headers = {
            "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
            "User-Agent": f"ilab-conjure-ops-cli/{VERSION}",
        }
        if self.auth_header:
            request_headers["Authorization"] = self.auth_header
        payload = body
        if json_body is not None:
            payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        request = Request(url, data=payload, headers=request_headers, method=method.upper())
        try:
            with urlopen(request, timeout=timeout or self.timeout) as response:
                return ResponseData(
                    status=int(response.status),
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except HTTPError as exc:
            raw = exc.read()
            content_type = str(exc.headers.get("Content-Type") or "")
            parsed_body: Any
            if "json" in content_type:
                try:
                    parsed_body = redact(json.loads(raw.decode("utf-8", errors="replace")))
                except json.JSONDecodeError:
                    parsed_body = raw.decode("utf-8", errors="replace")[:2000]
            else:
                parsed_body = raw.decode("utf-8", errors="replace")[:2000]
            detail = parsed_body
            if isinstance(parsed_body, dict) and "detail" in parsed_body:
                detail = parsed_body["detail"]
            raise APIError(exc.code, f"HTTP {exc.code}: {concise(detail, 500)}", parsed_body) from exc
        except URLError as exc:
            raise APIError(None, f"Connection failed: {exc.reason}") from exc

    def get_json(self, path: str, *, query: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, query=query).json()

    def send_json(self, method: str, path: str, payload: Any = None) -> Any:
        return self.request(method, path, json_body={} if payload is None else payload).json()

    def multipart(
        self,
        path: str,
        *,
        fields: Iterable[tuple[str, str]],
        files: Iterable[tuple[str, Path]],
        timeout: float | None = None,
    ) -> Any:
        body, content_type = encode_multipart(list(fields), list(files))
        return self.request(
            "POST",
            path,
            body=body,
            headers={"Content-Type": content_type},
            timeout=timeout,
        ).json()


def encode_multipart(fields: list[tuple[str, str]], files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = "----ilab-conjure-" + uuid.uuid4().hex
    marker = boundary.encode("ascii")
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.extend(
            [
                b"--" + marker + b"\r\n",
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    for field_name, path in files:
        if not path.is_file():
            raise CLIError(f"Upload file not found: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = path.name.replace('"', "_")
        chunks.extend(
            [
                b"--" + marker + b"\r\n",
                (
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                    f"Content-Type: {mime}\r\n\r\n"
                ).encode("utf-8"),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(b"--" + marker + b"--\r\n")
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def task_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise CLIError("Unexpected task-list response")
    return [item for item in payload["tasks"] if isinstance(item, dict)]


def task_from_detail(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("task"), dict):
        raise CLIError("Unexpected task-detail response")
    return payload["task"]


def summarize_task(task: dict[str, Any]) -> str:
    task_id = task.get("task_id") or task.get("id") or "?"
    status = task.get("status") or "unknown"
    provider = task.get("api_provider_name") or task.get("api_provider_id") or "-"
    generated = task.get("generated_count")
    total = task.get("total_count") or (task.get("params") or {}).get("n")
    count = f"{generated}/{total}" if generated is not None and total is not None else str(total or "-")
    created = task.get("created_at") or "-"
    prompt = concise(task.get("prompt"), 88)
    return f"{task_id}  {status:14} provider={provider} outputs={count} created={created}  {prompt}"


def completed_outputs(task: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = task.get("outputs")
    if not isinstance(outputs, list):
        outputs = []
    records = []
    for position, item in enumerate(outputs, start=1):
        if not isinstance(item, dict):
            continue
        record = dict(item)
        record.setdefault("index", position)
        if record.get("status") in {None, "completed", "success", "succeeded"} and record.get("url"):
            records.append(record)
    if not records:
        urls = task.get("output_urls") if isinstance(task.get("output_urls"), list) else []
        formats = task.get("output_formats") if isinstance(task.get("output_formats"), list) else []
        for position, url in enumerate(urls, start=1):
            if url:
                records.append(
                    {
                        "index": position,
                        "url": url,
                        "format": formats[position - 1] if position <= len(formats) else None,
                    }
                )
    return records


def latest_task(client: Client, limit: int = 200) -> dict[str, Any]:
    tasks = task_list(client.get_json("/api/tasks/recent", query={"limit": limit}))
    if not tasks:
        raise CLIError("No tasks found")
    task_id = str(tasks[0].get("task_id") or "").strip()
    if not task_id:
        raise CLIError("Latest task has no task ID")
    return task_from_detail(client.get_json(f"/api/tasks/{task_id}"))


def download_response(
    client: Client,
    url_or_path: str,
    destination: Path,
    *,
    expected_image: bool,
) -> dict[str, Any]:
    response = client.request("GET", url_or_path)
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if expected_image and content_type and not content_type.startswith("image/"):
        raise CLIError(f"Expected an image but server returned {content_type}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = unique_path(destination)
    tmp = destination.with_name(destination.name + ".part")
    try:
        tmp.write_bytes(response.body)
        os.replace(tmp, destination)
    finally:
        if tmp.exists():
            tmp.unlink()
    return {
        "path": str(destination.resolve()),
        "bytes": destination.stat().st_size,
        "content_type": content_type or "unknown",
        "sha256": file_digest(destination),
    }


def download_task_outputs(
    client: Client,
    task: dict[str, Any],
    *,
    indexes: list[int] | None,
    output_dir: Path,
) -> list[dict[str, Any]]:
    task_id = str(task.get("task_id") or "unknown-task")
    records = completed_outputs(task)
    if not records:
        raise CLIError(f"Task {task_id} has no completed downloadable outputs")
    by_index = {int(item["index"]): item for item in records}
    selected = sorted(by_index) if indexes is None else indexes
    results = []
    for index in selected:
        record = by_index.get(index)
        if record is None:
            raise CLIError(f"Task {task_id} has no completed output index {index}")
        url = str(record.get("url") or "")
        parsed_name = Path(urlparse(url).path).name
        extension = str(record.get("format") or "").lower().replace("jpeg", "jpg")
        fallback = f"{task_id}-image-{index}" + (f".{extension}" if extension else ".img")
        filename = safe_filename(str(record.get("file") or parsed_name), fallback)
        if "." not in filename and extension:
            filename += "." + extension
        result = download_response(client, url, output_dir / filename, expected_image=True)
        result.update({"task_id": task_id, "output_index": index})
        results.append(result)
    return results


def prompt_value(args: argparse.Namespace) -> str:
    if getattr(args, "prompt", None) is not None:
        value = args.prompt
    else:
        path = Path(args.prompt_file).expanduser()
        if not path.is_file():
            raise CLIError(f"Prompt file not found: {path}")
        value = path.read_text(encoding="utf-8")
    value = value.strip()
    if not value:
        raise CLIError("Prompt cannot be empty")
    return value


def provider_records(client: Client) -> list[dict[str, Any]]:
    payload = client.get_json("/api/generation-catalog")
    providers = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(providers, list):
        raise CLIError("Unexpected provider-catalog response")
    return [item for item in providers if isinstance(item, dict)]


def ensure_provider(client: Client, provider_id: str, operation: str) -> dict[str, Any]:
    for provider in provider_records(client):
        if str(provider.get("id")) != provider_id:
            continue
        if not provider.get("available", False):
            raise CLIError(f"Provider {provider_id} is not currently available")
        bindings = provider.get("bindings") if isinstance(provider.get("bindings"), list) else []
        compatible = [
            binding
            for binding in bindings
            if isinstance(binding, dict)
            and binding.get("canonical_model_id") == "gpt-image-2"
            and operation in (binding.get("operations") or [])
        ]
        if not compatible:
            raise CLIError(f"Provider {provider_id} does not support {operation} for gpt-image-2")
        return provider
    raise CLIError(f"Unknown provider ID: {provider_id}")


def generation_fields(args: argparse.Namespace, prompt: str) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [
        ("prompt", prompt),
        ("ui_language", "zh-CN"),
        ("model", args.model),
        ("size", args.size),
        ("quality", args.quality),
        ("output_format", args.output_format),
        ("n", str(args.count)),
        ("api_provider_id", args.provider),
        ("prompt_fidelity", args.prompt_fidelity),
    ]
    for name in ("resolution", "ratio", "orientation", "background", "moderation", "output_compression"):
        value = getattr(args, name, None)
        if value is not None:
            fields.append((name, str(value)))
    if getattr(args, "input_fidelity", None) is not None:
        fields.append(("input_fidelity", args.input_fidelity))
    return fields


def safe_request_summary(args: argparse.Namespace, operation: str, prompt: str) -> dict[str, Any]:
    result = {
        "dry_run": True,
        "operation": operation,
        "provider": args.provider,
        "model": args.model,
        "size": args.size,
        "resolution": args.resolution,
        "ratio": args.ratio,
        "orientation": args.orientation,
        "quality": args.quality,
        "format": args.output_format,
        "count": args.count,
        "prompt_fidelity": args.prompt_fidelity,
        "prompt_chars": len(prompt),
        "prompt_preview": concise(prompt, 240),
    }
    if operation == "edit":
        result["images"] = [str(Path(item).expanduser()) for item in args.image]
        result["mask"] = str(Path(args.mask).expanduser()) if args.mask else None
    else:
        result["reference_images"] = [str(Path(item).expanduser()) for item in args.reference_image]
    return result


def wait_for_task(
    client: Client,
    task_id: str,
    *,
    poll_seconds: float,
    wait_timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + wait_timeout
    previous = None

    while True:
        task = task_from_detail(client.get_json(f"/api/tasks/{task_id}"))
        status = str(task.get("status") or "unknown")
        if status != previous:
            print(f"task={task_id} status={status}", file=sys.stderr)
            previous = status
        if status in TERMINAL_STATUSES:
            return task
        if time.monotonic() >= deadline:
            raise CLIError(f"Timed out waiting for task {task_id}; last status={status}")
        time.sleep(poll_seconds)


def output_task(task: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print_json(task)
        return
    print(summarize_task(task))
    outputs = completed_outputs(task)
    if outputs:
        print("completed_outputs=" + ",".join(str(item.get("index")) for item in outputs))


def command_health(client: Client, args: argparse.Namespace) -> int:
    payload = client.get_json("/api/health")
    if args.json:
        print_json(payload)
        return 0
    auth = payload.get("auth") if isinstance(payload, dict) and isinstance(payload.get("auth"), dict) else {}
    print(f"ok={bool_text(bool(payload.get('ok')))}")
    print(f"auth_available={bool_text(bool(payload.get('auth_available')))}")
    print(f"auth_source={auth.get('effective_source') or auth.get('selected_source') or '-'}")
    print(f"queue_worker_running={bool_text(bool(payload.get('queue_worker_running')))}")
    return 0


def command_providers(client: Client, args: argparse.Namespace) -> int:
    providers = provider_records(client)
    if args.json:
        print_json({"providers": providers})
        return 0
    for provider in providers:
        print(
            f"{provider.get('id')}  name={provider.get('name')} available={bool_text(bool(provider.get('available')))} "
            f"concurrency={provider.get('concurrency', '-')}"
        )
        for binding in provider.get("bindings") or []:
            if not isinstance(binding, dict):
                continue
            print(
                "  "
                + f"binding={binding.get('id')} canonical={binding.get('canonical_model_id')} "
                + f"remote={binding.get('remote_model_id')} protocol={binding.get('protocol_profile')} "
                + f"operations={','.join(binding.get('operations') or [])}"
            )
    return 0


def command_queue(client: Client, args: argparse.Namespace) -> int:
    payload = client.get_json("/api/queue")
    if args.json:
        print_json(payload)
        return 0
    waiting = payload.get("waiting") if isinstance(payload, dict) else []
    running = payload.get("running") if isinstance(payload, dict) else {}
    print(f"waiting={len(waiting) if isinstance(waiting, list) else 0}")
    print(f"running={len(running) if isinstance(running, (list, dict)) else 0}")
    return 0


def command_gallery(client: Client, args: argparse.Namespace) -> int:
    payload = client.get_json("/api/gallery")
    if args.json:
        print_json(payload)
        return 0
    items = payload.get("items") if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []
    categories = payload.get("categories") if isinstance(payload, dict) and isinstance(payload.get("categories"), list) else []
    print(f"items={len(items)} categories={len(categories)}")
    for item in items:
        if isinstance(item, dict):
            print(f"{item.get('id')} category={item.get('category_id', '-')} name={item.get('name', '-')} prompt={concise(item.get('prompt'), 80)}")
    return 0


def command_tasks(client: Client, args: argparse.Namespace) -> int:
    tasks = task_list(client.get_json("/api/tasks/recent", query={"limit": args.limit}))
    if args.status:
        tasks = [task for task in tasks if str(task.get("status")) in set(args.status)]
    if args.json:
        print_json({"tasks": tasks})
        return 0
    print(f"count={len(tasks)}")
    for task in tasks:
        print(summarize_task(task))
    return 0


def command_task(client: Client, args: argparse.Namespace) -> int:
    task = task_from_detail(client.get_json(f"/api/tasks/{args.task_id}"))
    output_task(task, as_json=args.json)
    return 0


def command_latest(client: Client, args: argparse.Namespace) -> int:
    output_task(latest_task(client), as_json=args.json)
    return 0


def command_download(client: Client, args: argparse.Namespace, *, latest: bool = False) -> int:
    if latest:
        task = latest_task(client)
    else:
        task = task_from_detail(client.get_json(f"/api/tasks/{args.task_id}"))
    indexes = None if args.all else [args.index]
    results = download_task_outputs(client, task, indexes=indexes, output_dir=Path(args.output_dir).expanduser())
    if args.json:
        print_json({"downloads": results})
    else:
        for item in results:
            print(
                f"task={item['task_id']} index={item['output_index']} path={item['path']} "
                f"content_type={item['content_type']} bytes={item['bytes']} sha256={item['sha256']}"
            )
    return 0


def command_download_zip(client: Client, args: argparse.Namespace) -> int:
    task_id = args.task_id
    output_dir = Path(args.output_dir).expanduser()
    response = client.request("GET", f"/api/tasks/{task_id}/outputs.zip", query={"selected": bool_text(args.selected)})
    content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if content_type not in {"application/zip", "application/octet-stream"}:
        raise CLIError(f"Expected ZIP but server returned {content_type or 'unknown'}")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_path(output_dir / f"{safe_filename(task_id, 'task')}-images.zip")
    destination.write_bytes(response.body)
    result = {
        "task_id": task_id,
        "path": str(destination.resolve()),
        "content_type": content_type,
        "bytes": destination.stat().st_size,
        "sha256": file_digest(destination),
    }
    if args.json:
        print_json(result)
    else:
        print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


def command_submit(client: Client, args: argparse.Namespace, *, operation: str) -> int:
    prompt = prompt_value(args)
    summary = safe_request_summary(args, operation, prompt)
    if args.dry_run:
        print_json(summary) if args.json else print_json(summary)
        return 0
    if not args.yes:
        raise CLIError(f"{operation} may consume paid API quota; rerun with --yes after explicit user approval")
    ensure_provider(client, args.provider, operation)
    fields = generation_fields(args, prompt)
    files: list[tuple[str, Path]] = []
    if operation == "generate":
        files.extend(("reference_images", Path(item).expanduser()) for item in args.reference_image)
        endpoint = "/api/generate"
    else:
        files.extend(("images", Path(item).expanduser()) for item in args.image)
        if args.mask:
            files.append(("mask", Path(args.mask).expanduser()))
        endpoint = "/api/edit"
    payload = client.multipart(endpoint, fields=fields, files=files, timeout=max(client.timeout, 120.0))
    task = task_from_detail(payload)
    task_id = str(task.get("task_id") or "")
    if not task_id:
        raise CLIError("Submitted job did not return a task ID")
    if args.wait:
        task = wait_for_task(
            client,
            task_id,
            poll_seconds=args.poll,
            wait_timeout=args.wait_timeout,
        )
    result: dict[str, Any] = {"task": task}
    if args.download:
        if not args.wait:
            raise CLIError("--download requires --wait")
        downloads = download_task_outputs(
            client,
            task,
            indexes=None,
            output_dir=Path(args.output_dir).expanduser(),
        )
        result["downloads"] = downloads
    if args.json:
        print_json(result)
    else:
        output_task(task, as_json=False)
        for item in result.get("downloads", []):
            print(
                f"download index={item['output_index']} path={item['path']} content_type={item['content_type']} "
                f"bytes={item['bytes']} sha256={item['sha256']}"
            )
    status = str(task.get("status") or "")
    return 0 if not args.wait or status == "completed" else 4


def _source_reference_ids(task: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    reference_assets = task.get("reference_assets") if isinstance(task.get("reference_assets"), list) else []
    gallery_refs = task.get("gallery_refs") if isinstance(task.get("gallery_refs"), list) else []
    reference_files = task.get("reference_files") if isinstance(task.get("reference_files"), list) else []

    def ids(items: list[Any]) -> list[str]:
        return list(
            dict.fromkeys(
                str(item.get("id") or "").strip()
                for item in items
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            )
        )

    return ids(reference_assets), ids(gallery_refs), ids(reference_files)


def clone_form_fields(task: dict[str, Any], prompt: str) -> tuple[str, list[tuple[str, str]], dict[str, Any]]:
    mode = str(task.get("mode") or "").strip()
    if mode not in {"generate", "edit"}:
        raise CLIError(f"Source task mode cannot be cloned: {mode or 'unknown'}")
    snapshot = task.get("generation_snapshot")
    if not isinstance(snapshot, dict):
        raise CLIError("Source task has no generation_snapshot; exact parameter cloning is unavailable")
    required_snapshot = ("canonical_model_id", "provider_id", "binding_id", "requested_parameters")
    missing = [key for key in required_snapshot if not snapshot.get(key)]
    if missing:
        raise CLIError("Source generation_snapshot is incomplete: " + ", ".join(missing))
    requested_parameters = snapshot.get("requested_parameters")
    if not isinstance(requested_parameters, dict):
        raise CLIError("Source requested_parameters is not a JSON object")
    params = task.get("params") if isinstance(task.get("params"), dict) else {}
    asset_ids, gallery_ids, file_ids = _source_reference_ids(task)
    if mode == "edit" and not asset_ids and not gallery_ids:
        input_files = task.get("input_files") if isinstance(task.get("input_files"), list) else []
        if input_files:
            raise CLIError(
                "Source edit uses task-local uploaded images; exact clone requires promoting or re-uploading those inputs"
            )
        raise CLIError("Source edit has no reusable reference asset or gallery image")
    fields: list[tuple[str, str]] = [
        ("prompt", prompt),
        ("ui_language", "zh-CN"),
        ("main_model", str(params.get("main_model") or "gpt-5.4-mini")),
        ("canonical_model_id", str(snapshot["canonical_model_id"])),
        ("provider_id", str(snapshot["provider_id"])),
        ("binding_id", str(snapshot["binding_id"])),
        ("parameters_json", json.dumps(requested_parameters, ensure_ascii=False, separators=(",", ":"), sort_keys=True)),
        ("prompt_fidelity", str(params.get("prompt_fidelity") or "strict")),
    ]
    fields.extend(("reference_asset_ids", item) for item in asset_ids)
    fields.extend(("gallery_image_ids", item) for item in gallery_ids)
    fields.extend(("reference_file_ids", item) for item in file_ids)
    summary = {
        "source_task_id": task.get("task_id"),
        "source_mode": mode,
        "canonical_model_id": snapshot["canonical_model_id"],
        "provider_id": snapshot["provider_id"],
        "provider_name": snapshot.get("provider_name"),
        "binding_id": snapshot["binding_id"],
        "protocol_profile": snapshot.get("protocol_profile"),
        "parameter_codec": snapshot.get("parameter_codec"),
        "remote_model_id": snapshot.get("remote_model_id"),
        "requested_parameters": requested_parameters,
        "main_model": params.get("main_model") or "gpt-5.4-mini",
        "prompt_fidelity": params.get("prompt_fidelity") or "strict",
        "reference_asset_ids": asset_ids,
        "gallery_image_ids": gallery_ids,
        "reference_file_ids": file_ids,
        "source_prompt_sha256": hashlib.sha256(str(task.get("prompt") or "").encode("utf-8")).hexdigest(),
        "new_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "source_prompt_chars": len(str(task.get("prompt") or "")),
        "new_prompt_chars": len(prompt),
        "prompt_preview": concise(prompt, 240),
    }
    return mode, fields, summary


def command_clone(client: Client, args: argparse.Namespace) -> int:
    source = task_from_detail(client.get_json(f"/api/tasks/{args.source_task_id}"))
    prompt = prompt_value(args)
    mode, fields, summary = clone_form_fields(source, prompt)
    upload_files: list[tuple[str, Path]] = []
    override_images = [Path(item).expanduser() for item in (args.image or [])]
    if override_images:
        for path in override_images:
            if not path.is_file():
                raise CLIError(f"Reference override image not found: {path}")
        source_asset_ids = list(summary.get("reference_asset_ids") or [])
        source_gallery_ids = list(summary.get("gallery_image_ids") or [])
        fields = [
            (key, value)
            for key, value in fields
            if key not in {"reference_asset_ids", "gallery_image_ids"}
        ]
        upload_field = "images" if mode == "edit" else "reference_images"
        upload_files = [(upload_field, path) for path in override_images]
        summary["source_reference_asset_ids"] = source_asset_ids
        summary["source_gallery_image_ids"] = source_gallery_ids
        summary["reference_asset_ids"] = []
        summary["gallery_image_ids"] = []
        summary["reference_override_files"] = [str(path.resolve()) for path in override_images]
        summary["reference_override_field"] = upload_field
    if args.dry_run:
        print_json({"dry_run": True, "clone": summary})
        return 0
    if not args.yes:
        raise CLIError("clone submits a paid generation request; rerun with --yes after explicit user approval")
    ensure_provider(client, str(summary["provider_id"]), mode)
    payload = client.multipart(
        "/api/edit" if mode == "edit" else "/api/generate",
        fields=fields,
        files=upload_files,
        timeout=max(client.timeout, 120.0),
    )
    task = task_from_detail(payload)
    task_id = str(task.get("task_id") or "")
    if not task_id:
        raise CLIError("Cloned job did not return a task ID")
    if args.wait:
        task = wait_for_task(client, task_id, poll_seconds=args.poll, wait_timeout=args.wait_timeout)
    result: dict[str, Any] = {"clone": summary, "task": task}
    if args.download:
        if not args.wait:
            raise CLIError("--download requires --wait")
        result["downloads"] = download_task_outputs(
            client,
            task,
            indexes=None,
            output_dir=Path(args.output_dir).expanduser(),
        )
    if args.json:
        print_json(result)
    else:
        print(f"cloned_from={args.source_task_id} mode={mode}")
        output_task(task, as_json=False)
        for item in result.get("downloads", []):
            print(
                f"download index={item['output_index']} path={item['path']} content_type={item['content_type']} "
                f"bytes={item['bytes']} sha256={item['sha256']}"
            )
    status = str(task.get("status") or "")
    return 0 if not args.wait or status == "completed" else 4


def require_yes(args: argparse.Namespace, action: str) -> None:
    if not args.yes:
        raise CLIError(f"{action} changes platform state; rerun with --yes after explicit user approval")


def command_cancel(client: Client, args: argparse.Namespace) -> int:
    require_yes(args, "cancel")
    payload = client.request("DELETE", f"/api/queue/{args.task_id}").json()
    print_json(payload) if args.json else print(f"cancelled task={args.task_id}")
    return 0


def command_retry(client: Client, args: argparse.Namespace) -> int:
    require_yes(args, "retry")
    payload = client.send_json("POST", f"/api/tasks/{args.task_id}/retry-failed", {})
    task = task_from_detail(payload)
    if args.wait:
        task = wait_for_task(client, args.task_id, poll_seconds=args.poll, wait_timeout=args.wait_timeout)
    output_task(task, as_json=args.json)
    status = str(task.get("status") or "")
    return 0 if not args.wait or status == "completed" else 4


def command_delete(client: Client, args: argparse.Namespace) -> int:
    require_yes(args, "delete")
    payload = client.request("DELETE", f"/api/tasks/{args.task_id}").json()
    print_json(payload) if args.json else print(f"deleted task={args.task_id}")
    return 0


def add_download_options(parser: argparse.ArgumentParser) -> None:
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--index", type=int, default=1, help="1-based output index (default: 1)")
    selection.add_argument("--all", action="store_true", help="Download all completed outputs")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)


def add_generation_options(parser: argparse.ArgumentParser, *, edit: bool) -> None:
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file")
    parser.add_argument("--provider", default="default", help="Provider ID returned by the providers command")
    parser.add_argument("--model", choices=["gpt-image-2"], default="gpt-image-2", help="Canonical image model")
    parser.add_argument(
        "--size",
        "--canvas",
        dest="size",
        default="auto",
        help="Canvas size; --canvas is a compatibility alias",
    )
    parser.add_argument("--resolution")
    parser.add_argument("--ratio")
    parser.add_argument("--orientation", choices=["square", "portrait", "landscape"])
    parser.add_argument("--quality", choices=["auto", "low", "medium", "high"], default="low")
    parser.add_argument("--background")
    parser.add_argument("--format", dest="output_format", choices=["png", "jpeg", "webp"], default="png")
    parser.add_argument("--moderation")
    parser.add_argument("--output-compression")
    parser.add_argument("--count", type=int, choices=range(1, 5), default=1)
    parser.add_argument("--prompt-fidelity", choices=["off", "original", "strict"], default="strict")
    parser.add_argument(
        "--strict",
        dest="prompt_fidelity",
        action="store_const",
        const="strict",
        help="Compatibility alias for --prompt-fidelity strict",
    )
    if edit:
        parser.add_argument("--image", action="append", required=True, help="Input image; repeat for multiple images")
        parser.add_argument("--mask")
        parser.add_argument("--input-fidelity")
    else:
        parser.add_argument(
            "--reference-image",
            "--ref",
            dest="reference_image",
            action="append",
            default=[],
            help="Reference image; repeat for multiple images; --ref is a compatibility alias",
        )
    parser.add_argument("--dry-run", action="store_true", help="Print a safe request summary without submitting")
    parser.add_argument("--yes", action="store_true", help="Confirm the paid operation")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--download", action="store_true", help="Download all outputs after --wait")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--poll", type=float, default=3.0)
    parser.add_argument("--wait-timeout", type=float, default=900.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--netrc", default=DEFAULT_NETRC, help="netrc file for HTTP Basic Auth")
    parser.add_argument("--no-auth", action="store_true", help="Do not send HTTP Basic Auth (useful for local deployments)")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--json", action="store_true", help="Print complete redacted JSON")
    parser.add_argument("--allow-http", action="store_true", help="Allow Basic Auth over plain HTTP")
    parser.add_argument("--allow-insecure-netrc", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")
    sub.add_parser("providers")
    sub.add_parser("queue")
    sub.add_parser("gallery")

    tasks = sub.add_parser("tasks")
    tasks.add_argument("--limit", type=int, choices=range(1, 501), default=20)
    tasks.add_argument("--status", action="append")

    task = sub.add_parser("task")
    task.add_argument("task_id")
    sub.add_parser("latest")

    download = sub.add_parser("download")
    download.add_argument("task_id")
    add_download_options(download)

    download_latest = sub.add_parser("download-latest")
    add_download_options(download_latest)

    download_zip = sub.add_parser("download-zip")
    download_zip.add_argument("task_id")
    download_zip.add_argument("--selected", action="store_true")
    download_zip.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)

    generate = sub.add_parser("generate")
    add_generation_options(generate, edit=False)

    edit = sub.add_parser("edit")
    add_generation_options(edit, edit=True)

    clone = sub.add_parser("clone", help="Clone a source task exactly and replace only its prompt")
    clone.add_argument("source_task_id")
    clone_prompts = clone.add_mutually_exclusive_group(required=True)
    clone_prompts.add_argument("--prompt")
    clone_prompts.add_argument("--prompt-file")
    clone.add_argument(
        "--image",
        action="append",
        help="Explicitly replace the source image references; repeat for multiple images",
    )
    clone.add_argument("--dry-run", action="store_true")
    clone.add_argument("--yes", action="store_true", help="Confirm the paid cloned request")
    clone.add_argument("--wait", action="store_true")
    clone.add_argument("--download", action="store_true")
    clone.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    clone.add_argument("--poll", type=float, default=3.0)
    clone.add_argument("--wait-timeout", type=float, default=900.0)

    cancel = sub.add_parser("cancel")

    cancel.add_argument("task_id")
    cancel.add_argument("--yes", action="store_true")

    retry = sub.add_parser("retry")
    retry.add_argument("task_id")
    retry.add_argument("--yes", action="store_true")
    retry.add_argument("--wait", action="store_true")
    retry.add_argument("--poll", type=float, default=3.0)
    retry.add_argument("--wait-timeout", type=float, default=900.0)

    delete = sub.add_parser("delete")
    delete.add_argument("task_id")
    delete.add_argument("--yes", action="store_true")
    return parser


def dispatch(client: Client, args: argparse.Namespace) -> int:
    commands = {
        "health": command_health,
        "providers": command_providers,
        "queue": command_queue,
        "gallery": command_gallery,
        "tasks": command_tasks,
        "task": command_task,
        "latest": command_latest,
        "download-zip": command_download_zip,
        "cancel": command_cancel,
        "retry": command_retry,
        "delete": command_delete,
    }
    if args.command == "download":
        return command_download(client, args, latest=False)
    if args.command == "download-latest":
        return command_download(client, args, latest=True)
    if args.command == "generate":
        return command_submit(client, args, operation="generate")
    if args.command == "edit":
        return command_submit(client, args, operation="edit")
    if args.command == "clone":
        return command_clone(client, args)
    return commands[args.command](client, args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        client = Client(
            base_url=args.base_url,
            netrc_path=None if args.no_auth else args.netrc,
            timeout=args.timeout,
            allow_http=args.allow_http,
            allow_insecure_netrc=args.allow_insecure_netrc,
        )
        return dispatch(client, args)
    except (CLIError, APIError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, APIError) and exc.body is not None and args.json:
            print_json({"status": exc.status, "error": exc.body})
        return 2
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
