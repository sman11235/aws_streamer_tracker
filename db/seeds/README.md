# Popular VTuber seed

[`001_popular_vtubers.sql`](001_popular_vtubers.sql) seeds `streamer_info` from an original collection of **500 VTuber directory entries**, collected on **2026-10-26**. It uses the existing enum types and lets PostgreSQL generate each UUID.

The SQL now contains **655 entries** after manual curation and the **2026-10-27 roster expansion**. The original ranking snapshot had 500 entries; six were removed during manual curation, and the roster audit added 161. The original ranking counts below describe that initial snapshot, not every subsequent addition.

## Roster coverage

The roster audit compared the existing seed with the published Nijisanji JP/EN, VSPO! JP/EN, Neo-Porte, Nanashiinku (774 inc.), NoriPro, Phase Connect, and Mythic Talent VTuber rosters. Additions are current listed members regardless of subscriber count. Existing historical entries are retained.

| Agency | Members added | Current coverage |
| --- | ---: | --- |
| Nijisanji | 63 | 199 of 200 JP/EN members; Lunlun is listed in the audit pending a supported gender value. |
| VSPO! | 9 | All 32 JP/EN members. The former CN branch is excluded. |
| Neo-Porte | 17 | All 22 roster members. |
| Nanashiinku / 774 inc. | 27 | All 30 roster members. |
| NoriPro | 12 | All 14 distinct streaming channels; Norio and Tamaki share a channel. The separate AI singing-voice character is excluded. |
| Phase Connect | 31 | All 34 current members across OriginS, ALiAS, Euphoria, SAGA, Invaders WiSH, Invaders QUEST, and KALEiDO. |
| Mythic Talent | 2 | All five creators publicly tagged as VTubers on its creator page; the site offers its full roster by request, so this is public-roster coverage only. |

YokaSiri was already present and received the missing Mythic Talent affiliation. Nyanners was matched to Nyatasha Nyanners by channel ID. Norio's official profile links to Tamaki's existing channel, so no second channel owner was inserted. A stale NoriPro footer link for retired member Mikami Renge was excluded from additions; KALEiDO graduates Kaminari Clara and Kokoromo Memory were also excluded. Nijisanji's separate VirtuaReal listing is outside this JP/EN YouTube/Twitch audit.

Ten existing Japanese-name fields were corrected or enriched from matched official Nijisanji profiles, including Watarai Hibari's incorrect name, Usami Rito's stray opening parenthesis, and eight entries with an available official Japanese spelling or equivalent correction. Existing English aliases and generation edits were retained. These are seed-file corrections: applying the seed to an existing database still preserves that database's existing metadata.

[`001_popular_vtubers.roster-audit.csv`](001_popular_vtubers.roster-audit.csv) records each published roster entry, source links, channel ID, and whether it was added, already present, matched as an alias, or excluded. **Lunlun is not inserted:** the directory explicitly leaves the persona's gender unspecified, while the current enum accepts only `M`, `F`, or `NB`. No gender was guessed. The audit retains the verified channel ID and Ayakaki cohort for review.

[`001_popular_vtubers.roster-unlisted.csv`](001_popular_vtubers.roster-unlisted.csv) records 24 existing entries absent from the current public rosters. They remain in the seed; roster absence, especially from Mythic's partial public listing, does not establish retirement or contract status.

