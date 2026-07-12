from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from PIL import Image
except ModuleNotFoundError:
    print("Pillow is not installed. Installing it now...")
    install_commands = [
        [sys.executable, "-m", "pip", "install", "Pillow"],
        [sys.executable, "-m", "pip", "install", "--user", "Pillow"],
    ]

    install_error: Exception | None = None

    for command in install_commands:
        try:
            subprocess.check_call(command)
            install_error = None
            break
        except (subprocess.CalledProcessError, OSError) as error:
            install_error = error

    if install_error is not None:
        raise SystemExit(
            "Pillow could not be installed automatically. Run: "
            f'"{sys.executable}" -m pip install Pillow'
        ) from install_error

    from PIL import Image


# -----------------------------
# Configuration
# -----------------------------


API_KEY = "a"

# Existing Leonardo uploaded-image ID for the static character reference.
# The script reuses this ID and does not upload character.png again.
LOGO_IMAGE_ID = "310dbaea-a950-46e3-9ce6-611840cb6028"

DB_PATH = Path("db.json")
RESULTS_PATH = Path("final_urls.json")
LOG_PATH = Path("leonardo_generation.log")

CREATE_URL = "https://cloud.leonardo.ai/api/rest/v2/generations"
RETRIEVE_BASE_URL = "https://cloud.leonardo.ai/api/rest/v1/generations"
INIT_IMAGE_URL = "https://cloud.leonardo.ai/api/rest/v1/init-image"

MODEL = "nano-banana-2"
WIDTH = 768
HEIGHT = 1376
IMAGE_REFERENCE_STRENGTH = "MID"

POLL_INTERVAL_SECONDS = 5
GENERATION_TIMEOUT_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 90
MAX_REQUEST_ATTEMPTS = 3
ITERATION_DELAY_SECONDS = 10
JPEG_QUALITY = 95

# The inclusive scenario range is supplied when launching the script.
# Example: python gen_cli_iterations.py 20 40




def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate Leonardo images for an inclusive range of scenario IDs "
            "from db.json."
        )
    )
    parser.add_argument(
        "first_iteration",
        help="First scenario ID to process, inclusive.",
    )
    parser.add_argument(
        "last_iteration",
        help="Last scenario ID to process, inclusive.",
    )
    return parser.parse_args()


# -----------------------------
# Logging helpers
# -----------------------------


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def clear_log() -> None:
    """Start every run with a fresh log file."""
    LOG_PATH.write_text("", encoding="utf-8")


