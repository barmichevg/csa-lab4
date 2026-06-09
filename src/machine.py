from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from isa import (
    MMIO_IN_DATA,
    MMIO_IN_STATUS,
    MMIO_IRQ_ACK,
    MMIO_OUT_DATA,
    MMIO_READ_ONLY,
    Instruction,
    Opcode,
    read_data_binary,
    read_program_binary,
)

WORD_BITS = 32
WORD_MASK = (1 << WORD_BITS) - 1
WORD_SIGN_BIT = 1 << (WORD_BITS - 1)

DEFAULT_DATA_MEMORY_SIZE = 4096
DEFAULT_STACK_LIMIT = 1024
DEFAULT_TICK_LIMIT = 100_000

DEFAULT_CACHE_LINES = 8
CACHE_HIT_TICKS = 1
MEMORY_ACCESS_TICKS = 10
MEMORY_WAIT_TICKS = MEMORY_ACCESS_TICKS - CACHE_HIT_TICKS

RESET_VECTOR = 0
IRQ_VECTOR = 1


class MachineError(RuntimeError):
    """Ошибка состояния процессора или программы."""


class MicroState(Enum):
    """Состояния Control Unit."""

    FETCH = "fetch"
    DECODE = "decode"
    EXECUTE = "execute"
    MEM_WAIT = "mem_wait"
    IRQ_CHECK = "irq_check"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class InputEvent:
    """Одно событие ввода."""

    tick: int
    char_code: int


@dataclass(slots=True)
class CacheLine:
    valid: bool = False
    tag: int = 0
    value: int = 0


@dataclass(slots=True)
class DataCache:
    """Прямо отображаемый кэш, одна ячейка на строку."""

    line_count: int = DEFAULT_CACHE_LINES
    lines: list[CacheLine] = field(init=False)
    hits: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        if self.line_count <= 0:
            raise MachineError("cache line count must be positive")
        self.lines = [CacheLine() for _ in range(self.line_count)]

    def read(self, address: int, backing_words: list[int]) -> tuple[int, bool]:
        index = address % self.line_count
        tag = address // self.line_count
        line = self.lines[index]

        if line.valid and line.tag == tag:
            self.hits += 1
            return to_signed32(line.value), True

        self.misses += 1
        value = backing_words[address]
        line.valid = True
        line.tag = tag
        line.value = value
        return to_signed32(value), False

    def write(self, address: int, value: int, backing_words: list[int]) -> bool:
        raw_value = to_word(value)
        index = address % self.line_count
        tag = address // self.line_count
        line = self.lines[index]

        hit = line.valid and line.tag == tag
        if hit:
            self.hits += 1
        else:
            self.misses += 1
            line.valid = True
            line.tag = tag

        line.value = raw_value
        backing_words[address] = raw_value
        return hit


