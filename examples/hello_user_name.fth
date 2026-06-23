\ hello_user_name: ввод имени до '\n', максимум 31 символ

buffer username 32
variable username-len
variable username-ptr
variable username-overflow

:irq
    read-char ack-irq input-push iret
;

0 username-len !
0 username-overflow !
username 1 + username-ptr !
input-init ei

."What is your name?\n"

begin
    wait-char
    dup 10 =
    if
        drop
        1
    else
        username-len @ 31 <
        if
            username-ptr @ !
            username-ptr @ 1 + username-ptr !
            username-len @ 1 + username-len !
        else
            drop
            1 username-overflow !
        then
        0
    then
until

username-overflow @
if
    ."Name is too long\n"
    halt
then

username-len @ username !
."Hello, " username type ."!\n"
halt
