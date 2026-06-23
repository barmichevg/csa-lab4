\ prob1: Project Euler problem 4 for input bounds low high

variable low
variable high
variable prefix
variable factor
variable factor-start
variable palindrome
variable quotient
variable result

variable rev-x
variable rev-r

:irq
    read-char ack-irq input-push iret
;

: reverse-number
    rev-x !
    0 rev-r !
    begin
        rev-x @ 0 >
        if
            rev-r @ 10 * rev-x @ 10 mod + rev-r !
            rev-x @ 10 / rev-x !
            0
        else
            1
        then
    until
    rev-r @
;

: make-palindrome
    dup 1000 * swap reverse-number +
;

input-init ei

read-number low !
read-number high !
0 result !
high @ high @ 11 mod - factor-start !
high @ prefix !

begin
    prefix @ make-palindrome palindrome !
    factor-start @ factor !

    begin
        factor @ high @ * palindrome @ <
        if
            1
        else
            palindrome @ factor @ mod 0 =
            if
                palindrome @ factor @ / quotient !

                quotient @ low @ 1 - >
                quotient @ high @ 1 + <
                *
                if
                    palindrome @ result !
                then
            then

            result @ 0 >
            if
                1
            else
                factor @ 11 - factor !
                factor @ low @ <
            then
        then
    until

    result @ 0 >
    if
        1
    else
        prefix @ 1 - prefix !
        prefix @ low @ <
    then
until

."PROB1 "
result @ print-int
cr

halt
