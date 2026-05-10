from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Literal

WORD_BITS = 32
OPCODE_BITS = 8
ARG_BITS = WORD_BITS - OPCODE_BITS

WORD_MASK = (1 << WORD_BITS) - 1
OPCODE_MASK = (1 << OPCODE_BITS) - 1
ARG_MASK = (1 << ARG_BITS) - 1
ARG_SIGN_BIT = 1 << (ARG_BITS - 1)

MIN_SIGNED_ARG = -(1 << (ARG_BITS - 1))
MAX_SIGNED_ARG = (1 << (ARG_BITS - 1)) - 1
MAX_UNSIGNED_ARG = ARG_MASK

WORD_BYTEORDER: Literal["big", "little"] = "big"
WORD_SIZE_BYTES = WORD_BITS // 8

# Адреса memory-mapped ввода-вывода.
MMIO_IN_DATA = 0xFFF0
MMIO_IN_STATUS = 0xFFF1
MMIO_OUT_DATA = 0xFFF2
MMIO_IRQ_ACK = 0xFFF3
MMIO_READ_ONLY = {MMIO_IN_DATA, MMIO_IN_STATUS}
MMIO_ADDRESSES = {MMIO_IN_DATA, MMIO_IN_STATUS, MMIO_OUT_DATA, MMIO_IRQ_ACK}


class Opcode(IntEnum):
    """Коды операций стекового процессора."""

    # Стек и литералы
    NOP = 0x00
    LIT = 0x01
    DUP = 0x02
    DROP = 0x03
    SWAP = 0x04
    OVER = 0x05
    # Арифметика
    ADD = 0x10
    SUB = 0x11
    MUL = 0x12
    DIV = 0x13
    MOD = 0x14
    # Сравнения
    EQ = 0x20
    LT = 0x21
    GT = 0x22
    # Память данных
    LOAD = 0x30
    STORE = 0x31
    # Управление потоком
    JMP = 0x40
    JZ = 0x41
    CALL = 0x42
    RET = 0x43
    EXECUTE = 0x44
    # Прерывания
    EI = 0x50
    DI = 0x51
    IRET = 0x52
    # Останов
    HALT = 0xFF


ARGUMENT_OPCODES = {
    Opcode.LIT,
    Opcode.JMP,
    Opcode.JZ,
    Opcode.CALL,
}

SIGNED_ARGUMENT_OPCODES = {
    Opcode.LIT,
}

MNEMONICS: dict[Opcode, str] = {
    Opcode.NOP: "nop",
    Opcode.LIT: "lit",
    Opcode.DUP: "dup",
    Opcode.DROP: "drop",
    Opcode.SWAP: "swap",
    Opcode.OVER: "over",
    Opcode.ADD: "add",
    Opcode.SUB: "sub",
    Opcode.MUL: "mul",
    Opcode.DIV: "div",
    Opcode.MOD: "mod",
    Opcode.EQ: "eq",
    Opcode.LT: "lt",
    Opcode.GT: "gt",
    Opcode.LOAD: "load",
    Opcode.STORE: "store",
    Opcode.JMP: "jmp",
    Opcode.JZ: "jz",
    Opcode.CALL: "call",
    Opcode.RET: "ret",
    Opcode.EXECUTE: "execute",
    Opcode.EI: "ei",
    Opcode.DI: "di",
    Opcode.IRET: "iret",
    Opcode.HALT: "halt",
}


@dataclass(frozen=True, slots=True)
class Instruction:
    """Декодированная инструкция."""

    opcode: Opcode
    arg: int = 0


class IsaError(ValueError):
    """Ошибка кодирования или декодирования инструкции."""


def sign_extend_arg(raw_arg: int) -> int:
    """Преобразовать 24-битное знаковое значение в int."""
    if not 0 <= raw_arg <= ARG_MASK:
        raise IsaError(f"raw argument out of 24-bit range: {raw_arg}")
    if raw_arg & ARG_SIGN_BIT:
        return raw_arg - (1 << ARG_BITS)
    return raw_arg


def encode_arg(arg: int, *, signed: bool) -> int:
    """Закодировать аргумент в младшие 24 бита."""
    if signed:
        if not MIN_SIGNED_ARG <= arg <= MAX_SIGNED_ARG:
            raise IsaError(
                f"signed argument {arg} does not fit into {ARG_BITS} bits [{MIN_SIGNED_ARG}, {MAX_SIGNED_ARG}]"
            )
        return arg & ARG_MASK

    if not 0 <= arg <= MAX_UNSIGNED_ARG:
        raise IsaError(f"unsigned argument {arg} does not fit into {ARG_BITS} bits [0, {MAX_UNSIGNED_ARG}]")
    return arg


