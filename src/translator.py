from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from isa import (
    IRQ_ACK_READY,
    IRQ_VECTOR,
    MMIO_IN_DATA,
    MMIO_IN_STATUS,
    MMIO_IRQ_ACK,
    MMIO_OUT_DATA,
    RESET_VECTOR,
    Instruction,
    IsaError,
    Opcode,
    encode_instruction,
    make_data_listing,
    make_program_listing,
    write_data_binary,
    write_program_binary,
)


class TranslatorError(RuntimeError):
    """Ошибка трансляции программы."""


class TokenKind(Enum):
    WORD = "word"
    NUMBER = "number"
    PSTRING = "pstring"
    PRINT_STRING = "print_string"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    value: str | int
    line: int
    column: int
    source_name: str


@dataclass(frozen=True, slots=True)
class Fixup:
    instruction_index: int
    label: str
    token: Token


class ControlKind(Enum):
    IF = "if"
    ELSE = "else"
    BEGIN = "begin"


@dataclass(frozen=True, slots=True)
class ControlFrame:
    kind: ControlKind
    address: int
    opening_token: Token


@dataclass(frozen=True, slots=True)
class Procedure:
    name: str
    body: list[Token]
    is_irq: bool = False


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

USER_WORD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_?!-]*$")
INTERNAL_WORD_NAME_RE = re.compile(r"^__[A-Za-z0-9_?!-]+$")
UNSAFE_EXECUTION_TOKEN_WORDS = {"iret", "halt"}
STRING_TOKEN_PREFIXES = {
    'p"': TokenKind.PSTRING,
    '."': TokenKind.PRINT_STRING,
}
STRING_ESCAPE_MAP = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "0": "\0"}


