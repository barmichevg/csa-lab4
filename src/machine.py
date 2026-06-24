from __future__ import annotations

import argparse
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from isa import (
    INPUT_STATUS_OVERRUN,
    INPUT_STATUS_READY,
    IRQ_VECTOR,
    MMIO_IN_DATA,
    MMIO_IN_STATUS,
    MMIO_IRQ_ACK,
    MMIO_OUT_DATA,
    MNEMONICS,
    RESET_VECTOR,
    WORD_BITS,
    WORD_MASK,
    WORD_SIGN_BIT,
    IsaError,
    Opcode,
    decode_instruction,
    read_data_binary,
    read_program_words,
)

REGULAR_MEMORY_SIZE = MMIO_IN_DATA
DEFAULT_STACK_LIMIT = 1024
DEFAULT_TICK_LIMIT = 300_000

DEFAULT_CACHE_LINES = 8
MEMORY_ACCESS_TICKS = 10


class MachineError(RuntimeError):
    """Ошибка состояния процессора или программы."""


class ControlState(Enum):
    """Состояния hardwired Control Unit."""

    FETCH = "fetch"
    EXECUTE = "execute"
    MEM_WAIT = "mem_wait"
    IRQ_CHECK = "irq_check"
    HALTED = "halted"


class MemoryCompletion(Enum):
    """Действие после завершения отложенного обращения."""

    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    CACHE_READ_MISS = "cache_read_miss"
    CACHE_WRITE_MISS = "cache_write_miss"
    CACHE_HIT_WRITE_REGISTER = "cache_hit_write_register"


class AddressRegion(Enum):
    """Результат явного декодирования 32-битного адреса из TOS."""

    REGULAR = "regular"
    MMIO_IN_DATA = "mmio_in_data"
    MMIO_IN_STATUS = "mmio_in_status"
    MMIO_OUT_DATA = "mmio_out_data"
    MMIO_IRQ_ACK = "mmio_irq_ack"
    INVALID = "invalid"


class OpcodeClass(Enum):
    """Класс уже выделенного из IR поля opcode[7:0]."""

    STACK = "stack"
    ALU = "alu"
    MEMORY = "memory"
    FLOW = "flow"
    IRQ = "irq"
    HALT = "halt"


@dataclass(frozen=True, slots=True)
class InputEvent:
    """Одно событие trap-ввода: один байт."""

    tick: int
    token_code: int


@dataclass(frozen=True, slots=True)
class MemoryResponse:
    """Ответ интерфейса памяти; value задан только для завершённого LOAD."""

    event: str
    done: bool = False
    value: int | None = None


@dataclass(slots=True)
class PendingAccess:
    """Регистры незавершённого обращения внутри Memory Controller."""

    completion: MemoryCompletion
    address: int
    write_value: int | None = None
    remaining_ticks: int = 0

    @property
    def waits_for_write_register(self) -> bool:
        return self.completion == MemoryCompletion.CACHE_HIT_WRITE_REGISTER


@dataclass(frozen=True, slots=True)
class MemoryControllerTick:
    """Результат одного такта Data Cache / Memory Controller."""

    event: str = ""
    response: MemoryResponse | None = None


@dataclass(slots=True)
class WriteRegister:
    """Один аппаратный регистр write-through записи, не FIFO."""

    address: int | None = None
    value: int = 0
    remaining_ticks: int = 0

    @property
    def busy(self) -> bool:
        return self.address is not None

    def load(self, address: int, value: int) -> None:
        if self.busy:
            raise MachineError("write register is busy")
        self.address = address
        self.value = to_word(value)
        self.remaining_ticks = MEMORY_ACCESS_TICKS

    def tick(self) -> tuple[int, int] | None:
        if self.address is None:
            return None

        self.remaining_ticks -= 1
        if self.remaining_ticks > 0:
            return None

        commit = (self.address, self.value)
        self.address = None
        self.remaining_ticks = 0
        return commit


@dataclass(slots=True)
class CacheLine:
    valid: bool = False
    tag: int = 0
    value: int = 0


