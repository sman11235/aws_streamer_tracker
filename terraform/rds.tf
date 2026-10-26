resource "aws_db_subnet_group" "postgres" {
  name       = "${local.prefix}-postgres"
  subnet_ids = values(aws_subnet.private)[*].id

  description = "Private subnets for the streamer tracker PostgreSQL database."
}

resource "aws_db_instance" "postgres" {
  identifier = "${local.prefix}-postgres"

  engine         = "postgres"
  engine_version = local.settings.postgres_engine_version
  instance_class = local.settings.postgres_instance_class

  allocated_storage     = local.infrastructure.database.allocated_storage_gb
  max_allocated_storage = local.infrastructure.database.max_allocated_storage_gb
  storage_type          = local.infrastructure.database.storage_type
  storage_encrypted     = local.infrastructure.database.storage_encrypted
  port                  = local.runtime_config.database.port

  db_name  = local.settings.postgres_database_name
  username = local.settings.postgres_master_username

  # RDS generates and stores the master password in Secrets Manager.
  manage_master_user_password = true

  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  publicly_accessible    = local.infrastructure.database.publicly_accessible
  multi_az               = local.infrastructure.database.multi_az

  backup_retention_period = local.settings.postgres_backup_retention_days
  backup_window           = local.infrastructure.database.backup_window
  maintenance_window      = local.infrastructure.database.maintenance_window

  deletion_protection = local.infrastructure.database.deletion_protection
  skip_final_snapshot = local.infrastructure.database.skip_final_snapshot
  apply_immediately   = local.infrastructure.database.apply_immediately
}
