\ sort: length-prefixed формат: n x1 x2 ... xn, максимум 16 элементов

buffer numbers 16
variable n
variable i
variable j
variable limit
variable a
variable b

:irq
    read-char ack-irq input-push iret
;

input-init ei

read-number n !

n @ 1 < n @ 16 > + 0 >
if
    p"SORT TOO BIG\n" type
    halt
then

0 i !

\ чтение массива
begin
    read-number numbers i @ + !
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
    numbers i @ + @ print-int
    i @ 1 + dup i !

    n @ =
    if cr 1 else space 0 then
until

halt
