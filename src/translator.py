from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from isa import (
    MMIO_IN_DATA,
    MMIO_IN_STATUS,
    MMIO_IRQ_ACK,
    MMIO_OUT_DATA,
    Instruction,
    Opcode,
    make_data_listing,
    make_program_listing,
    write_data_binary,
    write_program_binary,
)

RESET_VECTOR = 0
IRQ_VECTOR = 1


class TranslatorError(RuntimeError):
    """Ошибка трансляции MiniForth-программы."""


class TokenKind(Enum):
    WORD = "word"
    NUMBER = "number"
    PSTRING = "pstring"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str | int
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class Fixup:
    instruction_index: int
    label: str
    token: Token
    opcode: Opcode


BUILTIN_WORDS: dict[str, Opcode] = {
    "dup": Opcode.DUP,
    "drop": Opcode.DROP,
    "swap": Opcode.SWAP,
    "over": Opcode.OVER,
    "+": Opcode.ADD,
    "-": Opcode.SUB,
    "*": Opcode.MUL,
    "/": Opcode.DIV,
    "mod": Opcode.MOD,
    "=": Opcode.EQ,
    "<": Opcode.LT,
    ">": Opcode.GT,
    "@": Opcode.LOAD,
    "!": Opcode.STORE,
    "execute": Opcode.EXECUTE,
    "ei": Opcode.EI,
    "di": Opcode.DI,
    "iret": Opcode.IRET,
    "halt": Opcode.HALT,
}

MMIO_WORDS: dict[str, int] = {
    "in-data": MMIO_IN_DATA,
    "in-status": MMIO_IN_STATUS,
    "out-data": MMIO_OUT_DATA,
    "irq-ack": MMIO_IRQ_ACK,
}

RESERVED_WORDS = (
    set(BUILTIN_WORDS)
    | set(MMIO_WORDS)
    | {
        ":",
        ";",
        ":irq",
        "variable",
        "buffer",
        "if",
        "else",
        "then",
        "begin",
        "until",
        "'",
        "main",
        "__irq_handler",
        "__default_irq_handler",
    }
)


