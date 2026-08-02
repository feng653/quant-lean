"""Build the offline research validation matrix from existing cache files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.research.validation_matrix import (  # noqa: E402
    build_validation_matrix,
    render_markdown,
)


def _outputs(value: str) -> tuple[Path, Path]:
    path = Path(value)
    suffix = path.suffix.lower()
    if suffix == ".json":
        return path, path.with_suffix(".md")
    if suffix in {".md", ".markdown"}:
        return path.with_suffix(".json"), path
    return (
        path / "RESEARCH_VALIDATION_MATRIX.json",
        path / "RESEARCH_VALIDATION_MATRIX.md",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Offline/read-only validation of registered strategies against "
            "existing parquet caches. Never fetches network data."
        )
    )
    parser.add_argument(
        "--cache-dir",
        default=str(settings.abs_path(settings.DATA_CACHE_DIR)),
        help="Existing cache root containing daily/*.parquet",
    )
    parser.add_argument(
        "--pool",
        action="append",
        dest="pools",
        help="Pool id to validate; repeat for multiple pools",
    )
    parser.add_argument(
        "--source-kind",
        choices=["cached_real", "synthetic"],
        default="cached_real",
    )
    parser.add_argument("--max-rows", type=int, default=420)
    parser.add_argument("--max-codes", type=int, default=12)
    parser.add_argument(
        "--output",
        help=(
            "Optional .json/.md path or output directory. Without this option "
            "both formats are printed and no files are written."
        ),
    )
    args = parser.parse_args(argv)
    report = build_validation_matrix(
        args.cache_dir,
        pool_ids=args.pools,
        source_kind=args.source_kind,
        max_rows=args.max_rows,
        max_codes=args.max_codes,
    )
    json_text = json.dumps(
        report,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
    )
    markdown = render_markdown(report)
    if args.output:
        json_path, markdown_path = _outputs(args.output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json_text + "\n", encoding="utf-8")
        markdown_path.write_text(markdown, encoding="utf-8")
        print(
            json.dumps(
                {
                    "json": json_path.name,
                    "markdown": markdown_path.name,
                    "rows": report["matrix_row_count"],
                },
                ensure_ascii=False,
            )
        )
    else:
        sys.stdout.write(json_text)
        sys.stdout.write("\n\n--- MARKDOWN ---\n\n")
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
