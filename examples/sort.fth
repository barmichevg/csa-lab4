\ sort: n и элементы — однозначные числа через пробел

buffer numbers 16
variable n
variable i
variable j
variable limit
variable a
variable b

: wait-char
    begin input-ready? until
    input-pop
;

: read-digit
    wait-char 48 -
;

: skip-char
    wait-char drop
;

:irq
    read-char ack-irq input-push iret
;

input-init ei

read-digit n !
0 i !

\ чтение массива
begin
    skip-char
    read-digit numbers i @ + !
    i @ 1 + dup i !
    n @ =
until

\ bubble sort
0 i !
begin
    n @ i @ - 1 - limit !
    0 j !

    begin
        numbers j @ + @ a !
        numbers j @ 1 + + @ b !

        a @ b @ >
        if
            b @ numbers j @ + !
            a @ numbers j @ 1 + + !
        then

        j @ 1 + dup j !
        limit @ =
    until

    i @ 1 + dup i !
    n @ 1 - =
until

\ печать массива
0 i !
begin
    numbers i @ + @ 48 + emit
    i @ 1 + dup i !

    n @ =
    if cr 1 else space 0 then
until

halt
