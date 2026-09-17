import csv
import json
from dataclasses import dataclass
import pytest

from crau.pipeline import ItemPipeline


@dataclass
class Empresa:
    nome: str
    cnpj: str


@dataclass
class Noticia:
    titulo: str
    data: str

    def serialize(self):
        # Custom serialize method format
        return {
            "titulo": self.titulo.upper(),
            "data": self.data,
            "custom_flag": True,
        }


def test_pipeline_dict_to_jsonl(tmp_path):
    pipeline = ItemPipeline(output_dir=tmp_path)
    pipeline.process_item({"a": 1, "b": "foo"})
    pipeline.process_item({"a": 2, "c": "bar"})
    pipeline.close()

    jsonl_file = tmp_path / "items.jsonl"
    assert jsonl_file.exists()
    lines = [json.loads(line) for line in jsonl_file.read_text().splitlines() if line]
    assert len(lines) == 2
    assert lines[0] == {"a": 1, "b": "foo"}
    assert lines[1] == {"a": 2, "c": "bar"}


def test_pipeline_dict_to_csv_consistency_check(tmp_path):
    pipeline = ItemPipeline(output_dir=tmp_path, default_format="csv")
    pipeline.process_item({"id": 1, "name": "first"})
    with pytest.raises(ValueError, match="Inconsistent keys for CSV export"):
        pipeline.process_item({"id": 2, "other": "mismatch"})
    pipeline.close()


def test_pipeline_dataclass_multi_item_csv(tmp_path):
    pipeline = ItemPipeline(output_dir=tmp_path, default_format="csv")

    # 1. Standard dataclass without custom .serialize()
    empresa1 = Empresa(nome="Pythonic Cafe", cnpj="12.345.678/0001-90")
    empresa2 = Empresa(nome="Outra Empresa", cnpj="98.765.432/0001-10")
    pipeline.process_item(empresa1)
    pipeline.process_item(empresa2)

    # 2. Dataclass with custom .serialize()
    noticia1 = Noticia(titulo="Lancamento", data="2026-04-18")
    pipeline.process_item(noticia1)

    pipeline.close()

    empresa_csv = tmp_path / "Empresa.csv"
    assert empresa_csv.exists()
    with open(empresa_csv, newline="") as f:
        reader = list(csv.reader(f))
        assert reader[0] == ["nome", "cnpj"]
        assert reader[1] == ["Pythonic Cafe", "12.345.678/0001-90"]
        assert reader[2] == ["Outra Empresa", "98.765.432/0001-10"]

    noticia_csv = tmp_path / "Noticia.csv"
    assert noticia_csv.exists()
    with open(noticia_csv, newline="") as f:
        reader = list(csv.reader(f))
        assert reader[0] == ["titulo", "data", "custom_flag"]
        assert reader[1] == ["LANCAMENTO", "2026-04-18", "True"]


def test_pipeline_dataclass_honors_jsonl_format(tmp_path):
    pipeline = ItemPipeline(output_dir=tmp_path)
    pipeline.process_item(Empresa(nome="Pythonic Cafe", cnpj="1"))
    pipeline.process_item(Noticia(titulo="Lancamento", data="2026-04-18"))
    pipeline.close()

    assert not (tmp_path / "Empresa.csv").exists()
    rows = [json.loads(line) for line in (tmp_path / "Empresa.jsonl").read_text().splitlines()]
    assert rows == [{"nome": "Pythonic Cafe", "cnpj": "1"}]
    rows = [json.loads(line) for line in (tmp_path / "Noticia.jsonl").read_text().splitlines()]
    assert rows == [Noticia(titulo="Lancamento", data="2026-04-18").serialize()]


def test_pipeline_csv_header_written_once_across_runs(tmp_path):
    for _ in range(2):
        pipeline = ItemPipeline(output_dir=tmp_path, default_format="csv")
        pipeline.process_item({"a": 1, "b": 2})
        pipeline.process_item(Empresa(nome="x", cnpj="y"))
        pipeline.close()

    with open(tmp_path / "items.csv", newline="") as fobj:
        assert list(csv.reader(fobj)) == [["a", "b"], ["1", "2"], ["1", "2"]]
    with open(tmp_path / "Empresa.csv", newline="") as fobj:
        assert list(csv.reader(fobj)) == [["nome", "cnpj"], ["x", "y"], ["x", "y"]]


def test_pipeline_rejects_unknown_format(tmp_path):
    with pytest.raises(ValueError):
        ItemPipeline(output_dir=tmp_path, default_format="xml")
