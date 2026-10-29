# Editor accounts and change history

Implemented locally on 28 October 2026. One project and one application process.
This is an editor workspace; a public approved address directory and review/
publication workflow are separate future work.

## First use

Open the port-8002 accounts review app on this computer. When no accounts exist, the
initial screen offers **Create administrator**. Choose a username, your display
name and a passphrase of 15-128 characters. No default password is created.
The local setup endpoint works only for direct loopback requests with a
localhost/loopback host; forwarded/proxied requests cannot use it.

For a hosted server, create the first account locally BEFORE making it available:

```powershell
.venv/Scripts/python.exe -m osmaddresssystem.app account --project PATH --username admin --name "Project administrator"
```

The command prompts privately for the passphrase; do not put passwords in shell
arguments, chat, code or configuration files. For console recovery, stop the
service, run the same command with `--recover`, then restart it. Recovery promotes
the named existing account to administrator and invalidates its sessions/invites;
it is recorded as a local console operation.

## Team workflow

1. Administrator opens **Team**, enters username/display name and chooses a role.
2. **Create invitation** displays a one-use link, valid for 24 hours. Share it
   privately. The application does not email or message anyone automatically.
3. Invitee opens the link and chooses their own passphrase. The token is carried
   in a URL fragment, removed immediately, and submitted in JSON rather than a
   logged query string. The database stores only its verifier.
4. The top bar displays the signed-in person and role. **History** shows authors,
   time, operation and expandable before/after details; it supports filtering and
   loading earlier records.
5. **Team** can change another account's role, disable it, or reset access using
   a fresh invitation. Such changes invalidate the account's existing sessions.
   An administrator cannot remove their own access through this screen.
6. Selecting your name opens passphrase change. Changing it signs out all your
   sessions. **Sign out** invalidates the current session on the server.

| Role | API-enforced capabilities |
|---|---|
| Viewer | Read project maps/data and acknowledge the reference point. No domain edits or editor history. |
| Contributor | Viewer access plus street names, computations, own Undo/Redo and editor history. |
| Mapper | Contributor access plus road geometry/connections/IDs, attributes, house access, work areas and checkpoints. |
| Administrator | Mapper access plus accounts, global settings/reference changes and checkpoint restoration. |

Contributors currently edit names directly. Name proposals/approval queues are
not implemented. Work-area status is manual progress, not an approval decision.
All editor APIs require sign-in; anonymous users cannot inspect current survey
data or exports. An invited viewer is a project member, not a public visitor.

Buttons are disabled where useful, but access control lives in the server
middleware, covering direct API calls and future unclassified mutation routes.
Authentication derives the actor ID from the session, never a `user` value in
the submitted payload. Membership is scoped to the project's database. Shared
identity across multiple community databases is not part of this increment.

## History and safe recovery

- Stable account IDs are saved on new edits. Audit entries also capture display
  name/username/role at the time of the operation.
- Existing event records are migrated once into audit evidence. Anonymous legacy
  edits remain **Unknown (legacy)**, retaining their original timestamps. Records
  already deleted by earlier versions cannot be recovered or attributed.
- `audit` is append-only through the application. It retains old/new data and
  edit, Undo, Redo, closed redo branch, checkpoint, restore and account operations.
  The replay projection may toggle `undone`, but no longer deletes undone events:
  `closed_redo` prevents their reuse after a new edit. Audit IDs identify evidence
  independently of replay events. Direct database-owner tampering is not prevented
  by this application-level audit trail.
- Undo acts only on the signed-in editor's events. It conservatively refuses when
  newer active edits belong to someone else, even if apparently unrelated. Redo
  also checks intervening audit mutations, including another person's Undo of a
  prerequisite. This avoids silently replaying changes against altered topology;
  it is not a general automatic conflict-merging system.
- Authenticated input mutations and role/session revocations are serialized in
  the project's single server process. Identity is resolved after taking that
  lock, so queued edits cannot retain revoked permissions. Do not run additional
  workers or modify the database concurrently from external tools.
- Only administrators can restore checkpoints. Restore requires the current
  revision and an explanatory reason. A stale request is refused. The old state,
  restored state and reason remain in the audit. Project checkpoints cannot roll
  back account credentials, roles or audit evidence.
- Computation requests identify the requester; completed computations identify
  their input hash and system actor. Saved, computed and human-reviewed remain
  different concepts. Reference acknowledgement is read-only in account mode.

## Session and hosting details

Passphrases use Python/OpenSSL scrypt with a random 128-bit salt, N=2^17, r=8,
p=1, with at most two simultaneous password computations. No external package or
identity-service subscription was added. The parameters follow the scrypt option
in [OWASP password-storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Sessions use independent random 256-bit tokens; the database holds SHA256
verifiers, not reusable raw session cookies. They are revocable, have an eight-
hour absolute lifetime and expire after 30 minutes without API activity. Browser
background requests count as activity; this is not a keyboard-idleness timer.
Cookies are HttpOnly, explicitly SameSite=Strict, and Secure except direct local
HTTP development. Cookie names are project-specific; cookie path respects ASGI
root_path. Mutations require a session CSRF token; cross-origin writes and
non-JSON writes are refused. Login/setup/activation have persistent rate-limit
buckets. See [OWASP session guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

For a cloud host: require HTTPS, configured trusted reverse-proxy headers and
the correct root path; use one process per project and persistent storage.
Keep database/backups private: they now contain account/password verifiers and
session records as well as survey data. Back up before schema migration; the
new `audit`, `closed_redo`, `account_*` tables are additive. Audit backfill does
not change project revision or rename existing data. This is not an independent
security assessment or proof of production load capacity. MFA/SSO, public
self-registration and email recovery are not implemented.

`create_app(..., auth_enabled=False)` is an explicit in-process legacy test
harness. The serve command defaults to accounts and exposes no bypass switch.
An older already-running backend can still serve its pre-upgrade behaviour;
refreshing JavaScript alone cannot secure it. Restart only the intended project
process after its backup, as recorded in the implementation log.

## Main files

- `app/auth.py`: accounts, invitation activation, sessions, CSRF, roles, throttling.
- `app/identity.py`: request-local actor context, propagated into synchronous APIs.
- `app/store.py`: additive audit migration, retained replay events and recovery guards.
- `app/server.py`: protected routes, trusted attribution and computation records.
- `app/static/auth.js`, `auth.css`: bilingual sign-in, team, account and history UI.
- `tests/test_accounts.py`, `test_accounts_browser.py`: access/revocation/recovery,
  restart/prefix handling, invitation and two-browser coverage.