class Compiler:
    """Компилятор."""

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
        self.builtin_xt_labels: dict[str, str] = {}

    def compile(self, source: str, *, source_name: str = "<input>") -> tuple[list[Instruction], list[int]]:
        stdlib_tokens = tokenize(load_stdlib_source(), source_name="<stdlib>")
        user_tokens = tokenize(source, source_name=source_name)
        tokens = [*stdlib_tokens, *user_tokens]
        procedures, main_tokens = self.parse(tokens)

        for procedure in procedures:
            self._compile_procedure(procedure.name, procedure.body, procedure.is_irq)

        main_addr = self.current_address
        self.code_labels["main"] = main_addr
        self._compile_tokens(main_tokens, "main")
        self.emit_halt_if_needed()

        self._emit_builtin_xt_trampolines()

        if self.irq_handler_label is None:
            self.irq_handler_label = "__default_irq_handler"
            self.code_labels[self.irq_handler_label] = self.current_address
            self.emit(Opcode.LIT, IRQ_ACK_READY)
            self.emit(Opcode.LIT, MMIO_IRQ_ACK)
            self.emit(Opcode.STORE)
            self.emit(Opcode.IRET)

        self.instructions[RESET_VECTOR] = Instruction(Opcode.JMP, main_addr)
        self.instructions[IRQ_VECTOR] = Instruction(Opcode.JMP, self.code_labels[self.irq_handler_label])

        self._resolve_fixups()
        return self.instructions, self.data

    @property
    def current_address(self) -> int:
        return len(self.instructions)

    def parse(self, tokens: list[Token]) -> tuple[list[Procedure], list[Token]]:
        procedures: list[Procedure] = []
        procedure_names: set[str] = set()
        main_tokens: list[Token] = []

        index = 0
        while index < len(tokens):
            token = tokens[index]

            if is_word(token, "variable") or is_word(token, "buffer"):
                declaration = str(token.value)
                name_token = require_token(tokens, index + 1, f"expected {declaration} name after '{declaration}'")
                name = require_word_name(name_token)
                if name in procedure_names:
                    raise error_at(name_token, f"name already used as procedure: {name}")

                size = 1
                consumed = 2
                if declaration == "buffer":
                    size_token = require_token(tokens, index + 2, "expected buffer size after buffer name")
                    if size_token.kind is not TokenKind.NUMBER:
                        raise error_at(size_token, "buffer size must be an integer literal")
                    size = int(size_token.value)
                    consumed = 3

                self._declare_data(name, size, name_token)
                index += consumed
                continue

            if is_word(token, ":"):
                name_token = require_token(tokens, index + 1, "expected procedure name after ':'")
                name = require_word_name(name_token)
                body, next_index = collect_until_semicolon(tokens, index + 2)
                self._check_user_word_name(name, name_token)
                if name in procedure_names:
                    raise error_at(name_token, f"procedure already declared: {name}")
                procedure_names.add(name)
                procedures.append(Procedure(name, body))
                index = next_index
                continue

            if is_word(token, ":irq"):
                body, next_index = collect_until_semicolon(tokens, index + 1)
                if self.irq_handler_label is not None:
                    raise error_at(token, "only one :irq handler is allowed")
                procedures.append(Procedure("__irq_handler", body, is_irq=True))
                self.irq_handler_label = "__irq_handler"
                index = next_index
                continue

            main_tokens.append(token)
            index += 1

        return procedures, main_tokens

    def emit(
        self,
        opcode: Opcode,
        arg: int = 0,
        *,
        token: Token | None = None,
    ) -> int:
        instruction = self._make_instruction(opcode, arg, token=token)
        index = len(self.instructions)
        self.instructions.append(instruction)
        return index

    @staticmethod
    def _make_instruction(opcode: Opcode, arg: int = 0, *, token: Token | None = None) -> Instruction:
        try:
            encode_instruction(opcode, arg)
        except IsaError as exc:
            if token is not None:
                raise error_at(token, str(exc)) from exc
            raise TranslatorError(str(exc)) from exc
        return Instruction(opcode, arg)

    def emit_fixup(self, opcode: Opcode, label: str, token: Token) -> None:
        index = self.emit(opcode, 0, token=token)
        self.fixups.append(Fixup(index, label, token))

    def emit_halt_if_needed(self) -> None:
        if not self.instructions or self.instructions[-1].opcode is not Opcode.HALT:
            self.emit(Opcode.HALT)

    def builtin_xt_label(self, name: str) -> str:
        label = self.builtin_xt_labels.get(name)
        if label is None:
            label = f"__xt_builtin_{len(self.builtin_xt_labels)}"
            self.builtin_xt_labels[name] = label
        return label

    def _emit_builtin_xt_trampolines(self) -> None:
        for name, label in self.builtin_xt_labels.items():
            if label in self.code_labels:
                continue
            opcode = BUILTIN_WORDS[name]
            self.code_labels[label] = self.current_address
            self.emit(opcode)
            self.emit(Opcode.RET)

    def patch_instruction_arg(self, instruction_index: int, arg: int) -> None:
        old = self.instructions[instruction_index]
        self.instructions[instruction_index] = self._make_instruction(old.opcode, arg)

    def _ensure_data_capacity(self, cell_count: int, token: Token | None = None) -> None:
        new_size = len(self.data) + cell_count
        if new_size <= MMIO_IN_DATA:
            return

        message = f"data image occupies {new_size} words and overlaps MMIO starting at 0x{MMIO_IN_DATA:04X}"
        if token is not None:
            raise error_at(token, message)
        raise TranslatorError(message)

    def allocate_zeroed(self, size: int, token: Token | None = None) -> int:
        address = len(self.data)
        self._ensure_data_capacity(size, token)
        self.data.extend([0] * size)
        return address

    def allocate_pstring(self, text: str, token: Token) -> int:
        try:
            encoded = text.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise error_at(token, "Pascal string must contain only one-byte characters") from exc

        address = len(self.data)
        self._ensure_data_capacity(1 + len(encoded), token)
        self.data.append(len(encoded))
        self.data.extend(encoded)
        return address

    def _declare_data(self, name: str, size: int, token: Token) -> None:
        self._check_user_word_name(name, token)
        if size <= 0:
            raise error_at(token, "data size must be positive")
        self.data_labels[name] = self.allocate_zeroed(size, token)

    def _check_user_word_name(self, name: str, token: Token) -> None:
        is_internal_stdlib_name = token.source_name == "<stdlib>" and INTERNAL_WORD_NAME_RE.fullmatch(name) is not None
        if not USER_WORD_NAME_RE.fullmatch(name) and not is_internal_stdlib_name:
            raise error_at(token, f"invalid word name: {name}")
        if name in RESERVED_WORDS or name.startswith("__xt_builtin_"):
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

        source_token = tokens[-1] if tokens else None
        self.emit(Opcode.RET, token=source_token)

    def _compile_tokens(self, tokens: list[Token], name: str) -> None:
        control_stack: list[ControlFrame] = []
        index = 0

        while index < len(tokens):
            index += self._compile_token(tokens, index, control_stack)

        if control_stack:
            frame = control_stack[-1]
            raise error_at(frame.opening_token, f"unclosed {frame.kind.value} in {name}")

    def _compile_token(
        self,
        tokens: list[Token],
        index: int,
        control_stack: list[ControlFrame],
    ) -> int:
        token = tokens[index]

        match token.kind:
            case TokenKind.NUMBER:
                self.emit(Opcode.LIT, int(token.value), token=token)

            case TokenKind.PSTRING | TokenKind.PRINT_STRING:
                address = self.allocate_pstring(str(token.value), token)
                self.emit(Opcode.LIT, address, token=token)
                if token.kind is TokenKind.PRINT_STRING:
                    self.emit_fixup(Opcode.CALL, "type", token)

            case TokenKind.WORD:
                return self._compile_word(tokens, index, control_stack)

            case _:
                raise error_at(token, f"unsupported token kind: {token.kind}")

        return 1

    def _compile_word(
        self,
        tokens: list[Token],
        index: int,
        control_stack: list[ControlFrame],
    ) -> int:
        token = tokens[index]
        word = str(token.value)

        if word == "'":
            name_token = require_token(tokens, index + 1, "expected word name after execution-token quote")
            quoted_name = require_word_name(name_token)
            if quoted_name in UNSAFE_EXECUTION_TOKEN_WORDS:
                raise error_at(name_token, f"word cannot be used as execution token: {quoted_name}")
            label = self.builtin_xt_label(quoted_name) if quoted_name in BUILTIN_WORDS else quoted_name
            self.emit_fixup(Opcode.LIT, label, name_token)
            return 2

        if self._compile_control_word(word, token, control_stack):
            return 1

        opcode = BUILTIN_WORDS.get(word)
        if opcode is not None:
            self.emit(opcode, token=token)
            return 1

        literal_address = MMIO_WORDS.get(word)
        if literal_address is None:
            literal_address = self.data_labels.get(word)

        if literal_address is not None:
            self.emit(Opcode.LIT, literal_address, token=token)
        else:
            self.emit_fixup(Opcode.CALL, word, token)
        return 1

    def _compile_control_word(
        self,
        word: str,
        token: Token,
        control_stack: list[ControlFrame],
    ) -> bool:
        match word:
            case "if":
                placeholder = self.emit(Opcode.JZ, 0, token=token)
                control_stack.append(ControlFrame(ControlKind.IF, placeholder, token))

            case "else":
                frame = require_control_frame(control_stack, token, ControlKind.IF)
                control_stack.pop()
                jump = self.emit(Opcode.JMP, 0, token=token)
                self.patch_instruction_arg(frame.address, self.current_address)
                control_stack.append(ControlFrame(ControlKind.ELSE, jump, token))

            case "then":
                frame = require_control_frame(control_stack, token, ControlKind.IF, ControlKind.ELSE)
                control_stack.pop()
                self.patch_instruction_arg(frame.address, self.current_address)

            case "begin":
                control_stack.append(ControlFrame(ControlKind.BEGIN, self.current_address, token))

            case "until":
                frame = require_control_frame(control_stack, token, ControlKind.BEGIN)
                control_stack.pop()
                self.emit(Opcode.JZ, frame.address, token=token)

            case _:
                return False

        return True

    def _resolve_fixups(self) -> None:
        for fixup in self.fixups:
            if fixup.label not in self.code_labels:
                raise error_at(fixup.token, f"unknown word: {fixup.label}")
            address = self.code_labels[fixup.label]
            self.patch_instruction_arg(fixup.instruction_index, address)


