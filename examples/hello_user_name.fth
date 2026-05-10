\ hello_user_name: ввод имени до '\n'

buffer username 32
variable username-len
variable username-ptr

: wait-char
    begin input-ready? until
    input-pop
;

:irq
    read-char ack-irq input-push iret
;

0 username-len !
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
        username-ptr @ !
        username-ptr @ 1 + username-ptr !
        username-len @ 1 + username-len !
        0
    then
until

username-len @ username !
."Hello, " username type ."!\n"
halt
