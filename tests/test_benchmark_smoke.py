import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "benchmarks" / "overhead.py"
spec = importlib.util.spec_from_file_location("overhead_benchmark", SCRIPT)
assert spec and spec.loader
overhead = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = overhead  # dataclasses resolve their module through sys.modules
spec.loader.exec_module(overhead)


def test_every_scenario_runs():
    results = overhead.run(n=200, repeats=1)
    assert [r.scenario for r in results] == [s.name for s in overhead.SCENARIOS]
    assert all(r.us_per_request > 0 for r in results)
    bare = [r for r in results if r.delta_us is None]
    bare_names = {f"{w}: {f} bare" for w in overhead.WORKLOADS for f in ("starlette", "fastapi")}
    assert {r.scenario for r in bare} == bare_names
    assert all(r.pct_of_request is not None for r in results if r.delta_us is not None)
    assert len(results) - len(bare) == 4 * len(overhead.WORKLOADS)


def test_select_adds_the_baseline():
    names = [s.name for s in overhead.select(["realistic: fastapi + route_class"])]
    assert names == ["realistic: fastapi bare", "realistic: fastapi + route_class"]
    assert len(overhead.select()) == len(overhead.SCENARIOS)


def test_unknown_scenario_raises():
    with pytest.raises(ValueError, match="no such scenario"):
        overhead.select(["no such scenario"])
    with pytest.raises(ValueError, match="unknown scenario"):
        overhead.run(n=10, repeats=1, names=["nope"])


def test_realistic_workload_does_real_work():
    assert overhead._payload(1, "trivial") == {"id": 1}
    assert len(overhead._payload(1, "realistic")["items"]) == overhead.PAYLOAD_ITEMS


def test_markdown_table():
    table = overhead.markdown(overhead.run(n=50, repeats=1, names=["trivial: starlette + middleware (memory)"]))
    lines = table.splitlines()
    assert lines[0].startswith("| Scenario")
    assert len(lines) == 4
    assert "| trivial: starlette bare |" in lines[2] and lines[2].endswith("| - |")
    assert "+" in lines[3] and "%" not in lines[3]


def test_main_prints_table_and_json(capsys):
    overhead.main(["--n", "50", "--repeats", "1"])
    assert "| Scenario" in capsys.readouterr().out
    overhead.main(["--n", "50", "--repeats", "1", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == len(overhead.SCENARIOS)
    assert {"scenario", "us_per_request", "delta_us", "pct_of_request"} <= set(data[0])


def test_main_rejects_bad_arguments():
    with pytest.raises(SystemExit):
        overhead.main(["--n", "abc"])


def test_main_writes_json_file(tmp_path, capsys):
    target = tmp_path / "out.json"
    overhead.main(["--n", "50", "--repeats", "1", "--json-file", str(target)])
    assert "| Scenario" in capsys.readouterr().out
    assert len(json.loads(target.read_text(encoding="utf-8"))) == len(overhead.SCENARIOS)