# Токенизация и вспомогательные функции
def require_control_frame(
    control_stack: list[ControlFrame],
    token: Token,
    *allowed_kinds: ControlKind,
) -> ControlFrame:
    if not control_stack:
        expected = "/".join(kind.value for kind in allowed_kinds)
        raise error_at(token, f"{token.value} without matching {expected}")

    frame = control_stack[-1]
    if frame.kind not in allowed_kinds:
        allowed = "/".join(kind.value for kind in allowed_kinds)
        opener = frame.opening_token
        raise error_at(
            token,
            f"{token.value} cannot close {frame.kind.value} opened at "
            f"{opener.source_name}:{opener.line}:{opener.column}; expected {allowed}",
        )
    return frame


def tokenize(source: str, *, source_name: str = "<input>") -> list[Token]:
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

        string_kind = STRING_TOKEN_PREFIXES.get(source[index : index + 2])
        if string_kind is not None:
            start_line, start_column = line, column
            text, index, line, column = read_string_literal(source, index + 2, line, column + 2)
            tokens.append(Token(string_kind, text, start_line, start_column, source_name))
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
        kind = TokenKind.NUMBER if number is not None else TokenKind.WORD
        value: str | int = number if number is not None else raw
        tokens.append(Token(kind, value, start_line, start_column, source_name))

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
            chars.append(STRING_ESCAPE_MAP.get(escaped, escaped))
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