@dataclass(slots=True)
class DataMemory:
    """Память данных, MMIO и кэш."""

    words: list[int]
    cache_enabled: bool = True
    cache_line_count: int = DEFAULT_CACHE_LINES
    output_buffer: list[str] = field(default_factory=list)
    input_data: int = 0
    input_status: int = 0
    irq_pending: bool = False
    input_overrun_count: int = 0
    cache: DataCache | None = field(init=False)
    uncached_reads: int = 0
    uncached_writes: int = 0

    def __post_init__(self) -> None:
        self.cache = DataCache(self.cache_line_count) if self.cache_enabled else None

    @classmethod
    def from_image(
        cls,
        image: Iterable[int],
        *,
        minimum_size: int = DEFAULT_DATA_MEMORY_SIZE,
        cache_enabled: bool = True,
        cache_line_count: int = DEFAULT_CACHE_LINES,
    ) -> DataMemory:
        words = [to_word(value) for value in image]
        if len(words) < minimum_size:
            words.extend([0] * (minimum_size - len(words)))
        return cls(words=words, cache_enabled=cache_enabled, cache_line_count=cache_line_count)

    def read(self, address: int) -> tuple[int | None, int, str, str]:
        if address == MMIO_IN_DATA:
            value = to_signed32(self.input_data)
            return value, 0, f"mmio_read [0x{address:04X}] -> {value}", f"load [0x{address:04X}] -> {value}"
        if address == MMIO_IN_STATUS:
            value = self.input_status
            return value, 0, f"mmio_read [0x{address:04X}] -> {value}", f"load [0x{address:04X}] -> {value}"
        if address == MMIO_OUT_DATA:
            return 0, 0, f"mmio_read [0x{address:04X}] -> 0", f"load [0x{address:04X}] -> 0"
        if address == MMIO_IRQ_ACK:
            return 0, 0, f"mmio_read [0x{address:04X}] -> 0", f"load [0x{address:04X}] -> 0"

        self._check_regular_address(address)

        if self.cache is None:
            self.uncached_reads += 1
            value = to_signed32(self.words[address])
            return (
                value,
                MEMORY_WAIT_TICKS,
                f"memory_read [0x{address:04X}] -> {value}; wait={MEMORY_WAIT_TICKS}",
                f"memory_read_done [0x{address:04X}] -> {value}",
            )

        value, hit = self.cache.read(address, self.words)
        if hit:
            return value, 0, f"cache_hit read [0x{address:04X}] -> {value}", f"load [0x{address:04X}] -> {value}"

        return (
            value,
            MEMORY_WAIT_TICKS,
            f"cache_miss read [0x{address:04X}] -> {value}; wait={MEMORY_WAIT_TICKS}",
            f"cache_fill_done read [0x{address:04X}] -> {value}",
        )

    def write(self, address: int, value: int) -> tuple[int | None, int, str, str]:
        if address == MMIO_OUT_DATA:
            self.output_buffer.append(chr(value & 0xFF))
            return None, 0, f"mmio_write {value} -> [0x{address:04X}]", f"store {value} -> [0x{address:04X}]"

        if address == MMIO_IRQ_ACK:
            if value != 0:
                self.input_status = 0
                self.irq_pending = False
            return None, 0, f"mmio_write {value} -> [0x{address:04X}]", f"store {value} -> [0x{address:04X}]"

        if address in MMIO_READ_ONLY:
            raise MachineError(f"attempt to write read-only MMIO register 0x{address:04X}")

        self._check_regular_address(address)

        if self.cache is None:
            self.uncached_writes += 1
            self.words[address] = to_word(value)
            return (
                None,
                MEMORY_WAIT_TICKS,
                f"memory_write {value} -> [0x{address:04X}]; wait={MEMORY_WAIT_TICKS}",
                f"memory_write_done {value} -> [0x{address:04X}]",
            )

        hit = self.cache.write(address, value, self.words)
        if hit:
            return None, 0, f"cache_hit write {value} -> [0x{address:04X}]", f"store {value} -> [0x{address:04X}]"

        return (
            None,
            MEMORY_WAIT_TICKS,
            f"cache_miss write {value} -> [0x{address:04X}]; wait={MEMORY_WAIT_TICKS}",
            f"cache_fill_done write {value} -> [0x{address:04X}]",
        )

    def push_input_char(self, char_code: int) -> None:
        """Передать байт во входной регистр устройства."""
        if self.input_status != 0:
            self.input_overrun_count += 1
            return

        self.input_data = char_code & 0xFF
        self.input_status = 1
        self.irq_pending = True

    @property
    def output(self) -> str:
        return "".join(self.output_buffer)

    @property
    def cache_hits(self) -> int:
        return 0 if self.cache is None else self.cache.hits

    @property
    def cache_misses(self) -> int:
        return 0 if self.cache is None else self.cache.misses

    def _check_regular_address(self, address: int) -> None:
        if not 0 <= address < len(self.words):
            raise MachineError(f"data memory address out of range: 0x{address:X}")


