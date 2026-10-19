# aws_streamer_tracker

Tracks subscribers, CCV, and streams for YouTube and Twitch streamers.

## Database

Terraform provisions a private Amazon RDS PostgreSQL instance. Its master password is generated and stored by RDS in Secrets Manager. The database schema is in [`terraform/schema.sql`](terraform/schema.sql); apply it after RDS is available with a PostgreSQL client that can reach the private database (for example, through a VPN, bastion host, or an approved migration job).

Lambda functions receive `DATABASE_HOST`, `DATABASE_PORT`, `DATABASE_NAME`, `DATABASE_SECRET_ARN`, and `DATABASE_SSLMODE`. Their deployment ZIPs must include a PostgreSQL driver and use these settings instead of the removed DynamoDB table environment variables.

The dedicated VPC includes a NAT gateway so VPC-attached Lambda functions can call AWS, YouTube, and Twitch APIs. The NAT gateway incurs AWS charges.