INTEGER_RE = re.compile(r"-?[0-9]+\Z")


def parse_number(raw: str) -> int | None:
    if INTEGER_RE.fullmatch(raw) is None:
        return None
    return int(raw, 10)


def is_word(token: Token, word: str) -> bool:
    return token.kind is TokenKind.WORD and token.value == word


def require_token(tokens: list[Token], index: int, message: str) -> Token:
    if index >= len(tokens):
        raise TranslatorError(message)
    return tokens[index]


def require_word_name(token: Token) -> str:
    if token.kind is not TokenKind.WORD:
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
    return TranslatorError(f"{message} at {token.source_name}:{token.line}:{token.column}")


def with_trailing_newline(text: str) -> str:
    return "" if not text else text.rstrip("\n") + "\n"


# Консольный интерфейс
def load_stdlib_source() -> str:
    return Path(__file__).with_name("stdlib.fth").read_text(encoding="utf-8")


def translate_source(
    source: str,
    *,
    source_name: str = "<input>",
) -> tuple[list[Instruction], list[int]]:
    compiler = Compiler()
    return compiler.compile(source, source_name=source_name)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Транслировать программу в бинарный машинный код")
    parser.add_argument("source", type=Path, help="исходный файл программы")
    parser.add_argument("program", type=Path, help="выходной program.bin")
    parser.add_argument("data", type=Path, help="выходной data.bin")
    parser.add_argument("--program-hex", type=Path, default=None, help="листинг команд")
    parser.add_argument("--data-hex", type=Path, default=None, help="листинг данных")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    instructions, data = translate_source(args.source.read_text(encoding="utf-8"), source_name=str(args.source))

    program_hex = args.program_hex or args.program.with_suffix(args.program.suffix + ".hex")
    data_hex = args.data_hex or args.data.with_suffix(args.data.suffix + ".hex")

    write_program_binary(args.program, instructions)
    write_data_binary(args.data, data)
    program_hex.write_text(with_trailing_newline(make_program_listing(instructions)), encoding="utf-8")
    data_hex.write_text(with_trailing_newline(make_data_listing(data)), encoding="utf-8")

    print(f"program: {args.program} ({len(instructions)} instructions)")
    print(f"data:    {args.data} ({len(data)} cells)")
    print(f"listing: {program_hex}")
    print(f"listing: {data_hex}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
