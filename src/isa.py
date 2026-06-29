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
WORD_SIGN_BIT = 1 << (WORD_BITS - 1)
OPCODE_MASK = (1 << OPCODE_BITS) - 1
ARG_MASK = (1 << ARG_BITS) - 1
ARG_SIGN_BIT = 1 << (ARG_BITS - 1)

MIN_SIGNED_ARG = -(1 << (ARG_BITS - 1))
MAX_SIGNED_ARG = (1 << (ARG_BITS - 1)) - 1
MAX_UNSIGNED_ARG = ARG_MASK

WORD_BYTEORDER: Literal["big", "little"] = "big"
WORD_SIZE_BYTES = WORD_BITS // 8

RESET_VECTOR = 0
IRQ_VECTOR = 1

# Адреса memory-mapped ввода-вывода.
MMIO_IN_DATA = 0xFFF0
MMIO_IN_STATUS = 0xFFF1
MMIO_OUT_DATA = 0xFFF2
MMIO_IRQ_ACK = 0xFFF3

# Биты статуса входа и регистра подтверждения IRQ.
INPUT_STATUS_READY = 0b0001
INPUT_STATUS_OVERRUN = 0b0010
IRQ_ACK_READY = INPUT_STATUS_READY
IRQ_ACK_OVERRUN = INPUT_STATUS_OVERRUN


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


def validate_word(word: int) -> None:
    """Проверить, что значение помещается в одно машинное слово."""
    if not 0 <= word <= WORD_MASK:
        raise IsaError(f"machine word out of 32-bit range: {word}")


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

    raw_arg = encode_arg(arg, signed=op is Opcode.LIT)
    return ((int(op) & OPCODE_MASK) << ARG_BITS) | raw_arg


def opcode_from_word(word: int) -> Opcode:
    """Выделить поле opcode[31:24] из сырого 32-битного слова IR."""
    validate_word(word)

    opcode_value = (word >> ARG_BITS) & OPCODE_MASK
    try:
        return Opcode(opcode_value)
    except ValueError as exc:
        raise IsaError(f"unknown opcode in machine word 0x{word:08X}: 0x{opcode_value:02X}") from exc


def raw_arg_from_word(word: int) -> int:
    """Выделить беззнаковое поле arg[23:0] из сырого слова IR."""
    validate_word(word)
    return word & ARG_MASK


def signed_arg_from_word(word: int) -> int:
    """Интерпретировать поле arg[23:0] как знаковый литерал."""
    return sign_extend_arg(raw_arg_from_word(word))


def decode_instruction(word: int) -> Instruction:
    """Комбинационно декодировать одно 32-битное слово IR."""
    opcode = opcode_from_word(word)
    raw_arg = word & ARG_MASK

    if opcode not in ARGUMENT_OPCODES and raw_arg != 0:
        raise IsaError(f"instruction {opcode.name} must not have an argument")

    arg = sign_extend_arg(raw_arg) if opcode is Opcode.LIT else raw_arg
    return Instruction(opcode=opcode, arg=arg)


def write_words(path: str | Path, words: Iterable[int]) -> None:
    """Записать последовательность 32-битных слов в бинарный файл."""
    with Path(path).open("wb") as file:
        for word in words:
            raw_word = word & WORD_MASK
            file.write(raw_word.to_bytes(WORD_SIZE_BYTES, byteorder=WORD_BYTEORDER, signed=False))


def write_program_binary(path: str | Path, instructions: Iterable[Instruction]) -> None:
    """Записать память команд в бинарный файл."""
    write_words(path, (encode_instruction(instruction.opcode, instruction.arg) for instruction in instructions))


def read_words(path: str | Path, *, description: str) -> list[int]:
    """Прочитать бинарный файл как последовательность 32-битных слов."""
    data = Path(path).read_bytes()
    if len(data) % WORD_SIZE_BYTES != 0:
        raise IsaError(f"{description} binary size must be divisible by {WORD_SIZE_BYTES}, got {len(data)}")

    return [
        int.from_bytes(
            data[offset : offset + WORD_SIZE_BYTES],
            byteorder=WORD_BYTEORDER,
            signed=False,
        )
        for offset in range(0, len(data), WORD_SIZE_BYTES)
    ]


def read_program_words(path: str | Path) -> list[int]:
    """Прочитать память команд как сырые 32-битные машинные слова."""
    return read_words(path, description="program")


def read_program_binary(path: str | Path) -> list[Instruction]:
    """Прочитать и декодировать память команд для листинга и тестов."""
    return [decode_instruction(word) for word in read_program_words(path)]


def write_data_binary(path: str | Path, words: Iterable[int]) -> None:
    """Записать память данных как 32-битные слова."""
    write_words(path, words)


def read_data_binary(path: str | Path) -> list[int]:
    """Прочитать память данных как 32-битные слова."""
    return read_words(path, description="data")


def format_instruction(instruction: Instruction) -> str:
    """Сформировать текст инструкции и при необходимости добавить аргумент."""
    mnemonic = instruction.opcode.mnemonic
    if instruction.opcode in ARGUMENT_OPCODES:
        return f"{mnemonic} {instruction.arg}"
    return mnemonic


def format_instruction_listing_line(address: int, instruction: Instruction) -> str:
    """Сформировать строку листинга команды."""
    word = encode_instruction(instruction.opcode, instruction.arg)
    return f"{address:08X} - {word:08X} - {format_instruction(instruction)}"


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
