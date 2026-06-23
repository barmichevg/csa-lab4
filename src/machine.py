from __future__ import annotations

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from isa import (
    IRQ_VECTOR,
    MMIO_IN_DATA,
    MMIO_IN_STATUS,
    MMIO_IRQ_ACK,
    MMIO_OUT_DATA,
    MMIO_READ_ONLY,
    MNEMONICS,
    RESET_VECTOR,
    WORD_BITS,
    WORD_MASK,
    WORD_SIGN_BIT,
    Instruction,
    Opcode,
    decode_instruction,
    read_data_binary,
    read_program_words,
)

DATA_ADDRESS_BITS = 16
DEFAULT_DATA_MEMORY_SIZE = 1 << DATA_ADDRESS_BITS
DEFAULT_STACK_LIMIT = 1024
DEFAULT_TICK_LIMIT = 300_000

DEFAULT_CACHE_LINES = 8
MEMORY_ACCESS_TICKS = 10

IN_STATUS_READY = 0b0001
IN_STATUS_OVERRUN = 0b0010


class MachineError(RuntimeError):
    """Ошибка состояния процессора или программы."""


class ControlState(Enum):
    """Состояния Control Unit."""

    FETCH = "fetch"
    DECODE = "decode"
    EXECUTE = "execute"
    MEM_WAIT = "mem_wait"
    IRQ_CHECK = "irq_check"
    HALTED = "halted"


class StopReason(Enum):
    """Причина штатной остановки цикла моделирования."""

    HALTED = "halted"
    PAUSED = "paused"


class MemoryPortOwner(Enum):
    """Владелец однопортовой нижней памяти на текущем такте."""

    NONE = "none"
    WRITE_REGISTER = "write_register"
    CPU = "cpu"


class MemoryCompletion(Enum):
    """Действие после завершения ожидания памяти."""

    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    CACHE_READ_MISS = "cache_read_miss"
    CACHE_WRITE_MISS = "cache_write_miss"
    CACHE_HIT_WRITE_REGISTER = "cache_hit_write_register"


