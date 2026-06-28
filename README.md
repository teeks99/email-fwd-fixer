# E-Mail Forward Fixer

There is an issue (with gmail) where emails forwarded from a domain (e.g. me@example.com, noticed when coming from [Dreamhost emails](https://help.dreamhost.com/hc/en-us/articles/115000326592-Using-Gmail-to-access-your-DreamHost-email-account)) to Gmail (e.g. example-me@gmail.com) will occasionally (maybe 1 in 100) get rejected. 

The goal of this is to provide a backup-check where emails that did not successfully forward will still be something that can be quickly notified.

# Architecture

Message Flow

```mermaid
flowchart LR
  alias[Fwd Only me@example.com]
  gmail[GMail Box example-me@gmail.com]
  imap1[IMap Box me-pass-through@example.com]
  imap2[IMap Box me-notify@example.com]
  alias --> gmail
  alias --> imap1

  program[This Program]
  imap1 --> program
  program --> imap2
```

How it runs

```mermaid
sequenceDiagram

  participant ExternalSender
  participant FwdOnly
  participant GmailBox
  participant IMapPassThrough
  participant Program
  participant IMapNotify

  ExternalSender ->> FwdOnly : Receive Message
  FwdOnly ->> GmailBox : Forward Message
  FwdOnly ->> IMapPassThrough : Forward Message

  Program ->> Program : TimerTick
  Program ->> IMapPassThrough : Check for new messages

  loop NewMessages
    Program ->> GmailBox : Does Message Exist? 
    alt MessageDoesNotExist
      Program ->> GmailBox : Add Message Manually
      Program ->> IMapNotify : Copy Message Here
    end
    Program ->> IMapPassThrough : Remove Message
  end
```