\ wide: 64-битное сложение, 4 limb по 16 бит, база 65536

variable a0
variable a1
variable a2
variable a3

variable b0
variable b1
variable b2
variable b3

variable r0
variable r1
variable r2
variable r3

variable carry
variable tmp

: add-limb
    + carry @ + tmp !

    tmp @ 65535 >
    if
        tmp @ 65536 - tmp !
        1 carry !
    else
        0 carry !
    then

    tmp @
;

65535 a0 !
65535 a1 !
65535 a2 !
1 a3 !

1 b0 !
0 b1 !
0 b2 !
0 b3 !

0 carry !

a0 @ b0 @ add-limb r0 !
a1 @ b1 @ add-limb r1 !
a2 @ b2 @ add-limb r2 !
a3 @ b3 @ add-limb r3 !

r0 @ 0 =
r1 @ 0 =
*
r2 @ 0 =
*
r3 @ 2 =
*
if
    ."WIDE OK\n"
else
    ."WIDE FAIL\n"
then

halt
