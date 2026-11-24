# Security

## Reporting a problem

If you find something that could harm a person using Museion (a way for a web page or a
downloaded file to make it run commands, read files it should not, or accept a tampered
download), please do not open a public issue first. Use **Report a vulnerability** under
this repository's Security tab on GitHub, which reaches the maintainer privately.
Anything else is an ordinary issue.

This is a hobby project with one maintainer: there is no bounty and no promised response
time, but a report like this will be read before anything else.

## What Museion does to stay safe

- **It listens on this computer only.** The page is served on `127.0.0.1`. The server
  refuses a request whose `Host` is not its own address, refuses a command that does not
  carry the page's own header, and refuses one whose `Origin` is another site
  (`museion/app.py`). Another website open in your browser cannot drive it.
- **Downloads are checked.** Every archive, model and helper program has a SHA-256 in
  the catalogue. A file gets its real name only after its checksum matches, so a
  partial or altered download is never used. Each release has a `SHA256SUMS.txt`.
- **It does not phone home.** There is no telemetry and no account. The network is used
  only when you ask: set-up and Fetch, making a map, looking up your home area's
  weather stations, and "Check for a newer version".
- **Agents ask before they write.** An agent changes your notes or records only after
  you press Allow on a card showing the change, and writes nowhere else but its own
  work folder.

## What it does not protect against

- **The downloads are not signed.** Windows and macOS warn about them for that reason.
  Check a download against `SHA256SUMS.txt` on the release page.
- **Anyone who can use your computer can use Museion**, read its library and your
  notes, and change its code. Changing its code is a feature here. Notes are plain files
  and are not encrypted.
- **The helper programs are other projects' builds** (llama.cpp, kiwix-serve, pdfium,
  the portable Python, opencode), fetched from their publishers and checked against
  pinned checksums. Museion trusts those builds.
- **A language model can be wrong.** That is a safety matter of a different kind:
  see [docs/safety.md](docs/safety.md).
