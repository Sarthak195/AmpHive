# Self-hosted mail server — Postfix + Dovecot + OpenDKIM on `amphive-relay`

*Built and verified 2026-08-18 against the live VM. Companion to
[deploy/docs/relay_consolidation_runbook.md](relay_consolidation_runbook.md)
(the same host runs the whole AmpHive app stack) and
[deploy/docs/web_tls_rollout.md](web_tls_rollout.md) (the Caddy front door that
already owns 80/443 on this box).*

This is a **learning/portfolio build**, not production infrastructure for the
product. AmpHive's own transactional email (password resets, billing notices)
still goes out through an external Gmail SMTP account on `smtp.gmail.com:587` —
verified in `~/amphive-relay/.env`, `SMTP_HOST=smtp.gmail.com`. **Nothing in the
product depends on this mail server.** That is deliberate: it means the box can
be broken, rebuilt, and experimented on without taking down customer email.

The document explains *why* each knob is set the way it is, not just what it is
set to. Email is a protocol stack where almost every default is wrong for a
modern internet-facing host, and most of the interesting content here is the
reasoning behind departures from those defaults.

---

## 0. The constraint that shapes the entire design: GCP blocks outbound port 25

> **Google Cloud blocks outbound TCP/25 from every Compute Engine VM,
> permanently and unconditionally. There is no exception request form, no
> support ticket, and no paid tier that lifts it.** This is different from AWS,
> where the block is real but *is* liftable via a request form tied to your
> account's sending reputation. On GCP the answer is simply no.
>
> **Consequence: this server can RECEIVE mail directly from the internet, but it
> can never DELIVER mail directly to another mail server.** Everything about the
> outbound half of the design is downstream of that single fact.

This was not taken on faith — it was measured from the VM on 2026-08-18. The
naive test (connect to a few MXes on 25, see them fail) is not sufficient
evidence, because a failure could equally mean "the destination is down", "DNS
gave us an IPv6 address and we have no IPv6", or "egress is broken generally".
The probe was therefore built with controls that isolate the port as the only
variable:

| Target | Port | Result | What it rules out |
|---|---|---|---|
| `74.125.135.26` (gmail-smtp-in.l.google.com) | 25 | **FAIL** — 12s, no RST | — |
| `142.250.107.26` (aspmx.l.google.com) | 25 | **FAIL** — 12s, no RST | not one bad host |
| `17.57.8.134` (mx1.mail.icloud.com) | 25 | **FAIL** — 12s, no RST | not one bad *vendor* |
| `74.125.135.109` (smtp.gmail.com) | **587** | **OK** — instant | egress works; DNS works |
| `74.125.135.109` (smtp.gmail.com) | **25** | **FAIL** — 12s, no RST | **same IP, port is the only variable** |
| `74.125.135.26` (gmail-smtp-in) | **443** | **OK** — instant | **same IP, port is the only variable** |
| `1.1.1.1` | 443 | **OK** — instant | general internet egress is healthy |

The last three rows are the ones that matter. `74.125.135.109:587` connects
instantly while `74.125.135.109:25` hangs for the full 12-second timeout — same
destination address, same route, same instant in time. The only difference is
the destination port. Likewise `74.125.135.26` answers on 443 and blackholes on
25.

Two further details worth understanding:

- **`ip -6 addr show scope global` is empty on this VM** — there is no global
  IPv6 address at all. An early version of this probe resolved
  `gmail-smtp-in.l.google.com` to `2607:f8b0:400e:c0a::1b` and "confirmed" the
  block, which would have been a false positive: with no IPv6 that connection
  was doomed regardless of port. All results in the table above are IPv4
  literals, resolved with `getent ahostsv4`. If you re-run this test, force IPv4
  or you will fool yourself.
- **The failures are silent drops, not refusals.** A closed port returns a TCP
  RST immediately and `connect()` fails in milliseconds. These sit for the full
  timeout with no response at all, which is the signature of a packet filter
  blackholing the SYN. That is what a cloud-provider egress block looks like
  from the inside, and it is also why a misconfigured outbound mail server on
  GCP appears to "hang" rather than error.