@dataclass(slots=True)
class DataCache:
    """Прямо отображаемый cache на восемь 32-битных строк."""

    lines: list[CacheLine] = field(default_factory=lambda: [CacheLine() for _ in range(DEFAULT_CACHE_LINES)])
    hits: int = 0
    misses: int = 0

    def split_address(self, address: int) -> tuple[int, int]:
        index = address & (DEFAULT_CACHE_LINES - 1)
        tag = address >> (DEFAULT_CACHE_LINES.bit_length() - 1)
        return index, tag

    def lookup(self, address: int) -> CacheLine | None:
        index, tag = self.split_address(address)
        line = self.lines[index]
        if line.valid and line.tag == tag:
            self.hits += 1
            return line

        self.misses += 1
        return None

    def read(self, address: int) -> int | None:
        line = self.lookup(address)
        return None if line is None else to_signed32(line.value)

    def probe_write(self, address: int) -> bool:
        return self.lookup(address) is not None

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


@dataclass(slots=True)
class InputIrqController:
    """Однобайтовое устройство ввода: ready, overrun и irq_pending."""

    data: int = 0
    status: int = 0
    overrun_count: int = 0

    @property
    def irq_pending(self) -> bool:
        return bool(self.status & INPUT_STATUS_READY)

    def push_token(self, token_code: int) -> bool:
        if not 0 <= token_code <= 0xFF:
            raise MachineError(f"input token out of range: {token_code}")
        if self.status & INPUT_STATUS_READY:
            self.status |= INPUT_STATUS_OVERRUN
            self.overrun_count += 1
            return False

        self.data = token_code
        self.status |= INPUT_STATUS_READY
        return True

    def acknowledge(self, value: int) -> None:
        if value & INPUT_STATUS_READY:
            self.status &= ~INPUT_STATUS_READY
        if value & INPUT_STATUS_OVERRUN:
            self.status &= ~INPUT_STATUS_OVERRUN


def decode_address(address: int) -> AddressRegion:
    """Address Decoder / Access Router для 32-битного TOS."""
    if 0 <= address < MMIO_IN_DATA:
        return AddressRegion.REGULAR
    if address == MMIO_IN_DATA:
        return AddressRegion.MMIO_IN_DATA
    if address == MMIO_IN_STATUS:
        return AddressRegion.MMIO_IN_STATUS
    if address == MMIO_OUT_DATA:
        return AddressRegion.MMIO_OUT_DATA
    if address == MMIO_IRQ_ACK:
        return AddressRegion.MMIO_IRQ_ACK
    return AddressRegion.INVALID


def classify_opcode(opcode: Opcode) -> OpcodeClass:
    """Комбинационный Opcode Classifier по старшей тетраде opcode."""
    if opcode is Opcode.HALT:
        return OpcodeClass.HALT

    match int(opcode) >> 4:
        case 0x0:
            return OpcodeClass.STACK
        case 0x1 | 0x2:
            return OpcodeClass.ALU
        case 0x3:
            return OpcodeClass.MEMORY
        case 0x4:
            return OpcodeClass.FLOW
        case 0x5:
            return OpcodeClass.IRQ

    raise MachineError(f"unsupported opcode: {opcode}")


