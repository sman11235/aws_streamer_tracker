# Platform ID uniqueness

`channels.platform_channel_id` is unique across the entire `channels` table, including different platforms. `videos.platform_video_id` is unique across the entire `videos` table, including different channels/platforms. The same literal ID cannot be assigned to two rows in its table. These text comparisons remain case-sensitive.

Local UUID primary keys and foreign keys are unchanged. The existing unique constraints on `(platform, platform_channel_id)` and `(channel_id, platform_video_id)` remain because the discovery and video-upsert SQL use them as conflict targets. An upsert cannot move an existing video to another channel: the new global constraint rejects that conflicting insert.

Fresh installations get these constraints from `db/schema.sql`. Existing installations must apply `db/migrations/003_unique_platform_ids.sql` after outstanding earlier migrations. Terraform/GitHub Actions does not apply SQL migrations.

Check existing data before applying:

```sql
SELECT platform_channel_id, COUNT(*)
FROM channels
GROUP BY platform_channel_id
HAVING COUNT(*) > 1;

SELECT platform_video_id, COUNT(*)
FROM videos
GROUP BY platform_video_id
HAVING COUNT(*) > 1;
```

Both queries should return no rows. If duplicates exist, review their ownership and related history/CCV before deciding how to correct them. The migration does not merge, rename, or delete any records automatically.

Apply the whole file in DBeaver's SQL editor, or use psql from a host that can reach RDS:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/003_unique_platform_ids.sql
```

The migration runs in one transaction and can be reapplied. A duplicate aborts the additions, leaving existing rows intact. If execution stops on an error while using an open DBeaver/psql connection, run `ROLLBACK;` before continuing. After correcting the data, rerun the migration. Adding the constraints takes table locks, so choose a quiet period for an existing production database.
