# CandidateZero

In 2025 a recruiting agency pitched me on a company called Revin. Nothing came of it. No interview, no introduction, nothing. In 2026 I applied to Revin directly and made it to the final round, reference checks and all. Same candidate, same company. The only variable was the middleman. So I audited six years of my Gmail: more than a dozen third-party recruiter contacts since 2020, zero interviews, zero placements. Every interview I have ever landed came from a direct application or a warm introduction. CandidateZero is the tool recruiters pretended to be. It watches company ATS boards overnight, keeps only the GTM engineering and RevOps roles that are verified live on the employer's own system, filters them against real pay data, and tells you who actually owns the hire. It never applies for you. It arms you, and you take the shot.

## What it does tonight

Reads a list of companies, pulls every posting from their real ATS, throws away
everything that is not the job you want, and writes you one markdown file.

```
uv run engine run
```

That writes `data/digest-YYYY-MM-DD.md`: new roles that passed every filter, a
flagged section for the ones worth a second look, and a footer with the counts of
what was dropped and why. Dropped roles are counts, never noise.

```
uv run engine check-org gongio
```

Probes both ATSs for a slug and tells you which one answered. Slugs are the most
common silent failure in this engine, because a slug is not a company name: it is
whatever the company typed into its ATS on setup. Wealth.com is `wealth-com`,
Gong is `gongio`, Telnyx is `telnyx54`. When a board disappears, this is the
doctor you run.

## Install

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run engine run
```

## Configuration

Everything the engine keeps or drops is decided by YAML, not by code.

```
config/
  example/     committed, sanitized, and doubles as the documentation
  private/     gitignored. Your real config. Same filenames.
```

Resolution is per file: `config/private/filters.yaml` wins over
`config/example/filters.yaml`, while `companies.yaml` can still come from the
example set. A fresh clone runs immediately off the committed example config.

- `companies.yaml` maps ATS slugs to companies, plus a `dead` list for boards
  that have gone missing so they are neither re-probed nor forgotten.
- `filters.yaml` holds the title families, the location rules, and the comp
  floor. Evaluation order is title, then location, then comp.
- `history.yaml` is where you applied and who passed. The committed example is
  schema only, with invented company names. The engine does not read it yet.

### The privacy firewall

The first commit in this repo is the `.gitignore`. `config/private/` and `data/`
were unpublishable before any other file existed, and
`tests/test_public_hygiene.py` fails the build if anything under them is ever
tracked. Your salary floor, your application history, and the companies that
rejected you are nobody's business but yours. The code is public. The data is
not.

## How it decides

- **Title.** Normalized to lowercase, depunctuated, and every spelling of
  go-to-market collapsed to `gtm`, so "Go-To-Market Engineer" matches
  `gtm engineer`. Adjacent families like growth engineer are surfaced and
  flagged, never auto-dropped.
- **Location.** NYC or acceptable US remote. Multi-location postings pass if any
  one segment matches. Remote qualified by a non-US region does not count.
- **Comp.** Pass if the top of the band clears the floor. If only the top
  clears, it is kept and flagged `band-top-only`. If no band is published it is
  kept and flagged `comp-unknown`, because NYC pay transparency means a missing
  band is a question, not a no.

Comp carries its provenance. `structured` means the ATS published a real band.
`parsed` means a regex read it out of the job description, which is an inference,
and the digest says so rather than pretending the two are the same fact.

## Sources

- **Ashby** publishes structured salary bands and full description text in one
  unauthenticated request per org. Note that its bands carry an interval, and it
  is not always a year, so hourly components are annualized before they meet the
  floor.
- **Greenhouse** publishes no pay in its list response at all. For roles that
  survive title and location, and only those, the engine fetches the posting
  detail and reads the band out of the description prose. Cheap first, expensive
  last: a 102 posting board costs about two extra requests.
- **Lever** answers one unauthenticated request per org. Some orgs publish a
  structured band; the rest bury it in prose, and the parser reads both.
- **Workable** returns every posting with its full description in one request,
  but publishes no structured comp anywhere, so bands only exist when the org
  wrote one into the prose.
- **SmartRecruiters** paginates a public list with no description and no comp;
  detail fetches follow the Greenhouse pattern, gated on the cheap filters.
- **Workday** answers a plain public POST per tenant and site, 20 postings a
  page. The list hides the posting date behind "Posted 3 Days Ago" prose, so
  the gated detail fetch is also what recovers the real date, the description,
  and the US pay-transparency band.

## Manners

An honest User-Agent that says who we are, real timeouts, half a second between
requests to the same host, and a dead org that lands in the digest footer instead
of taking down the run at 3am.

## Status

v0, built in public. Live: six-vendor sourcing (Ashby, Greenhouse, Lever,
Workable, SmartRecruiters, Workday), the self-healing org registry, liveness
verification, config-driven filters, dedupe against application history,
job-description shape rules, the seen-store, the digest, and the weekly public
report. Next: board scrapers and the who-owns-the-hire resolver.

## License

MIT.
