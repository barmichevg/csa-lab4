\ execution token: ' inc execute and primitive trampoline

: inc
    1 +
;

10 ' inc execute 11 =
20 22 ' + execute 42 =
*
if
    ."XT OK\n"
else
    ."XT FAIL\n"
then

halt
