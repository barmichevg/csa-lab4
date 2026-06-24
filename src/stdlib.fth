variable __type-ptr
variable __type-len

buffer __input-buffer 64
variable __input-head
variable __input-tail
variable __input-next
variable __input-tmp

variable __read-acc
variable __read-started
variable __read-sign
variable __read-ch

buffer __print-digits 10
variable __print-value
variable __print-count

: emit
    out-data !
;

: cr
    10 emit
;

: space
    32 emit
;

: read-char
    in-data @
;

: ack-irq
    1 irq-ack !
;

: input-init
    0 __input-head !
    0 __input-tail !
    0 __input-next !
;

: input-ready?
    __input-head @ __input-tail @ =
    if 0 else 1 then
;

: input-push
    __input-tail @
    1 +
    64 mod
    __input-next !

    __input-next @ __input-head @ =
    if
        drop
    else
        __input-buffer __input-tail @ + !
        __input-next @ __input-tail !
    then
;

: input-pop
    input-ready?
    if
        __input-buffer __input-head @ + @ __input-tmp !

        __input-head @
        1 +
        64 mod
        __input-head !

        __input-tmp @
    else
        0
    then
;

: wait-char
    begin input-ready? until
    input-pop
;

: digit?
    dup 47 > swap 58 < *
;

: read-number
    0 __read-acc !
    0 __read-started !
    1 __read-sign !

    begin
        wait-char __read-ch !

        __read-ch @ 45 = __read-started @ 0 = *
        if
            -1 __read-sign !
            0
        else
            __read-ch @ digit?
            if
                __read-acc @ 10 * __read-ch @ 48 - + __read-acc !
                1 __read-started !
                0
            else
                __read-started @
            then
        then
    until

    __read-acc @ __read-sign @ *
;

: print-int
    dup 0 <
    if
        45 emit
    else
        -1 *
    then

    __print-value !

    __print-value @ 0 =
    if
        48 emit
    else
        0 __print-count !

        begin
            __print-value @ 10 mod -1 * 48 + __print-digits __print-count @ + !
            __print-count @ 1 + __print-count !
            __print-value @ 10 / __print-value !
            __print-value @ 0 =
        until

        begin
            __print-count @ 1 - __print-count !
            __print-digits __print-count @ + @ emit
            __print-count @ 0 =
        until
    then
;

: type
    dup @ __type-len !
    1 + __type-ptr !

    begin
        __type-len @ 0 >
        if
            __type-ptr @ @ emit

            __type-ptr @
            1 +
            __type-ptr !

            __type-len @
            1 -
            __type-len !

            0
        else
            1
        then
    until
;
