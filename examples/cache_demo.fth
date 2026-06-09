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

."CACHE "
sum @ print-int
cr

halt