STACK_OPS = {Opcode.NOP, Opcode.LIT, Opcode.DUP, Opcode.DROP, Opcode.SWAP, Opcode.OVER}
ALU_OPS = {Opcode.ADD, Opcode.SUB, Opcode.MUL, Opcode.DIV, Opcode.MOD, Opcode.EQ, Opcode.LT, Opcode.GT}
MEMORY_OPS = {Opcode.LOAD, Opcode.STORE}
FLOW_OPS = {Opcode.JMP, Opcode.JZ, Opcode.CALL, Opcode.RET, Opcode.EXECUTE}
IRQ_OPS = {Opcode.EI, Opcode.DI, Opcode.IRET}


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Короткая ссылка машинного адреса на исходную строку Forth."""

    source_name: str
    line: int
    column: int
    source_text: str


@dataclass(frozen=True, slots=True)
class InputEvent:
    """Одно событие trap-ввода: один байт."""

    tick: int
    token_code: int


@dataclass(frozen=True, slots=True)
class MemoryAccess:
    """Запрос, который завершается сразу либо через состояние MEM_WAIT."""

    kind: str
    wait_ticks: int
    event: str
    value: int | None = None
    completion: MemoryCompletion | None = None
    address: int | None = None
    write_value: int | None = None

    @property
    def uses_lower_memory(self) -> bool:
        return self.completion in {
            MemoryCompletion.MEMORY_READ,
            MemoryCompletion.MEMORY_WRITE,
            MemoryCompletion.CACHE_READ_MISS,
            MemoryCompletion.CACHE_WRITE_MISS,
        }

    @property
    def waits_for_write_register(self) -> bool:
        return self.completion == MemoryCompletion.CACHE_HIT_WRITE_REGISTER


@dataclass(slots=True)
class WriteRegister:
    """Один аппаратный регистр write-through записи, не FIFO."""

    address: int | None = None
    value: int = 0
    remaining_ticks: int = 0
    enqueued: int = 0
    stalls: int = 0
    drained: int = 0

    @property
    def busy(self) -> bool:
        return self.address is not None

    def enqueue(self, address: int, value: int) -> None:
        if self.busy:
            raise MachineError("write register is busy")
        self.address = address
        self.value = to_word(value)
        self.remaining_ticks = MEMORY_ACCESS_TICKS
        self.enqueued += 1

    def tick(self, backing_words: list[int]) -> str | None:
        if self.address is None:
            return None

        self.remaining_ticks -= 1
        if self.remaining_ticks > 0:
            return None

        address = self.address
        value = self.value
        backing_words[address] = value
        self.address = None
        self.remaining_ticks = 0
        self.drained += 1
        return f"wr_commit {to_signed32(value)} -> [0x{address:04X}]"


@dataclass(slots=True)
class MemoryPortArbiter:
    """Арбитр единственного порта нижней памяти данных."""

    last_owner: MemoryPortOwner = MemoryPortOwner.NONE

    def choose(self, *, cpu_requests: bool, write_register_busy: bool) -> MemoryPortOwner:
        if write_register_busy:
            self.last_owner = MemoryPortOwner.WRITE_REGISTER
        elif cpu_requests:
            self.last_owner = MemoryPortOwner.CPU
        else:
            self.last_owner = MemoryPortOwner.NONE
        return self.last_owner


@dataclass(slots=True)
class CacheLine:
    valid: bool = False
    tag: int = 0
    value: int = 0


@dataclass(slots=True)
class DataCache:
    """Прямо отображаемый кэш, одна 32-битная ячейка на строку."""

    line_count: int = DEFAULT_CACHE_LINES
    lines: list[CacheLine] = field(init=False)
    hits: int = 0
    misses: int = 0

    def __post_init__(self) -> None:
        if self.line_count <= 0 or self.line_count & (self.line_count - 1):
            raise MachineError("cache line count must be a positive power of two")
        self.lines = [CacheLine() for _ in range(self.line_count)]

    def split_address(self, address: int) -> tuple[int, int]:
        index = address & (self.line_count - 1)
        tag = address >> (self.line_count.bit_length() - 1)
        return index, tag

    def read(self, address: int) -> tuple[int | None, bool]:
        index, tag = self.split_address(address)
        line = self.lines[index]

        if line.valid and line.tag == tag:
            self.hits += 1
            return to_signed32(line.value), True

        self.misses += 1
        return None, False

    def probe_write(self, address: int) -> bool:
        index, tag = self.split_address(address)
        line = self.lines[index]

        hit = line.valid and line.tag == tag
        if hit:
            self.hits += 1
        else:
            self.misses += 1
        return hit

    def update_hit(self, address: int, value: int) -> None:
        index, tag = self.split_address(address)
        line = self.lines[index]
        if not line.valid or line.tag != tag:
            raise MachineError("cache line changed before store commit")
        line.value = to_word(value)

    def fill(self, address: int, value: int) -> None:
        index, tag = self.split_address(address)
        line = self.lines[index]
        line.valid = True
        line.tag = tag
        line.value = to_word(value)


@dataclass(frozen=True, slots=True)
class MemoryPortActivity:
    owner: MemoryPortOwner
    event: str | None = None


@dataclass(slots=True)
class DataMemory:
    """16-битное адресное пространство данных, MMIO, cache и write register."""

    words: list[int]
    cache: DataCache | None
    write_register: WriteRegister | None
    output_buffer: list[str] = field(default_factory=list)
    input_data: int = 0
    input_status: int = 0
    irq_pending: bool = False
    input_overrun_count: int = 0
    memory_port_arbiter: MemoryPortArbiter = field(default_factory=MemoryPortArbiter)
    uncached_reads: int = 0
    uncached_writes: int = 0

    @classmethod
    def from_image(
        cls,
        image: Iterable[int],
        *,
        cache_enabled: bool = True,
        cache_line_count: int = DEFAULT_CACHE_LINES,
    ) -> DataMemory:
        words = [to_word(value) for value in image]
        if len(words) > MMIO_IN_DATA:
            raise MachineError(
                f"data image occupies {len(words)} words and overlaps MMIO starting at 0x{MMIO_IN_DATA:04X}"
            )
        words.extend([0] * (DEFAULT_DATA_MEMORY_SIZE - len(words)))
        return cls(
            words=words,
            cache=DataCache(cache_line_count) if cache_enabled else None,
            write_register=WriteRegister() if cache_enabled else None,
        )

    def read(self, address: int) -> MemoryAccess:
        if address == MMIO_IN_DATA:
            value = self.input_data
            return MemoryAccess(
                kind="load",
                wait_ticks=0,
                event=f"mmio_read [0x{address:04X}] -> {value}",
                value=value,
            )
        if address == MMIO_IN_STATUS:
            return MemoryAccess(
                kind="load",
                wait_ticks=0,
                event=f"mmio_read [0x{address:04X}] -> {self.input_status}",
                value=self.input_status,
            )
        if address in {MMIO_OUT_DATA, MMIO_IRQ_ACK}:
            raise MachineError(f"attempt to read write-only MMIO register 0x{address:04X}")

        self._check_regular_address(address)

        if self.cache is None:
            self.uncached_reads += 1
            return MemoryAccess(
                kind="load",
                wait_ticks=MEMORY_ACCESS_TICKS,
                event=f"memory_read [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
                completion=MemoryCompletion.MEMORY_READ,
                address=address,
            )

        cache_value, hit = self.cache.read(address)
        if hit:
            if cache_value is None:
                raise MachineError("cache hit did not return a value")
            return MemoryAccess(
                kind="load",
                wait_ticks=0,
                event=f"cache_hit read [0x{address:04X}] -> {cache_value}",
                value=cache_value,
            )

        return MemoryAccess(
            kind="load",
            wait_ticks=MEMORY_ACCESS_TICKS,
            event=f"cache_miss read [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
            completion=MemoryCompletion.CACHE_READ_MISS,
            address=address,
        )

    def write(self, address: int, value: int) -> MemoryAccess:
        if address == MMIO_OUT_DATA:
            self.output_buffer.append(chr(value & 0xFF))
            return MemoryAccess(kind="store", wait_ticks=0, event=f"mmio_write {value} -> [0x{address:04X}]")

        if address == MMIO_IRQ_ACK:
            if value & IN_STATUS_READY:
                self.input_status &= ~IN_STATUS_READY
                self.irq_pending = False
            if value & IN_STATUS_OVERRUN:
                self.input_status &= ~IN_STATUS_OVERRUN
            return MemoryAccess(kind="store", wait_ticks=0, event=f"mmio_write {value} -> [0x{address:04X}]")

        if address in MMIO_READ_ONLY:
            raise MachineError(f"attempt to write read-only MMIO register 0x{address:04X}")

        self._check_regular_address(address)

        if self.cache is None:
            self.uncached_writes += 1
            return MemoryAccess(
                kind="store",
                wait_ticks=MEMORY_ACCESS_TICKS,
                event=f"memory_write {value} -> [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
                completion=MemoryCompletion.MEMORY_WRITE,
                address=address,
                write_value=value,
            )

        hit = self.cache.probe_write(address)
        if hit:
            if self.write_register is None:
                raise MachineError("cache enabled without write register")

            if not self.write_register.busy:
                self.cache.update_hit(address, value)
                self.write_register.enqueue(address, value)
                return MemoryAccess(
                    kind="store",
                    wait_ticks=0,
                    event=f"cache_hit write {value} -> [0x{address:04X}]; wr_load",
                )

            self.write_register.stalls += 1
            return MemoryAccess(
                kind="store",
                wait_ticks=0,
                event=f"cache_hit write {value} -> [0x{address:04X}]; wr_busy",
                completion=MemoryCompletion.CACHE_HIT_WRITE_REGISTER,
                address=address,
                write_value=value,
            )

        return MemoryAccess(
            kind="store",
            wait_ticks=MEMORY_ACCESS_TICKS,
            event=f"cache_miss write {value} -> [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
            completion=MemoryCompletion.CACHE_WRITE_MISS,
            address=address,
            write_value=value,
        )

    def complete_access(self, access: MemoryAccess) -> tuple[int | None, str]:
        if access.completion is None:
            return access.value, access.event
        if access.address is None:
            raise MachineError("pending memory access has no address")

        address = access.address
        match access.completion:
            case MemoryCompletion.MEMORY_READ:
                value = to_signed32(self.words[address])
                return value, f"memory_read_done [0x{address:04X}] -> {value}"

            case MemoryCompletion.MEMORY_WRITE:
                if access.write_value is None:
                    raise MachineError("pending memory write has no value")
                self.words[address] = to_word(access.write_value)
                return None, f"memory_write_done {access.write_value} -> [0x{address:04X}]"

            case MemoryCompletion.CACHE_READ_MISS:
                if self.cache is None:
                    raise MachineError("cache read miss without cache")
                value = self.words[address]
                self.cache.fill(address, value)
                signed_value = to_signed32(value)
                return signed_value, f"cache_fill_done read [0x{address:04X}] -> {signed_value}"

            case MemoryCompletion.CACHE_WRITE_MISS:
                if self.cache is None:
                    raise MachineError("cache write miss without cache")
                if access.write_value is None:
                    raise MachineError("pending cache write has no value")
                self.words[address] = to_word(access.write_value)
                self.cache.fill(address, access.write_value)
                return None, f"cache_fill_done write {access.write_value} -> [0x{address:04X}]"

            case MemoryCompletion.CACHE_HIT_WRITE_REGISTER:
                if self.cache is None or self.write_register is None:
                    raise MachineError("cache-hit store without cache/write register")
                if access.write_value is None:
                    raise MachineError("pending buffered store has no value")
                if self.write_register.busy:
                    raise MachineError("write register still busy after wait")
                self.cache.update_hit(address, access.write_value)
                self.write_register.enqueue(address, access.write_value)
                return None, f"cache_hit_commit {access.write_value} -> [0x{address:04X}]; wr_load"

        raise MachineError(f"unsupported memory completion: {access.completion}")

    def tick_memory_port(self, *, cpu_requests: bool) -> MemoryPortActivity:
        write_register_busy = self.write_register is not None and self.write_register.busy
        owner = self.memory_port_arbiter.choose(
            cpu_requests=cpu_requests,
            write_register_busy=write_register_busy,
        )
        if owner == MemoryPortOwner.WRITE_REGISTER:
            if self.write_register is None:
                raise MachineError("arbiter selected absent write register")
            return MemoryPortActivity(owner, self.write_register.tick(self.words))
        return MemoryPortActivity(owner)

    @property
    def write_register_busy(self) -> bool:
        return self.write_register is not None and self.write_register.busy

    def push_input_token(self, token_code: int) -> bool:
        """Передать байт во входной регистр устройства."""
        if not 0 <= token_code <= 0xFF:
            raise MachineError(f"input token out of range: {token_code}")
        if self.input_status & IN_STATUS_READY:
            self.input_status |= IN_STATUS_OVERRUN
            self.input_overrun_count += 1
            return False

        self.input_data = token_code
        self.input_status |= IN_STATUS_READY
        self.irq_pending = True
        return True

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
        if not 0 <= address < MMIO_IN_DATA:
            raise MachineError(f"regular data memory address out of range: 0x{address:X}")


@dataclass(slots=True)
class Machine:
    """Потактовая модель стекового процессора."""

    program_memory: list[int]
    data_memory: DataMemory
    input_events: list[InputEvent] = field(default_factory=list)
    source_map: dict[int, SourceLocation] = field(default_factory=dict)

    pc: int = RESET_VECTOR
    ir_word: int | None = None
    ir_address: int | None = None
    decoded_instruction: Instruction | None = None
    control_state: ControlState = ControlState.FETCH
    tick_counter: int = 0

    data_stack: list[int] = field(default_factory=list)
    return_stack: list[int] = field(default_factory=list)

    irq_enable: bool = False
    in_irq: bool = False
    stop_reason: StopReason | None = None

    memory_wait_ticks: int = 0
    pending_memory_access: MemoryAccess | None = None

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
        source_map_path: str | Path | None = None,
        cache_enabled: bool = True,
        cache_line_count: int = DEFAULT_CACHE_LINES,
    ) -> Machine:
        program_file = Path(program_path)
        program_words = read_program_words(program_file)
        data_image = read_data_binary(data_path)
        input_events = read_input_schedule(input_path) if input_path is not None else []

        resolved_map_path: Path | None
        if source_map_path is not None:
            resolved_map_path = Path(source_map_path)
        else:
            candidate = program_file.with_suffix(program_file.suffix + ".map.json")
            resolved_map_path = candidate if candidate.exists() else None

        source_map = read_source_map(resolved_map_path) if resolved_map_path is not None else {}
        return cls(
            program_memory=program_words,
            data_memory=DataMemory.from_image(
                data_image,
                cache_enabled=cache_enabled,
                cache_line_count=cache_line_count,
            ),
            input_events=input_events,
            source_map=source_map,
        )

    def run(
        self,
        *,
        limit: int = DEFAULT_TICK_LIMIT,
        stop_at_tick: int | None = None,
    ) -> str:
        if limit <= 0:
            raise MachineError("hard tick limit must be positive")
        if stop_at_tick is not None and stop_at_tick < self.tick_counter:
            raise MachineError("stop-at-tick is before the current machine tick")

        self.stop_reason = None
        while self.control_state != ControlState.HALTED or self.data_memory.write_register_busy:
            if stop_at_tick is not None and self.tick_counter >= stop_at_tick:
                self.stop_reason = StopReason.PAUSED
                return self.data_memory.output
            if self.tick_counter >= limit:
                raise MachineError(f"hard tick limit exceeded: {limit}")
            self.step_tick()

        self.stop_reason = StopReason.HALTED
        return self.data_memory.output

    def step_tick(self) -> None:
        state_before = self.control_state
        access = self.pending_memory_access
        cpu_requests_port = state_before == ControlState.MEM_WAIT and access is not None and access.uses_lower_memory
        port_activity = self.data_memory.tick_memory_port(cpu_requests=cpu_requests_port)

        if state_before == ControlState.HALTED:
            event = "halted; drain_wr" if self.data_memory.write_register_busy else "halted"
            if port_activity.event is not None:
                event += f"; {port_activity.event}"
            self._append_log(event, state_before)
            self.tick_counter += 1
            return

        self._deliver_input_events_for_current_tick()

        match state_before:
            case ControlState.FETCH:
                event = self._tick_fetch()
            case ControlState.DECODE:
                event = self._tick_decode()
            case ControlState.EXECUTE:
                event = self._tick_execute()
            case ControlState.MEM_WAIT:
                event = self._tick_mem_wait(port_activity.owner)
            case ControlState.IRQ_CHECK:
                event = self._tick_irq_check()
            case _:
                raise MachineError(f"unsupported control state: {state_before}")

        if port_activity.event is not None:
            event += f"; {port_activity.event}"
        self._append_log(event, state_before)
        self.tick_counter += 1

    def _tick_fetch(self) -> str:
        self._check_program_address(self.pc)
        self.ir_address = self.pc
        self.ir_word = self.program_memory[self.pc]
        self.decoded_instruction = None
        old_pc = self.pc
        self.pc += 1
        self.control_state = ControlState.DECODE
        return f"fetch raw=0x{self.ir_word:08X} @{old_pc:08X}"

    def _tick_decode(self) -> str:
        if self.ir_word is None:
            raise MachineError("IR is empty")
        self.decoded_instruction = decode_instruction(self.ir_word)
        self.control_state = ControlState.EXECUTE
        return f"decode {self._ir_text()}"

    def _tick_execute(self) -> str:
        instruction = self._require_decoded_instruction()
        opcode = instruction.opcode

        if opcode in STACK_OPS:
            event = self._execute_stack(opcode, instruction.arg)
        elif opcode in ALU_OPS:
            event = self._execute_alu(opcode)
        elif opcode in MEMORY_OPS:
            return self._execute_memory(opcode)
        elif opcode in FLOW_OPS:
            event = self._execute_flow(opcode, instruction.arg)
        elif opcode in IRQ_OPS:
            event = self._execute_irq(opcode)
        elif opcode == Opcode.HALT:
            self.executed_instructions += 1
            self.control_state = ControlState.HALTED
            return "halt"
        else:
            raise MachineError(f"unsupported opcode: {opcode}")

        return self._finish_instruction(event)

    def _execute_stack(self, opcode: Opcode, arg: int) -> str:
        match opcode:
            case Opcode.NOP:
                return "execute nop"
            case Opcode.LIT:
                self.push(arg)
                return f"push {arg}"
            case Opcode.DUP:
                self.push(self.peek())
                return "dup"
            case Opcode.DROP:
                self.pop()
                return "drop"
            case Opcode.SWAP:
                right = self.pop()
                left = self.pop()
                self.push(right)
                self.push(left)
                return "swap"
            case Opcode.OVER:
                self.push(self.peek(1))
                return "over"
            case _:
                raise MachineError(f"not a stack opcode: {opcode}")

    def _execute_alu(self, opcode: Opcode) -> str:
        right = self.pop()
        left = self.pop()

        match opcode:
            case Opcode.ADD:
                result = left + right
            case Opcode.SUB:
                result = left - right
            case Opcode.MUL:
                result = left * right
            case Opcode.DIV:
                result, _ = trunc_divmod(left, right)
            case Opcode.MOD:
                _, result = trunc_divmod(left, right)
            case Opcode.EQ:
                result = int(left == right)
            case Opcode.LT:
                result = int(left < right)
            case Opcode.GT:
                result = int(left > right)
            case _:
                raise MachineError(f"not an ALU opcode: {opcode}")

        result = to_signed32(result)
        self.push(result)
        return f"{MNEMONICS[opcode]} {left} {right} -> {result}"

    def _execute_memory(self, opcode: Opcode) -> str:
        if opcode == Opcode.LOAD:
            address = self.pop()
            return self._handle_memory_access(self.data_memory.read(address), kind="load")

        address = self.pop()
        value = self.pop()
        return self._handle_memory_access(self.data_memory.write(address, value), kind="store")

    def _execute_flow(self, opcode: Opcode, arg: int) -> str:
        match opcode:
            case Opcode.JMP:
                self._check_program_address(arg)
                self.pc = arg
                return f"jmp 0x{arg:08X}"
            case Opcode.JZ:
                if self.pop() == 0:
                    self._check_program_address(arg)
                    self.pc = arg
                    return f"jz taken 0x{arg:08X}"
                return "jz not taken"
            case Opcode.CALL:
                self._check_program_address(arg)
                self.push_return_address(self.pc)
                self.pc = arg
                return f"call 0x{arg:08X}"
            case Opcode.RET:
                self.pc = self._pop_return_address()
                return f"ret 0x{self.pc:08X}"
            case Opcode.EXECUTE:
                target = self.pop()
                self._check_program_address(target)
                self.push_return_address(self.pc)
                self.pc = target
                return f"execute 0x{target:08X}"
            case _:
                raise MachineError(f"not a control-flow opcode: {opcode}")

    def _execute_irq(self, opcode: Opcode) -> str:
        match opcode:
            case Opcode.EI:
                self.irq_enable = True
                return "ei"
            case Opcode.DI:
                self.irq_enable = False
                return "di"
            case Opcode.IRET:
                if not self.in_irq:
                    raise MachineError("IRET outside interrupt handler")
                self.pc = self._pop_return_address()
                self.in_irq = False
                self.irq_enable = True
                return f"iret 0x{self.pc:08X}"
            case _:
                raise MachineError(f"not an interrupt-control opcode: {opcode}")

    def _finish_instruction(self, event: str) -> str:
        self.executed_instructions += 1
        self.control_state = ControlState.IRQ_CHECK
        return event

    def _handle_memory_access(self, access: MemoryAccess, *, kind: str) -> str:
        if access.kind != kind:
            raise MachineError(f"unexpected memory access kind: {access.kind}, expected {kind}")

        if access.wait_ticks == 0 and not access.waits_for_write_register:
            if kind == "load":
                if access.value is None:
                    raise MachineError("LOAD memory access did not return a value")
                self.push(access.value)
            return self._finish_instruction(access.event)

        self.pending_memory_access = access
        self.memory_wait_ticks = access.wait_ticks
        self.control_state = ControlState.MEM_WAIT
        return access.event

    def _tick_mem_wait(self, port_owner: MemoryPortOwner) -> str:
        access = self.pending_memory_access
        if access is None:
            raise MachineError("MEM_WAIT without pending memory access")

        if access.waits_for_write_register:
            if self.data_memory.write_register_busy:
                return "mem_wait; wr_busy"
            value, final_event = self.data_memory.complete_access(access)
            if value is not None:
                raise MachineError("STORE completion unexpectedly returned a value")
            self.pending_memory_access = None
            self.memory_wait_ticks = 0
            return self._finish_instruction(final_event)

        if not access.uses_lower_memory:
            raise MachineError("unsupported pending memory wait")
        if self.memory_wait_ticks <= 0:
            raise MachineError("invalid lower-memory wait counter")
        if port_owner != MemoryPortOwner.CPU:
            owner = "wr" if port_owner == MemoryPortOwner.WRITE_REGISTER else "idle"
            return f"mem_wait; {owner}; wait={self.memory_wait_ticks}"

        self.memory_wait_ticks -= 1
        if self.memory_wait_ticks > 0:
            return f"mem_wait; wait={self.memory_wait_ticks}"

        value, final_event = self.data_memory.complete_access(access)
        self.pending_memory_access = None
        if access.kind == "load":
            if value is None:
                raise MachineError("pending LOAD has no value")
            self.push(value)

        return self._finish_instruction(final_event)

    def _tick_irq_check(self) -> str:
        if self.irq_enable and self.data_memory.irq_pending and not self.in_irq:
            self.push_return_address(self.pc)
            self.pc = IRQ_VECTOR
            self.irq_enable = False
            self.in_irq = True
            self.control_state = ControlState.FETCH
            return "enter irq"

        self.control_state = ControlState.FETCH
        return "no irq"

    def push(self, value: int) -> None:
        if len(self.data_stack) >= DEFAULT_STACK_LIMIT:
            raise MachineError("data stack overflow")
        self.data_stack.append(to_signed32(value))

    def pop(self) -> int:
        if not self.data_stack:
            raise MachineError("data stack underflow")
        return self.data_stack.pop()

    def peek(self, depth: int = 0) -> int:
        if depth < 0 or depth >= len(self.data_stack):
            raise MachineError("data stack underflow")
        return self.data_stack[-1 - depth]

    def push_return_address(self, address: int) -> None:
        if len(self.return_stack) >= DEFAULT_STACK_LIMIT:
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
            input_event = self.input_events[self._next_input_event_index]
            if input_event.tick > self.tick_counter:
                break

            accepted = self.data_memory.push_input_token(input_event.token_code)
            token_text = format_input_token(input_event.token_code)
            result = "accepted, IRQ pending set" if accepted else "OVERRUN, IN_STATUS.ready already set"

            self.log_lines.append(
                f"DEBUG   machine:input_event   "
                f"TICK: {self.tick_counter:5} "
                f"scheduled:{input_event.tick:5} "
                f"token:{token_text:<6} "
                f"code:{input_event.token_code:3} -> {result}"
            )
            self._next_input_event_index += 1

    def _require_decoded_instruction(self) -> Instruction:
        if self.decoded_instruction is None:
            raise MachineError("instruction has not been decoded")
        return self.decoded_instruction

    def _check_program_address(self, address: int) -> None:
        if not 0 <= address < len(self.program_memory):
            raise MachineError(f"program address out of range: 0x{address:X}")

    def _ir_text(self) -> str:
        instruction = self.decoded_instruction
        if instruction is None:
            return "<none>" if self.ir_word is None else f"raw 0x{self.ir_word:08X}"
        mnemonic = MNEMONICS[instruction.opcode]
        if instruction.opcode in {Opcode.LIT, Opcode.JMP, Opcode.JZ, Opcode.CALL}:
            return f"{mnemonic} {instruction.arg}"
        return mnemonic

    def _source_text(self) -> str:
        if self.ir_address is None:
            return "-"
        source = self.source_map.get(self.ir_address)
        if source is None:
            return "-"
        location = source.source_name
        if source.line > 0:
            location += f":{source.line}:{source.column}"
        text = " ".join(source.source_text.split())
        if len(text) > 56:
            text = text[:53] + "..."
        return f"{location} {text}".strip()

    def cache_summary(self) -> str:
        cache_mode = "on" if self.data_memory.cache is not None else "off"
        write_register = self.data_memory.write_register
        if write_register is None:
            wr_summary = "wr=off"
        elif not write_register.busy:
            wr_summary = (
                f"wr=empty wr_enqueued={write_register.enqueued} "
                f"wr_stalls={write_register.stalls} wr_drained={write_register.drained}"
            )
        else:
            wr_summary = (
                f"wr=busy@{write_register.remaining_ticks} wr_enqueued={write_register.enqueued} "
                f"wr_stalls={write_register.stalls} wr_drained={write_register.drained}"
            )
        return (
            f"cache={cache_mode} hits={self.data_memory.cache_hits} misses={self.data_memory.cache_misses} "
            f"uncached_reads={self.data_memory.uncached_reads} uncached_writes={self.data_memory.uncached_writes} "
            f"{wr_summary} input_overruns={self.data_memory.input_overrun_count}"
        )

    @staticmethod
    def _format_stack_column(values: list[int], *, width: int, hexadecimal: bool = False) -> str:
        rendered = [f"0x{value:08X}" if hexadecimal else str(value) for value in values[-6:]]
        text = "[" + ",".join(rendered) + "]"
        while rendered and len(text) > width:
            rendered.pop(0)
            text = "[...," + ",".join(rendered) + "]"
        if len(text) > width:
            text = text[: width - 4] + "...]"
        return text

    def _append_log(self, event: str, state: ControlState) -> None:
        mode = "irq" if self.in_irq else "user"
        instr = self._ir_text()
        stack = self._format_stack_column(self.data_stack, width=20)
        rstack = self._format_stack_column(self.return_stack, width=20, hexadecimal=True)
        write_register = self.data_memory.write_register
        if write_register is None:
            wr_state = "off"
        elif not write_register.busy:
            wr_state = "empty"
        else:
            wr_state = f"busy@{write_register.remaining_ticks}"
        irq_state = f"E{int(self.irq_enable)}/P{int(self.data_memory.irq_pending)}/S{self.data_memory.input_status:02X}"
        cache_state = f"{self.data_memory.cache_hits}/{self.data_memory.cache_misses}"
        self.log_lines.append(
            f"DEBUG   machine:simulation    "
            f"TICK: {self.tick_counter:5} "
            f"PC: {self.pc:5} "
            f"STATE: {state.value:<9} "
            f"MODE: {mode:<4} "
            f"SP:{len(self.data_stack):3} "
            f"DS: {stack:<20} "
            f"RS: {rstack:<20} "
            f"IRQ:{irq_state:<9} "
            f"CACHE:{cache_state:<11} "
            f"WR:{wr_state:<8} "
            f"{instr:<15} [{event}] SRC:{self._source_text()}"
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


def trunc_divmod(dividend: int, divisor: int) -> tuple[int, int]:
    """Деление: частное округляется к нулю, остаток согласован."""
    if divisor == 0:
        raise MachineError("division by zero")

    quotient = abs(dividend) // abs(divisor)
    if (dividend < 0) != (divisor < 0):
        quotient = -quotient
    remainder = dividend - quotient * divisor
    return quotient, remainder


# Отладочная карта
def read_source_map(path: str | Path) -> dict[int, SourceLocation]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MachineError("source map root must be an object")

    result: dict[int, SourceLocation] = {}
    for address_text, item in raw.items():
        if not isinstance(item, dict):
            raise MachineError(f"invalid source map entry at {address_text}")
        try:
            address = int(address_text)
            result[address] = SourceLocation(
                source_name=str(item["source_name"]),
                line=int(item["line"]),
                column=int(item["column"]),
                source_text=str(item["source_text"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MachineError(f"invalid source map entry at {address_text}") from exc
    return result


# Расписание входных событий
def format_input_token(token_code: int) -> str:
    """Представить входной байт в журнале."""
    char = chr(token_code)
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\0": "\\0", " ": "space"}
    text = escapes.get(char, char)
    if char == "'":
        text = "\\'"
    elif char == "\\":
        text = "\\\\"
    return f"'{text}'"


def parse_input_token(token: str) -> int:
    """Разобрать один байт из расписания."""
    escapes = {"\\n": "\n", "\\r": "\r", "\\t": "\t", "\\0": "\0", "space": " "}
    value = escapes.get(token, token)
    if len(value) != 1:
        raise MachineError(f"input event value must be one character: {token!r}")
    code = ord(value)
    if code > 0xFF:
        raise MachineError(f"input character does not fit into one byte: {token!r}")
    return code


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

        tick_text, token_text = parts
        try:
            tick = int(tick_text)
        except ValueError as exc:
            raise MachineError(f"invalid tick on line {line_number}: {tick_text!r}") from exc

        if tick < 0:
            raise MachineError(f"negative tick on line {line_number}: {tick}")

        events.append(InputEvent(tick=tick, token_code=parse_input_token(token_text)))

    events.sort(key=lambda event: event.tick)
    return events


# Консольный интерфейс
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Запустить модель процессора")
    parser.add_argument("program", type=Path, help="program.bin от транслятора")
    parser.add_argument("data", type=Path, help="data.bin от транслятора")
    parser.add_argument("input", type=Path, nargs="?", default=None, help="файл расписания trap-ввода")
    parser.add_argument("--limit", type=int, default=DEFAULT_TICK_LIMIT, help="защитный лимит тактов")
    parser.add_argument("--stop-at-tick", type=int, default=None, help="штатно приостановить модель после N тактов")
    parser.add_argument("--source-map", type=Path, default=None, help="карта машинных адресов на Forth-код")
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
        source_map_path=args.source_map,
        cache_enabled=args.cache,
        cache_line_count=args.cache_lines,
    )
    output = machine.run(limit=args.limit, stop_at_tick=args.stop_at_tick)
    status = machine.stop_reason.value if machine.stop_reason is not None else "unknown"

    if args.log is not None:
        log_text = "\n".join(machine.log_lines)
        summary = (
            f"\nsummary: status={status} ticks={machine.tick_counter} "
            f"instructions={machine.executed_instructions} "
            f"{machine.cache_summary()}\n"
        )
        args.log.write_text(log_text + summary, encoding="utf-8")

    if args.output is not None:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")

    print(
        f"summary: status={status} ticks={machine.tick_counter} "
        f"instructions={machine.executed_instructions} {machine.cache_summary()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