def append_log(entry: dict[str, Any]) -> None:
    # The file is cleared once at startup, then entries are written for this run.
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def response_body(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def log_response(
    operation: str,
    response: requests.Response,
    request_payload: dict[str, Any] | None = None,
) -> Any:
    body = response_body(response)

    entry: dict[str, Any] = {
        "timestamp": utc_timestamp(),
        "operation": operation,
        "method": response.request.method if response.request else None,
        "url": response.url,
        "http_status": response.status_code,
        "response_body": body,
    }

    if request_payload is not None:
        entry["request_payload"] = request_payload

    append_log(entry)

    print(f"\n[{operation}] HTTP {response.status_code}")
    if isinstance(body, str):
        print(body or "<empty response body>")
    else:
        print(json.dumps(body, indent=2, ensure_ascii=False))

    return body


def _console_safe(text: str) -> str:
    """Return text that can be printed by the active Windows console encoding."""
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def log_binary_response(
    operation: str,
    response: requests.Response,
    request_metadata: dict[str, Any] | None = None,
) -> None:
    """Log downloads/uploads without trying to decode binary image bytes as text."""
    content_type = response.headers.get("content-type", "").lower()
    content_length = len(response.content)
    is_text_response = (
        content_type.startswith("text/")
        or "json" in content_type
        or "xml" in content_type
        or "javascript" in content_type
    )

    if is_text_response:
        encoding = response.encoding or "utf-8"
        decoded_body = response.content.decode(encoding, errors="replace")
        logged_body: Any = decoded_body or "<empty response body>"
    else:
        decoded_body = None
        logged_body = {
            "binary_body_omitted": True,
            "content_type": content_type or "unknown",
            "content_length_bytes": content_length,
        }

    entry: dict[str, Any] = {
        "timestamp": utc_timestamp(),
        "operation": operation,
        "method": response.request.method if response.request else None,
        "url": response.url,
        "http_status": response.status_code,
        "response_body": logged_body,
        "response_headers": dict(response.headers),
    }

    if request_metadata is not None:
        entry["request_metadata"] = request_metadata

    append_log(entry)

    print(f"\n[{operation}] HTTP {response.status_code}")

    if decoded_body is not None:
        print(_console_safe(decoded_body or "<empty response body>"))
    else:
        print(
            json.dumps(
                logged_body,
                indent=2,
                ensure_ascii=True,
            )
        )


# -----------------------------
# Leonardo response parsing
# -----------------------------


def find_generation_id(data: Any) -> str | None:
    if isinstance(data, list):
        for item in data:
            generation_id = find_generation_id(item)
            if generation_id:
                return generation_id
        return None

    if not isinstance(data, dict):
        return None

    for key in ("generationId", "generation_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value

    for value in data.values():
        generation_id = find_generation_id(value)
        if generation_id:
            return generation_id

    return None


def find_cost(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        for item in data:
            cost = find_cost(item)
            if cost:
                return cost
        return None

    if not isinstance(data, dict):
        return None

    cost = data.get("cost")
    if isinstance(cost, dict):
        return cost

    for value in data.values():
        nested_cost = find_cost(value)
        if nested_cost:
            return nested_cost

    return None


def extract_generation(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        for item in data:
            generation = extract_generation(item)
            if generation:
                return generation
        return None

    if not isinstance(data, dict):
        return None

    for key in ("generations_by_pk", "generation", "generate"):
        value = data.get(key)
        if isinstance(value, dict):
            if "status" in value or "generated_images" in value:
                return value

            nested = extract_generation(value)
            if nested:
                return nested

    if "status" in data or "generated_images" in data:
        return data

    for value in data.values():
        nested = extract_generation(value)
        if nested:
            return nested

    return None


def extract_upload_init_image(data: Any) -> dict[str, Any] | None:
    if isinstance(data, list):
        for item in data:
            result = extract_upload_init_image(item)
            if result:
                return result
        return None

    if not isinstance(data, dict):
        return None

    direct = data.get("uploadInitImage")
    if isinstance(direct, dict):
        return direct

    if all(key in data for key in ("id", "fields", "url")):
        return data

    for value in data.values():
        result = extract_upload_init_image(value)
        if result:
            return result

    return None


def decimal_cost(cost: dict[str, Any] | None) -> Decimal:
    if not cost:
        return Decimal("0")

    try:
        return Decimal(str(cost.get("amount", "0")))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


# -----------------------------
# HTTP helpers
# -----------------------------


def headers() -> dict[str, str]:
    api_key = API_KEY.strip()

    if not api_key or api_key == "YOUR_API_KEY":
        raise ValueError("Set API_KEY at the top of the script before running it.")

    return {
        "accept": "application/json",
        "authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }


def request_with_retries(
    method: str,
    url: str,
    operation: str,
    payload: dict[str, Any] | None = None,
) -> tuple[requests.Response, Any]:
    last_error: Exception | None = None

    for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers(),
                json=payload,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            body = log_response(operation, response, payload)

            if response.ok:
                return response, body

            if response.status_code not in {429, 500, 502, 503, 504}:
                raise RuntimeError(
                    f"{operation} failed with HTTP {response.status_code}. "
                    f"See {LOG_PATH} for the complete response body."
                )

            last_error = RuntimeError(
                f"{operation} returned HTTP {response.status_code}."
            )

        except requests.RequestException as error:
            last_error = error
            append_log(
                {
                    "timestamp": utc_timestamp(),
                    "operation": operation,
                    "attempt": attempt,
                    "network_error": str(error),
                }
            )

        if attempt < MAX_REQUEST_ATTEMPTS:
            wait_seconds = attempt * 5
            print(
                f"{operation} failed on attempt {attempt}. "
                f"Retrying in {wait_seconds} seconds..."
            )
            time.sleep(wait_seconds)

    raise RuntimeError(
        f"{operation} failed after {MAX_REQUEST_ATTEMPTS} attempts: {last_error}"
    )


# -----------------------------
# External reference upload
# -----------------------------


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def infer_extension(source: str, content_type: str | None = None) -> str:
    suffix = Path(urlparse(source).path).suffix.lower().lstrip(".")

    if suffix in {"jpg", "jpeg", "png", "webp"}:
        return suffix

    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            guessed = guessed.lower().lstrip(".")
            if guessed == "jpe":
                guessed = "jpg"
            if guessed in {"jpg", "jpeg", "png", "webp"}:
                return guessed

    raise ValueError(
        "Could not determine the reference image extension. "
        "Use a .jpg, .jpeg, .png, or .webp file or URL."
    )


def load_external_image(source: str) -> tuple[bytes, str, str, str]:
    if is_http_url(source):
        response = requests.get(source, timeout=REQUEST_TIMEOUT_SECONDS)
        log_binary_response(
            operation="external-reference:download",
            response=response,
            request_metadata={"source": source},
        )
        response.raise_for_status()

        extension = infer_extension(source, response.headers.get("content-type"))
        filename = Path(urlparse(source).path).name or f"reference.{extension}"
        mime_type = response.headers.get("content-type") or mimetypes.guess_type(filename)[0]
        return response.content, extension, filename, mime_type or "application/octet-stream"

    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(
            f"External reference image was not found: {path.resolve()}"
        )

    extension = infer_extension(str(path))
    filename = path.name
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    image_bytes = path.read_bytes()

    append_log(
        {
            "timestamp": utc_timestamp(),
            "operation": "external-reference:read-local-file",
            "path": str(path.resolve()),
            "size_bytes": len(image_bytes),
            "extension": extension,
            "mime_type": mime_type,
        }
    )

    print(
        f"Loaded external reference image {path} "
        f"({len(image_bytes)} bytes)."
    )

    return image_bytes, extension, filename, mime_type


def upload_external_reference_image(source: str) -> str:
    image_bytes, extension, filename, mime_type = load_external_image(source)

    _, init_body = request_with_retries(
        method="POST",
        url=INIT_IMAGE_URL,
        operation="external-reference:create-upload-slot",
        payload={"extension": extension},
    )

    upload_info = extract_upload_init_image(init_body)
    if not upload_info:
        raise RuntimeError(
            "Leonardo returned no uploadInitImage object. "
            f"See {LOG_PATH} for the response body."
        )

    uploaded_image_id = upload_info.get("id")
    upload_url = upload_info.get("url")
    raw_fields = upload_info.get("fields")

    if not uploaded_image_id or not upload_url or raw_fields is None:
        raise RuntimeError(
            "Leonardo's init-image response is missing id, url, or fields. "
            f"See {LOG_PATH} for the response body."
        )

    if isinstance(raw_fields, str):
        fields = json.loads(raw_fields)
    elif isinstance(raw_fields, dict):
        fields = raw_fields
    else:
        raise RuntimeError("Leonardo returned upload fields in an unsupported format.")

    # Do not send Leonardo authorization headers to the presigned S3 URL.
    upload_response = requests.post(
        str(upload_url),
        data=fields,
        files={"file": (filename, image_bytes, mime_type)},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    log_binary_response(
        operation="external-reference:upload-file",
        response=upload_response,
        request_metadata={
            "filename": filename,
            "extension": extension,
            "mime_type": mime_type,
            "size_bytes": len(image_bytes),
            "uploaded_image_id": uploaded_image_id,
        },
    )

    if not upload_response.ok:
        raise RuntimeError(
            f"External reference upload failed with HTTP {upload_response.status_code}. "
            f"See {LOG_PATH} for the response body."
        )

    print(f"External reference uploaded. Leonardo image ID: {uploaded_image_id}")
    return str(uploaded_image_id)


# -----------------------------
# Generation functions
# -----------------------------


def create_generation(
    prompt: str,
    operation: str,
    reference_image_id: str | None = None,
    reference_image_type: str = "GENERATED",
) -> tuple[str, dict[str, Any] | None]:
    cleaned_prompt = prompt.strip()

    if not cleaned_prompt:
        raise ValueError(f"Prompt for {operation} is empty.")

    if reference_image_type not in {"GENERATED", "UPLOADED"}:
        raise ValueError("reference_image_type must be GENERATED or UPLOADED.")

    parameters: dict[str, Any] = {
        "prompt": cleaned_prompt,
        "quantity": 1,
        "width": WIDTH,
        "height": HEIGHT,
        "style_ids": [
            "645e4195-f63d-4715-a3f2-3fb1e6eb8c70"
        ],
        "prompt_enhance": "OFF",
    }

    if reference_image_id:
        parameters["guidances"] = {
            "image_reference": [
                {
                    "image": {
                        "id": reference_image_id,
                        "type": reference_image_type,
                    },
                    "strength": IMAGE_REFERENCE_STRENGTH,
                }
            ]
        }

    payload = {
        "public": False,
        "model": MODEL,
        "parameters": parameters,
    }

    _, body = request_with_retries(
        method="POST",
        url=CREATE_URL,
        operation=operation,
        payload=payload,
    )

    generation_id = find_generation_id(body)
    cost = find_cost(body)

    if not generation_id:
        raise RuntimeError(
            f"{operation} succeeded but no generation ID was found. "
            f"See {LOG_PATH} for the response body."
        )

    if cost:
        print(f"{operation} cost: {cost.get('amount')} {cost.get('unit')}")
    else:
        print(f"{operation} cost: not returned by Leonardo")

    return generation_id, cost


def retrieve_generation(generation_id: str, operation: str) -> dict[str, Any]:
    _, body = request_with_retries(
        method="GET",
        url=f"{RETRIEVE_BASE_URL}/{generation_id}",
        operation=operation,
    )

    generation = extract_generation(body)

    if not generation:
        raise RuntimeError(
            f"Could not extract generation information for {generation_id}. "
            f"See {LOG_PATH} for the response body."
        )

    return generation


def wait_for_single_image(
    generation_id: str,
    operation: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + GENERATION_TIMEOUT_SECONDS
    poll_number = 0

    while time.monotonic() < deadline:
        poll_number += 1
        generation = retrieve_generation(
            generation_id,
            operation=f"{operation}:poll:{poll_number}",
        )

        status = str(generation.get("status", "")).upper()
        images = generation.get("generated_images") or []

        print(f"{operation} status: {status or 'UNKNOWN'}")

        if status == "COMPLETE" and images:
            image = images[0]

            if not isinstance(image, dict) or not image.get("id"):
                raise RuntimeError(
                    f"{operation} completed, but the first image has no ID."
                )

            return image

        if status in {"FAILED", "ERROR", "CANCELLED"}:
            raise RuntimeError(
                f"{operation} failed with status {status}. "
                f"See {LOG_PATH} for the response body."
            )

        time.sleep(POLL_INTERVAL_SECONDS)

    raise TimeoutError(
        f"{operation} did not complete within "
        f"{GENERATION_TIMEOUT_SECONDS} seconds."
    )


# -----------------------------
# db.json handling
# -----------------------------


def load_scenarios() -> list[dict[str, Any]]:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Could not find {DB_PATH.resolve()}. "
            "Place this script in the same folder as db.json."
        )

    with DB_PATH.open("r", encoding="utf-8") as db_file:
        data = json.load(db_file)

    if not isinstance(data, list):
        raise ValueError("db.json must contain a JSON array of scenarios.")

    required_fields = {"id", "img-q", "img-bad", "img-ok"}

    for index, scenario in enumerate(data, start=1):
        if not isinstance(scenario, dict):
            raise ValueError(f"Scenario {index} is not a JSON object.")

        missing = required_fields - scenario.keys()
        if missing:
            raise ValueError(
                f"Scenario {index} is missing fields: {sorted(missing)}"
            )

    return data


def save_results(results: list[dict[str, Any]]) -> None:
    with RESULTS_PATH.open("w", encoding="utf-8") as results_file:
        json.dump(results, results_file, indent=2, ensure_ascii=False)


def select_scenarios_by_id(
    scenarios: list[dict[str, Any]],
    first_iteration: int | str,
    last_iteration: int | str,
) -> list[dict[str, Any]]:
    """Return scenarios from first_iteration through last_iteration, inclusive."""
    first_id = str(first_iteration)
    last_id = str(last_iteration)
    first_index: int | None = None
    last_index: int | None = None

    for index, scenario in enumerate(scenarios):
        scenario_id = str(scenario["id"])

        if first_index is None and scenario_id == first_id:
            first_index = index

        if scenario_id == last_id:
            last_index = index

    if first_index is None:
        raise ValueError(
            f"first_iteration {first_iteration!r} was not found in {DB_PATH}."
        )

    if last_index is None:
        raise ValueError(
            f"last_iteration {last_iteration!r} was not found in {DB_PATH}."
        )

    if first_index > last_index:
        raise ValueError(
            "first_iteration must appear before or at last_iteration in db.json."
        )

    return scenarios[first_index : last_index + 1]


def download_image_as_jpg(
    image_url: Any,
    destination: Path,
    operation: str,
) -> None:
    """Download an image and convert it to a real RGB JPEG file."""
    if not isinstance(image_url, str) or not is_http_url(image_url):
        raise RuntimeError(f"{operation} returned no valid image URL.")

    response = requests.get(image_url, timeout=REQUEST_TIMEOUT_SECONDS)
    log_binary_response(
        operation=f"{operation}:download",
        response=response,
        request_metadata={
            "source": image_url,
            "destination": str(destination.resolve()),
        },
    )
    response.raise_for_status()

    temporary_path = destination.with_suffix(f"{destination.suffix}.tmp")

    try:
        with Image.open(BytesIO(response.content)) as source_image:
            source_image.convert("RGB").save(
                temporary_path,
                format="JPEG",
                quality=JPEG_QUALITY,
            )
        temporary_path.replace(destination)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    append_log(
        {
            "timestamp": utc_timestamp(),
            "operation": f"{operation}:save-jpg",
            "source": image_url,
            "destination": str(destination.resolve()),
            "size_bytes": destination.stat().st_size,
        }
    )
    print(f"Saved {destination}")


def ask_to_continue(scenario_id: Any, image_label: str) -> bool:
    """Ask whether the script should create the next image."""
    while True:
        try:
            answer = input(
                f"\n{image_label} for scenario {scenario_id} was downloaded. "
                "Continue? [y/n]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nNo confirmation received. Stopping the script.")
            return False

        if answer in {"y", "yes"}:
            return True

        if answer in {"n", "no"}:
            return False

        print("Please type y or n.")


def print_stop_summary(total_cost: Decimal) -> None:
    print("\nScript stopped by user.")
    print(f"Available results saved to {RESULTS_PATH}.")
    print(f"Detailed response bodies saved to {LOG_PATH}.")
    print(f"Total reported generation cost: {total_cost} DOLLARS")


# -----------------------------
# Main workflow
# -----------------------------


def main() -> None:
    args = parse_arguments()
    first_iteration = args.first_iteration
    last_iteration = args.last_iteration

    clear_log()
    scenarios = select_scenarios_by_id(
        load_scenarios(),
        first_iteration=first_iteration,
        last_iteration=last_iteration,
    )
    results: list[dict[str, Any]] = []
    total_cost = Decimal("0")

    print(
        f"Loaded {len(scenarios)} scenarios from {DB_PATH}, "
        f"from ID {first_iteration} through ID {last_iteration}."
    )
    print(f"Detailed API logs will be written to {LOG_PATH}.")

    # Reuse the existing Leonardo UPLOADED image ID without uploading again.
    logo_uploaded_image_id = LOGO_IMAGE_ID

    for iteration, scenario in enumerate(scenarios, start=1):
        scenario_id = scenario["id"]
        print(f"\n{'=' * 70}")
        print(f"Iteration {iteration} — scenario ID {scenario_id}")
        print(f"{'=' * 70}")

        print(
            f"Waiting {ITERATION_DELAY_SECONDS} seconds before "
            f"iteration {iteration}..."
        )
        time.sleep(ITERATION_DELAY_SECONDS)

        scenario_result: dict[str, Any] = {
            "img-q": None,
            "img-ok": None,
            "img-bad": None,
        }
        results.append(scenario_result)

        # 1. Generate and immediately download img-q using the uploaded reference.
        q_generation_id, q_cost = create_generation(
            prompt=str(scenario["img-q"]),
            operation=f"scenario:{scenario_id}:img-q:create",
            reference_image_id=logo_uploaded_image_id,
            reference_image_type="UPLOADED",
        )
        total_cost += decimal_cost(q_cost)

        q_image = wait_for_single_image(
            generation_id=q_generation_id,
            operation=f"scenario:{scenario_id}:img-q",
        )
        q_image_id = str(q_image["id"])
        q_url = q_image.get("url")
        print(f"img-q URL: {q_url}")

        download_image_as_jpg(
            image_url=q_url,
            destination=Path(f"{scenario_id}-q.jpg"),
            operation=f"scenario:{scenario_id}:img-q",
        )
        scenario_result["img-q"] = q_url
        save_results(results)

        if not ask_to_continue(scenario_id, "img-q"):
            print_stop_summary(total_cost)
            return

        # 2. Generate and immediately download img-bad using img-q.
        bad_generation_id, bad_cost = create_generation(
            prompt=str(scenario["img-bad"]),
            operation=f"scenario:{scenario_id}:img-bad:create",
            reference_image_id=q_image_id,
            reference_image_type="GENERATED",
        )
        total_cost += decimal_cost(bad_cost)

        bad_image = wait_for_single_image(
            generation_id=bad_generation_id,
            operation=f"scenario:{scenario_id}:img-bad",
        )
        bad_url = bad_image.get("url")
        print(f"img-bad URL: {bad_url}")

        download_image_as_jpg(
            image_url=bad_url,
            destination=Path(f"{scenario_id}-bad.jpg"),
            operation=f"scenario:{scenario_id}:img-bad",
        )
        scenario_result["img-bad"] = bad_url
        save_results(results)

        if not ask_to_continue(scenario_id, "img-bad"):
            print_stop_summary(total_cost)
            return

        # 3. Generate and immediately download img-ok using img-q.
        ok_generation_id, ok_cost = create_generation(
            prompt=str(scenario["img-ok"]),
            operation=f"scenario:{scenario_id}:img-ok:create",
            reference_image_id=q_image_id,
            reference_image_type="GENERATED",
        )
        total_cost += decimal_cost(ok_cost)

        ok_image = wait_for_single_image(
            generation_id=ok_generation_id,
            operation=f"scenario:{scenario_id}:img-ok",
        )
        ok_url = ok_image.get("url")
        print(f"img-ok URL: {ok_url}")

        download_image_as_jpg(
            image_url=ok_url,
            destination=Path(f"{scenario_id}-ok.jpg"),
            operation=f"scenario:{scenario_id}:img-ok",
        )
        scenario_result["img-ok"] = ok_url
        save_results(results)

        if not ask_to_continue(scenario_id, "img-ok"):
            print_stop_summary(total_cost)
            return

        print(f"\nScenario {scenario_id} completed.")
        print(f"img-q URL:   {q_url}")
        print(f"img-bad URL: {bad_url}")
        print(f"img-ok URL:  {ok_url}")
        print(f"Running total: {total_cost} DOLLARS")

    print(f"\nFinished. Results saved to {RESULTS_PATH}.")
    print(f"Detailed response bodies saved to {LOG_PATH}.")
    print(f"Total reported generation cost: {total_cost} DOLLARS")


if __name__ == "__main__":
    main()
