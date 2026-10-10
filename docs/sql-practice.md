# Practising SQL on your own data

You have 216,000 readings from five demo devices across 30 days. That is a
much better teacher than any tutorial dataset, because you already know
what a pressure cascade and a kWh counter are — so when a query returns
something odd you can tell.

Work through this in order. Each section assumes the one before it.

## Connecting

**psql**, the command line. This is the one to learn; it is on every
PostgreSQL machine you will ever touch.

```bash
installer\vendor\pgsql\bin\psql.exe -h 127.0.0.1 -p 5433 -U jetpace_app -d jetpace
```

The password is in `.local\app_password`. Set `PGPASSWORD` first to avoid
being asked each time, or create a `%APPDATA%\postgresql\pgpass.conf`.

**pgAdmin** came with your PostgreSQL 18 install. Point it at
`127.0.0.1:5433`, same credentials. Good for browsing the schema and
reading query plans visually; less good for learning to type SQL.

### psql commands worth knowing on day one

| | |
|---|---|
| `\dt` | list tables |
| `\d reading` | describe one table |
| `\d+ reading_hourly` | describe a view, including its definition |
| `\df consumption` | describe a function |
| `\timing on` | show how long each query took — use this constantly |
| `\x on` | expanded output, for wide rows |
| `\e` | open the last query in an editor |
| `\q` | quit |

## What is in there

```
tenant ── site ── gateway ── device ── device_parameter
                               │
parameter ─────────────────────┘

reading            raw, 1-minute, a hypertable
reading_hourly     continuous aggregate over reading
reading_daily      continuous aggregate over reading_hourly
consumption(...)   the billing function
```

| Device | Kind | Parameter | Behaviour |
|---|---|---|---|
| `DEMO-DP-01..03` | pressure | `dp` | Instantaneous. Wanders around 10/12.5/15 Pa, with door-open excursions that go negative |
| `DEMO-EM-01` | energy | `kwh_import` | Counter, only increases, faster during shifts |
| `DEMO-EM-02` | energy | `kwh_import` | Counter, **with a deliberate meter replacement** two thirds of the way through |
| `DP-001..003` | pressure | `dp` | The live prototype devices — only a few readings |

---

## 1. SELECT

Three worked examples, to set the style. After these, you write them.

```sql
-- every device that has not been retired
SELECT code, name, unit_id
FROM   device
WHERE  retired_at IS NULL
ORDER  BY code;
```

```sql
-- the ten most recent readings, whatever they are
SELECT ts, device_id, param, value
FROM   reading
ORDER  BY ts DESC
LIMIT  10;
```

```sql
-- how many readings are there, in total
SELECT count(*) FROM reading;
```

Now you:

1. List every parameter with its unit and aggregation.
2. Find all devices whose code starts with `DEMO-EM`.
3. The single highest `value` ever recorded in `reading`. (It will be a
   kWh counter — think about why that is obvious in hindsight.)

## 2. JOIN

`reading` stores `device_id` and `param` as numbers. To see names you must
join. You will write this join hundreds of times.

```sql
-- worked example: the last five readings, readably
SELECT r.ts, d.code, p.key, r.value
FROM   reading r
JOIN   device    d ON d.id = r.device_id
JOIN   parameter p ON p.id = r.param
ORDER  BY r.ts DESC
LIMIT  5;
```

4. Every device with its gateway's label and host.
5. Every device with its model's code and the datasheet it came from.
6. Devices that have **no** readings at all. (Hint: `LEFT JOIN` and
   `WHERE … IS NULL`, or `NOT EXISTS`.)

## 3. GROUP BY

7. Count readings per device, highest first.
8. For each demo room, the min, max and average `dp` over the whole 30
   days. Round to one decimal.
9. Count readings per day for `DEMO-DP-01`. (Hint: `date_trunc('day', ts)`.)
10. Which room has the **lowest** minimum pressure? Answer it with one
    query, not by reading the output of question 8.

## 4. Time

This is where industrial SQL lives, and where mistakes get expensive.

11. All readings from the last two hours.
12. Group `DEMO-EM-01`'s readings into hours with `date_trunc`, and count
    each hour.
13. Do question 12 again using TimescaleDB's `time_bucket('1 hour', ts)`.
    Compare the two. Then try `time_bucket('15 minutes', ts)` and note
    that `date_trunc` cannot do that at all.
14. `SET timezone = 'UTC';` then re-run question 9. Then
    `SET timezone = 'Asia/Kolkata';` and run it again. The day boundaries
    move. **This is the single most important thing on this page** — it is
    why `reading_daily` buckets with an explicit timezone and why the
    architecture says date arithmetic never happens in application code.

## 5. The questions the product actually answers

15. **The excursion report.** For each demo room over the last 7 days, how
    many minutes did it spend at or below 0 Pa? (Hint: each raw reading
    represents one minute, so a count *is* a duration. Say so in a comment
    — that assumption is load-bearing and will stop being true when scan
    classes differ.)
16. **Energy per day.** Using `reading_daily`, the kWh each meter consumed
    on each of the last 7 days. (Hint: `close_value - open_value`.)
17. Now get the same answer from `consumption()` and compare. Check
    `\timing` for both. Then explain why one of them can be trusted at a
    month boundary and the other cannot.
18. **Maximum demand.** The highest 30-minute energy increase for
    `DEMO-EM-01` in the last 7 days, and when it occurred. This needs a
    window function — `lag()` or `first_value()` over an ordered window.
    This is the query that decides a demand charge.

## 6. Reading the plans

19. `EXPLAIN (ANALYZE, BUFFERS)` on question 16 against `reading_daily`,
    then the equivalent query written against raw `reading`. Compare rows
    scanned and time taken. That difference is the entire reason the
    rollups exist.
20. `SELECT * FROM timescaledb_information.chunks WHERE hypertable_name =
    'reading';` — see how your 30 days is physically split, and which
    chunks the compression policy has already compressed.

---

## How to get unstuck

- The PostgreSQL manual is genuinely excellent and worth reading rather
  than only searching. Start with *Tutorial* then *The SQL Language*.
- `\d` and `\df` in psql answer most "what is this called again" questions
  faster than a search engine.
- When a query returns nothing, check your time filter first. It is almost
  always the time filter.
- Ask me for the answer to any of these — but write a wrong one first.
  A query that returns the wrong number teaches you more than one you
  copied, because you have to work out *why* it is wrong.

## One thing you will notice

Question 18's answer will come out slightly inexact — something like
`143615.90999999968` rather than a round number. That is single-precision
float arithmetic in `reading.value`, and it is why:

- money is rounded at the presentation layer, never accumulated
- `parameter.decimals` exists
- consumption is `last − first` on the *same* register, rather than a sum
  of per-interval differences, which would accumulate the error

That last point is a real design decision, not a detail.