@dataclass(slots=True)
class MemorySubsystem:
    """Address Router, MMIO, cache/controller и regular data memory."""

    words: list[int]
    cache: DataCache | None
    write_register: WriteRegister | None
    input_controller: InputIrqController = field(default_factory=InputIrqController)
    output_buffer: list[str] = field(default_factory=list)
    pending_access: PendingAccess | None = None
    uncached_reads: int = 0
    uncached_writes: int = 0

    def __post_init__(self) -> None:
        if (self.cache is None) != (self.write_register is None):
            raise MachineError("cache and write register must be enabled or disabled together")

    @classmethod
    def from_image(
        cls,
        image: Iterable[int],
        *,
        cache_enabled: bool = True,
    ) -> MemorySubsystem:
        words = [to_word(value) for value in image]
        if len(words) > REGULAR_MEMORY_SIZE:
            raise MachineError(
                f"data image occupies {len(words)} words and overlaps MMIO starting at 0x{MMIO_IN_DATA:04X}"
            )
        words.extend([0] * (REGULAR_MEMORY_SIZE - len(words)))
        return cls(
            words=words,
            cache=DataCache() if cache_enabled else None,
            write_register=WriteRegister() if cache_enabled else None,
        )

    def request_read(self, address: int) -> MemoryResponse:
        self._ensure_idle()
        region = decode_address(address)

        match region:
            case AddressRegion.MMIO_IN_DATA:
                value = self.input_controller.data
                return MemoryResponse(
                    event=f"mmio_read [0x{address:04X}] -> {value}",
                    value=value,
                    done=True,
                )
            case AddressRegion.MMIO_IN_STATUS:
                value = self.input_controller.status
                return MemoryResponse(
                    event=f"mmio_read [0x{address:04X}] -> {value}",
                    value=value,
                    done=True,
                )
            case AddressRegion.MMIO_OUT_DATA | AddressRegion.MMIO_IRQ_ACK:
                raise MachineError(f"attempt to read write-only MMIO register 0x{address:04X}")
            case AddressRegion.INVALID:
                raise MachineError(f"data memory address out of range or reserved: 0x{address:X}")
            case AddressRegion.REGULAR:
                return self._request_regular_read(address)

    def request_write(self, address: int, value: int) -> MemoryResponse:
        self._ensure_idle()
        region = decode_address(address)

        match region:
            case AddressRegion.MMIO_OUT_DATA:
                self.output_buffer.append(chr(value & 0xFF))
                return MemoryResponse(
                    event=f"mmio_write {value} -> [0x{address:04X}]",
                    done=True,
                )
            case AddressRegion.MMIO_IRQ_ACK:
                self.input_controller.acknowledge(value)
                return MemoryResponse(
                    event=f"mmio_write {value} -> [0x{address:04X}]",
                    done=True,
                )
            case AddressRegion.MMIO_IN_DATA | AddressRegion.MMIO_IN_STATUS:
                raise MachineError(f"attempt to write read-only MMIO register 0x{address:04X}")
            case AddressRegion.INVALID:
                raise MachineError(f"data memory address out of range or reserved: 0x{address:X}")
            case AddressRegion.REGULAR:
                return self._request_regular_write(address, value)

    def _start_pending(
        self,
        completion: MemoryCompletion,
        address: int,
        *,
        event: str,
        write_value: int | None = None,
        ticks: int = 0,
    ) -> MemoryResponse:
        self.pending_access = PendingAccess(
            completion=completion,
            address=address,
            write_value=write_value,
            remaining_ticks=ticks,
        )
        return MemoryResponse(event=event)

    def _request_regular_read(self, address: int) -> MemoryResponse:
        if self.cache is None:
            self.uncached_reads += 1
            return self._start_pending(
                MemoryCompletion.MEMORY_READ,
                address,
                ticks=MEMORY_ACCESS_TICKS,
                event=f"memory_read [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
            )

        cache_value = self.cache.read(address)
        if cache_value is not None:
            return MemoryResponse(
                event=f"cache_hit read [0x{address:04X}] -> {cache_value}",
                value=cache_value,
                done=True,
            )

        return self._start_pending(
            MemoryCompletion.CACHE_READ_MISS,
            address,
            ticks=MEMORY_ACCESS_TICKS,
            event=f"cache_miss read [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
        )

    def _request_regular_write(self, address: int, value: int) -> MemoryResponse:
        if self.cache is None:
            self.uncached_writes += 1
            return self._start_pending(
                MemoryCompletion.MEMORY_WRITE,
                address,
                write_value=value,
                ticks=MEMORY_ACCESS_TICKS,
                event=f"memory_write {value} -> [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
            )

        hit = self.cache.probe_write(address)
        if hit:
            write_register = self.write_register
            if write_register is None:
                raise MachineError("cache enabled without write register")

            if not write_register.busy:
                self.cache.update_hit(address, value)
                write_register.load(address, value)
                return MemoryResponse(
                    event=f"cache_hit write {value} -> [0x{address:04X}]; wr_load",
                    done=True,
                )

            return self._start_pending(
                MemoryCompletion.CACHE_HIT_WRITE_REGISTER,
                address,
                write_value=value,
                event=f"cache_hit write {value} -> [0x{address:04X}]; wr_busy",
            )

        return self._start_pending(
            MemoryCompletion.CACHE_WRITE_MISS,
            address,
            write_value=value,
            ticks=MEMORY_ACCESS_TICKS,
            event=f"cache_miss write {value} -> [0x{address:04X}]; wait={MEMORY_ACCESS_TICKS}",
        )

    def tick(self) -> MemoryControllerTick:
        """Один автономный такт контроллера; Write Register имеет приоритет."""
        pending = self.pending_access
        write_register = self.write_register
        wr_owns_port = write_register is not None and write_register.busy

        events: list[str] = []
        if write_register is not None and write_register.busy:
            commit = write_register.tick()
            if commit is not None:
                address, value = commit
                self.words[address] = value
                events.append(f"wr_commit {to_signed32(value)} -> [0x{address:04X}]")

        response: MemoryResponse | None = None
        if pending is not None:
            if pending.waits_for_write_register:
                if self.write_register_busy:
                    events.append("mem_wait; wr_busy")
                else:
                    response = self._complete_pending_access()
                    events.append(response.event)
            else:
                if pending.remaining_ticks <= 0:
                    raise MachineError("invalid lower-memory wait counter")
                if wr_owns_port:
                    events.append(f"mem_wait; wr; wait={pending.remaining_ticks}")
                else:
                    pending.remaining_ticks -= 1
                    if pending.remaining_ticks > 0:
                        events.append(f"mem_wait; wait={pending.remaining_ticks}")
                    else:
                        response = self._complete_pending_access()
                        events.append(response.event)

        return MemoryControllerTick(event="; ".join(events), response=response)

    def _complete_pending_access(self) -> MemoryResponse:
        pending = self.pending_access
        if pending is None:
            raise MachineError("no pending memory access")

        address = pending.address
        value: int | None = None

        match pending.completion:
            case MemoryCompletion.MEMORY_READ:
                value = to_signed32(self.words[address])
                event = f"memory_read_done [0x{address:04X}] -> {value}"

            case MemoryCompletion.MEMORY_WRITE:
                if pending.write_value is None:
                    raise MachineError("pending memory write has no value")
                self.words[address] = to_word(pending.write_value)
                event = f"memory_write_done {pending.write_value} -> [0x{address:04X}]"

            case MemoryCompletion.CACHE_READ_MISS:
                if self.cache is None:
                    raise MachineError("cache read miss without cache")
                raw_value = self.words[address]
                self.cache.fill(address, raw_value)
                value = to_signed32(raw_value)
                event = f"cache_fill_done read [0x{address:04X}] -> {value}"

            case MemoryCompletion.CACHE_WRITE_MISS:
                if self.cache is None:
                    raise MachineError("cache write miss without cache")
                if pending.write_value is None:
                    raise MachineError("pending cache write has no value")
                self.words[address] = to_word(pending.write_value)
                self.cache.fill(address, pending.write_value)
                event = f"cache_fill_done write {pending.write_value} -> [0x{address:04X}]"

            case MemoryCompletion.CACHE_HIT_WRITE_REGISTER:
                if self.cache is None or self.write_register is None:
                    raise MachineError("cache-hit store without cache/write register")
                if pending.write_value is None:
                    raise MachineError("pending buffered store has no value")
                if self.write_register.busy:
                    raise MachineError("write register reload attempted while busy")
                self.cache.update_hit(address, pending.write_value)
                self.write_register.load(address, pending.write_value)
                event = f"cache_hit_commit {pending.write_value} -> [0x{address:04X}]; wr_load"

        self.pending_access = None
        return MemoryResponse(event=event, value=value, done=True)

    def _ensure_idle(self) -> None:
        if self.pending_access is not None:
            raise MachineError("new memory request while another access is pending")

    @property
    def write_register_busy(self) -> bool:
        return self.write_register is not None and self.write_register.busy

    @property
    def output(self) -> str:
        return "".join(self.output_buffer)

    @property
    def cache_hits(self) -> int:
        return 0 if self.cache is None else self.cache.hits

    @property
    def cache_misses(self) -> int:
        return 0 if self.cache is None else self.cache.misses

    @property
    def irq_pending(self) -> bool:
        return self.input_controller.irq_pending

    @property
    def input_status(self) -> int:
        return self.input_controller.status

    @property
    def input_overrun_count(self) -> int:
        return self.input_controller.overrun_count

    def push_input_token(self, token_code: int) -> bool:
        return self.input_controller.push_token(token_code)