class Compiler:
    """Компилятор MiniForth."""

    def __init__(self) -> None:
        self.instructions: list[Instruction] = [
            Instruction(Opcode.JMP, 0),
            Instruction(Opcode.JMP, 0),
        ]
        self.data: list[int] = []

        self.data_labels: dict[str, int] = {}
        self.code_labels: dict[str, int] = {}
        self.fixups: list[Fixup] = []

        self.irq_handler_label: str | None = None

    def compile(self, source: str) -> tuple[list[Instruction], list[int]]:
        full_source = load_stdlib_source() + "\n" + source
        tokens = tokenize(full_source)
        procedures, main_tokens = self.parse(tokens)

        for name, body, is_irq in procedures:
            self._compile_procedure(name, body, is_irq)

        main_addr = self.current_address
        self.code_labels["main"] = main_addr
        self._compile_tokens(main_tokens, "main")
        self.emit(Opcode.HALT)

        if self.irq_handler_label is None:
            self.irq_handler_label = "__default_irq_handler"
            self.code_labels[self.irq_handler_label] = self.current_address
            self.emit(Opcode.IRET)

        self.instructions[RESET_VECTOR] = Instruction(Opcode.JMP, main_addr)
        self.instructions[IRQ_VECTOR] = Instruction(Opcode.JMP, self.code_labels[self.irq_handler_label])

        self._resolve_fixups()
        return self.instructions, self.data

    @property
    def current_address(self) -> int:
        return len(self.instructions)

    def parse(self, tokens: list[Token]) -> tuple[list[tuple[str, list[Token], bool]], list[Token]]:
        procedures: list[tuple[str, list[Token], bool]] = []
        procedure_names: set[str] = set()
        main_tokens: list[Token] = []

        index = 0
        while index < len(tokens):
            token = tokens[index]

            if is_word(token, "variable"):
                name_token = require_token(tokens, index + 1, "expected variable name after 'variable'")
                name = require_word_name(name_token)
                if name in procedure_names:
                    raise error_at(name_token, f"name already used as procedure: {name}")
                self._declare_variable(name, name_token)
                index += 2
                continue

            if is_word(token, "buffer"):
                name_token = require_token(tokens, index + 1, "expected buffer name after 'buffer'")
                size_token = require_token(tokens, index + 2, "expected buffer size after buffer name")
                name = require_word_name(name_token)
                if name in procedure_names:
                    raise error_at(name_token, f"name already used as procedure: {name}")
                if size_token.kind != TokenKind.NUMBER:
                    raise error_at(size_token, "buffer size must be an integer literal")
                self._declare_buffer(name, int(size_token.value), name_token)
                index += 3
                continue

            if is_word(token, ":"):
                name_token = require_token(tokens, index + 1, "expected procedure name after ':'")
                name = require_word_name(name_token)
                body, next_index = collect_until_semicolon(tokens, index + 2)
                self._check_user_word_name(name, name_token)
                if name in procedure_names:
                    raise error_at(name_token, f"procedure already declared: {name}")
                procedure_names.add(name)
                procedures.append((name, body, False))
                index = next_index
                continue

            if is_word(token, ":irq"):
                body, next_index = collect_until_semicolon(tokens, index + 1)
                if self.irq_handler_label is not None:
                    raise error_at(token, "only one :irq handler is allowed")
                procedures.append(("__irq_handler", body, True))
                self.irq_handler_label = "__irq_handler"
                index = next_index
                continue

            main_tokens.append(token)
            index += 1

        return procedures, main_tokens

    def emit(self, opcode: Opcode, arg: int = 0) -> int:
        index = len(self.instructions)
        self.instructions.append(Instruction(opcode, arg))
        return index

    def emit_fixup(self, opcode: Opcode, label: str, token: Token) -> None:
        index = self.emit(opcode, 0)
        self.fixups.append(Fixup(index, label, token, opcode))

    def patch_instruction_arg(self, instruction_index: int, arg: int) -> None:
        old = self.instructions[instruction_index]
        self.instructions[instruction_index] = Instruction(old.opcode, arg)

    def allocate_data(self, values: Iterable[int]) -> int:
        address = len(self.data)
        self.data.extend(values)
        return address

    def allocate_pstring(self, text: str) -> int:
        values = [len(text)] + [ord(char) for char in text]
        return self.allocate_data(values)

    def _declare_variable(self, name: str, token: Token) -> None:
        self._check_user_word_name(name, token)
        if name in self.data_labels:
            raise error_at(token, f"data label already declared: {name}")
        self.data_labels[name] = self.allocate_data([0])

    def _declare_buffer(self, name: str, size: int, token: Token) -> None:
        self._check_user_word_name(name, token)
        if size <= 0:
            raise error_at(token, "buffer size must be positive")
        if name in self.data_labels:
            raise error_at(token, f"data label already declared: {name}")
        self.data_labels[name] = self.allocate_data([0] * size)

    def _check_user_word_name(self, name: str, token: Token) -> None:
        if name in RESERVED_WORDS:
            raise error_at(token, f"reserved word cannot be redefined: {name}")
        if name in self.code_labels:
            raise error_at(token, f"procedure already declared: {name}")
        if name in self.data_labels:
            raise error_at(token, f"data label already declared: {name}")

    def _compile_procedure(self, name: str, tokens: list[Token], is_irq: bool) -> None:
        if name in self.code_labels:
            raise TranslatorError(f"duplicate procedure label: {name}")

        self.code_labels[name] = self.current_address
        self._compile_tokens(tokens, name)

        if is_irq:
            if not tokens or not is_word(tokens[-1], "iret"):
                raise TranslatorError(":irq handler must explicitly end with iret")
            return

        self.emit(Opcode.RET)

    def _compile_tokens(self, tokens: list[Token], name: str) -> None:
        if_stack: list[int] = []
        begin_stack: list[int] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]

            if token.kind == TokenKind.NUMBER:
                self.emit(Opcode.LIT, int(token.value))
                index += 1
                continue

            if token.kind == TokenKind.PSTRING:
                address = self.allocate_pstring(str(token.value))
                self.emit(Opcode.LIT, address)
                index += 1
                continue

            if token.kind != TokenKind.WORD:
                raise error_at(token, f"unsupported token kind: {token.kind}")

            word = str(token.value)

            if word == "'":
                name_token = require_token(tokens, index + 1, "expected word name after execution-token quote")
                name = require_word_name(name_token)
                self.emit_fixup(Opcode.LIT, name, name_token)
                index += 2
                continue

            if word == "if":
                placeholder_index = self.emit(Opcode.JZ, 0)
                if_stack.append(placeholder_index)
                index += 1
                continue

            if word == "else":
                if not if_stack:
                    raise error_at(token, "else without matching if")
                jz_index = if_stack.pop()
                jmp_index = self.emit(Opcode.JMP, 0)
                self.patch_instruction_arg(jz_index, self.current_address)
                if_stack.append(jmp_index)
                index += 1
                continue

            if word == "then":
                if not if_stack:
                    raise error_at(token, "then without matching if")
                jump_index = if_stack.pop()
                self.patch_instruction_arg(jump_index, self.current_address)
                index += 1
                continue

            if word == "begin":
                begin_stack.append(self.current_address)
                index += 1
                continue

            if word == "until":
                if not begin_stack:
                    raise error_at(token, "until without matching begin")
                begin_address = begin_stack.pop()
                self.emit(Opcode.JZ, begin_address)
                index += 1
                continue

            if word in BUILTIN_WORDS:
                self.emit(BUILTIN_WORDS[word])
                index += 1
                continue

            if word in MMIO_WORDS:
                self.emit(Opcode.LIT, MMIO_WORDS[word])
                index += 1
                continue

            if word in self.data_labels:
                self.emit(Opcode.LIT, self.data_labels[word])
                index += 1
                continue

            self.emit_fixup(Opcode.CALL, word, token)
            index += 1

        if if_stack:
            raise TranslatorError(f"unclosed if in {name}")
        if begin_stack:
            raise TranslatorError(f"unclosed begin in {name}")

    def _resolve_fixups(self) -> None:
        for fixup in self.fixups:
            if fixup.label not in self.code_labels:
                raise error_at(fixup.token, f"unknown word: {fixup.label}")
            address = self.code_labels[fixup.label]
            self.instructions[fixup.instruction_index] = Instruction(fixup.opcode, address)


