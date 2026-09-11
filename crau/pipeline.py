from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path
from typing import Any, TextIO


class ItemPipeline:
    """Fast item processing pipeline for dicts and dataclasses without asdict overhead."""

    def __init__(self, output_dir: str | Path, default_format: str = "jsonl"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.default_format = default_format.lower()

        self._files: dict[str, TextIO] = {}
        self._csv_writers: dict[str, csv.writer] = {}
        self._dict_csv_header: list[str] | None = None
        self._dataclass_fields_cache: dict[type, tuple[str, ...]] = {}

    def _get_file(self, filename: str) -> TextIO:
        if filename not in self._files:
            file_path = self.output_dir / filename
            self._files[filename] = open(file_path, mode="a", encoding="utf-8", newline="")
        return self._files[filename]

    def _process_dict(self, item: dict[str, Any]) -> None:
        if self.default_format == "csv":
            filename = "items.csv"
            fobj = self._get_file(filename)
            keys = list(item.keys())

            if self._dict_csv_header is None:
                self._dict_csv_header = keys
                writer = csv.writer(fobj)
                self._csv_writers[filename] = writer
                writer.writerow(keys)
                fobj.flush()
            elif keys != self._dict_csv_header:
                raise ValueError(
                    f"Inconsistent dict keys for CSV export. Expected {self._dict_csv_header}, got {keys}"
                )

            writer = self._csv_writers[filename]
            writer.writerow([item.get(k) for k in self._dict_csv_header])
            fobj.flush()
        else:
            filename = "items.jsonl"
            fobj = self._get_file(filename)
            fobj.write(json.dumps(item, default=str) + "\n")
            fobj.flush()

    def _process_dataclass(self, item: Any) -> None:
        cls = type(item)
        class_name = cls.__name__
        filename = f"{class_name}.csv"
        fobj = self._get_file(filename)

        has_custom_serialize = hasattr(item, "serialize") and callable(getattr(item, "serialize"))

        if has_custom_serialize:
            serialized_dict = item.serialize()
            keys = list(serialized_dict.keys())
            if filename not in self._csv_writers:
                writer = csv.writer(fobj)
                self._csv_writers[filename] = writer
                writer.writerow(keys)
                fobj.flush()
            writer = self._csv_writers[filename]
            writer.writerow([serialized_dict.get(k) for k in keys])
            fobj.flush()
        else:
            if cls not in self._dataclass_fields_cache:
                self._dataclass_fields_cache[cls] = tuple(f.name for f in dataclasses.fields(cls))
            field_names = self._dataclass_fields_cache[cls]

            if filename not in self._csv_writers:
                writer = csv.writer(fobj)
                self._csv_writers[filename] = writer
                writer.writerow(field_names)
                fobj.flush()

            writer = self._csv_writers[filename]
            # Fast attribute extraction without dataclasses.asdict deepcopy
            row = tuple(getattr(item, field_name) for field_name in field_names)
            writer.writerow(row)
            fobj.flush()

    def process_item(self, item: Any) -> None:
        if isinstance(item, dict):
            self._process_dict(item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            self._process_dataclass(item)
        else:
            raise TypeError(f"Unsupported item type for pipeline: {type(item).__name__}")

    def close(self) -> None:
        for fobj in self._files.values():
            try:
                fobj.close()
            except Exception:
                pass
        self._files.clear()
        self._csv_writers.clear()
