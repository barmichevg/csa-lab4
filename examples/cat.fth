\ cat: печать ввода до '\n'

: wait-char
    begin input-ready? until
    input-pop
;

:irq
    read-char ack-irq input-push iret
;

input-init ei

begin
    wait-char
    dup 10 =
    if emit 1 else emit 0 then
until

halt