def encode_instruction(opcode: Opcode | int, arg: int = 0) -> int:
    """Закодировать opcode и аргумент в 32-битную инструкцию."""
    try:
        op = Opcode(opcode)
    except ValueError as exc:
        raise IsaError(f"unknown opcode: {opcode}") from exc

    has_argument = op in ARGUMENT_OPCODES
    if not has_argument and arg != 0:
        raise IsaError(f"instruction {op.name} does not accept an argument")

    raw_arg = encode_arg(arg, signed=op in SIGNED_ARGUMENT_OPCODES)
    return ((int(op) & OPCODE_MASK) << ARG_BITS) | raw_arg


def decode_instruction(word: int) -> Instruction:
    """Декодировать одну 32-битную инструкцию."""
    if not 0 <= word <= WORD_MASK:
        raise IsaError(f"machine word out of 32-bit range: {word}")

    opcode_value = (word >> ARG_BITS) & OPCODE_MASK
    raw_arg = word & ARG_MASK

    try:
        opcode = Opcode(opcode_value)
    except ValueError as exc:
        raise IsaError(f"unknown opcode in machine word 0x{word:08X}: 0x{opcode_value:02X}") from exc

    arg = sign_extend_arg(raw_arg) if opcode in SIGNED_ARGUMENT_OPCODES else raw_arg
    return Instruction(opcode=opcode, arg=arg)


def write_program_binary(path: str | Path, instructions: Iterable[Instruction]) -> None:
    """Записать память команд в бинарный файл."""
    with Path(path).open("wb") as file:
        for instruction in instructions:
            word = encode_instruction(instruction.opcode, instruction.arg)
            file.write(word.to_bytes(WORD_SIZE_BYTES, byteorder=WORD_BYTEORDER, signed=False))


def read_program_binary(path: str | Path) -> list[Instruction]:
    """Прочитать память команд из бинарного файла."""
    data = Path(path).read_bytes()
    if len(data) % WORD_SIZE_BYTES != 0:
        raise IsaError(f"program binary size must be divisible by {WORD_SIZE_BYTES}, got {len(data)}")

    instructions: list[Instruction] = []
    for offset in range(0, len(data), WORD_SIZE_BYTES):
        chunk = data[offset : offset + WORD_SIZE_BYTES]
        word = int.from_bytes(chunk, byteorder=WORD_BYTEORDER, signed=False)
        instructions.append(decode_instruction(word))
    return instructions


def write_data_binary(path: str | Path, words: Iterable[int]) -> None:
    """Записать память данных как 32-битные слова."""
    with Path(path).open("wb") as file:
        for word in words:
            raw_word = word & WORD_MASK
            file.write(raw_word.to_bytes(WORD_SIZE_BYTES, byteorder=WORD_BYTEORDER, signed=False))


def read_data_binary(path: str | Path) -> list[int]:
    """Прочитать память данных как 32-битные слова."""
    data = Path(path).read_bytes()
    if len(data) % WORD_SIZE_BYTES != 0:
        raise IsaError(f"data binary size must be divisible by {WORD_SIZE_BYTES}, got {len(data)}")

    words: list[int] = []
    for offset in range(0, len(data), WORD_SIZE_BYTES):
        chunk = data[offset : offset + WORD_SIZE_BYTES]
        words.append(int.from_bytes(chunk, byteorder=WORD_BYTEORDER, signed=False))
    return words


def format_instruction_listing_line(address: int, instruction: Instruction) -> str:
    """Сформировать строку листинга команды."""
    word = encode_instruction(instruction.opcode, instruction.arg)
    mnemonic = MNEMONICS[instruction.opcode]
    if instruction.opcode in ARGUMENT_OPCODES:
        mnemonic = f"{mnemonic} {instruction.arg}"
    return f"{address:08X} - {word:08X} - {mnemonic}"


def format_data_listing_line(address: int, word: int) -> str:
    """Сформировать строку листинга данных."""
    return f"{address:08X} - {word & WORD_MASK:08X} - {word}"


def make_program_listing(instructions: Iterable[Instruction]) -> str:
    """Создать человекочитаемый листинг команд."""
    return "\n".join(
        format_instruction_listing_line(address, instruction) for address, instruction in enumerate(instructions)
    )


def make_data_listing(words: Iterable[int]) -> str:
    """Создать человекочитаемый листинг данных."""
    return "\n".join(format_data_listing_line(address, word) for address, word in enumerate(words))
