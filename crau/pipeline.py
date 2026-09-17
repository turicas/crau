from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path
from typing import Any, TextIO


class ItemPipeline:
    """Append scraped items (dicts or dataclasses) to files in an output directory.

    Dicts go to `items.<format>`, dataclasses to `<ClassName>.<format>`; format is
    `jsonl` (default) or `csv`. Files are opened in append mode so successive runs
    accumulate rows; a CSV header is only written when the file is empty.
    """

    def __init__(self, output_dir: str | Path, default_format: str = "jsonl"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.default_format = default_format.lower()
        if self.default_format not in ("jsonl", "csv"):
            raise ValueError(f"Unsupported items format: {default_format!r} (expected 'jsonl' or 'csv')")

        self._files: dict[str, TextIO] = {}
        self._csv_writers: dict[str, csv.writer] = {}
        self._csv_headers: dict[str, list[str]] = {}
        self._dataclass_fields_cache: dict[type, tuple[str, ...]] = {}

    def _get_file(self, filename: str) -> TextIO:
        if filename not in self._files:
            self._files[filename] = open(self.output_dir / filename, mode="a", encoding="utf-8", newline="")
        return self._files[filename]

    def _write_jsonl(self, filename: str, row: dict[str, Any]) -> None:
        fobj = self._get_file(filename)
        fobj.write(json.dumps(row, default=str) + "\n")
        fobj.flush()

    def _write_csv(self, filename: str, row: dict[str, Any]) -> None:
        fobj = self._get_file(filename)
        keys = list(row.keys())
        if filename not in self._csv_writers:
            writer = csv.writer(fobj)
            self._csv_writers[filename] = writer
            self._csv_headers[filename] = keys
            if fobj.tell() == 0:
                writer.writerow(keys)
        header = self._csv_headers[filename]
        if keys != header:
            raise ValueError(f"Inconsistent keys for CSV export to {filename}. Expected {header}, got {keys}")
        self._csv_writers[filename].writerow([row[key] for key in header])
        fobj.flush()

    def _write(self, stem: str, row: dict[str, Any]) -> None:
        filename = f"{stem}.{self.default_format}"
        if self.default_format == "csv":
            self._write_csv(filename, row)
        else:
            self._write_jsonl(filename, row)

    def _dataclass_to_row(self, item: Any) -> dict[str, Any]:
        serialize = getattr(item, "serialize", None)
        if callable(serialize):
            return serialize()
        cls = type(item)
        if cls not in self._dataclass_fields_cache:
            self._dataclass_fields_cache[cls] = tuple(field.name for field in dataclasses.fields(cls))
        # attribute access instead of dataclasses.asdict: no deep copy of nested values
        return {name: getattr(item, name) for name in self._dataclass_fields_cache[cls]}

    def process_item(self, item: Any) -> None:
        if isinstance(item, dict):
            self._write("items", item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            self._write(type(item).__name__, self._dataclass_to_row(item))
        else:
            raise TypeError(f"Unsupported item type for pipeline: {type(item).__name__}")

    def close(self) -> None:
        for fobj in self._files.values():
            fobj.close()
        self._files.clear()
        self._csv_writers.clear()
        self._csv_headers.clear()
