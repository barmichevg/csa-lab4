\ execution token: ' inc execute

: inc
    1 +
;

10 ' inc execute 11 =
if
    ."XT OK\n"
else
    ."XT FAIL\n"
then

halt