@dataclass(slots=True)
class Machine:
    """Модель процессора MiniForth."""

    program_memory: list[Instruction]
    data_memory: DataMemory
    input_events: list[InputEvent] = field(default_factory=list)
    stack_limit: int = DEFAULT_STACK_LIMIT

    pc: int = RESET_VECTOR
    ir: Instruction | None = None
    micro_state: MicroState = MicroState.FETCH
    tick_counter: int = 0

    data_stack: list[int] = field(default_factory=list)
    return_stack: list[int] = field(default_factory=list)

    irq_enable: bool = False
    in_irq: bool = False
    halted: bool = False

    memory_wait_ticks: int = 0
    pending_memory_kind: str | None = None
    pending_memory_value: int | None = None
    pending_memory_final_event: str = ""

    executed_instructions: int = 0
    log_lines: list[str] = field(default_factory=list)
    _next_input_event_index: int = 0

    @classmethod
    def from_files(
        cls,
        program_path: str | Path,
        data_path: str | Path,
        input_path: str | Path | None = None,
        *,
        cache_enabled: bool = True,
        cache_line_count: int = DEFAULT_CACHE_LINES,
    ) -> Machine:
        program = read_program_binary(program_path)
        data_image = read_data_binary(data_path)
        input_events = read_input_schedule(input_path) if input_path is not None else []
        return cls(
            program_memory=program,
            data_memory=DataMemory.from_image(
                data_image,
                cache_enabled=cache_enabled,
                cache_line_count=cache_line_count,
            ),
            input_events=input_events,
        )

    def run(self, *, limit: int = DEFAULT_TICK_LIMIT) -> str:
        while not self.halted and self.tick_counter < limit:
            self.step_tick()

        if not self.halted:
            raise MachineError(f"tick limit exceeded: {limit}")

        return self.data_memory.output

    def step_tick(self) -> None:
        state_before = self.micro_state

        if self.halted:
            self.micro_state = MicroState.HALTED
            self._append_log("already halted", state_before)
            self.tick_counter += 1
            return

        self._deliver_input_events_for_current_tick()

        if state_before == MicroState.FETCH:
            event = self._tick_fetch()
        elif state_before == MicroState.DECODE:
            event = self._tick_decode()
        elif state_before == MicroState.EXECUTE:
            event = self._tick_execute()
        elif state_before == MicroState.MEM_WAIT:
            event = self._tick_mem_wait()
        elif state_before == MicroState.IRQ_CHECK:
            event = self._tick_irq_check()
        else:
            raise MachineError(f"unsupported micro-state: {state_before}")

        self._append_log(event, state_before)
        self.tick_counter += 1

    def _tick_fetch(self) -> str:
        self._check_program_address(self.pc)
        self.ir = self.program_memory[self.pc]
        old_pc = self.pc
        self.pc += 1
        self.micro_state = MicroState.DECODE
        return f"fetch @{old_pc:08X}"

    def _tick_decode(self) -> str:
        self._require_ir()
        self.micro_state = MicroState.EXECUTE
        return f"decode {self._ir_text()}"

    def _tick_execute(self) -> str:
        instruction = self._require_ir()
        op = instruction.opcode
        arg = instruction.arg
        self.executed_instructions += 1

        if op == Opcode.NOP:
            event = "execute nop"
        elif op == Opcode.LIT:
            self.push(arg)
            event = f"push {arg}"
        elif op == Opcode.DUP:
            if not self.data_stack:
                raise MachineError("data stack underflow")
            self.push(self.data_stack[-1])
            event = "dup"
        elif op == Opcode.DROP:
            self.pop()
            event = "drop"
        elif op == Opcode.SWAP:
            b = self.pop()
            a = self.pop()
            self.push(b)
            self.push(a)
            event = "swap"
        elif op == Opcode.OVER:
            if len(self.data_stack) < 2:
                raise MachineError("data stack underflow on OVER")
            self.push(self.data_stack[-2])
            event = "over"
        elif op in {Opcode.ADD, Opcode.SUB, Opcode.MUL, Opcode.DIV, Opcode.MOD}:
            event = self._execute_arithmetic(op)
        elif op in {Opcode.EQ, Opcode.LT, Opcode.GT}:
            event = self._execute_comparison(op)
        elif op == Opcode.LOAD:
            address = self.pop()
            access = self.data_memory.read(address)
            return self._handle_memory_access(access, kind="load")
        elif op == Opcode.STORE:
            address = self.pop()
            value = self.pop()
            access = self.data_memory.write(address, value)
            return self._handle_memory_access(access, kind="store")
        elif op == Opcode.JMP:
            self._check_program_address(arg)
            self.pc = arg
            event = f"jmp 0x{arg:08X}"
        elif op == Opcode.JZ:
            flag = self.pop()
            if flag == 0:
                self._check_program_address(arg)
                self.pc = arg
                event = f"jz taken 0x{arg:08X}"
            else:
                event = "jz not taken"
        elif op == Opcode.CALL:
            self._check_program_address(arg)
            self.push_return_address(self.pc)
            self.pc = arg
            event = f"call 0x{arg:08X}"
        elif op == Opcode.RET:
            self.pc = self._pop_return_address()
            event = f"ret 0x{self.pc:08X}"
        elif op == Opcode.EXECUTE:
            target = self.pop()
            self._check_program_address(target)
            self.push_return_address(self.pc)
            self.pc = target
            event = f"execute 0x{target:08X}"
        elif op == Opcode.EI:
            self.irq_enable = True
            event = "ei"
        elif op == Opcode.DI:
            self.irq_enable = False
            event = "di"
        elif op == Opcode.IRET:
            if not self.in_irq:
                raise MachineError("IRET outside interrupt handler")
            self.pc = self._pop_return_address()
            self.in_irq = False
            self.irq_enable = True
            event = f"iret 0x{self.pc:08X}"
        elif op == Opcode.HALT:
            self.halted = True
            self.micro_state = MicroState.HALTED
            return "halt"
        else:
            raise MachineError(f"unsupported opcode: {op}")

        self.micro_state = MicroState.IRQ_CHECK
        return event

    def _handle_memory_access(
        self,
        access: tuple[int | None, int, str, str],
        *,
        kind: str,
    ) -> str:
        value, wait_ticks, event, final_event = access

        if wait_ticks == 0:
            if kind == "load":
                if value is None:
                    raise MachineError("LOAD memory access did not return a value")
                self.push(value)
            self.micro_state = MicroState.IRQ_CHECK
            return event

        self.pending_memory_kind = kind
        self.pending_memory_value = value
        self.pending_memory_final_event = final_event
        self.memory_wait_ticks = wait_ticks
        self.micro_state = MicroState.MEM_WAIT
        return event

    def _tick_mem_wait(self) -> str:
        if self.pending_memory_kind is None:
            raise MachineError("MEM_WAIT without pending memory access")
        if self.memory_wait_ticks <= 0:
            raise MachineError("invalid memory wait counter")

        self.memory_wait_ticks -= 1
        if self.memory_wait_ticks > 0:
            return f"cache_wait remaining={self.memory_wait_ticks}"

        kind = self.pending_memory_kind
        value = self.pending_memory_value
        final_event = self.pending_memory_final_event

        self.pending_memory_kind = None
        self.pending_memory_value = None
        self.pending_memory_final_event = ""

        if kind == "load":
            if value is None:
                raise MachineError("pending LOAD has no value")
            self.push(value)

        self.micro_state = MicroState.IRQ_CHECK
        return final_event

    def _tick_irq_check(self) -> str:
        if self.irq_enable and self.data_memory.irq_pending and not self.in_irq:
            self.push_return_address(self.pc)
            self.pc = IRQ_VECTOR
            self.irq_enable = False
            self.in_irq = True
            self.micro_state = MicroState.FETCH
            return "enter irq"

        self.micro_state = MicroState.FETCH
        return "no irq"

    def _execute_arithmetic(self, opcode: Opcode) -> str:
        b = self.pop()
        a = self.pop()

        if opcode == Opcode.ADD:
            result = a + b
            name = "add"
        elif opcode == Opcode.SUB:
            result = a - b
            name = "sub"
        elif opcode == Opcode.MUL:
            result = a * b
            name = "mul"
        elif opcode == Opcode.DIV:
            if b == 0:
                raise MachineError("division by zero")
            result = int(a / b)
            name = "div"
        elif opcode == Opcode.MOD:
            if b == 0:
                raise MachineError("modulo by zero")
            result = a % b
            name = "mod"
        else:
            raise MachineError(f"not an arithmetic opcode: {opcode}")

        result = to_signed32(result)
        self.push(result)
        return f"{name} {a} {b} -> {result}"

    def _execute_comparison(self, opcode: Opcode) -> str:
        b = self.pop()
        a = self.pop()

        if opcode == Opcode.EQ:
            result = int(a == b)
            name = "eq"
        elif opcode == Opcode.LT:
            result = int(a < b)
            name = "lt"
        elif opcode == Opcode.GT:
            result = int(a > b)
            name = "gt"
        else:
            raise MachineError(f"not a comparison opcode: {opcode}")

        self.push(result)
        return f"{name} {a} {b} -> {result}"

    def push(self, value: int) -> None:
        if len(self.data_stack) >= self.stack_limit:
            raise MachineError("data stack overflow")
        self.data_stack.append(to_signed32(value))

    def pop(self) -> int:
        if not self.data_stack:
            raise MachineError("data stack underflow")
        return self.data_stack.pop()

    def push_return_address(self, address: int) -> None:
        if len(self.return_stack) >= self.stack_limit:
            raise MachineError("return stack overflow")
        self._check_program_address(address)
        self.return_stack.append(address)

    def _pop_return_address(self) -> int:
        if not self.return_stack:
            raise MachineError("return stack underflow")
        address = self.return_stack.pop()
        self._check_program_address(address)
        return address

    def _deliver_input_events_for_current_tick(self) -> None:
        while self._next_input_event_index < len(self.input_events):
            event = self.input_events[self._next_input_event_index]
            if event.tick != self.tick_counter:
                break
            self.data_memory.push_input_char(event.char_code)
            self._next_input_event_index += 1

    def _require_ir(self) -> Instruction:
        if self.ir is None:
            raise MachineError("IR is empty")
        return self.ir

    def _check_program_address(self, address: int) -> None:
        if not 0 <= address < len(self.program_memory):
            raise MachineError(f"program address out of range: 0x{address:X}")

    def _ir_text(self) -> str:
        if self.ir is None:
            return "<none>"
        if self.ir.opcode in {Opcode.LIT, Opcode.JMP, Opcode.JZ, Opcode.CALL}:
            return f"{self.ir.opcode.name.lower()} {self.ir.arg}"
        return self.ir.opcode.name.lower()

    def cache_summary(self) -> str:
        cache_mode = "on" if self.data_memory.cache is not None else "off"
        return f"cache={cache_mode} hits={self.data_memory.cache_hits} misses={self.data_memory.cache_misses} uncached_reads={self.data_memory.uncached_reads} uncached_writes={self.data_memory.uncached_writes} input_overruns={self.data_memory.input_overrun_count}"

    def _append_log(self, event: str, state: MicroState) -> None:
        mode = "irq" if self.in_irq else "user"
        instr = self._ir_text()
        tos = str(self.data_stack[-1]) if self.data_stack else "-"
        nos = str(self.data_stack[-2]) if len(self.data_stack) > 1 else "-"
        stack = "[" + ",".join(str(value) for value in self.data_stack[-6:]) + "]"
        rstack = "[" + ",".join(f"0x{value:08X}" for value in self.return_stack[-6:]) + "]"
        self.log_lines.append(
            f"DEBUG   machine:simulation    "
            f"TICK: {self.tick_counter:5} "
            f"PC: {self.pc:5} "
            f"STATE: {state.value:<9} "
            f"MODE: {mode:<4} "
            f"TOS:{tos:>8} "
            f"NOS:{nos:>8} "
            f"DS_DEPTH:{len(self.data_stack):3} "
            f"RS_DEPTH:{len(self.return_stack):3} "
            f"DS: {stack:<16} "
            f"RS: {rstack:<16} "
            f"IE:{int(self.irq_enable)} "
            f"IP:{int(self.data_memory.irq_pending)} "
            f"CACHE:{self.data_memory.cache_hits}/{self.data_memory.cache_misses}	"
            f"{instr} [{event}]"
        )


