\ prob1: сумма кратных 3 или 5 ниже введённого limit

variable limit
variable last
variable m3
variable m5
variable m15
variable result

:irq
    read-char ack-irq input-push iret
;

input-init ei

read-number limit !
limit @ 1 - last !

last @ 3 / m3 !
last @ 5 / m5 !
last @ 15 / m15 !

3 m3 @ * m3 @ 1 + * 2 /
5 m5 @ * m5 @ 1 + * 2 /
+
15 m15 @ * m15 @ 1 + * 2 /
-
result !

p"PROB1 " type
result @ print-int
cr

halt
