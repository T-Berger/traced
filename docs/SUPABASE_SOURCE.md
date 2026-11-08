# Supabase content source

The Supabase connector reads a bounded sample from a PostgreSQL table and saves
the returned records through the existing Sources workflow. It uses the selected
Traced workspace, collection history, source import, and combined worker. It
does not replace Traced's local database or write to the source database.

## Configure the connection

Run `make setup` from the project root to install the locked dependencies. Put
connection settings in the root `.env` or the server's process environment. Keep
the password in local configuration; do not put it in a source locator, source
label, committed file, or exported case. The root launcher also accepts remaining
settings from `evidence-explorer/.env` under its existing precedence rules. See
[Common project root](PROJECT_ROOT.md).

```dotenv
SUPABASE_HOST=db.your-project-ref.supabase.co
SUPABASE_PORT=5432
SUPABASE_DATABASE=postgres
SUPABASE_USER=postgres
SUPABASE_PASSWORD=replace-with-your-database-password
SUPABASE_ID_COLUMN=id
SUPABASE_TEXT_COLUMN=content
```

The host and password are required. Port, database, user, ID column, and text
column have the defaults shown above. A direct `db.<project-ref>.supabase.co`
host supplies the project identity. With a pooler endpoint, configure the exact
host, port, and user provided by Supabase and set `SUPABASE_PROJECT_REF` explicitly
to the same project's reference. Do not substitute a guessed pooler hostname.

Use a database role with read access to the intended content table. The connector
requires TLS and opens a read-only, repeatable-read transaction with a bounded
statement timeout. Its queries use quoted identifiers and bound values; a source
locator cannot contain arbitrary SQL.

## Inspect and map the table

```sh
./traced supabase status
./traced supabase check --table public.content_items
```

`status` checks local configuration without connecting. It reports readiness and
a safe message, not connection settings or credentials. Readiness does not prove
database access. `check` connects read-only and prints column names, types, and
nullability. It does not fetch content rows, create evidence, or enqueue work.
Both commands return a nonzero exit code when their check fails.

Set the ID and text mappings to columns verified by `check`. The ID must be
unique and non-null, and the text mapping must identify the main content. Optional
fields are mapped only when explicitly configured:

| Setting | Meaning |
| --- | --- |
| `SUPABASE_TITLE_COLUMN` | Source-supplied title |
| `SUPABASE_URL_COLUMN` | Original public source URL, when the table actually contains one |
| `SUPABASE_PUBLISHED_AT_COLUMN` | Known original publication timestamp |
| `SUPABASE_AUTHOR_COLUMN` | Source-supplied author or account label |

Leave optional mappings unset when their meaning is unknown. A row's `created_at`
is not automatically treated as its original publication time, and a database
row does not establish the original source's provenance or factual truth. Unknown
publication times, authors, and URLs stay unknown.

The connector saves the selected raw row as part of its immutable observation,
alongside the mapped content and collection metadata. Choose a content table
that is appropriate to retain and share as evidence. Do not point it at a table
containing passwords, tokens, or other connection secrets.

## Collect and review

1. Open **Cases → Gather evidence → Live sources** in the integrated desk.
2. Choose **Supabase content items** from **Choose a source**. The preset fills
   **Supabase · Postgres**, its source name, and the locator `public.content_items`.
   Expand **Source details or a custom source** to inspect or change them.
3. Choose a bounded item limit from 1 to 20. Leave **Keep collecting on this
   schedule** unchecked for a one-time sample.
4. Select **Add & collect** explicitly for the first sample, then inspect the
   saved records and their metadata before importing them into a review case.
   Use **Collect now** on the saved source for a later explicit refresh.

Collection orders the sample by the configured ID descending. This is a stable
selection rule, not a historical or publication-time ordering. UUID ordering, for
example, does not mean newest content first. The saved sample and its limits
remain visible; a successful fetch does not establish historical completeness,
independent corroboration, or human approval.

Optional scheduling uses the existing source schedule and the single combined
worker for the selected workspace. Keep the first collection explicit and enable
a recurring schedule only when it is wanted. A refresh saves another observation;
it does not rewrite earlier saved evidence or transfer reviewer decisions.

## Verification limits

The supplied direct endpoint was not reachable from the development network over
IPv6. An alternative pooler endpoint and its connection settings remain to be
supplied before live schema and content access can be verified there. No live
collection is established by configuration readiness or offline fixture tests.
Use `check`, confirm the actual mappings, and inspect an explicit bounded
collection after connectivity is available.
