\ cache: повторное чтение x должно давать hits

variable x
variable i
variable sum

42 x !
0 i !
0 sum !

begin
    sum @ x @ + sum !
    i @ 1 + dup i !
    10 =
until

sum @ 420 =
if
    ."CACHE 420\n"
else
    ."CACHE FAIL\n"
then

halt
