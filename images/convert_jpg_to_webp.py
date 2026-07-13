#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageOps


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert JPG/JPEG images from a folder to compressed WebP files."
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=Path.cwd(),
        help="Input folder. Default: current folder.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output folder. Default: <input>/webp_output.",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=80,
        help="WebP quality from 1 to 100. Default: 80.",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=1200,
        help="Maximum width. Default: 1200.",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=900,
        help="Maximum height. Default: 900.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Also process JPG files in subfolders.",
    )
    return parser.parse_args()


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def unique_output_path(folder: Path, source: Path) -> Path:
    candidate = folder / f"{source.stem}.webp"
    counter = 2

    while candidate.exists():
        candidate = folder / f"{source.stem}_{counter}.webp"
        counter += 1

    return candidate


def convert_image(
    source: Path,
    destination: Path,
    quality: int,
    max_width: int,
    max_height: int,
) -> tuple[int, int]:
    original_size = source.stat().st_size

    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)

        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        image.thumbnail(
            (max_width, max_height),
            Image.Resampling.LANCZOS,
        )

        image.save(
            destination,
            format="WEBP",
            quality=quality,
            method=6,
            optimize=True,
        )

    return original_size, destination.stat().st_size


def main() -> int:
    args = parse_args()

    if not 1 <= args.quality <= 100:
        print("Error: --quality must be between 1 and 100.", file=sys.stderr)
        return 1

    if args.max_width <= 0 or args.max_height <= 0:
        print("Error: dimensions must be positive.", file=sys.stderr)
        return 1

    input_folder = args.input.expanduser().resolve()
    output_folder = (
        args.output.expanduser().resolve()
        if args.output
        else input_folder / "webp_output"
    )

    if not input_folder.is_dir():
        print(f"Error: input folder does not exist: {input_folder}", file=sys.stderr)
        return 1

    output_folder.mkdir(parents=True, exist_ok=True)

    pattern = "**/*" if args.recursive else "*"
    files = sorted(
        path
        for path in input_folder.glob(pattern)
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg"}
    )

    if not files:
        print(f"No JPG or JPEG files found in: {input_folder}")
        return 0

    total_original = 0
    total_webp = 0
    converted = 0
    failed = 0

    print(f"Input:  {input_folder}")
    print(f"Output: {output_folder}")
    print(f"Found:  {len(files)} image(s)\n")

    for source in files:
        destination = unique_output_path(output_folder, source)

        try:
            original_size, webp_size = convert_image(
                source,
                destination,
                args.quality,
                args.max_width,
                args.max_height,
            )
            total_original += original_size
            total_webp += webp_size
            converted += 1

            reduction = 100 * (1 - webp_size / original_size)
            print(
                f"OK  {source.name} -> {destination.name} "
                f"({format_size(original_size)} -> {format_size(webp_size)}, "
                f"{reduction:.1f}% smaller)"
            )
        except Exception as error:
            failed += 1
            print(f"FAIL {source.name}: {error}", file=sys.stderr)

    print(f"\nConverted: {converted}")
    print(f"Failed:    {failed}")

    if converted:
        total_reduction = 100 * (1 - total_webp / total_original)
        print(
            f"Total:     {format_size(total_original)} -> "
            f"{format_size(total_webp)} "
            f"({total_reduction:.1f}% smaller)"
        )

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
