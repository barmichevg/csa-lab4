from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

TICK_LIMIT = 300_000
MAX_LOG_LINES = 300

from isa import (  # noqa: E402
    make_data_listing,
    make_program_listing,
    read_data_binary,
    read_program_binary,
    write_data_binary,
    write_program_binary,
)
from machine import Machine  # noqa: E402
from translator import translate_source  # noqa: E402


@pytest.mark.golden_test("golden/*.yml")
def test_translator_and_machine(golden) -> None:
    with tempfile.TemporaryDirectory() as tmpdirname:
        tmpdir = Path(tmpdirname)
        code_bin = tmpdir / "program.bin"
        data_bin = tmpdir / "data.bin"
        input_file = tmpdir / "input.txt"

        input_file.write_text(golden["in_stdin"], encoding="utf-8")

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            instructions, data = translate_source(golden["in_source"])
            write_program_binary(code_bin, instructions)
            write_data_binary(data_bin, data)

            print(f"program: {code_bin.name} ({len(instructions)} instructions)")
            print(f"data:    {data_bin.name} ({len(data)} cells)")
            print("============================================================")

            machine = Machine.from_files(code_bin, data_bin, input_file)
            output = machine.run(limit=TICK_LIMIT)
            print(output, end="")
            print(f"ticks: {machine.tick_counter}")

        code_listing = make_program_listing(read_program_binary(code_bin))
        data_listing = make_data_listing(read_data_binary(data_bin))
        log_listing = adapt_log(machine.log_lines, max_lines=MAX_LOG_LINES)

    assert golden.out["out_code"] == ensure_trailing_newline(code_listing)
    assert golden.out["out_data"] == ensure_trailing_newline(data_listing)
    assert golden.out["out_stdout"] == stdout.getvalue()
    assert golden.out["out_log"] == ensure_trailing_newline(log_listing)


def adapt_log(log_lines: list[str], max_lines: int) -> str:
    if len(log_lines) <= max_lines:
        return "\n".join(log_lines)

    head_count = max_lines // 2
    tail_count = max_lines // 4
    important_count = max_lines - head_count - tail_count - 1
    head = log_lines[:head_count]
    tail = log_lines[-tail_count:]
    middle = log_lines[head_count:-tail_count]
    important = [line for line in middle if is_important_log_line(line)][:important_count]
    omitted = len(log_lines) - len(head) - len(important) - len(tail)
    return "\n".join([*head, *important, f"... omitted {omitted} lines ...", *tail])


def is_important_log_line(line: str) -> bool:
    return any(
        marker in line
        for marker in (
            "[enter irq]",
            "MODE: irq",
            "[iret",
            "0xFFF0",
            "0xFFF2",
            "0xFFF3",
            "[halt]",
            "cache_hit",
            "cache_miss",
            "cache_wait",
        )
    )


def ensure_trailing_newline(text: str) -> str:
    return "" if not text else text.rstrip("\n") + "\n"