# Токенизация и вспомогательные функции
def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    line = 1
    column = 1

    while index < len(source):
        char = source[index]

        if char in " \t\r\n":
            index, line, column = advance_position(source, index, line, column)
            continue

        if char == "\\":
            while index < len(source) and source[index] != "\n":
                index, line, column = advance_position(source, index, line, column)
            continue

        if source.startswith('p"', index):
            start_line, start_column = line, column
            text, index, line, column = read_string_literal(source, index + 2, line, column + 2)
            tokens.append(Token(TokenKind.PSTRING, text, start_line, start_column))
            continue

        start_index = index
        start_line = line
        start_column = column
        while index < len(source) and source[index] not in " \t\r\n":
            if source[index] == "\\":
                break
            index, line, column = advance_position(source, index, line, column)

        raw = source[start_index:index]
        if not raw:
            continue

        number = parse_number(raw)
        if number is not None:
            tokens.append(Token(TokenKind.NUMBER, number, start_line, start_column))
        else:
            tokens.append(Token(TokenKind.WORD, raw, start_line, start_column))

    return tokens


def read_string_literal(
    source: str,
    index: int,
    line: int,
    column: int,
) -> tuple[str, int, int, int]:
    chars: list[str] = []

    while index < len(source):
        char = source[index]
        if char == '"':
            index, line, column = advance_position(source, index, line, column)
            return "".join(chars), index, line, column

        if char == "\\":
            if index + 1 >= len(source):
                raise TranslatorError(f"unfinished escape sequence at line {line}, column {column}")
            escaped = source[index + 1]
            escape_map = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "0": "\0"}
            chars.append(escape_map.get(escaped, escaped))
            index, line, column = advance_position(source, index, line, column)
            index, line, column = advance_position(source, index, line, column)
            continue

        chars.append(char)
        index, line, column = advance_position(source, index, line, column)

    raise TranslatorError("unterminated string literal")


def advance_position(source: str, index: int, line: int, column: int) -> tuple[int, int, int]:
    if source[index] == "\n":
        return index + 1, line + 1, 1
    return index + 1, line, column + 1


def parse_number(raw: str) -> int | None:
    if raw in {"+", "-"}:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def is_word(token: Token, word: str) -> bool:
    return token.kind == TokenKind.WORD and token.value == word


def require_token(tokens: list[Token], index: int, message: str) -> Token:
    if index >= len(tokens):
        raise TranslatorError(message)
    return tokens[index]


def require_word_name(token: Token) -> str:
    if token.kind != TokenKind.WORD:
        raise error_at(token, "expected word name")
    return str(token.value)


def collect_until_semicolon(tokens: list[Token], start_index: int) -> tuple[list[Token], int]:
    body: list[Token] = []
    index = start_index
    while index < len(tokens):
        token = tokens[index]
        if is_word(token, ";"):
            return body, index + 1
        body.append(token)
        index += 1
    raise TranslatorError("definition is not closed with ';'")


def error_at(token: Token, message: str) -> TranslatorError:
    return TranslatorError(f"{message} at line {token.line}, column {token.column}")


# Консольный интерфейс
def load_stdlib_source() -> str:
    return Path(__file__).with_name("stdlib.fth").read_text(encoding="utf-8")


def translate_source(source: str) -> tuple[list[Instruction], list[int]]:
    return Compiler().compile(source)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Транслировать MiniForth в бинарный машинный код")
    parser.add_argument("source", type=Path, help="исходный файл MiniForth")
    parser.add_argument("program", type=Path, help="выходной program.bin")
    parser.add_argument("data", type=Path, help="выходной data.bin")
    parser.add_argument("--program-hex", type=Path, default=None, help="листинг команд")
    parser.add_argument("--data-hex", type=Path, default=None, help="листинг данных")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    instructions, data = translate_source(args.source.read_text(encoding="utf-8"))

    program_hex = args.program_hex or args.program.with_suffix(args.program.suffix + ".hex")
    data_hex = args.data_hex or args.data.with_suffix(args.data.suffix + ".hex")

    write_program_binary(args.program, instructions)
    write_data_binary(args.data, data)
    program_hex.write_text(make_program_listing(instructions).rstrip() + "\n", encoding="utf-8")
    data_hex.write_text(make_data_listing(data).rstrip() + "\n", encoding="utf-8")

    print(f"program: {args.program} ({len(instructions)} instructions)")
    print(f"data:    {args.data} ({len(data)} cells)")
    print(f"listing: {program_hex}")
    print(f"listing: {data_hex}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
