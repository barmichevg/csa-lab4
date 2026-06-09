\ execution token: ' inc execute

: inc
    1 +
;

10 ' inc execute 11 =
if
    p"XT OK\n" type
else
    p"XT FAIL\n" type
then

halt
