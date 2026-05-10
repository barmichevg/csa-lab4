\ irq_echo: печать символа прямо из обработчика

variable i

:irq
    read-char emit ack-irq iret
;

0 i !
ei

begin
    i @ 1 + dup i !
    20 =
until

halt
