\ double precision: пара high:low, база low = 10000

variable ahi
variable alo
variable bhi
variable blo
variable rhi
variable rlo
variable carry

1234 ahi !
9999 alo !
8765 bhi !
1 blo !

alo @ blo @ + rlo !

rlo @ 9999 >
if
    rlo @ 10000 - rlo !
    1 carry !
else
    0 carry !
then

ahi @ bhi @ + carry @ + rhi !

rhi @ 10000 =
rlo @ 0 =
*

if
    ."WIDE 10000:0\n"
else
    ."WIDE FAIL\n"
then

halt