Apply the schema and outstanding migrations first, then run from the repository root with a PostgreSQL client that can reach your database:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/seeds/001_popular_vtubers.sql
```

The transaction inserts only entries whose YouTube and optional Twitch IDs are absent from `streamer_info`. Rerunning it preserves existing UUIDs, metadata, and related records. Existing channel matches are skipped, so a populated database may receive fewer than 500 new rows. An advisory lock serializes concurrent executions of this seed; unrelated writers must coordinate separately because the table has no unique channel-ID constraints. No rows are written to `channels` or history tables; the channel updater discovers these IDs from `streamer_info`.

## Selection and field conventions

- Candidates come from [HoloList's YouTube subscriber ranking](https://hololist.net/top/), in ranking order. Entries with unresolved channel IDs or unmappable persona gender are skipped, and shared channel IDs appear only once. The selected entries span source ranks 1–527 and had at least 227,000 listed YouTube subscribers. This is a reproducible selection of popular directory entries, rather than a definitive ranking across all platforms.
- Retired and inactive entries are included: 453 active, 45 retired, one inactive, and one on hiatus according to their directory profiles. Shared personas such as FUWAMOCO remain a single entry because they share a channel. Separate personas are separate entries; the dataset does not identify the people behind them.
- `main_platform` is `YOUTUBE` for every entry: this seed chooses YouTube as the initial tracking platform. It does **not** assert that each creator prefers YouTube. Set individual rows to `TWITCH` if that suits your tracking policy.
- All 655 entries have canonical YouTube channel IDs. The 329 Twitch IDs are numeric user IDs, never Twitch logins. `has_youtube` and `has_twitch` reflect the IDs included in the row. A missing Twitch ID means none was resolved from the profile's listed links; it does not establish that no Twitch account exists. One directory-listed Twitch login in the expansion did not resolve; its YouTube-backed entry was retained with the unresolved optional Twitch field left `NULL` and noted in the audit.
- `jp_name` uses an available official Japanese name, the directory's Japanese original name, or the VDB Japanese name. Where no sourced Japanese spelling is available, it repeats `en_name` to satisfy `NOT NULL`; these fallbacks are identified in the provenance CSV. No Japanese transliterations were invented.
- Agency affiliations reflect the collected directory profiles and subsequent manual edits. Independent or unlisted affiliations are `NULL`. Generation values use short names without an agency prefix, such as `Gen 3`, `Myth`, or `Luxiem`. For hololive and HOLOSTARS, where multiple official groups match, the first listed group is used (for example, Council before Promise). Affiliation information can change.
- `gender` describes the **public VTuber persona**, using directory labels: Female → `F`, Male → `M`. Six profiles labeled Other use explicit Agender, Non-binary, or Gender Fluid labels from the earlier directory snapshot to map to `NB`; both labels and their sources are retained in the CSV. Unspecified labels are excluded.
- `lang` preserves the directory's comma-separated language names, such as `English,Japanese`. These are language labels, not ISO codes; the schema permits text and the current ingestion code does not interpret this field.

## Sources and provenance

[`001_popular_vtubers.sources.csv`](001_popular_vtubers.sources.csv) records each row's profile URL, ranking, subscriber count, status, gender sources, Japanese-name source or fallback, generation source, channel IDs, and ID lookup source. Counts and status are provenance only; they are not additional database columns.

[`001_popular_vtubers.generations.csv`](001_popular_vtubers.generations.csv) records the **2026-10-27 generation review** for all 360 seed members affiliated with Nijisanji, VSPO!, Neo-Porte, 774 inc., NoriPro, Mythic Talent, or Phase Connect. It includes the resulting value, review status, source links, and membership notes. The first pass filled 163 missing values and verified seven existing values. The roster expansion added 139 further named generation/cohort values and preserved existing generation edits.

Generation conventions differ by agency:

- **Nijisanji:** historical generations (`1st Gen`, `2nd Gen`, `EX-GAMERS`, `SEEDs Gen 1`, `SEEDs Gen 2`) or named debut cohorts (`Sanbaka`, `Idios`, `VOLTACTION`, `Luxiem`, etc.). Some cohort names were chosen by members after debut; they are not all formal agency divisions. Later collaboration units such as ROF-MAO are not substituted for debut cohorts. Former Korean-branch members retain `KR Gen 4` or `KR Gen 5`. Salome's solo debut has no named generation and remains `NULL`.
- **VSPO!:** original teams (`Lupinus Virtual Games`, `Iris Black Games`, `Cattleya Regina Games`) are historical cohort values. These team divisions are no longer used for current introductions. Later solo and EN members remain `NULL`; later collaboration units and numbered debut-year generations are not invented.
- **Neo-Porte:** numbered generations from the official filtered rosters, including founders as `Gen 0`.
- **774 inc.:** original subgroups (`AniMare`, `HoneyStrap`, `Sugar Lyric`) describe historical membership before their merger into Nanashiinku in March 2023.
- **NoriPro:** `Gen 1`, `Gamers`, and the Creators cohorts `PONuary`, `PARARARE`, and `3me`; founder/solo debutant Inuyama Tamaki remains `NULL`.
- **Mythic Talent:** no generations were found in its public talent-management roster, so these members remain `NULL` (`not_assigned` in the review CSV).
- **Phase Connect:** `OriginS` (Phase 01), `ALiAS` (Phase 02), `Euphoria` (Phase 03), `SAGA` (Phase 04), `Invaders WiSH`, `Invaders QUEST`, and `KALEiDO`. The three Japanese-centered OriginS members retain their original generation rather than replacing it with the later MIRARi sub-unit.

Blank generation values in the review CSV represent SQL `NULL`. `not_applicable` identifies documented solo debuts or members without an assigned team; `not_assigned` records that the agency's public roster supplies no generation. Sources for named cohorts and historical subgroups appear on each reviewed row.

- [HoloList](https://hololist.net/top/): current names, original names, languages, persona gender, agency affiliation, status, and platform links. The linked profile URL is recorded per entry.
- [Official hololive talent roster](https://hololive.hololivepro.com/en/talents/) and [HOLOSTARS talent roster](https://holostars.hololivepro.com/en/talent/): Japanese names and generation/group membership where available. The specific filtered roster URL is recorded per generation.
- [VDB](https://github.com/dd-center/vdb), via its [published list](https://vdb.vtbs.moe/json/list.json): supplemental Japanese names matched by an official YouTube channel ID.
- [xoltia/vtuber-database, release 2024-09-14](https://github.com/xoltia/vtuber-database/releases/tag/2024-09-14): canonical YouTube IDs are reused only when the snapshot's handle matches a current directory link; this is a historical ID lookup, **not** a fresh YouTube API verification of those IDs. Its explicit persona gender labels support the six `NB` mappings described above. Other IDs come from a current direct channel link or public YouTube channel-page metadata.
- [DecAPI Twitch ID resolver](https://docs.decapi.me/): numeric Twitch IDs resolved from directory-listed logins; each resolver URL is recorded in the CSV.
- Current membership is sourced from the [Nijisanji roster](https://www.nijisanji.jp/talents), [VSPO! roster](https://vspo.jp/member/), [Neo-Porte roster](https://neo-porte.jp/member/), [Nanashiinku roster](https://www.774.ai/talents), [NoriPro roster](https://noripro.jp/), and [Mythic public creator roster](https://www.mythictalent.com/creators). Phase Connect's [generation pages](https://phase-connect.com/phase-01-origins/), [JP site](https://jp.phase-connect.com/phase-jp-kaleido-2/), and [official SAGA collection](https://shop.phase-connect.com/products/phase-saga-the-story-begins-official-model-standee-bundle) support its current groups. Exact links are recorded per roster entry.
- Six new Nanashiinku profiles missing from HoloList use explicit public persona gender/language labels from [VTuber Popularity Lab](https://vtuber.lb-product.com/), matched by official channel ID. Aries Akana's persona gender comes from the creator's [own profile](https://linktr.ee/ariesakana), and Braxophone's public gender/language labels from [Streams Charts](https://streamscharts.com/channels/braxophone). These exceptions are identified per row in the provenance CSV.

These are public factual directory records, not creator biographies. Popularity and affiliations change, and directory data can contain errors. Review the provenance before importing into a production database.