**What this means operationally:** `relayhost` is empty and `default_transport`
is `smtp` (both verified), so if you hand this server a message for an external
domain today, Postfix will try to connect directly to the recipient's MX on port
25, get blackholed, and requeue. The message will sit in the deferred queue
retrying until `maximal_queue_lifetime` expires (Postfix default: 5 days) and
then bounce. **It will not error loudly at submission time** — it will accept
the mail and fail hours later, which is the single most confusing failure mode
on this host. Sending outbound requires configuring a smarthost on 587; see
[§9 Known gaps](#9-known-gaps--not-done-yet).

Note the pleasing symmetry: the AmpHive application already routes its mail
through `smtp.gmail.com:587` for exactly this reason. The port 25 block is not a
theoretical concern on this box — it has already dictated an architecture
decision elsewhere in the project.

**Inbound port 25 is *not* blocked.** Verified from an off-VM network (the dev
box): `136.117.94.209:25` accepts connections and returns
`220 mail.amphive.app ESMTP`. GCP's block is egress-only. So the receiving half
of this server is genuinely internet-facing and genuinely works.

---

## 1. Current state — what works today and what does not

| Component | State on 2026-08-18 | Verified how |
|---|---|---|
| Postfix 3.7.11 | running, enabled at boot | `systemctl is-active/is-enabled postfix` |
| Dovecot 2.3.19.1 | running, enabled at boot | same |
| OpenDKIM 2.11.0~beta2 | running, enabled at boot, signing | same + a real signature on a delivered message |
| Local submission → maildir | **working end to end** | test message traced through the log and read out of the maildir |
| Inbound from the public internet | **untested — cannot work yet** | no MX record exists (see below) |
| Outbound to the internet | **cannot work** | GCP port 25 block, no smarthost configured |
| TLS certificate | **self-signed snakeoil** | `CN=amphive-relay`, issuer `CN=amphive-relay` |
| Anti-open-relay | **working, already load-tested by a real attacker** | 25 × `554 5.7.1 Relay access denied` in the log |

> **The largest gap is DNS: none of the mail records are published.** Queried
> against `8.8.8.8` on 2026-08-18:
>
> - `amphive.app` **A** → `136.117.94.209` ✅ (the apex exists — it serves the web app)
> - `mail.amphive.app` **A** → **NXDOMAIN**
> - `amphive.app` **MX** → **none** (authority section only)
> - `amphive.app` **TXT** → **none** (no SPF)
> - `_dmarc.amphive.app` **TXT** → **NXDOMAIN**
> - `mail._domainkey.amphive.app` **TXT** → **NXDOMAIN**
>
> Until at least the A and MX records exist, **no mail will ever arrive from the
> internet**, because no sending server has any way to discover this host. The
> server is correctly configured and listening; nobody has been told it is
> there. Everything in [§7 DNS](#7-dns-records--what-each-one-is-for) is
> therefore a *to-do*, not a record of work done.

---

## 2. Architecture — the hop-by-hop life of an inbound message

This is the part worth internalising. Take a message sent from a Gmail user to
`support@amphive.app`, and assume for the moment that the DNS records from §7
exist.

**1. Sender-side MX lookup.** Gmail's outbound server needs to know which host
accepts mail for the *domain* `amphive.app`. It does **not** connect to the
domain's A record — that is a common misconception. It queries `MX amphive.app`,
gets back `10 mail.amphive.app`, then resolves `A mail.amphive.app` →
`136.117.94.209`. This indirection is the whole reason MX records exist: it lets
the web site and the mail server live on different machines. (Here they happen
to be the same machine, but the protocol does not know or care.)

**2. TCP connect to port 25.** Gmail connects to `136.117.94.209:25`. GCP's
ingress firewall rule `allow-amphive-mail` permits `tcp:25,tcp:587,tcp:993` from
`0.0.0.0/0` with no target tags, so this is allowed. Postfix's `master` process
is listening (`0.0.0.0:25`) and forks an `smtpd` to handle the session.

**3. Greeting and EHLO.** Postfix answers `220 mail.amphive.app ESMTP` — the
banner comes from `smtpd_banner = $myhostname ESMTP`, and the deliberate absence
of a version string is a small anti-fingerprinting measure. Gmail sends `EHLO`;
Postfix replies with its capability list. Because `smtpd_helo_required = yes`,
a client that skips EHLO/HELO entirely is rejected — trivially cheap, and a
surprising amount of spamware still fails it.

**4. STARTTLS.** The capability list advertises `STARTTLS`. Gmail issues it and
the session upgrades to TLS 1.3 before any addresses are exchanged. See
[§6 TLS](#6-tls-posture--and-why-a-snakeoil-cert-is-still-worth-having) for why
this is opportunistic rather than mandatory.

**5. `MAIL FROM` and `RCPT TO` — the gate.** Two independent checks fire here,
and conflating them is the classic way to build an open relay:

  - `smtpd_relay_restrictions` decides **may this client ask us to carry this
    mail at all**. See [§4](#4-the-anti-open-relay-configuration).
  - `smtpd_reject_unlisted_recipient` (effective value `yes`, verified) decides
    **do we actually have this mailbox**. For a domain listed in
    `virtual_mailbox_domains`, Postfix consults `virtual_alias_maps` and
    `virtual_mailbox_maps`; if neither yields a result the recipient is rejected
    *at RCPT TO*, before the message body is ever transmitted. See
    [§3.2](#32-virtual_mailbox_maps-and-why-rejecting-early-matters-backscatter).

**6. `DATA`.** Gmail transmits headers and body. Postfix's `smtpd` hands the
message to `cleanup`, which is where address rewriting happens:
`virtual_alias_maps` is applied *here*, so a message addressed to
`postmaster@amphive.app` has its recipient rewritten to `support@amphive.app`
at this point.

**7. The milter.** `smtpd_milters = inet:127.0.0.1:8891` sends the message to
OpenDKIM. On *inbound* mail OpenDKIM is in verify mode (`Mode sv` = sign **and**
verify) and adds an `Authentication-Results` header recording whether the
sender's own DKIM signature checked out. It does not reject anything; it
annotates. Deciding what to *do* with a failed signature is the spam filter's
job, and there is no spam filter yet ([§9](#9-known-gaps--not-done-yet)).

**8. Queue and hand-off.** `cleanup` writes the queue file; `qmgr` picks it up
and looks up a transport. Because `amphive.app` is in `virtual_mailbox_domains`,
`qmgr` uses `virtual_transport = lmtp:unix:private/dovecot-lmtp` rather than the
default `smtp`. This is the seam between the two halves of the system: **Postfix
stops here and Dovecot takes over.**

**9. LMTP to Dovecot.** Postfix's `lmtp` client connects to the UNIX socket
`private/dovecot-lmtp`. Note the path is relative — Postfix's daemons run
chrooted to `/var/spool/postfix`, so the socket must physically exist at
`/var/spool/postfix/private/dovecot-lmtp`. That is why Dovecot's config places
its LMTP listener *inside Postfix's chroot* rather than in a normal location
like `/run/dovecot/`. LMTP (RFC 2033) is used rather than SMTP because it gives
a **per-recipient** status response, so a multi-recipient message can be
partially delivered with accurate reporting instead of one all-or-nothing reply.

**10. Maildir write.** Dovecot's LMTP service resolves the user through its
`static` userdb (everything maps to uid/gid `vmail`), computes the path from
`mail_location = maildir:/var/mail/vhosts/%d/%n` →
`/var/mail/vhosts/amphive.app/support/`, and writes the message as a file in
`new/`. Maildir (one file per message) is used rather than mbox (one file per
folder) because it needs no locking — concurrent LMTP delivery and IMAP reads
cannot corrupt each other.

**11. Retrieval.** The user's mail client connects to port 993 (IMAPS),
authenticates against the `passwd-file` passdb, and reads the same maildir.

This path was verified end to end on 2026-08-18, though **from local submission
rather than from the internet** (there is no MX record yet, so step 1–2 could
not be exercised). The log shows the full chain for message `2B9E36B380`:

```
postfix/pickup[3059710]:  2B9E36B380: uid=0 from=<postmaster@amphive.app>
postfix/cleanup[3059850]: 2B9E36B380: message-id=<20260818095155.2B9E36B380@mail.amphive.app>
opendkim[3054394]:        2B9E36B380: DKIM-Signature field added (s=mail, d=amphive.app)
postfix/qmgr[3059709]:    2B9E36B380: from=<postmaster@amphive.app>, size=370, nrcpt=1 (queue active)
dovecot: lmtp(support@amphive.app)<...>: msgid=<...>: saved mail to INBOX
postfix/lmtp[3059853]:    2B9E36B380: to=<support@amphive.app>, relay=mail.amphive.app[private/dovecot-lmtp],
                          delay=0.13, dsn=2.0.0, status=sent (250 2.0.0 ... Saved)
postfix/qmgr[3059709]:    2B9E36B380: removed
```

Read that sequence once and the architecture stops being abstract: `pickup` →
`cleanup` → milter → `qmgr` → `lmtp` → Dovecot → `removed`.

---

## 3. Postfix identity and the virtual-domain decision

### 3.1 `mydestination` vs `virtual_mailbox_domains` — the mistake that eats mail

Verified configuration:

```
myhostname             = mail.amphive.app
mydomain               = amphive.app
myorigin               = $myhostname
mydestination          = $myhostname, localhost.$mydomain, localhost
virtual_mailbox_domains = amphive.app
virtual_transport       = lmtp:unix:private/dovecot-lmtp
```

Postfix has two entirely separate delivery universes and a domain must belong to
exactly one of them:

- **`mydestination`** — "final destination, delivered by the `local` transport
  to *system users*". Recipients are resolved against `/etc/passwd` (via
  `local_recipient_maps = proxy:unix:passwd.byname $alias_maps`, verified) and
  dropped into `/var/mail/<username>` mbox files.
- **`virtual_mailbox_domains`** — "final destination, delivered by
  `virtual_transport` to *virtual* recipients" that need not exist as Unix
  accounts.

**Listing a domain in both is the classic silent-data-loss bug.**
`mydestination` is evaluated first, so the domain gets handled by the `local`
transport; Dovecot never sees the message; it lands in an mbox file nobody is
reading, or bounces with "User unknown in local recipient table" depending on
whether a same-named Unix user happens to exist. Mail does not error visibly —
it goes somewhere you are not looking. Here `amphive.app` is deliberately **only**
in `virtual_mailbox_domains`, and this was confirmed by reading the effective
`postconf -n` output rather than trusting the intent.

Note that `mydestination` still contains `$myhostname`, i.e. `mail.amphive.app`.
That is correct and intentional: mail addressed to the *host* (`root@mail.amphive.app`,
cron output, `logwatch`-style local notifications) should go through local
delivery and `/etc/aliases`, which is a genuinely different concern from user
mail for the *domain*. The two namespaces do not collide because they are
different names — `mail.amphive.app` is the host, `amphive.app` is the mail
domain.

### 3.2 `virtual_mailbox_maps`, and why rejecting early matters (backscatter)

```
virtual_mailbox_maps = hash:/etc/postfix/vmailbox
```

Contents (verified — this is the entire file):

```
support@amphive.app     amphive.app/support/
```

The map's job is to answer "does this mailbox exist?" *during the SMTP session*.
With `smtpd_reject_unlisted_recipient = yes` (verified effective value), a
`RCPT TO:<nosuchuser@amphive.app>` is rejected at that moment with a 5xx, and
the sending server — which is still connected and still owns the message — is
the party responsible for telling its user.

The alternative, and the reason this matters, is **backscatter**. If you accept
everything and only discover the mailbox is missing at delivery time, you have
taken ownership of a message you cannot deliver, and you are now obliged to
generate a bounce back to the envelope sender. But spam and phishing forge the
envelope sender essentially always. So your server ends up mailing unsolicited
bounce messages to innocent third parties whose addresses were forged — which is
indistinguishable from spamming them, because it *is* spamming them. Backscatter
sources get listed on blocklists like Backscatterer.org and UCEPROTECT, and once
listed, your legitimate mail stops being delivered.

The rule to remember: **decide before you accept.** A 5xx during the SMTP
transaction costs you nothing and makes the problem the sender's. A bounce
generated after acceptance is a message *you* are sending, with your reputation
attached.

Verified behaviour of the map itself (read-only `postmap -q` lookups):

```
vmailbox  support@amphive.app     -> amphive.app/support/
vmailbox  nosuchuser@amphive.app  -> (no match)   # => 5xx at RCPT TO
vmailbox  postmaster@amphive.app  -> (no match)   # resolved by the alias map instead, see §3.3
```

The value `amphive.app/support/` is a path relative to Dovecot's mail root. The
**trailing slash is load-bearing**: it means "Maildir format". Without it,
Postfix's own `virtual` delivery agent would interpret the path as an mbox file.
In this setup Postfix never delivers to the path itself — it hands off to LMTP
and Dovecot computes its own path from `mail_location` — but the convention is
kept so the two agree if the transport is ever changed.

### 3.3 `virtual_alias_maps` — RFC 2142 and the DMARC feedback loop

```
virtual_alias_maps = hash:/etc/postfix/virtual
```

Contents (verified — the entire file):

```
postmaster@amphive.app  support@amphive.app
abuse@amphive.app       support@amphive.app
dmarc@amphive.app       support@amphive.app
```

Alias expansion runs *before* mailbox lookup, which is why `postmaster@` resolves
even though it is absent from `vmailbox`: it is rewritten to `support@`, and
*that* address is in `vmailbox`. Both maps are consulted at RCPT time for the
unlisted-recipient check, so `postmaster@amphive.app` is accepted and
`nosuchuser@amphive.app` is not.

Three reasons these specific addresses exist:

- **`postmaster@` is mandatory.** RFC 5321 §4.5.1 requires every domain that
  accepts mail to accept mail for `postmaster`, and it must be deliverable
  *without* authentication and without spam filtering standing in the way. It is
  the defined out-of-band channel for another operator to tell you your server is
  broken. RFC 2142 defines the wider set of role addresses.
- **`abuse@` is how you find out you have been compromised.** If this host ever
  starts emitting spam, the reports arrive here. An operator who does not read
  `abuse@` finds out from a blocklist instead, days later.
- **`dmarc@` is the DMARC feedback sink.** The DMARC record in §7 carries
  `rua=mailto:dmarc@amphive.app`, and receiving domains send aggregate XML
  reports there. **If that address is not deliverable the DMARC record is
  defective** — you have asked the world to report to a black hole, and you lose
  the one feedback mechanism that tells you whether your SPF/DKIM alignment is
  actually working in the wild. This is a genuinely common self-inflicted wound.

All three currently funnel to the single real mailbox, which is the right shape
for a one-person project: role addresses that exist and are read beat role
addresses that are architecturally pure and ignored.

---

## 4. The anti-open-relay configuration

```
smtpd_relay_restrictions = permit_mynetworks, permit_sasl_authenticated, reject_unauth_destination
mynetworks               = 127.0.0.0/8 [::1]/128
smtpd_sasl_auth_enable   = no          # globally off; see §5
```

**An open relay is a mail server that will accept a message from anyone and
carry it to anyone.** The failure is that both the sender and the recipient are
strangers: you are not the sender's mail provider and you are not the
recipient's mail server, so you are performing a service for someone with no
relationship to you. Spammers scan the entire IPv4 space for these continuously,
and an open relay is found in hours, not weeks.

The three-clause rule is evaluated left to right and the first match wins:

1. `permit_mynetworks` — allow if the *client* is in `mynetworks`. This is set to
   loopback only, so it means "processes on this machine". It does **not**
   include the VM's LAN or the GCP internal range. A common and catastrophic
   mistake is leaving the Debian default, which includes the local subnet: on a
   cloud VM that can encompass other tenants' machines.
2. `permit_sasl_authenticated` — allow if the client proved it owns an account.
   This is the clause that lets a real user send mail through the server, and it
   is why authentication and relaying are the same question.
3. `reject_unauth_destination` — **otherwise, only accept mail whose recipient is
   a domain we are the final destination for** (`mydestination`,
   `virtual_mailbox_domains`, and the relay domains). Inbound mail from strangers
   to `support@amphive.app` matches this and is accepted. Mail from strangers to
   `someone@example.com` does not, and is refused.

Note that `smtpd_relay_restrictions` is a separate parameter from
`smtpd_recipient_restrictions` (verified empty here) precisely because Postfix
2.10 split them apart: too many people had built an open relay by editing a long
`smtpd_recipient_restrictions` list and accidentally dropping
`reject_unauth_destination` off the end. Keeping the anti-relay rule in its own
parameter makes it much harder to delete by accident. Leave it alone.

### Why this is the one mistake you cannot afford

Most misconfigurations here cost you a bounce or an afternoon. An open relay on
a cloud VM is different in kind:

- Your IP is on blocklists (Spamhaus SBL/CSS, SORBS, Barracuda) within hours,
  and delisting requires proving remediation.
- **Google will suspend the project, not just the VM.** GCP's acceptable-use
  enforcement acts at the project level, which on this box means the AmpHive
  production stack — backend, Postgres, MQTT broker, the whole thing — goes down
  alongside the mail experiment. The blast radius of a mail misconfiguration is
  the entire product.

That is worth stating plainly because it is the reason this server is worth
being careful with despite being "just a learning project": it is co-tenanted
with production.

### This is not hypothetical — it was probed within hours of going up

The mail log already contains a live open-relay attack. Between 09:38:01 and
09:38:19 on 2026-08-18, a single host (`31.70.85.152`,
`ip31-70-85-152.pbiaas.com`) opened **52 connections** and attempted **25**
relay deliveries to `yxt@outlook.fr`, rotating the envelope sender through
`news@`, `careers@`, `fax@`, `abuse@`, `hostmaster@`, `noreply@`, `pop3@`,
`sysadmin@`, `web@`, `www@googleusercontent.com`:

```
postfix/smtpd[3057014]: NOQUEUE: reject: RCPT from ip31-70-85-152.pbiaas.com[31.70.85.152]:
  554 5.7.1 <yxt@outlook.fr>: Relay access denied;
  from=<news@googleusercontent.com> to=<yxt@outlook.fr> proto=ESMTP helo=<WIN-SUAMFUP8VQA>
```

Every attempt was refused by `reject_unauth_destination`. Grep tally: **25 ×
`Relay access denied`, 0 accepted.** The `NOQUEUE:` prefix is the detail to
appreciate — the message never got a queue ID, because it was rejected at RCPT
TO before Postfix allocated one. Nothing was stored, nothing was bounced,
nothing was our problem.

This is the best possible evidence that the configuration works, and it arrived
unprompted on day one. It also calibrates expectations: a server on a public
IPv4 address on port 25 is under continuous automated attack, forever.

---

## 5. Submission on 587, and why AUTH is disabled globally

The `submission` service in `master.cf` (verified via `postconf -M`/`-P`) sets
per-service overrides:

```
submission inet n - y - - smtpd
    -o syslog_name=postfix/submission
    -o smtpd_tls_security_level=encrypt
    -o smtpd_sasl_auth_enable=yes
    -o smtpd_sasl_type=dovecot
    -o smtpd_sasl_path=private/auth
    -o smtpd_client_restrictions=permit_sasl_authenticated,reject
    -o smtpd_relay_restrictions=permit_sasl_authenticated,reject
    -o milter_macro_daemon_name=ORIGINATING
```

The service is named `submission`, not `587`; Postfix resolves that through
`/etc/services`, where `submission 587/tcp` is defined (verified). The listener
is confirmed on `0.0.0.0:587`.

**Ports 25 and 587 are different protocols with the same syntax.** Port 25 is
*server-to-server transfer*: strangers deliver mail addressed to you, and
authentication is meaningless because Gmail does not have an account here. Port
587 is *client submission* (RFC 6409): your own users hand you mail addressed to
the outside world, and authentication is the entire point. Conflating them is
how relays get opened.

That distinction drives the AUTH decision. `smtpd_sasl_auth_enable = no` is set
**globally** and re-enabled **only** on the submission service. The reasoning:

- Port 25 has no legitimate use for AUTH, so offering it there is pure attack
  surface — an `AUTH` capability on a public MX is an open invitation to
  credential-stuffing, and every failed attempt costs CPU and log volume.
- A per-service opt-in fails safe. If someone later adds a new listener and
  forgets to think about auth, it inherits `no`. Had the global default been
  `yes` with per-service opt-*out*, the same mistake would expose credentials.

**Verified from an off-VM network — the decisive test.** EHLO on port 25, both
before and after STARTTLS:

```
220 mail.amphive.app ESMTP
250-PIPELINING / SIZE 26214400 / ETRN / STARTTLS / ENHANCEDSTATUSCODES
250-8BITMIME / DSN / SMTPUTF8 / CHUNKING          <-- no AUTH, in either phase
```

and on 587 *inside* the TLS session:

```
250-AUTH PLAIN LOGIN                               <-- present only after STARTTLS
```

So AUTH is genuinely absent from port 25 even once encrypted, and genuinely
present on 587. `PLAIN` and `LOGIN` are the only mechanisms, matching Dovecot's
`auth_mechanisms = plain login`; both send the password in the clear *within*
the TLS tunnel, which is standard and safe here because the tunnel is
mandatory.

**Why AUTH is invisible on 587 before STARTTLS:** it is `smtpd_tls_security_level
= encrypt` on that service, which makes TLS mandatory and causes Postfix to
withhold AUTH until the session is encrypted. It is worth being precise about
the mechanism, because the parameter usually credited with this —
`smtpd_tls_auth_only` — is **`no` on this host** (verified). If you ever relax
the submission service to `may`, you would need to set `smtpd_tls_auth_only=yes`
explicitly or credentials could be solicited in cleartext.

Two further notes:

- `smtpd_client_restrictions=permit_sasl_authenticated,reject` on submission
  means an unauthenticated client is dropped early in the session, not merely
  refused at RCPT. Nothing anonymous has any business on 587.
- `milter_macro_daemon_name=ORIGINATING` is the signal to OpenDKIM that mail
  arriving here is *ours* and should be **signed**, as opposed to inbound mail on
  25 which should merely be *verified*. Without it the milter cannot distinguish
  the two directions.
- **Port 465 (implicit TLS, "smtps") is not enabled** — verified closed from
  outside. Some mail clients prefer it. Adding it is a `master.cf` entry with
  `smtpd_tls_wrappermode=yes`; it is simply not done yet.

---

## 6. TLS posture — and why a snakeoil cert is still worth having

**Current certificate (verified):**

```
subject = CN = amphive-relay
issuer  = CN = amphive-relay          <-- self-signed
notBefore = Aug 18 09:18:58 2026 GMT
notAfter  = Aug 15 09:18:58 2036 GMT
```

This is Debian's `ssl-cert` package "snakeoil" certificate, used by both Postfix
(`smtpd_tls_cert_file`) and Dovecot (`ssl_cert`). **This is a known temporary
state**, and note that its CN is `amphive-relay` — the internal hostname, not
`mail.amphive.app`. It would fail hostname verification even if it were trusted.

Two different security levels are in play, and the asymmetry is deliberate:

**Port 25: `smtpd_tls_security_level = may` (opportunistic).** The server offers
STARTTLS; a sender that supports it uses it, a sender that does not still gets
its mail through in cleartext. This looks like weak security and is in fact the
only defensible setting for a public MX. SMTP has no mechanism for a sending
server to know in advance that you require encryption (absent MTA-STS or DANE,
neither of which is deployed here), so setting `encrypt` on port 25 does not
upgrade those senders — **it makes their mail bounce**. You would be silently
unreachable from a slice of the internet. The correct trade is: take encryption
whenever the peer offers it, never refuse mail for lack of it.

**Why a self-signed cert still beats no cert.** Sending MTAs doing opportunistic
TLS do not validate the certificate — they cannot, since a failed validation
would mean refusing mail, which is exactly the outcome `may` exists to avoid.
So in practice the snakeoil cert still buys real protection against **passive**
interception: a network observer between Gmail and this host sees a TLS stream
rather than plaintext headers, bodies, and addresses. It buys nothing against an
**active** man-in-the-middle, who can substitute their own certificate and go
unnoticed. Opportunistic TLS is a defence against surveillance, not against
attack — and that is still worth having, since the traffic would otherwise be
readable by every hop in between.

**Port 587 and IMAPS 993 are a different matter.** There, a real client with a
real password is connecting, the client *does* validate the certificate, and a
self-signed cert produces either a scary warning or an outright refusal. Any mail
client configured against this server today must be told to trust an unverified
certificate, which trains the user to click through exactly the warning that
protects their password. **This is the most user-visible defect on the box** and
the first thing to fix.

**How to fix it (path identified, not yet executed).** The obvious approach —
`certbot --standalone` — will fail on this host, because Caddy (in Docker) holds
both port 80 and port 443:

```
amphive-relay-caddy-1   caddy:2-alpine   0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
```

Caddy already holds real Let's Encrypt certificates for `amphive.app`,
`cpo.amphive.app`, and the sslip fallback (verified in the
`amphive-relay_caddydata` volume under
`/data/caddy/certificates/acme-v02.api.letsencrypt.org-directory/`), but **not**
for `mail.amphive.app`. The clean path is therefore to add `mail.amphive.app`
to the Caddyfile once its A record exists, let Caddy obtain and auto-renew the
cert as it already does for the other hostnames, and point
`smtpd_tls_cert_file`/`ssl_cert` at the copy in the Caddy data volume — with a
renewal hook to reload Postfix and Dovecot, since neither picks up a changed
certificate file on its own. This is a design sketch based on verified facts
about the host; **it has not been implemented or tested.**

---

## 7. DNS records — what each one is for

**None of these are published yet** (verified §1). This section is the
specification for work still to be done, and the explanation of why each record
exists.

| Type | Name | Value | Job |
|---|---|---|---|
| A | `mail.amphive.app` | `136.117.94.209` | resolves the mail host itself |
| MX | `amphive.app` | `10 mail.amphive.app.` | tells the world where to deliver |
| TXT | `amphive.app` | `v=spf1 mx ~all` | who may send as this domain |
| TXT | `mail._domainkey.amphive.app` | `v=DKIM1; h=sha256; k=rsa; p=<public key>` | signature verification key |
| TXT | `_dmarc.amphive.app` | `v=DMARC1; p=none; rua=mailto:dmarc@amphive.app` | policy + feedback |

**A record.** MX records must point at a hostname with an address record, and
[RFC 2181 §10.3] forbids an MX pointing at a CNAME. Hence a dedicated A record
rather than an alias to the apex.

**MX record.** The indirection layer described in §2 step 1. The `10` is a
preference value — lower is preferred, and it only matters when there are
several. Trailing dot matters in zone files (it makes the name absolute); most
DNS web consoles add it for you.

**SPF** publishes which IP addresses are allowed to send mail bearing this
domain in the envelope sender. `v=spf1 mx ~all` reads as: "the hosts listed in
my MX records may send; everything else is a soft fail". Using the `mx` mechanism
rather than a literal IP means the record stays correct if the mail host moves —
one less thing to forget.

The qualifier on `all` is the interesting choice. `~all` (**softfail**) says
"this is probably not me, but do not reject on that basis alone". `-all`
(**hardfail**) says "reject it". **Start at `~all`** because SPF's failure mode
is asymmetric and unforgiving: the moment you publish `-all`, any legitimate
sending path you forgot about — a monitoring service, a mailing list that
forwards without rewriting the envelope, a form handler, the AmpHive backend's
own Gmail relay — starts getting its mail rejected, and you find out from users
rather than from logs. Softfail lets those failures be *reported* (through DMARC
aggregate reports) instead of *enforced*, so you can discover the full inventory
of things that legitimately send as you before you start blocking. Tighten to
`-all` only once reports have been clean for a few weeks.

Note that SPF checks the **envelope** sender (`MAIL FROM`), not the `From:`
header the user sees. That gap is precisely what DMARC exists to close.

**DKIM** publishes the public half of the signing key, so a receiver can verify
the `DKIM-Signature` header that OpenDKIM adds. Retrieve the exact record —
including the quoted-string splitting that keeps each chunk under the 255-byte
TXT limit — from the file OpenDKIM generated:

```bash
sudo cat /etc/opendkim/keys/amphive.app/mail.txt
```

It has the form (public key elided here; it is 2048-bit RSA, verified):

```
mail._domainkey  IN  TXT  ( "v=DKIM1; h=sha256; k=rsa; "
    "p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0iHM81rz1navrPi4Md5DVDZ...<truncated>...IDAQAB" )
```

The `mail` label is the **selector**, which is why multiple keys can coexist on
one domain and why key rotation is possible without downtime: publish a second
selector, switch signing to it, then retire the first. Never paste the contents
of `mail.private` anywhere — that file is the signing key, is mode `0600
opendkim:opendkim`, and must not leave the host.

**DMARC** ties the other two together. SPF and DKIM each authenticate something
the user never sees (the envelope sender; the signing domain). DMARC checks that
one of them **aligns** with the visible `From:` header domain, and publishes what
receivers should do when neither does.

`p=none` is monitor-only: it changes no delivery decisions and asks receivers to
send reports. **Start here, always.** A domain that jumps straight to
`p=reject` before its authentication is provably correct simply stops being able
to send mail, and the diagnosis arrives as "why is nobody getting my email"
rather than as a log line. The progression is `p=none` → read the aggregate
reports until every legitimate source shows as passing and aligned →
`p=quarantine` → `p=reject`. The `rua=mailto:dmarc@amphive.app` is what makes
the first step useful, which is why that alias must be deliverable (§3.3).

**Reverse DNS — a gap, and a nuance.** The PTR for `136.117.94.209` is
`209.94.117.136.bc.googleusercontent.com` (verified), the GCP-generic default;
`publicPtrDomainName` is unset on the instance. Many receivers penalise or
reject mail from a host whose forward and reverse DNS do not match (FCrDNS).
GCP does allow a custom PTR on a *static reserved* external IP, and this IP is
reserved (`amphive-relay-ip`, `IN_USE`). **However, this matters far less here
than it would elsewhere:** because outbound mail must leave via a smarthost
(§0), the IP that receiving servers actually see and judge is the *smarthost's*,
not ours. Our PTR is close to irrelevant for deliverability as long as the port
25 block stands.

---

## 8. File, port and service inventory

All entries verified on 2026-08-18.

### Listening ports

| Port | Bind | Process | Purpose | Reachable from internet |
|---|---|---|---|---|
| 25 | `0.0.0.0` | Postfix `master`→`smtpd` | inbound SMTP (MX) | **yes** (tested) |
| 587 | `0.0.0.0` | Postfix `master`→`smtpd` | submission, TLS + AUTH required | **yes** (tested) |
| 993 | `0.0.0.0`, `[::]` | Dovecot | IMAPS | **yes** (tested) |
| 8891 | `127.0.0.1` | OpenDKIM | milter, loopback only | no (correct) |
| 143 / 110 / 995 / 465 | — | — | plaintext IMAP, POP3, POP3S, smtps | **not listening** (tested closed) |

Ports 143/110/995 are disabled in Dovecot with `port = 0` rather than by
firewall, which is the stronger form: the daemon never opens the socket, so a
firewall change cannot accidentally expose plaintext authentication. POP3 is
disabled entirely (both `pop3` and `pop3s` are `port = 0`, and `pop3` is absent
from `protocols`) — there is no reason to run a second, worse retrieval protocol
that defaults to deleting server-side mail.

GCP ingress rule `allow-amphive-mail`: `tcp:25,tcp:587,tcp:993` from
`0.0.0.0/0`, no target tags, enabled.

Postfix listens IPv4-only (`inet_protocols = ipv4`), consistent with the VM
having no global IPv6 address. Dovecot binds 993 on both families; the v6
listener is unreachable in practice.

### Files

| Path | Owner/mode | Contents |
|---|---|---|
| `/etc/postfix/main.cf` | root | global Postfix config |
| `/etc/postfix/master.cf` | root | service/listener definitions incl. `submission` |
| `/etc/postfix/virtual` + `.db` | `root 644` | alias map (postmaster/abuse/dmarc → support) |
| `/etc/postfix/vmailbox` + `.db` | `root 644` | mailbox existence map |
| `/etc/dovecot/dovecot.conf` (+ `conf.d/`) | root | Dovecot config |
| `/etc/dovecot/users` | `root:dovecot 640` | passwd-file passdb, SHA512-CRYPT hashes |
| `/etc/opendkim.conf` | root | milter config |
| `/etc/opendkim/KeyTable` | root | selector → domain:selector:keyfile |
| `/etc/opendkim/SigningTable` | root | sender pattern → selector |
| `/etc/opendkim/TrustedHosts` | root | `127.0.0.1`, `localhost`, `::1` |
| `/etc/opendkim/keys/amphive.app/mail.private` | `opendkim 600` | **private signing key — never copy or publish** |
| `/etc/opendkim/keys/amphive.app/mail.txt` | `opendkim 600` | the public DNS record to publish |
| `/var/mail/vhosts/<domain>/<user>/` | `vmail:vmail` | maildir store |
| `/var/log/mail.log` (+ rotations) | `root:adm 640` | all mail logging |

The `.db` files are Berkeley DB compilations of the plaintext maps. **Postfix
reads the `.db`, not the text file** — editing `virtual` or `vmailbox` without
running `postmap` on it changes nothing, and is a standard way to lose an hour.

### Services

Postfix on Debian runs as a systemd *instance*: `postfix@-.service` does the
work and `postfix.service` is a wrapper that starts all instances. Reload with
`systemctl reload postfix`; both units respond. All three of `postfix`,
`dovecot`, `opendkim` are `active` and `enabled`.

### Users

`vmail` = uid **5000**, gid **5000**, home `/var/mail/vhosts`, shell
`/usr/sbin/nologin` (verified). Every virtual mailbox is owned by this one
account.

**Why virtual users instead of system users.** A system-user setup gives every
mailbox owner a line in `/etc/passwd`, which means: creating a mailbox requires
`useradd` and root; the mailbox namespace collides with the login namespace (you
cannot have a mailbox named `backup` or `www-data`, and adding a mailbox called
`admin` may create a real account); a compromised mail password becomes a
foothold on the OS; and the mail store is scattered across home directories with
per-user ownership. Virtual users are just rows in a lookup table — adding one
is a text edit, they cannot log in, they have no shell, and the entire store
sits under one uid so it can be backed up, moved, or chowned as a unit. The
`nologin` shell is the belt-and-braces: even if something contrives to
authenticate as `vmail`, there is nothing to get.

**Cosmetic finding:** `/var/mail/vhosts/` contains `.bashrc`, `.profile` and
`.bash_logout`, because `useradd` populated the skeleton files into what became
the mail root. Harmless (Dovecot ignores dotfiles at that level) but untidy;
they could be removed.

---

## 9. Verification — exact commands

Run these after any change. Commands needing root are shown with `sudo`; note
that `postconf`, `postqueue` and friends live in `/usr/sbin`, which is not on a
non-root user's `PATH` on Debian — use full paths or `sudo`.

**Config sanity, without restarting anything:**

```bash
sudo /usr/sbin/postfix check          # syntax + permissions; silent = good
sudo /usr/sbin/postconf -n            # effective non-default settings
sudo /usr/sbin/postconf -M            # every listener defined in master.cf
sudo /usr/sbin/postconf -P            # per-service overrides (the submission block)
sudo doveconf -n                      # same idea for Dovecot
```

**Are the daemons up and listening:**

```bash
systemctl is-active postfix dovecot opendkim
sudo ss -lntp | grep -E ':(25|587|993|8891)\b'
```

Expect 25/587 on `master`, 993 on `dovecot`, 8891 on `opendkim` **bound to
127.0.0.1**. If 8891 is on `0.0.0.0` you have exposed a milter to the internet —
fix immediately.

**Do the maps resolve** (read-only; queries the compiled `.db`):

```bash
sudo /usr/sbin/postmap -q support@amphive.app     hash:/etc/postfix/vmailbox   # -> amphive.app/support/
sudo /usr/sbin/postmap -q postmaster@amphive.app  hash:/etc/postfix/virtual    # -> support@amphive.app
sudo /usr/sbin/postmap -q nosuchuser@amphive.app  hash:/etc/postfix/vmailbox   # -> (no output, exit 1)
```

An empty result for a mailbox you believe exists almost always means you edited
the text file and forgot `sudo postmap /etc/postfix/vmailbox`.

**What the outside world actually sees** — the important one, because everything
above inspects the server's opinion of itself. From a machine that is *not* the
VM:

```bash
# capability list on the public MX — there must be NO "AUTH" line here
openssl s_client -connect mail.amphive.app:25 -starttls smtp -crlf
# submission — "250-AUTH PLAIN LOGIN" must appear, and only after STARTTLS
openssl s_client -connect mail.amphive.app:587 -starttls smtp -crlf
# IMAPS
openssl s_client -connect mail.amphive.app:993
```

(Until DNS exists, substitute `136.117.94.209` for the hostname. The certificate
will not verify — see §6.)

**Prove you are not an open relay.** This is the test that matters most, and it
must be run from off the VM, because from loopback `permit_mynetworks` will let
you relay and you will "prove" the opposite of what you meant:

```
telnet 136.117.94.209 25
EHLO probe.example.com
MAIL FROM:<test@example.com>
RCPT TO:<someone@example.net>          <-- a domain we are NOT final destination for
```

The required answer is `554 5.7.1 <someone@example.net>: Relay access denied`.
**Any 2xx here is a five-alarm emergency** — stop the service and fix it before
doing anything else. Follow with `QUIT`; never send `DATA`.

**DKIM key and DNS agreement:**

```bash
sudo opendkim-testkey -d amphive.app -s mail -vvv
```

This reads the private key, derives the public half, fetches the published TXT
record and checks they match. **Current output (2026-08-18):**

```
opendkim-testkey: checking key 'mail._domainkey.amphive.app'
opendkim-testkey: 'mail._domainkey.amphive.app' record not found
```

which is correct and expected, because the record has not been published (§7).
After publishing, the success line is `key OK`. A `key not secure` warning
merely means the zone is not DNSSEC-signed and is not a problem.

**Prove signing works end to end** by inspecting a delivered message rather than
trusting the log. A message delivered on 2026-08-18 carried:

```
DKIM-Signature: v=1; a=rsa-sha256; c=relaxed/simple; d=amphive.app; s=mail;
        t=1787046715; bh=aMDepTS2cqcX4ZpH2Ey/mLbGB+sU4c3MjHnzbT8BtgI=;
        h=Subject:Date:From:From; b=vLBS+ToBAVSV5XtBZ3DtjXygzKqY6Xatbid/...
```

Every field is checkable against the config: `c=relaxed/simple` matches
`Canonicalization`, `d=`/`s=` match the KeyTable, `a=rsa-sha256` matches
`h=sha256` in the DNS record. The duplicated `From` in the `h=` list is
`OversignHeaders From` at work — see §10.

**Read the delivered mail:**

```bash
sudo ls -l /var/mail/vhosts/amphive.app/support/new/
sudo find /var/mail/vhosts -type f -name '*.amphive-relay*' | wc -l
```

Files in `new/` are unread; Dovecot moves them to `cur/` once a client has seen
them.

---

## 10. OpenDKIM specifics

```
Canonicalization  relaxed/simple
Mode              sv
SubDomains        no
OversignHeaders   From
Socket            inet:8891@127.0.0.1
KeyTable          /etc/opendkim/KeyTable
SigningTable      refile:/etc/opendkim/SigningTable
UserID            opendkim
```

**KeyTable/SigningTable rather than the single-domain shorthand.** OpenDKIM
supports a terse form (`Domain`, `Selector`, `KeyFile` as three scalars) that is
shorter for one domain and a dead end for two. The table form used here:

```
# KeyTable      — names a key:  <name>  <domain>:<selector>:<path>
mail._domainkey.amphive.app amphive.app:mail:/etc/opendkim/keys/amphive.app/mail.private

# SigningTable  — routes senders to keys:  <pattern>  <keyname>
*@amphive.app mail._domainkey.amphive.app
```

Adding a second domain is now exactly two lines plus a keypair, with no
restructuring. `refile:` on the SigningTable enables regex/wildcard patterns,
which is what makes `*@amphive.app` work.

**`Mode sv`** = sign outbound **and** verify inbound. Verification only adds an
`Authentication-Results` header; it never rejects, because rejection policy
belongs to a spam filter (§11).

**Canonicalization `relaxed/simple`** — header/body respectively. Canonicalization
determines how much incidental mangling a signature survives. `simple` tolerates
nothing; `relaxed` normalises whitespace and header case. Headers get `relaxed`
because intermediate hops routinely rewrap and re-case them, which would break a
`simple` header signature; bodies get `simple` because it is stricter and body
text is far less likely to be touched in transit. `relaxed/simple` is the common
pragmatic default.

**`OversignHeaders From`** is a subtle but important anti-abuse measure. A DKIM
signature covers the headers named in `h=`. If a header is listed once and
present once, an attacker who can inject a *second* `From:` header may get a
message that still verifies while displaying a different sender — the signature
covers the original, the mail client shows the injected one. Oversigning lists
the header **one more time than it appears** (hence `h=Subject:Date:From:From`
in the real signature above), which asserts "there is exactly this many of this
header". Adding another `From:` breaks the signature. This is the defence
against DKIM header-injection spoofing, and it is cheap.

**`milter_default_action = accept` — a deliberate availability choice.**
Postfix consults OpenDKIM over TCP. If OpenDKIM is stopped, crashed, or
mid-restart, this parameter decides what Postfix does with the message. The
alternatives are `reject` or `tempfail`; either would mean **a dead milter
becomes a mail outage**, turning a signing-daemon crash into "the domain stops
receiving mail". With `accept`, a dead milter degrades to "outbound mail goes
out unsigned and inbound mail is not annotated" — a deliverability and
observability problem, not an availability one. That is the right trade for a
non-critical signing function. It would be the wrong trade for a milter enforcing
a security policy, where fail-open defeats the purpose.

**Socket on loopback.** `inet:8891@127.0.0.1` — a milter port accepts and
processes messages, so exposing it publicly would be a serious hole. Verified
bound to 127.0.0.1 only.

> **Landmine — a stale config file that currently does nothing.**
> `/etc/default/opendkim` contains `SOCKET=local:$RUNDIR/opendkim.sock`, which
> **contradicts** the `Socket inet:8891@127.0.0.1` in `/etc/opendkim.conf`. If
> that setting were in effect, OpenDKIM would listen on a UNIX socket, Postfix
> would find nothing on 8891, and — because of `milter_default_action = accept`
> — **mail would keep flowing, silently unsigned**. A failure with no error.
>
> It is **not** in effect, verified three ways: the systemd unit is
> `ExecStart=/usr/sbin/opendkim` with **no `EnvironmentFile`** (so
> `/etc/default/opendkim` is never read — it is sysvinit-era leftover);
> `ss -lxp` shows **no** opendkim UNIX socket; and `ss -lntp` shows opendkim on
> `127.0.0.1:8891`. The conf file wins. But if signing ever mysteriously stops
> after a package upgrade that reinstates an `EnvironmentFile`, **look here
> first.** Deleting or correcting the stale line would remove the trap.

---

## 11. Troubleshooting

**Where the logs are.** `/var/log/mail.log` (plus `mail.log.1`, `.2.gz`…
rotated) carries everything from Postfix, Dovecot and OpenDKIM. The same records
are in the journal:

```bash
sudo tail -f /var/log/mail.log
sudo journalctl -u postfix -u postfix@- -u dovecot -u opendkim -f
```

**Follow one message.** Every message gets a queue ID (e.g. `2B9E36B380`) at
`cleanup` time, and every subsequent line about it carries that ID. That is the
key to reading mail logs — grep the ID, not the address:

```bash
sudo grep '2B9E36B380' /var/log/mail.log
```

A complete successful delivery ends in `status=sent` followed by `removed`. If
you never see `removed`, the message is still queued.

**Read the queue.**

```bash
sudo /usr/sbin/postqueue -p     # human-readable listing; "Mail queue is empty" is the healthy state
sudo /usr/sbin/postqueue -f     # flush: retry every deferred message now
sudo /usr/sbin/postcat -q <ID>  # dump a queued message, headers and body
sudo /usr/sbin/postsuper -d <ID>  # delete one message   (-d ALL empties the queue)
```

`postqueue -p` output has a fifth column giving the **reason** a message is
deferred, which is where the actual diagnosis lives. Interpreting the common
ones on *this* host:

- `Connection timed out` to a remote MX on port 25 → **this is the GCP egress
  block (§0), not a network fault.** No amount of retrying will fix it. The
  message will bounce after ~5 days. This will be the single most common
  deferral until a smarthost is configured.
- `Host or domain name not found` → DNS. Check `getent hosts <name>` on the VM.
- `Connection refused` to `private/dovecot-lmtp` → Dovecot is down, or its LMTP
  socket is missing from `/var/spool/postfix/private/`. Because Postfix is
  chrooted, the socket must exist *inside* the chroot; check
  `sudo ls -l /var/spool/postfix/private/dovecot-lmtp`.

Current state: **queue empty** (verified).

**Mail is accepted but never appears in the mailbox.** The classic cause is the
`mydestination` collision in §3.1 — check `postconf -n | grep -E 'mydestination|virtual_mailbox_domains'` and confirm the domain appears in exactly
one. Then check `/var/mail/` for an unexpected mbox file, which is where it will
have gone.

**Mail is rejected as "User unknown".** The recipient is missing from both
`virtual_alias_maps` and `virtual_mailbox_maps` — or, far more often, present in
the text file but not in the compiled `.db`. Run `postmap` and re-test with
`postmap -q`.

**Authentication fails on 587 or 993.** Postfix does not check passwords itself;
it delegates over the socket at `/var/spool/postfix/private/auth`, which Dovecot
creates. Check that socket exists and is `postfix:postfix 0660`, then test
Dovecot's side in isolation:

```bash
sudo doveadm auth test support@amphive.app     # prompts for the password
```

If `doveadm auth test` passes but SMTP AUTH fails, the problem is the socket or
the chroot, not the credentials.

**DKIM signatures missing.** In order: is `opendkim` running; is it on 8891
(`ss -lntp`); does the log say `DKIM-Signature field added`; does
`SigningTable` match the sender's domain; is the message arriving on a service
tagged `ORIGINATING` (§5) — mail submitted on port 25 from a stranger is *not*
signed, by design. Remember `milter_default_action = accept` means a dead milter
produces **no error at all**, just unsigned mail.

**Testing changes safely.** `postfix check` validates config without applying
it, and `systemctl reload postfix` re-reads config without dropping connections.
Prefer reload to restart. Changes to `master.cf` (new listeners) do require a
restart.

---

## 12. Known gaps / not done yet

Honest inventory as of 2026-08-18. Nothing in this list is scheduled; this is a
learning build and the gaps are as instructive as the working parts.

1. **DNS records are not published** — no A, MX, SPF, DKIM or DMARC (§1, §7).
   **This is the blocker.** Until at least A + MX exist, the server cannot
   receive mail from the internet, and the entire inbound path in §2 remains
   theoretical: it has been verified only via locally-submitted mail.
2. **No real TLS certificate** — self-signed snakeoil with the wrong CN (§6).
   Inbound opportunistic TLS on 25 is unaffected in practice, but IMAPS and
   submission clients must be told to trust an unverified certificate. Path
   identified (piggyback on the existing Caddy ACME setup) but not implemented.
3. **Outbound relays through a consumer Gmail account - MEASURED, and it
   works.** GCP blocks egress on 25 (SS0), so `relayhost = [smtp.gmail.com]:587`
   with `sasl_passwd` credentials is the only way out. Two questions mattered
   and both are now answered empirically rather than assumed:

   - **Does the relay rewrite `From`?** It did, until `support@amphive.app`
     was verified as a Gmail "Send mail as" alias. Before verification the
     header arrived as `From: sjgotnfts1@gmail.com` with
     `X-Google-Original-From` recording the replacement; after verification the
     `From` survives intact and that header is gone. The verification code was
     itself delivered to this server's own maildir - the mail server
     bootstrapped its own credential.
   - **Does our DKIM signature survive the relay?** **Yes.** Confirmed by
     Port25's independent verifier on the real path (Postfix -> OpenDKIM ->
     relay): `DKIM check: pass (matches From: support@amphive.app)`,
     `header.d=amphive.app`, verified against
     `mail._domainkey.amphive.app (2048 bits)`. Google *adds* its own
     `X-Google-DKIM-Signature` (`d=1e100.net`) rather than replacing ours.

   **Net DMARC result: PASS.** SPF passes but is NOT aligned (the envelope
   sender stays `sjgotnfts1@gmail.com`, so SPF authenticates gmail.com); DKIM
   passes AND is aligned with the From domain. DMARC requires only one of the
   two to pass and align, so mail from `support@amphive.app` is authenticated
   as ours. A transactional provider is therefore NOT required for alignment -
   it would only be needed to additionally align SPF, which needs an envelope
   sender in our own domain.

   CAVEAT: this rests on a consumer Gmail account, whose sending limits
   (roughly 500 recipients/day) and terms are not designed to be a service's
   MTA. Fine for a portfolio project; not something to build a product on.
4. **Spam filtering is installed but has never seen internet mail.** rspamd
   is present and running (`/usr/bin/rspamd`, listening on 127.0.0.1:11332 for
   the milter protocol plus 11333/11334), backed by redis, and chained into
   Postfix *after* OpenDKIM: `smtpd_milters = inet:127.0.0.1:8891,
   inet:127.0.0.1:11332`. Order is deliberate — OpenDKIM signs first, so rspamd
   scores the finished message.

   Two real caveats:

   - **It has scored nothing yet.** rspamd sits on `smtpd_milters`, which only
     applies to mail arriving over SMTP. The only messages delivered so far were
     injected locally with `sendmail`, and those traverse `non_smtpd_milters`
     (OpenDKIM alone, deliberately). Until the MX record exists and a real
     message arrives from outside, rspamd's configuration is untested against
     anything.
   - **Thresholds are set conservatively and unvalidated**: greylist 4,
     add_header 6, reject 15 (`/etc/rspamd/local.d/actions.conf`). `reject` is
     high on purpose — a false positive on a *rejected* message is mail that
     silently never arrives, which is worse than spam landing in Junk. Whether
     those numbers suit this domain's real traffic is unknown, because there is
     no real traffic.

   rspamd's own DKIM *signing* module is explicitly disabled
   (`/etc/rspamd/local.d/dkim_signing.conf`, `enabled = false`) so it cannot
   race OpenDKIM into adding a second `DKIM-Signature` header. rspamd's DKIM
   handling here is verification only, which is what feeds its scoring.
<!-- CORRECTION 2026-08-18: gaps 4 and 5 originally recorded rspamd and
     fail2ban as absent. They were observed mid-build, minutes before both
     were installed. Re-verified on the VM (rspamd active on 11332/11333/
     11334; fail2ban active with 4 jails; milter chain carrying both) and
     rewritten. The rest of the document's verified claims are unaffected. -->

5. **fail2ban is now installed; connection rate limiting is still absent.**
   The attacker recorded in §4 made 52 connections in 18 seconds and was neither
   throttled nor banned — only refused, over and over. That specific hole is
   closed: fail2ban is active with four jails (`sshd`, `postfix`,
   `postfix-sasl`, `dovecot`), `bantime` 1h / `findtime` 10m / `maxretry` 5,
   with `ignoreip` covering loopback and the VPC range so the box cannot ban
   itself. The `sshd` jail is deliberately lenient (`maxretry = 10`): SSH here
   is key-only so repeated failures are bots rather than the operator, but the
   cost of a false positive is losing access to the machine.

   Still absent, and worth knowing the difference: fail2ban reacts *after* a
   pattern of failures appears in the log, whereas Postfix's own
   `smtpd_client_connection_rate_limit` / `anvil` controls would refuse the
   51st connection in the same second. Nothing here does the latter. `smtpd_recipient_restrictions` and `smtpd_sender_restrictions`
   are both empty (verified), so there is no DNSBL check, no
   `reject_unknown_reverse_client_hostname`, and no HELO validation beyond
   requiring that HELO be sent at all. Postfix's own `anvil` is collecting rate
   statistics but no limits are enforced.
6. **No backups of the mail store.** `/var/mail/vhosts/` is not covered by the
   existing `backup_db.sh` regime, which handles Postgres only. A VM loss loses
   all mail. `/etc/dovecot/users` — the only copy of the mailbox credentials — is
   likewise unbacked.
7. **No monitoring or alerting.** Nothing watches queue depth, service liveness,
   certificate expiry, or the `abuse@` mailbox. If Postfix stops, the first
   symptom is silence, and silence is indistinguishable from "nobody emailed me".
   For a server whose whole job is delivering notifications, this is the most
   ironic gap on the list.
8. **Reverse DNS is the GCP default** (§7). Low priority precisely because
   outbound must go via a smarthost, whose IP is the one that gets judged.
9. **Single mailbox, single domain.** One account (`support@amphive.app`). The
   KeyTable/SigningTable and virtual-user structure were chosen so that growing
   past this is a data change rather than a redesign, but that has never been
   exercised.
10. **Port 465 (implicit TLS submission) not offered** (§5) — some clients
    prefer it.
11. **No Sieve filtering.** Pigeonhole 0.5.19 is present on the box (it ships
    with Dovecot) but `sieve` is not in `protocols`, so no server-side rules run.

---

## 13. Verified vs. assumed

In keeping with the rest of `deploy/docs/`, an explicit account of what this
document knows versus what it believes.

**Verified against the live VM or from off-host probes on 2026-08-18:** package
versions (Postfix 3.7.11, Dovecot 2.3.19.1, OpenDKIM 2.11.0~beta2) and that
exim4 is in `rc` (removed, config remaining) state, confirming it was displaced;
the complete effective Postfix config via `postconf -n`/`-M`/`-P` and the
specific defaults `smtpd_reject_unlisted_recipient=yes`, `smtpd_tls_auth_only=no`,
`smtpd_recipient_restrictions=` and `default_transport=smtp`; the full contents
of `virtual` and `vmailbox` and their behaviour under `postmap -q`; the complete
`doveconf -n`; `vmail` being uid/gid 5000; the maildir tree and two really-delivered
messages including one message's full header block; OpenDKIM's config, KeyTable,
SigningTable, TrustedHosts, key file permissions, and that the key is 2048-bit
RSA; listening sockets and service enable/active state; the GCP ingress rule and
the reserved static IP; the snakeoil certificate's subject, issuer and dates; the
absence of certbot and `sasl_passwd`; Caddy's container port
bindings and its existing Let's Encrypt certificate inventory; the empty mail
queue; `opendkim-testkey` output; the open-relay attack in the logs with its
exact reject counts; the DNS state of all five mail records queried against
`8.8.8.8`; the PTR record; inbound reachability of 25/587/993 and closure of
465/143/110 from an external network; and the EHLO capability lists on 25 and
587 both before and after STARTTLS.

**The GCP port 25 block was verified empirically**, not assumed, with the
same-IP/different-port controls in §0. What is *taken on trust from Google's
published documentation* is the claim that the block is permanent and
non-exemptible — that is policy, and policy cannot be measured from a socket.
The measurement establishes only that it is blocked today, comprehensively, from
this VM.

**Assumed, inferred, or stated but not tested:**

- **The entire inbound-from-internet path.** No mail has ever arrived from a
  third-party server, because there is no MX record. §2 steps 1–5 are derived
  from the verified configuration and from protocol behaviour, not observed. The
  first real inbound message may surface problems this document does not
  anticipate.
- **That `support@amphive.app` can actually authenticate on 993/587.** The
  passdb entry exists and the `AUTH PLAIN LOGIN` capability is advertised inside
  TLS, but no login was performed — that would require the mailbox password,
  which is deliberately not in this document and was not retrieved.
- **The Caddy-based certificate plan in §6** is a design sketch grounded in
  verified facts (Caddy owns 80/443, holds LE certs for other names, no cert for
  `mail.amphive.app`). It has not been implemented or tested, and the
  reload-on-renewal hook it needs does not exist.
- **`maximal_queue_lifetime` = 5 days** is Postfix's documented default; it is
  not set explicitly here and the value was not read back.
- **Deliverability claims generally** — how receivers treat softfail SPF, generic
  PTR records, or self-signed certs on opportunistic TLS — reflect widely
  documented industry behaviour, not measurements from this host. Nothing has
  been sent to an external recipient, and given §0, nothing can be until a
  smarthost exists.
- **Why the snakeoil certificate exists** is inferred from its timestamp
  (`notBefore` 09:18:58, minutes before the OpenDKIM keys at 09:21 and the
  Postfix maps at 09:32): it appears to have been generated by the `ssl-cert`
  package during this build. Nothing depends on that inference.

No password, private key, or credential appears anywhere in this file. The
mailbox password was never read; `/etc/opendkim/keys/amphive.app/mail.private`
was read only to confirm its modulus size, and its contents are not reproduced.
The DKIM public key is truncated in §7 with the command to retrieve the full
value locally.
