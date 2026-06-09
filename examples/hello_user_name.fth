\ hello_user_name: ввод имени до '\n'

buffer username 32
variable username-len
variable username-ptr

:irq
    read-char ack-irq input-push iret
;

0 username-len !
username 1 + username-ptr !
input-init ei

p"What is your name?\n" type

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
p"Hello, " type username type p"!\n" type
halt