# Вспомогательные функции
def to_word(value: int) -> int:
    """Преобразовать int в 32-битное машинное слово."""
    return value & WORD_MASK


def to_signed32(value: int) -> int:
    """Преобразовать значение в знаковое 32-битное число."""
    value &= WORD_MASK
    if value & WORD_SIGN_BIT:
        return value - (1 << WORD_BITS)
    return value


# Расписание входных событий
def parse_char_token(token: str) -> int:
    """Разобрать символ из файла входных событий."""
    escapes = {"\\n": "\n", "\\r": "\r", "\\t": "\t", "\\0": "\0", "space": " "}
    value = escapes.get(token, token)
    if len(value) != 1:
        raise MachineError(f"input event value must be one character: {token!r}")
    return ord(value)


def read_input_schedule(path: str | Path | None) -> list[InputEvent]:
    if path is None:
        return []

    events: list[InputEvent] = []
    for line_number, raw_line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise MachineError(f"invalid input schedule line {line_number}: {raw_line!r}")

        tick_text, char_text = parts
        try:
            tick = int(tick_text)
        except ValueError as exc:
            raise MachineError(f"invalid tick on line {line_number}: {tick_text!r}") from exc

        if tick < 0:
            raise MachineError(f"negative tick on line {line_number}: {tick}")

        events.append(InputEvent(tick=tick, char_code=parse_char_token(char_text)))

    events.sort(key=lambda event: event.tick)
    return events