@dataclass(slots=True)
class Machine:
    """Потактовая модель; constructor initialization играет роль power-on init."""

    program_memory: list[int]
    data_memory: MemorySubsystem
    input_events: list[InputEvent] = field(default_factory=list)
    pc: int = RESET_VECTOR
    ir_word: int | None = None
    control_state: ControlState = ControlState.FETCH
    tick_counter: int = 0

    data_stack: list[int] = field(default_factory=list)
    return_stack: list[int] = field(default_factory=list)

    irq_enable: bool = False
    in_irq: bool = False
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
    ) -> Machine:
        program_file = Path(program_path)
        program_words = read_program_words(program_file)
        data_image = read_data_binary(data_path)
        input_events = read_input_schedule(input_path) if input_path is not None else []

        return cls(
            program_memory=program_words,
            data_memory=MemorySubsystem.from_image(data_image, cache_enabled=cache_enabled),
            input_events=input_events,
        )

    def run(self, *, limit: int = DEFAULT_TICK_LIMIT) -> str:
        if limit <= 0:
            raise MachineError("hard tick limit must be positive")

        while self.control_state != ControlState.HALTED or self.data_memory.write_register_busy:
            if self.tick_counter >= limit:
                raise MachineError(f"hard tick limit exceeded: {limit}")
            self.step_tick()

        return self.data_memory.output

    def step_tick(self) -> None:
        old_state = self.control_state
        controller_tick = self.data_memory.tick()

        if old_state == ControlState.HALTED:
            event = "halted; drain_wr" if self.data_memory.write_register_busy else "halted"
            if controller_tick.event:
                event += f"; {controller_tick.event}"
            self._append_log(event, old_state, self.control_state)
            self.tick_counter += 1
            return

        self._deliver_input_events_for_current_tick()

        match old_state:
            case ControlState.FETCH:
                event = self._tick_fetch()
            case ControlState.EXECUTE:
                event = self._tick_execute()
            case ControlState.MEM_WAIT:
                event = self._tick_mem_wait(controller_tick)
            case ControlState.IRQ_CHECK:
                event = self._tick_irq_check()
            case _:
                raise MachineError(f"unsupported control state: {old_state}")

        if old_state != ControlState.MEM_WAIT and controller_tick.event:
            event += f"; {controller_tick.event}"
        self._append_log(event, old_state, self.control_state)
        self.tick_counter += 1

    def _tick_fetch(self) -> str:
        self._check_program_address(self.pc)
        self.ir_word = self.program_memory[self.pc]
        old_pc = self.pc
        self.pc += 1
        self.control_state = ControlState.EXECUTE
        return f"fetch raw=0x{self.ir_word:08X} @{old_pc:08X}"

    def _tick_execute(self) -> str:
        if self.ir_word is None:
            raise MachineError("IR is empty")
        try:
            instruction = decode_instruction(self.ir_word)
        except IsaError as exc:
            raise MachineError(f"invalid instruction in IR: {exc}") from exc

        opcode_class = classify_opcode(instruction.opcode)

        match opcode_class:
            case OpcodeClass.STACK:
                event = self._execute_stack(instruction.opcode, instruction.arg)
            case OpcodeClass.ALU:
                event = self._execute_alu(instruction.opcode)
            case OpcodeClass.MEMORY:
                return self._execute_memory(instruction.opcode)
            case OpcodeClass.FLOW:
                event = self._execute_flow(instruction.opcode, instruction.arg)
            case OpcodeClass.IRQ:
                event = self._execute_irq(instruction.opcode)
            case OpcodeClass.HALT:
                self.executed_instructions += 1
                self.control_state = ControlState.HALTED
                return "halt"

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
                if len(self.data_stack) < 2:
                    raise MachineError("data stack underflow")
                self.data_stack[-2], self.data_stack[-1] = self.data_stack[-1], self.data_stack[-2]
                return "swap"
            case Opcode.OVER:
                self.push(self.peek(1))
                return "over"
            case _:
                raise MachineError(f"not a stack opcode: {opcode}")

    def _execute_alu(self, opcode: Opcode) -> str:
        right = self.peek()
        left = self.peek(1)
        result = alu(opcode, left, right)

        self.data_stack[-2:] = [result]
        return f"{MNEMONICS[opcode]} {left} {right} -> {result}"

    def _execute_memory(self, opcode: Opcode) -> str:
        if opcode == Opcode.LOAD:
            address = self.peek()
            response = self.data_memory.request_read(address)
            if response.done:
                if response.value is None:
                    raise MachineError("completed LOAD did not return a value")
                self.replace_tos(response.value)
                return self._finish_instruction(response.event)

            self.pop()
            self.control_state = ControlState.MEM_WAIT
            return response.event

        address = self.peek()
        value = self.peek(1)
        response = self.data_memory.request_write(address, value)
        self.drop_two()
        if response.done:
            return self._finish_instruction(response.event)

        self.control_state = ControlState.MEM_WAIT
        return response.event

    def _execute_flow(self, opcode: Opcode, arg: int) -> str:
        match opcode:
            case Opcode.JMP:
                self._check_program_address(arg)
                self.pc = arg
                return f"jmp 0x{arg:08X}"
            case Opcode.JZ:
                condition = self.peek()
                if condition == 0:
                    self._check_program_address(arg)
                self.pop()
                if condition == 0:
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
                target = self.peek()
                self._check_program_address(target)
                self.push_return_address(self.pc)
                self.pop()
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

    def _tick_mem_wait(self, controller_tick: MemoryControllerTick) -> str:
        response = controller_tick.response
        if response is None:
            return controller_tick.event or "mem_wait"

        if not response.done:
            raise MachineError("MEM_WAIT received an incomplete memory response")

        if response.value is not None:
            self.push(response.value)

        return self._finish_instruction(controller_tick.event or response.event)

    def _tick_irq_check(self) -> str:
        if self.irq_enable and self.data_memory.irq_pending and not self.in_irq:
            self._check_program_address(IRQ_VECTOR)
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

    def replace_tos(self, value: int) -> None:
        if not self.data_stack:
            raise MachineError("data stack underflow")
        self.data_stack[-1] = to_signed32(value)

    def drop_two(self) -> None:
        if len(self.data_stack) < 2:
            raise MachineError("data stack underflow")
        del self.data_stack[-2:]

    def _ensure_return_stack_capacity(self) -> None:
        if len(self.return_stack) >= DEFAULT_STACK_LIMIT:
            raise MachineError("return stack overflow")

    def push_return_address(self, address: int) -> None:
        self._ensure_return_stack_capacity()
        self._check_program_address(address)
        self.return_stack.append(address)

    def _pop_return_address(self) -> int:
        if not self.return_stack:
            raise MachineError("return stack underflow")
        address = self.return_stack[-1]
        self._check_program_address(address)
        self.return_stack.pop()
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

    def _check_program_address(self, address: int) -> None:
        if not 0 <= address < len(self.program_memory):
            raise MachineError(f"program address out of range: 0x{address:X}")

    def _ir_text(self, state: ControlState) -> str:
        if self.ir_word is None:
            return "<none>"
        if state == ControlState.FETCH:
            return f"raw 0x{self.ir_word:08X}"
        try:
            instruction = decode_instruction(self.ir_word)
        except IsaError:
            return f"invalid 0x{self.ir_word:08X}"
        mnemonic = MNEMONICS[instruction.opcode]
        if instruction.opcode in {Opcode.LIT, Opcode.JMP, Opcode.JZ, Opcode.CALL}:
            return f"{mnemonic} {instruction.arg}"
        return mnemonic

    def cache_summary(self) -> str:
        cache_mode = "on" if self.data_memory.cache is not None else "off"
        write_register = self.data_memory.write_register
        if write_register is None:
            wr_summary = "wr=off"
        elif write_register.busy:
            wr_summary = f"wr=busy@{write_register.remaining_ticks}"
        else:
            wr_summary = "wr=empty"
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

    def _append_log(self, event: str, old_state: ControlState, new_state: ControlState) -> None:
        mode = "irq" if self.in_irq else "user"
        instr = self._ir_text(old_state)
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
        transition = f"{old_state.value}->{new_state.value}"
        self.log_lines.append(
            f"DEBUG   machine:simulation    "
            f"TICK: {self.tick_counter:5} "
            f"PC: {self.pc:5} "
            f"STATE: {transition:<21} "
            f"MODE: {mode:<4} "
            f"SP:{len(self.data_stack):3} "
            f"DS: {stack:<20} "
            f"RS: {rstack:<20} "
            f"IRQ:{irq_state:<9} "
            f"CACHE:{cache_state:<11} "
            f"WR:{wr_state:<8} "
            f"{instr:<15} [{event}]"
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


def alu(opcode: Opcode, left: int, right: int) -> int:
    """Комбинационная функция ALU без доступа к стеку и журналу."""
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

    return to_signed32(result)


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
    parser.add_argument("--log", type=Path, default=None, help="записать журнал процессора в файл")
    parser.add_argument("--output", type=Path, default=None, help="записать вывод процессора в файл")
    parser.add_argument(
        "--cache", action=argparse.BooleanOptionalAction, default=True, help="включить или выключить cache данных"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    machine = Machine.from_files(
        args.program,
        args.data,
        args.input,
        cache_enabled=args.cache,
    )
    output = machine.run(limit=args.limit)
    status = "halted"

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
