# Data handling: intake, masking, encryption, contract end, yearly checks

Rules this follows:

- Bookkeepers' personal-data safeguard regulation (記帳士及記帳及報稅代理業務人個人資料檔案安全維護計畫及業務終止後個人資料處理作業辦法), https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=G0340165 . Art. 15: permissions per person, a named person in charge, confidentiality agreements, hand-over when someone leaves. Art. 17: identity checks, display masking (顯示隱碼), encrypted transfer, access control, protection against intrusion, monitoring of abnormal use, with items 5 and 6 drilled at least once a year. Art. 19: usage records and trail data kept at least five years; after the business ends, a record of how the data was destroyed or transferred.
- 記帳士法 Art. 17: no disclosure of a client's business secrets without permission.
- 稅捐稽徵機關管理營利事業會計帳簿憑證辦法 (帳簿憑證辦法) Art. 27: vouchers kept at least five years.

## 1. What Ledger Room stores

| Where | What | Personal data? | Kept until |
|---|---|---|---|
| `data/<inbox>/clients/<client>/<YYYY-MM>/` | photos and scans of vouchers | yes: names on receipts, sometimes phone numbers | the client's contract ends (purge) |
| `data/<inbox>/clients.json` | the firm's client list | a sole proprietor's or owner's name, if entered | purge |
| `data/out/cache/` | OCR lines, model replies, read fields, keyed by the image's SHA-256 | yes | purge |
| `data/out/runs/<run>/result.json`, `crops/`, `export_*.csv` | exceptions, evidence crops, media-file CSV | yes | purge (the client's rows are removed) |
| `data/out/runs/<run>/audit_log.jsonl` | every read and exception: document id, SHA-256, model, device | no receipt contents | at least five years (Art. 19) |
| `data/out/oplog.jsonl` | who logged in, ran, viewed, exported, unmasked, purged | staff user names | at least five years (Art. 19) |
| `%LOCALAPPDATA%\LedgerRoom\` (or `~/.ledger_room/`) | `users.json` (password hashes), `audit.key` | staff user names | while the account exists |

**Photos are working copies; the paper originals stay with the client for five years (帳簿憑證辦法 Art. 27).** Ledger Room never replaces the paper. Replacing paper with electronic storage needs the tax office's approval after the income-tax assessment, and Ledger Room does not claim it.

## 2. Getting documents onto the box (keep them inside the firm)

Ways that keep the data inside the office:

1. **Scanner to the box.** Set the office scanner to save into a shared folder on the box, mapped to `data/<inbox>/clients/<client>/<YYYY-MM>/`. The scan goes from the scanner to the box over the office network and nowhere else.
2. **USB stick.** Copy from the client's USB stick or the firm's own stick, then delete the files from the stick. Use an encrypted stick (BitLocker To Go) for anything that leaves the building.
3. **Office phone on the office LAN.** A firm-owned phone on the office Wi-Fi (WPA2/WPA3) copies photos to the box's shared folder. An upload page in the web app is planned but not built yet; until then use the shared folder. Do not use a personal phone with cloud photo backup switched on: that sends every photo to the phone maker's servers.

Ways that **leave the firm**:

- **LINE and e-mail are transfers through third-party servers** (the messaging company's or the mail provider's). The photo is stored there, outside the firm's control, before anyone imports it. If a client insists on sending this way, import the file, then delete the chat or mail copy, and say in the client agreement that this channel is the client's choice.
- Cloud drives (Google Drive, OneDrive, Dropbox) are the same: a third party holds a copy.

Transfer inside the office: the web app speaks plain HTTP. On the office LAN this relies on the network itself being closed (wired, or WPA2/WPA3 Wi-Fi with no guest access). Encrypted transfer (Art. 17) over the LAN needs HTTPS in front of the app, for example a reverse proxy with the firm's own certificate; Ledger Room does not set this up yet.

## 3. Display masking (顯示隱碼)

On by default for every role, in the web app, the media-file CSV export and the log views (`ledger_room/privacy.py`):

| Field | Treatment |
|---|---|
| seller / buyer name | masked when it reads as a person's name: 王小明 -> 王○明, John Smith -> J*** S***. Names with a company or shop word (公司, 商行, 店, 記 ...) are kept. When unsure it masks. |
| note, item descriptions, exception messages | phone numbers (0912-***-678), national ID and resident certificate numbers (A12****789), e-mail addresses, labelled names (負責人：王○明) and any name found in the name fields |
| client owner, contact, phone, ID number, home address | masked; the client's name too when the client is marked `"sole_proprietor": true` |
| UBNs (統一編號), invoice numbers, dates, amounts | kept: a company's UBN is not personal data |
| receipt images and evidence crops | **not masked**: they are the voucher the bookkeeper has to check |

Unmasking: only roles in `LEDGER_UNMASK_ROLES` (default `admin`) see the "Show personal data" button. It asks for a reason, writes an `unmask` entry (user, run, reason, time, IP) to the operation log, and shows that one run unmasked for ten minutes. Staff cannot unmask; a refused attempt is logged as `denied`.

The web app also serves only images under `/img/` and only evidence crops under `/out/`, so result files, exports and logs cannot be fetched past the masking.

## 4. Encryption at rest

**Deployment requirement: the disk that holds `data/` uses full-disk encryption.**

- Windows: BitLocker (Pro / Enterprise / Education; Windows Home has "Device encryption"). Turn it on before copying any client data, store the recovery key away from the box (printed in the safe, or the firm's Microsoft account), and check: `manage-bde -status` (administrator).
- Linux: install on LUKS (dm-crypt). Check: `lsblk -o NAME,TYPE,MOUNTPOINT` shows a `crypt` layer under the data mount.

Ledger Room checks this when the web app starts (`python -m ledger_room.privacy check-encryption` does the same by hand). If the volume is not encrypted, BitLocker is suspended, or the status cannot be read, it prints a warning and writes `encryption_check` to the operation log. It never refuses to start.

Per-file encryption of stored images is not done: the Python standard library has no cipher, the `cryptography` package is not installed in this environment, and a key kept on the same disk would add little over full-disk encryption. Keep `audit.key` off the data disk (`LEDGER_AUDIT_KEY_FILE`, e.g. on a separate USB key).

## 5. When a client's contract ends

```
python -m ledger_room.privacy purge --client C01 --set main --reason "contract ended 2026-10-31" --dry-run
python -m ledger_room.privacy purge --client C01 --set main --reason "contract ended 2026-10-31" --operator amy
```

1. If the client or the next firm asks for the data, hand it over first (export, or copy the client folder to their medium) and note it in the reason.
2. Run with `--dry-run`. It lists every file it would delete or rewrite, with SHA-256 and size, and changes nothing.
3. Run without `--dry-run`. It asks you to type the client id, then:
   - deletes the client's photos, the cached reads of those photos (matched by image hash), copies of the photos under `data/out`, evidence crops and per-client media files;
   - removes the client's rows from run results and CSV exports, and the client's entry from `clients.json`;
   - **keeps** the audit logs and the operation log: they are the usage and trail records Art. 19 asks for, kept at least five years, and they hold document ids and hashes, not receipt contents;
   - writes the deletion record to `data/out/oplog.jsonl`, in the same HMAC chain as the audit log: `purge_start` (who, when, reason, method, legal basis), one `purge_file` per file (path, kind, SHA-256, bytes; or before/after hashes for rewritten files), and `purge_end` (counts, errors, what was kept, what is left for review).
4. Files it cannot classify that still name the client's documents (evaluation output, for example) are listed under `review` and left alone: check and delete them by hand.
5. **Backups are not touched.** Delete the client from backups when they rotate, or by hand, and note the date.

Method: files are deleted through the file system; there is no overwrite pass, because on SSDs overwriting is not reliable. Deleted data is unreadable afterwards only because the disk is encrypted, which is why section 4 is a requirement.

The paper originals were never on the box; they stay with the client.

## 6. Yearly drill and access review

Do this at least once a year, and when someone joins or leaves. Keep the filled checklist (date, who took part, findings, fixes) with the safeguard plan.

Access review (Art. 15):

- [ ] `python -m web.users list`: every account belongs to a current employee; remove leavers (`python -m web.users remove NAME`) on their last day.
- [ ] Roles are right: only the person in charge and named deputies are `admin`.
- [ ] Each account holder has signed the confidentiality agreement (記帳士法 Art. 17, client business secrets).
- [ ] The person in charge of personal data is named in the safeguard plan.
- [ ] Read the operation log (Log tab, admin): `unmask` entries have sensible reasons, `login_failed` / `login_locked` / `denied` spikes are explained.

Drill (Art. 17, intrusion protection and abnormal-use monitoring):

- [ ] Five wrong passwords lock the account for five minutes, and the lock appears in the operation log.
- [ ] A staff account cannot open the audit log, run the model live, or unmask personal data.
- [ ] The audit log and the operation log verify (Log tab shows "Hash chain and HMAC verified").
- [ ] `python -m ledger_room.privacy check-encryption` reports `encrypted`; the BitLocker / LUKS recovery key can actually be found.
- [ ] The box answers only on the office LAN: from outside the office, the web app is not reachable.
- [ ] Restore last week's backup to a spare machine and open one run.
- [ ] Run a purge with `--dry-run` on a test client and read the plan.
- [ ] Walk through the incident steps in the firm's safeguard plan (lost USB stick, stolen box, ransomware): who decides, who tells the clients, who tells the authority.