# Консольный интерфейс
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Запустить модель процессора MiniForth")
    parser.add_argument("program", type=Path, help="program.bin от транслятора")
    parser.add_argument("data", type=Path, help="data.bin от транслятора")
    parser.add_argument("input", type=Path, nargs="?", default=None, help="файл расписания trap-ввода")
    parser.add_argument("--limit", type=int, default=DEFAULT_TICK_LIMIT, help="лимит тактов")
    parser.add_argument("--log", type=Path, default=None, help="записать журнал процессора в файл")
    parser.add_argument("--output", type=Path, default=None, help="записать вывод процессора в файл")
    parser.add_argument(
        "--cache", action=argparse.BooleanOptionalAction, default=True, help="включить или выключить cache данных"
    )
    parser.add_argument("--cache-lines", type=int, default=DEFAULT_CACHE_LINES, help="число строк cache")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    machine = Machine.from_files(
        args.program,
        args.data,
        args.input,
        cache_enabled=args.cache,
        cache_line_count=args.cache_lines,
    )
    output = machine.run(limit=args.limit)

    if args.log is not None:
        log_text = "\n".join(machine.log_lines)
        summary = (
            f"\nsummary: ticks={machine.tick_counter} "
            f"instructions={machine.executed_instructions} "
            f"{machine.cache_summary()}\n"
        )
        args.log.write_text(log_text + summary, encoding="utf-8")

    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")

    print(
        f"summary: ticks={machine.tick_counter} instructions={machine.executed_instructions} {machine.cache_summary()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
