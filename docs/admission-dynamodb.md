# DynamoDB admission storage

The hosted CPU factory accepts exactly `TOUCHLINE_ADMISSION_STORE=sqlite` (the
default) or `dynamodb`. Dynamo mode additionally requires
`TOUCHLINE_ADMISSION_DYNAMODB_TABLE` to name an existing table. Invalid or missing
configuration fails with a static error; a Dynamo error never selects SQLite.
The legacy volume remains mounted by the deployment definition but Dynamo mode
does not reload, read, write or commit its database.

```dotenv
TOUCHLINE_ADMISSION_STORE=dynamodb
TOUCHLINE_ADMISSION_DYNAMODB_TABLE=your-existing-admission-table
AWS_DEFAULT_REGION=eu-west-2
```

The app's lifespan warms the policy before serving visitors. Warmup reads the
migration-ready settings, preserved peer salt and total-spend counter. A missing
or unready table cannot authorize work. The dedicated client uses refreshable
Modal OIDC credentials; its backend role needs scoped access to this table and
the existing private recordings. Local use retains the normal AWS credential
chain. Construction and offline tests make no AWS requests.

Paid starts atomically reserve total/day spending, the rolling IP start count,
visitor/component ownership and a permanent permit record. Creation quotas
atomically update peer/global vectors. Conditional collisions have bounded
retries; ambiguous transport failures never grant a lease. Local heartbeat,
HTTP and connection rate checks do not wait for a durable-write lock.
Confirmed cleanup closes only the matching permit and owner records. It never
reduces spending or removes history. Expired uncertain ownership continues to
block that visitor's same component, while other components/visitors can start.

## Schema version 1

The table has string partition/sort keys named `PK` and `SK`. Monetary counters
and permit amounts use integer microdollars (`$1 = 1,000,000`); timestamps retain
Unix seconds as DynamoDB Numbers. Every mutable counter/vector/owner/permit row
has a nonnegative integer `version`, initially zero.

| PK | SK | Remaining fields |
|---|---|---|
| `config` | `settings` | `schema_version=1`, `migration_ready=true`, `peer_salt` |
| `budget` | `total` | `amount_microusd`, `version` |
| `budget` | `day#YYYY-MM-DD` | `amount_microusd`, `version` |
| `peer#HASH` | `starts` | `events=[created timestamps]`, `version` |
| `creations` | `global` | `events=[created timestamps]`, `version` |
| `creations` | `peer#HASH` | `events=[created timestamps]`, `version` |
| `permit#ID` | `record` | `visitor_id`, `component`, `peer`, `created`, `day`, `amount_microusd`, `expires`, `closed`, `owner_keys`, `version` |
| `visitor#ID` | `owner#director` or `owner#captions` | `permit_id`, `expires`, `closed`, `version` |

`closed` and `migration_ready` are Booleans. `owner_keys` is a list of `{PK, SK}`
maps. A modern permit has one component/owner. An unmapped historical combined
permit uses `component=legacy` with both owner keys. Permanent permit records
preserve all historical amounts, creation/expiry times and closed states. No TTL
deletion applies to permits, ownership or budget counters. Rate vectors may trim
events outside the rolling 3600-second start / 600-second creation windows.

## Migration and ownership change

Switching stores is an explicit operator procedure, separate from app startup:

1. Drain/stop the CPU app and confirm owned paid work has terminated. Preserve a
   committed SQLite backup and all existing funding settings.
2. Import into a new empty table. Preserve the exact `peer_salt`, every permit
   and component mapping, and all still-relevant creation/start events. Populate
   lifetime and UTC-day counters from the original amounts without refunds.
3. Select each current owner from its unclosed permit, or latest closed permit
   when none is open. Refuse migration if multiple open permits claim the same
   visitor/component. An unmapped open legacy permit owns both components.
4. Verify complete permit identities/counts/amount sums, day totals, ownership
   and rate vectors against the backup. Write the ready settings marker last.
5. Configure the scoped role, explicit store and table; deploy only after the
   audited import succeeds. Preserve the original backup and sanitized audit.

After any Dynamo reservation, switching the selector back to the old SQLite
snapshot would lose spending/rate history. A rollback requires another stopped
app, complete audited migration; changing the selector alone is insufficient.
Do not initialize an empty ready table over an existing funded deployment.
